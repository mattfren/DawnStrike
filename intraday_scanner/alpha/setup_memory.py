"""Setup memory aggregation for AlphaOps."""

from __future__ import annotations

from statistics import median
from typing import Any


def build_setup_memory(rows: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        key = str(row.get("setup_key") or row.get("setup_grade") or "unknown")
        grouped.setdefault(key, []).append(row)
    return {key: summarize_setup(key, items) for key, items in grouped.items()}


def summarize_setup(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    raw_returns = [
        _float(row.get("high_after_entry_return") or row.get("return_pct")) for row in rows
    ]
    returns = [value for value in raw_returns if value is not None]
    wins = [value for value in returns if value > 0]
    return {
        "setup_key": key,
        "sample_size": len(rows),
        "avg_return_pct": round(sum(returns) / len(returns), 4) if returns else 0.0,
        "median_return_pct": round(float(median(returns)), 4) if returns else 0.0,
        "win_rate_pct": round((len(wins) / len(returns)) * 100.0, 2) if returns else 0.0,
        "max_drawdown_pct": min(
            [_float(row.get("low_after_entry_drawdown"), 0.0) or 0.0 for row in rows],
            default=0.0,
        ),
        "outlier_dependency": _outlier_dependency(returns),
    }


def _outlier_dependency(values: list[float]) -> float:
    positives = [max(0.0, value) for value in values]
    total = sum(positives)
    if total <= 0:
        return 0.0
    return round(max(positives) / total, 4)


def _float(value: Any, default: float | None = None) -> float | None:
    if value in {None, ""}:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default
