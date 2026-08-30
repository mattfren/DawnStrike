from __future__ import annotations

import math

import pytest

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.drift import build_drift_report
from intraday_scanner.alpha.v6.registry import (
    record_untouched_holdout_evaluation,
    register_experiment,
)


def _experiment() -> dict[str, object]:
    training = ["2026-08-01"]
    validation = ["2026-08-02"]
    holdout = ["2026-09-01", "2026-09-02"]
    windows = {
        "training": {
            "start": "2026-08-01",
            "end": "2026-08-01",
            "cutoff": "2026-08-01",
            "market_dates": training,
        },
        "validation": {
            "start": "2026-08-02",
            "end": "2026-08-02",
            "market_dates": validation,
        },
        "untouched_holdout": {
            "start": "2026-09-01",
            "end": "2026-09-02",
            "market_dates": holdout,
        },
    }
    return register_experiment(
        hypothesis="A bounded challenger improves net expectancy.",
        training_cutoff="2026-08-01",
        baseline_config={"threshold": 1},
        candidate_config={"threshold": 2},
        validation_start="2026-08-02",
        holdout_start="2026-09-01",
        stop_condition="Quarantine incomplete truth.",
        promotion_requirements=["manual review"],
        training_dates=training,
        validation_dates=validation,
        holdout_dates=holdout,
        validation_end="2026-08-02",
        holdout_end="2026-09-02",
        data_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_sha="c" * 40,
        window_hash_sha256=canonical_hash(windows),
        input_hash_sha256="d" * 64,
        v5_comparison_hash_sha256="e" * 64,
    )


def test_missing_windows_is_signed_and_stable() -> None:
    first = build_drift_report(baseline_rows=[], current_rows=[])
    second = build_drift_report(baseline_rows=[], current_rows=[])
    assert first["status"] == "NOT_EVALUABLE_FROZEN_WINDOWS_REQUIRED"
    assert first["auto_quarantine"] is True
    assert first["receipt_hash_sha256"] == second["receipt_hash_sha256"]
    assert first["missing_truth_is_zero"] is False


@pytest.mark.parametrize("threshold", [True, False, 1.5, math.nan, math.inf, "20"])
def test_drift_rejects_malformed_thresholds(threshold: object) -> None:
    with pytest.raises(ValueError):
        build_drift_report(
            baseline_rows=[], current_rows=[], minimum_observations=threshold  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("value", [True, math.nan, math.inf, "0.5"])
def test_holdout_rejects_malformed_expectancy(value: object) -> None:
    experiment = _experiment()
    evidence = {
        "experiment_id": experiment["experiment_id"],
        "configuration_hash_sha256": experiment["configuration_hash_sha256"],
        "data_hash_sha256": experiment["data_hash_sha256"],
        "source_hash_sha256": experiment["source_hash_sha256"],
        "code_sha": experiment["code_sha"],
        "window_hash_sha256": experiment["window_hash_sha256"],
        "input_hash_sha256": experiment["input_hash_sha256"],
        "v5_comparison_hash_sha256": experiment["v5_comparison_hash_sha256"],
        "evaluation_window": experiment["frozen_windows"],
        "coverage": {
            "baseline": {"market_dates": ["2026-09-01", "2026-09-02"]},
            "candidate": {"market_dates": ["2026-09-01", "2026-09-02"]},
        },
        "model_run_id": "v6m-hostile",
        "source_lineage_hash_sha256": experiment["source_hash_sha256"],
        "no_lookahead": True,
        "after_cost_expectancy_pct": value,
    }
    with pytest.raises(ValueError, match="expectancy"):
        record_untouched_holdout_evaluation(
            experiment=experiment,
            evidence=evidence,
            existing_evaluations=[],
            evaluated_at="2026-09-04T15:00:00-05:00",
        )


def test_holdout_receipt_preserves_actual_evaluation_instant() -> None:
    experiment = _experiment()
    evidence = {
        "experiment_id": experiment["experiment_id"],
        "configuration_hash_sha256": experiment["configuration_hash_sha256"],
        "data_hash_sha256": experiment["data_hash_sha256"],
        "source_hash_sha256": experiment["source_hash_sha256"],
        "code_sha": experiment["code_sha"],
        "window_hash_sha256": experiment["window_hash_sha256"],
        "input_hash_sha256": experiment["input_hash_sha256"],
        "v5_comparison_hash_sha256": experiment["v5_comparison_hash_sha256"],
        "evaluation_window": experiment["frozen_windows"],
        "coverage": {
            "baseline": {"market_dates": ["2026-09-01", "2026-09-02"]},
            "candidate": {"market_dates": ["2026-09-01", "2026-09-02"]},
        },
        "model_run_id": "v6m-hostile",
        "source_lineage_hash_sha256": experiment["source_hash_sha256"],
        "no_lookahead": True,
        "after_cost_expectancy_pct": None,
    }
    receipt = record_untouched_holdout_evaluation(
        experiment=experiment,
        evidence=evidence,
        existing_evaluations=[],
        evaluated_at="2026-09-04T15:00:00-05:00",
    )
    assert receipt["evaluated_at"] == "2026-09-04T20:00:00+00:00"
    assert receipt["status"] == "NEGATIVE_OR_INCOMPLETE_HOLDOUT"
    assert receipt["receipt_hash_sha256"]
