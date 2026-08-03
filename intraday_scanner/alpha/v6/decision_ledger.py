"""Decision-ledger validation and deterministic rejected-candidate sampling."""

from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.v6.contracts import (
    ALPHAOPS_V6_MODEL_VERSION,
    ALPHAOPS_V6_STRATEGY_VERSION,
    FEATURE_SCHEMA_VERSION,
    V6_COST_MODEL_VERSION,
    canonical_hash,
    decision_contract_violations,
)


def attach_rejected_candidate_sampling(
    decisions: list[dict[str, Any]], *, denominator: int = 5
) -> list[dict[str, Any]]:
    """Mark a stable stratified subset of policy-rejected candidates for regret labels."""

    if denominator < 1:
        raise ValueError("denominator must be positive")
    enriched: list[dict[str, Any]] = []
    for decision in decisions:
        row = dict(decision)
        if row.get("action") == "SHADOW_REJECTED_POLICY":
            bucket = int(canonical_hash(row.get("decision_id"))[:8], 16) % denominator
            row["rejected_sampling"] = {
                "policy_version": "dawnstrike-alphaops-v6-rejected-stratified-v1",
                "included": bucket == 0,
                "inclusion_probability": round(1.0 / denominator, 6),
                "stratum": "|".join(
                    (
                        str(row.get("setup_key") or "unknown"),
                        str(row.get("regime_key") or "UNKNOWN"),
                    )
                ),
            }
        enriched.append(row)
    return enriched


def validate_decision_batch(decisions: list[dict[str, Any]]) -> dict[str, Any]:
    invalid = [
        {"decision_id": row.get("decision_id"), "violations": decision_contract_violations(row)}
        for row in decisions
        if decision_contract_violations(row)
    ]
    return {
        "decision_count": len(decisions),
        "invalid_count": len(invalid),
        "invalid": invalid,
        "valid": not invalid,
        "research_only": True,
    }


def build_candidate_decisions(
    *,
    signals: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    feature_vectors: list[dict[str, Any]],
    source_summary: dict[str, Any],
    regime: dict[str, Any],
    prior_outcomes: list[dict[str, Any]],
    frozen_model_run: dict[str, Any] | None = None,
    decision_at: str,
    scan_id: str,
    universe_membership_by_ticker: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Ledger every candidate, including policy rejects that are not tracked.

    V6 outcomes are collected for the tracked shadow cohort.  Rejected rows are
    nevertheless immutable decision evidence and can join regret research only
    through the frozen deterministic sampling policy attached below.
    """

    from intraday_scanner.alpha.v6_shadow import build_v6_shadow_decisions

    tracked = build_v6_shadow_decisions(
        signals=signals,
        feature_vectors=feature_vectors,
        source_summary=source_summary,
        regime=regime,
        prior_outcomes=prior_outcomes,
        frozen_model_run=frozen_model_run,
        universe_membership_by_ticker=universe_membership_by_ticker,
    )
    tracked_tickers = {str(row.get("ticker") or "").upper() for row in tracked}
    feature_by_ticker = {
        str(row.get("ticker") or "").upper(): row for row in feature_vectors
    }
    rejected: list[dict[str, Any]] = []
    memberships = universe_membership_by_ticker or {}
    for candidate in candidates:
        ticker = str(candidate.get("ticker") or "").upper()
        feature = feature_by_ticker.get(ticker)
        if not ticker or feature is None or ticker in tracked_tickers:
            continue
        source_signal_id = "candidate-" + canonical_hash(
            {"scan_id": scan_id, "ticker": ticker}
        )[:24]
        row: dict[str, Any] = {
            "scan_id": scan_id,
            "source_signal_id": source_signal_id,
            "market_date": decision_at[:10],
            "decision_at": decision_at,
            "ticker": ticker,
            "strategy_version": ALPHAOPS_V6_STRATEGY_VERSION,
            "model_version": ALPHAOPS_V6_MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_hash_sha256": canonical_hash(feature),
            "action": "SHADOW_REJECTED_POLICY",
            "decision_state": "REJECTED",
            "policy_rejection_reason": "not_ranked_by_frozen_v5_candidate_policy",
            "setup_key": str(candidate.get("primary_setup") or "unknown"),
            "regime_key": str(regime.get("regime") or "UNKNOWN"),
            "feature_vector": feature,
            "universe_membership": _universe_membership(memberships.get(ticker)),
            "raw_facts": _candidate_facts(candidate),
            "source_summary": source_summary,
            "safety_vetoes": [],
            "estimated_round_trip_cost_bps": _cost_from_feature(feature),
            "cost_model_version": V6_COST_MODEL_VERSION,
            "execution_assumptions": {
                "policy": "sampled_rejected_open_to_close_counterfactual_v1",
                "entry": "first_eligible_regular_session_bar_open",
                "exit": "regular_session_close",
                "bar_interval": "1m",
                "round_trip_cost_bps": _cost_from_feature(feature),
            },
            "point_in_time": {
                "all_inputs_observed_at_or_before_decision": True,
                "decision_timestamp": decision_at,
                "feature_timestamp": feature.get("timestamp"),
            },
            "prediction": {
                "status": "NOT_SCORED_POLICY_REJECTED",
                "utility_lcb_pct": None,
                "research_only": True,
                "broker_execution_enabled": False,
            },
            "score_components": {
                "activation_probability": None,
                "conditional_net_excess_return_pct": None,
                "tail_loss_pct": None,
                "conservative_utility_lcb_pct": None,
            },
            "uncertainty": {
                "status": "NOT_SCORED_POLICY_REJECTED",
                "sample_size": 0,
                "interval_lower_pct": None,
                "interval_upper_pct": None,
            },
            "research_only": True,
            "broker_execution_enabled": False,
        }
        row["input_hash_sha256"] = canonical_hash(row)
        row["source_lineage_hash_sha256"] = canonical_hash(
            {
                "source_summary": source_summary,
                "feature_config_hash": feature.get("config_hash"),
                "feature_timestamp": feature.get("timestamp"),
            }
        )
        row["decision_id"] = "v6d-" + canonical_hash(
            {
                "scan_id": scan_id,
                "source_signal_id": source_signal_id,
                "strategy_version": ALPHAOPS_V6_STRATEGY_VERSION,
            }
        )[:28]
        row["shadow_signal_id"] = "v6s-" + canonical_hash(
            {"decision_id": row["decision_id"]}
        )[:28]
        rejected.append(row)
    rows = attach_rejected_candidate_sampling([*tracked, *rejected])
    if not any(row.get("action") == "SHADOW_TRACK" for row in rows):
        rows.append(
            _no_trade_decision(
                scan_id=scan_id,
                decision_at=decision_at,
                source_summary=source_summary,
                regime=regime,
                memberships=memberships,
                reasons=sorted(
                    {
                        reason
                        for row in rows
                        for reason in list(row.get("safety_vetoes") or [])
                    }
                )
                or ["no_candidate_admitted_by_frozen_shadow_policy"],
            )
        )
    return rows


def _no_trade_decision(
    *,
    scan_id: str,
    decision_at: str,
    source_summary: dict[str, Any],
    regime: dict[str, Any],
    memberships: dict[str, dict[str, Any]],
    reasons: list[str],
) -> dict[str, Any]:
    universe_identity = sorted(
        {
            str(row.get("universe_id") or "")
            for row in memberships.values()
            if row.get("universe_id")
        }
    )
    universe_lineage = canonical_hash(
        sorted(
            str(row.get("source_lineage_hash_sha256") or "")
            for row in memberships.values()
        )
    )
    feature: dict[str, Any] = {}
    row: dict[str, Any] = {
        "scan_id": scan_id,
        "source_signal_id": "no-trade-" + canonical_hash(scan_id)[:24],
        "shadow_signal_id": "v6n-" + canonical_hash(
            {"scan_id": scan_id, "market_date": decision_at[:10]}
        )[:28],
        "market_date": decision_at[:10],
        "decision_at": decision_at,
        "ticker": "NO_TRADE",
        "strategy_version": ALPHAOPS_V6_STRATEGY_VERSION,
        "model_version": ALPHAOPS_V6_MODEL_VERSION,
        "feature_schema_version": FEATURE_SCHEMA_VERSION,
        "feature_hash_sha256": canonical_hash(feature),
        "action": "SHADOW_NO_TRADE",
        "decision_state": "NO_TRADE",
        "setup_key": "no_trade",
        "regime_key": str(regime.get("regime") or "UNKNOWN"),
        "feature_vector": feature,
        "universe_membership": {
            "status": "SESSION_UNIVERSE",
            "universe_id": "+".join(universe_identity) or "unregistered-session-universe",
            "source_lineage_hash_sha256": universe_lineage,
        },
        "source_summary": source_summary,
        "safety_vetoes": reasons,
        "no_trade_reasons": reasons,
        "estimated_round_trip_cost_bps": None,
        "cost_model_version": V6_COST_MODEL_VERSION,
        "execution_assumptions": {"policy": "no_execution", "round_trip_cost_bps": None},
        "point_in_time": {
            "all_inputs_observed_at_or_before_decision": True,
            "decision_timestamp": decision_at,
            "feature_timestamp": decision_at,
        },
        "prediction": {
            "status": "NO_TRADE_SAFETY_FALLBACK",
            "utility_lcb_pct": None,
            "research_only": True,
            "broker_execution_enabled": False,
        },
        "score_components": {
            "activation_probability": None,
            "conditional_net_excess_return_pct": None,
            "tail_loss_pct": None,
            "conservative_utility_lcb_pct": None,
        },
        "uncertainty": {
            "status": "NO_TRADE_SAFETY_FALLBACK",
            "sample_size": 0,
            "interval_lower_pct": None,
            "interval_upper_pct": None,
        },
        "research_only": True,
        "broker_execution_enabled": False,
    }
    row["source_lineage_hash_sha256"] = canonical_hash(
        {"source_summary": source_summary, "universe_lineage": universe_lineage}
    )
    row["input_hash_sha256"] = canonical_hash(row)
    row["decision_id"] = "v6d-" + canonical_hash(
        {
            "scan_id": scan_id,
            "action": "SHADOW_NO_TRADE",
            "strategy_version": ALPHAOPS_V6_STRATEGY_VERSION,
        }
    )[:28]
    return row


def _candidate_facts(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        key: candidate.get(key)
        for key in (
            "ticker",
            "source",
            "source_url",
            "as_of_timestamp",
            "previous_close",
            "premarket_price",
            "premarket_volume",
            "premarket_dollar_volume",
            "spread_pct",
            "current_halt",
            "risk_flags",
            "avoid_reasons",
        )
    }


def _cost_from_feature(feature: dict[str, Any]) -> float | None:
    raw = feature.get("feature_json")
    raw_data = raw if isinstance(raw, dict) else {}
    liquidity = raw_data.get("liquidity_execution")
    liquidity_data = liquidity if isinstance(liquidity, dict) else {}
    try:
        spread = float(str(liquidity_data.get("spread_pct")))
    except (TypeError, ValueError):
        return None
    return round(max(15.0, spread * 125.0 + 10.0), 4)


def _universe_membership(membership: dict[str, Any] | None) -> dict[str, Any]:
    if membership is None:
        return {
            "status": "UNREGISTERED",
            "reason": "no_versioned_universe_registered_for_market_date",
            "missing_truth_is_zero": False,
        }
    return dict(membership)


__all__ = [
    "attach_rejected_candidate_sampling",
    "build_candidate_decisions",
    "validate_decision_batch",
]
