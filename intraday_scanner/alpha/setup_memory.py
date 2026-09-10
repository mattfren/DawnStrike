"""Setup memory aggregation for AlphaOps."""

from __future__ import annotations

from statistics import median
from typing import Any

from intraday_scanner.alpha.outcome_semantics import account_drawdown, realized_return


def build_setup_memory(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get("setup_key") or row.get("setup_grade") or "unknown")
        grouped.setdefault(key, []).append(row)
    return {key: summarize_setup(key, items) for key, items in grouped.items()}


def summarize_setup(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    raw_returns = [realized_return(row) for row in rows]
    returns = [value for value in raw_returns if value is not None]
    wins = [value for value in returns if value > 0]
    return {
        "setup_key": key,
        "sample_size": len(rows),
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else None,
        "median_return_pct": round(float(median(returns)), 4) if returns else None,
        "win_rate_pct": round((len(wins) / len(returns)) * 100.0, 2) if returns else None,
        "max_drawdown_pct": min(
            [value for value in (account_drawdown(row) for row in rows) if value is not None],
            default=None,
        ),
        "outlier_dependency": _outlier_dependency(returns),
    }


def _outlier_dependency(values: list[float]) -> float:
    positives = [max(0.0, value) for value in values]
    total = sum(positives)
    if total <= 0:
        return 0.0
    return round(max(positives) / total, 4)
