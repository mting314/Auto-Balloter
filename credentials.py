"""Resolve E+ login credentials without storing passwords in config.toml.

Lookup order for each [[Ballots]] entry:
  1. macOS Keychain, if the entry sets `keychain = "<service>"` (or EPLUS_KEYCHAIN_SERVICE is set).
     `username` is optional; if omitted, the first account stored under that service is used.
  2. EPLUS_USERNAME / EPLUS_PASSWORD environment variables (single-account override).
  3. Plaintext `username` / `password` in config.toml (legacy; discouraged).

Store a password once with:
    security add-generic-password -U -s eplus-auto-balloter -a you@example.com -w
"""

import os
import re
import subprocess
import sys
from typing import Any, Dict, Optional, Tuple

PLACEHOLDER_USERNAMES = {"an@example.com", "you@example.com", "placeholder", ""}


def _keychain_account(service: str) -> Optional[str]:
    """Return the account name of the first generic password stored under `service`."""
    if sys.platform != "darwin":
        return None
    res = subprocess.run(
        ["security", "find-generic-password", "-s", service],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None
    match = re.search(r'"acct"<blob>="([^"]*)"', res.stdout)
    return match.group(1) if match else None


def keychain_password(service: str, username: Optional[str] = None) -> Tuple[Optional[str], Optional[str]]:
    """Read (username, password) from the macOS login Keychain. Returns (None, None) if missing."""
    if sys.platform != "darwin":
        return None, None
    account = username if username and username not in PLACEHOLDER_USERNAMES else _keychain_account(service)
    if not account:
        return None, None
    res = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True,
        text=True,
    )
    if res.returncode != 0:
        return None, None
    password = res.stdout.rstrip("\n")
    return (account, password) if password else (None, None)


def resolve_credentials(creds_cfg: Optional[Dict[str, Any]] = None) -> Tuple[Optional[str], Optional[str]]:
    """Resolve (username, password) for one [[Ballots]] entry's `Credentials` table."""
    creds_cfg = creds_cfg or {}
    cfg_user = creds_cfg.get("username")
    service = creds_cfg.get("keychain") or os.environ.get("EPLUS_KEYCHAIN_SERVICE")

    if service:
        user, pw = keychain_password(service, cfg_user or os.environ.get("EPLUS_USERNAME"))
        if user and pw:
            return user, pw

    env_user = os.environ.get("EPLUS_USERNAME")
    env_pass = os.environ.get("EPLUS_PASSWORD")
    if env_user and env_pass:
        return env_user, env_pass

    cfg_pass = creds_cfg.get("password")
    if cfg_user and cfg_pass and cfg_user not in PLACEHOLDER_USERNAMES:
        return cfg_user, cfg_pass

    return None, None
