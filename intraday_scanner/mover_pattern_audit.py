"""Read-only retained-data audit for the Dawnstrike Mover Pattern Lab.

Database inspection intentionally lives outside ``intraday_scanner.v2``.  The
v2 research boundary remains free of database/runtime execution imports.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
from collections import defaultdict
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import MARKET_TIMEZONE, market_session
from intraday_scanner.providers.daily_movers_base import (
    DESCRIPTIVE_EOD_ROLE,
    REALIZED_EOD_KIND,
    VERIFIED_CORPORATE_ACTION_STATUSES,
)
from intraday_scanner.v2.mover_pattern_lab.core import (
    DEFAULT_OUTPUT_ROOT,
    SCHEMA_VERSION,
    MoverLabPaths,
)
from intraday_scanner.v2.mover_pattern_lab.trade_truth import (
    retained_trade_evidence_recomputes,
)

AUDIT_TABLES = (
    "daily_market_movers",
    "alpha_feature_vectors",
    "alpha_signals",
    "alpha_outcome_labels",
    "signal_outcomes",
    "historical_signals",
    "normalized_source_rows",
    "daily_review_runs",
    "daily_review_items",
    "learning_backfeed_events",
)
QUARANTINE_SCHEMA_VERSION = "v2.mover_evidence_quarantine.v1"


class QuarantinedEvidenceError(RuntimeError):
    """Raised when a learning consumer cannot prove evidence is unquarantined."""


def audit_retained_data(
    *,
    db_path: Path,
    output_root: Path = DEFAULT_OUTPUT_ROOT,
) -> dict[str, Any]:
    """Audit retained mover/feature/outcome truth without mutating SQLite."""

    paths = MoverLabPaths.create(output_root)
    if not db_path.exists():
        payload: dict[str, Any] = {
            "schema_version": f"{SCHEMA_VERSION}.audit",
            "status": "blocked",
            "db_path": str(db_path),
            "blockers": ["database_missing"],
            "warnings": [],
            "table_counts": {},
        }
        _write_audit(paths, payload)
        return payload

    connection = sqlite3.connect(
        f"file:{db_path.resolve().as_posix()}?mode=ro",
        uri=True,
    )
    connection.row_factory = sqlite3.Row
    try:
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        counts = {
            table: _table_count(connection, table) if table in tables else None
            for table in AUDIT_TABLES
        }
        mover_rows = (
            _load_sqlite_payload_rows(connection, "daily_market_movers")
            if "daily_market_movers" in tables
            else []
        )
        mover_audit = _audit_mover_rows(mover_rows)
        valid_movers = [row for row in mover_audit if row["eligible"]]
        contaminated = [
            row
            for row in mover_audit
            if "premarket_source_mislabeled_eod" in row["reasons"]
        ]
        valid_mover_days = len({row["market_date"] for row in valid_movers})
        feature_days = _distinct_day_count(
            connection,
            "alpha_feature_vectors",
            "timestamp",
            tables,
        )
        alpha_label_days = _distinct_day_count(
            connection,
            "alpha_outcome_labels",
            "created_at",
            tables,
        )
        sourced_outcome_rows = (
            _load_sqlite_payload_rows(connection, "signal_outcomes")
            if "signal_outcomes" in tables
            else []
        )
        learning_eligible_outcomes = [
            row
            for row in sourced_outcome_rows
            if outcome_is_learning_eligible(row)
        ]
        learning_outcome_days = len(
            {
                str(row.get("market_date") or row.get("created_at") or "")[:10]
                for row in learning_eligible_outcomes
                if str(row.get("market_date") or row.get("created_at") or "")[:10]
            }
        )
        review_runs = (
            _load_sqlite_payload_rows(connection, "daily_review_runs")
            if "daily_review_runs" in tables
            else []
        )
        review_items = (
            _load_sqlite_payload_rows(connection, "daily_review_items")
            if "daily_review_items" in tables
            else []
        )
        backfeed_rows = (
            _load_sqlite_payload_rows(connection, "learning_backfeed_events")
            if "learning_backfeed_events" in tables
            else []
        )
    finally:
        connection.close()

    blockers: list[str] = []
    warnings: list[str] = []
    if not valid_movers:
        blockers.append("no_semantically_valid_descriptive_eod_movers")
    if contaminated:
        blockers.append("premarket_rows_mislabeled_as_daily_movers")
    if not learning_eligible_outcomes:
        blockers.append("no_learning_eligible_sourced_trade_outcomes")
    if feature_days < 20:
        warnings.append("fewer_than_20_feature_days")
    if learning_outcome_days < 20:
        warnings.append("fewer_than_20_sourced_outcome_days")
    quarantine_state = _quarantine_state(
        mover_audit,
        review_runs,
        review_items,
        backfeed_rows,
    )
    quarantined_review_ids = quarantine_state["quarantined_review_ids"]
    eligible_review_ids = quarantine_state["eligible_review_ids"]
    quarantined_review_items = quarantine_state["quarantined_review_items"]
    quarantined_backfeed = quarantine_state["quarantined_backfeed"]
    quarantine_payload = {
        "schema_version": QUARANTINE_SCHEMA_VERSION,
        "status": "quarantined",
        "database_mutated": False,
        "reason": (
            "review and backfeed rows depend on semantically invalid or fixture "
            "daily mover labels"
        ),
        "review_ids": sorted(quarantined_review_ids),
        "eligible_review_ids": sorted(eligible_review_ids),
        "orphan_review_ids": sorted(quarantine_state["orphan_review_ids"]),
        "review_id_market_dates": {
            review_id: sorted(dates)
            for review_id, dates in sorted(
                quarantine_state["review_dates_by_id"].items()
            )
        },
        "review_item_ids": sorted(
            str(row.get("item_id") or "")
            for row in quarantined_review_items
            if row.get("item_id")
        ),
        "backfeed_event_ids": sorted(
            str(row.get("event_id") or "")
            for row in quarantined_backfeed
            if row.get("event_id")
        ),
        "dates_without_valid_complete_mover_truth": sorted(
            quarantine_state["invalid_mover_dates"]
        ),
        "learning_eligible": False,
        "automatic_application_allowed": False,
        "audit_input_fingerprint": quarantine_state["audit_input_fingerprint"],
    }
    quarantine_fingerprint = _payload_fingerprint(quarantine_payload)
    quarantine_manifest = {
        **quarantine_payload,
        "manifest_fingerprint": quarantine_fingerprint,
    }
    quarantine_path = (
        paths.audits
        / f"evidence_quarantine_{quarantine_fingerprint[:16]}.json"
    )
    _write_immutable_json(quarantine_path, quarantine_manifest)
    _write_json(
        paths.audits / "evidence_quarantine_latest.json",
        {
            "schema_version": f"{QUARANTINE_SCHEMA_VERSION}.latest",
            "manifest_path": str(quarantine_path.resolve()),
            "manifest_fingerprint": quarantine_fingerprint,
        },
    )
    payload = {
        "schema_version": f"{SCHEMA_VERSION}.audit",
        "status": "blocked" if blockers else "ready",
        "db_path": str(db_path.resolve()),
        "table_counts": counts,
        "feature_day_count": feature_days,
        "alpha_outcome_label_day_count": alpha_label_days,
        "valid_eod_mover_row_count": len(valid_movers),
        "valid_eod_mover_day_count": valid_mover_days,
        "contaminated_premarket_row_count": len(contaminated),
        "learning_eligible_outcome_count": len(learning_eligible_outcomes),
        "learning_eligible_outcome_day_count": learning_outcome_days,
        "quarantined_review_count": len(quarantined_review_ids),
        "quarantined_review_item_count": len(quarantined_review_items),
        "quarantined_backfeed_event_count": len(quarantined_backfeed),
        "quarantine_manifest_path": quarantine_path.as_posix(),
        "mover_source_summary": _mover_source_summary(mover_audit),
        "blockers": blockers,
        "warnings": warnings,
        "research_only": True,
        "broker_execution_enabled": False,
        "truth_note": (
            "Descriptive EOD movers cannot emit historical morning trades. "
            "Only prospective cutoff snapshots may produce paper signals."
        ),
    }
    _write_audit(paths, payload)
    return payload


def outcome_is_learning_eligible(row: dict[str, Any]) -> bool:
    """Return true only for a closed, sourced, timestamped, after-cost outcome."""

    if str(row.get("evidence_mode") or "") != "forward_observation":
        return False
    status = str(row.get("outcome_status") or row.get("status") or "").lower()
    if status not in {"complete_sourced", "closed", "complete"}:
        return False
    try:
        source_complete = _optional_bool(row.get("source_coverage_complete"))
        bar_sequence_complete = _optional_bool(
            row.get("source_bar_sequence_complete")
        )
    except ValueError:
        return False
    outcome_return = _first_present_number(
        row.get("net_return_pct"),
        row.get("realized_return_pct"),
        row.get("return_pct"),
    )
    if (
        source_complete is not True
        or bar_sequence_complete is not True
        or outcome_return is None
    ):
        return False
    total_cost = _optional_float(row.get("total_cost"))
    fee_cost = _first_present_number(row.get("fee_cost"), row.get("fees"))
    slippage_cost = _optional_float(row.get("slippage_cost"))
    if (
        total_cost is None
        or fee_cost is None
        or slippage_cost is None
        or min(total_cost, fee_cost, slippage_cost) < 0
        or not math.isclose(total_cost, fee_cost + slippage_cost, abs_tol=3e-5)
    ):
        return False
    entry_at = _aware_datetime(row.get("entry_at") or row.get("entry_time"))
    exit_at = _aware_datetime(row.get("exit_at") or row.get("exit_time"))
    signal_at = _aware_datetime(row.get("signal_at"))
    source_captured_at = _aware_datetime(row.get("source_captured_at"))
    if (
        entry_at is None
        or exit_at is None
        or signal_at is None
        or source_captured_at is None
    ):
        return False
    if not signal_at <= source_captured_at <= signal_at + timedelta(minutes=5):
        return False
    if not signal_at <= source_captured_at <= entry_at <= exit_at:
        return False
    market_date = str(row.get("market_date") or "")
    if (
        not market_date
        or signal_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        != market_date
        or entry_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        != market_date
        or exit_at.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        != market_date
    ):
        return False
    pnl = _first_present_number(row.get("pnl"), row.get("net_pnl"))
    notional = _first_present_number(
        row.get("notional_per_trade"), row.get("notional")
    )
    if pnl is None or notional is None or notional <= 0:
        return False
    if not math.isclose(outcome_return, pnl / notional * 100.0, abs_tol=2e-5):
        return False
    return _outcome_bar_evidence_valid(row)


def assert_backfeed_not_quarantined(
    review_id: str,
    quarantine_manifest_path: str | Path,
    *,
    db_path: str | Path,
) -> None:
    """Fail closed unless a valid manifest proves ``review_id`` is not quarantined.

    Learning/application consumers must call this before applying a retained
    backfeed event. A missing, unreadable, malformed, or unexpectedly permissive
    manifest is not proof of safety and therefore raises.
    """

    normalized_review_id = str(review_id or "").strip()
    if not normalized_review_id:
        raise QuarantinedEvidenceError("review_id is required for quarantine enforcement")
    path = Path(quarantine_manifest_path)
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise QuarantinedEvidenceError(
            f"quarantine manifest is unavailable or invalid: {path}"
        ) from exc
    if not isinstance(payload, dict):
        raise QuarantinedEvidenceError("quarantine manifest must be a JSON object")
    manifest_fingerprint = str(payload.get("manifest_fingerprint") or "")
    unhashed_payload = {
        key: value for key, value in payload.items() if key != "manifest_fingerprint"
    }
    if (
        re.fullmatch(r"[0-9a-f]{64}", manifest_fingerprint) is None
        or _payload_fingerprint(unhashed_payload) != manifest_fingerprint
        or path.stem != f"evidence_quarantine_{manifest_fingerprint[:16]}"
    ):
        raise QuarantinedEvidenceError(
            "quarantine manifest is not a valid content-addressed audit receipt"
        )
    if payload.get("schema_version") != QUARANTINE_SCHEMA_VERSION:
        raise QuarantinedEvidenceError("quarantine manifest schema is invalid")
    if payload.get("status") != "quarantined":
        raise QuarantinedEvidenceError("quarantine manifest status is invalid")
    if payload.get("database_mutated") is not False:
        raise QuarantinedEvidenceError(
            "quarantine manifest must explicitly preserve the retained database"
        )
    if payload.get("automatic_application_allowed") is not False:
        raise QuarantinedEvidenceError(
            "quarantine manifest does not explicitly disable automatic application"
        )
    if payload.get("learning_eligible") is not False:
        raise QuarantinedEvidenceError(
            "quarantine manifest does not explicitly mark quarantined evidence ineligible"
        )
    review_ids = payload.get("review_ids")
    if not isinstance(review_ids, list) or any(
        not isinstance(item, str) or not item.strip() for item in review_ids
    ):
        raise QuarantinedEvidenceError("quarantine manifest review_ids are invalid")
    if normalized_review_id in {item.strip() for item in review_ids}:
        raise QuarantinedEvidenceError(
            f"review_id is quarantined and cannot be applied: {normalized_review_id}"
        )
    eligible_review_ids = payload.get("eligible_review_ids")
    if not isinstance(eligible_review_ids, list) or any(
        not isinstance(item, str) or not item.strip()
        for item in eligible_review_ids
    ):
        raise QuarantinedEvidenceError(
            "quarantine manifest eligible_review_ids are invalid"
        )
    eligible = {item.strip() for item in eligible_review_ids}
    quarantined = {item.strip() for item in review_ids}
    if eligible & quarantined:
        raise QuarantinedEvidenceError(
            "quarantine manifest review classifications overlap"
        )
    fingerprint = str(payload.get("audit_input_fingerprint") or "")
    if re.fullmatch(r"sha256:[0-9a-f]{64}", fingerprint) is None:
        raise QuarantinedEvidenceError(
            "quarantine manifest audit_input_fingerprint is invalid"
        )
    if normalized_review_id not in eligible:
        raise QuarantinedEvidenceError(
            "review_id was not positively cleared by the retained-data audit: "
            f"{normalized_review_id}"
        )
    current_state = _load_current_quarantine_state(Path(db_path))
    if (
        fingerprint != current_state["audit_input_fingerprint"]
        or eligible != current_state["eligible_review_ids"]
        or quarantined != current_state["quarantined_review_ids"]
    ):
        raise QuarantinedEvidenceError(
            "quarantine manifest is stale or does not match the current retained database"
        )
    if normalized_review_id not in current_state["eligible_review_ids"]:
        raise QuarantinedEvidenceError(
            "review_id is not eligible in the current retained database: "
            f"{normalized_review_id}"
        )


def _audit_mover_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    audited = [_audit_mover_row(row) for row in rows]
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in audited:
        grouped[
            (row["market_date"], row["source"], row["source_artifact_ref"])
        ].append(row)
    for items in grouped.values():
        expected_counts = {item["expected_row_count"] for item in items}
        ranks = {item["rank"] for item in items}
        tickers = {item["ticker"] for item in items}
        complete = bool(
            len(expected_counts) == 1
            and None not in expected_counts
            and next(iter(expected_counts)) == len(items)
            and len(tickers) == len(items)
            and ranks == set(range(1, len(items) + 1))
        )
        if not complete:
            for item in items:
                item["reasons"].append("incomplete_or_inconsistent_eod_list")
    for item in audited:
        item["reasons"] = list(dict.fromkeys(item["reasons"]))
        item["eligible"] = not item["reasons"]
    return audited


def _audit_mover_row(row: dict[str, Any]) -> dict[str, Any]:
    source = str(row.get("source") or "").lower()
    source_url = str(row.get("source_url") or "").lower()
    role = str(row.get("dataset_role") or "")
    source_kind = str(row.get("source_snapshot_kind") or "")
    artifact_ref = str(row.get("source_artifact_ref") or row.get("source_ref") or "")
    reasons: list[str] = []
    raw_keys = {str(key).lower() for key in row}
    if (
        "premarket" in source
        or "premarket" in source_url
        or "pre-market" in source_url
        or {"premkt. price", "pre. volume"} & raw_keys
    ):
        reasons.append("premarket_source_mislabeled_eod")
    if role != DESCRIPTIVE_EOD_ROLE:
        reasons.append("missing_descriptive_eod_role")
    if source_kind != REALIZED_EOD_KIND:
        reasons.append("missing_realized_eod_source_kind")
    ingestion_channel = str(row.get("ingestion_channel") or "")
    if not ingestion_channel or ingestion_channel == "public_web_current_session_gainers":
        reasons.append("ineligible_or_missing_ingestion_channel")
    if _optional_bool(row.get("prospective_signal_eligible")) is not False:
        reasons.append("eod_labels_must_not_be_prospective_signal_eligible")
    if _optional_bool(row.get("source_coverage_complete")) is not True:
        reasons.append("source_coverage_not_proven_complete")
    if _optional_bool(row.get("source_complete")) is not True:
        reasons.append("compatibility_source_complete_not_true")
    if _optional_bool(row.get("list_coverage_complete")) is not True:
        reasons.append("list_coverage_not_proven_complete")
    if _optional_bool(row.get("eod_label_eligible")) is not True:
        reasons.append("eod_label_eligibility_not_explicit")
    corporate_status = str(row.get("corporate_action_status") or "").lower()
    if corporate_status not in VERIFIED_CORPORATE_ACTION_STATUSES:
        reasons.append("corporate_action_status_unverified")
    if not _retained_corporate_action_artifact_valid(row, artifact_ref):
        reasons.append("corporate_action_source_artifact_missing_or_hash_invalid")
    if not _retained_artifact_valid(row, artifact_ref=artifact_ref):
        reasons.append("source_artifact_missing_or_hash_invalid")
    if _optional_float(row.get("change_pct")) is None:
        reasons.append("missing_realized_change_pct")
    extracted_at = _aware_datetime(row.get("extracted_at"))
    system_received_at = _aware_datetime(row.get("system_received_at"))
    if extracted_at is None:
        reasons.append("missing_extraction_timestamp")
    elif not _after_published_market_close(
        str(row.get("market_date") or row.get("date") or ""),
        extracted_at,
    ):
        reasons.append("extraction_not_on_market_date_after_published_close")
    if system_received_at is None:
        reasons.append("missing_system_receipt_timestamp")
    elif extracted_at is not None and extracted_at > system_received_at:
        reasons.append("extraction_timestamp_after_system_receipt")
    return {
        "market_date": str(row.get("market_date") or row.get("date") or ""),
        "ticker": str(row.get("ticker") or "").upper(),
        "rank": _positive_int(row.get("rank")),
        "expected_row_count": _positive_int(row.get("expected_row_count")),
        "source_artifact_ref": artifact_ref,
        "source": str(row.get("source") or ""),
        "source_url": str(row.get("source_url") or ""),
        "eligible": not reasons,
        "reasons": reasons,
    }


def _mover_source_summary(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[(row["source"], row["source_url"])].append(row)
    return [
        {
            "source": key[0],
            "source_url": key[1],
            "row_count": len(items),
            "eligible_count": sum(1 for item in items if item["eligible"]),
            "invalid_reason_counts": _reason_counts(items),
        }
        for key, items in sorted(grouped.items())
    ]


def _reason_counts(rows: list[dict[str, Any]]) -> dict[str, int]:
    counts: dict[str, int] = defaultdict(int)
    for row in rows:
        for reason in row["reasons"]:
            counts[reason] += 1
    return dict(sorted(counts.items()))


def _write_audit(paths: MoverLabPaths, payload: dict[str, Any]) -> None:
    _write_json(paths.audits / "retained_data_audit.json", payload)
    lines = [
        "# Mover Pattern Lab Retained Data Audit",
        "",
        f"- Status: `{payload.get('status')}`",
        f"- Valid EOD mover rows: `{payload.get('valid_eod_mover_row_count', 0)}`",
        (
            "- Contaminated premarket rows: "
            f"`{payload.get('contaminated_premarket_row_count', 0)}`"
        ),
        (
            "- Learning-eligible sourced outcomes: "
            f"`{payload.get('learning_eligible_outcome_count', 0)}`"
        ),
        "",
        "## Blockers",
        "",
    ]
    blockers = list(payload.get("blockers") or [])
    lines.extend([f"- `{item}`" for item in blockers] or ["- None"])
    lines.extend(
        [
            "",
            "Descriptive end-of-day movers are never eligible to create historical "
            "morning paper signals.",
        ]
    )
    (paths.audits / "retained_data_audit.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _load_sqlite_payload_rows(
    connection: sqlite3.Connection,
    table: str,
) -> list[dict[str, Any]]:
    columns = {
        str(row[1])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    payload_column = (
        "payload_json"
        if "payload_json" in columns
        else "raw_payload_json"
        if "raw_payload_json" in columns
        else None
    )
    rows = connection.execute(f"SELECT * FROM {table} ORDER BY rowid DESC").fetchall()
    output: list[dict[str, Any]] = []
    for row in rows:
        merged: dict[str, Any] = {}
        if payload_column:
            try:
                payload = json.loads(str(row[payload_column] or "{}"))
            except json.JSONDecodeError:
                payload = {}
            if isinstance(payload, dict):
                merged = dict(payload)
        merged.update(
            {key: row[key] for key in row.keys() if key != payload_column}
        )
        output.append(merged)
    return output


def _table_count(connection: sqlite3.Connection, table: str) -> int:
    return int(connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])


def _distinct_day_count(
    connection: sqlite3.Connection,
    table: str,
    column: str,
    tables: set[str],
) -> int:
    if table not in tables:
        return 0
    return int(
        connection.execute(
            f"SELECT COUNT(DISTINCT substr({column}, 1, 10)) FROM {table}"
        ).fetchone()[0]
        or 0
    )


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _retained_artifact_valid(
    row: dict[str, Any],
    *,
    artifact_ref: str,
) -> bool:
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", artifact_ref.lower())
    if match is None:
        return False
    path_text = str(row.get("source_artifact_path") or "").strip()
    if not path_text:
        source_url = str(row.get("source_url") or "").strip()
        parsed = urlparse(source_url)
        if parsed.scheme not in {"", "file"}:
            return False
        path_text = parsed.path if parsed.scheme == "file" else source_url
    path = Path(path_text)
    if not path.is_file():
        return False
    try:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return False
    return digest == match.group(1)


def _retained_corporate_action_artifact_valid(
    row: dict[str, Any],
    mover_artifact_ref: str,
) -> bool:
    artifact_ref = str(row.get("corporate_action_source_ref") or "").strip()
    path_text = str(row.get("corporate_action_source_path") or "").strip()
    match = re.fullmatch(r"sha256:([0-9a-f]{64})", artifact_ref.lower())
    if match is None or artifact_ref == mover_artifact_ref or not path_text:
        return False
    path = Path(path_text)
    if not path.is_file():
        return False
    try:
        payload_bytes = path.read_bytes()
        payload = json.loads(payload_bytes.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return False
    observed_at = (
        _aware_datetime(payload.get("observed_at"))
        if isinstance(payload, dict)
        else None
    )
    received_at = _aware_datetime(row.get("system_received_at"))
    return bool(
        hashlib.sha256(payload_bytes).hexdigest() == match.group(1)
        and isinstance(payload, dict)
        and payload.get("schema_version")
        == "v2.corporate_action_evidence.v1"
        and payload.get("market_date")
        == str(row.get("market_date") or row.get("date") or "")
        and str(payload.get("symbol") or "").upper()
        == str(row.get("ticker") or row.get("symbol") or "").upper()
        and str(payload.get("corporate_action_status") or "").lower()
        == str(row.get("corporate_action_status") or "").lower()
        and bool(str(payload.get("source") or "").strip())
        and observed_at is not None
        and received_at is not None
        and _after_published_market_close(
            str(row.get("market_date") or row.get("date") or ""),
            observed_at,
        )
        and observed_at <= received_at
        and payload.get("research_only") is True
        and payload.get("broker_execution_enabled") is False
    )


def _audit_input_fingerprint(*row_sets: list[dict[str, Any]]) -> str:
    canonical_sets = []
    for rows in row_sets:
        canonical_sets.append(
            sorted(
                json.dumps(
                    row,
                    sort_keys=True,
                    separators=(",", ":"),
                    default=str,
                )
                for row in rows
            )
        )
    payload = json.dumps(
        canonical_sets,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _payload_fingerprint(payload: Any) -> str:
    canonical = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _quarantine_state(
    mover_audit: list[dict[str, Any]],
    review_runs: list[dict[str, Any]],
    review_items: list[dict[str, Any]],
    backfeed_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    mover_dates_with_invalid_rows = {
        str(row.get("market_date") or "")
        for row in mover_audit
        if not row["eligible"] and str(row.get("market_date") or "")
    }
    valid_complete_mover_dates = {
        str(row.get("market_date") or "")
        for row in mover_audit
        if row["eligible"] and str(row.get("market_date") or "")
    }
    review_dates = {
        str(row.get("market_date") or "")
        for row in review_runs
        if str(row.get("market_date") or "")
    }
    invalid_mover_dates = mover_dates_with_invalid_rows | (
        review_dates - valid_complete_mover_dates
    )
    review_dates_by_id: dict[str, set[str]] = defaultdict(set)
    for row in review_runs:
        review_id = str(row.get("review_id") or "").strip()
        if review_id:
            review_dates_by_id[review_id].add(
                str(row.get("market_date") or "").strip()
            )
    retained_review_ids = set(review_dates_by_id)
    referenced_review_ids = {
        str(row.get("review_id") or "").strip()
        for row in (*review_items, *backfeed_rows)
        if str(row.get("review_id") or "").strip()
    }
    orphan_review_ids = referenced_review_ids - retained_review_ids
    quarantined_review_ids = set(orphan_review_ids)
    eligible_review_ids: set[str] = set()
    for review_id, dates in review_dates_by_id.items():
        if (
            len(dates) != 1
            or "" in dates
            or not dates.issubset(valid_complete_mover_dates)
            or bool(dates & invalid_mover_dates)
        ):
            quarantined_review_ids.add(review_id)
        else:
            eligible_review_ids.add(review_id)
    return {
        "quarantined_review_ids": quarantined_review_ids,
        "eligible_review_ids": eligible_review_ids,
        "orphan_review_ids": orphan_review_ids,
        "review_dates_by_id": review_dates_by_id,
        "invalid_mover_dates": invalid_mover_dates,
        "quarantined_review_items": [
            row
            for row in review_items
            if str(row.get("review_id") or "") in quarantined_review_ids
        ],
        "quarantined_backfeed": [
            row
            for row in backfeed_rows
            if str(row.get("review_id") or "") in quarantined_review_ids
        ],
        "audit_input_fingerprint": _audit_input_fingerprint(
            mover_audit,
            review_runs,
            review_items,
            backfeed_rows,
        ),
    }


def _load_current_quarantine_state(db_path: Path) -> dict[str, Any]:
    if not db_path.is_file():
        raise QuarantinedEvidenceError(
            f"retained database is unavailable for quarantine proof: {db_path}"
        )
    try:
        connection = sqlite3.connect(
            f"file:{db_path.resolve().as_posix()}?mode=ro",
            uri=True,
        )
        connection.row_factory = sqlite3.Row
        try:
            tables = {
                str(row[0])
                for row in connection.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'"
                ).fetchall()
            }
            movers = (
                _load_sqlite_payload_rows(connection, "daily_market_movers")
                if "daily_market_movers" in tables
                else []
            )
            review_runs = (
                _load_sqlite_payload_rows(connection, "daily_review_runs")
                if "daily_review_runs" in tables
                else []
            )
            review_items = (
                _load_sqlite_payload_rows(connection, "daily_review_items")
                if "daily_review_items" in tables
                else []
            )
            backfeed_rows = (
                _load_sqlite_payload_rows(connection, "learning_backfeed_events")
                if "learning_backfeed_events" in tables
                else []
            )
        finally:
            connection.close()
    except sqlite3.Error as exc:
        raise QuarantinedEvidenceError(
            f"retained database could not be audited: {db_path}"
        ) from exc
    return _quarantine_state(
        _audit_mover_rows(movers),
        review_runs,
        review_items,
        backfeed_rows,
    )


def _write_immutable_json(path: Path, payload: Any) -> None:
    rendered = json.dumps(payload, indent=2, sort_keys=True) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        if path.read_text(encoding="utf-8") != rendered:
            raise ValueError(f"immutable audit artifact conflict: {path}")
        return
    path.write_text(rendered, encoding="utf-8")


def _after_published_market_close(
    market_date: str,
    extracted_at: datetime,
) -> bool:
    try:
        requested_date = date.fromisoformat(market_date)
        session = market_session(requested_date)
    except (ValueError, MarketCalendarCoverageError):
        return False
    if not session.is_trading_day or session.close_time_et is None:
        return False
    close_at = datetime.combine(
        requested_date,
        time.fromisoformat(session.close_time_et),
        tzinfo=MARKET_TIMEZONE,
    )
    extracted_et = extracted_at.astimezone(MARKET_TIMEZONE)
    return extracted_et.date() == requested_date and extracted_et >= close_at


def _outcome_bar_evidence_valid(row: dict[str, Any]) -> bool:
    return retained_trade_evidence_recomputes(row)


def _aware_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed


def _positive_int(value: Any) -> int | None:
    try:
        number = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _optional_float(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _optional_bool(value: Any) -> bool | None:
    if value in {None, ""}:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    normalized = str(value).strip().lower()
    if normalized in {"true", "1", "yes", "y"}:
        return True
    if normalized in {"false", "0", "no", "n"}:
        return False
    return None


def _first_present_number(*values: Any) -> float | None:
    for value in values:
        if value not in {None, ""}:
            return _optional_float(value)
    return None


__all__ = [
    "QuarantinedEvidenceError",
    "assert_backfeed_not_quarantined",
    "audit_retained_data",
    "outcome_is_learning_eligible",
]
