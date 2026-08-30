"""Durable, fail-closed V6 experiment and trial contracts.

The experiment ledger is deliberately separate from model fitting.  A trial is
an attempted search, including failed and quarantined attempts; it is never
reduced to the number of successful models.  The store is the source of truth
for the global and per-experiment counts used by selection-bias controls.
"""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.v6.contracts import (
    canonical_hash,
    is_valid_code_sha,
    is_valid_sha256,
    utc_now,
)


def preregistration_blockers(
    experiment: dict[str, Any] | None,
    *,
    arm_id: str | None,
    configuration_hash_sha256: str | None,
    feature_set_hash_sha256: str | None,
    cost_model_version: str | None,
    validation_window: dict[str, Any] | None,
) -> list[str]:
    """Return exact blockers for a training/evaluation attempt."""

    blockers: list[str] = []
    if not isinstance(experiment, dict) or not str(experiment.get("experiment_id") or ""):
        blockers.append("preregistered_experiment_missing")
    if not str(arm_id or "").strip():
        blockers.append("preregistered_arm_missing")
    if not is_valid_sha256(configuration_hash_sha256):
        blockers.append("configuration_hash_missing_or_invalid")
    if not is_valid_sha256(feature_set_hash_sha256):
        blockers.append("feature_set_hash_missing_or_invalid")
    if not str(cost_model_version or "").strip():
        blockers.append("cost_model_version_missing")
    frozen = experiment.get("frozen_windows") if isinstance(experiment, dict) else None
    if not isinstance(frozen, dict) or not frozen:
        blockers.append("frozen_validation_window_missing")
    if not isinstance(validation_window, dict) or validation_window != frozen:
        blockers.append("frozen_validation_window_mismatch")
    if isinstance(experiment, dict):
        if str(experiment.get("status") or "").endswith("MISSING_LINEAGE"):
            blockers.append("experiment_lineage_incomplete")
        if str(experiment.get("configuration_hash_sha256") or "") != str(
            configuration_hash_sha256 or ""
        ):
            blockers.append("experiment_configuration_mismatch")
        declared_feature = str(experiment.get("feature_set_hash_sha256") or "")
        if declared_feature and declared_feature != str(feature_set_hash_sha256 or ""):
            blockers.append("experiment_feature_set_mismatch")
        declared_cost = str(experiment.get("cost_model_version") or "")
        if declared_cost and declared_cost != str(cost_model_version or ""):
            blockers.append("experiment_cost_model_mismatch")
    return list(dict.fromkeys(blockers))


def build_trial_receipt(
    *,
    experiment: dict[str, Any] | None,
    arm_id: str,
    strategy_id: str,
    strategy_version: str,
    configuration_hash_sha256: str,
    feature_set_hash_sha256: str,
    cost_model_version: str,
    validation_window: dict[str, Any],
    code_sha: str,
    source_hash_sha256: str,
    status: str = "ATTEMPTED",
    trial_number: int | None = None,
) -> dict[str, Any]:
    """Build an append-only attempt receipt; persistence assigns its number."""

    blockers = preregistration_blockers(
        experiment,
        arm_id=arm_id,
        configuration_hash_sha256=configuration_hash_sha256,
        feature_set_hash_sha256=feature_set_hash_sha256,
        cost_model_version=cost_model_version,
        validation_window=validation_window,
    )
    if blockers:
        raise ValueError("trial preregistration is incomplete: " + ", ".join(blockers))
    if not is_valid_code_sha(code_sha) or not is_valid_sha256(source_hash_sha256):
        raise ValueError("trial code and source lineage must be valid hashes")
    if status not in {"ATTEMPTED", "COMPLETE", "FAILED", "QUARANTINED"}:
        raise ValueError("invalid experiment trial status")
    experiment_data = experiment if isinstance(experiment, dict) else {}
    experiment_id = str(experiment_data.get("experiment_id") or "")
    if not experiment_id:
        raise ValueError("trial experiment identity is required")
    content = {
        "experiment_id": experiment_id,
        "arm_id": arm_id,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "configuration_hash_sha256": configuration_hash_sha256,
        "feature_set_hash_sha256": feature_set_hash_sha256,
        "cost_model_version": cost_model_version,
        "validation_window": validation_window,
        "code_sha": code_sha,
        "source_hash_sha256": source_hash_sha256,
        "status": status,
    }
    receipt = {
        **content,
        "trial_id": "v6t-" + canonical_hash(content)[:28],
        "trial_number": trial_number,
        "attempted_at": utc_now(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt["payload_hash_sha256"] = canonical_hash(
        {key: value for key, value in receipt.items() if key != "payload_hash_sha256"}
    )
    return receipt


def trial_count_status(
    *,
    global_attempt_count: int,
    experiment_attempt_count: int,
    trial_count: int | None,
) -> dict[str, Any]:
    """Validate durable counts; absent counts are not treated as one trial."""

    valid = (
        isinstance(global_attempt_count, int)
        and global_attempt_count >= 1
        and isinstance(experiment_attempt_count, int)
        and experiment_attempt_count >= 1
        and isinstance(trial_count, int)
        and trial_count >= 1
    )
    return {
        "status": "EVALUABLE" if valid else "NOT_EVALUABLE_TRIAL_COUNT_MISSING",
        "global_attempt_count": global_attempt_count,
        "experiment_attempt_count": experiment_attempt_count,
        "trial_count": trial_count,
        "multiple_testing_penalty_allowed": valid,
        "missing_trial_count_is_not_one": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }


__all__ = [
    "build_trial_receipt",
    "preregistration_blockers",
    "trial_count_status",
]
