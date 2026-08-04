"""Audited terminal-missing forward-session evidence for PaperOps."""

from __future__ import annotations

import hashlib
import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.market_calendar import market_session
from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.storage import append_jsonl_unique, read_jsonl

GAP_SCHEMA_VERSION = "v2.paper_ops_forward_session_gap.v1"


def record_forward_session_gap(
    *,
    output_root: Path,
    market_date: str,
    reason_code: str,
) -> dict[str, object]:
    """Record a historical no-run session as missing truth, never zero return."""

    paths = PaperOpsPaths.create(output_root)
    selected = date.fromisoformat(market_date)
    if not market_session(selected).is_trading_day:
        raise ValueError(f"{market_date} is not a market session")
    if selected >= datetime.now(timezone.utc).date():
        raise ValueError("only completed historical sessions can be recorded as gaps")
    normalized_reason = reason_code.strip().lower()
    if not normalized_reason or any(
        character not in "abcdefghijklmnopqrstuvwxyz0123456789_-"
        for character in normalized_reason
    ):
        raise ValueError("reason_code must use lowercase letters, numbers, underscores, or hyphens")
    blockers = _session_evidence(paths, market_date)
    if blockers:
        raise ValueError(
            "cannot record a terminal gap where forward evidence exists: "
            + ", ".join(blockers)
        )
    existing, errors = load_forward_session_gaps(paths)
    if errors:
        raise ValueError("existing forward-session gap ledger is invalid: " + "; ".join(errors))
    same_date = [row for row in existing if row["market_date"] == market_date]
    if same_date:
        if same_date[0]["reason_code"] != normalized_reason:
            raise ValueError("the session already has a conflicting terminal-gap reason")
        return {
            "status": "already_recorded",
            "appended": 0,
            "record": same_date[0],
            "missing_truth_is_zero": False,
            "research_only": True,
            "broker_execution_enabled": False,
        }
    canonical: dict[str, object] = {
        "schema_version": GAP_SCHEMA_VERSION,
        "market_date": market_date,
        "mode": "forward",
        "status": "TERMINAL_MISSING",
        "reason_code": normalized_reason,
        "recorded_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    record = {
        **canonical,
        "record_id": hashlib.sha256(_canonical_bytes(canonical)).hexdigest(),
    }
    appended = append_jsonl_unique(
        paths.state / "forward_session_gaps.jsonl",
        [record],
        "record_id",
    )
    return {
        "status": "recorded",
        "appended": appended,
        "record": record,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def load_forward_session_gaps(
    paths: PaperOpsPaths,
) -> tuple[list[dict[str, Any]], list[str]]:
    rows = read_jsonl(paths.state / "forward_session_gaps.jsonl")
    accepted: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_dates: set[str] = set()
    seen_ids: set[str] = set()
    for index, raw in enumerate(rows, start=1):
        label = f"forward session gap row {index}"
        row = dict(raw)
        record_id = str(row.pop("record_id", ""))
        expected_id = hashlib.sha256(_canonical_bytes(row)).hexdigest()
        if not record_id or record_id != expected_id:
            errors.append(f"{label} record_id integrity mismatch")
        if record_id in seen_ids:
            errors.append(f"{label} duplicates record_id {record_id}")
        seen_ids.add(record_id)
        market_date = str(row.get("market_date") or "")
        try:
            session_date = date.fromisoformat(market_date)
            is_session = market_session(session_date).is_trading_day
        except (ValueError, TypeError):
            is_session = False
        if not is_session:
            errors.append(f"{label} market_date is not a valid market session")
        if market_date in seen_dates:
            errors.append(f"{label} duplicates market_date {market_date}")
        seen_dates.add(market_date)
        if row.get("schema_version") != GAP_SCHEMA_VERSION:
            errors.append(f"{label} has unsupported schema_version")
        if row.get("mode") != "forward" or row.get("status") != "TERMINAL_MISSING":
            errors.append(f"{label} is not a terminal-missing forward session")
        if not str(row.get("reason_code") or "").strip():
            errors.append(f"{label} has no reason_code")
        if row.get("missing_truth_is_zero") is not False:
            errors.append(f"{label} does not preserve missing truth")
        if row.get("research_only") is not True:
            errors.append(f"{label} does not preserve research-only scope")
        if row.get("broker_execution_enabled") is not False:
            errors.append(f"{label} does not preserve the no-broker boundary")
        recorded_at = str(row.get("recorded_at") or "")
        try:
            parsed_at = datetime.fromisoformat(recorded_at.replace("Z", "+00:00"))
        except ValueError:
            parsed_at = None
        if parsed_at is None or parsed_at.tzinfo is None:
            errors.append(f"{label} recorded_at is not timezone-aware")
        blockers = _session_evidence(paths, market_date) if market_date else []
        if blockers:
            errors.append(
                f"{label} conflicts with existing forward evidence: "
                + ", ".join(blockers)
            )
        accepted.append({**row, "record_id": record_id})
    if errors:
        return [], errors
    return accepted, []


def _session_evidence(paths: PaperOpsPaths, market_date: str) -> list[str]:
    blockers: list[str] = []
    calendar_path = paths.calendar / "strategy_daily_returns.csv"
    if calendar_path.is_file() and f"{market_date},forward," in calendar_path.read_text(
        encoding="utf-8"
    ):
        blockers.append("calendar rows")
    if (paths.reports / "daily" / f"forward_{market_date}.json").is_file():
        blockers.append("completed daily report")
    if any(
        str(row.get("trade_date") or "") == market_date
        and str(row.get("mode") or "") == "forward"
        for row in read_jsonl(paths.ledger / "paper_ledger.jsonl")
    ):
        blockers.append("ledger events")
    return blockers


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


__all__ = ["load_forward_session_gaps", "record_forward_session_gap"]
