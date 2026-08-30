from __future__ import annotations

import pytest

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.registry import (
    record_untouched_holdout_evaluation,
    register_experiment,
)
from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _complete_experiment() -> dict[str, object]:
    training = ["2026-07-30", "2026-07-31"]
    validation = ["2026-08-10", "2026-08-11"]
    holdout = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"]
    windows = {
        "training": {
            "start": training[0],
            "end": training[-1],
            "cutoff": training[-1],
            "market_dates": training,
        },
        "validation": {
            "start": validation[0],
            "end": validation[-1],
            "market_dates": validation,
        },
        "untouched_holdout": {
            "start": holdout[0],
            "end": holdout[-1],
            "market_dates": holdout,
        },
    }
    return register_experiment(
        hypothesis="Tighter spread filter reduces tail loss.",
        training_cutoff=training[-1],
        baseline_config={"max_spread_bps": 200, "min_volume": 1_000_000},
        candidate_config={"max_spread_bps": 150, "min_volume": 1_000_000},
        validation_start=validation[0],
        holdout_start=holdout[0],
        stop_condition="Quarantine after data-quality failure.",
        promotion_requirements=["manual approval"],
        training_dates=training,
        validation_dates=validation,
        holdout_dates=holdout,
        validation_end=validation[-1],
        holdout_end=holdout[-1],
        data_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_sha="c" * 40,
        window_hash_sha256=canonical_hash(windows),
        input_hash_sha256="d" * 64,
        v5_comparison_hash_sha256="e" * 64,
    )


def test_v6_registry_allows_exactly_one_forward_only_change() -> None:
    experiment = register_experiment(
        hypothesis="Tighter spread filter reduces tail loss.",
        training_cutoff="2026-08-01",
        baseline_config={"max_spread_bps": 200, "min_volume": 1_000_000},
        candidate_config={"max_spread_bps": 150, "min_volume": 1_000_000},
        validation_start="2026-08-02",
        holdout_start="2026-09-01",
        stop_condition="Quarantine after data-quality failure.",
        promotion_requirements=["manual approval"],
    )

    assert experiment["changed_field"] == "max_spread_bps"
    assert experiment["automatic_policy_change"] is False

    with pytest.raises(ValueError, match="exactly one"):
        register_experiment(
            hypothesis="bad",
            training_cutoff="2026-08-01",
            baseline_config={"a": 1, "b": 1},
            candidate_config={"a": 2, "b": 2},
            validation_start="2026-08-02",
            holdout_start="2026-09-01",
            stop_condition="stop",
            promotion_requirements=["manual"],
        )


def test_v6_holdout_can_be_recorded_only_once(tmp_path) -> None:
    experiment = _complete_experiment()
    evidence = {
        "experiment_id": experiment["experiment_id"],
        "model_run_id": "v6m-registry-test",
        "configuration_hash_sha256": experiment["configuration_hash_sha256"],
        "data_hash_sha256": experiment["data_hash_sha256"],
        "source_hash_sha256": experiment["source_hash_sha256"],
        "source_lineage_hash_sha256": experiment["source_hash_sha256"],
        "code_sha": experiment["code_sha"],
        "window_hash_sha256": experiment["window_hash_sha256"],
        "input_hash_sha256": experiment["input_hash_sha256"],
        "v5_comparison_hash_sha256": experiment["v5_comparison_hash_sha256"],
        "evaluation_window": experiment["frozen_windows"],
        "coverage": {
            "baseline": {
                "market_dates": experiment["frozen_windows"]["untouched_holdout"]["market_dates"]
            },
            "candidate": {
                "market_dates": experiment["frozen_windows"]["untouched_holdout"]["market_dates"]
            },
        },
        "after_cost_expectancy_pct": 0.5,
        "no_lookahead": True,
    }
    receipt = record_untouched_holdout_evaluation(
        experiment=experiment,
        evidence=evidence,
        existing_evaluations=[],
        evaluated_at="2026-10-01T21:00:00+00:00",
    )
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    store.persist_alpha_v6_experiments([experiment])

    assert store.persist_alpha_v6_holdout_evaluation(receipt) is True
    with pytest.raises(StorageError, match="receipt hash"):
        store.persist_alpha_v6_holdout_evaluation({**receipt, "status": "TUNED"})
    with pytest.raises(ValueError, match="already evaluated"):
        record_untouched_holdout_evaluation(
            experiment=experiment,
            evidence=evidence,
            existing_evaluations=[receipt],
        )
