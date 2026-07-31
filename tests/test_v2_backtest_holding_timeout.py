from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from intraday_scanner.v2.backtest import BacktestEngine, BacktestSettings
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.risk import RiskSettings
from intraday_scanner.v2.strategies import (
    Direction,
    StrategySignal,
    StrategySpec,
)


def _bar(day_offset: int, *, high: float = 101.0, low: float = 99.0) -> MarketBar:
    return MarketBar(
        symbol="TST",
        timestamp=datetime(2026, 1, 1, 21, tzinfo=timezone.utc)
        + timedelta(days=day_offset),
        open=100.0,
        high=high,
        low=low,
        close=100.0,
        volume=100_000,
    )


def _dataset(
    day_offsets: tuple[int, ...],
    *,
    final_high: float = 101.0,
    final_low: float = 99.0,
) -> MarketDataset:
    bars = tuple(
        _bar(
            day_offset,
            high=final_high if index == len(day_offsets) - 1 else 101.0,
            low=final_low if index == len(day_offsets) - 1 else 99.0,
        )
        for index, day_offset in enumerate(day_offsets)
    )
    return MarketDataset(
        dataset_id="holding_timeout_fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"TST": bars},
    )


def _strategy() -> StrategySpec:
    def signal(
        spec: StrategySpec,
        _dataset: MarketDataset,
        symbol: str,
        _bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        if index != 0:
            return None
        return StrategySignal(
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            symbol=symbol,
            signal_index=index,
            direction=Direction.LONG,
            entry_reference=100.0,
            stop=90.0,
            target=120.0,
            score=80.0,
            evidence=("holding-timeout fixture",),
            invalidation="stop hit",
        )

    return StrategySpec(
        strategy_id="holding_timeout_strategy",
        version="v1",
        status="experimental",
        description="Backtest holding-timeout fixture",
        compatible_timeframe="1d",
        required_data_fields=("open", "high", "low", "close", "volume"),
        parameters={},
        indicators=(),
        entry_logic="Enter once",
        exit_logic="Exit on stop, target, or holding timeout",
        stop_logic="Fixed stop",
        target_logic="Fixed target",
        position_sizing_assumption="Risk engine",
        known_failure_modes=(),
        validation_status="fixture",
        generate_signal=signal,
    )


def _settings(**overrides: object) -> BacktestSettings:
    values: dict[str, object] = {
        "fee_bps": 0.0,
        "slippage_bps": 0.0,
        "risk": RiskSettings(account_equity=100_000.0),
    }
    values.update(overrides)
    return BacktestSettings(**values)  # type: ignore[arg-type]


def test_backtest_default_timeout_matches_paper_ops_ten_calendar_days() -> None:
    dataset = _dataset((0, 1, 2, 4, 7, 9, 10, 11, 12))

    result = BacktestEngine(_settings()).run(_strategy(), dataset)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.entry_time == dataset.bars_by_symbol["TST"][1].timestamp
    assert trade.exit_time == dataset.bars_by_symbol["TST"][7].timestamp
    assert (trade.exit_time.date() - trade.entry_time.date()).days == 10
    assert trade.exit_reason == "timeout"
    assert trade.exit_price == 100.0


def test_backtest_timeout_is_configurable_in_calendar_days() -> None:
    dataset = _dataset((0, 1, 2, 3, 4, 5))

    result = BacktestEngine(
        _settings(holding_timeout_calendar_days=3)
    ).run(_strategy(), dataset)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_time == dataset.bars_by_symbol["TST"][4].timestamp
    assert (trade.exit_time.date() - trade.entry_time.date()).days == 3
    assert trade.exit_reason == "timeout"


@pytest.mark.parametrize(
    ("final_high", "final_low", "expected_reason", "expected_price"),
    (
        (101.0, 89.0, "stop", 90.0),
        (121.0, 99.0, "target", 120.0),
    ),
)
def test_backtest_stop_and_target_take_precedence_on_timeout_bar(
    final_high: float,
    final_low: float,
    expected_reason: str,
    expected_price: float,
) -> None:
    dataset = _dataset(
        (0, 1, 2, 3, 4),
        final_high=final_high,
        final_low=final_low,
    )

    result = BacktestEngine(
        _settings(holding_timeout_calendar_days=3)
    ).run(_strategy(), dataset)

    assert len(result.trades) == 1
    trade = result.trades[0]
    assert trade.exit_time == dataset.bars_by_symbol["TST"][-1].timestamp
    assert trade.exit_reason == expected_reason
    assert trade.exit_price == expected_price


@pytest.mark.parametrize("invalid_value", (0, -1, 1.5, True))
def test_backtest_rejects_invalid_holding_timeout(invalid_value: object) -> None:
    with pytest.raises(
        ValueError,
        match="holding_timeout_calendar_days must be an integer of at least 1",
    ):
        BacktestEngine(_settings(holding_timeout_calendar_days=invalid_value))
