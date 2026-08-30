"""Fail-closed empirical execution-cost challenger.

The provisional V5 cost proxy remains the champion assumption.  This module
can describe a separately versioned p75/p90 challenger only when a governed
FillTruth adapter authenticates each point-in-time fill/quote observation.  A
JSON flag, caller hash, or quote snapshot by itself is deliberately
insufficient.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.fill_truth import has_authenticated_committed_fill_truth

SCHEMA_VERSION = "dawnstrike.alpha.empirical_execution_cost_challenger.v1"
EMPIRICAL_COST_CHALLENGER_VERSION = "dawnstrike-empirical-cost-p75-p90-20260829.v1"
PROVISIONAL_COST_MODEL_VERSION = "alphaops-v5-cost-model-50bps-0.005ps"
MIN_AUTHENTICATED_OBSERVATIONS = 20
MIN_AUTHENTICATED_SESSIONS = 5
MARKET_TIMEZONE = ZoneInfo("America/Chicago")

FROZEN_CONFIGURATION: dict[str, Any] = {
    "quantiles": {"p75": 0.75, "p90": 0.90},
    "minimum_authenticated_observations": MIN_AUTHENTICATED_OBSERVATIONS,
    "minimum_authenticated_sessions": MIN_AUTHENTICATED_SESSIONS,
    "measurement": "direction_aware_adverse_fill_vs_point_in_time_quote_mid_bps_plus_commission",
    "missing_observation_policy": "null_and_blocked",
    "provisional_fallback": PROVISIONAL_COST_MODEL_VERSION,
    "promotion_policy": "manual_only",
}


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
        ).encode("utf-8")
    ).hexdigest()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0 else None


def _nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


def _quantile(values: Sequence[float], probability: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    # Nearest-rank is deterministic and conservative for sparse evidence.
    index = max(0, min(len(ordered) - 1, math.ceil(probability * len(ordered)) - 1))
    return round(ordered[index], 6)


def _is_hash(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    if len(text) != 64:
        return False
    return all(char in "0123456789abcdef" for char in text)


def _is_code_identity(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    if len(text) not in {40, 64}:
        return False
    return all(char in "0123456789abcdef" for char in text)


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None
    except ValueError:
        return None


def _leg(
    row: Mapping[str, Any], prefix: str
) -> tuple[float, float, float, datetime, datetime] | None:
    nested = row.get(prefix)
    values = nested if isinstance(nested, Mapping) else row
    fill = _number(
        values.get(f"{prefix}_fill_price") if values is row else values.get("fill_price")
    )
    quote = _number(
        values.get(f"{prefix}_quote_mid_price") if values is row else values.get("quote_mid_price")
    )
    commission = values.get(f"{prefix}_commission") if values is row else values.get("commission")
    commission_value = _nonnegative(commission)
    fill_at = _timestamp(
        values.get(f"{prefix}_fill_at") if values is row else values.get("fill_at")
    )
    quote_at = _timestamp(
        values.get(f"{prefix}_quote_at") if values is row else values.get("quote_at")
    )
    if commission_value is None or fill_at is None or quote_at is None:
        return None
    if fill is None or quote is None:
        return None
    if not all(
        _is_hash(values.get(name))
        for name in (
            f"{prefix}_fill_hash_sha256" if values is row else "fill_hash_sha256",
            f"{prefix}_quote_hash_sha256" if values is row else "quote_hash_sha256",
            f"{prefix}_source_lineage_hash_sha256"
            if values is row
            else "source_lineage_hash_sha256",
        )
    ):
        return None
    return fill, quote, commission_value, fill_at, quote_at


def _observation_cost_bps(row: Mapping[str, Any]) -> float | None:
    row_source_hash = row.get("source_lineage_hash_sha256")
    for prefix in ("entry", "exit"):
        leg = row.get(prefix)
        leg_source_hash = (
            leg.get("source_lineage_hash_sha256")
            if isinstance(leg, Mapping)
            else row.get(f"{prefix}_source_lineage_hash_sha256")
        )
        if leg_source_hash != row_source_hash:
            return None
    entry = _leg(row, "entry")
    exit_ = _leg(row, "exit")
    if entry is None or exit_ is None:
        return None
    entry_fill, entry_quote, entry_commission, entry_fill_at, entry_quote_at = entry
    exit_fill, exit_quote, exit_commission, exit_fill_at, exit_quote_at = exit_
    direction = str(row.get("direction") or "").strip().lower()
    if direction not in {"long", "short"}:
        return None
    session_date = _canonical_session_date(row)
    leg_times = (entry_fill_at, entry_quote_at, exit_fill_at, exit_quote_at)
    if session_date is None or any(
        observed.astimezone(MARKET_TIMEZONE).date().isoformat() != session_date
        for observed in leg_times
    ):
        return None
    # Long entry and short exit pay above the midpoint; long exit and short
    # entry pay below it. Favorable price improvement is not negative cost.
    entry_adverse = entry_fill - entry_quote if direction == "long" else entry_quote - entry_fill
    exit_adverse = exit_quote - exit_fill if direction == "long" else exit_fill - exit_quote
    entry_cost = max(0.0, entry_adverse) / entry_quote * 10_000
    exit_cost = max(0.0, exit_adverse) / exit_quote * 10_000
    commission = entry_commission + exit_commission
    notional = _number(row.get("round_trip_notional"))
    if notional is None:
        return None
    commission_bps = commission / notional * 10_000
    decision_at = _timestamp(row.get("decision_at"))
    if decision_at is None or not (
        decision_at <= entry_quote_at
        and decision_at <= entry_fill_at
        and entry_quote_at <= entry_fill_at
        and decision_at <= exit_quote_at
        and decision_at <= exit_fill_at
        and exit_quote_at <= exit_fill_at
        and entry_fill_at <= exit_fill_at
        and entry_quote_at <= exit_quote_at
    ):
        return None
    return round(entry_cost + exit_cost + commission_bps, 6)


def _point_in_time_quote_fill(row: Mapping[str, Any]) -> bool:
    point_in_time = row.get("point_in_time")
    if not isinstance(point_in_time, Mapping):
        return False
    if point_in_time.get("all_inputs_observed_at_or_before_decision") is not True:
        return False
    return bool(
        _is_hash(row.get("input_hash_sha256"))
        and _is_hash(row.get("source_lineage_hash_sha256"))
        and _timestamp(row.get("decision_at")) is not None
    )


def _canonical_session_date(row: Mapping[str, Any]) -> str | None:
    decision_at = _timestamp(row.get("decision_at"))
    if decision_at is None:
        return None
    observed = decision_at.astimezone(MARKET_TIMEZONE).date()
    market_date = str(row.get("market_date") or "")
    try:
        declared = date.fromisoformat(market_date)
    except ValueError:
        return None
    if declared.isoformat() != market_date or declared != observed:
        return None
    return market_date


def _window_contains(session_date: str, window: Mapping[str, Any]) -> bool:
    try:
        if window.get("date") is not None:
            raw = str(window["date"])
            return raw == date.fromisoformat(raw).isoformat() == session_date
        start_raw = str(window["start"])
        end_raw = str(window["end"])
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        start_raw == start.isoformat()
        and end_raw == end.isoformat()
        and start <= date.fromisoformat(session_date) <= end
    )


def _observation_identity(row: Mapping[str, Any]) -> str:
    return str(row.get("observation_id") or "").strip()


def build_empirical_cost_challenger(
    observations: Sequence[Mapping[str, Any]],
    *,
    source_manifest: Mapping[str, Any],
    code_sha: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a p75/p90 receipt from authenticated point-in-time evidence only.

    Authentication is resolved only through the repository's FillTruth
    boundary; callers cannot authenticate data by setting a JSON field or
    passing a callback.  A governed CommitBridge implementation can replace
    that boundary in the owning integration layer.
    """

    if not _is_code_identity(code_sha):
        raise ValueError("code_sha is required")
    if not source_manifest or not window:
        raise ValueError("source_manifest and window are required")
    candidates: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    for source in observations:
        row = dict(source)
        identity = _observation_identity(row)
        session_date = _canonical_session_date(row)
        if not identity:
            rejected["missing_observation_identity"] = rejected.get(
                "missing_observation_identity", 0
            ) + 1
            continue
        if row.get("research_only") is not True or row.get("broker_execution_enabled") is not False:
            rejected["research_only_broker_contract"] = rejected.get(
                "research_only_broker_contract", 0
            ) + 1
            continue
        if session_date is None or not _window_contains(session_date, window):
            rejected["observation_outside_or_invalid_window"] = rejected.get(
                "observation_outside_or_invalid_window", 0
            ) + 1
            continue
        if not _point_in_time_quote_fill(row):
            rejected["invalid_point_in_time_lineage"] = rejected.get(
                "invalid_point_in_time_lineage", 0
            ) + 1
            continue
        if not has_authenticated_committed_fill_truth(row):
            rejected["unauthenticated_fill_truth"] = rejected.get(
                "unauthenticated_fill_truth", 0
            ) + 1
            continue
        cost_bps = _observation_cost_bps(row)
        if cost_bps is None:
            rejected["missing_or_invalid_quote_fill_fields"] = rejected.get(
                "missing_or_invalid_quote_fill_fields", 0
            ) + 1
            continue
        row["observed_cost_bps"] = cost_bps
        row["canonical_session_date"] = session_date
        candidates.append(row)
    accepted_by_identity: dict[str, dict[str, Any]] = {}
    conflicted: set[str] = set()
    for row in sorted(
        candidates, key=lambda item: (_observation_identity(item), canonical_hash(item))
    ):
        identity = _observation_identity(row)
        existing = accepted_by_identity.get(identity)
        if existing is not None:
            conflicted.add(identity)
            rejected["duplicate_or_conflicting_observation_identity"] = rejected.get(
                "duplicate_or_conflicting_observation_identity", 0
            ) + 1
            continue
        accepted_by_identity[identity] = row
    accepted = [
        accepted_by_identity[identity]
        for identity in sorted(accepted_by_identity)
        if identity not in conflicted
    ]
    ordered_input = sorted(
        (dict(row) for row in observations),
        key=lambda item: (_observation_identity(item), canonical_hash(item)),
    )
    costs = [float(row["observed_cost_bps"]) for row in accepted]
    sessions = {
        str(row["canonical_session_date"])
        for row in accepted
    }
    sufficient_observations = len(costs) >= MIN_AUTHENTICATED_OBSERVATIONS
    sufficient_sessions = len(sessions) >= MIN_AUTHENTICATED_SESSIONS
    sufficient = sufficient_observations and sufficient_sessions
    status = (
        "EMPIRICAL_COST_EVALUABLE"
        if sufficient
        else "BLOCKED_INSUFFICIENT_AUTHENTICATED_PIT_EVIDENCE"
    )
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "challenger_version": EMPIRICAL_COST_CHALLENGER_VERSION,
        "configuration": dict(FROZEN_CONFIGURATION),
        "configuration_hash_sha256": canonical_hash(FROZEN_CONFIGURATION),
        "input_observations_hash_sha256": canonical_hash(ordered_input),
        "source_manifest": dict(source_manifest),
        "source_manifest_hash_sha256": canonical_hash(source_manifest),
        "code_sha": str(code_sha),
        "window": dict(window),
        "window_hash_sha256": canonical_hash(window),
        "status": status,
        "authenticated_observation_count": len(accepted),
        "authenticated_session_count": len(sessions),
        "minimum_observations_met": sufficient_observations,
        "minimum_sessions_met": sufficient_sessions,
        "rejected_observation_counts": dict(sorted(rejected.items())),
        "p75_cost_bps": _quantile(costs, 0.75) if sufficient else None,
        "p90_cost_bps": _quantile(costs, 0.90) if sufficient else None,
        "p75_bps": _quantile(costs, 0.75) if sufficient else None,
        "p90_bps": _quantile(costs, 0.90) if sufficient else None,
        "cost_model_version": EMPIRICAL_COST_CHALLENGER_VERSION,
        "model_version": EMPIRICAL_COST_CHALLENGER_VERSION,
        "output": {
            "p75_cost_bps": _quantile(costs, 0.75) if sufficient else None,
            "p90_cost_bps": _quantile(costs, 0.90) if sufficient else None,
            "cost_model_version": EMPIRICAL_COST_CHALLENGER_VERSION,
            "model_version": EMPIRICAL_COST_CHALLENGER_VERSION,
        },
        "evidence_rows": accepted if sufficient else [],
        "provisional_champion_cost_model_version": PROVISIONAL_COST_MODEL_VERSION,
        "provisional_champion_cost_model_unchanged": True,
        "research_only": True,
        "promotion_eligible": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }
    receipt["output_hash_sha256"] = canonical_hash(receipt["output"])
    receipt["model_hash_sha256"] = canonical_hash(
        {
            "model_version": EMPIRICAL_COST_CHALLENGER_VERSION,
            "configuration_hash_sha256": receipt["configuration_hash_sha256"],
        }
    )
    receipt["receipt_hash_sha256"] = canonical_hash(receipt)
    return receipt


def persist_empirical_cost_receipt(path: str | Path, receipt: Mapping[str, Any]) -> bool:
    """Persist once and reject any attempted mutation of an existing receipt."""

    target = Path(path)
    declared_hash = str(receipt.get("receipt_hash_sha256") or "")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    if not declared_hash or declared_hash != canonical_hash(unsigned):
        raise ValueError("receipt self-hash is missing or invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    )
    if target.exists():
        if target.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"immutable empirical cost receipt changed: {target}")
        return True
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            if target.read_text(encoding="utf-8") != encoded:
                raise ValueError(f"immutable empirical cost receipt changed: {target}") from None
            return True
        return False
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


# Explicit names used by research callers; these remain the same frozen
# implementation and do not create a second cost-model identity.
fit_empirical_execution_cost_challenger = build_empirical_cost_challenger
build_empirical_execution_cost_receipt = build_empirical_cost_challenger


__all__ = [
    "EMPIRICAL_COST_CHALLENGER_VERSION",
    "FROZEN_CONFIGURATION",
    "MIN_AUTHENTICATED_OBSERVATIONS",
    "MIN_AUTHENTICATED_SESSIONS",
    "PROVISIONAL_COST_MODEL_VERSION",
    "SCHEMA_VERSION",
    "build_empirical_cost_challenger",
    "build_empirical_execution_cost_receipt",
    "canonical_hash",
    "fit_empirical_execution_cost_challenger",
    "persist_empirical_cost_receipt",
]
