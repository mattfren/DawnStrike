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

_TRIAL_CONTENT_FIELDS = (
    "attempt_id",
    "experiment_id",
    "arm_id",
    "strategy_id",
    "strategy_version",
    "configuration_hash_sha256",
    "feature_set_hash_sha256",
    "cost_model_version",
    "validation_window",
    "code_sha",
    "source_hash_sha256",
    "status",
)
_TRIAL_IDENTITY_FIELDS = ("attempt_id",)


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
    attempt_id: str,
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
) -> dict[str, Any]:
    """Build an append-only attempt receipt; persistence assigns its number.

    ``attempt_id`` is the retry-stable identity of one actual search attempt.
    A caller must mint a new identity for every distinct attempt, including a
    rerun of identical hyperparameters.  Retrying the same crashed operation
    reuses the same identity and therefore remains idempotent.
    """

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
    if not str(attempt_id or "").strip():
        raise ValueError("trial attempt identity is required")
    experiment_data = experiment if isinstance(experiment, dict) else {}
    experiment_id = str(experiment_data.get("experiment_id") or "")
    if not experiment_id:
        raise ValueError("trial experiment identity is required")
    content = {
        "attempt_id": str(attempt_id).strip(),
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
        "trial_id": "v6t-"
        + canonical_hash({key: content[key] for key in _TRIAL_IDENTITY_FIELDS})[:28],
        "trial_number": None,
        "attempted_at": utc_now(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    receipt["payload_hash_sha256"] = canonical_hash(
        {key: value for key, value in receipt.items() if key != "payload_hash_sha256"}
    )
    return receipt


def validate_trial_receipt(
    receipt: dict[str, Any], *, assigned_number: bool
) -> list[str]:
    """Validate identity, ordinal state, and the complete payload hash."""

    blockers: list[str] = []
    content = {key: receipt.get(key) for key in _TRIAL_CONTENT_FIELDS}
    if any(value in (None, "") for value in content.values()):
        blockers.append("trial_content_missing")
    expected_trial_id = "v6t-" + canonical_hash(
        {key: content[key] for key in _TRIAL_IDENTITY_FIELDS}
    )[:28]
    if receipt.get("trial_id") != expected_trial_id:
        blockers.append("trial_identity_mismatch")
    supplied_hash = receipt.get("payload_hash_sha256")
    expected_hash = canonical_hash(
        {key: value for key, value in receipt.items() if key != "payload_hash_sha256"}
    )
    if not is_valid_sha256(supplied_hash) or supplied_hash != expected_hash:
        blockers.append("trial_payload_hash_mismatch")
    trial_number = receipt.get("trial_number")
    if assigned_number:
        if not isinstance(trial_number, int) or isinstance(trial_number, bool) or trial_number < 1:
            blockers.append("trial_number_missing_or_invalid")
    elif trial_number is not None:
        blockers.append("caller_assigned_trial_number_forbidden")
    if not str(receipt.get("attempted_at") or "").strip():
        blockers.append("trial_attempted_at_missing")
    if receipt.get("research_only") is not True:
        blockers.append("trial_not_research_only")
    if receipt.get("broker_execution_enabled") is not False:
        blockers.append("trial_broker_execution_not_disabled")
    return list(dict.fromkeys(blockers))


def assign_trial_number(receipt: dict[str, Any], *, trial_number: int) -> dict[str, Any]:
    """Bind the store-owned global ordinal into a newly persisted receipt."""

    blockers = validate_trial_receipt(receipt, assigned_number=False)
    if blockers:
        raise ValueError("invalid unassigned trial receipt: " + ", ".join(blockers))
    if not isinstance(trial_number, int) or isinstance(trial_number, bool) or trial_number < 1:
        raise ValueError("trial number must be a positive integer")
    assigned = dict(receipt)
    assigned["trial_number"] = trial_number
    assigned["payload_hash_sha256"] = canonical_hash(
        {key: value for key, value in assigned.items() if key != "payload_hash_sha256"}
    )
    return assigned


def trial_retry_semantics(receipt: dict[str, Any]) -> dict[str, Any]:
    """Return retry-stable semantics while preserving first-attempt evidence."""

    return {
        key: value
        for key, value in receipt.items()
        if key not in {"attempted_at", "trial_number", "payload_hash_sha256"}
    }


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
    "assign_trial_number",
    "build_trial_receipt",
    "preregistration_blockers",
    "trial_retry_semantics",
    "trial_count_status",
    "validate_trial_receipt",
]
