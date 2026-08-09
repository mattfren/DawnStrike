"""Descriptive metrics for causal intraday replays."""

from __future__ import annotations

import math
import random
from collections.abc import Mapping, Sequence
from statistics import mean
from typing import Any


def compute_intraday_metrics(
    trades: Sequence[Any],
    equity_curve: Sequence[Any],
    *,
    session_returns: Mapping[str, float] | None = None,
    benchmark_returns: Mapping[str, float] | None = None,
    sessions_per_year: int = 252,
) -> dict[str, Any]:
    """Return session-aware metrics without annualizing a one-session sample."""

    returns = [float(item.net_pnl) for item in trades]
    session_map = dict(session_returns or {})
    session_values = list(session_map.values())
    total_return = _compound(session_values)
    if len(session_values) < 2:
        annualized = None
        annualization_status = "NOT_APPLICABLE_INSUFFICIENT_SESSIONS"
    else:
        annualized = (1.0 + total_return) ** (sessions_per_year / len(session_values)) - 1.0
        annualization_status = "DESCRIPTIVE_ONLY"
    benchmark_total = _compound(list((benchmark_returns or {}).values()))
    return {
        "trade_count": len(trades),
        "winning_trade_count": sum(1 for value in returns if value > 0),
        "losing_trade_count": sum(1 for value in returns if value < 0),
        "after_cost_expectancy": mean(returns) if returns else None,
        "profit_factor": _profit_factor(returns),
        "total_session_return": total_return,
        "annualized_return": annualized,
        "annualization_status": annualization_status,
        "session_count": len(session_values),
        "session_returns": session_map,
        "benchmark_session_count": len(benchmark_returns or {}),
        "return_vs_cash": total_return,
        "return_vs_benchmark": (
            total_return - benchmark_total if benchmark_returns else None
        ),
        "maximum_drawdown_pct": _maximum_drawdown(equity_curve),
        "uncertainty": _bootstrap_interval(returns),
    }


def compare_benchmark(
    strategy_returns: Mapping[str, float],
    benchmark_returns: Mapping[str, float],
) -> dict[str, Any]:
    """Compare only overlapping sessions and expose missing benchmark truth."""

    overlap = sorted(set(strategy_returns) & set(benchmark_returns))
    if not overlap:
        return {
            "status": "DATA_INELIGIBLE",
            "overlap_sessions": 0,
            "return_vs_benchmark": None,
        }
    strategy = _compound([strategy_returns[key] for key in overlap])
    benchmark = _compound([benchmark_returns[key] for key in overlap])
    return {
        "status": "DESCRIPTIVE_ONLY",
        "overlap_sessions": len(overlap),
        "return_vs_benchmark": strategy - benchmark,
    }


def _compound(values: Sequence[float]) -> float:
    compounded = 1.0
    for value in values:
        if value <= -1.0:
            return -1.0
        compounded *= 1.0 + value
    return compounded - 1.0


def _profit_factor(values: Sequence[float]) -> float | None:
    gains = sum(value for value in values if value > 0)
    losses = abs(sum(value for value in values if value < 0))
    if losses == 0:
        return None if gains == 0 else math.inf
    return gains / losses


def _maximum_drawdown(equity_curve: Sequence[Any]) -> float | None:
    if not equity_curve:
        return None
    peak = float(equity_curve[0].equity)
    maximum = 0.0
    for point in equity_curve:
        equity = float(point.equity)
        peak = max(peak, equity)
        if peak:
            maximum = min(maximum, equity / peak - 1.0)
    return maximum


def _bootstrap_interval(values: Sequence[float]) -> dict[str, Any]:
    if len(values) < 2:
        return {"status": "INSUFFICIENT_SAMPLE", "lower": None, "upper": None}
    rng = random.Random(0)
    samples = [
        sum(rng.choice(values) for _ in values) / len(values)
        for _ in range(1000)
    ]
    samples.sort()
    return {
        "status": "DESCRIPTIVE_ONLY",
        "lower": samples[25],
        "upper": samples[974],
        "resamples": len(samples),
        "seed": 0,
    }


__all__ = ["compare_benchmark", "compute_intraday_metrics"]
