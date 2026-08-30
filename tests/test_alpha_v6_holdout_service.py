from __future__ import annotations

from datetime import date, timedelta

import pytest

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.registry import register_experiment
from intraday_scanner.services.alpha_v6_holdout_service import (
    evaluate_registered_holdout,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from tests._alpha_path_truth import (
    canonical_return_outcome,
    canonical_v6_decision,
    causal_identity_from,
)


def _complete_experiment(holdout_dates: list[str], model_run_id: str) -> dict[str, object]:
    training = ["2026-07-30", "2026-07-31"]
    validation = ["2026-08-10", "2026-08-11"]
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
            "start": holdout_dates[0],
            "end": holdout_dates[-1],
            "market_dates": holdout_dates,
        },
    }
    experiment = register_experiment(
        hypothesis="Tighter spread filter reduces tail loss.",
        training_cutoff=training[-1],
        baseline_config={"max_spread_bps": 200},
        candidate_config={"max_spread_bps": 150},
        validation_start=validation[0],
        holdout_start=holdout_dates[0],
        stop_condition="Quarantine after source failure.",
        promotion_requirements=["manual approval"],
        training_dates=training,
        validation_dates=validation,
        holdout_dates=holdout_dates,
        validation_end=validation[-1],
        holdout_end=holdout_dates[-1],
        data_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_sha="c" * 40,
        window_hash_sha256=canonical_hash(windows),
        input_hash_sha256="d" * 64,
        v5_comparison_hash_sha256="e" * 64,
    )
    experiment["model_run_id"] = model_run_id
    return experiment


def _persist_model_run(
    store: SQLiteScanStore, experiment: dict[str, object], model_run_id: str
) -> None:
    store.persist_alpha_v6_model_run(
        {
            "model_run_id": model_run_id,
            "model_version": "v6-test",
            "trained_at": "2026-08-12T00:00:00+00:00",
            "training_cutoff": experiment["training_cutoff"],
            "status": "COMPLETE",
            "training_input_hash_sha256": experiment["input_hash_sha256"],
            "experiment_id": experiment["experiment_id"],
            "configuration_hash_sha256": experiment["configuration_hash_sha256"],
            "source_lineage_hash_sha256": experiment["source_hash_sha256"],
            "code_sha": experiment["code_sha"],
            "evaluation_window": experiment["frozen_windows"],
        }
    )


def test_holdout_rejects_pre_start_and_records_one_tagged_two_arm_evaluation(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    holdout_dates = [(date(2026, 9, 1) + timedelta(days=index)).isoformat() for index in range(5)]
    model_run_id = "v6m-holdout-first"
    experiment = _complete_experiment(holdout_dates, model_run_id)
    store.persist_alpha_v6_experiments([experiment])
    _persist_model_run(store, experiment, model_run_id)

    pre_holdout = evaluate_registered_holdout(
        store,
        experiment_id=experiment["experiment_id"],
        as_of_date="2026-08-31T18:00:00-05:00",
        model_run_id=model_run_id,
    )

    assert pre_holdout["status"] == "PRE_HOLDOUT_EVALUATION_REJECTED"
    assert store.load_alpha_v6_holdout_evaluations() == []

    decisions = []
    outcomes = []
    start = date(2026, 9, 1)
    for arm, config_hash, value in (
        ("baseline", experiment["baseline_configuration_hash_sha256"], 0.1),
        ("candidate", experiment["configuration_hash_sha256"], 0.5),
    ):
        for index in range(10):
            decision_id = f"{arm}-{index}"
            market_date = (start + timedelta(days=index % 5)).isoformat()
            decision = {
                **canonical_v6_decision(decision_id, market_date=market_date),
                "experiment_assignment": {
                    "experiment_id": experiment["experiment_id"],
                    "arm": arm,
                    "configuration_hash_sha256": config_hash,
                },
            }
            decisions.append(decision)
            outcomes.append(
                {
                    **canonical_return_outcome(
                        market_date=market_date,
                        net_excess_return_pct=value,
                        causal_identity=causal_identity_from(
                            decision,
                            kind="alpha_v6_shadow_decision",
                        ),
                    ),
                    "decision_id": decision_id,
                    "shadow_signal_id": f"shadow-{decision_id}",
                    "market_date": market_date,
                    "observed_at": f"{market_date}T21:00:00+00:00",
                }
            )
    store.persist_alpha_v6_decisions(decisions)
    store.persist_alpha_v6_outcomes(outcomes)

    recorded = evaluate_registered_holdout(
        store,
        experiment_id=experiment["experiment_id"],
        as_of_date="2026-09-10T21:00:00-05:00",
        model_run_id=model_run_id,
    )

    assert recorded["status"] == "HOLDOUT_RECORDED"
    assert recorded["holdout_evaluation"]["evidence"]["after_cost_expectancy_pct"] > 0
    assert all(
        row["net_excess_return_pct"] == pytest.approx(0.1)
        for row in outcomes
        if row["decision_id"].startswith("baseline-")
    )
    assert {
        row["net_excess_return_pct"]
        for row in outcomes
        if row["decision_id"].startswith("candidate-")
    } == {0.5}
    assert len(store.load_alpha_v6_holdout_evaluations()) == 1
    assert evaluate_registered_holdout(
        store,
        experiment_id=experiment["experiment_id"],
        as_of_date="2026-09-10T21:00:00-05:00",
        model_run_id=model_run_id,
    )["status"] == "ALREADY_EVALUATED_IMMUTABLE"


def test_holdout_never_retrofits_untagged_history_into_an_experiment(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    holdout_dates = ["2026-09-01", "2026-09-02"]
    model_run_id = "v6m-holdout-untagged"
    experiment = _complete_experiment(holdout_dates, model_run_id)
    store.persist_alpha_v6_experiments([experiment])
    _persist_model_run(store, experiment, model_run_id)
    store.persist_alpha_v6_decisions(
        [
            {
                "decision_id": "untagged",
                "scan_id": "scan-untagged",
                "source_signal_id": "source-untagged",
                "shadow_signal_id": "shadow-untagged",
                "market_date": "2026-09-02",
                "decision_at": "2026-09-02T14:30:00+00:00",
                "ticker": "UNTG",
                "strategy_version": "test-v6",
                "model_version": "test-model",
                "action": "SHADOW_TRACK",
                "input_hash_sha256": "input-untagged",
                "source_lineage_hash_sha256": "lineage-untagged",
            }
        ]
    )
    store.persist_alpha_v6_outcomes(
        [
            {
                "outcome_id": "untagged-outcome",
                "decision_id": "untagged",
                "shadow_signal_id": "untagged-shadow",
                "market_date": "2026-09-02",
                "observed_at": "2026-09-02T21:00:00+00:00",
                "activation_status": "ACTIVATED",
                "outcome_status": "COMPLETE_SOURCED",
                "net_excess_return_pct": 99.0,
                "source_bar_hash_sha256": "a" * 64,
                "learning_eligible": True,
                "no_lookahead": True,
            }
        ]
    )

    result = evaluate_registered_holdout(
        store,
        experiment_id=experiment["experiment_id"],
        as_of_date="2026-09-10T21:00:00-05:00",
        model_run_id=model_run_id,
    )

    assert result["status"] == "NOT_EVALUABLE_INSUFFICIENT_PROSPECTIVE_HOLDOUT_EVIDENCE"
    assert result["persisted"] is False
    assert store.load_alpha_v6_holdout_evaluations() == []


def test_holdout_quarantines_legacy_boolean_outcomes_without_current_contract(
    tmp_path,
) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    holdout_dates = [(date(2026, 9, 1) + timedelta(days=index)).isoformat() for index in range(60)]
    model_run_id = "v6m-holdout-legacy"
    experiment = _complete_experiment(holdout_dates, model_run_id)
    store.persist_alpha_v6_experiments([experiment])
    _persist_model_run(store, experiment, model_run_id)
    decisions = []
    outcomes = []
    start = date(2026, 9, 1)
    for arm, config_hash in (
        ("baseline", experiment["baseline_configuration_hash_sha256"]),
        ("candidate", experiment["configuration_hash_sha256"]),
    ):
        for index in range(60):
            decision_id = f"legacy-{arm}-{index}"
            market_date = (start + timedelta(days=index % 60)).isoformat()
            decisions.append(
                {
                    "decision_id": decision_id,
                    "scan_id": f"scan-{decision_id}",
                    "source_signal_id": f"source-{decision_id}",
                    "shadow_signal_id": f"shadow-{decision_id}",
                    "market_date": market_date,
                    "decision_at": f"{market_date}T14:30:00+00:00",
                    "ticker": f"L{index}{arm[0]}",
                    "strategy_version": "test-v6",
                    "model_version": "test-model",
                    "action": "SHADOW_TRACK",
                    "input_hash_sha256": f"input-{decision_id}",
                    "source_lineage_hash_sha256": f"lineage-{decision_id}",
                    "experiment_assignment": {
                        "experiment_id": experiment["experiment_id"],
                        "arm": arm,
                        "configuration_hash_sha256": config_hash,
                    },
                }
            )
            outcomes.append(
                {
                    "outcome_id": f"outcome-{decision_id}",
                    "decision_id": decision_id,
                    "shadow_signal_id": f"shadow-{decision_id}",
                    "market_date": market_date,
                    "observed_at": f"{market_date}T21:00:00+00:00",
                    "activation_status": "ACTIVATED",
                    "outcome_status": "COMPLETE_SOURCED",
                    "net_excess_return_pct": 99.0,
                    "source_bar_hash_sha256": "a" * 64,
                    "learning_eligible": True,
                    "no_lookahead": True,
                }
            )
    store.persist_alpha_v6_decisions(decisions)
    store.persist_alpha_v6_outcomes(outcomes)

    result = evaluate_registered_holdout(
        store,
        experiment_id=experiment["experiment_id"],
        as_of_date="2026-12-31T21:00:00-05:00",
        model_run_id=model_run_id,
    )

    assert result["status"] == "NOT_EVALUABLE_INSUFFICIENT_PROSPECTIVE_HOLDOUT_EVIDENCE"
    assert "incomplete_or_unsourced_holdout_outcome" in result["blockers"]
    assert result["persisted"] is False
    assert store.load_alpha_v6_holdout_evaluations() == []


def test_holdout_rejects_authentic_retro_only_current_truth(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "v6.sqlite")
    holdout_dates = [(date(2026, 9, 1) + timedelta(days=index)).isoformat() for index in range(10)]
    model_run_id = "v6m-holdout-retro"
    experiment = _complete_experiment(holdout_dates, model_run_id)
    store.persist_alpha_v6_experiments([experiment])
    _persist_model_run(store, experiment, model_run_id)
    decisions = []
    outcomes = []
    for arm, config_hash in (
        ("baseline", experiment["baseline_configuration_hash_sha256"]),
        ("candidate", experiment["configuration_hash_sha256"]),
    ):
        for index in range(10):
            market_date = (date(2026, 9, 1) + timedelta(days=index)).isoformat()
            decision_id = f"retro-{arm}-{index}"
            decision = {
                **canonical_v6_decision(decision_id, market_date=market_date),
                "experiment_assignment": {
                    "experiment_id": experiment["experiment_id"],
                    "arm": arm,
                    "configuration_hash_sha256": config_hash,
                },
            }
            decisions.append(decision)
            outcomes.append(
                {
                    **canonical_return_outcome(
                        market_date=market_date,
                        prospective=False,
                        causal_identity=causal_identity_from(
                            decision,
                            kind="alpha_v6_shadow_decision",
                        ),
                    ),
                    "decision_id": decision_id,
                    "market_date": market_date,
                }
            )
    store.persist_alpha_v6_decisions(decisions)
    store.persist_alpha_v6_outcomes(outcomes)

    result = evaluate_registered_holdout(
        store,
        experiment_id=experiment["experiment_id"],
        as_of_date="2026-09-30T21:00:00-05:00",
        model_run_id=model_run_id,
    )

    assert result["status"] == "NOT_EVALUABLE_INSUFFICIENT_PROSPECTIVE_HOLDOUT_EVIDENCE"
    assert result["persisted"] is False
    assert store.load_alpha_v6_holdout_evaluations() == []
