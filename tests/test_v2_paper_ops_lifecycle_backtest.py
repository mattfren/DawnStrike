from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta, timezone

import pytest

from intraday_scanner.v2.backtest import BacktestEngine
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.paper_ops.engine import (
    _apply_close,
    _backtest_results,
    _check_position,
    _fill_entry_block_reason,
    _fill_order,
    _order_entry_block_reason,
    _order_from_pick,
    _position_from_fill,
    _recalculate_unrealized_accounts,
)
from intraday_scanner.v2.paper_ops.lifecycle_backtest import (
    PaperOpsLifecycleBacktestEngine,
)
from intraday_scanner.v2.paper_ops.models import (
    PaperCloseReason,
    PaperOpsConfig,
    PaperOrder,
    StrategyPaperAccount,
)
from intraday_scanner.v2.strategies import (
    Direction,
    StrategySignal,
    StrategySpec,
    build_strategy_catalog,
)


def test_historical_adapter_matches_canonical_paper_ops_lifecycle() -> None:
    dataset = _golden_dataset()
    strategies = (_strategy_a(), _strategy_b())
    config = PaperOpsConfig(
        starting_equity=100_000.0,
        risk_per_trade_pct=0.01,
        max_daily_loss_pct=0.015,
        max_open_risk_pct=0.02,
        max_gross_exposure_pct=1.0,
        max_concurrent_positions=1,
        min_reward_risk=1.0,
        fee_bps=1.0,
        slippage_bps=5.0,
        universe_symbols=dataset.symbols,
    )

    engine = PaperOpsLifecycleBacktestEngine(config)
    results = engine.run(strategies, dataset)
    audit = engine.audit
    assert audit is not None

    # The scan-card path is explicitly routed to this lifecycle, while the
    # standalone Alpha Lab engine retains its existing terminal liquidation.
    assert _backtest_results(dataset, strategies, config) == results
    assert results["strategy_a"].metrics["execution_model"] == "paper_ops_lifecycle"
    legacy = BacktestEngine().run(strategies[1], dataset)
    assert legacy.trades[-1].exit_reason == "end_of_test_liquidation"

    assert [pick.symbol for pick in audit.picks] == ["AAA", "BBB", "CCC", "DDD"]
    assert [order.symbol for order in audit.orders_created] == ["AAA", "CCC", "DDD"]
    assert [fill.symbol for fill in audit.fills] == ["AAA", "CCC"]
    assert [close.symbol for close in audit.closes] == ["AAA"]

    picks = {pick.symbol: pick for pick in audit.picks}
    orders = {order.symbol: order for order in audit.orders_created}
    fills = {fill.symbol: fill for fill in audit.fills}
    runs = audit.runs

    # Signal-close sizing is frozen by the production order constructor. The
    # next lower-scored card is rejected against the first pending reservation.
    expected_aaa_order = _order_from_pick(
        picks["AAA"],
        runs[0],
        config,
        _dataset_through(dataset, 0),
        equity_basis=config.starting_equity,
    )
    assert orders["AAA"] == expected_aaa_order
    expected_bbb_order = _order_from_pick(
        picks["BBB"],
        runs[0],
        config,
        _dataset_through(dataset, 0),
        equity_basis=config.starting_equity,
    )
    expected_entry_block = _order_entry_block_reason(
        expected_bbb_order,
        position_rows=[],
        pending_rows=[expected_aaa_order.to_dict()],
        account=_initial_account(expected_aaa_order, config),
        config=config,
        daily_closed_net=0.0,
    )
    assert expected_entry_block == "max_concurrent_positions"
    assert [(row["symbol"], row["reason"]) for row in audit.entry_blocks] == [
        ("BBB", expected_entry_block)
    ]

    # The canonical fill retains the frozen quantity. On a bar touching both
    # stop and target, the production position checker closes stop-first.
    expected_aaa_fill = _fill_order(
        expected_aaa_order,
        dataset.bars_by_symbol["AAA"][1],
        runs[1],
        config,
    )
    assert fills["AAA"] == expected_aaa_fill
    assert expected_aaa_fill.quantity == expected_aaa_order.quantity
    expected_aaa_position = _position_from_fill(
        expected_aaa_order,
        expected_aaa_fill,
    )
    assert (
        _fill_entry_block_reason(
            expected_aaa_order,
            fill=expected_aaa_fill,
            position=expected_aaa_position,
            fill_bar=dataset.bars_by_symbol["AAA"][1],
            position_rows=[],
            pending_rows=[],
            account=_initial_account(expected_aaa_order, config),
            config=config,
            daily_closed_net=0.0,
        )
        is None
    )
    _, expected_aaa_close = _check_position(
        expected_aaa_position,
        dataset.bars_by_symbol["AAA"][1],
        runs[1],
        config,
    )
    assert expected_aaa_close is not None
    assert expected_aaa_close.close_reason is PaperCloseReason.STOP
    assert audit.closes == (expected_aaa_close,)

    expected_a_accounts = _apply_close(
        {"strategy_a": _initial_account(expected_aaa_order, config)},
        expected_aaa_close,
    )
    expected_a_accounts = _recalculate_unrealized_accounts(expected_a_accounts, [])

    # A large next-open gap increases actual stop risk. PaperOps rejects the
    # entire frozen order instead of silently resizing it at the fill.
    expected_ddd_order = _order_from_pick(
        picks["DDD"],
        runs[2],
        config,
        _dataset_through(dataset, 2),
        equity_basis=expected_a_accounts["strategy_a"].current_equity,
    )
    assert orders["DDD"] == expected_ddd_order
    expected_ddd_fill = _fill_order(
        expected_ddd_order,
        dataset.bars_by_symbol["DDD"][3],
        runs[3],
        config,
    )
    expected_ddd_position = _position_from_fill(expected_ddd_order, expected_ddd_fill)
    expected_fill_block = _fill_entry_block_reason(
        expected_ddd_order,
        fill=expected_ddd_fill,
        position=expected_ddd_position,
        fill_bar=dataset.bars_by_symbol["DDD"][3],
        position_rows=[],
        pending_rows=[],
        account=expected_a_accounts["strategy_a"],
        config=config,
        daily_closed_net=0.0,
    )
    assert expected_fill_block == "fill_risk_budget_exceeded"
    assert [(row["symbol"], row["reason"]) for row in audit.fill_blocks] == [
        ("DDD", expected_fill_block)
    ]
    assert expected_ddd_fill.quantity == expected_ddd_order.quantity

    # The second strategy remains open and is marked using the same production
    # checker/account functions. It is deliberately not liquidated at test end.
    expected_ccc_order = _order_from_pick(
        picks["CCC"],
        runs[0],
        config,
        _dataset_through(dataset, 0),
        equity_basis=config.starting_equity,
    )
    expected_ccc_fill = _fill_order(
        expected_ccc_order,
        dataset.bars_by_symbol["CCC"][1],
        runs[1],
        config,
    )
    expected_ccc_position = _position_from_fill(expected_ccc_order, expected_ccc_fill)
    for run, bar in zip(runs[1:], dataset.bars_by_symbol["CCC"][1:], strict=True):
        expected_ccc_position, close_record = _check_position(
            expected_ccc_position,
            bar,
            run,
            config,
        )
        assert close_record is None
    expected_b_accounts = _recalculate_unrealized_accounts(
        {"strategy_b": _initial_account(expected_ccc_order, config)},
        [expected_ccc_position.to_dict()],
    )

    assert audit.final_pending_orders == ()
    assert audit.final_open_positions == (expected_ccc_position,)
    final_accounts = {account.strategy_id: account for account in audit.final_accounts}
    assert final_accounts["strategy_a"] == expected_a_accounts["strategy_a"]
    assert final_accounts["strategy_b"] == expected_b_accounts["strategy_b"]

    result_a = results["strategy_a"]
    result_b = results["strategy_b"]
    assert result_a.trades[0].quantity == expected_aaa_order.quantity
    assert result_a.trades[0].exit_reason == PaperCloseReason.STOP.value
    assert result_a.trades[0].net_pnl == pytest.approx(expected_aaa_close.net_pnl)
    assert result_a.equity_curve[-1].equity == pytest.approx(
        expected_a_accounts["strategy_a"].current_equity
    )
    assert result_b.trades == ()
    assert result_b.equity_curve[-1].equity == pytest.approx(
        expected_b_accounts["strategy_b"].current_equity
    )
    assert result_b.equity_curve[-1].open_positions == 1
    assert result_b.metrics["end_of_test_liquidations"] == 0
    assert result_b.metrics["open_positions_marked_not_liquidated"] == 1
    assert result_b.metrics["fees_paid"] == pytest.approx(expected_ccc_fill.fee)
    assert result_b.metrics["slippage_estimate"] == pytest.approx(
        expected_ccc_fill.slippage
    )


def test_historical_adapter_uses_canonical_ten_calendar_day_timeout() -> None:
    dataset = _timeout_dataset()
    strategy = _timeout_strategy()
    config = PaperOpsConfig(
        min_reward_risk=1.0,
        universe_symbols=("TMO",),
    )

    engine = PaperOpsLifecycleBacktestEngine(config)
    result = engine.run((strategy,), dataset)[strategy.strategy_id]
    audit = engine.audit
    assert audit is not None

    expected_order = _order_from_pick(
        audit.picks[0],
        audit.runs[0],
        config,
        _dataset_through(dataset, 0),
        equity_basis=config.starting_equity,
    )
    expected_fill = _fill_order(
        expected_order,
        dataset.bars_by_symbol["TMO"][1],
        audit.runs[1],
        config,
    )
    expected_position = _position_from_fill(expected_order, expected_fill)
    expected_position, first_close = _check_position(
        expected_position,
        dataset.bars_by_symbol["TMO"][1],
        audit.runs[1],
        config,
    )
    assert first_close is None
    _, expected_close = _check_position(
        expected_position,
        dataset.bars_by_symbol["TMO"][2],
        audit.runs[2],
        config,
    )

    assert expected_close is not None
    assert expected_close.close_reason is PaperCloseReason.TIMEOUT
    assert audit.closes == (expected_close,)
    assert result.trades[0].exit_reason == PaperCloseReason.TIMEOUT.value
    assert result.metrics["open_position_count"] == 0


def test_fingerprint_mismatch_falls_back_to_prefix_only_signal_input() -> None:
    dataset = _golden_dataset()
    template = next(
        strategy
        for strategy in build_strategy_catalog()
        if strategy.strategy_id == "gap_up_continuation_atr"
    )
    observed: list[tuple[int, int]] = []

    def prefix_probe(
        strategy: StrategySpec,
        as_of_dataset: MarketDataset,
        symbol: str,
        bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        del strategy, symbol
        observed.append((len(bars), index))
        assert len(bars) == index + 1
        assert all(
            len(symbol_bars) == index + 1
            for symbol_bars in as_of_dataset.bars_by_symbol.values()
        )
        return None

    changed = replace(
        template,
        entry_logic=f"{template.entry_logic} fingerprint mismatch fixture",
        generate_signal=prefix_probe,
    )
    engine = PaperOpsLifecycleBacktestEngine(
        PaperOpsConfig(universe_symbols=dataset.symbols)
    )

    result = engine.run((changed,), dataset)[changed.strategy_id]

    assert result.trades == ()
    assert len(observed) == len(dataset.symbols) * 4


def _initial_account(
    order: PaperOrder,
    config: PaperOpsConfig,
) -> StrategyPaperAccount:
    return StrategyPaperAccount(
        strategy_id=order.strategy_id,
        strategy_version=order.strategy_version,
        starting_equity=config.starting_equity,
        current_equity=config.starting_equity,
        realized_pnl=0.0,
        unrealized_pnl=0.0,
        execution_policy_version=order.execution_policy_version,
        strategy_semantics_fingerprint=order.strategy_semantics_fingerprint,
    )


def _strategy_a() -> StrategySpec:
    def signal(
        strategy: StrategySpec,
        _dataset: MarketDataset,
        symbol: str,
        _bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        setup = {
            (0, "AAA"): (100.0, 90.0, 120.0, 90.0),
            (0, "BBB"): (100.0, 90.0, 120.0, 80.0),
            (2, "DDD"): (100.0, 99.0, 140.0, 85.0),
        }.get((index, symbol))
        if setup is None:
            return None
        entry, stop, target, score = setup
        return StrategySignal(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=entry,
            stop=stop,
            target=target,
            score=score,
            evidence=(f"golden:{symbol}:{index}",),
            invalidation="golden fixture",
        )

    return _strategy("strategy_a", signal)


def _strategy_b() -> StrategySpec:
    def signal(
        strategy: StrategySpec,
        _dataset: MarketDataset,
        symbol: str,
        _bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        if (index, symbol) != (0, "CCC"):
            return None
        return StrategySignal(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=100.0,
            stop=90.0,
            target=120.0,
            score=70.0,
            evidence=("golden:CCC:0",),
            invalidation="golden fixture",
        )

    return _strategy("strategy_b", signal)


def _timeout_strategy() -> StrategySpec:
    def signal(
        strategy: StrategySpec,
        _dataset: MarketDataset,
        symbol: str,
        _bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        if (index, symbol) != (0, "TMO"):
            return None
        return StrategySignal(
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=100.0,
            stop=90.0,
            target=120.0,
            score=50.0,
            evidence=("golden:TMO:timeout",),
            invalidation="golden fixture",
        )

    return _strategy("timeout_strategy", signal)


def _strategy(strategy_id: str, signal: object) -> StrategySpec:
    return StrategySpec(
        strategy_id=strategy_id,
        version="v1",
        status="production",
        description="PaperOps lifecycle golden fixture",
        compatible_timeframe="1d",
        required_data_fields=("open", "high", "low", "close", "volume"),
        parameters={},
        indicators=(),
        entry_logic="fixture",
        exit_logic="fixture",
        stop_logic="fixture",
        target_logic="fixture",
        position_sizing_assumption="PaperOps risk sizing",
        known_failure_modes=(),
        validation_status="golden",
        generate_signal=signal,  # type: ignore[arg-type]
    )


def _golden_dataset() -> MarketDataset:
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    rows = {
        "AAA": (
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 121.0, 89.0, 100.0),
            (100.0, 102.0, 98.0, 101.0),
            (101.0, 103.0, 100.0, 102.0),
        ),
        "BBB": (
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 103.0, 97.0, 101.0),
            (101.0, 103.0, 99.0, 102.0),
            (102.0, 104.0, 101.0, 103.0),
        ),
        "CCC": (
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 105.0, 95.0, 104.0),
            (104.0, 107.0, 100.0, 106.0),
            (106.0, 110.0, 103.0, 108.0),
        ),
        "DDD": (
            (100.0, 101.0, 99.0, 100.0),
            (100.0, 102.0, 99.0, 101.0),
            (100.0, 102.0, 99.0, 100.0),
            (110.0, 115.0, 105.0, 112.0),
        ),
    }
    return MarketDataset(
        dataset_id="paper-ops-lifecycle-golden",
        source_kind="test_fixture",
        timeframe="1d",
        bars_by_symbol={
            symbol: tuple(
                MarketBar(
                    symbol=symbol,
                    timestamp=start + timedelta(days=index),
                    open=values[0],
                    high=values[1],
                    low=values[2],
                    close=values[3],
                    volume=1_000_000,
                )
                for index, values in enumerate(symbol_rows)
            )
            for symbol, symbol_rows in rows.items()
        },
    )


def _dataset_through(dataset: MarketDataset, index: int) -> MarketDataset:
    return MarketDataset(
        dataset_id=dataset.dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol={
            symbol: bars[: index + 1]
            for symbol, bars in dataset.bars_by_symbol.items()
        },
        source_path=dataset.source_path,
        warnings=dataset.warnings,
        source_refs=dataset.source_refs,
    )


def _timeout_dataset() -> MarketDataset:
    start = datetime(2026, 1, 5, tzinfo=timezone.utc)
    dates = (start, start + timedelta(days=1), start + timedelta(days=11))
    values = (
        (100.0, 101.0, 99.0, 100.0),
        (100.0, 105.0, 95.0, 101.0),
        (102.0, 105.0, 95.0, 103.0),
    )
    return MarketDataset(
        dataset_id="paper-ops-timeout-golden",
        source_kind="test_fixture",
        timeframe="1d",
        bars_by_symbol={
            "TMO": tuple(
                MarketBar(
                    symbol="TMO",
                    timestamp=timestamp,
                    open=row[0],
                    high=row[1],
                    low=row[2],
                    close=row[3],
                    volume=1_000_000,
                )
                for timestamp, row in zip(dates, values, strict=True)
            )
        },
    )
