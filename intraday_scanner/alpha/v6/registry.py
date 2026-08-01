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


__all__ = ["promotion_review_packet", "register_experiment"]
