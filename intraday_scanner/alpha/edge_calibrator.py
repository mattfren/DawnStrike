"""Empirical edge calibration with small-sample shrinkage."""

from __future__ import annotations

from statistics import median
from typing import Any

MIN_REAL_DAYS_FOR_EXPECTANCY = 20
MIN_ROWS_FOR_MODEL = 80


def shrink_empirical_mean(
    *,
    bucket_mean: float,
    bucket_count: int,
    global_mean: float,
    prior_weight: int = 20,
) -> float:
    total_weight = max(0, bucket_count) + max(1, prior_weight)
    numerator = (bucket_mean * max(0, bucket_count)) + (
        global_mean * max(1, prior_weight)
    )
    return numerator / total_weight


def calibrate_edge(
    *,
    bucket_rows: list[dict[str, Any]],
    global_rows: list[dict[str, Any]],
    real_shadow_days: int,
    prior_weight: int = 20,
) -> dict[str, Any]:
    if real_shadow_days < MIN_REAL_DAYS_FOR_EXPECTANCY:
        return {
            "mode": "insufficient_sample",
            "expected_return_bucket": "INSUFFICIENT_SAMPLE",
            "drawdown_risk_bucket": "INSUFFICIENT_SAMPLE",
            "hit_rate_bucket": "INSUFFICIENT_SAMPLE",
            "confidence_bucket": "INSUFFICIENT_SAMPLE",
            "sample_size": len(bucket_rows),
            "real_shadow_days": real_shadow_days,
            "insufficient_sample": True,
        }

    raw_global_returns = [_return(row) for row in global_rows]
    raw_bucket_returns = [_return(row) for row in bucket_rows]
    global_returns = [value for value in raw_global_returns if value is not None]
    bucket_returns = [value for value in raw_bucket_returns if value is not None]
    if not global_returns:
        global_returns = [0.0]
    if not bucket_returns:
        bucket_returns = [0.0]
    bucket_mean = sum(bucket_returns) / len(bucket_returns)
    global_mean = sum(global_returns) / len(global_returns)
    shrunk = shrink_empirical_mean(
        bucket_mean=bucket_mean,
        bucket_count=len(bucket_returns),
        global_mean=global_mean,
        prior_weight=prior_weight,
    )
    raw_drawdowns = [_drawdown(row) for row in bucket_rows]
    drawdowns = [value for value in raw_drawdowns if value is not None]
    hit_rate = _hit_rate(bucket_returns)
    outlier_dependency = _outlier_dependency(bucket_returns)
    return {
        "mode": "empirical_shrinkage",
        "expected_return_pct": round(shrunk, 4),
        "expected_return_bucket": _edge_bucket(shrunk),
        "drawdown_risk_bucket": _drawdown_bucket(drawdowns),
        "hit_rate_pct": round(hit_rate, 2),
        "hit_rate_bucket": _hit_bucket(hit_rate),
        "outlier_dependency": round(outlier_dependency, 4),
        "confidence_bucket": _confidence_bucket(len(bucket_returns), outlier_dependency),
        "sample_size": len(bucket_returns),
        "real_shadow_days": real_shadow_days,
        "insufficient_sample": False,
    }


def outlier_warning(rows: list[dict[str, Any]]) -> dict[str, Any]:
    raw_returns = [_return(row) for row in rows]
    returns = [value for value in raw_returns if value is not None]
    dependency = _outlier_dependency(returns)
    return {
        "outlier_dependency": round(dependency, 4),
        "outlier_dependent": dependency >= 0.50 and len(returns) >= 3,
    }


def score_decile(value: Any) -> int:
    try:
        score = max(0.0, min(100.0, float(value)))
    except (TypeError, ValueError):
        score = 0.0
    return min(10, int(score // 10) + 1)


def _return(row: dict[str, Any]) -> float | None:
    for key in (
        "high_after_entry_return",
        "high_after_entry_return_pct",
        "return_pct",
        "close_return_pct",
    ):
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _drawdown(row: dict[str, Any]) -> float | None:
    for key in ("low_after_entry_drawdown", "max_adverse_excursion", "drawdown_pct"):
        value = row.get(key)
        if value is None or value == "":
            continue
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    return None


def _hit_rate(values: list[float]) -> float:
    if not values:
        return 0.0
    return (sum(1 for value in values if value > 0) / len(values)) * 100.0


def _outlier_dependency(values: list[float]) -> float:
    if len(values) < 3:
        return 1.0 if values and max(values) > 0 else 0.0
    positives = [max(0.0, value) for value in values]
    total = sum(positives)
    if total <= 0:
        return 0.0
    return max(positives) / total


def _edge_bucket(value: float) -> str:
    if value >= 8:
        return "HIGH"
    if value >= 3:
        return "MEDIUM"
    if value > 0:
        return "LOW"
    return "NEGATIVE"


def _drawdown_bucket(values: list[float]) -> str:
    if not values:
        return "UNKNOWN"
    worst = min(values)
    if worst <= -15:
        return "HIGH"
    if worst <= -7:
        return "MEDIUM"
    return "LOW"


def _hit_bucket(value: float) -> str:
    if value >= 60:
        return "HIGH"
    if value >= 45:
        return "MEDIUM"
    if value > 0:
        return "LOW"
    return "UNKNOWN"


def _confidence_bucket(sample_size: int, dependency: float) -> str:
    if sample_size < 20:
        return "LOW_SAMPLE"
    if dependency >= 0.50:
        return "OUTLIER_DEPENDENT"
    if sample_size >= 50:
        return "HIGH"
    return "MEDIUM"


def median_return(rows: list[dict[str, Any]]) -> float:
    raw_values = [_return(row) for row in rows]
    values = [value for value in raw_values if value is not None]
    return round(float(median(values)), 4) if values else 0.0
