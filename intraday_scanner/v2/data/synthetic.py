"""Deterministic synthetic OHLCV fixture generation for v2 Alpha Lab demos."""

from __future__ import annotations

import math
from datetime import date, datetime, time, timedelta, timezone

from intraday_scanner.v2.data.market import MarketBar, MarketDataset


def build_synthetic_ohlcv_dataset(
    *,
    end_date: date,
    trading_days: int = 260,
    dataset_id: str = "synthetic_alpha_lab_v1",
) -> MarketDataset:
    """Build a deterministic multi-symbol fixture with varied regimes.

    The fixture is intentionally synthetic. It exists so the Alpha Lab can exercise
    backtesting, risk, reporting, and scan flows without paid data or credentials.
    """

    days = _business_days(end_date=end_date, count=trading_days)
    symbols = ("NOVA", "RIFT", "VEGA", "PULSE", "AXIS", "QUAD")
    bars_by_symbol: dict[str, tuple[MarketBar, ...]] = {}
    for symbol_index, symbol in enumerate(symbols):
        bars: list[MarketBar] = []
        previous_close = 8.0 + symbol_index * 4.5
        for day_index, current_day in enumerate(days):
            drift = _drift(symbol_index, day_index)
            cycle = math.sin(day_index / (6.0 + symbol_index) + symbol_index * 0.7)
            faster_cycle = math.sin(day_index / 2.7 + symbol_index)
            event = _event_return(symbol_index, day_index)
            daily_return = drift + 0.008 * cycle + 0.003 * faster_cycle + event
            open_price = max(0.75, previous_close * (1.0 + 0.0025 * faster_cycle))
            close_price = max(0.75, previous_close * (1.0 + daily_return))
            range_pct = 0.018 + 0.007 * abs(cycle) + 0.003 * symbol_index
            high_price = max(open_price, close_price) * (1.0 + range_pct)
            low_price = min(open_price, close_price) * (1.0 - range_pct * 0.82)
            volume = int(
                220_000
                + symbol_index * 75_000
                + day_index * 950
                + abs(cycle) * 120_000
                + abs(event) * 8_000_000
            )
            bars.append(
                MarketBar(
                    symbol=symbol,
                    timestamp=datetime.combine(current_day, time(21, 0), tzinfo=timezone.utc),
                    open=round(open_price, 4),
                    high=round(high_price, 4),
                    low=round(low_price, 4),
                    close=round(close_price, 4),
                    volume=volume,
                )
            )
            previous_close = close_price
        bars_by_symbol[symbol] = tuple(bars)

    adjusted = dict(bars_by_symbol)
    adjusted["NOVA"] = _force_latest_breakout(adjusted["NOVA"])
    adjusted["VEGA"] = _force_latest_failed_breakout(adjusted["VEGA"])

    return MarketDataset(
        dataset_id=dataset_id,
        source_kind="synthetic",
        timeframe="1d",
        bars_by_symbol=adjusted,
        source_path=None,
        warnings=(
            "synthetic_fixture: generated deterministic OHLCV; do not treat returns "
            "as market evidence",
        ),
    )


def _business_days(*, end_date: date, count: int) -> tuple[date, ...]:
    days: list[date] = []
    cursor = end_date
    while len(days) < count:
        if cursor.weekday() < 5:
            days.append(cursor)
        cursor -= timedelta(days=1)
    return tuple(reversed(days))


def _drift(symbol_index: int, day_index: int) -> float:
    if symbol_index == 0:
        return 0.0018
    if symbol_index == 1:
        return -0.0003 if day_index < 130 else 0.0011
    if symbol_index == 2:
        return 0.0002
    if symbol_index == 3:
        return 0.0011 if day_index % 90 < 45 else -0.0008
    if symbol_index == 4:
        return 0.0006
    return -0.0001


def _event_return(symbol_index: int, day_index: int) -> float:
    if symbol_index == 0 and day_index in {72, 153, 221}:
        return 0.055
    if symbol_index == 1 and day_index in {44, 118, 205}:
        return -0.045
    if symbol_index == 2 and day_index in {65, 166, 236}:
        return 0.038 if day_index != 236 else -0.032
    if symbol_index == 3 and day_index % 57 == 0 and day_index > 0:
        return 0.028
    if symbol_index == 4 and day_index in {100, 101, 102}:
        return 0.018
    if symbol_index == 5 and day_index in {140, 210}:
        return -0.026
    return 0.0


def _force_latest_breakout(bars: tuple[MarketBar, ...]) -> tuple[MarketBar, ...]:
    if len(bars) < 25:
        return bars
    prior_high = max(bar.high for bar in bars[-22:-2])
    previous = bars[-2]
    latest = bars[-1]
    open_price = max(previous.close * 1.012, prior_high * 1.002)
    close_price = prior_high * 1.035
    high_price = close_price * 1.012
    low_price = min(open_price, close_price) * 0.988
    replacement = MarketBar(
        symbol=latest.symbol,
        timestamp=latest.timestamp,
        open=round(open_price, 4),
        high=round(high_price, 4),
        low=round(low_price, 4),
        close=round(close_price, 4),
        volume=max(latest.volume, previous.volume * 2),
    )
    return bars[:-1] + (replacement,)


def _force_latest_failed_breakout(bars: tuple[MarketBar, ...]) -> tuple[MarketBar, ...]:
    if len(bars) < 25:
        return bars
    prior_high = max(bar.high for bar in bars[-22:-2])
    previous = bars[-2]
    latest = bars[-1]
    open_price = previous.close * 1.01
    high_price = prior_high * 1.025
    close_price = prior_high * 0.988
    low_price = min(open_price, close_price) * 0.972
    replacement = MarketBar(
        symbol=latest.symbol,
        timestamp=latest.timestamp,
        open=round(open_price, 4),
        high=round(high_price, 4),
        low=round(low_price, 4),
        close=round(close_price, 4),
        volume=max(latest.volume, previous.volume * 2),
    )
    return bars[:-1] + (replacement,)
