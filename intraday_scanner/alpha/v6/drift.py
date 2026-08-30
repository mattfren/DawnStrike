"""Frozen, fail-closed V6 drift evidence.

Drift is an evidence receipt, not a live heuristic. Both cohorts and their
market-date windows are supplied by the caller and included in receipt
identity. This module never chooses a midpoint or turns absent metrics into
numeric zero.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from collections.abc import Mapping, Sequence
from datetime import date
from typing import Any

from intraday_scanner.alpha.v6.contracts import (
    canonical_hash,
    is_valid_code_sha,
    is_valid_sha256,
    utc_now,
)

DRIFT_DIMENSIONS = (
    "source",
    "missingness",
    "feature",
    "score",
    "calibration",
    "liquidity",
    "cost",
    "outcome",
    "setup",
    "regime",
)
_WARNING_THRESHOLD = 0.10
_QUARANTINE_THRESHOLD = 0.25
_ISO_DATE = re.compile(r"^[0-9]{4}-[0-9]{2}-[0-9]{2}$")
_DIMENSION_FIELDS: dict[str, tuple[str, ...]] = {
    "source": ("source_key", "source", "source_id", "provider", "source_lineage_hash_sha256"),
    "feature": ("feature_key", "feature_schema_version", "feature_hash_sha256", "features"),
    "score": ("score", "model_score", "directional_score", "score_bucket"),
    "calibration": ("calibration_status", "calibration_bucket", "calibration", "confidence"),
    "liquidity": ("liquidity_bucket", "liquidity_status", "liquidity", "adv", "volume"),
    "cost": ("cost_model_version", "cost_status", "cost_bps", "estimated_cost_bps", "cost"),
    "outcome": ("outcome_status", "outcome_label", "outcome", "learning_eligible"),
    "setup": ("setup_key", "setup", "setup_type"),
    "regime": ("regime_key", "regime", "regime_type"),
}


def build_drift_report(
    *,
    baseline_rows: list[dict[str, Any]],
    current_rows: list[dict[str, Any]],
    reference_window: Mapping[str, Any] | Sequence[str] | None = None,
    recent_window: Mapping[str, Any] | Sequence[str] | None = None,
    config: Mapping[str, Any] | None = None,
    source: Mapping[str, Any] | str | None = None,
    config_hash_sha256: str | None = None,
    source_hash_sha256: str | None = None,
    window_hash_sha256: str | None = None,
    input_hash_sha256: str | None = None,
    code_sha: str | None = None,
    minimum_observations: int = 20,
    minimum_market_sessions: int = 5,
) -> dict[str, Any]:
    """Build a deterministic receipt from two explicitly frozen cohorts.

    Windows accept a date sequence or ``{"start", "end", "market_dates"}``.
    Missing dimensions and lineage remain unknown and force quarantine.
    """
    if (
        isinstance(minimum_observations, bool)
        or isinstance(minimum_market_sessions, bool)
        or not isinstance(minimum_observations, int)
        or not isinstance(minimum_market_sessions, int)
        or minimum_observations < 1
        or minimum_market_sessions < 1
    ):
        raise ValueError("drift evidence thresholds must be positive")
    # A scheduled monitor must remain operational before the first governed
    # frozen windows exist.  Return a signed, deterministic quarantine receipt
    # rather than throwing or inferring a window from observed rows.
    if reference_window is None or recent_window is None:
        return _not_evaluable_missing_windows(
            baseline_rows=baseline_rows,
            current_rows=current_rows,
            config=config,
            source=source,
            config_hash_sha256=config_hash_sha256,
            source_hash_sha256=source_hash_sha256,
            window_hash_sha256=window_hash_sha256,
            input_hash_sha256=input_hash_sha256,
            code_sha=code_sha,
            minimum_observations=minimum_observations,
            minimum_market_sessions=minimum_market_sessions,
        )
    for name, value in {
        "config_hash_sha256": config_hash_sha256,
        "source_hash_sha256": source_hash_sha256,
        "window_hash_sha256": window_hash_sha256,
        "input_hash_sha256": input_hash_sha256,
    }.items():
        if not is_valid_sha256(value):
            raise ValueError(f"{name} is required and must be a SHA-256 hash")
    if not is_valid_code_sha(code_sha):
        raise ValueError("code_sha is required and must be an exact code SHA")
    baseline = _canonical_rows(baseline_rows)
    current = _canonical_rows(current_rows)
    if not baseline or not current:
        raise ValueError("drift cohorts must be non-empty")
    _validate_identity_rows(baseline, "reference")
    _validate_identity_rows(current, "recent")
    baseline_ids = {_row_identity(row) for row in baseline}
    current_ids = {_row_identity(row) for row in current}
    if baseline_ids & current_ids:
        raise ValueError("reference and recent drift cohorts share an observation identity")
    reference = _normalize_window(reference_window, baseline)
    recent = _normalize_window(recent_window, current)
    if reference["end"] >= recent["start"]:
        raise ValueError("reference and recent drift windows must be disjoint and ordered")
    _validate_window_rows(baseline, reference, "reference")
    _validate_window_rows(current, recent, "recent")
    windows = {"reference": reference, "recent": recent}
    input_hash = _exact_hash(
        input_hash_sha256,
        canonical_hash({"reference": baseline, "recent": current}),
        "input",
    )
    config_hash = _exact_hash(
        config_hash_sha256,
        canonical_hash(config) if config is not None else None,
        "config",
    )
    source_hash = _exact_hash(
        source_hash_sha256,
        canonical_hash(source) if source is not None else None,
        "source",
    )
    window_hash = _exact_hash(window_hash_sha256, canonical_hash(windows), "window")
    if code_sha is not None and not is_valid_code_sha(code_sha):
        raise ValueError("code_sha is malformed")
    reference_dates = _dates(reference, baseline)
    recent_dates = _dates(recent, current)
    coverage = {
        "reference": _coverage(baseline, reference_dates),
        "recent": _coverage(current, recent_dates),
    }
    dimensions: dict[str, dict[str, Any]] = {}
    unknown_dimensions: list[str] = []
    for dimension in DRIFT_DIMENSIONS:
        result = _dimension_report(dimension, baseline, current)
        dimensions[dimension] = result
        if result["status"] == "UNKNOWN":
            unknown_dimensions.append(dimension)
    high = any(row["status"] == "QUARANTINE" for row in dimensions.values())
    warning = any(row["status"] == "WARNING" for row in dimensions.values())
    insufficient = any(
        row["observations"] < minimum_observations
        or row["market_sessions"] < minimum_market_sessions
        for row in coverage.values()
    )
    if insufficient:
        status = "QUARANTINE_INSUFFICIENT_DRIFT_EVIDENCE"
    elif high:
        status = "QUARANTINE_DRIFT"
    elif unknown_dimensions:
        status = "QUARANTINE_UNKNOWN_DRIFT_DIMENSION"
    elif warning:
        status = "WARNING_DRIFT"
    else:
        status = "STABLE"
    identity = {
        "status": status,
        "minimum_observations": minimum_observations,
        "minimum_market_sessions": minimum_market_sessions,
        "coverage": coverage,
        "windows": windows,
        "dimensions": dimensions,
        "config_hash_sha256": config_hash,
        "source_hash_sha256": source_hash,
        "code_sha": code_sha,
        "window_hash_sha256": window_hash,
        "input_hash_sha256": input_hash,
        "auto_quarantine": status.startswith("QUARANTINE"),
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
    }
    identity_hash = canonical_hash(identity)
    payload = {**identity, "created_at": utc_now(), "drift_report_id": "v6dr-" + identity_hash[:28]}
    payload["receipt_hash_sha256"] = canonical_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"receipt_hash_sha256", "created_at"}
        }
    )
    return payload


def _canonical_rows(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(row) for row in sorted(rows, key=lambda item: canonical_hash(dict(item)))]


def _row_identity(row: Mapping[str, Any]) -> str:
    for field in ("observation_id", "decision_id", "signal_id", "id"):
        value = str(row.get(field) or "").strip()
        if value:
            return f"{field}:{value}"
    raise ValueError("drift row requires a unique observation_id or decision_id")


def _validate_identity_rows(rows: Sequence[Mapping[str, Any]], label: str) -> None:
    identities = [_row_identity(row) for row in rows]
    if len(set(identities)) != len(identities):
        raise ValueError(f"{label} drift cohort contains duplicate observation identities")
    for row in rows:
        value = str(row.get("market_date") or "")
        if not _ISO_DATE.fullmatch(value):
            raise ValueError(f"{label} drift row market_date must be YYYY-MM-DD")
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{label} drift row market_date is invalid") from exc
        if parsed.isoformat() != value:
            raise ValueError(f"{label} drift row market_date must be canonical")


def _exact_hash(value: str | None, derived: str | None, label: str) -> str | None:
    if value is None:
        return derived
    if not is_valid_sha256(value):
        raise ValueError(f"{label}_hash_sha256 is malformed")
    if derived is not None and value != derived:
        raise ValueError(f"{label}_hash_sha256 does not match frozen evidence")
    return value


def _normalize_window(
    window: Mapping[str, Any] | Sequence[str] | None, rows: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    del rows
    if window is None:
        raise ValueError("frozen drift window is required")
    if isinstance(window, Mapping):
        values = window.get("market_dates") or window.get("dates") or []
        dates = sorted({str(item) for item in values if str(item).strip()})
        start = str(window.get("start") or "")
        end = str(window.get("end") or "")
    else:
        dates = sorted({str(item) for item in window if str(item).strip()})
        start, end = (dates[0], dates[-1]) if dates else ("", "")
    if not dates or not start or not end:
        raise ValueError("frozen drift windows require start, end, and market_dates")
    if not _ISO_DATE.fullmatch(start) or not _ISO_DATE.fullmatch(end):
        raise ValueError("frozen drift window dates must be YYYY-MM-DD")
    if any(not _ISO_DATE.fullmatch(item) for item in dates):
        raise ValueError("frozen drift window dates must be YYYY-MM-DD")
    start = _canonical_date(start, "frozen drift window start")
    end = _canonical_date(end, "frozen drift window end")
    dates = [_canonical_date(item, "frozen drift window market date") for item in dates]
    if start > end or dates[0] < start or dates[-1] > end:
        raise ValueError("drift window is reversed")
    if len(set(dates)) != len(dates):
        raise ValueError("frozen drift window market dates must be unique")
    return {"market_dates": dates, "start": start, "end": end}


def _canonical_date(value: object, label: str) -> str:
    raw = str(value)
    if not _ISO_DATE.fullmatch(raw):
        raise ValueError(f"{label} must be YYYY-MM-DD")
    try:
        parsed = date.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    if parsed.isoformat() != raw:
        raise ValueError(f"{label} must equal its canonical ISO date")
    return raw


def _not_evaluable_missing_windows(
    *,
    baseline_rows: Sequence[Mapping[str, Any]],
    current_rows: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any] | None,
    source: Mapping[str, Any] | str | None,
    config_hash_sha256: str | None,
    source_hash_sha256: str | None,
    window_hash_sha256: str | None,
    input_hash_sha256: str | None,
    code_sha: str | None,
    minimum_observations: int,
    minimum_market_sessions: int,
) -> dict[str, Any]:
    baseline = sorted(
        (dict(row) for row in baseline_rows if isinstance(row, Mapping)),
        key=canonical_hash,
    )
    current = sorted(
        (dict(row) for row in current_rows if isinstance(row, Mapping)),
        key=canonical_hash,
    )
    config_hash = _exact_hash(
        config_hash_sha256, canonical_hash(config) if config is not None else None, "config"
    )
    source_hash = _exact_hash(
        source_hash_sha256, canonical_hash(source) if source is not None else None, "source"
    )
    input_hash = _exact_hash(
        input_hash_sha256,
        canonical_hash({"reference": baseline, "recent": current}),
        "input",
    )
    if window_hash_sha256 is not None and not is_valid_sha256(window_hash_sha256):
        raise ValueError("window_hash_sha256 is malformed")
    if code_sha is not None and not is_valid_code_sha(code_sha):
        raise ValueError("code_sha is malformed")
    identity = {
        "status": "NOT_EVALUABLE_FROZEN_WINDOWS_REQUIRED",
        "minimum_observations": minimum_observations,
        "minimum_market_sessions": minimum_market_sessions,
        "coverage": {
            "reference": {
                "observations": len(baseline),
                "market_sessions": None,
                "market_dates": None,
            },
            "recent": {
                "observations": len(current),
                "market_sessions": None,
                "market_dates": None,
            },
        },
        "windows": {"reference": None, "recent": None},
        "dimensions": {
            dimension: {"status": "UNKNOWN", "score": None, "observations": None}
            for dimension in DRIFT_DIMENSIONS
        },
        "config_hash_sha256": config_hash,
        "source_hash_sha256": source_hash,
        "code_sha": code_sha,
        "window_hash_sha256": window_hash_sha256,
        "input_hash_sha256": input_hash,
        "auto_quarantine": True,
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
    }
    identity_hash = canonical_hash(identity)
    payload = {
        **identity,
        "created_at": utc_now(),
        "drift_report_id": "v6dr-" + identity_hash[:28],
    }
    payload["receipt_hash_sha256"] = canonical_hash(
        {
            key: value
            for key, value in payload.items()
            if key not in {"receipt_hash_sha256", "created_at"}
        }
    )
    return payload


def _dates(window: Mapping[str, Any], rows: Sequence[Mapping[str, Any]]) -> set[str]:
    del rows
    return {str(value) for value in window.get("market_dates", [])}


def _validate_window_rows(
    rows: Sequence[Mapping[str, Any]], window: Mapping[str, Any], label: str
) -> None:
    start, end = window.get("start"), window.get("end")
    declared = {str(value) for value in window.get("market_dates", [])}
    for row in rows:
        market_date = str(row.get("market_date") or "")
        if declared and market_date not in declared:
            raise ValueError(f"{label} drift row is outside its frozen market-date window")
        if start and market_date < str(start) or end and market_date > str(end):
            raise ValueError(f"{label} drift row is outside its frozen market-date window")
    observed = {str(row.get("market_date")) for row in rows}
    if observed != declared:
        raise ValueError(
            f"{label} drift cohort does not exactly cover its frozen market-date window"
        )


def _coverage(rows: Sequence[Mapping[str, Any]], dates: set[str]) -> dict[str, Any]:
    observed = {str(row.get("market_date") or "") for row in rows if row.get("market_date")}
    valid_dates = observed & dates
    return {
        "observations": len(rows),
        "market_sessions": len(valid_dates),
        "market_dates": sorted(valid_dates),
    }


def _dimension_report(
    dimension: str, baseline: Sequence[Mapping[str, Any]], current: Sequence[Mapping[str, Any]]
) -> dict[str, Any]:
    before = [_dimension_value(row, dimension) for row in baseline]
    after = [_dimension_value(row, dimension) for row in current]
    missing_before = sum(value is None for value in before)
    missing_after = sum(value is None for value in after)
    if not before or not after or (missing_before == len(before) and missing_after == len(after)):
        return {
            "status": "UNKNOWN",
            "score": None,
            "observations": len(before) + len(after),
            "missingness": {"reference": missing_before, "recent": missing_after},
        }
    reference = [value for value in before if value is not None]
    recent = [value for value in after if value is not None]
    score = round(
        max(
            _shift_score(reference, recent),
            abs(missing_before / len(before) - missing_after / len(after)),
        ),
        6,
    )
    state = (
        "QUARANTINE"
        if score >= _QUARANTINE_THRESHOLD
        else "WARNING"
        if score >= _WARNING_THRESHOLD
        else "STABLE"
    )
    return {
        "status": state,
        "score": score,
        "observations": len(before) + len(after),
        "missingness": {"reference": missing_before, "recent": missing_after},
        "reference_distribution": _distribution(reference),
        "recent_distribution": _distribution(recent),
    }


def _dimension_value(row: Mapping[str, Any], dimension: str) -> Any:
    if dimension == "missingness":
        missing = [
            name
            for name in DRIFT_DIMENSIONS
            if name not in {"missingness"} and _dimension_value(row, name) is None
        ]
        return ",".join(missing) if missing else "complete"
    nested = row.get("drift_dimensions") or row.get("dimensions")
    if isinstance(nested, Mapping) and dimension in nested:
        value = nested[dimension]
        return None if value is None or value == "" else value
    for field in _DIMENSION_FIELDS.get(dimension, ()):
        if field in row and row[field] is not None and row[field] != "":
            value = row[field]
            if isinstance(value, bool):
                return None
            if isinstance(value, (int, float)) and not math.isfinite(float(value)):
                return None
            return canonical_hash(value) if isinstance(value, Mapping) else value
    return None


def _distribution(values: Sequence[Any]) -> dict[str, float]:
    if not values:
        return {}
    counts = Counter(str(value) for value in values)
    total = len(values)
    return {key: round(count / total, 6) for key, count in sorted(counts.items())}


def _shift_score(reference: Sequence[Any], recent: Sequence[Any]) -> float:
    if not reference or not recent:
        return 1.0
    if all(
        isinstance(value, (int, float)) and not isinstance(value, bool)
        for value in (*reference, *recent)
    ):
        left = sum(float(value) for value in reference) / len(reference)
        right = sum(float(value) for value in recent) / len(recent)
        left_variance = sum((float(value) - left) ** 2 for value in reference) / len(reference)
        right_variance = sum((float(value) - right) ** 2 for value in recent) / len(recent)
        mean_shift = abs(left - right) / max(abs(left), abs(right), 1.0)
        variance_shift = abs(left_variance - right_variance) / max(
            left_variance, right_variance, 1.0
        )
        return min(1.0, max(mean_shift, variance_shift))
    left_distribution, right_distribution = _distribution(reference), _distribution(recent)
    return (
        sum(
            abs(
                left_distribution.get(key, 0.0)
                - right_distribution.get(key, 0.0)
            )
            for key in set(left_distribution) | set(right_distribution)
        )
        / 2.0
    )


__all__ = ["DRIFT_DIMENSIONS", "build_drift_report"]
