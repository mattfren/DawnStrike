"""Forward-only one-change experiment and manual promotion contracts."""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.v6.contracts import canonical_hash, utc_now


def register_experiment(
    *,
    hypothesis: str,
    training_cutoff: str,
    baseline_config: dict[str, Any],
    candidate_config: dict[str, Any],
    validation_start: str,
    holdout_start: str,
    stop_condition: str,
    promotion_requirements: list[str],
) -> dict[str, Any]:
    """Register exactly one prospective policy difference; never apply it."""

    changed = sorted(
        key
        for key in set(baseline_config) | set(candidate_config)
        if baseline_config.get(key) != candidate_config.get(key)
    )
    if len(changed) != 1:
        raise ValueError("A V6 challenger experiment must change exactly one field.")
    if not hypothesis.strip() or not stop_condition.strip() or not promotion_requirements:
        raise ValueError("Experiment hypothesis, stop condition, and promotion rules are required.")
    if not (training_cutoff < validation_start < holdout_start):
        raise ValueError("Experiment windows must be strictly forward of the training cutoff.")
    payload = {
        "hypothesis": hypothesis.strip(),
        "training_cutoff": training_cutoff,
        "baseline_config": baseline_config,
        "candidate_config": candidate_config,
        "changed_field": changed[0],
        "unchanged_controls": sorted(key for key in baseline_config if key != changed[0]),
        "baseline_configuration_hash_sha256": canonical_hash(baseline_config),
        "validation_start": validation_start,
        "untouched_holdout_start": holdout_start,
        "holdout_evaluated_at": None,
        "stop_condition": stop_condition.strip(),
        "promotion_requirements": promotion_requirements,
        "status": "REGISTERED_NOT_APPLIED",
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["experiment_id"] = "v6x-" + canonical_hash(payload)[:28]
    payload["created_at"] = utc_now()
    payload["configuration_hash_sha256"] = canonical_hash(candidate_config)
    return payload


def promotion_review_packet(
    *, evidence: dict[str, Any], operator: str | None = None
) -> dict[str, Any]:
    """Make promotion a recorded human decision, never an automated outcome."""

    payload = {
        "created_at": utc_now(),
        "operator": operator,
        "evidence": evidence,
        "status": "PENDING_MANUAL_REVIEW",
        "approved": False,
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["review_id"] = "v6pr-" + canonical_hash(payload)[:28]
    return payload


def record_untouched_holdout_evaluation(
    *,
    experiment: dict[str, Any],
    evidence: dict[str, Any],
    existing_evaluations: list[dict[str, Any]],
    evaluated_at: str | None = None,
) -> dict[str, Any]:
    """Create the sole immutable holdout receipt for one frozen experiment."""

    experiment_id = str(experiment.get("experiment_id") or "")
    if not experiment_id:
        raise ValueError("A registered experiment_id is required.")
    if any(row.get("experiment_id") == experiment_id for row in existing_evaluations):
        raise ValueError("The untouched holdout for this experiment was already evaluated.")
    holdout_start = str(experiment.get("untouched_holdout_start") or "")
    if not holdout_start:
        raise ValueError("The experiment has no frozen untouched holdout.")
    timestamp = evaluated_at or utc_now()
    if timestamp[:10] < holdout_start[:10]:
        raise ValueError("The untouched holdout cannot be evaluated before its start date.")
    if evidence.get("no_lookahead") is not True:
        raise ValueError("Holdout evidence must pass the no-lookahead audit.")
    expectancy = evidence.get("after_cost_expectancy_pct")
    status = (
        "POSITIVE_HOLDOUT"
        if isinstance(expectancy, (int, float)) and float(expectancy) > 0.0
        else "NEGATIVE_OR_INCOMPLETE_HOLDOUT"
    )
    payload = {
        "experiment_id": experiment_id,
        "evaluated_at": timestamp,
        "holdout_start": holdout_start,
        "configuration_hash_sha256": experiment.get("configuration_hash_sha256"),
        "status": status,
        "evidence": evidence,
        "evidence_hash_sha256": canonical_hash(evidence),
        "evaluated_once": True,
        "automatic_promotion": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["holdout_evaluation_id"] = "v6h-" + canonical_hash(
        {"experiment_id": experiment_id, "evidence_hash": payload["evidence_hash_sha256"]}
    )[:28]
    return payload


__all__ = [
    "promotion_review_packet",
    "record_untouched_holdout_evaluation",
    "register_experiment",
]
