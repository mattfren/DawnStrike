"""Calibration and interval checks for deterministic V6 research outputs."""

from __future__ import annotations

from typing import Any

MIN_CALIBRATION_EFFECTIVE_SAMPLES = 30
MIN_CALIBRATION_SESSIONS = 5
MIN_INTERVAL_EFFECTIVE_SAMPLES = 30
MIN_INTERVAL_SESSIONS = 5


def calibration_report(rows: list[dict[str, Any]], *, bins: int = 10) -> dict[str, Any]:
    """Calculate Brier score and ECE only for explicit activation labels."""

    if bins < 1:
        raise ValueError("bins must be positive")

    pairs = [
        (float(row["activation_probability"]), float(row["activation_label"]))
        for row in rows
        if _finite_probability(row.get("activation_probability"))
        and row.get("activation_label") in {0, 1}
    ]
    if not pairs:
        return _empty("NO_CALIBRATION_LABELS")
    candidate_rows = [row for row in rows if row.get("activation_probability") is not None]
    invalid_count = sum(
        1
        for row in candidate_rows
        if not _finite_probability(row.get("activation_probability"))
        or row.get("activation_label") not in {0, 1}
    )
    effective_samples = _effective_sample_size(
        [row for row in candidate_rows if _finite_probability(row.get("activation_probability"))]
    )
    session_count = _session_count(
        [row for row in rows if _finite_probability(row.get("activation_probability"))]
    )
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
        "effective_sample_size": round(effective_samples, 6),
        "session_count": session_count,
        "invalid_row_count": invalid_count,
        "brier_score": round(brier, 6),
        "expected_calibration_error": round(ece, 6),
        "bins": detail,
        "research_only": True,
        "display_eligible": bool(
            invalid_count == 0
            and effective_samples >= MIN_CALIBRATION_EFFECTIVE_SAMPLES
            and session_count >= MIN_CALIBRATION_SESSIONS
        ),
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
        and float(row["interval_lower_pct"]) <= float(row["interval_upper_pct"])
    ]
    if not pairs:
        return {
            "status": "NO_INTERVAL_LABELS",
            "sample_size": 0,
            "effective_sample_size": 0,
            "session_count": 0,
            "invalid_row_count": sum(1 for row in rows if _has_interval_fields(row)),
            "coverage_pct": None,
            "display_eligible": False,
        }
    candidate_rows = [row for row in rows if _has_interval_fields(row)]
    invalid_count = sum(
        1
        for row in candidate_rows
        if not (
            _finite(row.get("interval_lower_pct"))
            and _finite(row.get("interval_upper_pct"))
            and _finite(row.get("realized_return_pct"))
            and float(row["interval_lower_pct"]) <= float(row["interval_upper_pct"])
        )
    )
    effective_samples = _effective_sample_size(
        [row for row in candidate_rows if _valid_interval_row(row)]
    )
    session_count = _session_count(
        [row for row in candidate_rows if _valid_interval_row(row)]
    )
    covered = sum(1 for lower, upper, actual in pairs if lower <= actual <= upper)
    return {
        "status": "EVALUABLE",
        "sample_size": len(pairs),
        "effective_sample_size": round(effective_samples, 6),
        "session_count": session_count,
        "invalid_row_count": invalid_count,
        "coverage_pct": round(100.0 * covered / len(pairs), 6),
        "research_only": True,
        "display_eligible": bool(
            invalid_count == 0
            and effective_samples >= MIN_INTERVAL_EFFECTIVE_SAMPLES
            and session_count >= MIN_INTERVAL_SESSIONS
        ),
    }


def _empty(status: str) -> dict[str, Any]:
    return {
        "status": status,
        "sample_size": 0,
        "effective_sample_size": 0,
        "session_count": 0,
        "invalid_row_count": 0,
        "brier_score": None,
        "expected_calibration_error": None,
        "bins": [],
        "research_only": True,
        "display_eligible": False,
    }


def _finite(value: object) -> bool:
    try:
        parsed = float(str(value))
        return parsed == parsed and abs(parsed) != float("inf")
    except (TypeError, ValueError):
        return False


def _finite_probability(value: object) -> bool:
    return _finite(value) and 0.0 <= float(str(value)) <= 1.0


def _has_interval_fields(row: dict[str, Any]) -> bool:
    return any(
        row.get(key) is not None
        for key in ("interval_lower_pct", "interval_upper_pct", "realized_return_pct")
    )


def _valid_interval_row(row: dict[str, Any]) -> bool:
    return bool(
        _finite(row.get("interval_lower_pct"))
        and _finite(row.get("interval_upper_pct"))
        and _finite(row.get("realized_return_pct"))
        and float(row["interval_lower_pct"]) <= float(row["interval_upper_pct"])
    )


def _effective_sample_size(rows: list[dict[str, Any]]) -> float:
    weights: list[float] = []
    for row in rows:
        try:
            weight = float(str(row.get("inverse_probability_weight", 1.0)))
        except (TypeError, ValueError):
            weight = 1.0
        if weight > 0 and weight == weight and abs(weight) != float("inf"):
            weights.append(weight)
        else:
            weights.append(1.0)
    total = sum(weights)
    return total * total / sum(weight * weight for weight in weights) if total else 0.0


def _session_count(rows: list[dict[str, Any]]) -> int:
    dates = {str(row.get("market_date") or "")[:10] for row in rows}
    return len({date for date in dates if date}) or (1 if rows else 0)


__all__ = [
    "calibration_report",
    "interval_coverage",
    "MIN_CALIBRATION_EFFECTIVE_SAMPLES",
    "MIN_CALIBRATION_SESSIONS",
    "MIN_INTERVAL_EFFECTIVE_SAMPLES",
    "MIN_INTERVAL_SESSIONS",
]
