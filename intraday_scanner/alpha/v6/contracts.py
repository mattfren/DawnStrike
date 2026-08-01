"""Immutable contracts shared by the AlphaOps V6 research system."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from typing import Any

ALPHAOPS_V6_STRATEGY_VERSION = "dawnstrike-alphaops-v6-shadow"
ALPHAOPS_V6_MODEL_VERSION = "dawnstrike-alphaops-v6-empirical-v1"
V6_COST_MODEL_VERSION = "dawnstrike-alphaops-v6-conservative-cost-v1"
FEATURE_SCHEMA_VERSION = "dawnstrike-alphaops-v6-feature-schema-v1"
LABEL_SCHEMA_VERSION = "dawnstrike-alphaops-v6-label-schema-v1"
DATASET_SCHEMA_VERSION = "dawnstrike-alphaops-v6-dataset-v1"
ALLOWED_DECISION_ACTIONS = frozenset(
    {"SHADOW_TRACK", "SHADOW_REJECT_VETO", "SHADOW_REJECTED_POLICY"}
)


def canonical_hash(value: object) -> str:
    """Hash a JSON-compatible value with deterministic serialization."""

    encoded = json.dumps(
        value,
        default=str,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def point_in_time_valid(decision: dict[str, Any]) -> bool:
    """Require explicit decision-time provenance without inferring missing truth."""

    point_in_time = decision.get("point_in_time")
    if not isinstance(point_in_time, dict):
        return False
    if point_in_time.get("all_inputs_observed_at_or_before_decision") is not True:
        return False
    return bool(
        decision.get("decision_at")
        and decision.get("input_hash_sha256")
        and decision.get("source_lineage_hash_sha256")
    )


def decision_contract_violations(decision: dict[str, Any]) -> list[str]:
    """Return every violation so no malformed decision silently enters training."""

    violations: list[str] = []
    for field in (
        "decision_id",
        "market_date",
        "decision_at",
        "ticker",
        "strategy_version",
        "model_version",
        "input_hash_sha256",
        "source_lineage_hash_sha256",
    ):
        if not str(decision.get(field) or "").strip():
            violations.append(f"missing_{field}")
    if str(decision.get("action") or "") not in ALLOWED_DECISION_ACTIONS:
        violations.append("invalid_action")
    if decision.get("research_only") is not True:
        violations.append("research_only_required")
    if decision.get("broker_execution_enabled") is not False:
        violations.append("broker_execution_must_be_disabled")
    if not point_in_time_valid(decision):
        violations.append("point_in_time_lineage_invalid")
    if not isinstance(decision.get("safety_vetoes"), list):
        violations.append("safety_vetoes_missing")
    universe_membership = decision.get("universe_membership")
    if not isinstance(universe_membership, dict):
        violations.append("universe_membership_missing")
    elif not str(universe_membership.get("universe_id") or "").strip():
        violations.append("universe_version_missing")
    elif not str(universe_membership.get("source_lineage_hash_sha256") or "").strip():
        violations.append("universe_lineage_missing")
    if decision.get("action") == "SHADOW_TRACK" and decision.get("safety_vetoes"):
        violations.append("tracked_decision_has_safety_veto")
    return violations


def label_contract_violations(label: dict[str, Any]) -> list[str]:
    violations: list[str] = []
    for field in ("label_id", "decision_id", "label_family", "market_date"):
        if not str(label.get(field) or "").strip():
            violations.append(f"missing_{field}")
    if label.get("research_only") is not True:
        violations.append("research_only_required")
    if label.get("broker_execution_enabled") is not False:
        violations.append("broker_execution_must_be_disabled")
    if label.get("learning_eligible") is True and label.get("source_bar_hash_sha256") is None:
        violations.append("eligible_label_missing_source_hash")
    if label.get("missing_truth_is_zero") is not False:
        violations.append("missing_truth_contract_missing")
    return violations


__all__ = [
    "ALLOWED_DECISION_ACTIONS",
    "ALPHAOPS_V6_MODEL_VERSION",
    "ALPHAOPS_V6_STRATEGY_VERSION",
    "DATASET_SCHEMA_VERSION",
    "FEATURE_SCHEMA_VERSION",
    "LABEL_SCHEMA_VERSION",
    "V6_COST_MODEL_VERSION",
    "canonical_hash",
    "decision_contract_violations",
    "label_contract_violations",
    "point_in_time_valid",
    "utc_now",
]
