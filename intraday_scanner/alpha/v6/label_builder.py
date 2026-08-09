"""Build immutable V6 label families without converting gaps to neutral returns."""

from __future__ import annotations

from typing import Any

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
    eligible_return = bool(
        outcome.get("learning_eligible") is True
        and source_hash
        and path_replay_available
        and retrospective_eligible
        and return_truth["eligible"]
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
        "no_lookahead": outcome.get("no_lookahead") is True,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    labels = [
        _label(
            base,
            family="activation",
            value=1.0 if activated else 0.0 if conclusive else None,
            eligible=conclusive and bool(source_hash),
            exclusion=None if conclusive and source_hash else "activation_truth_missing",
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
        "net_return_after_cost": _number(
            outcome.get("after_cost_return_pct")
            if outcome.get("after_cost_return_pct") is not None
            else outcome.get("net_return_pct")
        ),
        "benchmark_relative_excess_return": _number(outcome.get("net_excess_return_pct")),
        "stop_first_target_first": _first_touch_label(outcome),
        "mfe_pct": _number(outcome.get("mfe_pct")),
        "mae_pct": _number(outcome.get("mae_pct")),
        "tail_loss_event": _tail_loss_label(outcome),
    }
    for family in _RETURN_FAMILIES:
        value = values[family]
        allowed = conclusive and bool(source_hash) and (not activated or value is not None)
        eligible = allowed and (
            eligible_return or family == "simulated_fill_feasibility"
        )
        labels.append(
            _label(
                base,
                family=family,
                value=value,
                eligible=eligible,
                exclusion=(
                    None if eligible else "return_truth_missing_or_ineligible"
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
                    else "rejected_candidate_not_in_frozen_sampling_policy"
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
    payload["label_id"] = "v6l-" + canonical_hash(
        {
            "decision_id": payload["decision_id"],
            "family": family,
            "source_outcome_id": payload["source_outcome_id"],
            "value": value,
        }
    )[:28]
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
    """Check the additive complete-return contract when its fields are present."""

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
        return {"contract_present": False, "eligible": True, "status": "LEGACY_CONTRACT"}

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
        "missing_requirements": missing,
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
