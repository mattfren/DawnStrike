"""Fail-closed policy for AlphaOps EOD outcome-dependent stages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.v5_policy import alphaops_strategy_contract
from intraday_scanner.services.alpha_official_cohort_service import (
    validate_or_recover_official_cohort,
)
from intraday_scanner.services.alpha_outcome_capture_service import CONCLUSIVE_STATUSES
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
    """Authorize official EOD stages from exact immutable identities only."""

    selected_date = market_date[:10]
    store = SQLiteScanStore(db_path)
    store.initialize()
    strategy_id, strategy_version = alphaops_strategy_contract(
        f"{selected_date}T12:00:00-04:00"
    )
    validation = validate_or_recover_official_cohort(
        store,
        market_date=selected_date,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
    )
    selections = list(validation.selections)
    no_trade = [row for row in selections if _is_canonical_no_trade(row, selected_date)]
    contradictory = [
        row
        for row in selections
        if _has_no_trade_marker(row)
        and not _is_canonical_no_trade(row, selected_date)
    ]
    official = [row for row in selections if not _has_no_trade_marker(row)]

    capture, capture_error = _load_json_object(capture_result_path)
    outcome_gap, gap_error = _load_json_object(outcome_gap_path)
    errors = list(validation.errors)
    if contradictory:
        errors.append("official cohort contains a contradictory no-trade identity")
    if no_trade and official:
        errors.append("official cohort mixes no-trade and selected signals")
    if len(no_trade) > 1:
        errors.append("official cohort contains multiple no-trade sentinels")
    errors.extend(
        _artifact_errors(
            capture,
            label="capture result",
            market_date=selected_date,
            missing_field="missing_values_are_zero",
            load_error=capture_error,
        )
    )
    errors.extend(
        _artifact_errors(
            outcome_gap,
            label="outcome-gap result",
            market_date=selected_date,
            missing_field="missing_truth_is_zero",
            load_error=gap_error,
        )
    )

    exact_outcome_ids: list[str] = []
    if official:
        outcome_errors, exact_outcome_ids = _exact_outcome_errors(
            store,
            selections=official,
            market_date=selected_date,
        )
        errors.extend(outcome_errors)

    official_outcomes_required = bool(official)
    if no_trade and not official:
        status = "NO_ELIGIBLE"
        reason_code = "official_no_trade"
    elif official:
        status = "COMPLETE"
        reason_code = "official_outcomes_complete"
    else:
        status = "BLOCKED"
        reason_code = "official_cohort_missing"
    if errors:
        status = "BLOCKED"
        reason_code = "eod_outcome_gate_failed"

    cohort = validation.cohort or {}
    gap_status = str(outcome_gap.get("status") or "").upper()
    payload: dict[str, Any] = {
        "schema_version": "dawnstrike.alpha_eod_gate.v2",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "market_date": selected_date,
        "status": status,
        "reason_code": reason_code,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "official_cohort_id": str(cohort.get("official_cohort_id") or ""),
        "official_membership_sha256": str(cohort.get("membership_sha256") or ""),
        "official_cohort_recovered": validation.recovered,
        "official_selection_ids": sorted(
            str(row.get("selection_id") or "") for row in selections
        ),
        "official_signal_ids": sorted(
            str(row.get("signal_id") or "") for row in official
        ),
        "exact_outcome_signal_ids": sorted(exact_outcome_ids),
        "official_outcomes_required": official_outcomes_required,
        "selection_count": len(selections),
        "official_signal_count": len(official),
        "no_trade_count": len(no_trade),
        "capture_exit_code": int(capture_exit_code),
        "capture_status": str(capture.get("status") or ""),
        "outcome_gap_status": gap_status,
        "eligible_candidate_count": _nonnegative_int(
            outcome_gap.get("eligible_candidate_count")
        ),
        "missing_outcome_count": _nonnegative_int(
            outcome_gap.get("missing_outcome_count")
        ),
        "errors": list(dict.fromkeys(errors)),
        "warnings": (
            [
                "aggregate capture is incomplete only for non-official research targets; "
                "the frozen official cohort has exact terminal truth"
            ]
            if capture_exit_code != 0 and not errors
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


def _exact_outcome_errors(
    store: SQLiteScanStore,
    *,
    selections: list[dict[str, Any]],
    market_date: str,
) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    accepted: list[str] = []
    for selection in selections:
        signal_id = str(selection.get("signal_id") or "")
        ticker = str(selection.get("ticker") or "").upper()
        rows = store.load_signal_outcomes(signal_id=signal_id, limit=10)
        matches = [
            row
            for row in rows
            if str(row.get("market_date") or "")[:10] == market_date
            and str(row.get("ticker") or "").upper() == ticker
        ]
        if len(matches) != 1:
            errors.append(
                f"official signal {signal_id} requires one exact sourced outcome"
            )
            continue
        outcome = matches[0]
        status = str(outcome.get("outcome_status") or "")
        requirements = {
            "conclusive outcome_status": status in CONCLUSIVE_STATUSES,
            "timestamp validation": outcome.get("validated_against_signal_timestamp") is True,
            "automatic sourced data": outcome.get("automatic_sourced_data") is True,
            "complete source coverage": outcome.get("source_coverage_complete") is True,
            "no-lookahead proof": outcome.get("no_lookahead") is True,
            "research-only scope": outcome.get("research_only") is True,
            "no-broker boundary": outcome.get("broker_execution_enabled") is False,
            "source bar hash": bool(
                str(outcome.get("source_bar_hash_sha256") or "").strip()
            ),
        }
        failed = [name for name, passed in requirements.items() if not passed]
        if failed:
            errors.append(
                f"official signal {signal_id} outcome lacks " + ", ".join(failed)
            )
            continue
        accepted.append(signal_id)
    return errors, accepted


def _artifact_errors(
    payload: dict[str, Any],
    *,
    label: str,
    market_date: str,
    missing_field: str,
    load_error: str | None,
) -> list[str]:
    if load_error:
        return [f"{label} invalid: {load_error}"]
    errors: list[str] = []
    if str(payload.get("market_date") or "")[:10] != market_date:
        errors.append(f"{label} market_date does not match the requested session")
    if payload.get(missing_field) is not False:
        errors.append(f"{label} does not preserve missing truth")
    if payload.get("research_only") is not True:
        errors.append(f"{label} does not preserve research-only scope")
    if payload.get("broker_execution_enabled") is not False:
        errors.append(f"{label} does not preserve the no-broker boundary")
    return errors


def _is_canonical_no_trade(selection: dict[str, Any], market_date: str) -> bool:
    scan_id = str(selection.get("scan_id") or "")
    return (
        str(selection.get("decision") or "").lower() == "no_trade"
        and str(selection.get("ticker") or "").upper() == "NO_TRADE"
        and selection.get("rank") == 0
        and str(selection.get("signal_id") or "")
        == f"no_trade:{scan_id}:{market_date}"
        and str(selection.get("event_key") or "").endswith(":alpha_no_trade")
    )


def _has_no_trade_marker(selection: dict[str, Any]) -> bool:
    return any(
        (
            str(selection.get("decision") or "").lower() == "no_trade",
            str(selection.get("ticker") or "").upper() == "NO_TRADE",
            str(selection.get("signal_id") or "").startswith("no_trade:"),
            str(selection.get("event_key") or "").endswith(":alpha_no_trade"),
        )
    )


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
