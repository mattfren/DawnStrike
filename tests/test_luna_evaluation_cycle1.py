from __future__ import annotations

import json
from hashlib import sha256

import pytest

from intraday_scanner.alpha.v6.calibration import calibration_report, interval_coverage
from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.registry import (
    record_untouched_holdout_evaluation,
    register_experiment,
)
from intraday_scanner.alpha.v6.validation import (
    aggregate_daily_returns,
    daily_weighting_status,
    evaluate_return_predictions,
)
from intraday_scanner.alpha.v6_shadow import promotion_readiness
from intraday_scanner.performance.account_comparison import build_account_comparison
from intraday_scanner.services.alpha_v6_holdout_service import evaluate_registered_holdout
from intraday_scanner.services.alpha_v6_learning_service import (
    _comparison_to_v5_evidence,
    _holdout_evidence_for_model,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore, StorageError


def _hash(value: object) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _complete_experiment() -> dict[str, object]:
    training = ["2026-07-30", "2026-07-31"]
    validation = ["2026-08-10", "2026-08-11"]
    holdout = ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04", "2026-09-05"]
    windows = {
        "training": {
            "start": training[0], "end": training[-1], "cutoff": training[-1],
            "market_dates": training,
        },
        "validation": {"start": validation[0], "end": validation[-1], "market_dates": validation},
        "untouched_holdout": {"start": holdout[0], "end": holdout[-1], "market_dates": holdout},
    }
    return register_experiment(
        hypothesis="Require exact lineage across the learning evidence spine.",
        training_cutoff=training[-1],
        baseline_config={"threshold": 1},
        candidate_config={"threshold": 2},
        validation_start=validation[0],
        holdout_start=holdout[0],
        stop_condition="Quarantine on lineage mismatch.",
        promotion_requirements=["manual approval"],
        training_dates=training,
        validation_dates=validation,
        holdout_dates=holdout,
        validation_end=validation[-1],
        holdout_end=holdout[-1],
        data_hash_sha256="a" * 64,
        source_hash_sha256="a" * 64,
        code_sha="d" * 40,
        window_hash_sha256=canonical_hash(windows),
        input_hash_sha256="a" * 64,
        v5_comparison_hash_sha256="a" * 64,
    )


def test_v6_loader_is_complete_and_explicit_caps_fail_closed(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "large.sqlite")
    store.initialize()
    rows = [
        (
            f"decision-{index:05d}",
            f"scan-{index:05d}",
            f"source-{index:05d}",
            f"shadow-{index:05d}",
            "2026-08-29",
            f"2026-08-29T12:{index // 60:02d}:{index % 60:02d}+00:00",
            "AAA",
            "v6",
            "model",
            "SHADOW_TRACK",
            "setup",
            "regime",
            "[]",
            "a" * 64,
            "b" * 64,
            "2026-08-29T23:00:00+00:00",
            json.dumps({"decision_id": f"decision-{index:05d}"}),
        )
        for index in range(50_001)
    ]
    with store._connect() as connection:
        connection.executemany(
            """
            INSERT INTO alpha_v6_decisions
            (decision_id, scan_id, source_signal_id, shadow_signal_id,
             market_date, decision_at, ticker, strategy_version, model_version,
             action, setup_key, regime_key, safety_vetoes_json,
             input_hash_sha256, source_lineage_hash_sha256, stored_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    loaded = store.load_alpha_v6_decisions()
    assert len(loaded) == 50_001
    assert any(row["decision_id"] == "decision-50000" for row in loaded)
    with pytest.raises(StorageError, match="truncated"):
        store.load_alpha_v6_decisions(limit=50_000)
    for index in range(150):
        store.persist_alpha_v6_model_run(
            {
                "model_run_id": f"model-{index:03d}",
                "model_version": "v6",
                "trained_at": f"2026-08-{(index % 28) + 1:02d}T00:00:00+00:00",
                "training_cutoff": "2026-08-01",
                "status": "TRAINED",
                "training_input_hash_sha256": "a" * 64,
            }
        )
    assert store.load_alpha_v6_model_run("model-000")["model_run_id"] == "model-000"


def test_risk_metrics_are_permutation_invariant_and_daily_weighted() -> None:
    rows = [
        {
            "decision_id": "a",
            "market_date": "2026-08-01",
            "utility_lcb_pct": 1.0,
            "realized_net_excess_return_pct": 10.0,
            "allocation_weight": 0.25,
            "account_weight": 1.0,
        },
        {
            "decision_id": "b",
            "market_date": "2026-08-01",
            "utility_lcb_pct": 1.0,
            "realized_net_excess_return_pct": -10.0,
            "allocation_weight": 0.75,
            "account_weight": 1.0,
        },
        {
            "decision_id": "c",
            "market_date": "2026-08-02",
            "utility_lcb_pct": 1.0,
            "realized_net_excess_return_pct": 2.0,
            "allocation_weight": 0.25,
            "account_weight": 1.0,
        },
    ]
    assert aggregate_daily_returns(rows)["2026-08-01"][0] == pytest.approx(-5.0)
    first = evaluate_return_predictions(rows, bootstrap_samples=100)
    second = evaluate_return_predictions(list(reversed(rows)), bootstrap_samples=100)
    assert first["risk_observation_count"] == 2
    assert first["annualized_observation_sharpe"] == second["annualized_observation_sharpe"]
    assert first["maximum_drawdown_pct"] == second["maximum_drawdown_pct"]
    assert first["gain_loss_concentration_pct"] == second["gain_loss_concentration_pct"]
    assert first["downside_deviation_pct"] == pytest.approx(3.535534)
    assert first["profit_factor"] == pytest.approx(0.1)


def test_partial_or_missing_allocation_truth_is_non_promotion_diagnostic() -> None:
    partial = [
        {
            "market_date": "2026-08-01",
            "realized_net_excess_return_pct": 10.0,
            "allocation_pct": 25.0,
        },
        {"market_date": "2026-08-01", "realized_net_excess_return_pct": -10.0},
    ]
    explicit_partial = [partial[0]]
    assert aggregate_daily_returns(explicit_partial)["2026-08-01"][0] == pytest.approx(2.5)
    assert daily_weighting_status(partial)["status"] == "PARTIAL_ALLOCATION_TRUTH"
    assert daily_weighting_status(partial)["promotion_eligible"] is False

    missing = [
        {"market_date": "2026-08-01", "realized_net_excess_return_pct": 10.0},
        {"market_date": "2026-08-02", "realized_net_excess_return_pct": -10.0},
    ]
    assert daily_weighting_status(missing)["status"] == "EQUAL_WEIGHT_RESEARCH_DIAGNOSTIC"
    metrics = evaluate_return_predictions(
        [
            {
                **row,
                "utility_lcb_pct": 1.0,
            }
            for row in missing
        ],
        bootstrap_samples=100,
    )
    assert metrics["risk_series_promotion_eligible"] is False
    assert daily_weighting_status(
        [
            {
                "market_date": "not-a-date",
                "realized_net_excess_return_pct": 1.0,
                "allocation_weight": 0.5,
            }
        ]
    )["status"] == "INVALID_ALLOCATION_TRUTH"
    assert daily_weighting_status(
        [
            {
                "market_date": "2026-08-01",
                "realized_net_excess_return_pct": 1.0,
                "allocation_weight": 2.0,
                "account_weight": 0.5,
            }
        ]
    )["status"] == "INVALID_ALLOCATION_TRUTH"
    assert daily_weighting_status(
        [
            {
                "market_date": "2026-08-01",
                "realized_net_excess_return_pct": 1.0,
                "position_weight": 0.5,
                "allocation_pct": 80.0,
            }
        ]
    )["status"] == "AUTHENTIC_ACCOUNT_WEIGHTED"
    assert aggregate_daily_returns(
        [
            {
                "market_date": "2026-08-01",
                "realized_net_excess_return_pct": 10.0,
                "position_weight": 0.5,
                "allocation_pct": 80.0,
            }
        ]
    )["2026-08-01"][0] == pytest.approx(5.0)
    assert daily_weighting_status(
        [
            {
                "market_date": "2026-08-01",
                "realized_net_excess_return_pct": 1.0,
                "allocation_weight": 0.0,
            }
        ]
    )["status"] == "ZERO_ALLOCATION_ACCOUNT_DAY"


def test_model_bound_holdout_rejects_forged_or_unpersisted_lineage(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "holdout-lineage.sqlite")
    experiment = _complete_experiment()
    store.persist_alpha_v6_experiments([experiment])
    result = evaluate_registered_holdout(
        store,
        experiment_id=str(experiment["experiment_id"]),
        as_of_date="2026-09-10",
        model_run_id="forged-model",
    )
    assert result["status"] == "MODEL_RUN_LINEAGE_MISSING_OR_MISMATCHED"
    assert result["persisted"] is False

    store.persist_alpha_v6_model_run(
        {
            "model_run_id": "forged-model",
            "experiment_id": str(experiment["experiment_id"]),
            "configuration_hash_sha256": "f" * 64,
            "dataset_hash_sha256": "s" * 64,
            "code_sha": "d" * 40,
            "training_cutoff": experiment["training_cutoff"],
            "evaluation_window": experiment["frozen_windows"],
        }
    )
    result = evaluate_registered_holdout(
        store,
        experiment_id=str(experiment["experiment_id"]),
        as_of_date="2026-09-10",
        model_run_id="forged-model",
    )
    assert result["status"] == "MODEL_RUN_LINEAGE_MISSING_OR_MISMATCHED"
    assert "configuration_hash_mismatch" in result["blockers"]
    store.persist_alpha_v6_model_run(
        {
            "model_run_id": "forged-cutoff",
            "experiment_id": str(experiment["experiment_id"]),
            "configuration_hash_sha256": experiment["configuration_hash_sha256"],
            "dataset_hash_sha256": "s" * 64,
            "code_sha": "d" * 40,
            "training_cutoff": "2026-08-02",
            "evaluation_window": experiment["frozen_windows"],
        }
    )
    result = evaluate_registered_holdout(
        store,
        experiment_id=str(experiment["experiment_id"]),
        as_of_date="2026-09-10",
        model_run_id="forged-cutoff",
    )
    assert "model_training_cutoff_mismatch" in result["blockers"]


def test_holdout_service_prefers_exact_indexed_lineage_loaders() -> None:
    experiment = _complete_experiment()
    model = {
        "model_run_id": "indexed-model",
        "experiment_id": experiment["experiment_id"],
        "configuration_hash_sha256": experiment["configuration_hash_sha256"],
        "dataset_hash_sha256": experiment["source_hash_sha256"],
        "code_sha": "d" * 40,
        "training_cutoff": experiment["training_cutoff"],
        "evaluation_window": experiment["frozen_windows"],
    }

    class ExactLoaderSpy:
        def load_alpha_v6_experiment(self, experiment_id: str) -> dict[str, object]:
            assert experiment_id == experiment["experiment_id"]
            return experiment

        def load_alpha_v6_model_run(self, model_run_id: str) -> dict[str, object]:
            assert model_run_id == "indexed-model"
            return model

        def load_alpha_v6_experiments(self, *, limit: int) -> list[dict[str, object]]:
            raise AssertionError("old bounded experiment scan used")

        def load_alpha_v6_model_runs(self, *, limit: int) -> list[dict[str, object]]:
            raise AssertionError("old bounded model scan used")

        def load_alpha_v6_holdout_evaluations(self, *, limit: int) -> list[dict[str, object]]:
            return []

        def load_alpha_v6_decisions(self) -> list[dict[str, object]]:
            return []

        def load_alpha_v6_outcomes(self) -> list[dict[str, object]]:
            return []

    result = evaluate_registered_holdout(
        ExactLoaderSpy(),
        experiment_id=str(experiment["experiment_id"]),
        as_of_date="2026-09-10",
        model_run_id="indexed-model",
    )
    assert result["status"] == "NOT_EVALUABLE_INSUFFICIENT_PROSPECTIVE_HOLDOUT_EVIDENCE"


def test_calibration_display_requires_samples_sessions_valid_bounds_and_model_binding() -> None:
    calibration = calibration_report(
        [
            {"activation_probability": 0.8, "activation_label": 1, "market_date": "2026-08-01"},
            {"activation_probability": 0.2, "activation_label": 0, "market_date": "2026-08-02"},
        ]
    )
    intervals = interval_coverage(
        [
            {
                "interval_lower_pct": 2.0,
                "interval_upper_pct": 1.0,
                "realized_return_pct": 1.5,
                "market_date": "2026-08-01",
            }
        ]
    )
    assert calibration["display_eligible"] is False
    assert intervals["display_eligible"] is False

    forged_holdout = {
        "evaluated_once": True,
        "holdout_evaluation_id": "h1",
        "experiment_id": "exp1",
        "model_run_id": "model1",
        "configuration_hash_sha256": "c" * 64,
        "evidence": {"after_cost_expectancy_pct": 1.0, "model_run_id": "model1"},
        "evidence_hash_sha256": "0" * 64,
        "model_binding_hash_sha256": "0" * 64,
    }
    readiness = promotion_readiness(
        [],
        evaluation={"model_run_id": "model1", "untouched_holdout": forged_holdout},
    )
    assert readiness["status"] == "NOT_ELIGIBLE_FOR_PROMOTION"
    assert "untouched_holdout_receipt_missing_or_hash_mismatch" in readiness["promotion_blockers"]


class _ComparisonStore:
    def __init__(self, comparison: dict[str, object]) -> None:
        self.comparison = comparison
        self.model_runs: list[dict[str, object]] | None = None
        self.experiments: list[dict[str, object]] | None = None
        self.comparisons: list[dict[str, object]] | None = None
        self.holdouts: list[dict[str, object]] = []

    def load_latest_account_performance_comparison(self) -> dict[str, object]:
        return self.comparison

    def load_alpha_v6_model_runs(self, *, limit: int) -> list[dict[str, object]]:
        if self.model_runs is not None:
            return self.model_runs
        return [
            {
                "model_run_id": "model1",
                "code_sha": "d" * 40,
                "dataset_hash_sha256": "e" * 64,
                "training_cutoff": "2026-08-01",
            }
        ]

    def load_alpha_v6_experiments(self, *, limit: int) -> list[dict[str, object]]:
        if self.experiments is not None:
            return self.experiments
        return [
            {
                "experiment_id": "exp1",
                "configuration_hash_sha256": "c" * 64,
                "validation_start": "2026-08-10",
                "untouched_holdout_start": "2026-09-01",
            }
        ]

    def load_account_performance_comparisons(self, *, limit: int) -> list[dict[str, object]]:
        return self.comparisons if self.comparisons is not None else [self.comparison]

    def load_alpha_v6_holdout_evaluations(self, *, limit: int) -> list[dict[str, object]]:
        return self.holdouts


def test_v5_comparison_without_persisted_candidate_lineage_is_blocked() -> None:
    result = _comparison_to_v5_evidence(
        _ComparisonStore(
            {
                "status": "COMPLETE_ACCOUNT_LEVEL_COMPARISON",
                "comparison_id": "comparison1",
                "input_hash_sha256": "a" * 64,
                "series_metrics": {},
            }
        ),
        model_run_id="model1",
    )
    assert result["status"] == "COMPARISON_LINEAGE_MISSING_OR_MISMATCHED"
    assert "model_run_id_mismatch" in result["lineage_blockers"]
    assert "candidate_experiment_missing" in result["lineage_blockers"]
    assert "source_lineage_hash_mismatch_or_missing" in result["lineage_blockers"]
    assert "comparison_metrics_hash_mismatch_or_missing" in result["lineage_blockers"]


def test_v5_comparison_with_wrong_persisted_model_binding_is_blocked() -> None:
    result = _comparison_to_v5_evidence(
        _ComparisonStore(
            {
                "status": "COMPLETE_ACCOUNT_LEVEL_COMPARISON",
                "comparison_id": "comparison1",
                "input_hash_sha256": "a" * 64,
                "model_run_id": "different-model",
                "experiment_id": "exp1",
                "configuration_hash_sha256": "c" * 64,
                "source_lineage_hash_sha256": "e" * 64,
                "code_sha": "d" * 40,
                "evaluation_window": {
                    "training_cutoff": "2026-08-01",
                    "validation_start": "2026-08-10",
                    "untouched_holdout_start": "2026-09-01",
                },
                "model_binding_hash_sha256": "0" * 64,
                "series_metrics": {},
            }
        ),
        model_run_id="model1",
    )
    assert result["status"] == "COMPARISON_LINEAGE_MISSING_OR_MISMATCHED"
    assert "model_run_id_mismatch" in result["lineage_blockers"]
    assert "model_binding_hash_mismatch_or_missing" in result["lineage_blockers"]


def test_bound_experiment_model_holdout_and_comparison_reach_manual_readiness(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "bound-lineage.sqlite")
    experiment = _complete_experiment()
    model_lineage = {
        "model_run_id": "bound-model",
        "experiment_id": experiment["experiment_id"],
        "configuration_hash_sha256": experiment["configuration_hash_sha256"],
        "source_lineage_hash_sha256": "a" * 64,
        "code_sha": "d" * 40,
        "evaluation_window": experiment["frozen_windows"],
    }
    store.persist_alpha_v6_experiments([experiment])
    store.persist_alpha_v6_model_run(
        {
            **model_lineage,
            "model_version": "v6",
            "trained_at": "2026-08-02T00:00:00+00:00",
            "training_cutoff": experiment["training_cutoff"],
            "dataset_hash_sha256": model_lineage["source_lineage_hash_sha256"],
            "training_input_hash_sha256": model_lineage["source_lineage_hash_sha256"],
        }
    )
    holdout = record_untouched_holdout_evaluation(
        experiment=experiment,
        evidence={
            **model_lineage,
            "data_hash_sha256": experiment["data_hash_sha256"],
            "source_hash_sha256": experiment["source_hash_sha256"],
            "window_hash_sha256": experiment["window_hash_sha256"],
            "input_hash_sha256": experiment["input_hash_sha256"],
            "v5_comparison_hash_sha256": experiment["v5_comparison_hash_sha256"],
            "coverage": {
                "baseline": {
                    "market_dates": experiment["frozen_windows"]["untouched_holdout"][
                        "market_dates"
                    ]
                },
                "candidate": {
                    "market_dates": experiment["frozen_windows"]["untouched_holdout"][
                        "market_dates"
                    ]
                },
            },
            "after_cost_expectancy_pct": 0.5,
            "no_lookahead": True,
        },
        existing_evaluations=[],
        evaluated_at="2026-09-10T23:59:59+00:00",
    )
    assert store.persist_alpha_v6_holdout_evaluation(holdout) is True

    days = [f"2026-09-{index:02d}" for index in range(1, 6)]
    def account_rows(value: float, prefix: str) -> list[dict[str, object]]:
        return [
            {
                "market_date": day,
                "status": "COMPLETE",
                "net_return_pct": value,
                "source_hash_sha256": f"{prefix}{index}".ljust(64, "0"),
                "account_id": prefix,
                "strategy_id": prefix,
                "strategy_version": prefix,
                "execution_policy_version": "paper-v1",
                "cost_model_version": "cost-v1",
                "realized_net_pnl_cents": 100,
                "trade_count": 1,
                "beginning_equity_cents": 10_000,
                "ending_equity_cents": 10_000,
            }
            for index, day in enumerate(days)
        ]
    comparison = build_account_comparison(
        v5_ledger=account_rows(0.1, "v5"),
        v6_ledger=account_rows(0.2, "v6"),
        benchmark_rows=[
            {"market_date": day, "symbol": symbol, "return_close": 0.0,
             "source_bar_hash_sha256": f"{symbol}{index}".ljust(64, "0")}
            for index, day in enumerate(days)
            for symbol in ("SPY", "IWM")
        ],
        calculated_at="2026-09-11T00:00:00+00:00",
        model_lineage=model_lineage,
    )
    bound_store = _ComparisonStore(comparison)
    bound_store.model_runs = [
        {
            **model_lineage,
            "dataset_hash_sha256": model_lineage["source_lineage_hash_sha256"],
            "training_cutoff": experiment["training_cutoff"],
        }
    ]
    bound_store.experiments = [experiment]
    bound_store.holdouts = [holdout]
    assert _holdout_evidence_for_model(
        bound_store, model_run_id="bound-model"
    )["model_run_id"] == "bound-model"
    comparison_evidence = _comparison_to_v5_evidence(
        bound_store, model_run_id="bound-model"
    )
    assert comparison_evidence["status"] == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
    assert comparison_evidence["objective_delta_pct"] > 0
    bound_store.comparisons = [
        {**comparison, "model_run_id": f"newer-unrelated-model-{index}"}
        for index in range(150)
    ] + [comparison]
    assert _comparison_to_v5_evidence(
        bound_store, model_run_id="bound-model"
    )["status"] == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
    bound_store.comparisons = [comparison, {**comparison}]
    assert _comparison_to_v5_evidence(
        bound_store, model_run_id="bound-model"
    )["status"] == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
    bound_store.comparisons = [
        comparison,
        {**comparison, "comparison_id": "conflicting-retry"},
    ]
    conflicting = _comparison_to_v5_evidence(
        bound_store, model_run_id="bound-model"
    )
    assert conflicting["status"] == "COMPARISON_LINEAGE_AMBIGUOUS"
    readiness = promotion_readiness(
        [],
        evaluation={
            "model_run_id": "bound-model",
            "untouched_holdout": holdout,
            "comparison_to_v5": comparison_evidence,
        },
    )
    assert readiness["criteria"]["untouched_holdout_receipt_exactly_bound"] is True
    assert readiness["criteria"]["comparison_to_v5_receipt_exactly_bound"] is True
    assert readiness["automatic_promotion"] is False

    forged = {
        **holdout,
        "evidence": {
            **holdout["evidence"],
            "source_lineage_hash_sha256": "x" * 64,
        },
    }
    forged["evidence_hash_sha256"] = _hash(forged["evidence"])
    bound_store.holdouts = [forged]
    assert _holdout_evidence_for_model(
        bound_store, model_run_id="bound-model"
    )["status"] == "HOLDOUT_RECEIPT_MODEL_RUN_MISMATCH"
