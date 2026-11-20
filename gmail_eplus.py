"""Gmail reader & parser for E+ (イープラス) OTPs, receipts, and lottery results.

Uses the `gmail` CLI (strictly `readonly` commands) to fetch and parse:
  1. First-time login 6-digit email OTP (`【e+より】認証コード通知`)
  2. Ballot confirmation receipts (`【e+より】《重要》申込み完了・抽選結果確認期間のご案内`)
  3. Lottery win/loss results & conbini payment deadlines (`【e+より】当選のご案内` / `抽選結果のご案内`)

Can be used standalone as a CLI or wired directly into `ios_balloter.py --otp-cmd`:
    uv run ios_balloter.py --otp-cmd "uv run gmail_eplus.py otp --poll 90"
"""

import argparse
import email.utils
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional


GMAIL_BIN_FALLBACK = os.path.expanduser("~/.gemini/antigravity/bin/gmail")


def _find_gmail_bin() -> str:
    found = shutil.which("gmail")
    if found:
        return found
    if os.path.exists(GMAIL_BIN_FALLBACK):
        return GMAIL_BIN_FALLBACK
    raise RuntimeError(
        "The `gmail` CLI is not on PATH. Install/enable the Gmail plugin and run its setup script."
    )


def _run_gmail_read(query: str, max_results: int = 5, account: str = "") -> str:
    """Invoke `gmail readonly read` cleanly (never shell-piped, strictly readonly)."""
    gmail_bin = _find_gmail_bin()
    env = os.environ.copy()
    if account:
        env["AGY_ACCOUNT"] = account
    cmd = [gmail_bin, "readonly", "read", "--max", str(max_results), "--", query]
    res = subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=30)
    if res.returncode != 0:
        err = (res.stderr or res.stdout or "").strip()
        raise RuntimeError(f"gmail readonly read failed (exit {res.returncode}): {err[:300]}")
    return res.stdout


@dataclass
class GmailMessage:
    date_raw: str
    timestamp_utc: float
    sender: str
    subject: str
    body: str


_MSG_SPLIT_RE = re.compile(r"(?m)^---\s*Message\s+\d+\s*---\s*$")


def split_gmail_read_output(raw: str) -> List[GmailMessage]:
    """Parse plain-text output from `gmail readonly read` into GmailMessage objects."""
    if not raw or not raw.strip():
        return []
    chunks = _MSG_SPLIT_RE.split(raw)
    messages: List[GmailMessage] = []
    for chunk in chunks:
        chunk = chunk.strip("\r\n")
        if not chunk.strip():
            continue
        # Header block ends at the first `---` divider line
        parts = re.split(r"(?m)^---\s*$", chunk, maxsplit=1)
        header_block = parts[0]
        body = parts[1].replace("\r\n", "\n").strip() if len(parts) > 1 else ""

        headers: Dict[str, str] = {}
        for line in header_block.replace("\r\n", "\n").splitlines():
            if ":" in line:
                k, v = line.split(":", 1)
                headers[k.strip().lower()] = v.strip()

        date_raw = headers.get("date", "")
        ts = 0.0
        if date_raw:
            try:
                dt = email.utils.parsedate_to_datetime(date_raw)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                ts = dt.timestamp()
            except Exception:
                ts = 0.0

        messages.append(
            GmailMessage(
                date_raw=date_raw,
                timestamp_utc=ts,
                sender=headers.get("from", ""),
                subject=headers.get("subject", ""),
                body=body,
            )
        )
    # Sort newest first
    messages.sort(key=lambda m: m.timestamp_utc, reverse=True)
    return messages


def _nfkc(s: str) -> str:
    return re.sub(r"[ \t]+", " ", unicodedata.normalize("NFKC", s or "")).strip()


def _extract_field(body: str, label: str, multiline: bool = False) -> str:
    """Extract a `Label ： Value` field from an E+ plain-text email."""
    pattern = rf"(?m)^[ \u3000]*{re.escape(label)}[ \u3000]*[：:][ \u3000]*(.*)$"
    m = re.search(pattern, body)
    if not m:
        return ""
    value = m.group(1).strip()
    if not multiline:
        return _nfkc(value)
    # Continuation lines on E+ emails are heavily indented with full-width spaces (　)
    lines = [value] if value else []
    rest = body[m.end() :].splitlines()
    for ln in rest[1:] if rest and rest[0] == "" else rest:
        if re.match(r"^[\u3000]{4,}\S", ln) or re.match(r"^[ ]{8,}\S", ln):
            if "：" in ln or ":" in ln:
                break
            lines.append(ln.strip())
        else:
            break
    return _nfkc(" ".join(lines))


def _extract_yen(text: str) -> Optional[int]:
    m = re.search(r"[\\¥￥]\s*([\d,]+)", text or "")
    if not m:
        return None
    return int(m.group(1).replace(",", ""))


# ---------------------------------------------------------------------------
# 1. OTP Parsing & Polling
# ---------------------------------------------------------------------------

_OTP_RE = re.compile(r"認証コード[ \u3000]*[：:][ \u3000]*(\d{6})")


def parse_otp_message(msg: GmailMessage) -> Optional[Dict[str, Any]]:
    """Extract a 6-digit E+ verification code from a single GmailMessage."""
    if "eplus.co.jp" not in msg.sender.lower():
        return None
    m = _OTP_RE.search(msg.body)
    if not m:
        return None
    return {
        "code": m.group(1),
        "date": msg.date_raw,
        "timestamp_utc": msg.timestamp_utc,
        "subject": msg.subject,
    }


def fetch_latest_otp(
    min_timestamp: Optional[float] = None,
    max_age_seconds: int = 600,
    poll_timeout: int = 0,
    poll_interval: float = 3.0,
    account: str = "",
) -> Optional[Dict[str, Any]]:
    """Fetch the newest E+ 6-digit OTP email from Gmail, optionally polling until a fresh one arrives.

    - `min_timestamp`: ignore emails older than this UTC epoch timestamp (also reads EPLUS_OTP_MIN_TS).
    - `max_age_seconds`: ignore emails older than `now - max_age_seconds` (0 disables age check).
    - `poll_timeout`: seconds to keep checking before returning None (0 = single check).
    """
    if min_timestamp is None and os.environ.get("EPLUS_OTP_MIN_TS"):
        try:
            min_timestamp = float(os.environ["EPLUS_OTP_MIN_TS"])
        except ValueError:
            min_timestamp = None

    query = "from:eplus.co.jp subject:認証コード通知"
    deadline = time.time() + max(0, poll_timeout)

    while True:
        raw = _run_gmail_read(query, max_results=3, account=account)
        msgs = split_gmail_read_output(raw)
        now = time.time()
        for msg in msgs:
            parsed = parse_otp_message(msg)
            if not parsed:
                continue
            ts = parsed["timestamp_utc"]
            if min_timestamp and ts and ts < min_timestamp:
                continue
            if max_age_seconds > 0 and ts and (now - ts) > max_age_seconds:
                continue
            parsed["age_seconds"] = round(max(0.0, now - ts), 1) if ts else None
            return parsed

        if time.time() >= deadline:
            return None
        time.sleep(poll_interval)


# ---------------------------------------------------------------------------
# 2. Application Receipt Parsing (申込み完了)
# ---------------------------------------------------------------------------

def parse_receipt_message(msg: GmailMessage) -> Optional[Dict[str, Any]]:
    """Parse an E+ `申込み完了` confirmation email into structured fields."""
    if "eplus.co.jp" not in msg.sender.lower():
        return None
    if "申込み完了" not in msg.subject and "申込み完了" not in msg.body:
        return None

    event_name = _extract_field(msg.body, "公演名", multiline=True)
    venue = _extract_field(msg.body, "会場名")
    show_dt = _extract_field(msg.body, "公演日時")
    seat_info = _extract_field(msg.body, "席種・料金")
    total_raw = _extract_field(msg.body, "料金合計")
    payment = _extract_field(msg.body, "支払方法")
    delivery = _extract_field(msg.body, "受取方法")

    qty_m = re.search(r"[×xX]\s*(\d+)\s*枚", seat_info)
    quantity = int(qty_m.group(1)) if qty_m else None

    window_m = re.search(r"［\s*抽選結果確認期間[ \u3000]*[：:][ \u3000]*([^］]+)］", msg.body)
    result_window = _nfkc(window_m.group(1)) if window_m else ""

    dl_m = re.search(r"※\s*(\d{4}/\d{2}/\d{2}[^\n]*ダウンロード可能[^\n]*)", msg.body)
    download_note = _nfkc(dl_m.group(1)) if dl_m else ""

    return {
        "date": msg.date_raw,
        "timestamp_utc": msg.timestamp_utc,
        "subject": msg.subject,
        "event_name": event_name,
        "venue": venue,
        "show_datetime": show_dt,
        "seat_info": seat_info,
        "quantity": quantity,
        "total_jpy": _extract_yen(total_raw),
        "payment_method": payment,
        "delivery_method": delivery,
        "result_window": result_window,
        "download_note": download_note,
    }


def fetch_receipts(
    event_filter: str = "",
    max_results: int = 10,
    account: str = "",
) -> List[Dict[str, Any]]:
    query = "from:eplus.co.jp subject:申込み完了"
    if event_filter.strip():
        query = f"{query} {event_filter.strip()}"
    raw = _run_gmail_read(query, max_results=max_results, account=account)
    results = []
    for msg in split_gmail_read_output(raw):
        parsed = parse_receipt_message(msg)
        if parsed:
            results.append(parsed)
    return results


# ---------------------------------------------------------------------------
# 3. Lottery Results & Payment Deadline Parsing (当選 / 落選)
# ---------------------------------------------------------------------------

def parse_result_message(msg: GmailMessage) -> Optional[Dict[str, Any]]:
    """Parse an E+ lottery result email (`当選のご案内` / `抽選結果のご案内`)."""
    if "eplus.co.jp" not in msg.sender.lower():
        return None
    if "申込み完了" in msg.subject:
        return None
    is_result = (
        "当選のご案内" in msg.subject
        or "抽選結果" in msg.subject
        or "抽選結果のご案内" in msg.body
    )
    if not is_result:
        return None

    won = (
        "当選のご案内" in msg.subject
        or "ご用意いたしました" in msg.body
        or bool(re.search(r"抽選結果[ \u3000]*当選", msg.body))
    )
    lost = "ご用意できませんでした" in msg.body or "ご用意することができませんでした" in msg.body or "落選" in msg.body
    if won:
        outcome = "WON"
    elif lost:
        outcome = "LOST"
    else:
        outcome = "UNKNOWN"

    event_name = _extract_field(msg.body, "公演名", multiline=True)
    venue = _extract_field(msg.body, "会場名")
    show_dt = _extract_field(msg.body, "公演日時")
    seat_info = _extract_field(msg.body, "席種・料金")
    total_raw = _extract_field(msg.body, "支払金額") or _extract_field(msg.body, "料金合計")
    payment_method = _extract_field(msg.body, "支払方法")
    payment_slip = _extract_field(msg.body, "払込票番号")
    payment_deadline = _extract_field(msg.body, "支払期限")

    qty_m = re.search(r"[×xX]\s*(\d+)\s*枚", seat_info)
    quantity = int(qty_m.group(1)) if qty_m else None

    return {
        "date": msg.date_raw,
        "timestamp_utc": msg.timestamp_utc,
        "subject": msg.subject,
        "outcome": outcome,
        "event_name": event_name,
        "venue": venue,
        "show_datetime": show_dt,
        "seat_info": seat_info,
        "quantity": quantity,
        "total_jpy": _extract_yen(total_raw),
        "payment_method": payment_method,
        "payment_slip_number": payment_slip,
        "payment_deadline": payment_deadline,
    }


def fetch_results(
    event_filter: str = "",
    max_results: int = 10,
    account: str = "",
) -> List[Dict[str, Any]]:
    query = "from:eplus.co.jp -subject:申込み完了 (当選のご案内 OR 抽選結果のご案内)"
    if event_filter.strip():
        query = f"{query} {event_filter.strip()}"
    raw = _run_gmail_read(query, max_results=max_results, account=account)
    results = []
    for msg in split_gmail_read_output(raw):
        parsed = parse_result_message(msg)
        if parsed:
            results.append(parsed)
    return results


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="E+ Gmail OTP, receipt, and lottery result helper")
    parser.add_argument("--account", default="", help="Optional AGY_ACCOUNT email when multiple Gmail accounts are connected")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_otp = sub.add_parser("otp", help="Print the latest 6-digit E+ verification code (compatible with --otp-cmd)")
    p_otp.add_argument("--poll", type=int, default=0, help="Seconds to poll waiting for a fresh OTP email (default: 0)")
    p_otp.add_argument("--max-age", type=int, default=600, help="Max OTP age in seconds (default: 600; 0 disables)")
    p_otp.add_argument("--min-ts", type=float, default=None, help="Ignore OTP emails older than this UTC epoch timestamp")
    p_otp.add_argument("--json", action="store_true", help="Print full JSON metadata instead of bare 6-digit code")

    p_rec = sub.add_parser("receipts", help="List parsed E+ 申込み完了 receipts as JSON")
    p_rec.add_argument("--filter", default="", help="Optional event keyword filter (e.g. Roselia)")
    p_rec.add_argument("--max", type=int, default=10, help="Max emails to inspect (default: 10)")

    p_res = sub.add_parser("results", help="List parsed E+ 当選/落選 lottery results as JSON")
    p_res.add_argument("--filter", default="", help="Optional event keyword filter")
    p_res.add_argument("--max", type=int, default=10, help="Max emails to inspect (default: 10)")

    args = parser.parse_args()

    if args.cmd == "otp":
        otp = fetch_latest_otp(
            min_timestamp=args.min_ts,
            max_age_seconds=args.max_age,
            poll_timeout=args.poll,
            account=args.account,
        )
        if not otp:
            print("No matching E+ OTP email found.", file=sys.stderr)
            sys.exit(1)
        if args.json:
            print(json.dumps(otp, ensure_ascii=False, indent=2))
        else:
            # Bare 6-digit output plugs directly into `ios_balloter.py --otp-cmd`
            print(otp["code"])
        return

    if args.cmd == "receipts":
        items = fetch_receipts(event_filter=args.filter, max_results=args.max, account=args.account)
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return

    if args.cmd == "results":
        items = fetch_results(event_filter=args.filter, max_results=args.max, account=args.account)
        print(json.dumps(items, ensure_ascii=False, indent=2))
        return


if __name__ == "__main__":
    main()
