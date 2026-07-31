"""Mechanical strategy implementations for the Dawnstrike v2 Alpha Lab."""

from __future__ import annotations

from statistics import median

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.indicators import (
    atr,
    bollinger_bands,
    donchian_high,
    donchian_low,
    rate_of_change,
    rolling_volatility,
    rsi,
    sma,
)
from intraday_scanner.v2.strategies.models import Direction, StrategySignal, StrategySpec

_FEATURE_CACHE: dict[tuple[object, ...], object] = {}


def build_strategy_catalog() -> tuple[StrategySpec, ...]:
    return (
        StrategySpec(
            strategy_id="ts_momentum_sma_atr",
            version="v1.0",
            status="experimental",
            description=(
                "Time-series momentum: long when close is above SMA and trailing return "
                "is positive."
            ),
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={"sma_period": 50, "momentum_period": 20, "atr_period": 14, "atr_stop": 2.0},
            indicators=("SMA(50)", "ROC(20)", "ATR(14)"),
            entry_logic="Enter long when close[t] > SMA50[t] and close[t] / close[t-20] - 1 > 0.",
            exit_logic="Exit on stop, target, or end-of-test liquidation.",
            stop_logic="Stop = close[t] - 2.0 * ATR14[t].",
            target_logic="Target = close[t] + 3.0 * ATR14[t].",
            position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
            known_failure_modes=(
                "whipsaws in range-bound regimes",
                "late entries after exhausted trends",
                "synthetic fixture results are not market evidence",
            ),
            validation_status="fixture_backtested_only",
            generate_signal=_time_series_momentum,
        ),
        StrategySpec(
            strategy_id="donchian_breakout_20_10",
            version="v1.0",
            status="experimental",
            description="Donchian breakout: long on a close above the prior 20-bar high.",
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={
                "entry_lookback": 20,
                "exit_lookback": 10,
                "atr_period": 14,
                "atr_stop": 2.0,
            },
            indicators=("DonchianHigh(20)", "DonchianLow(10)", "ATR(14)"),
            entry_logic="Enter long when close[t] > highest_high[t-20:t-1].",
            exit_logic="Exit on stop, target, or end-of-test liquidation.",
            stop_logic="Stop = max(prior_10_low, close[t] - 2.0 * ATR14[t]) when available.",
            target_logic="Target = close[t] + 3.0 * initial risk.",
            position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
            known_failure_modes=(
                "false breakouts after news spikes",
                "gap-through stops",
                "poor behavior in low-liquidity symbols",
            ),
            validation_status="fixture_backtested_only",
            generate_signal=_donchian_breakout,
        ),
        StrategySpec(
            strategy_id="cross_sectional_relative_strength",
            version="v1.0",
            status="experimental",
            description=(
                "Cross-sectional relative strength: rank symbols by volatility-adjusted momentum."
            ),
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={"momentum_period": 60, "vol_period": 20, "top_n": 2, "sma_period": 50},
            indicators=("ROC(60)", "RollingVolatility(20)", "SMA(50)"),
            entry_logic=(
                "Enter long when symbol ranks in the top 2 by ROC60 / vol20 and "
                "close[t] > SMA50[t]."
            ),
            exit_logic="Exit on stop, target, or end-of-test liquidation.",
            stop_logic="Stop = close[t] - 2.5 * ATR14[t].",
            target_logic="Target = close[t] + 3.0 * initial risk.",
            position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
            known_failure_modes=(
                "universe selection bias",
                "momentum crashes after crowded rotations",
                "requires clean synchronized universe history",
            ),
            validation_status="fixture_backtested_only",
            generate_signal=_cross_sectional_relative_strength,
        ),
        StrategySpec(
            strategy_id="pullback_reclaim_uptrend",
            version="v1.0",
            status="experimental",
            description=(
                "Mean-reversion pullback in an uptrend with RSI/Bollinger reclaim confirmation."
            ),
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={"sma_period": 50, "rsi_period": 14, "bb_period": 20, "rsi_threshold": 42},
            indicators=("SMA(50)", "RSI(14)", "BollingerBands(20,2)", "ATR(14)"),
            entry_logic=(
                "Enter long when close[t] > SMA50[t], close[t-1] <= lower_band[t-1] or "
                "RSI14[t-1] < 42, and close[t] > close[t-1]."
            ),
            exit_logic="Exit on stop, target, or end-of-test liquidation.",
            stop_logic="Stop = min(low[t], close[t] - 1.5 * ATR14[t]).",
            target_logic="Target = Bollinger midline if above entry, otherwise entry + 2R.",
            position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
            known_failure_modes=(
                "averaging into broken trends",
                "RSI remains oversold during waterfall declines",
                "target can be too conservative in strong trend continuation",
            ),
            validation_status="fixture_backtested_only",
            generate_signal=_pullback_reclaim,
        ),
        StrategySpec(
            strategy_id="volatility_contraction_breakout",
            version="v1.0",
            status="experimental",
            description="Volatility contraction breakout after compressed ATR and range.",
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={"compression_window": 10, "atr_period": 14, "percentile_window": 60},
            indicators=("ATR(14)", "ATR percentile", "DonchianHigh(10)", "RangeCompression(10)"),
            entry_logic=(
                "Enter long when ATR14/close is in the lowest quartile of the trailing 60 bars "
                "and close[t] > highest_high[t-10:t-1]."
            ),
            exit_logic="Exit on stop, target, or end-of-test liquidation.",
            stop_logic="Stop = lowest_low[t-10:t-1].",
            target_logic="Target = close[t] + 2.5 * initial risk.",
            position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
            known_failure_modes=(
                "compression can precede breakdowns",
                "low ATR can reflect dead liquidity",
                "breakout confirmation may be late after gap opens",
            ),
            validation_status="fixture_backtested_only",
            generate_signal=_volatility_contraction_breakout,
        ),
        StrategySpec(
            strategy_id="failed_breakout_reversal_short",
            version="v1.0",
            status="experimental",
            description=(
                "Failed breakout reversal: short when price sweeps a prior high and "
                "closes back below it."
            ),
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={"sweep_lookback": 20, "atr_period": 14, "atr_buffer": 0.25},
            indicators=("DonchianHigh(20)", "ATR(14)"),
            entry_logic=(
                "Enter short when high[t] > highest_high[t-20:t-1] and close[t] "
                "< highest_high[t-20:t-1]."
            ),
            exit_logic="Exit on stop, target, or end-of-test liquidation.",
            stop_logic="Stop = high[t] + 0.25 * ATR14[t].",
            target_logic="Target = close[t] - 2.0 * initial risk.",
            position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
            known_failure_modes=(
                "shorting may be unavailable or expensive",
                "failed breakouts can re-break upward",
                "same-bar high/low ordering is unknowable from OHLC data",
            ),
            validation_status="fixture_backtested_only",
            generate_signal=_failed_breakout_short,
        ),
        StrategySpec(
            strategy_id="bullish_fvg_continuation",
            version="v1.0",
            status="experimental",
            description=(
                "Experimental bullish fair-value-gap continuation using a strict "
                "three-candle imbalance."
            ),
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={"sma_period": 20, "atr_period": 14, "retrace_tolerance": 0.35},
            indicators=("SMA(20)", "ATR(14)", "three-candle imbalance"),
            entry_logic=(
                "Bullish imbalance if low[t-1] > high[t-3]; enter when close[t] remains above "
                "the gap midpoint and close[t] > SMA20[t]."
            ),
            exit_logic="Exit on stop, target, or end-of-test liquidation.",
            stop_logic="Stop = high[t-3] - 0.5 * ATR14[t].",
            target_logic="Target = close[t] + 2.0 * initial risk.",
            position_sizing_assumption="Fixed fractional equity risk from entry reference to stop.",
            known_failure_modes=(
                "FVG terminology is often discretionary and overfit",
                "daily OHLC gaps do not prove order-flow imbalance",
                "experimental thesis requires deeper validation before trust",
            ),
            validation_status="experimental_fixture_only",
            generate_signal=_bullish_fvg_continuation,
        ),
        StrategySpec(
            strategy_id="benchmark_buy_hold_equal_weight",
            version="v1.0",
            status="benchmark",
            description="Equal-weight buy-and-hold comparator with a wide catastrophe stop.",
            compatible_timeframe="1d",
            required_data_fields=("open", "high", "low", "close", "volume"),
            parameters={"start_index": 60, "catastrophe_stop_pct": 0.35},
            indicators=("none",),
            entry_logic="Enter long once per symbol after warm-up at the next bar open.",
            exit_logic="Hold to end of test unless catastrophe stop is touched.",
            stop_logic="Stop = entry_reference * 0.65.",
            target_logic="No profit target; benchmark comparator only.",
            position_sizing_assumption="Small fixed fractional equity allocation per symbol.",
            known_failure_modes=(
                "not a tradeable alpha strategy",
                "does not rebalance after entry",
                "catastrophe stop is a fixture risk control",
            ),
            validation_status="benchmark_only",
            generate_signal=_benchmark_buy_hold,
        ),
        StrategySpec(
            strategy_id="cash_no_trade_baseline",
            version="v1.0",
            status="baseline",
            description="No-trade cash baseline.",
            compatible_timeframe="1d",
            required_data_fields=(),
            parameters={},
            indicators=("none",),
            entry_logic="Never enters a position.",
            exit_logic="Not applicable.",
            stop_logic="Not applicable.",
            target_logic="Not applicable.",
            position_sizing_assumption="Capital remains in cash.",
            known_failure_modes=("does not measure opportunity cost except through comparison",),
            validation_status="baseline_only",
            generate_signal=_cash_baseline,
        ),
    )


def _time_series_momentum(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    closes = _closes(bars)
    sma_values = _sma_cached(bars, int(spec.parameters["sma_period"]))
    momentum = _roc_cached(bars, int(spec.parameters["momentum_period"]))
    atr_values = _atr_cached(bars, int(spec.parameters["atr_period"]))
    if not _has_values(sma_values[index], momentum[index], atr_values[index]):
        return None
    sma_current = _required(sma_values[index])
    momentum_current = _required(momentum[index])
    atr_current = _required(atr_values[index])
    if closes[index] <= sma_current or momentum_current <= 0:
        return None
    entry = closes[index]
    stop = entry - float(spec.parameters["atr_stop"]) * atr_current
    if stop <= 0 or stop >= entry:
        return None
    target = entry + 1.5 * (entry - stop)
    return _long_signal(
        spec,
        symbol,
        index,
        entry,
        stop,
        target,
        min(100.0, 55.0 + momentum_current * 220.0),
        (
            f"close {entry:.2f} above SMA50 {sma_current:.2f}",
            f"20-bar trailing return {momentum_current * 100:.2f}%",
        ),
        "Close below SMA50 or stop hit invalidates the trend thesis.",
    )


def _donchian_breakout(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    entry_lookback = int(spec.parameters["entry_lookback"])
    prior_high = donchian_high(bars, index, entry_lookback)
    shorter_low = donchian_low(bars, index, int(spec.parameters["exit_lookback"]))
    atr_values = _atr_cached(bars, int(spec.parameters["atr_period"]))
    if not _has_values(prior_high, shorter_low, atr_values[index]):
        return None
    prior_high_value = _required(prior_high)
    shorter_low_value = _required(shorter_low)
    atr_current = _required(atr_values[index])
    close = bars[index].close
    if close <= prior_high_value:
        return None
    atr_stop = close - float(spec.parameters["atr_stop"]) * atr_current
    stop = max(shorter_low_value, atr_stop)
    if stop >= close:
        stop = close - atr_current
    target = close + 3.0 * (close - stop)
    return _long_signal(
        spec,
        symbol,
        index,
        close,
        stop,
        target,
        78.0,
        (f"close {close:.2f} broke prior {entry_lookback}-bar high {prior_high_value:.2f}",),
        "Close back inside the Donchian channel or stop hit invalidates the breakout.",
    )


def _cross_sectional_relative_strength(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    momentum_period = int(spec.parameters["momentum_period"])
    vol_period = int(spec.parameters["vol_period"])
    if index < momentum_period or index < vol_period:
        return None

    scores = _relative_strength_scores(dataset, index, momentum_period, vol_period)
    if not scores:
        return None
    ranked = sorted(scores, reverse=True)
    top_symbols = {ranked_symbol for _, ranked_symbol in ranked[: int(spec.parameters["top_n"])]}
    if symbol not in top_symbols:
        return None
    sma_values = _sma_cached(bars, int(spec.parameters["sma_period"]))
    atr_values = _atr_cached(bars, 14)
    if not _has_values(sma_values[index], atr_values[index]):
        return None
    sma_current = _required(sma_values[index])
    atr_current = _required(atr_values[index])
    if bars[index].close <= sma_current:
        return None
    close = bars[index].close
    stop = close - 2.5 * atr_current
    if stop <= 0 or stop >= close:
        return None
    target = close + 3.0 * (close - stop)
    rank = next(
        rank for rank, (_, ranked_symbol) in enumerate(ranked, start=1) if ranked_symbol == symbol
    )
    return _long_signal(
        spec,
        symbol,
        index,
        close,
        stop,
        target,
        80.0 - rank * 4.0,
        (
            f"rank {rank} by volatility-adjusted 60-bar momentum",
            f"close {close:.2f} above SMA50 {sma_current:.2f}",
        ),
        "Rank decay, trend loss, or stop hit invalidates the relative-strength thesis.",
    )


def _pullback_reclaim(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    if index < 2:
        return None
    closes = _closes(bars)
    sma_values = _sma_cached(bars, int(spec.parameters["sma_period"]))
    rsi_values = _rsi_cached(bars, int(spec.parameters["rsi_period"]))
    middle, _, lower = _bb_cached(bars, int(spec.parameters["bb_period"]))
    atr_values = _atr_cached(bars, 14)
    if not _has_values(
        sma_values[index], rsi_values[index - 1], lower[index - 1], atr_values[index]
    ):
        return None
    sma_current = _required(sma_values[index])
    previous_rsi = _required(rsi_values[index - 1])
    previous_lower = _required(lower[index - 1])
    atr_current = _required(atr_values[index])
    close = closes[index]
    if close <= sma_current or close <= closes[index - 1]:
        return None
    had_pullback = closes[index - 1] <= previous_lower or previous_rsi < float(
        spec.parameters["rsi_threshold"]
    )
    if not had_pullback:
        return None
    stop = min(bars[index].low, close - 1.5 * atr_current)
    if stop <= 0 or stop >= close:
        return None
    midline = middle[index]
    risk = close - stop
    target = (
        float(midline) if midline is not None and float(midline) > close else close + 2.0 * risk
    )
    return _long_signal(
        spec,
        symbol,
        index,
        close,
        stop,
        target,
        68.0,
        (
            f"uptrend close {close:.2f} above SMA50 {sma_current:.2f}",
            "prior bar showed RSI/Bollinger pullback and current bar reclaimed upward",
        ),
        "Loss of reclaim low or stop hit invalidates the pullback continuation thesis.",
    )


def _volatility_contraction_breakout(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    compression_window = int(spec.parameters["compression_window"])
    percentile_window = int(spec.parameters["percentile_window"])
    atr_values = _atr_cached(bars, int(spec.parameters["atr_period"]))
    prior_high = donchian_high(bars, index, compression_window)
    prior_low = donchian_low(bars, index, compression_window)
    if index < percentile_window or not _has_values(atr_values[index], prior_high, prior_low):
        return None
    atr_current = _required(atr_values[index])
    prior_high_value = _required(prior_high)
    prior_low_value = _required(prior_low)
    close = bars[index].close
    atr_pcts: list[float] = []
    for cursor in range(index - percentile_window, index):
        atr_value = atr_values[cursor]
        if atr_value is not None and bars[cursor].close > 0:
            atr_pcts.append(atr_value / bars[cursor].close)
    if len(atr_pcts) < percentile_window // 2:
        return None
    current_atr_pct = atr_current / close
    if current_atr_pct > _percentile(atr_pcts, 25) or close <= prior_high_value:
        return None
    stop = prior_low_value
    if stop <= 0 or stop >= close:
        return None
    target = close + 2.5 * (close - stop)
    return _long_signal(
        spec,
        symbol,
        index,
        close,
        stop,
        target,
        74.0,
        (
            f"ATR/close {current_atr_pct:.4f} is in trailing low-volatility quartile",
            f"close {close:.2f} broke compression high {prior_high_value:.2f}",
        ),
        "Close back inside the compressed range or stop hit invalidates the breakout.",
    )


def _failed_breakout_short(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    prior_high = donchian_high(bars, index, int(spec.parameters["sweep_lookback"]))
    atr_values = _atr_cached(bars, int(spec.parameters["atr_period"]))
    if not _has_values(prior_high, atr_values[index]):
        return None
    prior_high_value = _required(prior_high)
    atr_current = _required(atr_values[index])
    bar = bars[index]
    if bar.high <= prior_high_value or bar.close >= prior_high_value:
        return None
    stop = bar.high + float(spec.parameters["atr_buffer"]) * atr_current
    if stop <= bar.close:
        return None
    target = bar.close - 2.0 * (stop - bar.close)
    if target <= 0:
        target = max(0.01, bar.close * 0.5)
    return StrategySignal(
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        symbol=symbol,
        signal_index=index,
        direction=Direction.SHORT,
        entry_reference=bar.close,
        stop=stop,
        target=target,
        score=72.0,
        evidence=(
            f"high {bar.high:.2f} swept prior 20-bar high {prior_high_value:.2f}",
            f"close {bar.close:.2f} failed back below prior high",
        ),
        invalidation="Close back above sweep high or stop hit invalidates the reversal thesis.",
        warnings=("shorting availability and borrow cost are not modeled",),
    )


def _bullish_fvg_continuation(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    if index < 4:
        return None
    sma_values = _sma_cached(bars, int(spec.parameters["sma_period"]))
    atr_values = _atr_cached(bars, int(spec.parameters["atr_period"]))
    if not _has_values(sma_values[index], atr_values[index]):
        return None
    sma_current = _required(sma_values[index])
    atr_current = _required(atr_values[index])
    gap_low = bars[index - 1].low
    gap_high = bars[index - 3].high
    if gap_low <= gap_high:
        return None
    gap_midpoint = gap_high + (gap_low - gap_high) / 2.0
    close = bars[index].close
    if close <= gap_midpoint or close <= sma_current:
        return None
    stop = gap_high - 0.5 * atr_current
    if stop <= 0 or stop >= close:
        return None
    target = close + 2.0 * (close - stop)
    return _long_signal(
        spec,
        symbol,
        index,
        close,
        stop,
        target,
        63.0,
        (
            f"bullish three-candle gap zone {gap_high:.2f}-{gap_low:.2f}",
            f"close {close:.2f} held above gap midpoint {gap_midpoint:.2f}",
        ),
        "Close below gap midpoint or stop hit invalidates the imbalance continuation thesis.",
        warnings=("experimental FVG proxy; not validated against order-flow data",),
    )


def _benchmark_buy_hold(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del dataset
    start_index = int(spec.parameters["start_index"])
    if index != start_index:
        return None
    entry = bars[index].close
    stop = entry * (1.0 - float(spec.parameters["catastrophe_stop_pct"]))
    return _long_signal(
        spec,
        symbol,
        index,
        entry,
        stop,
        None,
        50.0,
        ("benchmark equal-weight buy-and-hold entry after warm-up",),
        "Catastrophe stop or end-of-test liquidation exits the benchmark.",
        warnings=("benchmark comparator; no profit target by design",),
    )


def _cash_baseline(
    spec: StrategySpec,
    dataset: MarketDataset,
    symbol: str,
    bars: tuple[MarketBar, ...],
    index: int,
) -> StrategySignal | None:
    del spec, dataset, symbol, bars, index
    return None


def _long_signal(
    spec: StrategySpec,
    symbol: str,
    index: int,
    entry: float,
    stop: float,
    target: float | None,
    score: float,
    evidence: tuple[str, ...],
    invalidation: str,
    warnings: tuple[str, ...] = (),
) -> StrategySignal:
    return StrategySignal(
        strategy_id=spec.strategy_id,
        strategy_version=spec.version,
        symbol=symbol,
        signal_index=index,
        direction=Direction.LONG,
        entry_reference=entry,
        stop=stop,
        target=target,
        score=score,
        evidence=evidence,
        invalidation=invalidation,
        warnings=warnings,
    )


def _has_values(*values: float | None) -> bool:
    return all(value is not None for value in values)


def _required(value: float | None) -> float:
    if value is None:
        raise ValueError("indicator value is not available after warm-up check")
    return value


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        raise ValueError("values are required")
    if len(values) == 1:
        return values[0]
    sorted_values = sorted(values)
    rank = (len(sorted_values) - 1) * percentile / 100.0
    lower = int(rank)
    upper = min(lower + 1, len(sorted_values) - 1)
    weight = rank - lower
    return sorted_values[lower] * (1.0 - weight) + sorted_values[upper] * weight


def describe_strategy(spec: StrategySpec) -> dict[str, object]:
    return {
        "strategy_id": spec.strategy_id,
        "version": spec.version,
        "status": spec.status,
        "description": spec.description,
        "compatible_timeframe": spec.compatible_timeframe,
        "required_data_fields": list(spec.required_data_fields),
        "parameters": dict(spec.parameters),
        "indicators": list(spec.indicators),
        "entry_logic": spec.entry_logic,
        "exit_logic": spec.exit_logic,
        "stop_logic": spec.stop_logic,
        "target_logic": spec.target_logic,
        "position_sizing_assumption": spec.position_sizing_assumption,
        "known_failure_modes": list(spec.known_failure_modes),
        "validation_status": spec.validation_status,
    }


def summarize_signal_density(
    strategy: StrategySpec,
    dataset: MarketDataset,
    *,
    max_index: int | None = None,
) -> dict[str, int | float]:
    signal_count = 0
    eligible_symbols = 0
    bars_checked = 0
    for symbol, bars in dataset.bars_by_symbol.items():
        if not bars:
            continue
        eligible_symbols += 1
        end = min(len(bars), max_index if max_index is not None else len(bars))
        for index in range(end):
            bars_checked += 1
            if strategy.signal(dataset, symbol, bars, index):
                signal_count += 1
    density = signal_count / bars_checked if bars_checked else 0.0
    return {
        "eligible_symbols": eligible_symbols,
        "bars_checked": bars_checked,
        "signal_count": signal_count,
        "signal_density": density,
    }


def median_close(dataset: MarketDataset) -> float:
    closes = [bar.close for bars in dataset.bars_by_symbol.values() for bar in bars]
    return median(closes) if closes else 0.0


def _cache_key(name: str, bars: tuple[MarketBar, ...], *parts: object) -> tuple[object, ...]:
    return (name, id(bars), len(bars), *parts)


def _closes(bars: tuple[MarketBar, ...]) -> list[float]:
    key = _cache_key("closes", bars)
    cached = _FEATURE_CACHE.get(key)
    if cached is None:
        cached = [bar.close for bar in bars]
        _FEATURE_CACHE[key] = cached
    return cached  # type: ignore[return-value]


def _sma_cached(bars: tuple[MarketBar, ...], period: int) -> list[float | None]:
    key = _cache_key("sma", bars, period)
    cached = _FEATURE_CACHE.get(key)
    if cached is None:
        cached = sma(_closes(bars), period)
        _FEATURE_CACHE[key] = cached
    return cached  # type: ignore[return-value]


def _roc_cached(bars: tuple[MarketBar, ...], period: int) -> list[float | None]:
    key = _cache_key("roc", bars, period)
    cached = _FEATURE_CACHE.get(key)
    if cached is None:
        cached = rate_of_change(_closes(bars), period)
        _FEATURE_CACHE[key] = cached
    return cached  # type: ignore[return-value]


def _vol_cached(bars: tuple[MarketBar, ...], period: int) -> list[float | None]:
    key = _cache_key("vol", bars, period)
    cached = _FEATURE_CACHE.get(key)
    if cached is None:
        cached = rolling_volatility(_closes(bars), period)
        _FEATURE_CACHE[key] = cached
    return cached  # type: ignore[return-value]


def _rsi_cached(bars: tuple[MarketBar, ...], period: int) -> list[float | None]:
    key = _cache_key("rsi", bars, period)
    cached = _FEATURE_CACHE.get(key)
    if cached is None:
        cached = rsi(_closes(bars), period)
        _FEATURE_CACHE[key] = cached
    return cached  # type: ignore[return-value]


def _atr_cached(bars: tuple[MarketBar, ...], period: int) -> list[float | None]:
    key = _cache_key("atr", bars, period)
    cached = _FEATURE_CACHE.get(key)
    if cached is None:
        cached = atr(bars, period)
        _FEATURE_CACHE[key] = cached
    return cached  # type: ignore[return-value]


def _bb_cached(
    bars: tuple[MarketBar, ...],
    period: int,
) -> tuple[list[float | None], list[float | None], list[float | None]]:
    key = _cache_key("bb", bars, period)
    cached = _FEATURE_CACHE.get(key)
    if cached is None:
        cached = bollinger_bands(_closes(bars), period)
        _FEATURE_CACHE[key] = cached
    return cached  # type: ignore[return-value]


def _relative_strength_scores(
    dataset: MarketDataset,
    index: int,
    momentum_period: int,
    vol_period: int,
) -> list[tuple[float, str]]:
    key = ("rs_scores", id(dataset), index, momentum_period, vol_period)
    cached = _FEATURE_CACHE.get(key)
    if cached is not None:
        return cached  # type: ignore[return-value]
    scores: list[tuple[float, str]] = []
    for other_symbol, other_bars in dataset.bars_by_symbol.items():
        if len(other_bars) <= index:
            continue
        momentum = _roc_cached(other_bars, momentum_period)[index]
        vol = _vol_cached(other_bars, vol_period)[index]
        if momentum is None or vol is None or vol <= 0:
            continue
        scores.append((float(momentum) / float(vol), other_symbol))
    ranked = sorted(scores, reverse=True)
    _FEATURE_CACHE[key] = ranked
    return ranked
