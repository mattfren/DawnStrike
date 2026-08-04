"""Tamper-evident terminal-missing forward-session evidence for PaperOps."""

from __future__ import annotations

import csv
import hashlib
import hmac
import json
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import market_session
from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.storage import (
    append_jsonl_unique,
    exclusive_file_lock,
    read_jsonl,
)

GAP_SCHEMA_VERSION = "v2.paper_ops_forward_session_gap.v2"
ANCHOR_SCHEMA_VERSION = "v2.paper_ops_forward_session_gap_anchor.v2"
SIGNING_KEY_ENV = "DAWNSTRIKE_FORWARD_GAP_HMAC_KEY"
_GAP_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "previous_record_id",
        "market_date",
        "mode",
        "status",
        "reason_code",
        "recorded_at",
        "missing_truth_is_zero",
        "research_only",
        "broker_execution_enabled",
        "record_id",
    }
)
_ANCHOR_FIELDS = frozenset(
    {
        "schema_version",
        "sequence",
        "previous_anchor_id",
        "gap_count",
        "head_record_id",
        "ledger_sha256",
        "anchored_at",
        "anchor_id",
        "signature_hmac_sha256",
    }
)


def record_forward_session_gap(
    *,
    output_root: Path,
    market_date: str,
    reason_code: str,
) -> dict[str, object]:
    """Append one historical no-run session without inventing a return."""

    paths = PaperOpsPaths.create(output_root)
    selected = date.fromisoformat(market_date)
    if not market_session(selected).is_trading_day:
        raise ValueError(f"{market_date} is not a market session")
    if selected >= datetime.now(timezone.utc).date():
        raise ValueError("only completed historical sessions can be recorded as gaps")
    normalized_reason = reason_code.strip().lower()
    if re.fullmatch(r"[a-z0-9_-]+", normalized_reason) is None:
        raise ValueError(
            "reason_code must use lowercase letters, numbers, underscores, or hyphens"
        )

    with exclusive_file_lock(paths.state / ".forward_session_gaps.lock"):
        blockers = _session_evidence(paths, market_date)
        if blockers:
            raise ValueError(
                "cannot record a terminal gap where forward evidence exists: "
                + ", ".join(blockers)
            )
        existing, errors = _load_forward_session_gaps(paths)
        if errors:
            raise ValueError(
                "existing forward-session gap ledger is invalid: " + "; ".join(errors)
            )
        same_date = [row for row in existing if row["market_date"] == market_date]
        if same_date:
            if same_date[0]["reason_code"] != normalized_reason:
                raise ValueError("the session already has a conflicting terminal-gap reason")
            return _result("already_recorded", 0, same_date[0])

        sequence = len(existing) + 1
        canonical: dict[str, object] = {
            "schema_version": GAP_SCHEMA_VERSION,
            "sequence": sequence,
            "previous_record_id": (
                str(existing[-1]["record_id"]) if existing else ""
            ),
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
        signing_key = _signing_key()
        gap_path = paths.state / "forward_session_gaps.jsonl"
        appended = append_jsonl_unique(gap_path, [record], "record_id")
        if appended != 1:
            raise ValueError("forward-session gap append was not unique")
        _append_anchor(
            paths,
            gap_path=gap_path,
            record=record,
            signing_key=signing_key,
        )
        verified, verification_errors = _load_forward_session_gaps(paths)
        if verification_errors or len(verified) != sequence:
            raise ValueError(
                "forward-session gap post-write verification failed: "
                + "; ".join(verification_errors or ["unexpected record count"])
            )
        return _result("recorded", appended, record)


def load_forward_session_gaps(
    paths: PaperOpsPaths,
) -> tuple[list[dict[str, Any]], list[str]]:
    with exclusive_file_lock(paths.state / ".forward_session_gaps.lock"):
        return _load_forward_session_gaps(paths)


def _load_forward_session_gaps(
    paths: PaperOpsPaths,
) -> tuple[list[dict[str, Any]], list[str]]:
    gap_path = paths.state / "forward_session_gaps.jsonl"
    anchor_path = paths.state / "forward_session_gap_anchors.jsonl"
    try:
        rows = read_jsonl(gap_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], [f"forward session gap ledger cannot be read: {exc}"]
    try:
        anchors = read_jsonl(anchor_path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        return [], [f"forward session gap anchor ledger cannot be read: {exc}"]

    accepted: list[dict[str, Any]] = []
    errors: list[str] = []
    seen_dates: set[str] = set()
    seen_ids: set[str] = set()
    previous_record_id = ""
    for index, raw in enumerate(rows, start=1):
        label = f"forward session gap row {index}"
        if set(raw) != _GAP_FIELDS:
            errors.append(f"{label} has non-canonical fields")
            continue
        row = dict(raw)
        record_id = str(row.pop("record_id", ""))
        if record_id != hashlib.sha256(_canonical_bytes(row)).hexdigest():
            errors.append(f"{label} record_id integrity mismatch")
        if row.get("sequence") != index:
            errors.append(f"{label} sequence mismatch")
        if str(row.get("previous_record_id") or "") != previous_record_id:
            errors.append(f"{label} previous_record_id chain mismatch")
        if record_id in seen_ids:
            errors.append(f"{label} duplicates record_id {record_id}")
        seen_ids.add(record_id)
        previous_record_id = record_id
        market_date = str(row.get("market_date") or "")
        if not _valid_market_session(market_date):
            errors.append(f"{label} market_date is not a valid market session")
        if market_date in seen_dates:
            errors.append(f"{label} duplicates market_date {market_date}")
        seen_dates.add(market_date)
        if row.get("schema_version") != GAP_SCHEMA_VERSION:
            errors.append(f"{label} has unsupported schema_version")
        if row.get("mode") != "forward" or row.get("status") != "TERMINAL_MISSING":
            errors.append(f"{label} is not a terminal-missing forward session")
        reason = str(row.get("reason_code") or "")
        if re.fullmatch(r"[a-z0-9_-]+", reason) is None:
            errors.append(f"{label} has invalid reason_code")
        if row.get("missing_truth_is_zero") is not False:
            errors.append(f"{label} does not preserve missing truth")
        if row.get("research_only") is not True:
            errors.append(f"{label} does not preserve research-only scope")
        if row.get("broker_execution_enabled") is not False:
            errors.append(f"{label} does not preserve the no-broker boundary")
        if not _timezone_aware(str(row.get("recorded_at") or "")):
            errors.append(f"{label} recorded_at is not timezone-aware")
        blockers = _session_evidence(paths, market_date) if market_date else []
        if blockers:
            errors.append(
                f"{label} conflicts with existing forward evidence: "
                + ", ".join(blockers)
            )
        accepted.append({**row, "record_id": record_id})

    errors.extend(_anchor_errors(anchors, rows=rows, gap_path=gap_path))
    if errors:
        return [], list(dict.fromkeys(errors))
    return accepted, []


def _append_anchor(
    paths: PaperOpsPaths,
    *,
    gap_path: Path,
    record: dict[str, object],
    signing_key: bytes,
) -> None:
    anchor_path = paths.state / "forward_session_gap_anchors.jsonl"
    existing = read_jsonl(anchor_path)
    sequence = len(existing) + 1
    canonical: dict[str, object] = {
        "schema_version": ANCHOR_SCHEMA_VERSION,
        "sequence": sequence,
        "previous_anchor_id": (
            str(existing[-1].get("anchor_id") or "") if existing else ""
        ),
        "gap_count": int(str(record["sequence"])),
        "head_record_id": str(record["record_id"]),
        "ledger_sha256": hashlib.sha256(gap_path.read_bytes()).hexdigest(),
        "anchored_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    identified = {
        **canonical,
        "anchor_id": hashlib.sha256(_canonical_bytes(canonical)).hexdigest(),
    }
    anchor = {
        **identified,
        "signature_hmac_sha256": hmac.new(
            signing_key,
            _canonical_bytes(identified),
            hashlib.sha256,
        ).hexdigest(),
    }
    if append_jsonl_unique(anchor_path, [anchor], "anchor_id") != 1:
        raise ValueError("forward-session anchor append was not unique")


def _anchor_errors(
    anchors: list[dict[str, object]],
    *,
    rows: list[dict[str, object]],
    gap_path: Path,
) -> list[str]:
    if bool(rows) != bool(anchors):
        return ["forward session gap ledger and anchor ledger presence mismatch"]
    errors: list[str] = []
    signing_key: bytes | None = None
    if anchors:
        try:
            signing_key = _signing_key()
        except ValueError as exc:
            errors.append(str(exc))
    previous_anchor_id = ""
    for index, raw in enumerate(anchors, start=1):
        label = f"forward session gap anchor {index}"
        if set(raw) != _ANCHOR_FIELDS:
            errors.append(f"{label} has non-canonical fields")
            continue
        row = dict(raw)
        signature = str(row.pop("signature_hmac_sha256", ""))
        anchor_id = str(row.pop("anchor_id", ""))
        if anchor_id != hashlib.sha256(_canonical_bytes(row)).hexdigest():
            errors.append(f"{label} anchor_id integrity mismatch")
        identified = {**row, "anchor_id": anchor_id}
        expected_signature = (
            hmac.new(
                signing_key,
                _canonical_bytes(identified),
                hashlib.sha256,
            ).hexdigest()
            if signing_key is not None
            else ""
        )
        if not signature or not hmac.compare_digest(signature, expected_signature):
            errors.append(f"{label} signature_hmac_sha256 mismatch")
        if row.get("schema_version") != ANCHOR_SCHEMA_VERSION:
            errors.append(f"{label} has unsupported schema_version")
        if row.get("sequence") != index:
            errors.append(f"{label} sequence mismatch")
        if str(row.get("previous_anchor_id") or "") != previous_anchor_id:
            errors.append(f"{label} previous_anchor_id chain mismatch")
        if row.get("gap_count") != index:
            errors.append(f"{label} gap_count mismatch")
        if not _timezone_aware(str(row.get("anchored_at") or "")):
            errors.append(f"{label} anchored_at is not timezone-aware")
        previous_anchor_id = anchor_id
    if anchors and rows:
        latest = anchors[-1]
        if latest.get("gap_count") != len(rows):
            errors.append("latest gap anchor count does not match the ledger")
        if str(latest.get("head_record_id") or "") != str(
            rows[-1].get("record_id") or ""
        ):
            errors.append("latest gap anchor head does not match the ledger")
        actual_sha = hashlib.sha256(gap_path.read_bytes()).hexdigest()
        if str(latest.get("ledger_sha256") or "") != actual_sha:
            errors.append("latest gap anchor digest does not match the ledger")
    return errors


def _result(status: str, appended: int, record: dict[str, object]) -> dict[str, object]:
    return {
        "status": status,
        "appended": appended,
        "record": record,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _session_evidence(paths: PaperOpsPaths, market_date: str) -> list[str]:
    blockers: list[str] = []
    calendar_path = paths.calendar / "strategy_daily_returns.csv"
    if calendar_path.is_file():
        with calendar_path.open("r", encoding="utf-8", newline="") as handle:
            if any(
                str(row.get("date") or "") == market_date
                and str(row.get("mode") or "") == "forward"
                for row in csv.DictReader(handle)
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


def _valid_market_session(value: str) -> bool:
    try:
        return market_session(date.fromisoformat(value)).is_trading_day
    except (MarketCalendarCoverageError, TypeError, ValueError):
        return False


def _timezone_aware(value: str) -> bool:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None


def _canonical_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")


def _signing_key() -> bytes:
    raw = os.environ.get(SIGNING_KEY_ENV, "").strip()
    if len(raw) < 32:
        raise ValueError(
            f"{SIGNING_KEY_ENV} is required and must contain at least 32 characters"
        )
    return raw.encode("utf-8")


__all__ = [
    "SIGNING_KEY_ENV",
    "load_forward_session_gaps",
    "record_forward_session_gap",
]
