"""V6 shadow-ledger orchestration; deterministic and research-only."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.alpha.v6_shadow import (
    ALPHAOPS_V6_MODEL_VERSION,
    build_v6_outcomes,
    promotion_readiness,
    strict_walk_forward_evaluation,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def synchronize_v6_learning(store: SQLiteScanStore) -> dict[str, Any]:
    """Append sourced V6 outcome receipts and write one evaluable model receipt."""

    decisions = store.load_alpha_v6_decisions(action="SHADOW_TRACK")
    existing = {
        str(row.get("decision_id") or "") for row in store.load_alpha_v6_outcomes()
    }
    pending = [
        row for row in decisions if str(row.get("decision_id") or "") not in existing
    ]
    generated = build_v6_outcomes(
        decisions=pending,
        sourced_outcomes=store.load_signal_outcomes(limit=50_000),
        capture_attempts=store.load_outcome_capture_attempts(limit=50_000),
    )
    outcome_stats = store.persist_alpha_v6_outcomes(generated) if generated else {
        "inserted": 0,
        "skipped": 0,
    }
    outcomes = store.load_alpha_v6_outcomes()
    evaluation = strict_walk_forward_evaluation(
        decisions=store.load_alpha_v6_decisions(), outcomes=outcomes
    )
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
    inserted_model = store.persist_alpha_v6_model_run(model_run)
    evaluation_payload = {
        "evaluation_id": "v6e-" + _hash(
            {"model_run_id": model_run_id, "evaluation": evaluation}
        )[:28],
        "model_run_id": model_run_id,
        "evaluated_at": now,
        "status": evaluation["status"],
        "evaluation_input_hash_sha256": _hash(evaluation),
        **evaluation,
    }
    inserted_evaluation = store.persist_alpha_v6_evaluation(evaluation_payload)
    return {
        "schema_version": "dawnstrike.alphaops_v6.learning_run.v1",
        "status": evaluation["status"],
        "decision_count": len(decisions),
        "pending_outcome_count": len(pending),
        "outcome_generation": outcome_stats,
        "model_run": {**model_run, "inserted": inserted_model},
        "evaluation": {**evaluation, "inserted": inserted_evaluation},
        "promotion_readiness": promotion_readiness(outcomes),
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
    return {
        "schema_version": "dawnstrike.alphaops_v6.public_status.v1",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "decision_count": len(decisions),
        "tracked_count": sum(
            1 for row in decisions if row.get("action") == "SHADOW_TRACK"
        ),
        "outcome_count": len(outcomes),
        "learning_eligible_outcome_count": sum(
            1 for row in outcomes if row.get("learning_eligible") is True
        ),
        "latest_model_run": model_runs[0] if model_runs else None,
        "latest_evaluation": evaluations[0] if evaluations else None,
        "promotion_readiness": promotion_readiness(outcomes),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def build_v6_failure_attribution(store: SQLiteScanStore) -> dict[str, Any]:
    """Explain V6 outcomes by setup/regime without silently changing policy."""

    decisions = store.load_alpha_v6_decisions()
    outcomes = store.load_alpha_v6_outcomes()
    by_id = {str(row.get("decision_id") or ""): row for row in decisions}
    groups: dict[str, list[dict[str, Any]]] = {}
    for outcome in outcomes:
        decision = by_id.get(str(outcome.get("decision_id") or ""))
        if decision is None:
            continue
        key = "|".join((
            str(decision.get("setup_key") or "unknown"),
            str(decision.get("regime_key") or "UNKNOWN"),
        ))
        groups.setdefault(key, []).append({**outcome, "decision": decision})
    breakdown = []
    experiments = []
    now = _utc_now()
    for key, rows in sorted(groups.items()):
        values = [
            float(row["net_excess_return_pct"])
            for row in rows
            if row.get("learning_eligible") is True
            and row.get("net_excess_return_pct") is not None
        ]
        missing = sum(1 for row in rows if row.get("outcome_status") == "TERMINAL_MISSING")
        activation = sum(1 for row in rows if row.get("activation_status") == "ACTIVATED")
        tail = min(values) if values else None
        record = {
            "group": key,
            "outcome_count": len(rows),
            "eligible_return_count": len(values),
            "terminal_missing_count": missing,
            "activation_count": activation,
            "mean_net_excess_return_pct": round(sum(values) / len(values), 6) if values else None,
            "worst_net_excess_return_pct": tail,
            "missing_truth_is_zero": False,
        }
        breakdown.append(record)
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
    persisted = store.persist_alpha_v6_experiments(experiments) if experiments else {
        "inserted": 0,
        "skipped": 0,
    }
    return {
        "schema_version": "dawnstrike.alphaops_v6.failure_attribution.v1",
        "status": "COMPLETE" if breakdown else "WAITING_FOR_OUTCOMES",
        "breakdown": breakdown,
        "proposed_experiments": experiments,
        "experiment_persistence": persisted,
        "automatic_policy_change": False,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "build_v6_failure_attribution",
    "synchronize_v6_learning",
    "v6_public_status",
]
