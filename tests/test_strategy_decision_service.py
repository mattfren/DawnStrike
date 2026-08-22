from __future__ import annotations

import pytest

from intraday_scanner.decisioning.condition_registry import registry_for_strategy
from intraday_scanner.services.strategy_decision_service import StrategyDecisionService


def _candidate(
    strategy_id: str = "ts_momentum_sma_atr",
) -> tuple[StrategyDecisionService, dict, dict[str, bool]]:
    service = StrategyDecisionService(
        code_sha="b" * 40,
        source_identity="fixture-source",
        score_threshold=50,
    )
    candidate = {
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "TEST",
        "market_date": "2026-08-22",
        "score": 80,
        "entry_reference": 10,
        "stop": 9,
        "target": 12,
        "reward_risk_ratio": 2,
    }
    values = {row.condition_id: True for row in registry_for_strategy(strategy_id)}
    return service, candidate, values


def test_missing_advisory_fact_allows_disclosed_gap_pick() -> None:
    service, candidate, values = _candidate("ts_momentum_sma_atr")
    values["catalyst_identified"] = None
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "PICK_WITH_DISCLOSED_GAPS"
    assert receipt.research_pick_eligible is True


def test_missing_borrow_is_conditional_and_not_paper_entry() -> None:
    service, candidate, values = _candidate("failed_breakout_reversal_short")
    values["borrow_or_locate_verified"] = None
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "CONDITIONAL_PICK"
    assert receipt.research_pick_eligible is True
    assert receipt.paper_entry_eligible is False


def test_hard_risk_failure_blocks_safety() -> None:
    service, candidate, values = _candidate()
    values["not_currently_halted"] = False
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "BLOCKED_SAFETY"
    assert receipt.research_pick_eligible is False


def test_missing_hard_market_fact_blocks_data() -> None:
    service, candidate, values = _candidate()
    values["positive_current_price"] = None
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "BLOCKED_DATA"
    assert receipt.first_blocking_failure == "positive_current_price"


def test_reward_risk_floor_remains_a_safety_gate() -> None:
    service, candidate, values = _candidate()
    candidate["reward_risk_ratio"] = 1.49
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "BLOCKED_SAFETY"
    assert "reward_risk_at_least_1_50" in receipt.all_blocking_failures


def test_not_applicable_cannot_override_hard_risk() -> None:
    service, candidate, values = _candidate()
    values["not_currently_halted"] = {"status": "NOT_APPLICABLE"}
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "BLOCKED_SAFETY"
    assert receipt.research_pick_eligible is False


def test_ai_resolver_cannot_override_strategy_core_or_risk() -> None:
    service, candidate, values = _candidate()
    values["valid_stop_geometry"] = {
        "status": "PASS",
        "resolver_id": "strategy_gap_resolver",
    }
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "BLOCKED_SAFETY"
    assert receipt.research_pick_eligible is False


def test_missing_score_is_not_converted_to_zero() -> None:
    service, candidate, values = _candidate()
    candidate.pop("score")
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "BLOCKED_DATA"
    assert receipt.research_pick_eligible is False
    assert "deterministic_score_missing" in receipt.all_blocking_failures


def test_existing_score_adjustment_and_final_score_are_preserved() -> None:
    service, candidate, values = _candidate()
    candidate.update({"score_adjustment": -5, "final_score": 75})
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.base_strategy_score == 80
    assert receipt.score_adjustment == -5
    assert receipt.final_score == 75


def test_target_not_required_is_explicitly_not_applicable() -> None:
    service, candidate, values = _candidate()
    candidate["target_required"] = False
    values["valid_target_when_required"] = None
    receipt = service.build_receipt(candidate, condition_overrides=values)
    target_result = next(
        row for row in receipt.condition_results if row.condition_id == "valid_target_when_required"
    )
    assert target_result.status.value == "NOT_APPLICABLE"
    assert receipt.research_pick_eligible is True


def test_fvg_daily_proxy_cannot_establish_paper_entry() -> None:
    service, candidate, values = _candidate("bullish_fvg_continuation")
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.research_pick_eligible is True
    assert receipt.paper_entry_eligible is False
    assert receipt.pick_tier.value == "CONDITIONAL_PICK"
    assert "daily_ohlc_proxy" in receipt.disclosed_gaps


def test_invalid_identity_and_non_research_receipts_fail_closed() -> None:
    service, candidate, values = _candidate()
    candidate["symbol"] = "not a ticker"
    with pytest.raises(ValueError, match="symbol"):
        service.build_receipt(candidate, condition_overrides=values)
    candidate["symbol"] = "TEST"
    with pytest.raises(ValueError, match="research-only"):
        service.build_receipt(candidate, condition_overrides=values, research_only=False)


def test_unknown_cross_sectional_sector_is_conditional_unknown_bucket() -> None:
    service, candidate, values = _candidate("cross_sectional_relative_strength")
    values["sector_industry"] = None
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "CONDITIONAL_PICK"
    assert "sector_industry:UNKNOWN" in receipt.disclosed_gaps


def test_unknown_sector_cohort_allows_only_one_conditional_candidate() -> None:
    service, candidate, values = _candidate("cross_sectional_relative_strength")
    values["sector_industry"] = None
    second = dict(candidate)
    second["symbol"] = "TEST2"
    receipts = service.evaluate_candidates(
        [candidate, second],
        condition_overrides={"TEST": values, "TEST2": values},
        decision_at="2026-08-22T14:30:00+00:00",
    )
    assert receipts[0].pick_tier.value == "CONDITIONAL_PICK"
    assert receipts[1].pick_tier.value == "WATCH_ONLY"
    assert receipts[1].research_pick_eligible is False
    assert receipts[1].first_blocking_failure == "sector_industry:UNKNOWN_COHORT_LIMIT"


def test_gap_corporate_action_uncertainty_is_conditional() -> None:
    service, candidate, values = _candidate("gap_up_continuation")
    values["corporate_action_basis"] = None
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "CONDITIONAL_PICK"
    assert receipt.research_pick_eligible is True
    assert receipt.paper_entry_eligible is False
    assert "corporate_action_basis" in receipt.disclosed_gaps


def test_confirmed_split_basis_cannot_be_ordinary_gap_continuation() -> None:
    service, candidate, values = _candidate("gap_up_continuation")
    values["corporate_action_basis"] = False
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "BLOCKED_DATA"
    assert receipt.research_pick_eligible is False
    assert "corporate_action_basis" in receipt.all_blocking_failures
