"""Full AlphaOps V6 shadow-learning orchestration.

The service composes immutable raw outcomes into label families, datasets,
training receipts, drift evidence, and a manual promotion-review packet.  It
never alters V5, creates orders, or changes a production policy.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from intraday_scanner.alpha.fill_truth import MISSING_COMMITTED_FILL_TRUTH
from intraday_scanner.alpha.v6.calibration import calibration_report, interval_coverage
from intraday_scanner.alpha.v6.contracts import (
    canonical_hash,
    is_valid_code_sha,
    is_valid_sha256,
    utc_now,
)
from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset
from intraday_scanner.alpha.v6.decision_ledger import validate_decision_batch
from intraday_scanner.alpha.v6.drift import build_drift_report
from intraday_scanner.alpha.v6.experiment_ledger import build_trial_receipt
from intraday_scanner.alpha.v6.label_builder import build_label_families
from intraday_scanner.alpha.v6.registry import promotion_review_packet
from intraday_scanner.alpha.v6.training import (
    train_shadow_challengers,
    walk_forward_challenger_predictions,
)
from intraday_scanner.alpha.v6.validation import (
    compare_catalyst_ablations,
    evaluate_return_predictions,
    expanding_purged_splits,
)
from intraday_scanner.services.v6_learning_service import synchronize_v6_outcomes
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

_MIN_EXACT_COMMON_FOLD_PREDICTIONS = 30
_MIN_MATERIAL_OBJECTIVE_IMPROVEMENT_PCT = 0.25

MODEL_COMPETITION_CONTRACT = {
    "primary_objective": "bootstrap_lower_95_after_cost_benchmark_excess_expectancy_pct",
    "tie_breaker": "out_of_fold_rank_correlation",
    "minimum_exact_common_fold_predictions": _MIN_EXACT_COMMON_FOLD_PREDICTIONS,
    "minimum_material_objective_improvement_pct": _MIN_MATERIAL_OBJECTIVE_IMPROVEMENT_PCT,
    "required_constraints": (
        "purged_no_lookahead",
        "positive_two_x_slippage_expectancy",
        "positive_one_point_five_x_slippage_expectancy",
        "non_worsening_maximum_drawdown",
        "non_worsening_conditional_value_at_risk",
        "non_worsening_profit_factor",
        "non_worsening_turnover",
        "non_worsening_gain_loss_concentration",
        "non_worsening_capacity",
        "non_worsening_calibration_and_interval_coverage",
        "non_worsening_top_decile_lift",
        "non_worsening_rank_correlation",
        "regime_source_liquidity_catalyst_stability",
        "multiple_testing_adjusted_sharpe_not_worse_and_positive",
    ),
    "automatic_policy_change": False,
    "research_only": True,
    "broker_execution_enabled": False,
}


def run_alpha_v6_daily_monitor(
    store: SQLiteScanStore,
    *,
    market_date: str | None = None,
    reference_window: dict[str, Any] | None = None,
    recent_window: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Persist daily outcome, label, dataset, and drift evidence without refitting.

    This is the only V6 operation that belongs in the daily EOD job.  It is
    intentionally unable to train a model, write a model artifact, evaluate a
    challenger, or alter a policy.  That separation keeps today's outcomes
    from silently changing today's research score.
    """

    outcome_sync = synchronize_v6_outcomes(store)
    decisions = store.load_alpha_v6_decisions()
    outcomes = store.load_alpha_v6_outcomes()
    by_decision = {str(row.get("decision_id") or ""): row for row in decisions}
    labels = [
        label
        for outcome in outcomes
        if (decision := by_decision.get(str(outcome.get("decision_id") or ""))) is not None
        for label in build_label_families(decision=decision, outcome=outcome)
    ]
    label_stats = store.persist_alpha_v6_labels(labels) if labels else {"inserted": 0, "skipped": 0}
    persisted_labels = store.load_alpha_v6_labels()
    dataset = build_return_dataset(decisions=decisions, labels=persisted_labels)
    dataset_inserted = store.persist_alpha_v6_dataset(dataset)
    quarantined_return_count = int(
        dataset["exclusion_counts"].get("committed_fill_truth_missing", 0)
    )
    label_generation = {
        "generated_count": len(labels),
        "persistence": label_stats,
        "quarantined_return_count": quarantined_return_count,
        "quarantine_reason": (MISSING_COMMITTED_FILL_TRUTH if quarantined_return_count else None),
    }
    drift = _drift(
        decisions,
        reference_window=reference_window,
        recent_window=recent_window,
    )
    drift_inserted = store.persist_alpha_v6_drift_report(drift)
    receipt = _operational_receipt(
        receipt_kind="daily_monitor",
        market_date=market_date,
        dataset=dataset,
        outcome_count=len(outcomes),
        label_generation=label_generation,
        drift=drift,
    )
    receipt_inserted = store.persist_alpha_v6_operational_receipt(receipt)
    return {
        "schema_version": "dawnstrike.alphaops_v6.daily_monitor.v1",
        "status": "COMPLETE",
        "outcome_sync": outcome_sync,
        "decision_contract": validate_decision_batch(decisions),
        "label_generation": label_generation,
        "dataset": {**_dataset_summary(dataset), "inserted": dataset_inserted},
        "drift": {**drift, "inserted": drift_inserted},
        "operational_receipt": {**receipt, "inserted": receipt_inserted},
        "model_refit_performed": False,
        "challenger_evaluation_performed": False,
        "performance_status": "WAITING_FOR_FORWARD_EVIDENCE",
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
        "generated_at": utc_now(),
    }


def run_alpha_v6_weekly_training(
    store: SQLiteScanStore,
    *,
    code_sha: str = "unresolved-local-sha",
    market_date: str | None = None,
    reference_window: dict[str, Any] | None = None,
    recent_window: dict[str, Any] | None = None,
    experiment_id: str | None = None,
    arm_id: str | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Run the separately scheduled V6 refit and all-family OOF evaluation."""

    daily_monitor = run_alpha_v6_daily_monitor(
        store,
        market_date=market_date,
        reference_window=reference_window,
        recent_window=recent_window,
    )
    decisions = store.load_alpha_v6_decisions()
    outcomes = store.load_alpha_v6_outcomes()
    persisted_labels = store.load_alpha_v6_labels()
    dataset = build_return_dataset(decisions=decisions, labels=persisted_labels)
    dataset_inserted = store.persist_alpha_v6_dataset(dataset)
    experiment = store.load_alpha_v6_experiment(experiment_id) if experiment_id else None
    # A small/empty historical set retains the legacy NOT_TRAINED result.  Any
    # actual fit is governed by a persisted preregistered experiment and arm.
    require_preregistration = bool(dataset.get("row_count"))
    configuration_hash = (
        str(experiment.get("configuration_hash_sha256") or "")
        if isinstance(experiment, dict)
        else ""
    )
    feature_hash = (
        str(experiment.get("feature_set_hash_sha256") or "")
        if isinstance(experiment, dict) and experiment.get("feature_set_hash_sha256")
        else canonical_hash({"feature_schema_version": dataset.get("feature_schema_version")})
    )
    validation_window = experiment.get("frozen_windows") if isinstance(experiment, dict) else None
    cost_model_version = (
        str(experiment.get("cost_model_version") or "")
        if isinstance(experiment, dict) and experiment.get("cost_model_version")
        else "dawnstrike-alphaops-v6-conservative-cost-v1"
    )
    if require_preregistration and isinstance(experiment, dict):
        if not str(attempt_id or "").strip():
            raise ValueError("a retry-stable attempt_id is required for a V6 training attempt")
        trial = build_trial_receipt(
            attempt_id=str(attempt_id),
            experiment=experiment,
            arm_id=str(arm_id or "candidate"),
            strategy_id="alphaops_v6",
            strategy_version="v6",
            configuration_hash_sha256=configuration_hash,
            feature_set_hash_sha256=feature_hash,
            cost_model_version=cost_model_version,
            validation_window=validation_window if isinstance(validation_window, dict) else {},
            code_sha=code_sha,
            source_hash_sha256=str(dataset.get("dataset_hash_sha256") or ""),
        )
        trial_inserted = store.persist_alpha_v6_trial(trial)
        trial_counts = store.alpha_v6_trial_counts(experiment_id=str(experiment["experiment_id"]))
        persisted_trial = next(
            row
            for row in store.load_alpha_v6_trials(experiment_id=str(experiment["experiment_id"]))
            if row.get("trial_id") == trial.get("trial_id")
        )
        trial = persisted_trial
    else:
        trial_inserted = False
        trial_counts = {"global_attempt_count": 0, "experiment_attempt_count": None}
        trial = None
    training = train_shadow_challengers(
        dataset,
        code_sha=code_sha,
        experiment=experiment,
        arm_id=arm_id,
        configuration_hash_sha256=configuration_hash,
        feature_set_hash_sha256=feature_hash,
        cost_model_version=cost_model_version,
        validation_window=validation_window if isinstance(validation_window, dict) else None,
        require_preregistration=require_preregistration,
    )
    training["trial_counts"] = trial_counts
    training["trial_id"] = trial.get("trial_id") if isinstance(trial, dict) else None
    training["trial_inserted"] = trial_inserted
    training_inserted = store.persist_alpha_v6_model_run(training)
    artifact = training.get("artifact")
    artifact_inserted = False
    if isinstance(artifact, dict) and training.get("model_artifact_hash_sha256"):
        artifact_row = {
            "artifact_id": "v6a-"
            + canonical_hash({"model_run_id": training["model_run_id"], "artifact": artifact})[:28],
            "model_run_id": training["model_run_id"],
            "created_at": training["trained_at"],
            "artifact_hash_sha256": training["model_artifact_hash_sha256"],
            "artifact": artifact,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        artifact_inserted = store.persist_alpha_v6_model_artifact(artifact_row)
    predictions = walk_forward_challenger_predictions(
        dataset,
        model_run_id=str(training["model_run_id"]),
    )
    prediction_stats = (
        store.persist_alpha_v6_shadow_predictions(predictions)
        if predictions
        else {"inserted": 0, "skipped": 0}
    )
    folds = expanding_purged_splits(dataset["rows"])
    evaluation_rows = _evaluation_rows(predictions, outcomes, dataset)
    family_metrics = _family_evaluation_metrics(
        evaluation_rows,
        trial_count=(
            trial_counts.get("global_attempt_count")
            if isinstance(trial_counts, dict) and trial_counts.get("global_attempt_count")
            else None
        ),
        experiment_trial_count=(
            trial_counts.get("experiment_attempt_count") if isinstance(trial_counts, dict) else None
        ),
    )
    model_competition = select_research_model_family(family_metrics)
    return_metrics = dict(
        family_metrics.get("regularized_baselines", {}).get("full_oof")
        or evaluate_return_predictions([])
    )
    baseline_evaluation_rows = [
        row for row in evaluation_rows if row.get("model_family") == "regularized_baselines"
    ]
    calibration = {
        **calibration_report(baseline_evaluation_rows),
        "model_run_id": training["model_run_id"],
    }
    intervals = {
        **interval_coverage(baseline_evaluation_rows),
        "model_run_id": training["model_run_id"],
    }
    holdout_evidence = _holdout_evidence_for_model(
        store, model_run_id=str(training["model_run_id"])
    )
    comparison_to_v5 = _comparison_to_v5_evidence(store, model_run_id=str(training["model_run_id"]))
    evaluation = {
        "model_run_id": training["model_run_id"],
        "experiment_id": training.get("experiment_id"),
        "arm_id": training.get("arm_id"),
        "configuration_hash_sha256": training.get("configuration_hash_sha256"),
        "feature_set_hash_sha256": training.get("feature_set_hash_sha256"),
        "cost_model_version": training.get("cost_model_version"),
        "evaluation_window": training.get("evaluation_window"),
        "evaluated_at": utc_now(),
        "status": return_metrics["status"],
        "evaluation_method": "date_grouped_purged_embargoed_expanding_walk_forward",
        "fold_count": len(folds),
        "prediction_count": len(predictions),
        "return_metrics": return_metrics,
        "all_family_oof_comparison": family_metrics,
        "model_competition": model_competition,
        "calibration": calibration,
        "interval_coverage": intervals,
        "catalyst_ablation_plan": dataset.get("catalyst_ablation_plan"),
        "catalyst_ablation_comparison": compare_catalyst_ablations(evaluation_rows),
        "no_lookahead": bool(
            predictions and all(row.get("no_lookahead") is True for row in predictions)
        ),
        "untouched_holdout": holdout_evidence,
        "comparison_to_v5": comparison_to_v5,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    evaluation["evaluation_input_hash_sha256"] = canonical_hash(
        {key: value for key, value in evaluation.items() if key != "evaluated_at"}
    )
    evaluation["evaluation_id"] = (
        "v6e-"
        + canonical_hash(
            {
                "model_run_id": training["model_run_id"],
                "input_hash": evaluation["evaluation_input_hash_sha256"],
            }
        )[:28]
    )
    evaluation_inserted = store.persist_alpha_v6_evaluation(evaluation)
    drift = dict(daily_monitor["drift"])
    review = promotion_review_packet(
        evidence={
            "daily_monitor": daily_monitor,
            "dataset": _dataset_summary(dataset),
            "return_metrics": return_metrics,
            "calibration": calibration,
            "interval_coverage": intervals,
            "drift": drift,
            "walk_forward_fold_count": len(folds),
            "evaluation": evaluation,
        }
    )
    review_inserted = store.persist_alpha_v6_promotion_review(review)
    weekly_receipt = _operational_receipt(
        receipt_kind="weekly_training",
        market_date=market_date,
        dataset=dataset,
        outcome_count=len(outcomes),
        label_generation={"generated_count": daily_monitor["label_generation"]["generated_count"]},
        drift=drift,
        model_run_id=str(training["model_run_id"]),
        evaluation_id=str(evaluation["evaluation_id"]),
    )
    weekly_receipt_inserted = store.persist_alpha_v6_operational_receipt(weekly_receipt)
    return {
        "schema_version": "dawnstrike.alphaops_v6.weekly_training.v1",
        "status": training["status"],
        "daily_monitor": daily_monitor,
        "decision_contract": validate_decision_batch(decisions),
        "label_generation": daily_monitor["label_generation"],
        "dataset": {**_dataset_summary(dataset), "inserted": dataset_inserted},
        "training": {
            **training,
            "inserted": training_inserted,
            "artifact_inserted": artifact_inserted,
        },
        "prediction_persistence": prediction_stats,
        "validation": {
            "method": "date_grouped_purged_expanding_walk_forward",
            "fold_count": len(folds),
            "folds": folds,
            "return_metrics": return_metrics,
            "all_family_oof_comparison": family_metrics,
            "model_competition": model_competition,
            "calibration": calibration,
            "interval_coverage": intervals,
            "persisted_evaluation": {**evaluation, "inserted": evaluation_inserted},
        },
        "drift": drift,
        "operational_receipt": {
            **weekly_receipt,
            "inserted": weekly_receipt_inserted,
        },
        "model_refit_performed": True,
        "promotion_review": {**review, "inserted": review_inserted},
        "performance_status": "WAITING_FOR_FORWARD_EVIDENCE",
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
        "generated_at": utc_now(),
    }


def run_alpha_v6_learning(
    store: SQLiteScanStore,
    *,
    code_sha: str = "unresolved-local-sha",
) -> dict[str, Any]:
    """Backward-compatible alias for the weekly-only training operation."""

    result = run_alpha_v6_weekly_training(store, code_sha=code_sha)
    return {
        **result,
        "model_run": result["training"],
        "deprecated_command": "alpha-v6-learn; use alpha-v6-train-weekly",
    }


def _evaluation_rows(
    predictions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    dataset: dict[str, Any],
) -> list[dict[str, Any]]:
    outcome_by_decision = {str(row.get("decision_id") or ""): row for row in outcomes}
    dataset_by_decision = {
        str(row.get("decision_id") or ""): row for row in list(dataset.get("rows") or [])
    }
    rows: list[dict[str, Any]] = []
    for row in predictions:
        outcome = outcome_by_decision.get(str(row.get("decision_id") or ""))
        prediction = row.get("prediction")
        if outcome is None or not isinstance(prediction, dict):
            continue
        source = dataset_by_decision.get(str(row.get("decision_id") or ""), {})
        family_predictions = prediction.get("family_predictions")
        families = (
            family_predictions
            if isinstance(family_predictions, dict)
            else {"regularized_baselines": prediction}
        )
        for model_family, family_prediction in sorted(families.items()):
            if not isinstance(family_prediction, dict):
                continue
            rows.append(
                {
                    "decision_id": row.get("decision_id"),
                    "fold_id": row.get("fold_id"),
                    "model_family": model_family,
                    "market_date": row.get("market_date"),
                    "training_max_market_date": row.get("training_max_market_date"),
                    "no_lookahead": row.get("no_lookahead") is True,
                    "utility_lcb_pct": family_prediction.get("utility_lcb_pct"),
                    "realized_net_excess_return_pct": outcome.get("net_excess_return_pct"),
                    "realized_return_pct": outcome.get("net_excess_return_pct"),
                    "activation_probability": family_prediction.get("activation_probability"),
                    "interval_lower_pct": family_prediction.get("interval_lower_pct"),
                    "interval_upper_pct": family_prediction.get("interval_upper_pct"),
                    "estimated_round_trip_cost_bps": source.get("estimated_round_trip_cost_bps"),
                    "inverse_probability_weight": source.get("inverse_probability_weight"),
                    "allocation_weight": source.get(
                        "allocation_weight", outcome.get("allocation_weight")
                    ),
                    "account_weight": source.get("account_weight", outcome.get("account_weight")),
                    "portfolio_allocation_weight": source.get(
                        "portfolio_allocation_weight",
                        outcome.get("portfolio_allocation_weight"),
                    ),
                    "setup_key": source.get("setup_key"),
                    "regime_key": source.get("regime_key"),
                    "source_key": source.get("source_key"),
                    "liquidity_bucket": source.get("liquidity_bucket"),
                    "catalyst_bucket": source.get("catalyst_bucket"),
                    "catalyst_feature_block": source.get("catalyst_feature_block"),
                    "source_artifact_hash_sha256": source.get("source_artifact_hash_sha256"),
                    "source_artifact_hashes": source.get("source_artifact_hashes"),
                    "path_replay_id": source.get("path_replay_id"),
                    "benchmark_hash_sha256": source.get("benchmark_hash_sha256"),
                    "observed_cost_model_identity": source.get("observed_cost_model_identity"),
                    "modeled_cost_model_identity": source.get("modeled_cost_model_identity"),
                    "evidence_cohort": source.get("evidence_cohort"),
                    "evidence_lineage_hash_sha256": source.get("evidence_lineage_hash_sha256"),
                    "retrospective_research_eligible": source.get(
                        "retrospective_research_eligible"
                    ),
                    "prospective_promotion_eligible": source.get("prospective_promotion_eligible"),
                    "activation_label": (
                        1
                        if outcome.get("activation_status") == "ACTIVATED"
                        else 0
                        if outcome.get("activation_status") == "NOT_TRIGGERED"
                        else None
                    ),
                }
            )
    return rows


def _holdout_evidence_for_model(store: SQLiteScanStore, *, model_run_id: str) -> dict[str, Any]:
    """Project one immutable holdout receipt only when it binds this model run."""

    receipt_loader = getattr(store, "load_alpha_v6_holdout_evaluation", None)
    model_loader = getattr(store, "load_alpha_v6_model_run", None)
    model_run = (
        model_loader(model_run_id)
        if callable(model_loader)
        else next(
            (
                row
                for row in store.load_alpha_v6_model_runs(limit=100)
                if str(row.get("model_run_id") or "") == model_run_id
            ),
            {},
        )
    )
    model_experiment_id = str(model_run.get("experiment_id") or "")
    experiment_loader = getattr(store, "load_alpha_v6_experiment", None)
    experiment = (
        experiment_loader(model_experiment_id)
        if callable(experiment_loader) and model_experiment_id
        else next(
            (
                row
                for row in store.load_alpha_v6_experiments(limit=100)
                if str(row.get("experiment_id") or "") == model_experiment_id
            ),
            {},
        )
    )
    if callable(receipt_loader) and model_experiment_id:
        exact_receipt = receipt_loader(model_experiment_id)
        receipts = [exact_receipt] if exact_receipt else []
    else:
        receipts = store.load_alpha_v6_holdout_evaluations(limit=100)
    for receipt in receipts:
        if _holdout_receipt_binding_is_exact(
            receipt,
            model_run_id=model_run_id,
            model_run=model_run,
            experiment=experiment,
        ):
            evidence = receipt.get("evidence")
            evidence_data = evidence if isinstance(evidence, dict) else {}
            return {
                **receipt,
                "model_run_id": model_run_id,
                "after_cost_expectancy_pct": evidence_data.get("after_cost_expectancy_pct"),
            }
    if receipts:
        latest = receipts[0]
        return {
            "status": "HOLDOUT_RECEIPT_MODEL_RUN_MISMATCH",
            "evaluated_once": False,
            "experiment_id": latest.get("experiment_id"),
            "holdout_evaluation_id": latest.get("holdout_evaluation_id"),
            "configuration_hash_sha256": latest.get("configuration_hash_sha256"),
            "evidence_hash_sha256": latest.get("evidence_hash_sha256"),
            "evidence": latest.get("evidence"),
        }
    return {
        "status": "NOT_EVALUATED_NO_REGISTERED_FROZEN_HOLDOUT",
        "evaluated_once": False,
    }


def _holdout_receipt_binding_is_exact(
    receipt: dict[str, Any],
    *,
    model_run_id: str,
    model_run: dict[str, Any],
    experiment: dict[str, Any],
) -> bool:
    _, model_blockers = _model_experiment_lineage(model_run, experiment)
    if model_blockers:
        return False
    evidence = receipt.get("evidence")
    evidence_data = evidence if isinstance(evidence, dict) else {}
    experiment_id = str(experiment.get("experiment_id") or "")
    configuration_hash = str(experiment.get("configuration_hash_sha256") or "")
    source_hash = str(
        model_run.get("source_lineage_hash_sha256") or model_run.get("dataset_hash_sha256") or ""
    )
    code_sha = str(model_run.get("code_sha") or "")
    expected_window = {
        "training_cutoff": model_run.get("training_cutoff"),
        "validation_start": experiment.get("validation_start"),
        "untouched_holdout_start": experiment.get("untouched_holdout_start"),
    }
    if isinstance(experiment.get("frozen_windows"), dict):
        expected_window = experiment["frozen_windows"]
    persisted_window = model_run.get("evaluation_window")
    evaluation_window = persisted_window if isinstance(persisted_window, dict) else {}
    evidence_hash = str(receipt.get("evidence_hash_sha256") or "")
    expected_binding = canonical_hash(
        {
            "model_run_id": model_run_id,
            "experiment_id": experiment_id,
            "configuration_hash_sha256": configuration_hash,
            "source_lineage_hash_sha256": source_hash,
            "code_sha": code_sha,
            "evaluation_window": evaluation_window,
            "data_hash_sha256": evidence_data.get("data_hash_sha256"),
            "source_hash_sha256": evidence_data.get("source_hash_sha256"),
            "evidence_hash_sha256": evidence_hash,
        }
    )
    return bool(
        not model_blockers
        and model_run
        and experiment_id
        and configuration_hash
        and source_hash
        and code_sha
        and str(model_run.get("experiment_id") or "") == experiment_id
        and str(model_run.get("configuration_hash_sha256") or "") == configuration_hash
        and evaluation_window == expected_window
        and receipt.get("evaluated_once") is True
        and str(receipt.get("model_run_id") or "") == model_run_id
        and str(receipt.get("experiment_id") or "") == experiment_id
        and str(receipt.get("configuration_hash_sha256") or "") == configuration_hash
        and evidence_data.get("model_run_id") == model_run_id
        and evidence_data.get("experiment_id") == experiment_id
        and evidence_data.get("configuration_hash_sha256") == configuration_hash
        and evidence_data.get("source_lineage_hash_sha256") == source_hash
        and evidence_data.get("code_sha") == code_sha
        and evidence_data.get("evaluation_window") == expected_window
        and evidence_data
        and is_valid_sha256(evidence_hash)
        and evidence_hash == canonical_hash(evidence_data)
        and str(receipt.get("model_binding_hash_sha256") or "") == expected_binding
    )


def _model_experiment_lineage(
    model_run: dict[str, Any], experiment: dict[str, Any]
) -> tuple[dict[str, Any], list[str]]:
    """Validate persisted model identity/config/source/window against an experiment."""

    expected_experiment_id = str(experiment.get("experiment_id") or "")
    expected_configuration = str(experiment.get("configuration_hash_sha256") or "")
    model_experiment_id = str(model_run.get("experiment_id") or "")
    model_configuration = str(model_run.get("configuration_hash_sha256") or "")
    source_hash = str(
        model_run.get("source_lineage_hash_sha256") or model_run.get("dataset_hash_sha256") or ""
    )
    expected_window = {
        "training_cutoff": experiment.get("training_cutoff"),
        "validation_start": experiment.get("validation_start"),
        "untouched_holdout_start": experiment.get("untouched_holdout_start"),
    }
    if isinstance(experiment.get("frozen_windows"), dict):
        expected_window = experiment["frozen_windows"]
    persisted_window = model_run.get("evaluation_window")
    blockers: list[str] = []
    if not model_run:
        blockers.append("model_run_not_persisted")
    if model_experiment_id != expected_experiment_id:
        blockers.append("model_experiment_id_mismatch")
    if not is_valid_sha256(expected_configuration) or not is_valid_sha256(model_configuration):
        blockers.append("model_configuration_hash_invalid")
    elif model_configuration != expected_configuration:
        blockers.append("model_configuration_hash_mismatch")
    if not is_valid_sha256(source_hash):
        blockers.append("model_source_lineage_missing")
    if not is_valid_code_sha(model_run.get("code_sha")):
        blockers.append("model_code_sha_missing")
    if model_run.get("training_cutoff") != experiment.get("training_cutoff"):
        blockers.append("model_training_cutoff_mismatch")
    if not isinstance(persisted_window, dict) or persisted_window != expected_window:
        blockers.append("model_evaluation_window_mismatch")
    return {
        "experiment_id": expected_experiment_id,
        "configuration_hash_sha256": expected_configuration,
        "source_lineage_hash_sha256": source_hash,
        "code_sha": str(model_run.get("code_sha") or ""),
        "evaluation_window": expected_window,
    }, blockers


def _comparison_to_v5_evidence(store: SQLiteScanStore, *, model_run_id: str) -> dict[str, Any]:
    """Accept only a persisted comparison already bound to this candidate.

    Account comparison rows are authoritative evidence.  This reader may
    project their metrics, but it must not create lineage or model-binding
    hashes around an otherwise unrelated latest row.
    """

    model_loader = getattr(store, "load_alpha_v6_model_run", None)
    model_run = (
        model_loader(model_run_id)
        if callable(model_loader)
        else next(
            (
                row
                for row in store.load_alpha_v6_model_runs(limit=100)
                if str(row.get("model_run_id") or "") == model_run_id
            ),
            {},
        )
    )
    model_experiment_id = str(model_run.get("experiment_id") or "")
    comparison_loader = getattr(store, "load_account_performance_comparisons", None)
    exact_comparison_loader = getattr(
        store, "load_account_performance_comparisons_for_lineage", None
    )
    if callable(exact_comparison_loader) and model_experiment_id:
        comparisons = exact_comparison_loader(
            model_run_id=model_run_id, experiment_id=model_experiment_id
        )
    elif callable(comparison_loader):
        comparisons = comparison_loader(limit=100)
    else:
        latest = store.load_latest_account_performance_comparison()
        comparisons = [latest] if latest else []
    comparison_experiment_id = str(comparisons[0].get("experiment_id") or "") if comparisons else ""
    expected_experiment_id = model_experiment_id or comparison_experiment_id
    experiment_loader = getattr(store, "load_alpha_v6_experiment", None)
    if callable(experiment_loader) and expected_experiment_id:
        exact_experiment = experiment_loader(expected_experiment_id)
        experiments = [exact_experiment] if exact_experiment else []
    else:
        experiments = store.load_alpha_v6_experiments(limit=100)
    experiment: dict[str, Any] = next(
        (
            row
            for row in experiments
            if str(row.get("experiment_id") or "") == expected_experiment_id
        ),
        {},
    )
    matching_comparisons = [
        row
        for row in comparisons
        if str(row.get("model_run_id") or "") == model_run_id
        and str(row.get("experiment_id") or "") == expected_experiment_id
    ]
    if len(matching_comparisons) > 2:
        return {
            "status": "COMPARISON_LINEAGE_AMBIGUOUS",
            "promotion_eligible": False,
            "lineage_blockers": ["multiple_exact_comparison_receipts"],
        }
    if len(matching_comparisons) == 2:
        fingerprints = {canonical_hash(row) for row in matching_comparisons}
        if len(fingerprints) != 1:
            return {
                "status": "COMPARISON_LINEAGE_AMBIGUOUS",
                "promotion_eligible": False,
                "lineage_blockers": ["conflicting_exact_comparison_receipts"],
            }
    comparison = (
        matching_comparisons[0] if matching_comparisons else (comparisons[0] if comparisons else {})
    )
    if not comparison:
        return {
            "status": "NOT_AVAILABLE_NO_AUTHORITATIVE_ACCOUNT_COMPARISON",
            "promotion_eligible": False,
        }
    comparison_experiment_id = str(comparison.get("experiment_id") or "")
    _, model_lineage_blockers = _model_experiment_lineage(model_run, experiment)
    expected_configuration_hash = str(experiment.get("configuration_hash_sha256") or "")
    expected_code_sha = str(model_run.get("code_sha") or "")
    expected_source_hash = str(
        model_run.get("source_lineage_hash_sha256") or model_run.get("dataset_hash_sha256") or ""
    )
    expected_window = {
        "training_cutoff": model_run.get("training_cutoff"),
        "validation_start": experiment.get("validation_start"),
        "untouched_holdout_start": experiment.get("untouched_holdout_start"),
    }
    if isinstance(experiment.get("frozen_windows"), dict):
        expected_window = experiment["frozen_windows"]
    persisted_window = comparison.get("evaluation_window")
    persisted_window_data = persisted_window if isinstance(persisted_window, dict) else {}
    persisted_source_hash = str(
        comparison.get("source_lineage_hash_sha256") or comparison.get("source_hash_sha256") or ""
    )
    persisted_binding_hash = str(
        comparison.get("model_binding_hash_sha256") or comparison.get("binding_hash_sha256") or ""
    )
    comparison_id = str(comparison.get("comparison_id") or "")
    input_hash = str(comparison.get("input_hash_sha256") or "")
    metrics_value = comparison.get("series_metrics")
    metric_data: dict[str, Any] = metrics_value if isinstance(metrics_value, dict) else {}
    metrics_hash = canonical_hash(metric_data) if metric_data else ""
    persisted_metrics_hash = str(comparison.get("comparison_metrics_hash_sha256") or "")
    lineage_blockers: list[str] = []
    lineage_blockers.extend(model_lineage_blockers)
    if not expected_experiment_id:
        lineage_blockers.append("candidate_experiment_missing")
    if not model_experiment_id:
        lineage_blockers.append("candidate_model_experiment_missing")
    if (
        model_experiment_id
        and comparison_experiment_id
        and model_experiment_id != comparison_experiment_id
    ):
        lineage_blockers.append("model_experiment_id_mismatch")
    if not is_valid_sha256(expected_configuration_hash):
        lineage_blockers.append("candidate_configuration_hash_missing")
    if not is_valid_code_sha(expected_code_sha):
        lineage_blockers.append("candidate_code_sha_missing")
    if not is_valid_sha256(expected_source_hash):
        lineage_blockers.append("candidate_source_hash_missing")
    if str(comparison.get("model_run_id") or "") != model_run_id:
        lineage_blockers.append("model_run_id_mismatch")
    if str(comparison.get("experiment_id") or "") != expected_experiment_id:
        lineage_blockers.append("experiment_id_mismatch")
    if str(comparison.get("configuration_hash_sha256") or "") != expected_configuration_hash:
        lineage_blockers.append("configuration_hash_mismatch")
    if str(comparison.get("code_sha") or "") != expected_code_sha:
        lineage_blockers.append("code_sha_mismatch")
    if not is_valid_sha256(persisted_source_hash) or persisted_source_hash != expected_source_hash:
        lineage_blockers.append("source_lineage_hash_mismatch_or_missing")
    if persisted_window_data != expected_window:
        lineage_blockers.append("evaluation_window_mismatch_or_missing")
    if not comparison_id:
        lineage_blockers.append("comparison_id_missing")
    if not input_hash:
        lineage_blockers.append("comparison_input_hash_missing")
    if (
        not is_valid_sha256(input_hash)
        or not is_valid_sha256(persisted_metrics_hash)
        or not metrics_hash
        or persisted_metrics_hash != metrics_hash
    ):
        lineage_blockers.append("comparison_metrics_hash_mismatch_or_missing")
    expected_binding_hash = canonical_hash(
        {
            "model_run_id": model_run_id,
            "experiment_id": expected_experiment_id,
            "configuration_hash_sha256": expected_configuration_hash,
            "source_lineage_hash_sha256": expected_source_hash,
            "code_sha": expected_code_sha,
            "evaluation_window": expected_window,
            "comparison_id": comparison_id,
            "input_hash_sha256": input_hash,
            "comparison_metrics_hash_sha256": metrics_hash,
        }
    )
    if persisted_binding_hash != expected_binding_hash:
        lineage_blockers.append("model_binding_hash_mismatch_or_missing")
    if lineage_blockers:
        return {
            "status": "COMPARISON_LINEAGE_MISSING_OR_MISMATCHED",
            "promotion_eligible": False,
            "lineage_blockers": lineage_blockers,
        }

    v5_value = metric_data.get("v5")
    v6_value = metric_data.get("v6")
    v5: dict[str, Any] = v5_value if isinstance(v5_value, dict) else {}
    v6: dict[str, Any] = v6_value if isinstance(v6_value, dict) else {}
    v5_expectancy = _number(v5.get("expectancy_pct"))
    v6_expectancy = _number(v6.get("expectancy_pct"))
    objective_delta = (
        round(v6_expectancy - v5_expectancy, 6)
        if v5_expectancy is not None and v6_expectancy is not None
        else None
    )
    return {
        **comparison,
        "comparison_id": comparison_id,
        "input_hash_sha256": input_hash,
        "objective_delta_pct": objective_delta,
        "promotion_eligible": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _family_evaluation_metrics(
    rows: list[dict[str, Any]],
    *,
    trial_count: int | None = None,
    experiment_trial_count: int | None = None,
) -> dict[str, Any]:
    """Compare model families only on their shared out-of-fold decisions."""

    by_family: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        by_family[str(row.get("model_family") or "unknown")].append(row)
    if not by_family:
        return {
            "comparison_status": "NOT_EVALUABLE_NO_OOF_PREDICTIONS",
            "families_evaluated": [],
            "common_fold_decision_count": 0,
        }
    decision_keys = {
        family: {
            (str(row.get("decision_id") or ""), str(row.get("fold_id") or ""))
            for row in family_rows
        }
        for family, family_rows in by_family.items()
    }
    common_keys = set.intersection(*decision_keys.values())
    output: dict[str, Any] = {
        "comparison_status": (
            "EVALUABLE_EXACT_COMMON_FOLDS"
            if len(by_family) > 1 and common_keys
            else "NOT_COMPARABLE_SINGLE_OR_DISJOINT_FAMILY"
        ),
        "families_evaluated": sorted(by_family),
        "common_fold_decision_count": len(common_keys),
    }
    for family, family_rows in sorted(by_family.items()):
        exact_rows = [
            row
            for row in family_rows
            if (str(row.get("decision_id") or ""), str(row.get("fold_id") or "")) in common_keys
        ]
        output[family] = {
            "full_oof": evaluate_return_predictions(
                family_rows,
                trial_count=trial_count,
                experiment_trial_count=experiment_trial_count,
                require_durable_trial_count=True,
            ),
            "exact_common_fold_oof": evaluate_return_predictions(
                exact_rows,
                trial_count=trial_count,
                experiment_trial_count=experiment_trial_count,
                require_durable_trial_count=True,
            ),
            "exact_common_fold_calibration": calibration_report(exact_rows),
            "exact_common_fold_interval_coverage": interval_coverage(exact_rows),
            "full_oof_prediction_count": len(family_rows),
            "exact_common_fold_prediction_count": len(exact_rows),
        }
    return output


def select_research_model_family(family_metrics: dict[str, Any]) -> dict[str, Any]:
    """Apply the frozen research winner rule without altering serving policy.

    The result is a persisted evidence receipt, not a policy change: V5 remains
    frozen and V6's visible score remains the regularized baseline until the
    separate forward/holdout/manual-promotion gates pass.
    """

    baseline_name = "regularized_baselines"
    baseline = _exact_family_metrics(family_metrics, baseline_name)
    candidates = {
        family: _competition_candidate(
            family,
            _exact_family_metrics(family_metrics, family),
            baseline,
        )
        for family in sorted(family_metrics.get("families_evaluated") or [])
    }
    challenger_rows = [
        candidate
        for family, candidate in candidates.items()
        if family != baseline_name and candidate["eligible_for_research_win"] is True
    ]
    winner = max(
        challenger_rows,
        key=lambda row: (
            float(row["objective_lower_bound_pct"]),
            float(row["rank_correlation"]),
            str(row["family"]),
        ),
        default=None,
    )
    selected = str(winner["family"]) if winner is not None else baseline_name
    baseline_is_evaluable = bool(candidates.get(baseline_name, {}).get("evaluable"))
    rejected = [
        {
            "family": family,
            "reasons": list(candidate["rejection_reasons"]),
        }
        for family, candidate in candidates.items()
        if family != selected
    ]
    return {
        "schema_version": "dawnstrike.alphaops_v6.model_competition.v1",
        "comparison_status": family_metrics.get("comparison_status"),
        "contract": MODEL_COMPETITION_CONTRACT,
        "selected_research_family": selected,
        "selection_status": (
            "CHALLENGER_RESEARCH_WINNER_NOT_PROMOTED"
            if winner is not None
            else "BASELINE_RETAINED_RESEARCH_ONLY"
            if baseline_is_evaluable
            else "WAITING_FOR_FORWARD_EVIDENCE"
        ),
        "candidates": candidates,
        "rejected_alternatives": rejected,
        "automatic_policy_change": False,
        "automatic_model_serving_change": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _exact_family_metrics(family_metrics: dict[str, Any], family: str) -> dict[str, Any]:
    value = family_metrics.get(family)
    data = value if isinstance(value, dict) else {}
    exact = data.get("exact_common_fold_oof")
    if not isinstance(exact, dict):
        return {}
    output = dict(exact)
    calibration = data.get("exact_common_fold_calibration")
    interval = data.get("exact_common_fold_interval_coverage")
    output["calibration"] = calibration if isinstance(calibration, dict) else {}
    output["interval_coverage"] = interval if isinstance(interval, dict) else {}
    return output


def _competition_candidate(
    family: str,
    metrics: dict[str, Any],
    baseline: dict[str, Any],
) -> dict[str, Any]:
    interval = metrics.get("bootstrap_expectancy_95_ci_pct")
    interval_data = interval if isinstance(interval, dict) else {}
    baseline_interval = baseline.get("bootstrap_expectancy_95_ci_pct")
    baseline_interval_data = baseline_interval if isinstance(baseline_interval, dict) else {}
    objective = _number(interval_data.get("lower"))
    baseline_objective = _number(baseline_interval_data.get("lower"))
    sample_size = _number(metrics.get("sample_size"))
    reasons: list[str] = []
    if metrics.get("status") != "EVALUABLE":
        reasons.append("not_evaluable")
    if sample_size is None or sample_size < _MIN_EXACT_COMMON_FOLD_PREDICTIONS:
        reasons.append("insufficient_exact_common_fold_predictions")
    if metrics.get("no_lookahead_audit_passed") is not True:
        reasons.append("no_lookahead_not_proven")
    if objective is None:
        reasons.append("primary_objective_unavailable")
    if family != "regularized_baselines":
        if baseline_objective is None:
            reasons.append("baseline_primary_objective_unavailable")
        elif objective is not None and (
            objective < baseline_objective + _MIN_MATERIAL_OBJECTIVE_IMPROVEMENT_PCT
        ):
            reasons.append("primary_objective_not_materially_better")
        if objective is None or objective <= 0.0:
            reasons.append("primary_objective_not_positive")
        if _not_positive(_slippage_metric(metrics, "one_point_five_x_expectancy_pct")):
            reasons.append("one_point_five_x_slippage_expectancy_not_positive")
        if _not_positive(_slippage_metric(metrics, "two_x_expectancy_pct")):
            reasons.append("two_x_slippage_expectancy_not_positive")
        if _worsened_negative_metric(
            metrics,
            baseline,
            "maximum_drawdown_pct",
        ):
            reasons.append("maximum_drawdown_worsened")
        if _worsened_negative_metric(
            metrics,
            baseline,
            "conditional_value_at_risk_95_pct",
        ):
            reasons.append("conditional_value_at_risk_worsened")
        if _worsened_positive_metric(metrics, baseline, "profit_factor", higher_is_better=True):
            reasons.append("profit_factor_worsened")
        if _worsened_positive_metric(
            metrics,
            baseline,
            "turnover_observations_per_session",
        ):
            reasons.append("turnover_worsened")
        if _worsened_positive_metric(
            metrics,
            baseline,
            "gain_loss_concentration_pct",
        ):
            reasons.append("gain_loss_concentration_worsened")
        if _worsened_capacity(metrics, baseline):
            reasons.append("capacity_worsened_or_missing")
        if _worsened_calibration_or_interval(family_metrics=metrics, baseline=baseline):
            reasons.append("calibration_or_interval_coverage_worsened_or_missing")
        if _worsened_positive_metric(
            metrics, baseline, "top_decile_lift_pct", higher_is_better=True
        ):
            reasons.append("top_decile_lift_worsened")
        if _worsened_positive_metric(metrics, baseline, "rank_correlation", higher_is_better=True):
            reasons.append("rank_correlation_worsened")
        if _segmented_stability_worsened(metrics, baseline):
            reasons.append("segmented_stability_worsened_or_missing")
        if _multiple_testing_metric_worsened(metrics, baseline):
            reasons.append("multiple_testing_adjusted_sharpe_worsened_or_not_positive")
    return {
        "family": family,
        "objective_lower_bound_pct": objective,
        "baseline_objective_lower_bound_pct": baseline_objective,
        "rank_correlation": _number(metrics.get("rank_correlation")),
        "exact_common_fold_prediction_count": metrics.get("sample_size"),
        "evaluable": not {
            "not_evaluable",
            "insufficient_exact_common_fold_predictions",
            "no_lookahead_not_proven",
            "primary_objective_unavailable",
        }.intersection(reasons),
        "eligible_for_research_win": not reasons,
        "rejection_reasons": reasons,
    }


def _slippage_metric(metrics: dict[str, Any], field: str) -> Any:
    stress = metrics.get("slippage_stress")
    data = stress if isinstance(stress, dict) else {}
    return data.get(field)


def _not_positive(value: Any) -> bool:
    number = _number(value)
    return number is None or number <= 0.0


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _worsened_negative_metric(
    candidate: dict[str, Any], baseline: dict[str, Any], field: str
) -> bool:
    candidate_value = _number(candidate.get(field))
    baseline_value = _number(baseline.get(field))
    return candidate_value is None or baseline_value is None or candidate_value < baseline_value


def _worsened_positive_metric(
    candidate: dict[str, Any],
    baseline: dict[str, Any],
    field: str,
    *,
    higher_is_better: bool = False,
) -> bool:
    candidate_value = _number(candidate.get(field))
    baseline_value = _number(baseline.get(field))
    return (
        candidate_value is None
        or baseline_value is None
        or (
            candidate_value < baseline_value
            if higher_is_better
            else candidate_value > baseline_value
        )
    )


def _worsened_capacity(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    candidate_capacity = candidate.get("capacity")
    baseline_capacity = baseline.get("capacity")
    candidate_data = candidate_capacity if isinstance(candidate_capacity, dict) else {}
    baseline_data = baseline_capacity if isinstance(baseline_capacity, dict) else {}
    candidate_value = _number(candidate_data.get("median_capacity_dollars"))
    baseline_value = _number(baseline_data.get("median_capacity_dollars"))
    return (
        candidate_data.get("status") != "EVALUABLE"
        or baseline_data.get("status") != "EVALUABLE"
        or candidate_value is None
        or baseline_value is None
        or candidate_value < baseline_value
    )


def _worsened_calibration_or_interval(
    *, family_metrics: dict[str, Any], baseline: dict[str, Any]
) -> bool:
    candidate_calibration = family_metrics.get("calibration")
    baseline_calibration = baseline.get("calibration")
    candidate_interval = family_metrics.get("interval_coverage")
    baseline_interval = baseline.get("interval_coverage")
    candidate_calibration_data = (
        candidate_calibration if isinstance(candidate_calibration, dict) else {}
    )
    baseline_calibration_data = (
        baseline_calibration if isinstance(baseline_calibration, dict) else {}
    )
    candidate_interval_data = candidate_interval if isinstance(candidate_interval, dict) else {}
    baseline_interval_data = baseline_interval if isinstance(baseline_interval, dict) else {}
    candidate_brier = _number(candidate_calibration_data.get("brier_score"))
    baseline_brier = _number(baseline_calibration_data.get("brier_score"))
    candidate_ece = _number(candidate_calibration_data.get("expected_calibration_error"))
    baseline_ece = _number(baseline_calibration_data.get("expected_calibration_error"))
    candidate_coverage = _number(candidate_interval_data.get("coverage_pct"))
    baseline_coverage = _number(baseline_interval_data.get("coverage_pct"))
    return (
        candidate_calibration_data.get("status") != "EVALUABLE"
        or baseline_calibration_data.get("status") != "EVALUABLE"
        or candidate_interval_data.get("status") != "EVALUABLE"
        or baseline_interval_data.get("status") != "EVALUABLE"
        or candidate_brier is None
        or baseline_brier is None
        or candidate_ece is None
        or baseline_ece is None
        or candidate_coverage is None
        or baseline_coverage is None
        or candidate_brier > baseline_brier
        or candidate_ece > baseline_ece
        or abs(candidate_coverage - 90.0) > abs(baseline_coverage - 90.0)
    )


def _segmented_stability_worsened(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    candidate_segments = candidate.get("segmented_performance")
    baseline_segments = baseline.get("segmented_performance")
    candidate_data = candidate_segments if isinstance(candidate_segments, dict) else {}
    baseline_data = baseline_segments if isinstance(baseline_segments, dict) else {}
    required = ("regime_key", "source_key", "liquidity_bucket", "catalyst_bucket")
    for dimension in required:
        candidate_rows = candidate_data.get(dimension)
        baseline_rows = baseline_data.get(dimension)
        if not isinstance(candidate_rows, list) or not isinstance(baseline_rows, list):
            return True
        candidate_values = {
            str(row.get("segment")): _number(row.get("after_cost_expectancy_pct"))
            for row in candidate_rows
            if isinstance(row, dict)
        }
        baseline_values = {
            str(row.get("segment")): _number(row.get("after_cost_expectancy_pct"))
            for row in baseline_rows
            if isinstance(row, dict)
        }
        if not candidate_values or set(candidate_values) != set(baseline_values):
            return True
        for key in sorted(baseline_values):
            candidate_value = candidate_values[key]
            baseline_value = baseline_values[key]
            if (
                candidate_value is None
                or baseline_value is None
                or candidate_value < baseline_value
            ):
                return True
    return False


def _multiple_testing_metric_worsened(candidate: dict[str, Any], baseline: dict[str, Any]) -> bool:
    candidate_value = _number(candidate.get("multiple_testing_adjusted_sharpe"))
    baseline_value = _number(baseline.get("multiple_testing_adjusted_sharpe"))
    return (
        candidate_value is None
        or baseline_value is None
        or candidate_value <= 0.0
        or candidate_value < baseline_value
    )


def _drift(
    decisions: list[dict[str, Any]],
    *,
    reference_window: dict[str, Any] | None,
    recent_window: dict[str, Any] | None,
) -> dict[str, Any]:
    """Build drift from caller-frozen, non-overlapping date windows.

    The caller owns the frozen market-date cohorts. This monitor never selects
    a midpoint or a latest-percentile slice from the observed rows.
    """
    if reference_window is None or recent_window is None:
        return build_drift_report(
            baseline_rows=[],
            current_rows=[],
            config={"window_policy": "caller_frozen_disjoint_exact"},
            minimum_observations=20,
            minimum_market_sessions=5,
        )
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        by_date[str(decision.get("market_date") or "")].append(decision)
    reference_dates = [str(value) for value in reference_window.get("market_dates") or []]
    recent_dates = [str(value) for value in recent_window.get("market_dates") or []]
    if not reference_dates or not recent_dates:
        return build_drift_report(
            baseline_rows=[],
            current_rows=[],
            config={"window_policy": "caller_frozen_disjoint_exact"},
            minimum_observations=20,
            minimum_market_sessions=5,
        )
    baseline = [row for market_date in reference_dates for row in by_date[market_date]]
    current = [row for market_date in recent_dates for row in by_date[market_date]]
    config = {
        "reference_sessions": len(reference_dates),
        "recent_sessions": len(recent_dates),
        "dimensions": "v6-drift-v2",
        "warning_threshold": 0.10,
        "quarantine_threshold": 0.25,
        "window_policy": "caller_frozen_disjoint_exact",
    }
    source_values = sorted(
        {
            str(row.get("source_lineage_hash_sha256") or row.get("source") or "")
            for row in [*baseline, *current]
        }
    )
    source_values = [value for value in source_values if value]
    if not source_values:
        raise ValueError("V6 drift requires exact source lineage across observations")
    code_values = {
        str(row.get("code_sha") or "") for row in [*baseline, *current] if row.get("code_sha")
    }
    if len(code_values) != 1:
        raise ValueError("V6 drift requires one exact code SHA across observations")
    window = {
        "reference": {
            "start": reference_dates[0],
            "end": reference_dates[-1],
            "market_dates": reference_dates,
        },
        "recent": {
            "start": recent_dates[0],
            "end": recent_dates[-1],
            "market_dates": recent_dates,
        },
    }
    return build_drift_report(
        baseline_rows=baseline,
        current_rows=current,
        reference_window=reference_window,
        recent_window=recent_window,
        config=config,
        source={"lineage_hashes": source_values},
        config_hash_sha256=canonical_hash(config),
        source_hash_sha256=canonical_hash({"lineage_hashes": source_values}),
        window_hash_sha256=canonical_hash(window),
        input_hash_sha256=canonical_hash(
            {
                "reference": sorted(baseline, key=canonical_hash),
                "recent": sorted(current, key=canonical_hash),
            }
        ),
        code_sha=next(iter(code_values)),
        minimum_observations=20,
        minimum_market_sessions=5,
    )


def _dataset_summary(dataset: dict[str, Any]) -> dict[str, Any]:
    return {
        key: dataset.get(key)
        for key in (
            "dataset_id",
            "dataset_hash_sha256",
            "training_cutoff",
            "row_count",
            "activation_row_count",
            "exclusion_counts",
            "feature_schema_version",
            "eligibility_counts",
            "catalyst_ablation_plan",
        )
    }


def _operational_receipt(
    *,
    receipt_kind: str,
    market_date: str | None,
    dataset: dict[str, Any],
    outcome_count: int,
    label_generation: dict[str, Any],
    drift: dict[str, Any],
    model_run_id: str | None = None,
    evaluation_id: str | None = None,
) -> dict[str, Any]:
    as_of_date = str(market_date or utc_now())[:10]
    content = {
        "receipt_kind": receipt_kind,
        "as_of_date": as_of_date,
        "dataset_id": dataset.get("dataset_id"),
        "dataset_hash_sha256": dataset.get("dataset_hash_sha256"),
        "outcome_count": outcome_count,
        "label_generation": label_generation,
        "drift_report_id": drift.get("drift_report_id"),
        "model_run_id": model_run_id,
        "evaluation_id": evaluation_id,
    }
    input_hash = canonical_hash(content)
    receipt = {
        **content,
        "receipt_id": "v6op-" + input_hash[:28],
        "created_at": utc_now(),
        "status": "COMPLETE",
        "input_hash_sha256": input_hash,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt["receipt_hash_sha256"] = canonical_hash(
        {
            key: value
            for key, value in receipt.items()
            if key not in {"receipt_hash_sha256", "created_at"}
        }
    )
    return receipt


__all__ = [
    "MODEL_COMPETITION_CONTRACT",
    "run_alpha_v6_daily_monitor",
    "run_alpha_v6_learning",
    "run_alpha_v6_weekly_training",
    "select_research_model_family",
]
