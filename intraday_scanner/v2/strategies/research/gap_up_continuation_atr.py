"""Volatility-normalized daily gap-up continuation research challenger."""

from __future__ import annotations

from statistics import mean

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.indicators import atr, sma
from intraday_scanner.v2.strategies.models import Direction, StrategySignal, StrategySpec


def build_strategy() -> StrategySpec:
    """Build the frozen v1.0 forward-paper challenger."""

    return StrategySpec(
        strategy_id="gap_up_continuation_atr",
        version="v1.0",
        status="experimental",
        description=(
            "Volatility-normalized daily gap-up continuation after a strong, liquid close "
            "in a long-term uptrend."
        ),
        compatible_timeframe="1d",
        required_data_fields=("open", "high", "low", "close", "volume"),
        parameters={
            "min_gap_atr": 0.50,
            "min_close_location": 0.70,
            "trend_sma_period": 100,
            "volume_window": 20,
            "atr_period": 14,
            "stop_atr_buffer": 0.25,
            "reward_risk": 2.0,
        },
        indicators=("overnight gap / prior ATR", "close location", "SMA(100)", "ATR(14)"),
        entry_logic=(
            "Signal long when open[t] is at least 0.50 * ATR14[t-1] above close[t-1], "
            "the bar closes above its open in the top 30% of its range, close[t] is above "
            "SMA100[t], and volume[t] is at least the prior-20-bar mean. Enter no earlier "
            "than the next valid daily bar open."
        ),
        exit_logic=(
            "Exit on stop, 2R target, the PaperOps ten-calendar-day timeout, or forced "
            "paper-account liquidation."
        ),
        stop_logic="Stop = low[t] - 0.25 * ATR14[t].",
        target_logic="Target = close[t] + 2.0 * (close[t] - stop).",
        position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
        known_failure_modes=(
            "the 0.50 ATR threshold was selected from a small retained-snapshot grid",
            "overnight news can reverse the signal before the next open",
            "daily OHLC cannot establish the intraday path within the signal bar",
            "unreconciled splits or dividends can create false public-data gaps",
            "historical candidate screening is subject to selection and multiple-testing bias",
        ),
        validation_status="retained_snapshot_grid_selected_forward_validation_required",
        generate_signal=_generate_signal,
    )


def _generate_signal(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    trend_period = int(spec.parameters["trend_sma_period"])
    volume_window = int(spec.parameters["volume_window"])
    atr_period = int(spec.parameters["atr_period"])
    if index < max(trend_period - 1, volume_window, atr_period, 1):
        return None

    available = bars[: index + 1]
    atr_values = atr(available, atr_period)
    atr_previous = atr_values[index - 1]
    atr_current = atr_values[index]
    trend = sma([item.close for item in available], trend_period)[index]
    if (
        atr_previous is None
        or atr_current is None
        or atr_previous <= 0
        or atr_current <= 0
        or trend is None
    ):
        return None

    bar = bars[index]
    previous = bars[index - 1]
    gap = bar.open - previous.close
    minimum_gap = float(spec.parameters["min_gap_atr"]) * atr_previous
    if gap < minimum_gap or bar.close <= bar.open:
        return None
    bar_range = bar.high - bar.low
    if bar_range <= 0:
        return None
    close_location = (bar.close - bar.low) / bar_range
    if close_location < float(spec.parameters["min_close_location"]):
        return None
    if bar.close <= trend:
        return None
    prior_volume_mean = mean(item.volume for item in bars[index - volume_window : index])
    if bar.volume < prior_volume_mean:
        return None

    stop = bar.low - float(spec.parameters["stop_atr_buffer"]) * atr_current
    if stop <= 0 or stop >= bar.close:
        return None
    risk = bar.close - stop
    target = bar.close + float(spec.parameters["reward_risk"]) * risk
    gap_atr = gap / atr_previous
    score = min(95.0, 60.0 + gap_atr * 10.0 + close_location * 10.0)
    return StrategySignal(
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        symbol=symbol,
        signal_index=index,
        direction=Direction.LONG,
        entry_reference=bar.close,
        stop=stop,
        target=target,
        score=score,
        evidence=(
            (
                f"open gap {gap_atr:.2f} ATR exceeded "
                f"{float(spec.parameters['min_gap_atr']):.2f} ATR"
            ),
            (
                f"close location {close_location * 100:.1f}% with close "
                f"{bar.close:.2f} above SMA100 {trend:.2f}"
            ),
            f"volume {bar.volume:,} met prior-20 mean {prior_volume_mean:,.0f}",
        ),
        invalidation="Failure below the signal low/ATR buffer invalidates continuation.",
        warnings=(
            "grid-selected experimental candidate; only new forward evidence can validate it",
            "public daily OHLC does not prove intraday ordering or corporate-action truth",
        ),
    )


__all__ = ["build_strategy"]
