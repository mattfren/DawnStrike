from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from intraday_scanner.v2.backtest import BacktestEngine, BacktestSettings
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.paper_ops.lifecycle_backtest import (
    PaperOpsLifecycleBacktestEngine,
)
from intraday_scanner.v2.paper_ops.models import PaperOpsConfig
from intraday_scanner.v2.strategies import build_strategy_catalog


def _bars(symbol: str, prices: tuple[float, ...]) -> tuple[MarketBar, ...]:
    return tuple(
        MarketBar(
            symbol=symbol,
            timestamp=datetime(2026, 1, 1, 21, tzinfo=timezone.utc)
            + timedelta(days=index),
            open=price,
            high=price * 1.01,
            low=price * 0.99,
            close=price,
            volume=1_000_000,
        )
        for index, price in enumerate(prices)
    )


def _dataset() -> MarketDataset:
    return MarketDataset(
        dataset_id="comparator-fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "AAA": _bars("AAA", (100.0, 100.0, 100.0, 110.0)),
            "BBB": _bars("BBB", (50.0, 50.0, 50.0, 45.0)),
        },
    )


def _catalog_strategy(strategy_id: str):
    return next(
        strategy
        for strategy in build_strategy_catalog()
        if strategy.strategy_id == strategy_id
    )


def test_equal_weight_benchmark_uses_next_bar_and_holds_to_end() -> None:
    benchmark = _catalog_strategy("benchmark_buy_hold_equal_weight")
    benchmark = replace(benchmark, parameters={**benchmark.parameters, "start_index": 1})

    result = BacktestEngine(
        BacktestSettings(fee_bps=0.0, slippage_bps=0.0)
    ).run(benchmark, _dataset())

    assert len(result.trades) == 2
    assert {trade.exit_reason for trade in result.trades} == {
        "end_of_test_liquidation"
    }
    assert {trade.entry_time for trade in result.trades} == {
        datetime(2026, 1, 3, 21, tzinfo=timezone.utc)
    }
    assert result.metrics["final_equity"] == pytest.approx(100_000.0)
    assert result.metrics["total_return_pct"] == pytest.approx(0.0)
    assert result.metrics["trade_count"] == 2


def test_cash_baseline_remains_zero_return_without_trades() -> None:
    result = BacktestEngine().run(_catalog_strategy("cash_no_trade_baseline"), _dataset())

    assert result.trades == ()
    assert result.metrics["final_equity"] == 100_000.0
    assert result.metrics["total_return_pct"] == 0.0
    assert result.metrics["trade_count"] == 0


def test_paper_ops_lifecycle_routes_comparators_to_dedicated_semantics() -> None:
    benchmark = _catalog_strategy("benchmark_buy_hold_equal_weight")
    benchmark = replace(benchmark, parameters={**benchmark.parameters, "start_index": 1})
    cash = _catalog_strategy("cash_no_trade_baseline")
    results = PaperOpsLifecycleBacktestEngine(
        PaperOpsConfig(universe_symbols=("AAA", "BBB"))
    ).run((benchmark, cash), _dataset())

    assert results[benchmark.strategy_id].metrics["execution_model"] == (
        "dedicated_comparator"
    )
    assert results[benchmark.strategy_id].metrics["trade_count"] == 2
    assert results[cash.strategy_id].metrics["execution_model"] == (
        "dedicated_comparator"
    )
    assert results[cash.strategy_id].metrics["trade_count"] == 0
