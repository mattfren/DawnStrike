from __future__ import annotations

from copy import deepcopy

from intraday_scanner.alpha.alert_gate import apply_alert_gate
from intraday_scanner.alpha.plan_constructor import construct_alphaops_v5_plan
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACTIVATION_TIMESTAMP,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
    alphaops_strategy_contract,
    evaluate_v5_official_paper,
    is_v5_active,
    v5_execution_cost_model,
)


def test_alert_gate_never_prematurely_marks_a_row_official() -> None:
    row = _clean_signal()
    row.update(
        {
            "current_halt": False,
            "premarket_price": 10.0,
            "premarket_volume": 500_000,
            "risk_flags": "",
            "data_source_kind": "verified_snapshot",
        }
    )

    gated = apply_alert_gate(row)

    assert gated["alert_gate_status"] == "PASS"
    assert gated["official_paper_gate_passed"] is True
    assert gated["official_paper_eligible"] is False
    assert gated["official_paper_eligibility_status"] == "PENDING_V5_EXECUTION_POLICY"


def test_watch_only_alert_gate_is_explicitly_research_only() -> None:
    row = _clean_signal()
    row["source_count"] = 1

    gated = apply_alert_gate(row)

    assert gated["alert_gate_status"] == "WATCH_ONLY"
    assert gated["can_alert"] is False
    assert gated["no_trade_reason"] == "only one source confirmed it"
    assert gated["official_paper_gate_passed"] is False
    assert gated["official_paper_eligible"] is False
    assert gated["official_paper_eligibility_status"] == "RESEARCH_ONLY"


def test_alert_gate_quarantines_the_failed_legacy_signal_profile() -> None:
    row = _clean_signal(ticker="LOSS", gap_pct=86.5, invalidation_level=6.45)
    row.update(
        {
            "can_alert": True,
            "edge_bucket": "LOW",
            "confidence_bucket": "INSUFFICIENT_SAMPLE",
            "setup_grade": "C",
            "source_confidence": 34.5,
            "source_count": 2,
            "data_quality_score": 25.0,
            "catalyst_summary": "No clear catalyst",
            "catalyst_category": "no_clear_catalyst",
            "catalyst_confidence": 0.2,
            "conflict_flags": "volume_conflict",
            "coverage_warning": (
                "url_table_unverified;halt_status_unverified;sec_risk_unverified;"
                "unknown_float"
            ),
            "float_shares": None,
            "data_source_kind": "web_url",
        }
    )

    gated = apply_alert_gate(row)

    assert gated["alert_gate_version"] == "dawnstrike-alert-gate-v2.0.0"
    assert gated["alert_gate_status"] == "BLOCKED"
    assert gated["can_alert"] is False
    assert gated["official_paper_gate_passed"] is False
    reasons = gated["alert_gate_reasons"]
    assert "source conflict unresolved" in reasons
    assert "source confidence below alert threshold" in reasons
    assert "public table identity not verified" in reasons
    assert "gap regime outside alert policy" in reasons
    assert "stop distance exceeds alert policy" in reasons


def test_insufficient_sample_is_research_only_even_with_clean_inputs() -> None:
    row = _clean_signal()
    row["can_alert"] = True
    row["confidence_bucket"] = "INSUFFICIENT_SAMPLE"

    gated = apply_alert_gate(row)

    assert gated["alert_gate_status"] == "WATCH_ONLY"
    assert gated["can_alert"] is False
    assert gated["manual_confirmation_required"] is True
    assert "not enough history yet" in gated["alert_gate_reasons"]


def test_uncalibrated_or_unstable_confidence_cannot_alert() -> None:
    for confidence_bucket in ("", "LOW_SAMPLE", "MISSING_RETURN_TRUTH", "OUTLIER_DEPENDENT"):
        row = _clean_signal()
        row["can_alert"] = True
        row["confidence_bucket"] = confidence_bucket

        gated = apply_alert_gate(row)

        assert gated["alert_gate_status"] == "NO_EDGE"
        assert gated["can_alert"] is False
        assert "confidence evidence below alert threshold" in gated["alert_gate_reasons"]


def test_missing_critical_alert_evidence_fails_closed() -> None:
    for field, reason in (
        ("source_quality_status", "source quality status is not verified clear"),
        ("data_quality_score", "data quality unavailable"),
        ("catalyst_confidence", "catalyst confidence unavailable"),
        ("setup_grade", "setup grade below alert threshold"),
    ):
        row = _clean_signal()
        row["can_alert"] = True
        row.pop(field)

        gated = apply_alert_gate(row)

        assert gated["can_alert"] is False
        assert reason in gated["alert_gate_reasons"]


def test_v5_clean_candidate_is_risk_sized_from_simulated_equity() -> None:
    decision = evaluate_v5_official_paper(
        _clean_signal(),
        _observation(),
        simulated_equity=100_000,
    )

    assert decision.eligible_for_official_paper is True
    assert decision.action == "OFFICIAL_PAPER_ALLOW"
    assert decision.strategy_id == ALPHAOPS_V5_STRATEGY_ID
    assert decision.account_id == "alphaops_v5_simulated"
    assert decision.sizing["shares"] == 216
    assert decision.sizing["proposed_risk"] <= 250
    assert decision.sizing["proposed_notional"] <= 10_000
    assert decision.computed["actual_after_cost_reward_risk"] >= 1.5
    assert decision.feasibility_score == 100
    assert len(decision.decision_fingerprint) == 64
    assert decision.broker_execution_enabled is False


def test_v5_direct_policy_rejects_allowlisted_target_without_strict_plan() -> None:
    signal = _clean_signal()
    signal.pop("alphaops_market_structure_plan")
    signal.pop("plan_hash_sha256")

    decision = evaluate_v5_official_paper(signal, _observation())

    assert decision.eligible_for_official_paper is False
    assert "strict_frozen_plan_missing_or_invalid" in decision.reasons


def test_shadow_receipt_cannot_alert_when_envelope_is_missing_or_ineligible() -> None:
    row = _clean_signal()
    row.update(
        {
            "can_alert": True,
            "strategy_receipt_enabled": True,
            "strategy_receipt_shadow_only": True,
            "strategy_receipt_construction_status": "COMPLETE",
            "strategy_receipt_persistence_status": "PERSISTED",
            "receipt_id": "sdr-" + "a" * 24,
            "receipt_hash_sha256": "a" * 64,
            "strategy_receipt_tier": "QUALIFIED_PICK",
            "strategy_receipt_research_pick_eligible": False,
            "strategy_receipt_paper_entry_eligible": False,
        }
    )

    gated = apply_alert_gate(row)

    assert gated["alert_gate_status"] == "BLOCKED"
    assert gated["can_alert"] is False
    assert "strategy decision receipt unavailable or unauthenticated" in gated[
        "alert_gate_reasons"
    ]


def test_v5_cost_model_is_explicitly_provisional_until_empirical_evidence() -> None:
    model = v5_execution_cost_model()

    assert model.version == "alphaops-v5-cost-model-50bps-0.005ps"
    assert model.entry_slippage_bps == 50.0
    assert model.exit_slippage_bps == 50.0
    assert model.evaluation_status == "NOT_EVALUABLE_PENDING_EMPIRICAL_COST"


def test_v5_watch_only_fallback_and_manual_confirmation_never_enter() -> None:
    fallback = _clean_signal()
    fallback["decision"] = "probability_fallback"
    fallback["classification"] = "WATCH ONLY"
    fallback["manual_confirmation_required"] = True
    fallback["alert_gate_status"] = "WATCH_ONLY"

    decision = evaluate_v5_official_paper(fallback, _observation())

    assert decision.eligible_for_official_paper is False
    assert "selection_not_clean_edge" in decision.reasons
    assert "alert_gate_not_pass" in decision.reasons
    assert "manual_confirmation_required" in decision.reasons
    assert "research_only_selection_tier" in decision.reasons


def test_v5_actual_after_cost_r_below_1_5_cannot_enter() -> None:
    signal = _clean_signal()
    signal["target_1"] = 11.75

    decision = evaluate_v5_official_paper(signal, _observation())

    assert decision.computed["gross_reward_risk"] > 1.5
    assert decision.computed["actual_after_cost_reward_risk"] < 1.5
    assert decision.eligible_for_official_paper is False
    assert "after_cost_reward_risk_below_policy" in decision.reasons


def test_v5_missing_critical_evidence_cannot_enter() -> None:
    signal = _clean_signal()
    signal["float_shares"] = None
    signal["float_source"] = ""
    signal["sec_risk_status"] = ""
    signal["catalyst_url"] = ""

    decision = evaluate_v5_official_paper(signal, _observation())

    assert decision.eligible_for_official_paper is False
    assert "float_evidence_missing" in decision.reasons
    assert "sec_risk_status_unknown_or_blocked" in decision.reasons
    assert "catalyst_evidence_missing_or_weak" in decision.reasons


def test_biya_july_20_regression_is_blocked_by_gap_and_stop_policy() -> None:
    signal = _clean_signal(
        ticker="BIYA",
        gap_pct=151.32,
        entry_watch_level=10.0,
        invalidation_level=5.78,
        target_1=17.25,
    )
    observation = _observation(price=10.05)

    decision = evaluate_v5_official_paper(signal, observation)

    assert decision.eligible_for_official_paper is False
    assert decision.computed["gap_pct"] == 151.32
    assert decision.computed["stop_distance_pct"] > 40
    assert "gap_regime_outside_policy" in decision.reasons
    assert "stop_distance_exceeds_policy" in decision.reasons


def test_slnd_regression_cannot_enter_after_regular_close() -> None:
    signal = _clean_signal(ticker="SLND")
    observation = _observation(
        requested_at="2026-07-31T16:01:00-04:00",
        observed_at="2026-07-31T16:00:00-04:00",
        freshness_seconds=60,
    )

    decision = evaluate_v5_official_paper(signal, observation)

    assert decision.eligible_for_official_paper is False
    assert "entry_outside_registered_session" in decision.reasons


def test_skyq_regression_cannot_enter_after_v5_cutoff() -> None:
    signal = _clean_signal(ticker="SKYQ")
    observation = _observation(
        requested_at="2026-07-31T15:55:00-04:00",
        observed_at="2026-07-31T15:54:00-04:00",
        freshness_seconds=60,
    )

    decision = evaluate_v5_official_paper(signal, observation)

    assert decision.eligible_for_official_paper is False
    assert "entry_outside_registered_session" in decision.reasons


def test_v5_target_must_be_independent_not_a_manufactured_r_multiple() -> None:
    signal = _clean_signal()
    signal["target_basis_kind"] = "risk_multiple"
    signal["target_derived_from_risk"] = True

    decision = evaluate_v5_official_paper(signal, _observation())

    assert decision.eligible_for_official_paper is False
    assert "target_not_independently_derived" in decision.reasons


def test_v5_decision_trace_is_deterministic_and_does_not_mutate_v4_payload() -> None:
    signal = _clean_signal()
    immutable_copy = deepcopy(signal)

    first = evaluate_v5_official_paper(signal, _observation())
    second = evaluate_v5_official_paper(signal, _observation())

    assert first.decision_fingerprint == second.decision_fingerprint
    assert signal == immutable_copy


def test_strategy_contract_switches_only_at_prospective_activation() -> None:
    assert is_v5_active("2026-07-30T23:59:59-04:00") is False
    assert is_v5_active(ALPHAOPS_V5_ACTIVATION_TIMESTAMP) is True
    assert alphaops_strategy_contract("2026-07-30T23:59:59-04:00") == (
        "alphaops_v4",
        "dawnstrike-alphaops-v4",
    )
    assert alphaops_strategy_contract(ALPHAOPS_V5_ACTIVATION_TIMESTAMP) == (
        ALPHAOPS_V5_STRATEGY_ID,
        ALPHAOPS_V5_STRATEGY_VERSION,
    )


def _clean_signal(
    *,
    ticker: str = "NOVA",
    gap_pct: float = 25.0,
    entry_watch_level: float = 10.0,
    invalidation_level: float = 9.0,
    target_1: float = 12.75,
) -> dict[str, object]:
    signal: dict[str, object] = {
        "signal_id": f"sig-{ticker}",
        "selection_id": f"selection-{ticker}",
        "ticker": ticker,
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "decision": "clean_edge",
        "decision_tier": "clean_edge",
        "alert_gate_status": "PASS",
        "manual_confirmation_required": False,
        "classification": "TRADE SETUP",
        "review_label": "",
        "source_confidence": 92.0,
        "source_count": 3,
        "source_quality_status": "verified",
        "data_quality_score": 90.0,
        "stale_data_flag": False,
        "previous_close": 8.0,
        "premarket_price": 10.0,
        "premarket_high": 10.1,
        "premarket_low": 9.6,
        "premarket_volume": 500_000,
        "dollar_volume": 5_000_000,
        "gap_pct": gap_pct,
        "spread_pct": 0.5,
        "liquidity_tier": "high_liquidity",
        "float_shares": 8_000_000,
        "float_status": "verified",
        "float_source": "stockanalysis_public_table",
        "catalyst_summary": "FDA clearance announced before market open",
        "catalyst_url": "https://example.test/catalyst",
        "catalyst_status": "verified",
        "catalyst_tier": "A",
        "catalyst_confidence": 0.9,
        "halt_status": "clear",
        "sec_risk_status": "clear",
        "corporate_action_status": "clear",
        "edge_bucket": "MEDIUM",
        "confidence_bucket": "MEDIUM",
        "setup_grade": "A",
        "entry_watch_level": entry_watch_level,
        "invalidation_level": invalidation_level,
        "target_1": target_1,
        "target_basis_kind": "sourced_resistance",
        "target_basis_value": target_1,
        "target_basis_source": "premarket_structure_fixture",
        "target_derived_from_risk": False,
        "market_structure_observations": {
            "entry": _plan_observation(entry_watch_level, "a" * 64, "sourced_entry"),
            "stop": _plan_observation(invalidation_level, "b" * 64, "sourced_stop"),
            "target": {
                **_plan_observation(target_1, "c" * 64, "prior_day_resistance"),
                "target_basis_kind": "sourced_resistance",
            },
        },
    }
    plan = construct_alphaops_v5_plan(
        signal,
        decision_at="2026-07-31T13:30:00+00:00",
    )
    signal["alphaops_market_structure_plan"] = plan.to_dict()
    signal["plan_hash_sha256"] = plan.plan_hash_sha256
    signal["direction"] = plan.direction
    signal["target_basis_kind"] = plan.target_basis_kind
    signal["plan_levels_frozen"] = True
    signal["plan_construction_status"] = plan.status
    return signal


def _plan_observation(
    value: float, source_hash: str, observation_kind: str
) -> dict[str, object]:
    return {
        "value": value,
        "raw_value": value,
        "observed_at": "2026-07-31T13:00:00+00:00",
        "completed_at": "2026-07-31T13:00:00+00:00",
        "source": "completed-market-feed",
        "source_url": "https://example.test/market",
        "source_hash": source_hash,
        "observation_kind": observation_kind,
        "derivation_policy": "identity",
        "is_complete": True,
    }


def _observation(
    *,
    price: float = 10.05,
    requested_at: str = "2026-07-31T10:00:00-04:00",
    observed_at: str = "2026-07-31T09:59:30-04:00",
    freshness_seconds: int = 30,
) -> dict[str, object]:
    return {
        "observation_id": "observation-NOVA",
        "ticker": "NOVA",
        "requested_at": requested_at,
        "observed_at": observed_at,
        "price": price,
        "freshness_seconds": freshness_seconds,
        "is_usable": True,
        "provider_status": "fresh_prior_bar",
    }
