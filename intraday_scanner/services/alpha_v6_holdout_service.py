"""One-time, forward-only V6 holdout evaluation from immutable shadow receipts."""

from __future__ import annotations

import math
from collections import defaultdict
from typing import Any

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_RETURN_TRUTH,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.v6.registry import record_untouched_holdout_evaluation
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

_MIN_DECISIONS_PER_ARM = 10
_MIN_SESSIONS_PER_ARM = 5


def evaluate_registered_holdout(
    store: SQLiteScanStore,
    *,
    experiment_id: str,
    as_of_date: str,
) -> dict[str, Any]:
    """Evaluate one registered experiment once, only after its frozen holdout.

    Both experiment arms must be explicitly tagged on immutable decisions with
    their frozen configuration hashes.  Untagged historical rows are never
    retrofitted into a holdout, which prevents post-hoc cohort selection.
    """

    experiments = {
        str(row.get("experiment_id") or ""): row
        for row in store.load_alpha_v6_experiments(limit=50_000)
    }
    experiment = experiments.get(experiment_id)
    if experiment is None:
        return _blocked("EXPERIMENT_NOT_FOUND", experiment_id=experiment_id)
    existing = [
        row
        for row in store.load_alpha_v6_holdout_evaluations(limit=50_000)
        if row.get("experiment_id") == experiment_id
    ]
    if existing:
        return {
            "status": "ALREADY_EVALUATED_IMMUTABLE",
            "experiment_id": experiment_id,
            "persisted": False,
            "existing_evaluation": existing[0],
            "research_only": True,
            "broker_execution_enabled": False,
        }
    holdout_start = str(experiment.get("untouched_holdout_start") or "")[:10]
    cutoff = str(as_of_date or "")[:10]
    if not holdout_start or not cutoff:
        return _blocked("HOLDOUT_DATE_REQUIRED", experiment_id=experiment_id)
    if cutoff < holdout_start:
        return _blocked(
            "PRE_HOLDOUT_EVALUATION_REJECTED",
            experiment_id=experiment_id,
            holdout_start=holdout_start,
            as_of_date=cutoff,
        )

    decisions = {
        str(row.get("decision_id") or ""): row
        for row in store.load_alpha_v6_decisions()
    }
    rows_by_arm: dict[str, list[dict[str, Any]]] = defaultdict(list)
    blockers: set[str] = set()
    for outcome in store.load_alpha_v6_outcomes():
        decision = decisions.get(str(outcome.get("decision_id") or ""))
        if decision is None:
            continue
        assignment = _assignment(decision)
        if assignment.get("experiment_id") != experiment_id:
            continue
        market_date = str(outcome.get("market_date") or "")[:10]
        if not (holdout_start <= market_date <= cutoff):
            continue
        arm = str(assignment.get("arm") or "")
        expected_hash = _expected_hash(experiment, arm)
        if arm not in {"baseline", "candidate"}:
            blockers.add("experiment_arm_missing_or_invalid")
            continue
        if assignment.get("configuration_hash_sha256") != expected_hash:
            blockers.add(f"{arm}_configuration_hash_mismatch")
            continue
        if not _eligible_outcome(outcome, decision=decision):
            blockers.add("incomplete_or_unsourced_holdout_outcome")
            continue
        value = _number(outcome.get("net_excess_return_pct"))
        if value is None:
            blockers.add("holdout_return_missing")
            continue
        rows_by_arm[arm].append(
            {
                "decision_id": str(decision.get("decision_id") or ""),
                "market_date": market_date,
                "net_excess_return_pct": value,
                "source_bar_hash_sha256": outcome.get("source_bar_hash_sha256"),
            }
        )
    coverage = _coverage(rows_by_arm)
    if blockers or not _enough_evidence(coverage):
        return {
            "status": "NOT_EVALUABLE_INSUFFICIENT_PROSPECTIVE_HOLDOUT_EVIDENCE",
            "experiment_id": experiment_id,
            "holdout_start": holdout_start,
            "as_of_date": cutoff,
            "coverage": coverage,
            "blockers": sorted(
                blockers
                | _coverage_blockers(coverage)
            ),
            "persisted": False,
            "missing_truth_is_zero": False,
            "research_only": True,
            "broker_execution_enabled": False,
        }
    baseline = _mean(rows_by_arm["baseline"])
    candidate = _mean(rows_by_arm["candidate"])
    evidence = {
        "evaluation_method": "immutable_tagged_prospective_two_arm_holdout",
        "as_of_date": cutoff,
        "holdout_start": holdout_start,
        "baseline_after_cost_expectancy_pct": baseline,
        "candidate_after_cost_expectancy_pct": candidate,
        "after_cost_expectancy_pct": round(candidate - baseline, 6),
        "candidate_excess_vs_baseline_pct": round(candidate - baseline, 6),
        "coverage": coverage,
        "no_lookahead": True,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt = record_untouched_holdout_evaluation(
        experiment=experiment,
        evidence=evidence,
        existing_evaluations=[],
        evaluated_at=f"{cutoff}T23:59:59+00:00",
    )
    persisted = store.persist_alpha_v6_holdout_evaluation(receipt)
    return {
        "status": "HOLDOUT_RECORDED" if persisted else "ALREADY_EVALUATED_IMMUTABLE",
        "experiment_id": experiment_id,
        "persisted": persisted,
        "holdout_evaluation": receipt,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _assignment(decision: dict[str, Any] | None) -> dict[str, Any]:
    data = decision or {}
    nested = data.get("experiment_assignment")
    if isinstance(nested, dict):
        return nested
    return {
        "experiment_id": data.get("experiment_id"),
        "arm": data.get("experiment_arm"),
        "configuration_hash_sha256": data.get("experiment_configuration_hash_sha256"),
    }


def _expected_hash(experiment: dict[str, Any], arm: str) -> str:
    if arm == "candidate":
        return str(experiment.get("configuration_hash_sha256") or "")
    return str(experiment.get("baseline_configuration_hash_sha256") or "")


def _eligible_outcome(
    outcome: dict[str, Any], *, decision: dict[str, Any]
) -> bool:
    return bool(
        classify_canonical_return_truth(outcome, decision=decision)
        == CURRENT_RETURN_TRUTH
        and outcome.get("learning_eligible") is True
        and outcome.get("prospective_promotion_eligible") is True
        and outcome.get("no_lookahead") is True
        and str(outcome.get("source_bar_hash_sha256") or "")
    )


def _coverage(rows_by_arm: dict[str, list[dict[str, Any]]]) -> dict[str, dict[str, Any]]:
    return {
        arm: {
            "decision_count": len(rows_by_arm.get(arm, [])),
            "session_count": len(
                {str(row.get("market_date") or "") for row in rows_by_arm.get(arm, [])}
            ),
            "source_hashes_complete": all(
                bool(row.get("source_bar_hash_sha256"))
                for row in rows_by_arm.get(arm, [])
            ),
        }
        for arm in ("baseline", "candidate")
    }


def _enough_evidence(coverage: dict[str, dict[str, Any]]) -> bool:
    return all(
        int(row["decision_count"]) >= _MIN_DECISIONS_PER_ARM
        and int(row["session_count"]) >= _MIN_SESSIONS_PER_ARM
        and row["source_hashes_complete"] is True
        for row in coverage.values()
    )


def _coverage_blockers(coverage: dict[str, dict[str, Any]]) -> set[str]:
    blockers: set[str] = set()
    for arm, row in coverage.items():
        if int(row["decision_count"]) < _MIN_DECISIONS_PER_ARM:
            blockers.add(f"{arm}_minimum_decisions_not_met")
        if int(row["session_count"]) < _MIN_SESSIONS_PER_ARM:
            blockers.add(f"{arm}_minimum_sessions_not_met")
        if row["source_hashes_complete"] is not True:
            blockers.add(f"{arm}_source_lineage_incomplete")
    return blockers


def _mean(rows: list[dict[str, Any]]) -> float:
    return sum(float(row["net_excess_return_pct"]) for row in rows) / len(rows)


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _blocked(status: str, **details: str) -> dict[str, Any]:
    return {
        "status": status,
        **details,
        "persisted": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


__all__ = ["evaluate_registered_holdout"]
