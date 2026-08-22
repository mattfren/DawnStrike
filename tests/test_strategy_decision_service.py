from __future__ import annotations

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


def test_unknown_cross_sectional_sector_is_conditional_unknown_bucket() -> None:
    service, candidate, values = _candidate("cross_sectional_relative_strength")
    values["sector_industry"] = None
    receipt = service.build_receipt(candidate, condition_overrides=values)
    assert receipt.pick_tier.value == "CONDITIONAL_PICK"
    assert "sector_industry:UNKNOWN" in receipt.disclosed_gaps
