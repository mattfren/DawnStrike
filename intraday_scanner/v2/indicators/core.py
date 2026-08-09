"""Small no-lookahead indicator library for mechanical v2 strategies."""

from __future__ import annotations

import math
from statistics import mean, pstdev

from intraday_scanner.v2.data import MarketBar


def sma(values: list[float], period: int) -> list[float | None]:
    _require_period(period)
    output: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            output.append(None)
            continue
        output.append(mean(values[index + 1 - period : index + 1]))
    return output


def rate_of_change(values: list[float], period: int) -> list[float | None]:
    _require_period(period)
    output: list[float | None] = []
    for index, value in enumerate(values):
        if index < period:
            output.append(None)
            continue
        base = values[index - period]
        output.append(None if base == 0 else (value / base) - 1.0)
    return output


def rolling_volatility(values: list[float], period: int) -> list[float | None]:
    _require_period(period)
    returns = rate_of_change(values, 1)
    output: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period + 1:
            output.append(None)
            continue
        window = [value for value in returns[index + 1 - period : index + 1] if value is not None]
        output.append(pstdev(window) if len(window) >= 2 else None)
    return output


def rsi(values: list[float], period: int = 14) -> list[float | None]:
    _require_period(period)
    output: list[float | None] = []
    for index in range(len(values)):
        if index < period:
            output.append(None)
            continue
        gains: list[float] = []
        losses: list[float] = []
        for cursor in range(index + 1 - period, index + 1):
            change = values[cursor] - values[cursor - 1]
            if change >= 0:
                gains.append(change)
                losses.append(0.0)
            else:
                gains.append(0.0)
                losses.append(abs(change))
        average_gain = mean(gains)
        average_loss = mean(losses)
        if average_loss == 0:
            output.append(100.0)
        elif average_gain == 0:
            output.append(0.0)
        else:
            relative_strength = average_gain / average_loss
            output.append(100.0 - (100.0 / (1.0 + relative_strength)))
    return output


def atr(bars: tuple[MarketBar, ...], period: int = 14) -> list[float | None]:
    _require_period(period)
    true_ranges: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            true_ranges.append(bar.high - bar.low)
            continue
        previous_close = bars[index - 1].close
        true_ranges.append(
            max(
                bar.high - bar.low,
                abs(bar.high - previous_close),
                abs(bar.low - previous_close),
            )
        )

    output: list[float | None] = []
    for index in range(len(true_ranges)):
        if index + 1 < period:
            output.append(None)
            continue
        output.append(mean(true_ranges[index + 1 - period : index + 1]))
    return output


def bollinger_bands(
    values: list[float],
    period: int = 20,
    stdev_multiplier: float = 2.0,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    _require_period(period)
    middle: list[float | None] = []
    upper: list[float | None] = []
    lower: list[float | None] = []
    for index in range(len(values)):
        if index + 1 < period:
            middle.append(None)
            upper.append(None)
            lower.append(None)
            continue
        window = values[index + 1 - period : index + 1]
        center = mean(window)
        spread = pstdev(window) * stdev_multiplier
        middle.append(center)
        upper.append(center + spread)
        lower.append(center - spread)
    return middle, upper, lower


def donchian_high(bars: tuple[MarketBar, ...], index: int, lookback: int) -> float | None:
    _require_period(lookback)
    if index < lookback:
        return None
    return max(bar.high for bar in bars[index - lookback : index])


def donchian_low(bars: tuple[MarketBar, ...], index: int, lookback: int) -> float | None:
    _require_period(lookback)
    if index < lookback:
        return None
    return min(bar.low for bar in bars[index - lookback : index])


def prior_sma(values: list[float], period: int) -> list[float | None]:
    """Return a mean using only observations strictly before each index."""

    _require_period(period)
    output: list[float | None] = []
    for index in range(len(values)):
        if index < period:
            output.append(None)
        else:
            output.append(mean(values[index - period : index]))
    return output


def rolling_zscore(values: list[float], period: int) -> list[float | None]:
    """Compute a point-in-time z-score against the prior window only."""

    _require_period(period)
    output: list[float | None] = []
    for index, value in enumerate(values):
        if index < period:
            output.append(None)
            continue
        window = values[index - period : index]
        spread = pstdev(window)
        output.append((value - mean(window)) / spread if spread else 0.0)
    return output


def session_vwap(bars: tuple[MarketBar, ...]) -> list[float | None]:
    """Return cumulative session VWAP, resetting on exchange session identity."""

    output: list[float | None] = []
    session: str | None = None
    price_volume = 0.0
    volume_total = 0
    for bar in bars:
        if bar.exchange_session_id != session:
            session = bar.exchange_session_id
            price_volume = 0.0
            volume_total = 0
        typical_price = (bar.high + bar.low + bar.close) / 3.0
        price_volume += typical_price * bar.volume
        volume_total += bar.volume
        output.append(price_volume / volume_total if volume_total else None)
    return output


def _require_period(period: int) -> None:
    if period <= 0 or not math.isfinite(period):
        raise ValueError("period must be a positive integer")
