---
name: eplus-sumachike-balloter
description: >-
  Automate Japanese E+ (イープラス / eplus.jp) smartphone-only Sumachike (スマチケ)
  ticket lottery applications using an Xcode iOS Simulator and Appium XCUITest
  driving genuine Mobile Safari. Use this skill when the user asks to apply for
  E+ Sumachike / smartphone-only serial-code ballots, set up mobile E+ balloting
  on macOS, or handle E+ first-time email OTP verification during automation.
---

# E+ Sumachike iOS Simulator Balloter

Automates E+ (`eplus.jp`) smartphone-only **Sumachike (`スマチケ`)** serial-code lottery applications on macOS by driving genuine Mobile Safari inside an Xcode iOS Simulator through **Appium XCUITest** in native UI mode (`bundleId: com.apple.mobilesafari`).

## Why iOS Simulator + Native XCUITest

- **Desktop Chrome + CDP mobile emulation** unlocks E+'s front-end `sp.atom.eplus.jp` layout, **but** Akamai Bot Manager (`edgesuite.net` / `member.eplus.jp/akam`) blocks the login POST (`Access Denied` / `Error Code 99`) due to TLS/WebGL-vs-User-Agent mismatches and CDP markers.
- **Apple `safaridriver`** sets `navigator.webdriver = true` in Mobile Safari, which Akamai also blocks on `sp.atom.eplus.jp/sys/main.jsp`.
- **Appium XCUITest (`bundleId: com.apple.mobilesafari`)** interacts with clean Mobile Safari strictly via iOS accessibility taps/keystrokes (`navigator.webdriver` stays `false`, real iOS WebKit + Apple TLS stack), passing both E+'s Sumachike mobile check and Akamai.
- **Note on App-Only Receptions (`アプリ限定先行`) & Ticket Download:** Web-based Sumachike ballots can be submitted in the iOS Simulator and downloaded later in the official E+ app on a physical smartphone. Receptions strictly requiring the native E+ app (`アプリ限定`) or venue entry still require a real phone tied to SMS verification.

## Prerequisites (macOS)

1. Install Python dependencies with `uv`:
   ```bash
   uv sync
   ```
2. Install Appium and the XCUITest driver (one-time):
   ```bash
   npm install -g appium
   appium driver install xcuitest
   ```
3. Boot an iPhone Simulator and open the Simulator GUI onscreen (Xcode 27 uses `DeviceHub.app`; older Xcode uses `Simulator.app`):
   ```bash
   xcrun simctl list devices available | grep iPhone
   xcrun simctl boot <IPHONE_UDID>
   open -a Simulator 2>/dev/null || open "$(xcode-select -p)/../Applications/DeviceHub.app"
   ```
4. Start the Appium server daemon:
   ```bash
   appium --port 4723
   ```

## Configuring `config.toml`

Update `config.toml` (never commit real credentials or serial codes to git). Store each account's password in the macOS Keychain with `security add-generic-password -U -s eplus-auto-balloter -a <email> -w` and reference it as `Credentials = { username = "<email>", keychain = "eplus-auto-balloter" }`:

- `URL`: The E+ serial reception URL (e.g., `https://eplus.jp/serial/<event_slug>/`).
- `Codes`: List of serial codes to submit.
- `Sessions`: List of tour stops or sessions matching the text on the E+ page (e.g., `["福岡公演", "愛知公演"]`).
- `SeatKeyword` / `GoodsKeyword`: Substrings matching the seat tier dropdown (`"一般指定席"`) and goods toggle (`"特製グッズ無し"` or `"特製グッズ付き"`).
- `Quantity` & `Renban`:
  - **For Sumachike (`スマチケ`):** Set `Quantity = 2` (or `1`) and **`Renban = 0`**. Sumachike does **not** select companions (`同行者`) at application time—you apply for multiple tickets under your own account and distribute (`分配`) them later in the E+ app after winning.
  - **For non-Sumachike Renban (`連番` / `同行者`):** Set `Quantity = 2` and `Renban = "Family Given"` (or 1-based `No.` from `uv run ios_balloter.py --list-companions` / `eplus_list_companions()`). `ios_balloter.py` matches by NFKC-normalized name in either word order and refuses to submit if a Renban page is missing a companion (`COMPANION_REQUIRED`), if a Sumachike page was mistakenly given a companion (`COMPANION_SELECTOR_NOT_FOUND`), or if the same companion is reused for the same stop (`DUPLICATE_COMPANION`).

## Running Ballots & Handling First-Time Email OTP (`メールアドレス認証`)

The first time you sign into `member.eplus.jp` on a newly booted iOS Simulator, E+ sends a 6-digit email verification code (`メールアドレス認証`) and iOS may show a `"Save Password?"` sheet and/or `"同一端末での複数アカウント利用..."` (`追加認証が必要です`) modal.

[ios_balloter.py](../../../ios_balloter.py) handles all of these automatically (`Save Password?` dismissal, `次へ` modal dismissal, numeric keyboard `Done` dismissal, `登録` click, and blank-redirect recovery) using one of three OTP modes:

1. **Agentic / Non-Interactive Mode (`--otp-file`) — Recommended for AI Agents:**
   Launch `ios_balloter.py` in the background with a private per-user path (avoid world-writable `/tmp`):
   ```bash
   uv run ios_balloter.py --otp-file ~/.cache/auto-balloter/otp.txt --screenshot-prefix ~/.cache/auto-balloter/done
   ```
   - Watch stdout for `>>> [OTP REQUIRED] ... Write the 6-digit code to ~/.cache/auto-balloter/otp.txt`.
   - Retrieve the 6-digit code (ask the user or check their email if authorized) and write it with restrictive permissions:
     ```bash
     (umask 077 && printf '123456' > ~/.cache/auto-balloter/otp.txt)
     ```
   - `ios_balloter.py` immediately detects the file, deletes it, enters the code, completes verification, and finishes all remaining ballots automatically (subsequent ballots reuse the verified Mobile Safari session without needing another OTP).

2. **Automated Gmail OTP Hook (`--otp-cmd`):**
   Use [`gmail_eplus.py`](../../../gmail_eplus.py) with the `gmail` CLI (`readonly` only). `ios_balloter.py` automatically passes `EPLUS_OTP_MIN_TS` so stale OTP emails are ignored:
   ```bash
   uv run ios_balloter.py --otp-cmd "uv run gmail_eplus.py otp --poll 90"
   ```

3. **Interactive Terminal Mode:**
   Run directly in a terminal and type the 6-digit code when prompted:
   ```bash
   uv run ios_balloter.py --screenshot-prefix ~/.cache/auto-balloter/done
   ```

4. **Submitting Specific Code/Stop Pairs Only:**
   ```bash
   uv run ios_balloter.py --pairs <SERIAL_1>:福岡公演 <SERIAL_2>:愛知公演
   ```

## MCP Server Mode (`mcp_server.py`)

When running inside an MCP-enabled agent alongside Gmail / Google Sheets / Discord MCPs, start [mcp_server.py](../../../mcp_server.py) (`uv run mcp_server.py`) to use structured tools instead of CLI polling:

1. Call `sim_status()` (and `sim_boot()` if needed) to verify the iPhone Simulator and Appium are ready.
2. For non-Sumachike Renban ballots, call `eplus_list_companions()` first to verify registered `同行者` names.
3. Call `eplus_submit_ballot(entry_url, serial_code, session_name, seat_keyword, goods_keyword, quantity, companions=..., delivery_keyword=..., dry_run=True)` to verify the final confirmation screen via `sim_screenshot()`, then re-call with `dry_run=False`.
4. If `eplus_submit_ballot()` returns `status == "OTP_REQUIRED"`, call `eplus_gmail_fetch_otp(poll_timeout=60)` (or ask the user if it is another person's account), pass the 6-digit code to `eplus_submit_otp(otp_code=...)`, and re-call `eplus_submit_ballot()`.
5. Call `eplus_check_history()`, `eplus_gmail_receipts()`, and `eplus_gmail_results()` to reconcile receipts, lottery outcomes (`WON`/`LOST`), and conbini payment deadlines (`支払期限` / `払込票番号`).
6. For multi-account or Google Sheets workflows, use `plan_from_sheet_rows()` / `plan_allocate()` / `plan_validate()` to show the user a validated Markdown plan table for approval before calling `plan_execute_next(plan_path, dry_run=...)`. Each completed job returns `sheet_row` and `row_values` for immediate write-back to Google Sheets, and logs to `~/.cache/auto-balloter/filings.jsonl`.
7. Credentials come from the macOS Keychain inside the server process (default service `eplus-auto-balloter`), so passwords never enter LLM tool arguments. Never ask the user to paste a password into chat; ask them to run `security add-generic-password -U -s eplus-auto-balloter -a <email> -w` themselves.

## Validation Steps

After running `ios_balloter.py`:
1. Confirm stdout prints `[SUCCESS] <code> -> <stop>` for every pair.
2. Inspect the saved completion screenshots (`--screenshot-prefix`) to verify **`申込完了`**, event stop, seat tier, ticket count, and total yen amount.
3. Check `apply.log` for full per-application audit logs.
4. Remind the user to confirm all filings in E+'s **申込み履歴** (`https://eplus.jp/jyoukyou/`) and never commit real credentials in `config.toml`.
