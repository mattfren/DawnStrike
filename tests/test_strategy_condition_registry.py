from intraday_scanner.decisioning.condition_registry import (
    build_condition_registry,
    registry_for_strategy,
    strategy_ids,
)
from intraday_scanner.decisioning.contracts import ConditionCategory


def test_registry_contains_all_nine_strategies() -> None:
    assert len(strategy_ids()) == 9
    assert {row.strategy_id for row in build_condition_registry()} == set(strategy_ids())
    assert all(registry_for_strategy(strategy) for strategy in strategy_ids())


def test_registry_contains_common_and_strategy_specific_classes() -> None:
    rows = registry_for_strategy("gap_up_continuation_atr")
    categories = {row.category for row in rows}
    assert ConditionCategory.HARD_MARKET in categories
    assert ConditionCategory.HARD_RISK in categories
    assert ConditionCategory.STRATEGY_CORE in categories
    assert ConditionCategory.AI_RESOLVABLE in categories
    assert "atr_normalization_valid" in {row.condition_id for row in rows}


def test_fvg_daily_proxy_is_disclosed_and_not_execution_proof() -> None:
    rows = {row.condition_id: row for row in registry_for_strategy("bullish_fvg_continuation")}
    assert rows["daily_ohlc_proxy"].category == ConditionCategory.ADVISORY
    assert rows["daily_ohlc_proxy"].blocking_for_paper_entry is True
    assert rows["intraday_microstructure_confirmed"].blocking_for_paper_entry is True


def test_registry_missing_policies_preserve_tier_semantics() -> None:
    short_rows = {
        row.condition_id: row
        for row in registry_for_strategy("failed_breakout_reversal_short")
    }
    assert short_rows["borrow_or_locate_verified"].missing_policy == "CONDITIONAL_PICK"

    gap_rows = {row.condition_id: row for row in registry_for_strategy("gap_up_continuation")}
    assert gap_rows["corporate_action_basis"].missing_policy == "CONDITIONAL_PICK"
    assert gap_rows["corporate_action_basis"].blocking_for_research_pick is False

    risk_rows = {
        row.condition_id: row
        for row in gap_rows.values()
        if row.category == ConditionCategory.HARD_RISK
    }
    assert risk_rows["reward_risk_at_least_1_50"].threshold_contract == {
        "operator": ">=",
        "minimum_reward_risk": 1.50,
    }
