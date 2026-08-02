"""Full AlphaOps V6 shadow-learning orchestration.

The service composes immutable raw outcomes into label families, datasets,
training receipts, drift evidence, and a manual promotion-review packet.  It
never alters V5, creates orders, or changes a production policy.
"""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from intraday_scanner.alpha.v6.calibration import calibration_report, interval_coverage
from intraday_scanner.alpha.v6.contracts import canonical_hash, utc_now
from intraday_scanner.alpha.v6.dataset_builder import build_return_dataset
from intraday_scanner.alpha.v6.decision_ledger import validate_decision_batch
from intraday_scanner.alpha.v6.drift import build_drift_report
from intraday_scanner.alpha.v6.label_builder import build_label_families
from intraday_scanner.alpha.v6.registry import promotion_review_packet
from intraday_scanner.alpha.v6.training import (
    train_shadow_challengers,
    walk_forward_challenger_predictions,
)
from intraday_scanner.alpha.v6.validation import (
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
    store: SQLiteScanStore, *, market_date: str | None = None
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
    drift = _drift(decisions)
    drift_inserted = store.persist_alpha_v6_drift_report(drift)
    receipt = _operational_receipt(
        receipt_kind="daily_monitor",
        market_date=market_date,
        dataset=dataset,
        outcome_count=len(outcomes),
        label_generation={"generated_count": len(labels)},
        drift=drift,
    )
    receipt_inserted = store.persist_alpha_v6_operational_receipt(receipt)
    return {
        "schema_version": "dawnstrike.alphaops_v6.daily_monitor.v1",
        "status": "COMPLETE",
        "outcome_sync": outcome_sync,
        "decision_contract": validate_decision_batch(decisions),
        "label_generation": {"generated_count": len(labels), "persistence": label_stats},
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
) -> dict[str, Any]:
    """Run the separately scheduled V6 refit and all-family OOF evaluation."""

    daily_monitor = run_alpha_v6_daily_monitor(store, market_date=market_date)
    decisions = store.load_alpha_v6_decisions()
    outcomes = store.load_alpha_v6_outcomes()
    persisted_labels = store.load_alpha_v6_labels()
    dataset = build_return_dataset(decisions=decisions, labels=persisted_labels)
    dataset_inserted = store.persist_alpha_v6_dataset(dataset)
    training = train_shadow_challengers(dataset, code_sha=code_sha)
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
    family_metrics = _family_evaluation_metrics(evaluation_rows)
    model_competition = select_research_model_family(family_metrics)
    return_metrics = dict(
        family_metrics.get("regularized_baselines", {}).get("full_oof")
        or evaluate_return_predictions([])
    )
    baseline_evaluation_rows = [
        row for row in evaluation_rows if row.get("model_family") == "regularized_baselines"
    ]
    calibration = calibration_report(baseline_evaluation_rows)
    intervals = interval_coverage(baseline_evaluation_rows)
    evaluation = {
        "model_run_id": training["model_run_id"],
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
        "no_lookahead": bool(
            predictions and all(row.get("no_lookahead") is True for row in predictions)
        ),
        "untouched_holdout": {
            "status": "NOT_EVALUATED_NO_REGISTERED_FROZEN_HOLDOUT",
            "evaluated_once": False,
        },
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
                    "setup_key": source.get("setup_key"),
                    "regime_key": source.get("regime_key"),
                    "source_key": source.get("source_key"),
                    "liquidity_bucket": source.get("liquidity_bucket"),
                    "catalyst_bucket": source.get("catalyst_bucket"),
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


def _family_evaluation_metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
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
            "full_oof": evaluate_return_predictions(family_rows),
            "exact_common_fold_oof": evaluate_return_predictions(exact_rows),
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


def _drift(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    by_date: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for decision in decisions:
        by_date[str(decision.get("market_date") or "")[:10]].append(decision)
    dates = sorted(date for date in by_date if date)
    midpoint = len(dates) // 2
    baseline = [row for date in dates[:midpoint] for row in by_date[date]]
    current = [row for date in dates[midpoint:] for row in by_date[date]]
    return build_drift_report(baseline_rows=baseline, current_rows=current)


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
    return {
        **content,
        "receipt_id": "v6op-" + input_hash[:28],
        "created_at": utc_now(),
        "status": "COMPLETE",
        "input_hash_sha256": input_hash,
        "research_only": True,
        "broker_execution_enabled": False,
    }


__all__ = [
    "MODEL_COMPETITION_CONTRACT",
    "run_alpha_v6_daily_monitor",
    "run_alpha_v6_learning",
    "run_alpha_v6_weekly_training",
    "select_research_model_family",
]
