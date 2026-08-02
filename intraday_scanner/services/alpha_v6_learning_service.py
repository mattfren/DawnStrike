"""Full AlphaOps V6 shadow-learning orchestration.

The service composes immutable raw outcomes into label families, datasets,
training receipts, drift evidence, and a manual promotion-review packet.  It
never alters V5, creates orders, or changes a production policy.
"""

from __future__ import annotations

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
            "artifact_id": "v6a-" + canonical_hash(
                {"model_run_id": training["model_run_id"], "artifact": artifact}
            )[:28],
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
    evaluation["evaluation_id"] = "v6e-" + canonical_hash(
        {
            "model_run_id": training["model_run_id"],
            "input_hash": evaluation["evaluation_input_hash_sha256"],
        }
    )[:28]
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
        label_generation={
            "generated_count": daily_monitor["label_generation"]["generated_count"]
        },
        drift=drift,
        model_run_id=str(training["model_run_id"]),
        evaluation_id=str(evaluation["evaluation_id"]),
    )
    weekly_receipt_inserted = store.persist_alpha_v6_operational_receipt(
        weekly_receipt
    )
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
                    "estimated_round_trip_cost_bps": source.get(
                        "estimated_round_trip_cost_bps"
                    ),
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
            if (str(row.get("decision_id") or ""), str(row.get("fold_id") or ""))
            in common_keys
        ]
        output[family] = {
            "full_oof": evaluate_return_predictions(family_rows),
            "exact_common_fold_oof": evaluate_return_predictions(exact_rows),
            "full_oof_prediction_count": len(family_rows),
            "exact_common_fold_prediction_count": len(exact_rows),
        }
    return output


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
    "run_alpha_v6_daily_monitor",
    "run_alpha_v6_learning",
    "run_alpha_v6_weekly_training",
]
