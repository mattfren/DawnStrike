"""Fail-closed policy for AlphaOps EOD outcome-dependent stages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.v5_policy import alphaops_strategy_contract
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def evaluate_alpha_eod_gate(
    *,
    db_path: str | Path,
    market_date: str,
    capture_exit_code: int,
    capture_result_path: str | Path,
    outcome_gap_path: str | Path,
    out_path: str | Path | None = None,
) -> dict[str, Any]:
    """Decide whether official paper reconciliation and learning are required.

    Explicit official no-trade evidence is the only path that can make the
    outcome-dependent stages not applicable. Shadow candidates never turn a
    no-trade day into an official selection, and their missing outcomes remain
    persisted by the capture/V6 services rather than being converted to zero.
    """

    selected_date = market_date[:10]
    store = SQLiteScanStore(db_path)
    store.initialize()
    strategy_id = alphaops_strategy_contract(
        f"{selected_date}T12:00:00-04:00"
    )[0]
    selections = [
        row
        for row in store.load_signal_selections(
            strategy_id=strategy_id,
            cohort="official_telegram",
            limit=50_000,
        )
        if str(row.get("selected_at") or "")[:10] == selected_date
    ]
    no_trade = [
        row
        for row in selections
        if str(row.get("decision") or "").lower() == "no_trade"
        or str(row.get("ticker") or "").upper() == "NO_TRADE"
    ]
    official = [
        row
        for row in selections
        if str(row.get("decision") or "").lower() != "no_trade"
        and str(row.get("ticker") or "").upper() != "NO_TRADE"
    ]
    capture, capture_error = _load_json_object(capture_result_path)
    outcome_gap, gap_error = _load_json_object(outcome_gap_path)
    errors: list[str] = []
    if not selections:
        errors.append("exact official session selection evidence is absent")
    if no_trade and official:
        errors.append("official selection evidence mixes no-trade and selected signals")
    if capture_error:
        errors.append(f"capture result invalid: {capture_error}")
    if gap_error:
        errors.append(f"outcome-gap result invalid: {gap_error}")
    if capture and str(capture.get("market_date") or "")[:10] != selected_date:
        errors.append("capture result market_date does not match the requested session")
    if capture and capture.get("missing_values_are_zero") is not False:
        errors.append("capture result does not preserve missing truth")
    if capture and capture.get("broker_execution_enabled") is not False:
        errors.append("capture result does not preserve the no-broker boundary")
    if outcome_gap and str(outcome_gap.get("market_date") or "")[:10] != selected_date:
        errors.append("outcome-gap market_date does not match the requested session")
    if outcome_gap and outcome_gap.get("missing_truth_is_zero") is not False:
        errors.append("outcome-gap result does not preserve missing truth")
    if outcome_gap and outcome_gap.get("broker_execution_enabled") is not False:
        errors.append("outcome-gap result does not preserve the no-broker boundary")

    gap_status = str(outcome_gap.get("status") or "").upper()
    eligible_count = _nonnegative_int(outcome_gap.get("eligible_candidate_count"))
    missing_count = _nonnegative_int(outcome_gap.get("missing_outcome_count"))
    official_outcomes_required = bool(official)
    if no_trade and not official:
        if gap_status != "NO_ELIGIBLE" or eligible_count != 0:
            errors.append(
                "official no-trade evidence requires a NO_ELIGIBLE outcome-gap result"
            )
        status = "NO_ELIGIBLE"
        reason_code = "official_no_trade"
    elif official:
        if gap_status != "COMPLETE" or missing_count != 0:
            errors.append("required official outcomes are not complete")
        status = "COMPLETE"
        reason_code = "official_outcomes_complete"
    else:
        status = "BLOCKED"
        reason_code = "official_selection_missing"

    if errors:
        status = "BLOCKED"
        reason_code = "eod_outcome_gate_failed"
    payload: dict[str, Any] = {
        "schema_version": "dawnstrike.alpha_eod_gate.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "market_date": selected_date,
        "status": status,
        "reason_code": reason_code,
        "official_outcomes_required": official_outcomes_required,
        "selection_count": len(selections),
        "official_signal_count": len(official),
        "no_trade_count": len(no_trade),
        "capture_exit_code": int(capture_exit_code),
        "capture_status": str(capture.get("status") or ""),
        "outcome_gap_status": gap_status,
        "eligible_candidate_count": eligible_count,
        "missing_outcome_count": missing_count,
        "errors": errors,
        "warnings": (
            [
                "raw capture returned nonzero because non-official research targets "
                "were incomplete; exact official outcome truth is complete"
            ]
            if official and capture_exit_code != 0 and not errors
            else []
        ),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["payload_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if out_path is not None:
        _atomic_write(Path(out_path), payload)
    return payload


def _load_json_object(path: str | Path) -> tuple[dict[str, Any], str | None]:
    candidate = Path(path)
    if not candidate.is_file():
        return {}, "file is absent"
    try:
        value = json.loads(candidate.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {}, str(exc)
    if not isinstance(value, dict):
        return {}, "payload is not an object"
    return value, None


def _nonnegative_int(value: object) -> int:
    if isinstance(value, bool):
        return -1
    if not isinstance(value, str | int | float):
        return -1
    if isinstance(value, float) and not value.is_integer():
        return -1
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return -1
    return parsed if parsed >= 0 else -1


def _atomic_write(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


__all__ = ["evaluate_alpha_eod_gate"]
