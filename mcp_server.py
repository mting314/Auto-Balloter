"""MCP Server for the E+ Sumachike iOS Simulator Auto-Balloter.

Wraps IOSSafariAutomator (Appium XCUITest driving genuine Mobile Safari inside an
Xcode iOS Simulator) as composable Model Context Protocol (MCP) tools so AI agents
can coordinate ballot filings alongside Gmail, Google Sheets, and Discord MCPs.

Run (stdio transport, default for Antigravity / Claude Code / Codex):
    uv run mcp_server.py

Credentials (kept out of LLM tool arguments):
    Set EPLUS_USERNAME and EPLUS_PASSWORD in the MCP server environment, or set
    EPLUS_KEYCHAIN_SERVICE (macOS Keychain generic password) with EPLUS_USERNAME.
"""

import argparse
import atexit
import json
import logging
import os
import re
import subprocess
import threading
import time
import tomllib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlparse

import requests
from mcp.server.mcpserver import Image, MCPServer

from ios_balloter import (
    APPIUM_URL,
    EmailOTPRequiredError,
    IOSSafariAutomator,
    detect_booted_iphone_udid,
    ensure_appium_running,
)

CACHE_DIR = Path.home() / ".cache" / "auto-balloter"
SCREENSHOT_DIR = CACHE_DIR / "screenshots"
ALLOWED_EPLUS_DOMAINS = ("eplus.jp",)


def _ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def _validate_eplus_url(url: str) -> str:
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme != "https":
        raise ValueError(f"Only https:// eplus.jp URLs are allowed (got scheme {parsed.scheme!r})")
    if not any(host == d or host.endswith(f".{d}") for d in ALLOWED_EPLUS_DOMAINS):
        raise ValueError(f"URL host {host!r} is not an eplus.jp domain")
    return url


def _resolve_credentials(
    config_path: str = "config.toml",
    account_index: int = 0,
) -> Tuple[Optional[str], Optional[str]]:
    """Resolve E+ credentials from env vars, macOS Keychain, or config.toml (never LLM args)."""
    env_user = os.environ.get("EPLUS_USERNAME")
    env_pass = os.environ.get("EPLUS_PASSWORD")
    if env_user and env_pass:
        return env_user, env_pass

    keychain_service = os.environ.get("EPLUS_KEYCHAIN_SERVICE")
    if keychain_service and env_user:
        try:
            pw = subprocess.check_output(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    keychain_service,
                    "-a",
                    env_user,
                    "-w",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            ).strip()
            if pw:
                return env_user, pw
        except Exception as exc:
            logging.warning(f"<MCP> macOS Keychain lookup failed: {exc}")

    cfg_file = Path(config_path)
    if cfg_file.exists():
        with cfg_file.open("rb") as fd:
            cfg = tomllib.load(fd)
        ballots = cfg.get("Ballots") or []
        if 0 <= account_index < len(ballots):
            creds = ballots[account_index].get("Credentials") or {}
            user = env_user or creds.get("username")
            pw = env_pass or creds.get("password")
            if user and pw and user != "an@example.com":
                return user, pw

    return env_user, env_pass


class BalloterSessionManager:
    """Manages a lazily attached, thread-safe Appium XCUITest session."""

    def __init__(self, appium_url: str = APPIUM_URL):
        self.appium_url = appium_url.rstrip("/")
        self._lock = threading.Lock()
        self._automator: Optional[IOSSafariAutomator] = None
        atexit.register(self.close)

    def get_automator(self, udid: Optional[str] = None) -> IOSSafariAutomator:
        with self._lock:
            if self._automator is not None:
                # Verify session is still alive
                try:
                    r = requests.get(f"{self._automator.base}/timeouts", timeout=5)
                    if r.ok:
                        return self._automator
                except Exception:
                    pass
                self._automator = None

            target_udid = udid or os.environ.get("SIM_UDID") or detect_booted_iphone_udid()
            otp_cmd = os.environ.get("EPLUS_OTP_CMD") or None
            self._automator = IOSSafariAutomator(
                udid=target_udid,
                appium_url=self.appium_url,
                otp_cmd=otp_cmd,
                defer_otp=(otp_cmd is None),
            )
            return self._automator

    def close(self) -> bool:
        with self._lock:
            if self._automator is None:
                return False
            self._automator.close()
            self._automator = None
            return True


SESSION = BalloterSessionManager(
    appium_url=os.environ.get("APPIUM_URL", APPIUM_URL),
)

mcp = MCPServer(
    name="eplus-auto-balloter",
    instructions=(
        "Automates Japanese E+ (eplus.jp) smartphone-only Sumachike (スマチケ) lottery "
        "applications using genuine Mobile Safari in a booted Xcode iOS Simulator via "
        "Appium XCUITest. Always check sim_status() first. If eplus_submit_ballot() "
        "returns status='OTP_REQUIRED', fetch the 6-digit email code (e.g. via Gmail MCP "
        "or the user), call eplus_submit_otp(otp_code), and then re-call eplus_submit_ballot()."
    ),
)


@mcp.tool()
def sim_status() -> Dict[str, Any]:
    """Check booted iPhone Simulator(s) and Appium XCUITest server health."""
    booted: List[Dict[str, str]] = []
    available: List[Dict[str, str]] = []
    try:
        out = subprocess.check_output(
            ["xcrun", "simctl", "list", "devices", "available", "-j"], text=True
        )
        data = json.loads(out)
        for runtime, devices in data.get("devices", {}).items():
            if "iOS" not in runtime:
                continue
            for dev in devices:
                if "iPhone" not in dev.get("name", ""):
                    continue
                entry = {
                    "name": dev["name"],
                    "udid": dev["udid"],
                    "state": dev.get("state", ""),
                    "runtime": runtime.rsplit(".", 1)[-1],
                }
                if entry["state"] == "Booted":
                    booted.append(entry)
                elif len(available) < 8:
                    available.append(entry)
    except Exception as exc:
        return {"ready": False, "error": f"Failed to query simctl: {exc}"}

    appium_ok = False
    appium_error = None
    try:
        ensure_appium_running(SESSION.appium_url)
        appium_ok = True
    except Exception as exc:
        appium_error = str(exc)

    return {
        "ready": bool(booted) and appium_ok,
        "booted_iphones": booted,
        "available_iphones": available if not booted else [],
        "appium_url": SESSION.appium_url,
        "appium_reachable": appium_ok,
        "appium_error": appium_error,
        "credentials_configured": bool(
            os.environ.get("EPLUS_USERNAME")
            and (os.environ.get("EPLUS_PASSWORD") or os.environ.get("EPLUS_KEYCHAIN_SERVICE"))
        ),
    }


@mcp.tool()
def sim_boot(device_name: str = "iPhone", open_gui: bool = True) -> Dict[str, Any]:
    """Boot an available iPhone Simulator (if none is booted) and optionally open Simulator GUI."""
    try:
        existing = detect_booted_iphone_udid()
        udid = existing
        action = "already_booted"
    except RuntimeError:
        out = subprocess.check_output(
            ["xcrun", "simctl", "list", "devices", "available", "-j"], text=True
        )
        data = json.loads(out)
        udid = None
        picked_name = None
        for runtime, devices in sorted(data.get("devices", {}).items(), reverse=True):
            if "iOS" not in runtime:
                continue
            for dev in devices:
                if device_name.lower() in dev.get("name", "").lower():
                    udid = dev["udid"]
                    picked_name = dev["name"]
                    break
            if udid:
                break
        if not udid:
            return {"ok": False, "error": f"No available iOS Simulator matching {device_name!r}"}
        subprocess.run(["xcrun", "simctl", "boot", udid], check=True)
        time.sleep(3.0)
        action = f"booted:{picked_name}"

    if open_gui:
        res = subprocess.run(["open", "-a", "Simulator"], check=False, capture_output=True)
        if res.returncode != 0:
            xcode_dev = subprocess.check_output(["xcode-select", "-p"], text=True).strip()
            device_hub = Path(xcode_dev).parent / "Applications" / "DeviceHub.app"
            if device_hub.exists():
                subprocess.run(["open", str(device_hub)], check=False)

    return {"ok": True, "udid": udid, "action": action}


@mcp.tool()
def sim_screenshot(label: str = "screen", udid: Optional[str] = None) -> Image:
    """Capture the live booted iOS Simulator screen and return it as a PNG image."""
    target_udid = udid or os.environ.get("SIM_UDID") or detect_booted_iphone_udid()
    _ensure_private_dir(SCREENSHOT_DIR)
    safe_label = re.sub(r"[^a-zA-Z0-9_-]", "_", label)[:40] or "screen"
    dest = SCREENSHOT_DIR / f"{safe_label}_{int(time.time())}.png"
    subprocess.run(
        ["xcrun", "simctl", "io", target_udid, "screenshot", str(dest)],
        check=True,
        capture_output=True,
    )
    return Image(path=dest, format="png")


@mcp.tool()
def sim_page_summary() -> Dict[str, Any]:
    """Summarize visible text, buttons, links, and dropdowns in Mobile Safari."""
    auto = SESSION.get_automator()
    return auto.page_summary()


@mcp.tool()
def eplus_open_url(url: str) -> Dict[str, Any]:
    """Open an https://*.eplus.jp URL in Mobile Safari on the booted iOS Simulator."""
    try:
        validated = _validate_eplus_url(url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}
    auto = SESSION.get_automator()
    auto.open_url(validated)
    return {"ok": True, "url": validated, "summary": auto.page_summary(max_items=40)}


@mcp.tool()
def eplus_submit_ballot(
    entry_url: str,
    serial_code: str,
    session_name: str,
    seat_keyword: str = "一般指定席",
    goods_keyword: str = "特製グッズ無し",
    quantity: int = 2,
    dry_run: bool = False,
    save_screenshot: bool = True,
    config_path: str = "config.toml",
    account_index: int = 0,
) -> Dict[str, Any]:
    """Submit a single E+ Sumachike serial-code ballot via iOS Simulator Mobile Safari.

    Set dry_run=True to stop right before clicking 同意して申込み and inspect the summary.
    If E+ prompts for first-time email OTP (メールアドレス認証), returns status='OTP_REQUIRED'
    without blocking so the agent can fetch the 6-digit code and call eplus_submit_otp().
    """
    try:
        validated_url = _validate_eplus_url(entry_url)
    except ValueError as exc:
        return {"status": "FAILED", "detail": str(exc)}
    if quantity not in (1, 2, 3, 4):
        return {"status": "FAILED", "detail": f"Invalid ticket quantity: {quantity}"}

    username, password = _resolve_credentials(config_path=config_path, account_index=account_index)
    auto = SESSION.get_automator()

    screenshot_prefix: Optional[str] = None
    expected_screenshot: Optional[str] = None
    if save_screenshot:
        _ensure_private_dir(SCREENSHOT_DIR)
        ts = int(time.time())
        screenshot_prefix = str(SCREENSHOT_DIR / f"ballot_{ts}")
        suffix = "_dryrun.png" if dry_run else ".png"
        expected_screenshot = f"{screenshot_prefix}_{serial_code}_{session_name}{suffix}"

    try:
        ok, info = auto.apply_single(
            entry_url=validated_url,
            code=serial_code,
            session_name=session_name,
            seat_keyword=seat_keyword,
            goods_keyword=goods_keyword,
            quantity=quantity,
            username=username,
            password=password,
            screenshot_prefix=screenshot_prefix,
            dry_run=dry_run,
        )
    except EmailOTPRequiredError as exc:
        return {
            "status": "OTP_REQUIRED",
            "detail": str(exc),
            "next_step": (
                "Retrieve the 6-digit E+ verification code from email (e.g. Gmail MCP or ask the user), "
                "call eplus_submit_otp(otp_code='123456'), and then re-call eplus_submit_ballot()."
            ),
        }
    except Exception as exc:
        logging.exception("<MCP> eplus_submit_ballot failed")
        return {"status": "FAILED", "detail": str(exc)}

    if ok and info == "DRY_RUN_READY":
        return {
            "status": "DRY_RUN_READY",
            "serial_code": serial_code,
            "session_name": session_name,
            "quantity": quantity,
            "screenshot_path": expected_screenshot if expected_screenshot and Path(expected_screenshot).exists() else None,
            "detail": "Stopped before clicking 同意して申込み. Inspect sim_screenshot() and re-run with dry_run=False to submit.",
        }

    if ok:
        return {
            "status": "COMPLETED",
            "serial_code": serial_code,
            "session_name": session_name,
            "quantity": quantity,
            "receipt": info,
            "screenshot_path": expected_screenshot if expected_screenshot and Path(expected_screenshot).exists() else None,
        }

    if info in ("USED_CODE", "INVALID_CODE"):
        return {
            "status": info,
            "serial_code": serial_code,
            "session_name": session_name,
            "detail": f"E+ rejected serial code with {info}",
        }

    return {
        "status": "FAILED",
        "serial_code": serial_code,
        "session_name": session_name,
        "detail": info,
        "screenshot_path": expected_screenshot if expected_screenshot and Path(expected_screenshot).exists() else None,
    }


@mcp.tool()
def eplus_submit_otp(otp_code: str) -> Dict[str, Any]:
    """Enter a 6-digit E+ メールアドレス認証 code into the active Mobile Safari screen."""
    auto = SESSION.get_automator()
    try:
        landed_on_ballot, status = auto.submit_email_otp(otp_code)
    except Exception as exc:
        return {"status": "FAILED", "detail": str(exc)}
    return {
        "status": status,
        "landed_on_ballot": landed_on_ballot,
        "next_step": (
            "Device verified. Re-run eplus_submit_ballot() to complete the application "
            "(subsequent applications in this Simulator session will not require OTP)."
        ),
    }


@mcp.tool()
def eplus_check_history(
    config_path: str = "config.toml",
    account_index: int = 0,
) -> Dict[str, Any]:
    """Open E+ 申込み履歴 (https://eplus.jp/jyoukyou/) and summarize submitted ballots / lottery results."""
    username, password = _resolve_credentials(config_path=config_path, account_index=account_index)
    auto = SESSION.get_automator()
    try:
        summary = auto.check_application_history(username=username, password=password)
    except EmailOTPRequiredError as exc:
        return {
            "status": "OTP_REQUIRED",
            "detail": str(exc),
            "next_step": "Call eplus_submit_otp(otp_code='123456') and then re-call eplus_check_history().",
        }
    except Exception as exc:
        return {"status": "FAILED", "detail": str(exc)}

    _ensure_private_dir(SCREENSHOT_DIR)
    shot = SCREENSHOT_DIR / f"history_{int(time.time())}.png"
    auto.screenshot(str(shot))
    return {
        "status": "OK",
        "screenshot_path": str(shot),
        "summary": summary,
    }


@mcp.tool()
def eplus_logout() -> Dict[str, Any]:
    """Log out of member.eplus.jp in Mobile Safari (use before switching E+ accounts)."""
    auto = SESSION.get_automator()
    auto.logout()
    return {"status": "LOGGED_OUT"}


@mcp.tool()
def balloter_close_session() -> Dict[str, Any]:
    """Close the active Appium XCUITest session cleanly."""
    closed = SESSION.close()
    return {"closed": closed}


def main() -> None:
    parser = argparse.ArgumentParser(description="E+ Sumachike Auto-Balloter MCP Server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="MCP transport protocol (default: stdio)",
    )
    parser.add_argument(
        "--appium-url",
        default=os.environ.get("APPIUM_URL", APPIUM_URL),
        help="Appium server URL (default: http://127.0.0.1:4723)",
    )
    args = parser.parse_args()

    logging.basicConfig(
        filename="apply.log",
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        filemode="a",
    )
    SESSION.appium_url = args.appium_url.rstrip("/")
    mcp.run(transport=args.transport)


if __name__ == "__main__":
    main()
