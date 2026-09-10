"""Typed outcome semantics shared by AlphaOps reporting consumers.

Excursions describe the path.  A realized return describes the selected exit
and must never be inferred from a favorable or adverse excursion.
"""

from __future__ import annotations

import math
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
    """Reconcile drawdown from an ordered account-equity series."""

    peak: float | None = None
    worst: float | None = None
    for row in rows:
        equity = finite_number(row.get("account_equity"))
        if equity is None:
            continue
        peak = equity if peak is None else max(peak, equity)
        if peak:
            drawdown = ((equity / peak) - 1.0) * 100.0
            worst = drawdown if worst is None else min(worst, drawdown)
    return worst


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
    "finite_number",
    "maximum_adverse_excursion",
    "maximum_favorable_excursion",
    "realized_return",
]
