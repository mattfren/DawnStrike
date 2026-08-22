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
    assert rows["intraday_microstructure_confirmed"].blocking_for_paper_entry is True
