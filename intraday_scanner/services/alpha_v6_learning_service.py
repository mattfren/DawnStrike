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
from intraday_scanner.alpha.v6.training import train_shadow_challengers
from intraday_scanner.alpha.v6.validation import (
    evaluate_return_predictions,
    expanding_purged_splits,
)
from intraday_scanner.alpha.v6_shadow import V6EmpiricalShadowModel
from intraday_scanner.services.v6_learning_service import synchronize_v6_learning
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def run_alpha_v6_learning(
    store: SQLiteScanStore,
    *,
    code_sha: str = "unresolved-local-sha",
) -> dict[str, Any]:
    """Run the V6 daily learning chain idempotently against durable ledgers."""

    raw_learning = synchronize_v6_learning(store)
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
    predictions = _shadow_predictions(
        decisions=decisions,
        outcomes=outcomes,
        model_run_id=str(training["model_run_id"]),
    )
    prediction_stats = (
        store.persist_alpha_v6_shadow_predictions(predictions)
        if predictions
        else {"inserted": 0, "skipped": 0}
    )
    folds = expanding_purged_splits(dataset["rows"])
    evaluation_rows = _evaluation_rows(predictions, outcomes)
    return_metrics = evaluate_return_predictions(evaluation_rows)
    calibration = calibration_report(evaluation_rows)
    intervals = interval_coverage(evaluation_rows)
    drift = _drift(decisions)
    drift_inserted = store.persist_alpha_v6_drift_report(drift)
    review = promotion_review_packet(
        evidence={
            "raw_learning": raw_learning,
            "dataset": _dataset_summary(dataset),
            "return_metrics": return_metrics,
            "calibration": calibration,
            "interval_coverage": intervals,
            "drift": drift,
            "walk_forward_fold_count": len(folds),
        }
    )
    review_inserted = store.persist_alpha_v6_promotion_review(review)
    return {
        "schema_version": "dawnstrike.alphaops_v6.full_learning_run.v1",
        "status": training["status"],
        "raw_learning": raw_learning,
        "decision_contract": validate_decision_batch(decisions),
        "label_generation": {"generated_count": len(labels), "persistence": label_stats},
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
            "calibration": calibration,
            "interval_coverage": intervals,
        },
        "drift": {**drift, "inserted": drift_inserted},
        "promotion_review": {**review, "inserted": review_inserted},
        "performance_status": "WAITING_FOR_FORWARD_EVIDENCE",
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
        "generated_at": utc_now(),
    }


def _shadow_predictions(
    *,
    decisions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
    model_run_id: str,
) -> list[dict[str, Any]]:
    model = V6EmpiricalShadowModel(outcomes)
    generated_at = utc_now()
    rows: list[dict[str, Any]] = []
    for decision in decisions:
        prediction = model.predict(decision).to_dict()
        row = {
            "decision_id": decision.get("decision_id"),
            "model_run_id": model_run_id,
            "market_date": decision.get("market_date"),
            "generated_at": generated_at,
            "status": prediction["status"],
            "prediction": prediction,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        row["prediction_id"] = "v6p-" + canonical_hash(
            {"decision_id": row["decision_id"], "model_run_id": model_run_id}
        )[:28]
        rows.append(row)
    return rows


def _evaluation_rows(
    predictions: list[dict[str, Any]], outcomes: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    outcome_by_decision = {str(row.get("decision_id") or ""): row for row in outcomes}
    rows: list[dict[str, Any]] = []
    for row in predictions:
        outcome = outcome_by_decision.get(str(row.get("decision_id") or ""))
        prediction = row.get("prediction")
        if outcome is None or not isinstance(prediction, dict):
            continue
        rows.append(
            {
                "utility_lcb_pct": prediction.get("utility_lcb_pct"),
                "realized_net_excess_return_pct": outcome.get("net_excess_return_pct"),
                "activation_probability": prediction.get("activation_probability"),
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
            "exclusion_counts",
            "feature_schema_version",
        )
    }


__all__ = ["run_alpha_v6_learning"]
