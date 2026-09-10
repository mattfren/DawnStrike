"""Typed outcome semantics shared by AlphaOps reporting consumers.

Excursions describe the path.  A realized return describes the selected exit
and must never be inferred from a favorable or adverse excursion.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any

REALIZED_RETURN_FIELDS = (
    "net_return_pct",
    "after_cost_return_pct",
    "close_return_pct",
    "policy_exit_return_pct",
    "broker_realized_return_pct",
    "return_pct",
)
MFE_FIELDS = ("high_after_entry_return", "high_after_entry_return_pct")
MAE_FIELDS = ("low_after_entry_drawdown", "max_adverse_excursion")


def finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def realized_return(row: dict[str, Any]) -> float | None:
    """Return the explicit realized exit result, preserving zero and absence."""

    for field in REALIZED_RETURN_FIELDS:
        value = finite_number(row.get(field))
        if value is not None:
            return value
        if row.get(field) not in {None, ""}:
            return None
    return None


def maximum_favorable_excursion(row: dict[str, Any]) -> float | None:
    return _first_finite(row, MFE_FIELDS)


def maximum_adverse_excursion(row: dict[str, Any]) -> float | None:
    return _first_finite(row, MAE_FIELDS)


def account_drawdown(row: dict[str, Any]) -> float | None:
    """Read account-level drawdown only; trade MAE is a different quantity."""

    return finite_number(row.get("account_drawdown_pct"))


def account_equity_drawdown(rows: list[dict[str, Any]]) -> float | None:
    """Return chain-linked unitized account drawdown after external flows.

    A flow must be explicitly timed and denominated.  A start-of-valuation
    flow is removed before calculating the period return, so a deposit is not
    mistaken for performance.  Missing flow metadata makes the result
    unavailable rather than guessing.
    """

    index = 1.0
    peak = 1.0
    worst: float | None = 0.0
    previous_equity: float | None = None
    previous_currency: str | None = None
    for row in rows:
        equity = finite_number(row.get("account_equity"))
        if equity is None or equity <= 0:
            return None
        currency = str(row.get("valuation_currency") or row.get("currency") or "").strip()
        # The initial valuation has no prior interval and therefore needs no
        # flow row. Every later interval must carry an explicit finite flow,
        # including an explicit zero. A present non-finite value is invalid
        # measurement data and must stay unavailable; coercing NaN/inf or an
        # absent interval to zero would manufacture performance.
        raw_flow = row.get("cash_flow")
        has_explicit_flow = "cash_flow" in row and raw_flow not in {None, ""}
        if has_explicit_flow:
            flow = finite_number(raw_flow)
            if flow is None:
                return None
        else:
            flow = 0.0
        if flow != 0.0:
            timing = str(row.get("cash_flow_timing") or "").lower()
            flow_currency = str(row.get("cash_flow_currency") or currency).strip()
            if timing != "start" or not currency or flow_currency != currency:
                return None
        if previous_equity is None:
            if flow != 0.0:
                return None
            previous_equity = equity
            previous_currency = currency or None
            continue
        if not has_explicit_flow:
            return None
        if previous_currency and currency and previous_currency != currency:
            return None
        period_base = previous_equity
        period_end = equity - flow
        if period_base <= 0 or period_end <= 0:
            return None
        index *= period_end / period_base
        peak = max(peak, index)
        drawdown = ((index / peak) - 1.0) * 100.0
        worst = min(worst, drawdown)
        previous_equity = equity
        previous_currency = currency or previous_currency
    return worst if previous_equity is not None else None


def chronology_valid(
    *, decision: dict[str, Any], label: dict[str, Any] | None = None
) -> bool:
    """Require aware feature chronology and, for labels, post-decision availability."""

    decision_at = _timestamp(decision.get("decision_at"))
    point = decision.get("point_in_time")
    point = point if isinstance(point, dict) else {}
    feature_value = (
        decision.get("feature_timestamp")
        or decision.get("features_observed_at")
        or point.get("feature_timestamp")
        or point.get("features_observed_at")
        or point.get("latest_feature_timestamp")
    )
    feature_at = _timestamp(feature_value)
    available_value = (
        decision.get("feature_available_at")
        or point.get("feature_available_at")
        or point.get("features_available_at")
    )
    ingested_value = (
        decision.get("feature_ingested_at")
        or point.get("feature_ingested_at")
        or point.get("features_ingested_at")
    )
    available_at = _timestamp(available_value)
    ingested_at = _timestamp(ingested_value)
    if (
        decision_at is None
        or feature_at is None
        or available_at is None
        or ingested_at is None
        or not (feature_at <= available_at <= ingested_at <= decision_at)
    ):
        return False
    if label is None:
        return True
    available_value = next(
        (
            label.get(field)
            for field in ("label_available_at", "available_at", "matured_at", "observed_at")
            if label.get(field) not in {None, ""}
        ),
        None,
    )
    available_at = _timestamp(available_value)
    return available_at is not None and available_at > decision_at


def typed_return_contract_valid(label: dict[str, Any]) -> bool:
    """Reject ambiguous units, horizon, denominator, basis, or cost truth."""

    units = str(label.get("return_units") or "").lower()
    basis = str(label.get("return_basis") or "").lower()
    denominator = str(label.get("return_denominator") or "").lower()
    cost_status = str(label.get("cost_availability_status") or "").lower()
    horizon = finite_number(label.get("holding_horizon_minutes"))
    gross = finite_number(label.get("gross_return_pct"))
    net = finite_number(label.get("after_cost_return_pct"))
    return (
        units in {"pct", "percent"}
        and horizon is not None
        and horizon > 0
        and denominator in {"entry_notional", "entry_price"}
        and basis == "net_after_cost"
        and gross is not None
        and net is not None
        and cost_status in {"complete", "observed", "reconciled"}
        and _timestamp(label.get("label_available_at")) is not None
    )


def _timestamp(value: object) -> datetime | None:
    if value in {None, ""}:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _first_finite(row: dict[str, Any], fields: tuple[str, ...]) -> float | None:
    for field in fields:
        value = finite_number(row.get(field))
        if value is not None:
            return value
        if row.get(field) not in {None, ""}:
            return None
    return None


__all__ = [
    "account_drawdown",
    "account_equity_drawdown",
    "chronology_valid",
    "finite_number",
    "maximum_adverse_excursion",
    "maximum_favorable_excursion",
    "realized_return",
    "typed_return_contract_valid",
]
