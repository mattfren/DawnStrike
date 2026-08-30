"""V6 shadow-ledger orchestration; deterministic and research-only."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
    CURRENT_CENSORED_PATH,
    CURRENT_RETURN_TRUTH,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.fill_truth import has_authenticated_committed_fill_truth
from intraday_scanner.alpha.v6_shadow import (
    ALPHAOPS_V6_MODEL_VERSION,
    build_v6_outcomes,
    promotion_readiness,
    strict_walk_forward_evaluation,
)
from intraday_scanner.performance.account_comparison import public_account_comparison
from intraday_scanner.services.outcome_capture_contract import classify_missing_capture
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def synchronize_v6_outcomes(store: SQLiteScanStore) -> dict[str, Any]:
    """Append sourced V6 outcome receipts without fitting or scoring a model."""

    all_decisions = store.load_alpha_v6_decisions()
    decisions = [
        row
        for row in all_decisions
        if row.get("action") == "SHADOW_TRACK"
        or (
            row.get("action") == "SHADOW_REJECTED_POLICY"
            and isinstance(row.get("rejected_sampling"), dict)
            and row["rejected_sampling"].get("included") is True
        )
    ]
    existing_rows = store.load_alpha_v6_outcomes()
    decision_by_id = {str(row.get("decision_id") or ""): row for row in decisions}
    existing = {str(row.get("decision_id") or "") for row in existing_rows}
    blocked_legacy = sum(
        1
        for row in existing_rows
        if (
            (decision := decision_by_id.get(str(row.get("decision_id") or ""))) is not None
            and classify_canonical_return_truth(row, decision=decision)
            not in {
                CURRENT_RETURN_TRUTH,
                CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
                CURRENT_CENSORED_PATH,
            }
        )
    )
    pending = [row for row in decisions if str(row.get("decision_id") or "") not in existing]
    all_sources = store.load_signal_outcomes(limit=50_000)
    sources_by_signal: dict[str, list[dict[str, Any]]] = {}
    for source in all_sources:
        sources_by_signal.setdefault(str(source.get("signal_id") or ""), []).append(source)
    safe_sources: list[dict[str, Any]] = []
    blocked_current_source_conflicts = 0
    blocked_decision_ids: set[str] = set()
    for decision in pending:
        candidates = sources_by_signal.get(str(decision.get("shadow_signal_id") or ""), [])
        current = [
            row
            for row in candidates
            if classify_canonical_return_truth(row, decision=decision)
            in {
                CURRENT_RETURN_TRUTH,
                CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
                CURRENT_CENSORED_PATH,
            }
        ]
        signatures = {
            _hash(
                {
                    "return_truth_hash_sha256": row.get("return_truth_hash_sha256"),
                    "replay_receipt_hash_sha256": row.get("replay_receipt_hash_sha256"),
                    "path_replay_id": row.get("path_replay_id"),
                    "outcome_status": row.get("outcome_status"),
                }
            )
            for row in current
        }
        if len(signatures) > 1:
            blocked_current_source_conflicts += 1
            blocked_decision_ids.add(str(decision.get("decision_id") or ""))
            continue
        if current:
            safe_sources.append(current[0])
    generatable = [
        row for row in pending if str(row.get("decision_id") or "") not in blocked_decision_ids
    ]
    generated = build_v6_outcomes(
        decisions=generatable,
        sourced_outcomes=safe_sources,
        capture_attempts=store.load_outcome_capture_attempts(limit=50_000),
    )
    outcome_stats = (
        store.persist_alpha_v6_outcomes(generated)
        if generated
        else {
            "inserted": 0,
            "skipped": 0,
        }
    )
    outcomes = store.load_alpha_v6_outcomes()
    return {
        "schema_version": "dawnstrike.alphaops_v6.outcome_sync.v1",
        "status": "COMPLETE",
        "decision_count": len(decisions),
        "pending_outcome_count": len(pending),
        "blocked_legacy_outcome_count": blocked_legacy,
        "blocked_current_source_conflict_count": blocked_current_source_conflicts,
        "outcome_revision_required": blocked_legacy > 0,
        "outcome_generation": outcome_stats,
        "outcome_count": len(outcomes),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def synchronize_v6_learning(
    store: SQLiteScanStore, *, persist_baseline_evaluation: bool = True
) -> dict[str, Any]:
    """Compatibility evaluator that appends outcomes and records baseline evidence.

    New daily operations must use :func:`synchronize_v6_outcomes`; this legacy
    function remains for backwards compatibility with existing audit tooling.
    """

    outcome_sync = synchronize_v6_outcomes(store)
    all_decisions = store.load_alpha_v6_decisions()
    decisions = [
        row
        for row in all_decisions
        if row.get("action") == "SHADOW_TRACK"
        or (
            row.get("action") == "SHADOW_REJECTED_POLICY"
            and isinstance(row.get("rejected_sampling"), dict)
            and row["rejected_sampling"].get("included") is True
        )
    ]
    outcomes = store.load_alpha_v6_outcomes()
    evaluation = strict_walk_forward_evaluation(decisions=all_decisions, outcomes=outcomes)
    training_hash = _hash({"decisions": decisions, "outcomes": outcomes})
    now = _utc_now()
    model_run_id = "v6m-" + training_hash[:28]
    model_run = {
        "model_run_id": model_run_id,
        "model_version": ALPHAOPS_V6_MODEL_VERSION,
        "trained_at": now,
        "training_cutoff": max(
            (str(row.get("market_date") or "")[:10] for row in outcomes),
            default=None,
        ),
        "status": evaluation["status"],
        "training_input_hash_sha256": training_hash,
        "outcome_count": len(outcomes),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    inserted_model = (
        store.persist_alpha_v6_model_run(model_run) if persist_baseline_evaluation else False
    )
    evaluation_payload = {
        "evaluation_id": "v6e-"
        + _hash({"model_run_id": model_run_id, "evaluation": evaluation})[:28],
        "model_run_id": model_run_id,
        "evaluated_at": now,
        "status": evaluation["status"],
        "evaluation_input_hash_sha256": _hash(evaluation),
        **evaluation,
    }
    inserted_evaluation = (
        store.persist_alpha_v6_evaluation(evaluation_payload)
        if persist_baseline_evaluation
        else False
    )
    return {
        "schema_version": "dawnstrike.alphaops_v6.learning_run.v1",
        "status": evaluation["status"],
        "decision_count": len(decisions),
        "pending_outcome_count": outcome_sync["pending_outcome_count"],
        "outcome_generation": outcome_sync["outcome_generation"],
        "model_run": {**model_run, "inserted": inserted_model},
        "evaluation": {**evaluation, "inserted": inserted_evaluation},
        "promotion_readiness": promotion_readiness(
            outcomes,
            decisions=all_decisions,
            evaluation=evaluation_payload,
        ),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def v6_public_status(store: SQLiteScanStore) -> dict[str, Any]:
    """Read-only public projection for the Research surface."""

    decisions = store.load_alpha_v6_decisions()
    outcomes = store.load_alpha_v6_outcomes()
    model_runs = store.load_alpha_v6_model_runs(limit=1)
    evaluations = store.load_alpha_v6_evaluations(limit=1)
    drift_reports = store.load_alpha_v6_drift_reports(limit=1)
    reviews = store.load_alpha_v6_promotion_reviews(limit=1)
    daily_receipts = store.load_alpha_v6_operational_receipts(receipt_kind="daily_monitor", limit=1)
    weekly_receipts = store.load_alpha_v6_operational_receipts(
        receipt_kind="weekly_training", limit=1
    )
    latest_evaluation = evaluations[0] if evaluations else None
    failure_attribution = build_v6_failure_attribution(store, persist=False)
    account_comparison = store.load_latest_account_performance_comparison()
    evidence_gate = _public_prediction_evidence_gate(latest_evaluation)
    return {
        "schema_version": "dawnstrike.alphaops_v6.public_status.v1",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "decision_count": len(decisions),
        "tracked_count": sum(1 for row in decisions if row.get("action") == "SHADOW_TRACK"),
        "outcome_count": len(outcomes),
        "learning_eligible_outcome_count": sum(
            1
            for row in outcomes
            if row.get("learning_eligible") is True and has_authenticated_committed_fill_truth(row)
        ),
        "latest_model_run": _public_model_run(model_runs[0]) if model_runs else None,
        "latest_evaluation": (_public_evaluation(latest_evaluation) if latest_evaluation else None),
        "latest_drift": drift_reports[0] if drift_reports else None,
        "operational_freshness": {
            "latest_daily_monitor": _public_operational_receipt(
                daily_receipts[0] if daily_receipts else None
            ),
            "latest_weekly_training": _public_operational_receipt(
                weekly_receipts[0] if weekly_receipts else None
            ),
        },
        "latest_promotion_review": (
            {
                "review_id": reviews[0].get("review_id"),
                "created_at": reviews[0].get("created_at"),
                "status": reviews[0].get("status"),
                "approved": reviews[0].get("approved") is True,
            }
            if reviews
            else None
        ),
        "prediction_evidence_gate": evidence_gate,
        "failure_attribution": _public_failure_attribution(failure_attribution),
        "account_comparison": public_account_comparison(account_comparison),
        "decision_replay": [
            _public_decision(row, prediction_visible=evidence_gate["passed"])
            for row in reversed(decisions[-50:])
        ],
        "promotion_readiness": promotion_readiness(
            outcomes,
            decisions=decisions,
            evaluation=latest_evaluation,
        ),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _public_prediction_evidence_gate(
    evaluation: dict[str, Any] | None,
) -> dict[str, Any]:
    data = evaluation or {}
    calibration = data.get("calibration")
    intervals = data.get("interval_coverage")
    calibration_data = calibration if isinstance(calibration, dict) else {}
    interval_data = intervals if isinstance(intervals, dict) else {}
    model_run_id = str(data.get("model_run_id") or "")
    checks = {
        "purged_evaluation_evaluable": data.get("status") == "EVALUABLE",
        "no_lookahead": data.get("no_lookahead") is True,
        "activation_calibration_evaluable": bool(
            calibration_data.get("status") == "EVALUABLE"
            and calibration_data.get("display_eligible") is True
        ),
        "prediction_intervals_evaluable": bool(
            interval_data.get("status") == "EVALUABLE"
            and interval_data.get("display_eligible") is True
        ),
        "calibration_model_run_exact": bool(
            model_run_id and calibration_data.get("model_run_id") == model_run_id
        ),
        "interval_model_run_exact": bool(
            model_run_id and interval_data.get("model_run_id") == model_run_id
        ),
    }
    return {
        "passed": all(checks.values()),
        "checks": checks,
        "reason": (
            "Evidence gate passed for research display."
            if all(checks.values())
            else "Probability and expected-return values are hidden until every gate passes."
        ),
    }


def _public_model_run(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "model_run_id",
            "model_version",
            "trained_at",
            "training_cutoff",
            "status",
            "dataset_id",
            "dataset_hash_sha256",
            "feature_schema_version",
            "code_sha",
            "model_artifact_hash_sha256",
        )
    }


def _public_evaluation(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: row.get(key)
        for key in (
            "evaluation_id",
            "model_run_id",
            "evaluated_at",
            "status",
            "evaluation_method",
            "fold_count",
            "prediction_count",
            "return_metrics",
            "calibration",
            "interval_coverage",
            "no_lookahead",
            "untouched_holdout",
        )
    }


def _public_failure_attribution(report: dict[str, Any]) -> dict[str, Any]:
    """Publish aggregates only; source values, raw outcomes, and experiment IDs stay private."""

    causal = report.get("causal_attribution")
    causal_data = causal if isinstance(causal, dict) else {}
    categories = (
        "by_setup_regime",
        "by_source_quality",
        "by_liquidity",
        "by_catalyst",
        "by_volatility",
    )
    return {
        "status": report.get("status"),
        "categories": {
            category: [
                {
                    key: row.get(key)
                    for key in (
                        "group",
                        "outcome_count",
                        "eligible_return_count",
                        "terminal_missing_count",
                        "authoritative_terminal_count",
                        "recoverable_missing_count",
                        "activation_count",
                        "not_triggered_count",
                        "mean_net_excess_return_pct",
                        "worst_net_excess_return_pct",
                        "missing_truth_is_zero",
                    )
                }
                for row in causal_data.get(category, [])
                if isinstance(row, dict)
            ]
            for category in categories
        },
        "failure_modes": causal_data.get("failure_modes")
        if isinstance(causal_data.get("failure_modes"), dict)
        else {},
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _public_operational_receipt(
    row: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if row is None:
        return None
    return {
        key: row.get(key)
        for key in (
            "receipt_id",
            "receipt_kind",
            "as_of_date",
            "created_at",
            "status",
            "dataset_id",
            "dataset_hash_sha256",
            "outcome_count",
            "drift_report_id",
            "model_run_id",
            "evaluation_id",
        )
    }


def _public_decision(row: dict[str, Any], *, prediction_visible: bool) -> dict[str, Any]:
    prediction = row.get("prediction")
    prediction_data = prediction if isinstance(prediction, dict) else {}
    source_summary = row.get("source_summary")
    source_data = source_summary if isinstance(source_summary, dict) else {}
    return {
        "decision_id": row.get("decision_id"),
        "market_date": row.get("market_date"),
        "decision_at": row.get("decision_at"),
        "ticker": row.get("ticker"),
        "decision_state": row.get("decision_state"),
        "action": row.get("action"),
        "setup_key": row.get("setup_key"),
        "regime_key": row.get("regime_key"),
        "reasons": list(
            row.get("safety_vetoes")
            or row.get("no_trade_reasons")
            or ([row.get("policy_rejection_reason")] if row.get("policy_rejection_reason") else [])
        ),
        "model_version": row.get("model_version"),
        "feature_schema_version": row.get("feature_schema_version"),
        "feature_hash_sha256": row.get("feature_hash_sha256"),
        "source_lineage_hash_sha256": row.get("source_lineage_hash_sha256"),
        "source_status": source_data.get("status"),
        "prediction_status": prediction_data.get("status"),
        "prediction_visible": prediction_visible,
        "activation_probability": (
            prediction_data.get("activation_probability") if prediction_visible else None
        ),
        "conditional_net_excess_return_pct": (
            prediction_data.get("conditional_net_excess_return_pct") if prediction_visible else None
        ),
        "utility_lcb_pct": (prediction_data.get("utility_lcb_pct") if prediction_visible else None),
        "sample_size": prediction_data.get("sample_size"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def build_v6_failure_attribution(store: SQLiteScanStore, *, persist: bool = True) -> dict[str, Any]:
    """Explain V6 outcomes by setup/regime without silently changing policy."""

    decisions = store.load_alpha_v6_decisions()
    outcomes = store.load_alpha_v6_outcomes()
    by_id = {str(row.get("decision_id") or ""): row for row in decisions}
    records: list[dict[str, Any]] = []
    groups: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        decision = by_id.get(str(outcome.get("decision_id") or ""))
        if decision is None:
            continue
        record = {**outcome, "decision": decision}
        records.append(record)
        key = "|".join(
            (
                str(decision.get("setup_key") or "unknown"),
                str(decision.get("regime_key") or "UNKNOWN"),
            )
        )
        groups.setdefault(key, []).append(record)
    breakdown = []
    experiments = []
    now = _utc_now()
    for key, rows in sorted(groups.items()):
        summary = _cohort_summary(key, rows)
        values = list(summary["eligible_returns"])
        tail = summary["worst_net_excess_return_pct"]
        breakdown.append(_public_cohort_summary(summary))
        if len(values) >= 12 and tail is not None and tail <= -3.0:
            experiment = {
                "experiment_id": "v6x-" + _hash({"group": key, "tail": tail})[:28],
                "created_at": now,
                "status": "PROPOSED_NOT_APPLIED",
                "hypothesis": (
                    f"{key} has adverse tail evidence; test a stricter shadow filter "
                    "against a future held-out cohort."
                ),
                "group": key,
                "sample_size": len(values),
                "baseline_tail_loss_pct": tail,
                "requires_forward_holdout": True,
                "automatic_policy_change": False,
                "research_only": True,
                "broker_execution_enabled": False,
            }
            experiments.append(experiment)
    causal_attribution = {
        "by_setup_regime": breakdown,
        "by_source_quality": _cohort_breakdown(records, _source_quality_key),
        "by_liquidity": _cohort_breakdown(records, _liquidity_key),
        "by_catalyst": _cohort_breakdown(records, _catalyst_key),
        "by_volatility": _cohort_breakdown(records, _volatility_key),
        "failure_modes": _failure_modes(records),
    }
    persisted = (
        store.persist_alpha_v6_experiments(experiments)
        if persist and experiments
        else {"inserted": 0, "skipped": 0, "persistence_skipped": not persist}
    )
    return {
        "schema_version": "dawnstrike.alphaops_v6.failure_attribution.v2",
        "status": "COMPLETE" if breakdown else "WAITING_FOR_OUTCOMES",
        "breakdown": breakdown,
        "causal_attribution": causal_attribution,
        "proposed_experiments": experiments,
        "experiment_persistence": persisted,
        "automatic_policy_change": False,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _cohort_breakdown(records: list[dict[str, Any]], key_fn: Any) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for record in records:
        groups.setdefault(str(key_fn(record)), []).append(record)
    return [
        _public_cohort_summary(_cohort_summary(key, rows)) for key, rows in sorted(groups.items())
    ]


def _cohort_summary(key: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    values = [
        float(row["net_excess_return_pct"])
        for row in rows
        if row.get("learning_eligible") is True
        and has_authenticated_committed_fill_truth(row)
        and _number(row.get("net_excess_return_pct")) is not None
    ]
    authoritative_terminal_count = sum(
        1 for row in rows if _capture_missing_classification(row) == "authoritative_terminal"
    )
    recoverable_missing_count = sum(
        1 for row in rows if _capture_missing_classification(row) == "recoverable"
    )
    return {
        "group": key,
        "outcome_count": len(rows),
        "eligible_return_count": len(values),
        "terminal_missing_count": authoritative_terminal_count,
        "authoritative_terminal_count": authoritative_terminal_count,
        "recoverable_missing_count": recoverable_missing_count,
        "activation_count": sum(1 for row in rows if row.get("activation_status") == "ACTIVATED"),
        "not_triggered_count": sum(
            1 for row in rows if row.get("activation_status") == "NOT_TRIGGERED"
        ),
        "mean_net_excess_return_pct": (round(sum(values) / len(values), 6) if values else None),
        "worst_net_excess_return_pct": min(values) if values else None,
        "eligible_returns": values,
        "missing_truth_is_zero": False,
    }


def _public_cohort_summary(summary: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in summary.items() if key != "eligible_returns"}


_capture_missing_classification = classify_missing_capture


def _source_quality_key(record: dict[str, Any]) -> str:
    decision = dict(record.get("decision") or {})
    source = decision.get("source_summary")
    source_data = source if isinstance(source, dict) else {}
    if not str(decision.get("source_lineage_hash_sha256") or ""):
        return "missing_lineage"
    status = str(source_data.get("status") or "unknown").lower()
    if status in {"success", "complete"}:
        return "sourced_complete"
    return f"source_{status}"


def _feature_data(record: dict[str, Any]) -> dict[str, Any]:
    decision = dict(record.get("decision") or {})
    feature = decision.get("feature_vector")
    feature_data = feature if isinstance(feature, dict) else {}
    raw = feature_data.get("feature_json")
    return raw if isinstance(raw, dict) else {}


def _liquidity_key(record: dict[str, Any]) -> str:
    raw = _feature_data(record)
    liquidity = raw.get("liquidity_execution")
    value = (
        _number((liquidity or {}).get("premarket_dollar_volume"))
        if isinstance(liquidity, dict)
        else None
    )
    if value is None:
        return "liquidity_missing"
    if value < 5_000_000:
        return "under_5m"
    if value < 20_000_000:
        return "5m_to_20m"
    return "over_20m"


def _catalyst_key(record: dict[str, Any]) -> str:
    catalyst = _feature_data(record).get("catalyst")
    if not isinstance(catalyst, dict) or not catalyst:
        return "catalyst_missing"
    if catalyst.get("confirmed") is True or catalyst.get("sourced") is True:
        return "catalyst_sourced"
    return "catalyst_unconfirmed"


def _volatility_key(record: dict[str, Any]) -> str:
    raw = _feature_data(record)
    value = _number(raw.get("volatility_pct"))
    if value is None:
        value = _number(raw.get("atr_pct"))
    if value is None:
        return "volatility_missing"
    if value < 3.0:
        return "volatility_low"
    if value < 8.0:
        return "volatility_medium"
    return "volatility_high"


def _failure_modes(records: list[dict[str, Any]]) -> dict[str, Any]:
    costs = [
        value
        for record in records
        if (
            value := _number(
                dict(record.get("decision") or {}).get("estimated_round_trip_cost_bps")
            )
        )
        is not None
    ]
    mfes = [value for record in records if (value := _number(record.get("mfe_pct"))) is not None]
    maes = [value for record in records if (value := _number(record.get("mae_pct"))) is not None]
    first_touch: dict[str, int] = {}
    for record in records:
        key = str(record.get("first_touch") or "missing").lower()
        first_touch[key] = first_touch.get(key, 0) + 1
    action_counts: dict[str, int] = {}
    for record in records:
        action = str(dict(record.get("decision") or {}).get("action") or "unknown")
        action_counts[action] = action_counts.get(action, 0) + 1
    return {
        "selection_quality": {"decision_actions": action_counts},
        "entry_timing": {
            "activated_count": sum(
                1 for row in records if row.get("activation_status") == "ACTIVATED"
            ),
            "not_triggered_count": sum(
                1 for row in records if row.get("activation_status") == "NOT_TRIGGERED"
            ),
        },
        "execution_cost": {
            "sample_size": len(costs),
            "mean_estimated_round_trip_cost_bps": (
                round(sum(costs) / len(costs), 6) if costs else None
            ),
            "observed_slippage_pct": None,
            "observed_slippage_status": "MISSING_NOT_IMPUTED",
        },
        "exit_invalidation": {
            "first_touch_counts": dict(sorted(first_touch.items())),
            "mean_mfe_pct": round(sum(mfes) / len(mfes), 6) if mfes else None,
            "mean_mae_pct": round(sum(maes) / len(maes), 6) if maes else None,
        },
        "data_quality": {
            "terminal_missing_count": sum(
                1
                for row in records
                if _capture_missing_classification(row) == "authoritative_terminal"
            ),
            "authoritative_terminal_count": sum(
                1
                for row in records
                if _capture_missing_classification(row) == "authoritative_terminal"
            ),
            "recoverable_missing_count": sum(
                1 for row in records if _capture_missing_classification(row) == "recoverable"
            ),
            "sourced_complete_count": sum(
                1 for row in records if row.get("outcome_status") == "COMPLETE_SOURCED"
            ),
            "missing_truth_is_zero": False,
        },
    }


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "build_v6_failure_attribution",
    "synchronize_v6_learning",
    "synchronize_v6_outcomes",
    "v6_public_status",
]
