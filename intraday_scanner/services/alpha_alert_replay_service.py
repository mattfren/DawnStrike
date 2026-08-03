"""Read-only replay of historical AlphaOps alert-gate decisions.

This module exists to distinguish what a legacy record claimed at decision time
from what the current deterministic gate would have allowed using only its
stored decision inputs.  Outcome rows are joined only after the decision has
been replayed, so they can explain a historical result but cannot influence the
replayed decision.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.alert_gate import ALERT_GATE_VERSION, apply_alert_gate
from intraday_scanner.errors import SnapshotValidationError

ALERT_REPLAY_VERSION = "dawnstrike-alpha-alert-replay-v1.0.0"


def replay_alpha_alert_history(*, db_path: str | Path) -> dict[str, Any]:
    """Replay every stored AlphaOps signal without writing to the database."""

    path = Path(db_path).expanduser().resolve()
    if not path.is_file():
        raise SnapshotValidationError(f"Alpha alert replay database does not exist: {path}")

    with sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True) as connection:
        connection.row_factory = sqlite3.Row
        tables = _table_names(connection)
        if "alpha_signals" not in tables:
            raise SnapshotValidationError("Alpha alert replay requires alpha_signals.")
        signal_rows = connection.execute(
            """
            SELECT signal_key, scan_id, ticker, rank, timestamp, alpha_score,
                   edge_bucket, confidence_bucket, can_alert, no_trade_reason,
                   payload_json
            FROM alpha_signals
            ORDER BY timestamp ASC, signal_key ASC
            """
        ).fetchall()
        outcome_rows = _load_outcomes(connection) if "signal_outcomes" in tables else []

    outcomes_by_signal = {
        str(row["signal_id"]): dict(row)
        for row in outcome_rows
        if str(row["signal_id"])
    }
    records = [_replay_row(dict(row), outcomes_by_signal) for row in signal_rows]
    input_hash = _sha256(records, keys=("decision_input_hash",))
    losses = [record for record in records if record["is_gross_close_loss"]]
    losses_replay_blocked = [
        record for record in losses if record["replay_blocked_decision_time"]
    ]
    losses_replay_unblocked = [
        record for record in losses if not record["replay_blocked_decision_time"]
    ]
    return {
        "status": "PASS" if not losses_replay_unblocked else "FAIL",
        "replay_version": ALERT_REPLAY_VERSION,
        "alert_gate_version": ALERT_GATE_VERSION,
        "database_path": str(path),
        "decision_input_hash_sha256": input_hash,
        "contract": {
            "decision_time_only": True,
            "outcomes_used_for_decision": False,
            "database_write_mode": "read_only",
            "gross_close_return_formula": "(close_price / entry_price - 1) * 100",
            "return_scope": "sourced signal outcome; not account return; no costs",
        },
        "summary": {
            "signal_count": len(records),
            "stored_alertable_count": sum(record["stored_can_alert"] for record in records),
            "replay_alertable_count": sum(record["replay_can_alert"] for record in records),
            "legacy_alert_truth_mismatch_count": sum(
                record["legacy_alert_truth_mismatch"] for record in records
            ),
            "gross_close_eligible_count": sum(
                record["gross_close_return_pct"] is not None for record in records
            ),
            "gross_close_loss_count": len(losses),
            "gross_close_losses_replay_blocked_count": len(losses_replay_blocked),
            "gross_close_losses_replay_unblocked_count": len(losses_replay_unblocked),
        },
        "records": records,
    }


def write_alpha_alert_replay_report(
    *,
    db_path: str | Path,
    out_path: str | Path,
) -> dict[str, Any]:
    """Write a canonical replay artifact after a read-only database replay."""

    report = replay_alpha_alert_history(db_path=db_path)
    artifact = Path(out_path)
    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**report, "artifact_path": str(artifact)}


def _replay_row(
    row: dict[str, Any],
    outcomes_by_signal: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    signal_key = str(row["signal_key"])
    decision_input = _decision_input(row)
    # The gate executes before this outcome lookup. Keep that ordering explicit:
    # a future outcome must never influence the replayed historical decision.
    replay = apply_alert_gate(decision_input)
    outcome = outcomes_by_signal.get(signal_key)
    gross_close_return = _gross_close_return(outcome)
    stored_can_alert = _truthy(row.get("can_alert"))
    replay_can_alert = _truthy(replay.get("can_alert"))
    return {
        "signal_id": signal_key,
        "scan_id": str(row.get("scan_id") or ""),
        "timestamp": str(row.get("timestamp") or ""),
        "ticker": str(row.get("ticker") or "").upper(),
        "rank": _integer(row.get("rank")),
        "decision_input_hash": _sha256(decision_input),
        "stored_can_alert": stored_can_alert,
        "stored_alert_gate_status": str(
            decision_input.get("alert_gate_status") or ""
        ).upper(),
        "replay_can_alert": replay_can_alert,
        "replay_alert_gate_status": str(replay.get("alert_gate_status") or "").upper(),
        "replay_blocked_decision_time": stored_can_alert and not replay_can_alert,
        "legacy_alert_truth_mismatch": stored_can_alert != replay_can_alert,
        "replay_block_reasons": list(replay.get("alert_gate_reasons") or []),
        "outcome_status": str(dict(outcome or {}).get("outcome_status") or ""),
        "gross_close_return_pct": gross_close_return,
        "is_gross_close_loss": gross_close_return is not None and gross_close_return < 0,
    }


def _decision_input(row: dict[str, Any]) -> dict[str, Any]:
    payload = _json_object(row.get("payload_json"))
    # The normalized table columns are authoritative identity values. All other
    # gate inputs stay exactly as recorded in the immutable payload.
    return {
        **payload,
        "signal_key": str(row.get("signal_key") or payload.get("signal_key") or ""),
        "scan_id": str(row.get("scan_id") or payload.get("scan_id") or ""),
        "ticker": str(row.get("ticker") or payload.get("ticker") or "").upper(),
        "rank": _integer(row.get("rank")) or _integer(payload.get("rank")),
        "timestamp": str(row.get("timestamp") or payload.get("timestamp") or ""),
        "alpha_score": _number(row.get("alpha_score"))
        if _number(row.get("alpha_score")) is not None
        else payload.get("alpha_score"),
        "edge_bucket": str(row.get("edge_bucket") or payload.get("edge_bucket") or ""),
        "confidence_bucket": str(
            row.get("confidence_bucket") or payload.get("confidence_bucket") or ""
        ),
        "can_alert": _truthy(row.get("can_alert")),
        "no_trade_reason": str(row.get("no_trade_reason") or payload.get("no_trade_reason") or ""),
    }


def _load_outcomes(connection: sqlite3.Connection) -> list[sqlite3.Row]:
    return connection.execute(
        """
        SELECT signal_id, outcome_status, entry_price, close_price
        FROM signal_outcomes
        ORDER BY signal_id ASC
        """
    ).fetchall()


def _table_names(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0])
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }


def _gross_close_return(outcome: dict[str, Any] | None) -> float | None:
    row = dict(outcome or {})
    entry = _number(row.get("entry_price"))
    close = _number(row.get("close_price"))
    if entry is None or close is None or entry <= 0:
        return None
    return round(((close / entry) - 1.0) * 100.0, 6)


def _json_object(value: Any) -> dict[str, Any]:
    if not isinstance(value, str) or not value.strip():
        return {}
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return {}
    return dict(parsed) if isinstance(parsed, dict) else {}


def _sha256(value: Any, *, keys: tuple[str, ...] | None = None) -> str:
    if keys is not None:
        value = [{key: row.get(key) for key in keys} for row in value]
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _integer(value: Any) -> int | None:
    number = _number(value)
    return int(number) if number is not None else None


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == 1
    return str(value).strip().lower() in {"1", "true", "yes", "y"}
