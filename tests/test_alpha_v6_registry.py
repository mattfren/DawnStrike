from __future__ import annotations

import pytest

from intraday_scanner.alpha.v6.registry import register_experiment


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
