"""Build immutable V6 label families without converting gaps to neutral returns."""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.v6.contracts import LABEL_SCHEMA_VERSION, canonical_hash, utc_now

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
    conclusive = str(outcome.get("outcome_status") or "").upper() == "COMPLETE_SOURCED"
    activated = str(outcome.get("activation_status") or "").upper() == "ACTIVATED"
    eligible_return = bool(outcome.get("learning_eligible") is True and source_hash)
    observed_at = str(outcome.get("observed_at") or utc_now())
    base = {
        "decision_id": decision_id,
        "market_date": market_date,
        "observed_at": observed_at,
        "source_bar_hash_sha256": source_hash,
        "source_outcome_id": outcome.get("outcome_id"),
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
        "net_return_after_cost": _number(outcome.get("net_return_pct")),
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
