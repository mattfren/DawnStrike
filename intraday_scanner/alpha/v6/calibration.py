"""Calibration and interval checks for deterministic V6 research outputs."""

from __future__ import annotations

from typing import Any


def calibration_report(rows: list[dict[str, Any]], *, bins: int = 10) -> dict[str, Any]:
    """Calculate Brier score and ECE only for explicit activation labels."""

    pairs = [
        (float(row["activation_probability"]), float(row["activation_label"]))
        for row in rows
        if _finite_probability(row.get("activation_probability"))
        and row.get("activation_label") in {0, 1}
    ]
    if not pairs:
        return _empty("NO_CALIBRATION_LABELS")
    brier = sum((prediction - actual) ** 2 for prediction, actual in pairs) / len(pairs)
    buckets: list[list[tuple[float, float]]] = [[] for _ in range(bins)]
    for prediction, actual in pairs:
        buckets[min(bins - 1, int(prediction * bins))].append((prediction, actual))
    ece = 0.0
    detail = []
    for index, bucket in enumerate(buckets):
        if not bucket:
            continue
        predicted = sum(pair[0] for pair in bucket) / len(bucket)
        observed = sum(pair[1] for pair in bucket) / len(bucket)
        ece += abs(predicted - observed) * len(bucket) / len(pairs)
        detail.append(
            {
                "bin": index,
                "count": len(bucket),
                "mean_prediction": round(predicted, 6),
                "observed_rate": round(observed, 6),
            }
        )
    return {
        "status": "EVALUABLE",
        "sample_size": len(pairs),
        "brier_score": round(brier, 6),
        "expected_calibration_error": round(ece, 6),
        "bins": detail,
        "research_only": True,
    }


def interval_coverage(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [
        (
            float(row["interval_lower_pct"]),
            float(row["interval_upper_pct"]),
            float(row["realized_return_pct"]),
        )
        for row in rows
        if _finite(row.get("interval_lower_pct"))
        and _finite(row.get("interval_upper_pct"))
        and _finite(row.get("realized_return_pct"))
    ]
    if not pairs:
        return {"status": "NO_INTERVAL_LABELS", "sample_size": 0, "coverage_pct": None}
    covered = sum(1 for lower, upper, actual in pairs if lower <= actual <= upper)
    return {
        "status": "EVALUABLE",
        "sample_size": len(pairs),
        "coverage_pct": round(100.0 * covered / len(pairs), 6),
        "research_only": True,
    }


def _empty(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "sample_size": 0,
        "brier_score": None,
        "expected_calibration_error": None,
        "bins": [],
        "research_only": True,
    }


def _finite(value: object) -> bool:
    try:
        parsed = float(str(value))
        return parsed == parsed and abs(parsed) != float("inf")
    except (TypeError, ValueError):
        return False


def _finite_probability(value: object) -> bool:
    return _finite(value) and 0.0 <= float(str(value)) <= 1.0


__all__ = ["calibration_report", "interval_coverage"]
