from __future__ import annotations

import pytest

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.drift import build_drift_report
from intraday_scanner.alpha.v6.registry import (
    record_untouched_holdout_evaluation,
    register_experiment,
)


def _window(start: str, end: str, dates: list[str]) -> dict[str, object]:
    return {"start": start, "end": end, "market_dates": dates}


def _receipt_kwargs(reference: dict[str, object], recent: dict[str, object]):
    config = {"threshold": 0.2, "dimensions": ["feature", "score"]}
    source = {"provider": "fixture", "snapshot": "s1"}
    rows = {
        "reference": [
            {"observation_id": "r1", "market_date": "2026-01-01", "feature_key": 1.0},
            {"observation_id": "r2", "market_date": "2026-01-02", "feature_key": 3.0},
        ],
        "recent": [
            {"observation_id": "n1", "market_date": "2026-01-03", "feature_key": 0.0},
            {"observation_id": "n2", "market_date": "2026-01-04", "feature_key": 4.0},
        ],
    }
    return {
        "baseline_rows": rows["reference"],
        "current_rows": rows["recent"],
        "reference_window": reference,
        "recent_window": recent,
        "config": config,
        "source": source,
        "config_hash_sha256": canonical_hash(config),
        "source_hash_sha256": canonical_hash(source),
        "window_hash_sha256": canonical_hash(
            {"reference": reference, "recent": recent}
        ),
        "input_hash_sha256": canonical_hash(
            {
                "reference": sorted(rows["reference"], key=canonical_hash),
                "recent": sorted(rows["recent"], key=canonical_hash),
            }
        ),
        "code_sha": "a" * 40,
        "minimum_observations": 1,
        "minimum_market_sessions": 1,
    }


def test_drift_requires_frozen_windows_lineage_and_exact_dates() -> None:
    missing = build_drift_report(baseline_rows=[], current_rows=[])
    assert missing["status"] == "NOT_EVALUABLE_FROZEN_WINDOWS_REQUIRED"
    assert missing["auto_quarantine"] is True
    assert missing["receipt_hash_sha256"]
    kwargs = _receipt_kwargs(
        _window("2026-01-01", "2026-01-02", ["2026-01-01", "2026-01-02"]),
        _window("2026-01-03", "2026-01-04", ["2026-01-03", "2026-01-04"]),
    )
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        kwargs["reference_window"] = _window(
            "2026-01-01", "2026-01-02", ["2026-01-01-junk", "2026-01-02"]
        )
        build_drift_report(**kwargs)
    with pytest.raises(ValueError, match="unique observation"):
        kwargs = _receipt_kwargs(
            _window("2026-01-01", "2026-01-02", ["2026-01-01", "2026-01-02"]),
            _window("2026-01-03", "2026-01-04", ["2026-01-03", "2026-01-04"]),
        )
        kwargs["baseline_rows"][0].pop("observation_id")
        build_drift_report(**kwargs)


def test_drift_rejects_overlapping_windows_and_detects_same_mean_distribution_shift() -> None:
    reference = _window("2026-01-01", "2026-01-02", ["2026-01-01", "2026-01-02"])
    recent = _window("2026-01-02", "2026-01-03", ["2026-01-02", "2026-01-03"])
    kwargs = _receipt_kwargs(reference, recent)
    with pytest.raises(ValueError, match="disjoint"):
        build_drift_report(**kwargs)
    kwargs = _receipt_kwargs(
        _window("2026-01-01", "2026-01-02", ["2026-01-01", "2026-01-02"]),
        _window("2026-01-03", "2026-01-04", ["2026-01-03", "2026-01-04"]),
    )
    report = build_drift_report(**kwargs)
    assert report["dimensions"]["feature"]["status"] == "QUARANTINE"
    assert report["receipt_hash_sha256"]


def test_legacy_experiment_is_explicitly_not_evaluable() -> None:
    experiment = register_experiment(
        hypothesis="Legacy lineage remains outside governed evaluation.",
        training_cutoff="2026-01-02",
        baseline_config={"threshold": 1},
        candidate_config={"threshold": 2},
        validation_start="2026-01-03",
        holdout_start="2026-01-04",
        stop_condition="Stop on missing truth.",
        promotion_requirements=["manual operator decision"],
    )
    assert experiment["status"] == "REGISTERED_NOT_EVALUABLE_MISSING_LINEAGE"
    with pytest.raises(ValueError, match="not evaluable"):
        record_untouched_holdout_evaluation(
            experiment=experiment,
            evidence={"no_lookahead": True},
            existing_evaluations=[],
        )
