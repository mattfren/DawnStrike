"""Build immutable V6 label families without converting gaps to neutral returns."""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
    CURRENT_CENSORED_PATH,
    CURRENT_RETURN_TRUTH,
    LEGACY_OR_INCOMPLETE,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.fill_truth import (
    MISSING_COMMITTED_FILL_TRUTH,
    has_authenticated_committed_fill_truth,
)
from intraday_scanner.alpha.v6.contracts import LABEL_SCHEMA_VERSION, canonical_hash, utc_now
from intraday_scanner.alpha.v6.models import evidence_lineage

_RETURN_FAMILIES = (
    "simulated_fill_feasibility",
    "net_return_after_cost",
    "benchmark_relative_excess_return",
    "stop_first_target_first",
    "mfe_pct",
    "mae_pct",
    "tail_loss_event",
)


def build_label_families(
    *, decision: dict[str, Any], outcome: dict[str, Any]
) -> list[dict[str, Any]]:
    """Create deterministic label receipts from one source-validated outcome.

    A terminal-missing receipt still records a data-quality label, but no
    absent price becomes a zero-valued return observation.
    """

    decision_id = str(decision.get("decision_id") or "")
    market_date = str(decision.get("market_date") or "")[:10]
    source_hash = _text_or_none(outcome.get("source_bar_hash_sha256"))
    path_replay_id = _text_or_none(outcome.get("path_replay_id"))
    path_contract_present = "path_replay_id" in outcome
    path_replay_available = bool(path_replay_id) if path_contract_present else True
    conclusive = str(outcome.get("outcome_status") or "").upper() == "COMPLETE_SOURCED"
    activated = str(outcome.get("activation_status") or "").upper() == "ACTIVATED"
    retrospective_eligible = outcome.get("retrospective_research_eligible") is not False
    prospective_eligible = outcome.get("prospective_promotion_eligible") is True
    lineage = evidence_lineage({**outcome, **decision})
    return_truth = _return_truth_contract(decision=decision, outcome=outcome)
    fill_truth_present = has_authenticated_committed_fill_truth(outcome)
    classification = return_truth["classification"]
    current_return = classification == CURRENT_RETURN_TRUTH
    current_not_triggered = classification == CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED
    current_censored = classification == CURRENT_CENSORED_PATH
    current_path = current_return or current_not_triggered or current_censored
    activation_conclusive = conclusive or current_not_triggered
    eligible_return = bool(
        outcome.get("learning_eligible") is True
        and source_hash
        and path_replay_available
        and retrospective_eligible
        and current_return
        and return_truth["eligible"]
        and fill_truth_present
    )
    observed_at = str(outcome.get("observed_at") or utc_now())
    base = {
        "decision_id": decision_id,
        "market_date": market_date,
        "observed_at": observed_at,
        "source_bar_hash_sha256": source_hash,
        "source_artifact_hash_sha256": lineage["source_artifact_hash_sha256"],
        "source_artifact_hashes": lineage["source_artifact_hashes"],
        "source_outcome_id": outcome.get("outcome_id"),
        "path_replay_id": lineage["path_replay_id"],
        "path_truth_status": outcome.get("path_truth_status"),
        "benchmark_hash_sha256": lineage["benchmark_hash_sha256"],
        "observed_cost_model_identity": lineage["observed_cost_model_identity"],
        "modeled_cost_model_identity": lineage["modeled_cost_model_identity"],
        "evidence_cohort": lineage["evidence_cohort"],
        "evidence_lineage_hash_sha256": lineage["evidence_lineage_hash_sha256"],
        "retrospective_research_eligible": retrospective_eligible,
        "prospective_promotion_eligible": prospective_eligible,
        "return_truth_contract_present": return_truth["contract_present"],
        "return_truth_status": return_truth["status"],
        "return_label_eligible": eligible_return,
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "eligibility_policy_version": outcome.get("eligibility_policy_version"),
        "no_lookahead": outcome.get("no_lookahead") is True,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
        "fill_truth_status": (
            "committed" if fill_truth_present else "missing_committed_fill_truth"
        ),
        "fill_truth_bound": fill_truth_present,
    }
    truth_lineage = _truth_lineage(outcome)
    base["truth_lineage_hash_sha256"] = canonical_hash(truth_lineage)
    labels = [
        _label(
            base,
            family="activation",
            value=1.0 if activated else 0.0 if activation_conclusive else None,
            eligible=(
                activation_conclusive
                and bool(source_hash)
                and current_path
                and not current_censored
            ),
            exclusion=(
                None
                if activation_conclusive
                and source_hash
                and current_path
                and not current_censored
                else "activation_truth_missing"
            ),
        ),
        _label(
            base,
            family="data_quality_failure",
            value=0.0 if conclusive and source_hash else 1.0,
            eligible=True,
            exclusion=None,
        ),
    ]
    values = {
        "simulated_fill_feasibility": 1.0 if activated else 0.0 if conclusive else None,
        "net_return_after_cost": _number(outcome.get("after_cost_return_pct")),
        "benchmark_relative_excess_return": _number(outcome.get("net_excess_return_pct")),
        "stop_first_target_first": _first_touch_label(outcome),
        "mfe_pct": (
            _number(outcome.get("mfe_pct"))
            if outcome.get("excursion_exact") is True
            else None
        ),
        "mae_pct": (
            _number(outcome.get("mae_pct"))
            if outcome.get("excursion_exact") is True
            else None
        ),
        "tail_loss_event": _tail_loss_label(outcome),
    }
    for family in _RETURN_FAMILIES:
        value = values[family]
        allowed = conclusive and bool(source_hash) and (not activated or value is not None)
        eligible = allowed and eligible_return and fill_truth_present
        labels.append(
            _label(
                base,
                family=family,
                value=value,
                eligible=eligible,
                exclusion=(
                    None
                    if eligible
                    else (
                        MISSING_COMMITTED_FILL_TRUTH
                        if not fill_truth_present
                        else "return_truth_missing_or_ineligible"
                    )
                ),
            )
        )
    if str(decision.get("action") or "") == "SHADOW_REJECTED_POLICY":
        labels.append(
            _label(
                base,
                family="rejected_candidate_regret",
                value=_number(outcome.get("net_excess_return_pct")),
                eligible=eligible_return and _sampled_rejected_candidate(decision),
                exclusion=(
                    None
                    if eligible_return and _sampled_rejected_candidate(decision)
                    else (
                        MISSING_COMMITTED_FILL_TRUTH
                        if not fill_truth_present
                        else "rejected_candidate_not_in_frozen_sampling_policy"
                    )
                ),
            )
        )
    return labels


def _label(
    base: dict[str, Any],
    *,
    family: str,
    value: float | None,
    eligible: bool,
    exclusion: str | None,
) -> dict[str, Any]:
    payload = {
        **base,
        "label_family": family,
        "label_value": value,
        "learning_eligible": eligible,
        "exclusion_reason": exclusion,
    }
    if family in {*_RETURN_FAMILIES, "rejected_candidate_regret"}:
        fill_truth_present = base.get("fill_truth_bound") is True
        payload.update(
            {
                "fill_truth_required": True,
                "fill_truth_status": (
                    "committed" if fill_truth_present else "missing_committed_fill_truth"
                ),
                "return_learning_quarantine_reason": (
                    None if fill_truth_present else MISSING_COMMITTED_FILL_TRUTH
                ),
            }
        )
    identity = {
        "schema_version": payload["label_schema_version"],
        "decision_id": payload["decision_id"],
        "family": family,
        "value": value,
        "truth_lineage_hash_sha256": payload["truth_lineage_hash_sha256"],
    }
    payload["label_id"] = "v6l-v2-" + canonical_hash(identity)
    payload["label_payload_hash_sha256"] = canonical_hash(
        {**identity, "label_id": payload["label_id"]}
    )
    return payload


def _sampled_rejected_candidate(decision: dict[str, Any]) -> bool:
    sampling = decision.get("rejected_sampling")
    return isinstance(sampling, dict) and sampling.get("included") is True and _number(
        sampling.get("inclusion_probability")
    ) is not None


def _first_touch_label(outcome: dict[str, Any]) -> float | None:
    value = str(outcome.get("first_touch") or "").lower()
    if value in {"target", "target_first"}:
        return 1.0
    if value in {"stop", "stop_first"}:
        return 0.0
    return None


def _tail_loss_label(outcome: dict[str, Any]) -> float | None:
    loss = _number(outcome.get("net_excess_return_pct"))
    if loss is None:
        return None
    return 1.0 if loss <= -3.0 else 0.0


def _return_truth_contract(
    *, decision: dict[str, Any], outcome: dict[str, Any]
) -> dict[str, Any]:
    """Require the authenticated canonical return contract for return labels."""

    classification = classify_canonical_return_truth(outcome, decision=decision)
    if classification != CURRENT_RETURN_TRUTH:
        return {
            "contract_present": classification != LEGACY_OR_INCOMPLETE,
            "eligible": False,
            "status": (
                "MISSING_CURRENT_CONTRACT"
                if classification == LEGACY_OR_INCOMPLETE
                else "CURRENT_NONRETURN_CONTRACT"
                if classification
                in {CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED, CURRENT_CENSORED_PATH}
                else "INVALID_CURRENT_CONTRACT"
            ),
            "classification": classification,
            "missing_requirements": ["authenticated_canonical_return_truth"],
        }

    contract_keys = {
        "after_cost_return_pct",
        "benchmark_hash_sha256",
        "benchmark_source_bar_hash_sha256",
        "causal_decision_identity",
        "independent_reconciliation_complete",
        "independent_reconciliation_status",
    }
    contract_present = bool(contract_keys.intersection(outcome))
    if not contract_present:
        return {
            "contract_present": False,
            "eligible": False,
            "status": "CANONICAL_RETURN_TRUTH_REQUIRED",
            "missing_requirements": ["authenticated_canonical_return_truth"],
        }

    after_cost = _number(
        outcome.get("after_cost_return_pct")
        if outcome.get("after_cost_return_pct") is not None
        else outcome.get("net_return_pct")
    )
    benchmark_hash = (
        _text_or_none(outcome.get("benchmark_hash_sha256"))
        or _text_or_none(outcome.get("benchmark_artifact_hash_sha256"))
        or _text_or_none(outcome.get("benchmark_source_bar_hash_sha256"))
    )
    benchmark_value = _number(outcome.get("benchmark_return_pct"))
    excess_value = _number(outcome.get("net_excess_return_pct"))
    independent = outcome.get("independent_reconciliation_complete") is True
    if not independent:
        statuses = [
            outcome.get("independent_reconciliation_status"),
            outcome.get("benchmark_independent_reconciliation_status"),
        ]
        present_statuses = [str(value or "").upper() for value in statuses if value is not None]
        independent = bool(present_statuses) and all(
            value in {"PASSED", "PASS", "COMPLETE", "RECONCILED"}
            for value in present_statuses
        )
    causal = outcome.get("causal_decision_identity") is True or (
        bool(str(decision.get("decision_id") or "").strip())
        and bool(str(decision.get("input_hash_sha256") or "").strip())
        and bool(str(decision.get("source_lineage_hash_sha256") or "").strip())
        and isinstance(decision.get("point_in_time"), dict)
        and decision["point_in_time"].get(
            "all_inputs_observed_at_or_before_decision"
        )
        is True
    )
    missing = []
    if str(outcome.get("outcome_status") or "").upper() != "COMPLETE_SOURCED":
        missing.append("complete_sourced_outcome")
    if not independent:
        missing.append("independent_reconciliation")
    if after_cost is None:
        missing.append("after_cost_return")
    if not benchmark_hash or benchmark_value is None and excess_value is None:
        missing.append("benchmark_alignment")
    if not causal:
        missing.append("causal_decision_identity")
    return {
        "contract_present": True,
        "eligible": not missing,
        "status": "COMPLETE" if not missing else "INCOMPLETE",
        "classification": classification,
        "missing_requirements": missing,
    }


_TRUTH_LINEAGE_FIELDS = (
    "path_replay_schema_version",
    "path_replay_id",
    "path_replay_policy_version",
    "path_replay_policy_hash_sha256",
    "replay_input_hash_sha256",
    "replay_truth_hash_sha256",
    "replay_receipt_hash_sha256",
    "source_artifact_identity",
    "source_artifact_hash_sha256",
    "source_bar_hash_sha256",
    "source_coverage_complete",
    "source_conflict",
    "corporate_action_unresolved",
    "sequence_complete_through_exit",
    "path_truth_status",
    "path_event",
    "entry_price",
    "exit_price",
    "return_truth_schema_version",
    "return_truth_hash_sha256",
    "cost_schema_version",
    "cost_receipt_id",
    "cost_receipt_hash_sha256",
    "cost_receipt",
    "observed_cost_model_identity",
    "modeled_cost_model_identity",
    "cost_components",
    "after_cost_return_pct",
    "benchmark_symbol",
    "benchmark_return_pct",
    "benchmark_source_bar_hash_sha256",
    "benchmark_independent_reconciliation_status",
    "secondary_benchmark_symbol",
    "secondary_benchmark_return_pct",
    "secondary_benchmark_source_bar_hash_sha256",
    "secondary_benchmark_independent_reconciliation_status",
    "reconciliation_schema_version",
    "reconciliation_receipt_id",
    "reconciliation_receipt_hash_sha256",
    "reconciliation_receipt",
    "independent_reconciliation_status",
    "causal_decision_identity",
    "eligibility_policy_version",
    "retrospective_research_eligible",
    "prospective_promotion_eligible",
    "evidence_cohort",
    "no_lookahead",
    "validated_against_signal_timestamp",
    "research_only",
    "broker_execution_enabled",
)


def _truth_lineage(outcome: dict[str, Any]) -> dict[str, Any]:
    """Bind label identity to every current return-truth dimension."""

    return {
        "label_schema_version": LABEL_SCHEMA_VERSION,
        **{field: outcome.get(field) for field in _TRUTH_LINEAGE_FIELDS},
    }


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _text_or_none(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


__all__ = ["build_label_families"]
