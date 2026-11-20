"""Job-list & serial-allocation planner for E+ multi-account / Renban / Sumachike ballots.

Supports:
  - Loading/saving ballot plans as TOML or CSV/TSV (Google Sheets round-trip compatible).
  - Parsing raw 2D Google Sheets row arrays (for direct use with Google Sheets MCP).
  - Allocating a pool of serial codes across accounts and tour stops.
  - Pre-flight constraint validation (serial stop limits, duplicate serial+stop,
    Sumachike vs. Renban companion counts, duplicate person/companion across stops).
  - Sequential multi-account execution (grouped by account: finish Account A ->
    logout -> Account B + owner email OTP) with per-filing audit records.

Usage:
    uv run planner.py validate plan.example.toml
    uv run planner.py run my_plan.toml --dry-run
    uv run planner.py export-csv my_plan.toml my_plan.csv
"""

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
import tomllib
import unicodedata
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from credentials import resolve_credentials
from ios_balloter import (
    EmailOTPRequiredError,
    IOSSafariAutomator,
    _norm_name,
    detect_booted_iphone_udid,
    normalize_companions,
)

CACHE_DIR = Path.home() / ".cache" / "auto-balloter"
FILINGS_LOG = CACHE_DIR / "filings.jsonl"

DONE_STATUSES = {"applied", "won", "lost", "paid", "completed", "done"}
PENDING_STATUSES = {"", "not filed", "pending", "todo", "queued", "retry"}


@dataclass
class AccountProfile:
    id: str
    username: str = ""
    keychain: str = "eplus-auto-balloter"
    display_name: str = ""

    def person_key(self) -> str:
        return _norm_name(self.display_name or self.username or self.id)


@dataclass
class EventRules:
    entry_url: str
    is_sumachike: bool = True
    max_stops_per_serial: int = 2
    max_qty_per_ballot: int = 2
    allow_duplicate_person_per_stop: bool = False
    allow_cross_renban: bool = False
    delivery_keyword: str = "スマチケ"


@dataclass
class BallotJob:
    job_id: str
    account_id: str
    serial_code: str
    session_name: str
    quantity: int = 2
    seat_keyword: str = "一般指定席"
    goods_keyword: str = "特製グッズ無し"
    companions: List[str] = field(default_factory=list)
    intended_companions: List[str] = field(default_factory=list)
    status: str = "Not filed"
    receipt: str = ""
    screenshot_path: str = ""
    notes: str = ""
    sheet_row: Optional[int] = None
    updated_at: str = ""

    def is_pending(self) -> bool:
        return self.status.strip().lower() in PENDING_STATUSES

    def is_done(self) -> bool:
        return self.status.strip().lower() in DONE_STATUSES


@dataclass
class BallotPlan:
    rules: EventRules
    accounts: Dict[str, AccountProfile] = field(default_factory=dict)
    jobs: List[BallotJob] = field(default_factory=list)


@dataclass
class ValidationResult:
    valid: bool
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
    pending_count: int = 0
    completed_count: int = 0
    accounts_order: List[str] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Validation & Ordering
# ---------------------------------------------------------------------------

def order_jobs_by_account(jobs: Sequence[BallotJob]) -> List[BallotJob]:
    """Group jobs by account_id (preserving first-seen account order) to minimize account switches."""
    account_order: List[str] = []
    buckets: Dict[str, List[BallotJob]] = defaultdict(list)
    for job in jobs:
        if job.account_id not in buckets:
            account_order.append(job.account_id)
        buckets[job.account_id].append(job)
    ordered: List[BallotJob] = []
    for acc_id in account_order:
        ordered.extend(buckets[acc_id])
    return ordered


def validate_plan(
    plan: BallotPlan,
    check_keychain: bool = False,
) -> ValidationResult:
    """Validate all E+ serial, Renban, Sumachike, and multi-account constraints before filing."""
    errors: List[str] = []
    warnings: List[str] = []

    rules = plan.rules
    if not rules.entry_url.startswith("https://") or "eplus.jp" not in rules.entry_url:
        errors.append(f"Invalid entry_url (must be https://*.eplus.jp): {rules.entry_url!r}")

    serial_stops: Dict[str, List[str]] = defaultdict(list)
    serial_stop_seen: Dict[Tuple[str, str], str] = {}
    account_stop_count: Dict[Tuple[str, str], int] = defaultdict(int)
    # Track (stop, person_key) -> list of (job_id, role, partner_key_set)
    stop_people: Dict[Tuple[str, str], List[Tuple[str, str, set]]] = defaultdict(list)

    pending_count = 0
    completed_count = 0
    seen_accounts: List[str] = []

    for job in plan.jobs:
        if job.is_done():
            completed_count += 1
        elif job.is_pending():
            pending_count += 1

        if job.account_id not in seen_accounts:
            seen_accounts.append(job.account_id)

        if not job.serial_code.strip():
            errors.append(f"[{job.job_id}] Missing serial_code")
        if not job.session_name.strip():
            errors.append(f"[{job.job_id}] Missing session_name")
        if not (1 <= job.quantity <= rules.max_qty_per_ballot):
            errors.append(
                f"[{job.job_id}] quantity={job.quantity} out of bounds (1..{rules.max_qty_per_ballot})"
            )

        # Account registration check
        acc = plan.accounts.get(job.account_id)
        if plan.accounts and acc is None:
            errors.append(f"[{job.job_id}] Unknown account_id {job.account_id!r}")
        elif check_keychain and job.is_pending() and acc is not None:
            u, p = resolve_credentials({"username": acc.username, "keychain": acc.keychain})
            if not (u and p):
                warnings.append(
                    f"Account {acc.id!r} ({acc.username or 'default'}) has no password in Keychain "
                    f"service {acc.keychain!r}"
                )

        # Serial constraints (check across both already-filed and pending jobs!)
        code_norm = job.serial_code.strip().lower()
        stop_norm = unicodedata.normalize("NFKC", job.session_name).strip()
        key_cs = (code_norm, stop_norm)
        if key_cs in serial_stop_seen:
            errors.append(
                f"[{job.job_id}] Duplicate serial+stop ({job.serial_code} -> {job.session_name}); "
                f"already in [{serial_stop_seen[key_cs]}]"
            )
        else:
            serial_stop_seen[key_cs] = job.job_id
            serial_stops[code_norm].append(stop_norm)

        # Sumachike vs. Renban companion rules
        comps = [str(c).strip() for c in normalize_companions(job.companions)]
        if rules.is_sumachike:
            if comps:
                errors.append(
                    f"[{job.job_id}] Sumachike ballots do not take 同行者 at filing time; "
                    f"clear companions and record {comps} in intended_companions for post-win 分配"
                )
        else:
            if job.quantity > 1 and len(comps) != job.quantity - 1:
                errors.append(
                    f"[{job.job_id}] Non-Sumachike Renban ballot with quantity={job.quantity} "
                    f"requires {job.quantity - 1} companion(s), got {len(comps)}"
                )
            elif job.quantity == 1 and comps:
                errors.append(
                    f"[{job.job_id}] Solo ballot (quantity=1) should not specify companions: {comps}"
                )

        # Multi-filing / duplicate-person checks per stop
        account_stop_count[(job.account_id, stop_norm)] += 1
        filer_key = acc.person_key() if acc else _norm_name(job.account_id)
        comp_keys = {_norm_name(c) for c in comps}
        if filer_key in comp_keys:
            errors.append(f"[{job.job_id}] Filing account {job.account_id!r} is also listed as its own companion")

        stop_people[(stop_norm, filer_key)].append((job.job_id, "filer", comp_keys))
        for c in comps:
            stop_people[(stop_norm, _norm_name(c))].append((job.job_id, "companion", {filer_key}))

    # Serial stop-count limits
    for code_norm, stops in serial_stops.items():
        if len(stops) > rules.max_stops_per_serial:
            errors.append(
                f"Serial {code_norm!r} assigned to {len(stops)} stops ({stops}), "
                f"exceeding max_stops_per_serial={rules.max_stops_per_serial}"
            )

    # Same account filing multiple serials for the same stop
    for (acc_id, stop_norm), cnt in account_stop_count.items():
        if cnt > 1:
            if rules.is_sumachike:
                warnings.append(
                    f"Account {acc_id!r} has {cnt} filings for '{stop_norm}'. Allowed on serial lotteries, "
                    "but note Sumachike 親チケ (lead ticket) cannot be transferred if multiple entries win."
                )
            elif not rules.allow_duplicate_person_per_stop:
                warnings.append(
                    f"Account {acc_id!r} has {cnt} non-Sumachike filings for '{stop_norm}'; "
                    "verify whether this event allows multiple entries per account."
                )

    # Duplicate person across accounts / companions on the same stop (Renban 重複無効 protection)
    if not rules.is_sumachike and not rules.allow_duplicate_person_per_stop:
        for (stop_norm, pkey), entries in stop_people.items():
            if len(entries) <= 1:
                continue
            # Check if this is purely a cross-renban pair (A+B and B+A) when allow_cross_renban=True
            roles = {e[1] for e in entries}
            if rules.allow_cross_renban and len(entries) == 2 and roles == {"filer", "companion"}:
                continue
            job_refs = ", ".join(f"{jid}({role})" for jid, role, _ in entries)
            errors.append(
                f"Duplicate person '{pkey}' on '{stop_norm}' across {job_refs} "
                "(risk of 重複無効; set allow_cross_renban=True or allow_duplicate_person_per_stop=True if allowed)"
            )

    return ValidationResult(
        valid=len(errors) == 0,
        errors=errors,
        warnings=warnings,
        pending_count=pending_count,
        completed_count=completed_count,
        accounts_order=seen_accounts,
    )


# ---------------------------------------------------------------------------
# Serial Auto-Allocator
# ---------------------------------------------------------------------------

def allocate_serials(
    serials: Sequence[Dict[str, Any]],
    accounts: Sequence[AccountProfile],
    target_stops: Sequence[str],
    rules: EventRules,
    default_quantity: int = 2,
    seat_keyword: str = "一般指定席",
    goods_keyword: str = "特製グッズ無し",
) -> BallotPlan:
    """Distribute a pool of serial codes across accounts and stops respecting event rules.

    Each item in `serials` can be:
      {"code": "abc12345", "owner": "primary", "stops": ["福岡公演", "愛知公演"],
       "quantity": 2, "companions": [], "intended_companions": ["Friend B"], "account_id": "primary"}
    """
    acc_map = {a.id: a for a in accounts}
    acc_ids = [a.id for a in accounts]
    if not acc_ids:
        acc_map["primary"] = AccountProfile(id="primary")
        acc_ids = ["primary"]

    # Track load per (account_id, stop) to balance allocations evenly
    load: Dict[Tuple[str, str], int] = defaultdict(int)
    total_load: Dict[str, int] = defaultdict(int)
    jobs: List[BallotJob] = []

    for idx, item in enumerate(serials, start=1):
        code = str(item["code"]).strip()
        stops = list(item.get("stops") or target_stops)[: rules.max_stops_per_serial]
        qty = int(item.get("quantity", default_quantity))
        explicit_acc = item.get("account_id")
        owner_acc = item.get("owner")
        comps = [str(c) for c in normalize_companions(item.get("companions"))]
        intended = [str(c) for c in normalize_companions(item.get("intended_companions"))]
        notes = str(item.get("notes", ""))

        for s_idx, stop in enumerate(stops, start=1):
            if explicit_acc and explicit_acc in acc_map:
                chosen_acc = explicit_acc
            elif owner_acc and owner_acc in acc_map and load[(owner_acc, stop)] == 0:
                chosen_acc = owner_acc
            else:
                # Pick the account with the fewest filings on this stop, then fewest total filings
                chosen_acc = min(acc_ids, key=lambda aid: (load[(aid, stop)], total_load[aid]))

            load[(chosen_acc, stop)] += 1
            total_load[chosen_acc] += 1
            jobs.append(
                BallotJob(
                    job_id=f"job_{idx}_{s_idx}",
                    account_id=chosen_acc,
                    serial_code=code,
                    session_name=stop,
                    quantity=qty,
                    seat_keyword=str(item.get("seat_keyword", seat_keyword)),
                    goods_keyword=str(item.get("goods_keyword", goods_keyword)),
                    companions=comps if not rules.is_sumachike else [],
                    intended_companions=intended if rules.is_sumachike else [],
                    status=str(item.get("status", "Not filed")),
                    notes=notes,
                )
            )

    return BallotPlan(rules=rules, accounts=acc_map, jobs=order_jobs_by_account(jobs))


# ---------------------------------------------------------------------------
# Google Sheets / CSV / TOML Serialization
# ---------------------------------------------------------------------------

SHEET_HEADERS = [
    "JobID",
    "Account",
    "Serial",
    "Stop",
    "Qty",
    "Seat",
    "Goods",
    "Companion",
    "IntendedCompanion",
    "Status",
    "Receipt",
    "Screenshot",
    "Notes",
]

_HEADER_ALIASES: Dict[str, str] = {
    "jobid": "job_id",
    "job_id": "job_id",
    "id": "job_id",
    "account": "account_id",
    "account_id": "account_id",
    "アカウント": "account_id",
    "filer": "account_id",
    "serial": "serial_code",
    "serial_code": "serial_code",
    "code": "serial_code",
    "シリアル": "serial_code",
    "シリアルナンバー": "serial_code",
    "stop": "session_name",
    "session": "session_name",
    "session_name": "session_name",
    "公演": "session_name",
    "会場": "session_name",
    "qty": "quantity",
    "quantity": "quantity",
    "枚数": "quantity",
    "seat": "seat_keyword",
    "seat_keyword": "seat_keyword",
    "席種": "seat_keyword",
    "goods": "goods_keyword",
    "goods_keyword": "goods_keyword",
    "グッズ": "goods_keyword",
    "companion": "companions",
    "companions": "companions",
    "renban": "companions",
    "同行者": "companions",
    "連番": "companions",
    "intendedcompanion": "intended_companions",
    "intended_companion": "intended_companions",
    "intended_companions": "intended_companions",
    "分配先": "intended_companions",
    "status": "status",
    "ステータス": "status",
    "状態": "status",
    "receipt": "receipt",
    "受付番号": "receipt",
    "screenshot": "screenshot_path",
    "screenshot_path": "screenshot_path",
    "notes": "notes",
    "memo": "notes",
    "備考": "notes",
}


def _split_list_cell(val: str) -> List[str]:
    parts = [p.strip() for p in re.split(r"[,;/、]+", val or "") if p.strip()]
    return [p for p in parts if p.lower() not in ("none", "n/a", "-", "0")]


def jobs_from_sheet_rows(
    rows: Sequence[Sequence[Any]],
    default_account_id: str = "primary",
    default_seat: str = "一般指定席",
    default_goods: str = "特製グッズ無し",
    default_quantity: int = 2,
    is_sumachike: bool = True,
) -> List[BallotJob]:
    """Parse a 2D Google Sheets / CSV table (header row + data rows) into BallotJobs."""
    if not rows:
        return []

    raw_headers = [str(c).strip() for c in rows[0]]
    col_map: Dict[int, str] = {}
    for idx, h in enumerate(raw_headers):
        key = re.sub(r"[\s\-]+", "", h.lower())
        canonical = _HEADER_ALIASES.get(key) or _HEADER_ALIASES.get(h.lower()) or _HEADER_ALIASES.get(h)
        if canonical:
            col_map[idx] = canonical

    jobs: List[BallotJob] = []
    for row_idx, row in enumerate(rows[1:], start=2):
        if not any(str(c).strip() for c in row):
            continue
        rec: Dict[str, str] = {}
        for c_idx, field_name in col_map.items():
            if c_idx < len(row):
                rec[field_name] = str(row[c_idx]).strip()

        serial = rec.get("serial_code", "")
        stop = rec.get("session_name", "")
        if not serial and not stop:
            continue

        qty_raw = rec.get("quantity", "")
        qty_match = re.search(r"\d+", qty_raw)
        qty = int(qty_match.group(0)) if qty_match else default_quantity

        comp_list = _split_list_cell(rec.get("companions", ""))
        intended_list = _split_list_cell(rec.get("intended_companions", ""))
        # On Sumachike sheets, users often put the post-win companion in the "Companion" column
        if is_sumachike and comp_list and not intended_list:
            intended_list = comp_list
            comp_list = []

        jobs.append(
            BallotJob(
                job_id=rec.get("job_id") or f"row_{row_idx}",
                account_id=rec.get("account_id") or default_account_id,
                serial_code=serial,
                session_name=stop,
                quantity=qty,
                seat_keyword=rec.get("seat_keyword") or default_seat,
                goods_keyword=rec.get("goods_keyword") or default_goods,
                companions=comp_list,
                intended_companions=intended_list,
                status=rec.get("status") or "Not filed",
                receipt=rec.get("receipt", ""),
                screenshot_path=rec.get("screenshot_path", ""),
                notes=rec.get("notes", ""),
                sheet_row=row_idx,
            )
        )
    return jobs


def job_to_sheet_row(job: BallotJob) -> List[str]:
    return [
        job.job_id,
        job.account_id,
        job.serial_code,
        job.session_name,
        str(job.quantity),
        job.seat_keyword,
        job.goods_keyword,
        ", ".join(job.companions),
        ", ".join(job.intended_companions),
        job.status,
        job.receipt,
        job.screenshot_path,
        job.notes,
    ]


def jobs_to_sheet_rows(jobs: Sequence[BallotJob]) -> List[List[str]]:
    return [list(SHEET_HEADERS)] + [job_to_sheet_row(j) for j in jobs]


def load_plan_toml(path: str | Path) -> BallotPlan:
    p = Path(path)
    with p.open("rb") as fd:
        data = tomllib.load(fd)

    ev = data.get("Event") or {}
    is_suma = bool(ev.get("IsSumachike", True))
    rules = EventRules(
        entry_url=ev.get("URL") or data.get("URL", ""),
        is_sumachike=is_suma,
        max_stops_per_serial=int(ev.get("MaxStopsPerSerial", 2)),
        max_qty_per_ballot=int(ev.get("MaxQtyPerBallot", 2)),
        allow_duplicate_person_per_stop=bool(ev.get("AllowDuplicatePersonPerStop", False)),
        allow_cross_renban=bool(ev.get("AllowCrossRenban", False)),
        delivery_keyword=ev.get("DeliveryKeyword") or ("スマチケ" if is_suma else "default"),
    )

    accounts: Dict[str, AccountProfile] = {}
    for acc_id, cfg in (data.get("Accounts") or {}).items():
        accounts[acc_id] = AccountProfile(
            id=acc_id,
            username=str(cfg.get("username", "")),
            keychain=str(cfg.get("keychain", "eplus-auto-balloter")),
            display_name=str(cfg.get("display_name", acc_id)),
        )

    jobs: List[BallotJob] = []
    for idx, j in enumerate(data.get("Jobs") or [], start=1):
        jobs.append(
            BallotJob(
                job_id=str(j.get("id") or f"job_{idx}"),
                account_id=str(j.get("account", "primary")),
                serial_code=str(j["serial"]),
                session_name=str(j["stop"]),
                quantity=int(j.get("quantity", 2)),
                seat_keyword=str(j.get("seat", ev.get("DefaultSeat", "一般指定席"))),
                goods_keyword=str(j.get("goods", ev.get("DefaultGoods", "特製グッズ無し"))),
                companions=[str(c) for c in normalize_companions(j.get("companions") or j.get("renban"))],
                intended_companions=[str(c) for c in normalize_companions(j.get("intended_companions"))],
                status=str(j.get("status", "Not filed")),
                receipt=str(j.get("receipt", "")),
                screenshot_path=str(j.get("screenshot_path", "")),
                notes=str(j.get("notes", "")),
                sheet_row=j.get("sheet_row"),
                updated_at=str(j.get("updated_at", "")),
            )
        )
    return BallotPlan(rules=rules, accounts=accounts, jobs=jobs)


def _toml_str(val: str) -> str:
    return json.dumps(val, ensure_ascii=False)


def save_plan_toml(path: str | Path, plan: BallotPlan) -> None:
    lines: List[str] = [
        "[Event]",
        f"URL = {_toml_str(plan.rules.entry_url)}",
        f"IsSumachike = {str(plan.rules.is_sumachike).lower()}",
        f"MaxStopsPerSerial = {plan.rules.max_stops_per_serial}",
        f"MaxQtyPerBallot = {plan.rules.max_qty_per_ballot}",
        f"AllowDuplicatePersonPerStop = {str(plan.rules.allow_duplicate_person_per_stop).lower()}",
        f"AllowCrossRenban = {str(plan.rules.allow_cross_renban).lower()}",
        f"DeliveryKeyword = {_toml_str(plan.rules.delivery_keyword)}",
        "",
    ]
    for acc in plan.accounts.values():
        lines.extend(
            [
                f"[Accounts.{acc.id}]",
                f"username = {_toml_str(acc.username)}",
                f"keychain = {_toml_str(acc.keychain)}",
                f"display_name = {_toml_str(acc.display_name)}",
                "",
            ]
        )
    for job in plan.jobs:
        comps_lit = "[" + ", ".join(_toml_str(c) for c in job.companions) + "]"
        intended_lit = "[" + ", ".join(_toml_str(c) for c in job.intended_companions) + "]"
        lines.extend(
            [
                "[[Jobs]]",
                f"id = {_toml_str(job.job_id)}",
                f"account = {_toml_str(job.account_id)}",
                f"serial = {_toml_str(job.serial_code)}",
                f"stop = {_toml_str(job.session_name)}",
                f"quantity = {job.quantity}",
                f"seat = {_toml_str(job.seat_keyword)}",
                f"goods = {_toml_str(job.goods_keyword)}",
                f"companions = {comps_lit}",
                f"intended_companions = {intended_lit}",
                f"status = {_toml_str(job.status)}",
                f"receipt = {_toml_str(job.receipt)}",
                f"screenshot_path = {_toml_str(job.screenshot_path)}",
                f"notes = {_toml_str(job.notes)}",
                f"updated_at = {_toml_str(job.updated_at)}",
                "",
            ]
        )
    Path(path).write_text("\n".join(lines), encoding="utf-8")


def load_plan_csv(
    path: str | Path,
    entry_url: str = "",
    is_sumachike: bool = True,
) -> BallotPlan:
    p = Path(path)
    with p.open("r", encoding="utf-8", newline="") as fd:
        rows = list(csv.reader(fd))
    jobs = jobs_from_sheet_rows(rows, is_sumachike=is_sumachike)
    accounts = {
        j.account_id: AccountProfile(id=j.account_id, display_name=j.account_id)
        for j in jobs
    }
    return BallotPlan(
        rules=EventRules(entry_url=entry_url, is_sumachike=is_sumachike),
        accounts=accounts,
        jobs=jobs,
    )


def save_plan_csv(path: str | Path, plan: BallotPlan) -> None:
    p = Path(path)
    with p.open("w", encoding="utf-8", newline="") as fd:
        writer = csv.writer(fd)
        writer.writerows(jobs_to_sheet_rows(plan.jobs))


def load_plan(path: str | Path, entry_url: str = "", is_sumachike: bool = True) -> BallotPlan:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        return load_plan_csv(p, entry_url=entry_url, is_sumachike=is_sumachike)
    return load_plan_toml(p)


def save_plan(path: str | Path, plan: BallotPlan) -> None:
    p = Path(path)
    if p.suffix.lower() == ".csv":
        save_plan_csv(p, plan)
    else:
        save_plan_toml(p, plan)


# ---------------------------------------------------------------------------
# Audit Logging & Markdown Rendering
# ---------------------------------------------------------------------------

def append_filing_audit(job: BallotJob, entry_url: str, account_email: str) -> None:
    """Append a structured JSONL audit record to ~/.cache/auto-balloter/filings.jsonl."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(CACHE_DIR, 0o700)
    except OSError:
        pass
    record = {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "entry_url": entry_url,
        "account_id": job.account_id,
        "account_email": account_email,
        "job_id": job.job_id,
        "serial_code": job.serial_code,
        "session_name": job.session_name,
        "quantity": job.quantity,
        "companions": job.companions,
        "intended_companions": job.intended_companions,
        "status": job.status,
        "receipt": job.receipt,
        "screenshot_path": job.screenshot_path,
    }
    with FILINGS_LOG.open("a", encoding="utf-8") as fd:
        fd.write(json.dumps(record, ensure_ascii=False) + "\n")


def format_plan_markdown(plan: BallotPlan, validation: ValidationResult) -> str:
    """Render a human-readable Markdown plan preview for approval before filing."""
    ordered = order_jobs_by_account(plan.jobs)
    mode = "Sumachike (スマチケ — post-win 分配)" if plan.rules.is_sumachike else "Renban (連番 — 同行者 at filing)"
    lines = [
        f"### Ballot Plan ({mode})",
        f"- **URL:** {plan.rules.entry_url}",
        f"- **Pending:** {validation.pending_count} | **Completed:** {validation.completed_count} | **Valid:** {'Yes' if validation.valid else 'No'}",
        f"- **Account execution order:** {' -> '.join(validation.accounts_order) or '(none)'}",
        "",
        "| ID | Account | Serial | Stop | Qty | Companion / 分配先 | Status | Receipt |",
        "| :--- | :--- | :--- | :--- | :---: | :--- | :--- | :--- |",
    ]
    for j in ordered:
        comp_display = ", ".join(j.companions or j.intended_companions) or "—"
        lines.append(
            f"| `{j.job_id}` | `{j.account_id}` | `{j.serial_code}` | {j.session_name} | "
            f"{j.quantity} | {comp_display} | {j.status} | {j.receipt or '—'} |"
        )
    if validation.errors:
        lines.append("\n**Errors (blocking):**")
        for err in validation.errors:
            lines.append(f"- {err}")
    if validation.warnings:
        lines.append("\n**Warnings:**")
        for w in validation.warnings:
            lines.append(f"- {w}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Sequential Multi-Account Execution
# ---------------------------------------------------------------------------

def execute_job(
    auto: IOSSafariAutomator,
    plan: BallotPlan,
    job: BallotJob,
    previous_account_id: Optional[str] = None,
    dry_run: bool = False,
    screenshot_dir: Optional[Path] = None,
) -> Tuple[str, Dict[str, Any]]:
    """Run one BallotJob on an active IOSSafariAutomator, logging out cleanly on account switch."""
    acc = plan.accounts.get(job.account_id) or AccountProfile(id=job.account_id)
    username, password = resolve_credentials({"username": acc.username, "keychain": acc.keychain})
    if not (username and password):
        return "FAILED", {
            "job_id": job.job_id,
            "account_id": job.account_id,
            "detail": (
                f"No Keychain credentials found for account {acc.id!r} "
                f"(service={acc.keychain!r}, user={acc.username!r})."
            ),
        }

    if previous_account_id is not None and previous_account_id != job.account_id:
        auto.logout()

    shot_dir = screenshot_dir or (CACHE_DIR / "screenshots")
    shot_dir.mkdir(parents=True, exist_ok=True)
    ts = int(time.time())
    prefix = str(shot_dir / f"plan_{job.job_id}_{ts}")
    suffix = "_dryrun.png" if dry_run else ".png"
    expected_shot = f"{prefix}_{job.serial_code}_{job.session_name}{suffix}"

    try:
        ok, info = auto.apply_single(
            entry_url=plan.rules.entry_url,
            code=job.serial_code,
            session_name=job.session_name,
            seat_keyword=job.seat_keyword,
            goods_keyword=job.goods_keyword,
            quantity=job.quantity,
            username=username,
            password=password,
            screenshot_prefix=prefix,
            dry_run=dry_run,
            companions=job.companions,
            delivery_keyword=plan.rules.delivery_keyword,
        )
    except EmailOTPRequiredError as exc:
        return "OTP_REQUIRED", {
            "job_id": job.job_id,
            "account_id": job.account_id,
            "account_email": username,
            "detail": str(exc),
            "next_step": (
                f"Account '{job.account_id}' ({username}) requires a 6-digit E+ email OTP. "
                "Obtain the code from the account owner, call eplus_submit_otp(otp_code), "
                "and re-run plan_execute_next()."
            ),
        }

    now_iso = datetime.now(timezone.utc).isoformat()
    job.updated_at = now_iso
    if Path(expected_shot).exists():
        job.screenshot_path = expected_shot

    if ok and info == "DRY_RUN_READY":
        job.status = "Dry run"
        append_filing_audit(job, plan.rules.entry_url, username)
        return "DRY_RUN_READY", {
            "job_id": job.job_id,
            "account_id": job.account_id,
            "status": job.status,
            "screenshot_path": job.screenshot_path,
            "sheet_row": job.sheet_row,
            "row_values": job_to_sheet_row(job),
        }

    if ok:
        job.status = "Applied"
        job.receipt = info
        append_filing_audit(job, plan.rules.entry_url, username)
        return "COMPLETED", {
            "job_id": job.job_id,
            "account_id": job.account_id,
            "status": job.status,
            "receipt": job.receipt,
            "screenshot_path": job.screenshot_path,
            "sheet_row": job.sheet_row,
            "row_values": job_to_sheet_row(job),
        }

    job.status = f"Failed: {info[:80]}"
    append_filing_audit(job, plan.rules.entry_url, username)
    return "FAILED", {
        "job_id": job.job_id,
        "account_id": job.account_id,
        "detail": info,
        "screenshot_path": job.screenshot_path,
        "sheet_row": job.sheet_row,
        "row_values": job_to_sheet_row(job),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="E+ Multi-Account Job-List & Serial-Allocation Planner")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_val = sub.add_parser("validate", help="Validate a TOML or CSV ballot plan and print preview")
    p_val.add_argument("plan", help="Path to plan .toml or .csv")
    p_val.add_argument("--url", default="", help="Event URL (required for CSV plans)")
    p_val.add_argument("--renban", action="store_true", help="Treat CSV plan as non-Sumachike Renban")
    p_val.add_argument("--check-keychain", action="store_true", help="Verify macOS Keychain entries")

    p_run = sub.add_parser("run", help="Execute pending jobs in a plan sequentially by account")
    p_run.add_argument("plan", help="Path to plan .toml or .csv (updated in place after each job)")
    p_run.add_argument("--url", default="", help="Event URL (required for CSV plans)")
    p_run.add_argument("--renban", action="store_true", help="Treat CSV plan as non-Sumachike Renban")
    p_run.add_argument("--dry-run", action="store_true", help="Stop before 同意して申込み on each job")
    p_run.add_argument("--limit", type=int, default=0, help="Max pending jobs to run (0 = all)")
    p_run.add_argument("--udid", default=None, help="Booted iOS Simulator UDID")
    p_run.add_argument("--otp-file", default=None, help="Private OTP handoff file path")
    p_run.add_argument("--otp-cmd", default=None, help="Trusted local OTP command")

    p_exp = sub.add_parser("export-csv", help="Export a TOML plan to Google-Sheets-ready CSV")
    p_exp.add_argument("plan", help="Input TOML plan path")
    p_exp.add_argument("output", help="Output CSV path")

    args = parser.parse_args()

    logging.basicConfig(
        filename="apply.log",
        level=logging.INFO,
        format="%(asctime)s %(message)s",
        filemode="a",
    )

    if args.cmd == "validate":
        plan = load_plan(args.plan, entry_url=args.url, is_sumachike=not args.renban)
        val = validate_plan(plan, check_keychain=args.check_keychain)
        print(format_plan_markdown(plan, val))
        sys.exit(0 if val.valid else 1)

    if args.cmd == "export-csv":
        plan = load_plan_toml(args.plan)
        save_plan_csv(args.output, plan)
        print(f"Exported {len(plan.jobs)} jobs to {args.output}")
        return

    if args.cmd == "run":
        plan = load_plan(args.plan, entry_url=args.url, is_sumachike=not args.renban)
        val = validate_plan(plan, check_keychain=True)
        print(format_plan_markdown(plan, val))
        if not val.valid:
            print("\nAborting: fix validation errors before running.", file=sys.stderr)
            sys.exit(1)

        pending = [j for j in order_jobs_by_account(plan.jobs) if j.is_pending()]
        if args.limit > 0:
            pending = pending[: args.limit]
        if not pending:
            print("\nNo pending jobs to run.")
            return

        udid = args.udid or detect_booted_iphone_udid()
        prev_acc: Optional[str] = None
        with IOSSafariAutomator(
            udid=udid,
            otp_file=args.otp_file,
            otp_cmd=args.otp_cmd,
        ) as auto:
            for job in pending:
                status, detail = execute_job(
                    auto=auto,
                    plan=plan,
                    job=job,
                    previous_account_id=prev_acc,
                    dry_run=args.dry_run,
                )
                prev_acc = job.account_id
                save_plan(args.plan, plan)
                print(f"[{status}] {job.job_id} ({job.account_id}) {job.serial_code} -> {job.session_name}: {detail}")


if __name__ == "__main__":
    main()
