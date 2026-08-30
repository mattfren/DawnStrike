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

# The older ``build_empirical_cost_challenger`` surface is retained for
# compatibility with the Cycle-2 shadow reranker.  The cost-model receipt
# surface below is the stricter implementation used by new ledger/backtest
# consumers.  It never changes the V5 champion assumption.
COST_COMPONENTS = ("spread", "slippage", "fees", "regulatory", "borrow")
COST_BUCKET_DIMENSIONS = (
    "price",
    "dollar_liquidity",
    "participation_rate",
    "volatility",
    "time_of_day",
    "side",
    "order_type",
    "venue_feed",
)
_COST_BUCKET_FALLBACKS = (
    ("EXACT", COST_BUCKET_DIMENSIONS),
    ("DROP_VENUE_FEED", COST_BUCKET_DIMENSIONS[:-1]),
    ("DROP_ORDER_TYPE", COST_BUCKET_DIMENSIONS[:-2]),
    ("DROP_SIDE", COST_BUCKET_DIMENSIONS[:-3]),
    ("DROP_TIME_OF_DAY", COST_BUCKET_DIMENSIONS[:-4]),
    ("DROP_VOLATILITY", COST_BUCKET_DIMENSIONS[:-5]),
    ("DROP_PARTICIPATION_RATE", COST_BUCKET_DIMENSIONS[:-6]),
    ("DROP_DOLLAR_LIQUIDITY", COST_BUCKET_DIMENSIONS[:-7]),
    ("GLOBAL", ()),
)
EMPIRICAL_COST_MODEL_SCHEMA_VERSION = "dawnstrike.alpha.empirical_execution_cost_model.v2"
EMPIRICAL_COST_MODEL_VERSION = "dawnstrike-empirical-execution-cost-p50-p75-p90.v1"
MIN_COST_MODEL_OBSERVATIONS = 20
MIN_COST_MODEL_SESSIONS = 5
MIN_COST_BUCKET_OBSERVATIONS = 5

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


def _finite_nonnegative(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    result = float(value)
    return result if math.isfinite(result) and result >= 0 else None


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
    decision_at = _timestamp(row.get("decision_at") or row.get("entry_at"))
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
            rejected["missing_observation_identity"] = (
                rejected.get("missing_observation_identity", 0) + 1
            )
            continue
        if row.get("research_only") is not True or row.get("broker_execution_enabled") is not False:
            rejected["research_only_broker_contract"] = (
                rejected.get("research_only_broker_contract", 0) + 1
            )
            continue
        if session_date is None or not _window_contains(session_date, window):
            rejected["observation_outside_or_invalid_window"] = (
                rejected.get("observation_outside_or_invalid_window", 0) + 1
            )
            continue
        if not _point_in_time_quote_fill(row):
            rejected["invalid_point_in_time_lineage"] = (
                rejected.get("invalid_point_in_time_lineage", 0) + 1
            )
            continue
        if not has_authenticated_committed_fill_truth(row):
            rejected["unauthenticated_fill_truth"] = (
                rejected.get("unauthenticated_fill_truth", 0) + 1
            )
            continue
        cost_bps = _observation_cost_bps(row)
        if cost_bps is None:
            rejected["missing_or_invalid_quote_fill_fields"] = (
                rejected.get("missing_or_invalid_quote_fill_fields", 0) + 1
            )
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
            rejected["duplicate_or_conflicting_observation_identity"] = (
                rejected.get("duplicate_or_conflicting_observation_identity", 0) + 1
            )
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
    sessions = {str(row["canonical_session_date"]) for row in accepted}
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
        json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n"
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


def _component_value(row: Mapping[str, Any], component: str) -> float | None:
    """Read one observed component without turning an absent value into zero."""

    aliases = {
        "spread": ("spread_bps", "observed_spread_bps"),
        "slippage": ("slippage_bps", "observed_slippage_bps"),
        "fees": ("fees_bps", "fee_bps", "commission_bps", "observed_fees_bps"),
        "regulatory": (
            "regulatory_bps",
            "regulatory_fee_bps",
            "observed_regulatory_bps",
        ),
        "borrow": ("borrow_bps", "borrow_cost_bps", "observed_borrow_bps"),
    }
    nested_sources: list[Mapping[str, Any]] = []
    for key in (
        "observed_cost_components",
        "cost_components",
        "execution_cost_components",
        "components",
    ):
        value = row.get(key)
        if isinstance(value, Mapping):
            nested_sources.append(value)
    nested_sources.append(row)
    notional = _number(
        row.get("round_trip_notional", row.get("notional", row.get("dollar_notional")))
    )
    if notional is None:
        quantity = _finite_nonnegative(row.get("quantity"))
        entry_price = _number(row.get("entry_price"))
        if quantity is not None and quantity > 0 and entry_price is not None:
            notional = quantity * entry_price
    for source in nested_sources:
        raw: object = None
        for key in (component, *aliases[component]):
            if key in source:
                raw = source[key]
                break
        if isinstance(raw, Mapping):
            raw = raw.get("bps", raw.get("basis_points"))
        value = _finite_nonnegative(raw)
        if value is not None:
            return value
        # Cents/dollars are accepted only with an observed notional.  Missing
        # notional remains unknown rather than becoming a zero-cost estimate.
        for key in (f"{component}_cost_cents", f"{component}_cents"):
            cents = _finite_nonnegative(source.get(key))
            if cents is not None and notional is not None and notional > 0:
                return cents / 100.0 / notional * 10_000.0
        for key in (f"{component}_cost", f"{component}_dollars"):
            dollars = _finite_nonnegative(source.get(key))
            if dollars is not None and notional is not None and notional > 0:
                return dollars / notional * 10_000.0
    return None


def _derived_component_values(row: Mapping[str, Any]) -> dict[str, float | None]:
    """Normalize explicit components, with legacy slippage/fee derivation only."""

    result = {component: _component_value(row, component) for component in COST_COMPONENTS}
    # A legacy point-in-time row has enough data to derive adverse slippage and
    # commission, but not spread/regulatory/borrow.  Preserve the distinction.
    if result["slippage"] is None:
        entry = _leg(row, "entry")
        exit_ = _leg(row, "exit")
        direction = str(row.get("direction") or "").strip().lower()
        if entry is not None and exit_ is not None and direction in {"long", "short"}:
            entry_fill, entry_quote, _, _, _ = entry
            exit_fill, exit_quote, _, _, _ = exit_
            entry_adverse = (
                entry_fill - entry_quote if direction == "long" else entry_quote - entry_fill
            )
            exit_adverse = exit_quote - exit_fill if direction == "long" else exit_fill - exit_quote
            result["slippage"] = round(
                max(0.0, entry_adverse) / entry_quote * 10_000.0
                + max(0.0, exit_adverse) / exit_quote * 10_000.0,
                6,
            )
    if result["fees"] is None:
        entry = _leg(row, "entry")
        exit_ = _leg(row, "exit")
        notional = _number(row.get("round_trip_notional"))
        if entry is not None and exit_ is not None and notional is not None and notional > 0:
            result["fees"] = round((entry[2] + exit_[2]) / notional * 10_000.0, 6)
    return result


def _bucket_label(value: object, dimension: str) -> str:
    number = _finite_nonnegative(value)
    if dimension == "price":
        if number is None:
            return "UNKNOWN"
        return (
            "LT_5"
            if number < 5
            else "5_20"
            if number < 20
            else "20_50"
            if number < 50
            else "50_100"
            if number < 100
            else "GE_100"
        )
    if dimension == "dollar_liquidity":
        if number is None:
            return "UNKNOWN"
        return (
            "LT_1M"
            if number < 1_000_000
            else "1M_10M"
            if number < 10_000_000
            else "10M_100M"
            if number < 100_000_000
            else "GE_100M"
        )
    if dimension == "participation_rate":
        if number is None:
            return "UNKNOWN"
        return (
            "LT_10BP"
            if number < 0.001
            else "10_50BP"
            if number < 0.005
            else "50_100BP"
            if number < 0.01
            else "1_5PCT"
            if number < 0.05
            else "GE_5PCT"
        )
    if dimension == "volatility":
        if number is None:
            return "UNKNOWN"
        return (
            "LT_1PCT"
            if number < 1
            else "1_2PCT"
            if number < 2
            else "2_5PCT"
            if number < 5
            else "5_10PCT"
            if number < 10
            else "GE_10PCT"
        )
    return str(value or "UNKNOWN").strip().upper() or "UNKNOWN"


def _cost_bucket_dimensions(row: Mapping[str, Any]) -> dict[str, str]:
    entry_value = row.get("entry")
    entry: Mapping[str, Any] = entry_value if isinstance(entry_value, Mapping) else {}
    price = row.get("price", row.get("entry_price", entry.get("fill_price")))
    liquidity = row.get(
        "dollar_liquidity", row.get("average_dollar_volume", row.get("avg_dollar_volume"))
    )
    participation = row.get("participation_rate", row.get("participation"))
    volatility = row.get("volatility", row.get("volatility_pct", row.get("atr_pct")))
    observed_at = _timestamp(row.get("decision_at"))
    if row.get("time_of_day") is not None:
        tod = str(row.get("time_of_day") or "UNKNOWN").strip().upper() or "UNKNOWN"
    elif observed_at is None:
        tod = "UNKNOWN"
    else:
        local_time = observed_at.astimezone(MARKET_TIMEZONE)
        minutes = local_time.hour * 60 + local_time.minute
        tod = (
            "OPEN_30M"
            if minutes < 9 * 60 + 30
            else "MORNING"
            if minutes < 11 * 60
            else "MIDDAY"
            if minutes < 14 * 60
            else "AFTERNOON"
            if minutes < 15 * 60 + 30
            else "CLOSE_30M"
        )
    side = row.get("side", row.get("direction"))
    venue = str(row.get("venue") or "UNKNOWN").strip().upper() or "UNKNOWN"
    feed = str(row.get("feed") or row.get("data_feed") or "UNKNOWN").strip().upper() or "UNKNOWN"
    direct_venue_feed = str(row.get("venue_feed") or "").strip().upper()
    return {
        "price": _bucket_label(price, "price"),
        "dollar_liquidity": _bucket_label(liquidity, "dollar_liquidity"),
        "participation_rate": _bucket_label(participation, "participation_rate"),
        "volatility": _bucket_label(volatility, "volatility"),
        "time_of_day": tod,
        "side": str(side or "UNKNOWN").strip().upper() or "UNKNOWN",
        "order_type": str(row.get("order_type") or "UNKNOWN").strip().upper() or "UNKNOWN",
        "venue_feed": direct_venue_feed or f"{venue}/{feed}",
    }


def _component_summary(
    rows: Sequence[Mapping[str, Any]],
    component: str,
    minimum_observations: int,
    minimum_sessions: int,
) -> dict[str, Any]:
    values = [
        (float(value), str(row["canonical_session_date"]))
        for row in rows
        if (value := row.get("cost_components_normalized", {}).get(component)) is not None
    ]
    numbers = [value for value, _ in values]
    sessions = {session for _, session in values}
    sufficient = len(numbers) >= minimum_observations and len(sessions) >= minimum_sessions
    quantiles = {
        f"p{percentile}": _quantile(numbers, percentile / 100) if sufficient else None
        for percentile in (50, 75, 90)
    }
    if sufficient:
        status = "EVALUABLE"
    elif not numbers:
        status = "NOT_EVALUABLE_MISSING_OBSERVATIONS"
    else:
        status = "INSUFFICIENT_EVIDENCE"
    level = (
        "HIGH"
        if len(numbers) >= 100 and len(sessions) >= 20
        else "MEDIUM"
        if len(numbers) >= 50 and len(sessions) >= 10
        else "LOW"
        if sufficient
        else "NONE"
    )
    return {
        "status": status,
        "quantiles_bps": quantiles,
        "p50_bps": quantiles["p50"],
        "p75_bps": quantiles["p75"],
        "p90_bps": quantiles["p90"],
        "sample_count": len(numbers),
        "session_count": len(sessions),
        "confidence": {
            "level": level,
            "sample_count": len(numbers),
            "session_count": len(sessions),
        },
        "missing_count": len(rows) - len(numbers),
    }


def _model_group(
    rows: Sequence[Mapping[str, Any]],
    dimensions: Sequence[str],
    minimum_observations: int,
    minimum_sessions: int,
    *,
    level: str,
) -> dict[str, Any]:
    component_models = {
        component: _component_summary(rows, component, minimum_observations, minimum_sessions)
        for component in COST_COMPONENTS
    }
    total_rows = [
        row
        for row in rows
        if all(
            row.get("cost_components_normalized", {}).get(component) is not None
            for component in COST_COMPONENTS
        )
    ]
    total = _component_summary(
        [
            {
                **row,
                "cost_components_normalized": {
                    "total": sum(
                        float(row["cost_components_normalized"][component])
                        for component in COST_COMPONENTS
                    )
                },
            }
            for row in total_rows
        ],
        "total",
        minimum_observations,
        minimum_sessions,
    )
    return {
        "bucket_level": level,
        "dimensions": list(dimensions),
        "bucket": {
            dimension: (rows[0]["cost_bucket_dimensions"][dimension] if rows else "UNKNOWN")
            for dimension in dimensions
        },
        "sample_count": len(rows),
        "session_count": len({str(row["canonical_session_date"]) for row in rows}),
        "components": component_models,
        "total": total,
    }


def _strict_receipt_valid(
    receipt: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any] | None = None,
    code_sha: str | None = None,
    window: Mapping[str, Any] | None = None,
) -> bool:
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    if receipt.get("receipt_hash_sha256") != canonical_hash(unsigned):
        return False
    if receipt.get("schema_version") != EMPIRICAL_COST_MODEL_SCHEMA_VERSION:
        return False
    if (
        receipt.get("research_only") is not True
        or receipt.get("broker_execution_enabled") is not False
    ):
        return False
    if (
        receipt.get("promotion_eligible") is not False
        or receipt.get("champion_cost_model_unchanged") is not True
    ):
        return False
    manifest = receipt.get("source_manifest")
    declared_manifest_hash = receipt.get("source_manifest_hash_sha256")
    if not isinstance(manifest, Mapping) or declared_manifest_hash != canonical_hash(manifest):
        return False
    if source_manifest is not None and declared_manifest_hash != canonical_hash(source_manifest):
        return False
    declared_code_sha = str(receipt.get("code_sha") or "")
    if not _is_code_identity(declared_code_sha) or (
        code_sha is not None and declared_code_sha != str(code_sha)
    ):
        return False
    declared_window = receipt.get("window")
    if not isinstance(declared_window, Mapping) or receipt.get(
        "window_hash_sha256"
    ) != canonical_hash(declared_window):
        return False
    if window is not None and receipt.get("window_hash_sha256") != canonical_hash(window):
        return False
    return True


def build_empirical_execution_cost_model_receipt(
    observations: Sequence[Mapping[str, Any]],
    *,
    source_manifest: Mapping[str, Any],
    code_sha: str,
    window: Mapping[str, Any],
    minimum_observations: int = MIN_COST_MODEL_OBSERVATIONS,
    minimum_sessions: int = MIN_COST_MODEL_SESSIONS,
    minimum_bucket_observations: int = MIN_COST_BUCKET_OBSERVATIONS,
) -> dict[str, Any]:
    """Build a versioned, receipt-bound cost challenger from closed FillTruth."""

    if not _is_code_identity(code_sha):
        raise ValueError("code_sha is required")
    if (
        not isinstance(source_manifest, Mapping)
        or not source_manifest
        or not isinstance(window, Mapping)
        or not window
    ):
        raise ValueError("source_manifest and window are required")
    accepted: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    for source in observations:
        row = dict(source)
        # AuthenticatedFillTruth is a Mapping; plain JSON, even with a valid
        # looking hash/status, is deliberately rejected by this boundary.
        if not has_authenticated_committed_fill_truth(source):
            rejected["unauthenticated_fill_truth"] = (
                rejected.get("unauthenticated_fill_truth", 0) + 1
            )
            continue
        if row.get("research_only") is not True or row.get("broker_execution_enabled") is not False:
            rejected["research_only_broker_contract"] = (
                rejected.get("research_only_broker_contract", 0) + 1
            )
            continue
        if (
            str(row.get("execution_status") or "").upper() != "CLOSED"
            or str(row.get("fill_truth_status") or "COMMITTED").upper() != "COMMITTED"
        ):
            rejected["closed_committed_fill_required"] = (
                rejected.get("closed_committed_fill_required", 0) + 1
            )
            continue
        session_date = _canonical_session_date(row)
        if session_date is None or not _window_contains(session_date, window):
            rejected["invalid_window_or_session"] = rejected.get("invalid_window_or_session", 0) + 1
            continue
        identity = str(row.get("observation_id") or row.get("receipt_id") or "").strip()
        if not identity:
            rejected["missing_observation_identity"] = (
                rejected.get("missing_observation_identity", 0) + 1
            )
            continue
        components = _derived_component_values(row)
        if all(value is None for value in components.values()):
            rejected["missing_cost_components"] = rejected.get("missing_cost_components", 0) + 1
            continue
        row["observation_id"] = identity
        row["canonical_session_date"] = session_date
        row["cost_components_normalized"] = components
        row["cost_bucket_dimensions"] = _cost_bucket_dimensions(row)
        accepted.append(row)
    accepted.sort(key=lambda row: (str(row["observation_id"]), canonical_hash(row)))
    deduped: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in accepted:
        identity = str(row["observation_id"])
        if identity in seen:
            rejected["duplicate_observation_identity"] = (
                rejected.get("duplicate_observation_identity", 0) + 1
            )
            continue
        seen.add(identity)
        deduped.append(row)
    accepted = deduped
    sessions = {str(row["canonical_session_date"]) for row in accepted}
    eligible = len(accepted) >= minimum_observations and len(sessions) >= minimum_sessions
    component_models = {
        component: _component_summary(accepted, component, minimum_observations, minimum_sessions)
        for component in COST_COMPONENTS
    }
    full_component_rows = [
        row
        for row in accepted
        if all(
            row["cost_components_normalized"].get(component) is not None
            for component in COST_COMPONENTS
        )
    ]
    total_rows = [
        {
            **row,
            "cost_components_normalized": {
                "total": sum(
                    float(row["cost_components_normalized"][component])
                    for component in COST_COMPONENTS
                )
            },
        }
        for row in full_component_rows
    ]
    total = _component_summary(total_rows, "total", minimum_observations, minimum_sessions)
    buckets: list[dict[str, Any]] = []
    for level, dimensions in _COST_BUCKET_FALLBACKS:
        grouped: dict[tuple[tuple[str, str], ...], list[dict[str, Any]]] = {}
        for row in accepted:
            key = tuple(
                (dimension, row["cost_bucket_dimensions"][dimension]) for dimension in dimensions
            )
            grouped.setdefault(key, []).append(row)
        for _key, rows in sorted(grouped.items(), key=lambda item: item[0]):
            buckets.append(
                _model_group(
                    rows,
                    dimensions,
                    minimum_bucket_observations if level != "GLOBAL" else minimum_observations,
                    minimum_sessions,
                    level=level,
                )
            )
    input_rows = [
        {
            key: value
            for key, value in row.items()
            if key not in {"cost_components_normalized", "cost_bucket_dimensions"}
        }
        for row in observations
    ]
    receipt: dict[str, Any] = {
        "schema_version": EMPIRICAL_COST_MODEL_SCHEMA_VERSION,
        "cost_model_version": EMPIRICAL_COST_MODEL_VERSION,
        "configuration": {
            "components": list(COST_COMPONENTS),
            "bucket_dimensions": list(COST_BUCKET_DIMENSIONS),
            "fallback_hierarchy": [level for level, _ in _COST_BUCKET_FALLBACKS],
            "minimum_observations": minimum_observations,
            "minimum_sessions": minimum_sessions,
            "minimum_bucket_observations": minimum_bucket_observations,
            "quantiles": ["p50", "p75", "p90"],
            "missing_value_policy": "unknown_and_blocked_not_zero",
            "promotion_policy": "manual_only_shadow_only",
        },
        "source_manifest": dict(source_manifest),
        "source_manifest_hash_sha256": canonical_hash(source_manifest),
        "code_sha": code_sha,
        "window": dict(window),
        "window_hash_sha256": canonical_hash(window),
        "input_observations_hash_sha256": canonical_hash(input_rows),
        "status": "EVALUABLE" if eligible else "NOT_EVALUABLE",
        "evidence_status": "EVALUABLE" if eligible else "INSUFFICIENT_EVIDENCE",
        "authenticated_observation_count": len(accepted),
        "authenticated_session_count": len(sessions),
        "minimum_observations_met": len(accepted) >= minimum_observations,
        "minimum_sessions_met": len(sessions) >= minimum_sessions,
        "rejected_observation_counts": dict(sorted(rejected.items())),
        "components": component_models,
        "total": total,
        "buckets": buckets,
        "fallback_hierarchy": [
            {"level": level, "dimensions": list(dimensions), "requires_observed_data": True}
            for level, dimensions in _COST_BUCKET_FALLBACKS
        ],
        "stress": {
            "status": "EVALUABLE_2X_STRESS" if eligible else "NOT_EVALUABLE",
            "multiplier": 2.0,
            "components": {
                component: {
                    key: (value * 2.0 if isinstance(value, (int, float)) else None)
                    for key, value in model["quantiles_bps"].items()
                }
                for component, model in component_models.items()
            },
            "total": {
                key: (value * 2.0 if isinstance(value, (int, float)) else None)
                for key, value in total["quantiles_bps"].items()
            },
        },
        "evidence_rows": accepted if eligible else [],
        "research_only": True,
        "promotion_eligible": False,
        "automatic_promotion": False,
        "champion_cost_model_version": PROVISIONAL_COST_MODEL_VERSION,
        "champion_cost_model_unchanged": True,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }
    receipt["configuration_hash_sha256"] = canonical_hash(receipt["configuration"])
    receipt["model_hash_sha256"] = canonical_hash(
        {
            "cost_model_version": EMPIRICAL_COST_MODEL_VERSION,
            "configuration_hash_sha256": receipt["configuration_hash_sha256"],
        }
    )
    receipt["output_hash_sha256"] = canonical_hash(
        {"components": component_models, "total": total, "stress": receipt["stress"]}
    )
    receipt["receipt_hash_sha256"] = canonical_hash(receipt)
    return receipt


def select_empirical_cost(
    receipt: Mapping[str, Any],
    *,
    dimensions: Mapping[str, Any] | None = None,
    quantile: str = "p75",
    source_manifest: Mapping[str, Any] | None = None,
    code_sha: str | None = None,
    window: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Request a p50/p75/p90 cost without granting an unverified empirical claim."""

    quantile = str(quantile).strip().lower()
    result: dict[str, Any] = {
        "status": "NOT_EVALUABLE",
        "empirical_claim": False,
        "quantile": quantile,
        "cost_bps": None,
        "components_bps": None,
        "stress_2x_bps": None,
        "fallback_level": None,
    }
    if quantile not in {"p50", "p75", "p90"} or not _strict_receipt_valid(
        receipt, source_manifest=source_manifest, code_sha=code_sha, window=window
    ):
        return result
    if (
        receipt.get("status") != "EVALUABLE"
        or not receipt.get("minimum_observations_met")
        or not receipt.get("minimum_sessions_met")
    ):
        result["status"] = "INSUFFICIENT_EVIDENCE"
        return result
    requested = _cost_bucket_dimensions(dimensions or {})
    minimum_bucket = int(
        receipt.get("configuration", {}).get(
            "minimum_bucket_observations", MIN_COST_BUCKET_OBSERVATIONS
        )
    )
    chosen: Mapping[str, Any] | None = None
    for level, level_dimensions in _COST_BUCKET_FALLBACKS:
        wanted = {dimension: requested[dimension] for dimension in level_dimensions}
        for candidate in receipt.get("buckets", []):
            if not isinstance(candidate, Mapping) or candidate.get("bucket_level") != level:
                continue
            if candidate.get("bucket") != wanted:
                continue
            if int(candidate.get("sample_count", 0)) < (
                int(
                    receipt.get("configuration", {}).get(
                        "minimum_observations", MIN_COST_MODEL_OBSERVATIONS
                    )
                )
                if level == "GLOBAL"
                else minimum_bucket
            ):
                continue
            chosen = candidate
            break
        if chosen is not None:
            break
    if chosen is None:
        result["status"] = "INSUFFICIENT_EVIDENCE"
        return result
    components_value = chosen.get("components")
    components: Mapping[str, Any] = (
        components_value if isinstance(components_value, Mapping) else {}
    )
    component_values: dict[str, float] = {}
    for component in COST_COMPONENTS:
        model = components.get(component)
        if (
            not isinstance(model, Mapping)
            or model.get("status") != "EVALUABLE"
            or not isinstance(model.get("quantiles_bps"), Mapping)
        ):
            result["status"] = "NOT_EVALUABLE_MISSING_COMPONENT_EVIDENCE"
            return result
        value = model["quantiles_bps"].get(quantile)
        if not isinstance(value, (int, float)):
            result["status"] = "NOT_EVALUABLE_MISSING_COMPONENT_EVIDENCE"
            return result
        component_values[component] = float(value)
    total_model = chosen.get("total")
    total_value = (
        total_model.get("quantiles_bps", {}).get(quantile)
        if isinstance(total_model, Mapping)
        else None
    )
    if not isinstance(total_value, (int, float)):
        result["status"] = "NOT_EVALUABLE_MISSING_COMPONENT_EVIDENCE"
        return result
    fallback_level = str(chosen.get("bucket_level"))
    result.update(
        {
            "status": "EVALUABLE_EMPIRICAL"
            if fallback_level == "EXACT"
            else "EVALUABLE_WITH_SPARSE_FALLBACK",
            "empirical_claim": True,
            "cost_bps": float(total_value),
            "components_bps": component_values,
            "stress_2x_bps": float(total_value) * 2.0,
            "fallback_level": fallback_level,
            "sample_count": chosen.get("sample_count"),
            "session_count": chosen.get("session_count"),
            "receipt_hash_sha256": receipt.get("receipt_hash_sha256"),
            "cost_model_version": receipt.get("cost_model_version"),
            "lineage": {
                "source_manifest_hash_sha256": receipt.get("source_manifest_hash_sha256"),
                "code_sha": receipt.get("code_sha"),
                "window_hash_sha256": receipt.get("window_hash_sha256"),
            },
        }
    )
    return result


def validate_empirical_cost_receipt(
    receipt: Mapping[str, Any],
    *,
    source_manifest: Mapping[str, Any] | None = None,
    code_sha: str | None = None,
    window: Mapping[str, Any] | None = None,
) -> bool:
    """Validate receipt/hash/lineage identity without making a cost claim."""

    return _strict_receipt_valid(
        receipt,
        source_manifest=source_manifest,
        code_sha=code_sha,
        window=window,
    )


# Intentional aliases make the contract discoverable to ledger/backtest code
# while retaining one implementation and one versioned model identity.
build_empirical_cost_model_receipt = build_empirical_execution_cost_model_receipt
calibrate_empirical_execution_cost = build_empirical_execution_cost_model_receipt
request_empirical_cost = select_empirical_cost
resolve_empirical_cost = select_empirical_cost
persist_empirical_cost_model_receipt = persist_empirical_cost_receipt


# Explicit names used by research callers; these remain the same frozen
# implementation and do not create a second cost-model identity.
fit_empirical_execution_cost_challenger = build_empirical_cost_challenger
build_empirical_execution_cost_receipt = build_empirical_cost_challenger


__all__ = [
    "COST_BUCKET_DIMENSIONS",
    "COST_COMPONENTS",
    "EMPIRICAL_COST_MODEL_SCHEMA_VERSION",
    "EMPIRICAL_COST_MODEL_VERSION",
    "EMPIRICAL_COST_CHALLENGER_VERSION",
    "FROZEN_CONFIGURATION",
    "MIN_AUTHENTICATED_OBSERVATIONS",
    "MIN_AUTHENTICATED_SESSIONS",
    "MIN_COST_BUCKET_OBSERVATIONS",
    "MIN_COST_MODEL_OBSERVATIONS",
    "MIN_COST_MODEL_SESSIONS",
    "PROVISIONAL_COST_MODEL_VERSION",
    "SCHEMA_VERSION",
    "build_empirical_cost_challenger",
    "build_empirical_cost_model_receipt",
    "build_empirical_execution_cost_model_receipt",
    "build_empirical_execution_cost_receipt",
    "calibrate_empirical_execution_cost",
    "canonical_hash",
    "fit_empirical_execution_cost_challenger",
    "persist_empirical_cost_receipt",
    "persist_empirical_cost_model_receipt",
    "request_empirical_cost",
    "resolve_empirical_cost",
    "select_empirical_cost",
    "validate_empirical_cost_receipt",
]
