"""Causal OHLCV feature snapshots for market-first discovery and evaluation."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from statistics import mean, pstdev
from zoneinfo import ZoneInfo

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.opportunity.catalyst import InjectedCatalystAdapter
from intraday_scanner.v2.opportunity.models import (
    Availability,
    CategoricalFeature,
    DataQuality,
    FeatureSnapshot,
    FeatureStage,
    NumericFeature,
    SessionSegment,
    stable_identity,
)

EASTERN = ZoneInfo("America/New_York")


class FeatureInputError(ValueError):
    """Raised when supplied observations violate the point-in-time contract."""


@dataclass(frozen=True)
class FeatureConfig:
    short_return_period: int = 1
    long_return_period: int = 3
    acceleration_period: int = 1
    atr_window: int = 3
    volume_window: int = 3
    volatility_short_window: int = 2
    volatility_long_window: int = 4
    structure_lookback: int = 3
    min_cross_section_size: int = 3
    config_version: str = "opportunity-features-heuristic-v1"

    def __post_init__(self) -> None:
        for name in (
            "short_return_period",
            "long_return_period",
            "acceleration_period",
            "atr_window",
            "volume_window",
            "volatility_short_window",
            "volatility_long_window",
            "structure_lookback",
            "min_cross_section_size",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be positive")
        for name in (
            "volume_window",
            "volatility_short_window",
            "volatility_long_window",
            "min_cross_section_size",
        ):
            if getattr(self, name) < 2:
                raise ValueError(f"{name} must be at least two")
        if not self.config_version.strip():
            raise ValueError("config_version cannot be blank")


DEFAULT_FEATURE_CONFIG = FeatureConfig()


def build_feature_snapshots(
    dataset: MarketDataset,
    *,
    decision_at: datetime,
    universe_id: str,
    stage: FeatureStage,
    symbols: tuple[str, ...] | None = None,
    benchmark_symbol: str | None = None,
    config: FeatureConfig = DEFAULT_FEATURE_CONFIG,
    catalyst_adapter: InjectedCatalystAdapter | None = None,
) -> tuple[FeatureSnapshot, ...]:
    """Build deterministic snapshots from observations no later than decision time."""

    _require_aware(decision_at, "decision_at")
    requested_source = dataset.symbols if symbols is None else symbols
    requested = tuple(sorted(set(requested_source)))
    if not requested:
        return ()
    if benchmark_symbol is not None and benchmark_symbol not in dataset.bars_by_symbol:
        raise FeatureInputError(f"benchmark symbol {benchmark_symbol} is absent")

    validation_symbols = set(requested)
    if benchmark_symbol is not None:
        validation_symbols.add(benchmark_symbol)
    for symbol in sorted(validation_symbols):
        bars = dataset.bars_by_symbol.get(symbol)
        if not bars:
            raise FeatureInputError(f"{symbol} has no bars")
        _validate_bars(symbol, bars, decision_at)

    snapshots = [
        _build_symbol_snapshot(
            dataset,
            symbol=symbol,
            decision_at=decision_at,
            universe_id=universe_id,
            stage=stage,
            benchmark_symbol=benchmark_symbol,
            config=config,
            catalyst_adapter=catalyst_adapter,
        )
        for symbol in requested
    ]
    return _add_cross_section_features(snapshots, config=config)


def _build_symbol_snapshot(
    dataset: MarketDataset,
    *,
    symbol: str,
    decision_at: datetime,
    universe_id: str,
    stage: FeatureStage,
    benchmark_symbol: str | None,
    config: FeatureConfig,
    catalyst_adapter: InjectedCatalystAdapter | None,
) -> FeatureSnapshot:
    bars = dataset.bars_by_symbol[symbol]
    latest = bars[-1]
    numerical = _cheap_features(bars, decision_at=decision_at, config=config)
    categorical = [_session_segment_feature(latest, decision_at)]
    limitations: list[str] = []

    if stage is FeatureStage.RICH:
        benchmark_bars = (
            dataset.bars_by_symbol[benchmark_symbol]
            if benchmark_symbol is not None and benchmark_symbol != symbol
            else None
        )
        numerical.extend(
            _rich_features(
                bars,
                benchmark_bars=benchmark_bars,
                decision_at=decision_at,
                config=config,
            )
        )
        catalyst = (
            catalyst_adapter.evidence_at(symbol, decision_at=decision_at)
            if catalyst_adapter is not None
            else None
        )
        categorical.append(
            CategoricalFeature(
                name="catalyst_state",
                value=catalyst.state if catalyst is not None else None,
                availability=(
                    Availability.AVAILABLE if catalyst is not None else Availability.UNSUPPORTED
                ),
                method=(
                    f"point_in_time_catalyst:{catalyst.source_identity}:"
                    f"{catalyst.payload_hash_sha256}"
                    if catalyst is not None
                    else "point_in_time_catalyst_not_supplied"
                ),
                observed_at=catalyst.observed_at if catalyst is not None else latest.timestamp,
                reason=None if catalyst is not None else "point_in_time_catalyst_unavailable",
            )
        )
        limitations.extend(
            (
                "session_vwap_is_ohlcv_typical_price_proxy",
                "true_order_flow_requires_aggressor_side_trade_evidence",
                *(("point_in_time_catalyst_not_supplied",) if catalyst is None else ()),
            )
        )

    quality = _data_quality(len(bars), latest.timestamp, decision_at, config)
    return _snapshot(
        symbol=symbol,
        decision_at=decision_at,
        universe_id=universe_id,
        dataset_id=dataset.dataset_id,
        stage=stage,
        latest_bar_at=latest.timestamp,
        numerical=tuple(numerical),
        categorical=tuple(categorical),
        data_quality=quality,
        limitations=tuple(limitations),
    )


def _cheap_features(
    bars: tuple[MarketBar, ...],
    *,
    decision_at: datetime,
    config: FeatureConfig,
) -> list[NumericFeature]:
    latest = bars[-1]
    closes = [bar.close for bar in bars]
    volumes = [float(bar.volume) for bar in bars]
    observed_at = latest.timestamp
    output: list[NumericFeature] = []

    output.append(
        _numeric(
            "close_price",
            latest.close,
            observed_at,
            method="observed_bar_close",
            sample_size=1,
            window_id="current_bar",
        )
    )

    return_short = _period_return(closes, config.short_return_period)
    return_long = _period_return(closes, config.long_return_period)
    output.append(
        _numeric(
            "return_short",
            return_short,
            observed_at,
            method="close_return",
            sample_size=min(len(closes), config.short_return_period + 1),
            window_id=f"bars:{config.short_return_period + 1}",
            reason="insufficient_return_history",
        )
    )
    output.append(
        _numeric(
            "return_long",
            return_long,
            observed_at,
            method="close_return",
            sample_size=min(len(closes), config.long_return_period + 1),
            window_id=f"bars:{config.long_return_period + 1}",
            reason="insufficient_return_history",
        )
    )
    acceleration_return = _period_return(closes, config.acceleration_period)
    prior_return = _period_return(closes[:-1], config.acceleration_period)
    acceleration = (
        acceleration_return - prior_return
        if acceleration_return is not None and prior_return is not None
        else None
    )
    output.append(
        _numeric(
            "price_acceleration",
            acceleration,
            observed_at,
            method="latest_return_minus_prior_return",
            sample_size=min(len(closes), config.acceleration_period + 2),
            window_id=f"bars:{config.acceleration_period + 2}",
            reason="insufficient_acceleration_history",
        )
    )

    gap = _session_gap(bars)
    output.append(
        _numeric(
            "gap_return",
            gap,
            observed_at,
            method="current_session_open_over_prior_session_close",
            sample_size=2 if gap is not None else 1,
            window_id=f"sessions:{_session_id(latest)}:and_prior",
            reason="prior_session_close_unavailable",
        )
    )
    current_range = latest.high - latest.low
    range_position = (latest.close - latest.low) / current_range if current_range > 0 else None
    output.append(
        _numeric(
            "range_position",
            range_position,
            observed_at,
            method="close_position_within_current_bar",
            sample_size=1,
            window_id="current_bar",
            reason="zero_current_range",
        )
    )

    true_ranges = _true_ranges(bars)
    prior_true_ranges = true_ranges[-(config.atr_window + 1) : -1]
    atr_prior = mean(prior_true_ranges) if len(prior_true_ranges) == config.atr_window else None
    output.append(
        _numeric(
            "atr_prior",
            atr_prior,
            observed_at,
            method="mean_true_range_strictly_prior",
            sample_size=len(prior_true_ranges),
            window_id=f"prior_true_ranges:{config.atr_window}",
            reason="insufficient_prior_true_ranges",
        )
    )
    normalized_range = current_range / atr_prior if atr_prior and atr_prior > 0 else None
    output.append(
        _numeric(
            "normalized_range_atr",
            normalized_range,
            observed_at,
            method="current_range_over_prior_atr",
            sample_size=len(prior_true_ranges),
            window_id=f"prior_true_ranges:{config.atr_window}",
            reason="prior_atr_unavailable_or_zero",
        )
    )

    returns = _one_bar_returns(closes)
    short_vol = _tail_pstdev(returns, config.volatility_short_window)
    prior_long_returns = (
        returns[-(config.volatility_long_window + 1) : -1]
        if len(returns) >= config.volatility_long_window + 1
        else []
    )
    long_vol = pstdev(prior_long_returns) if len(prior_long_returns) >= 2 else None
    volatility_ratio = (
        short_vol / long_vol
        if short_vol is not None and long_vol is not None and long_vol > 0
        else None
    )
    output.append(
        _numeric(
            "realized_volatility_ratio",
            volatility_ratio,
            observed_at,
            method="recent_return_volatility_over_prior_return_volatility",
            sample_size=len(prior_long_returns),
            window_id=(
                f"recent:{config.volatility_short_window};prior:{config.volatility_long_window}"
            ),
            reason="insufficient_or_zero_prior_volatility",
        )
    )

    prior_volumes = volumes[-(config.volume_window + 1) : -1]
    prior_volume_mean = mean(prior_volumes) if len(prior_volumes) == config.volume_window else None
    relative_volume = (
        volumes[-1] / prior_volume_mean if prior_volume_mean and prior_volume_mean > 0 else None
    )
    output.append(
        _numeric(
            "relative_volume",
            relative_volume,
            observed_at,
            method="current_volume_over_strictly_prior_mean",
            sample_size=len(prior_volumes),
            window_id=f"prior_volumes:{config.volume_window}",
            reason="insufficient_or_zero_prior_volume",
        )
    )
    volume_acceleration = (
        (volumes[-1] / volumes[-2]) - 1.0 if len(volumes) >= 2 and volumes[-2] > 0 else None
    )
    output.append(
        _numeric(
            "volume_acceleration",
            volume_acceleration,
            observed_at,
            method="current_volume_over_previous_volume_minus_one",
            sample_size=min(len(volumes), 2),
            window_id="bars:2",
            reason="previous_volume_unavailable_or_zero",
        )
    )
    volume_zscore = _zscore_against_prior(volumes[-1], prior_volumes)
    output.append(
        _numeric(
            "volume_rolling_zscore",
            volume_zscore,
            observed_at,
            method="population_zscore_against_strictly_prior_window",
            sample_size=len(prior_volumes),
            window_id=f"prior_volumes:{config.volume_window}",
            reason="insufficient_or_zero_variance_prior_volume",
        )
    )
    volume_percentile = (
        _percentile(volumes[-1], prior_volumes)
        if len(prior_volumes) == config.volume_window
        else None
    )
    output.append(
        _numeric(
            "volume_rolling_percentile",
            volume_percentile,
            observed_at,
            method="midrank_percentile_against_strictly_prior_window",
            sample_size=len(prior_volumes),
            window_id=f"prior_volumes:{config.volume_window}",
            reason="insufficient_prior_volume_window",
        )
    )
    output.append(
        _numeric(
            "dollar_volume_proxy",
            latest.close * latest.volume,
            observed_at,
            method="close_times_bar_volume_proxy",
            sample_size=1,
            window_id="current_bar",
            source_kind="OHLCV_CLOSE_VOLUME_PROXY",
        )
    )
    minutes = _minutes_since_open(latest.timestamp)
    output.append(
        _numeric(
            "minutes_since_open",
            float(minutes),
            observed_at,
            method="exchange_time_minus_0930_eastern",
            sample_size=1,
            window_id=f"session:{latest.timestamp.astimezone(EASTERN).date().isoformat()}",
        )
    )
    if latest.timestamp > decision_at:
        raise FeatureInputError("feature observation after decision time")
    return output


def _rich_features(
    bars: tuple[MarketBar, ...],
    *,
    benchmark_bars: tuple[MarketBar, ...] | None,
    decision_at: datetime,
    config: FeatureConfig,
) -> list[NumericFeature]:
    latest = bars[-1]
    observed_at = latest.timestamp
    closes = [bar.close for bar in bars]
    returns = _one_bar_returns(closes)
    output: list[NumericFeature] = []

    vwap_values = _session_vwap_proxy(bars)
    vwap = vwap_values[-1]
    prior_vwap = vwap_values[-2] if len(vwap_values) >= 2 else None
    prior_close = bars[-2].close if len(bars) >= 2 else None
    displacement = (latest.close / vwap) - 1.0 if vwap and vwap > 0 else None
    slope = (vwap / prior_vwap) - 1.0 if vwap and prior_vwap and prior_vwap > 0 else None
    reclaim: float | None = None
    loss: float | None = None
    if prior_close is not None and prior_vwap is not None and vwap is not None:
        reclaim = float(prior_close <= prior_vwap and latest.close > vwap)
        loss = float(prior_close >= prior_vwap and latest.close < vwap)
    for name, value, method, reason in (
        (
            "session_vwap_proxy",
            vwap,
            "cumulative_typical_price_times_volume_over_volume",
            "session_volume_unavailable_or_zero",
        ),
        (
            "vwap_proxy_displacement",
            displacement,
            "close_over_session_vwap_proxy_minus_one",
            "session_vwap_proxy_unavailable",
        ),
        (
            "vwap_proxy_slope",
            slope,
            "current_over_prior_session_vwap_proxy_minus_one",
            "prior_session_vwap_proxy_unavailable",
        ),
        (
            "vwap_proxy_reclaim",
            reclaim,
            "prior_close_below_proxy_and_current_close_above_proxy",
            "prior_vwap_proxy_state_unavailable",
        ),
        (
            "vwap_proxy_loss",
            loss,
            "prior_close_above_proxy_and_current_close_below_proxy",
            "prior_vwap_proxy_state_unavailable",
        ),
    ):
        output.append(
            _numeric(
                name,
                value,
                observed_at,
                method=method,
                sample_size=len(vwap_values),
                window_id=_session_id(latest),
                reason=reason,
                source_kind="OHLCV_TYPICAL_PRICE_VWAP_PROXY",
            )
        )

    market_relative = _market_relative_return(
        bars,
        benchmark_bars,
        period=config.long_return_period,
    )
    output.append(
        _numeric(
            "market_relative_strength",
            market_relative,
            observed_at,
            method="symbol_return_minus_timestamp_aligned_benchmark_return",
            sample_size=(config.long_return_period + 1 if market_relative is not None else 0),
            window_id=f"aligned_bars:{config.long_return_period + 1}",
            reason="benchmark_missing_or_timestamp_misaligned",
            source_kind="OHLCV_ALIGNED_BENCHMARK",
        )
    )

    volume_acceleration = _value(output, "volume_acceleration")
    price_return = _period_return(closes, 1)
    if volume_acceleration is None:
        cheap_volume_acceleration = (
            (bars[-1].volume / bars[-2].volume) - 1.0
            if len(bars) >= 2 and bars[-2].volume > 0
            else None
        )
    else:
        cheap_volume_acceleration = volume_acceleration
    divergence = (
        1.0
        if price_return is not None
        and cheap_volume_acceleration is not None
        and price_return * cheap_volume_acceleration < 0
        else 0.0
        if price_return is not None and cheap_volume_acceleration is not None
        else None
    )
    output.append(
        _numeric(
            "price_volume_divergence",
            divergence,
            observed_at,
            method="opposite_sign_latest_price_return_and_volume_acceleration",
            sample_size=min(len(bars), 2),
            window_id="bars:2",
            reason="price_or_volume_change_unavailable",
        )
    )

    structure = _structure_features(bars, config.structure_lookback)
    for name, value in structure.items():
        output.append(
            _numeric(
                name,
                value,
                observed_at,
                method=f"strictly_prior_{config.structure_lookback}_bar_structure",
                sample_size=min(max(len(bars) - 1, 0), config.structure_lookback),
                window_id=f"prior_bars:{config.structure_lookback}",
                reason="insufficient_prior_structure_history",
            )
        )

    efficiency = _directional_efficiency(closes, config.volatility_long_window)
    autocorrelation = _lag_one_autocorrelation(returns, config.volatility_long_window)
    range_persistence = _range_persistence(bars, config.structure_lookback)
    for name, value, method in (
        (
            "directional_efficiency",
            efficiency,
            "absolute_net_change_over_sum_absolute_changes",
        ),
        ("return_autocorrelation_lag1", autocorrelation, "lag_one_population_correlation"),
        (
            "range_persistence",
            range_persistence,
            "fraction_of_recent_closes_in_same_range_half",
        ),
    ):
        output.append(
            _numeric(
                name,
                value,
                observed_at,
                method=method,
                sample_size=min(len(bars), config.volatility_long_window + 1),
                window_id=f"bars:{config.volatility_long_window + 1}",
                reason="insufficient_regime_history",
            )
        )

    for name in ("true_cvd", "aggressor_imbalance"):
        output.append(
            NumericFeature(
                name=name,
                value=None,
                availability=Availability.UNSUPPORTED,
                method="requires_aggressor_side_trade_classification",
                sample_size=0,
                window_id="unsupported:ohlcv",
                observed_at=observed_at,
                source_kind="UNSUPPORTED_BY_OHLCV",
                reason="aggressor_side_trade_evidence_required",
            )
        )
    if observed_at > decision_at:
        raise FeatureInputError("rich feature observation after decision time")
    return output


def _add_cross_section_features(
    snapshots: list[FeatureSnapshot],
    *,
    config: FeatureConfig,
) -> tuple[FeatureSnapshot, ...]:
    if not snapshots:
        return ()
    aligned = len({snapshot.latest_bar_at for snapshot in snapshots}) == 1
    feature_specs = (
        ("gap_return", "cross_section_gap_zscore", "zscore"),
        ("relative_volume", "cross_section_relative_volume_percentile", "percentile"),
        ("dollar_volume_proxy", "cross_section_liquidity_percentile", "percentile"),
    )
    additions: dict[str, list[NumericFeature]] = {snapshot.symbol: [] for snapshot in snapshots}
    for source_name, target_name, method in feature_specs:
        available = [
            (snapshot, feature)
            for snapshot in snapshots
            if (feature := snapshot.numeric(source_name)) is not None
            and feature.availability is Availability.AVAILABLE
            and feature.value is not None
        ]
        sample_size = len(available)
        for snapshot in snapshots:
            source = snapshot.numeric(source_name)
            value: float | None = None
            reason = "insufficient_timestamp_aligned_cross_section"
            if (
                aligned
                and sample_size >= config.min_cross_section_size
                and source is not None
                and source.value is not None
            ):
                values = [
                    float(feature.value) for _, feature in available if feature.value is not None
                ]
                if method == "zscore":
                    value = _zscore_against_population(float(source.value), values)
                    reason = "zero_cross_section_variance"
                else:
                    value = _percentile(float(source.value), values)
                    reason = "cross_section_value_unavailable"
            additions[snapshot.symbol].append(
                _numeric(
                    target_name,
                    value,
                    snapshot.latest_bar_at,
                    method=f"timestamp_aligned_cross_section_{method}",
                    sample_size=sample_size,
                    window_id=(
                        f"dataset:{snapshot.dataset_id};as_of:{snapshot.latest_bar_at.isoformat()}"
                    ),
                    reason=reason,
                    source_kind="OHLCV_TIMESTAMP_ALIGNED_CROSS_SECTION",
                )
            )
    return tuple(
        _snapshot(
            symbol=snapshot.symbol,
            decision_at=snapshot.decision_at,
            universe_id=snapshot.universe_id,
            dataset_id=snapshot.dataset_id,
            stage=snapshot.stage,
            latest_bar_at=snapshot.latest_bar_at,
            numerical=snapshot.numerical + tuple(additions[snapshot.symbol]),
            categorical=snapshot.categorical,
            data_quality=snapshot.data_quality,
            limitations=snapshot.limitations,
        )
        for snapshot in snapshots
    )


def _snapshot(
    *,
    symbol: str,
    decision_at: datetime,
    universe_id: str,
    dataset_id: str,
    stage: FeatureStage,
    latest_bar_at: datetime,
    numerical: tuple[NumericFeature, ...],
    categorical: tuple[CategoricalFeature, ...],
    data_quality: DataQuality,
    limitations: tuple[str, ...],
) -> FeatureSnapshot:
    unavailable_names = {
        feature.name for feature in numerical if feature.availability is not Availability.AVAILABLE
    }
    unavailable_names.update(
        feature.name
        for feature in categorical
        if feature.availability is not Availability.AVAILABLE
    )
    unavailable = tuple(sorted(unavailable_names))
    payload = {
        "symbol": symbol,
        "decision_at": decision_at,
        "universe_id": universe_id,
        "dataset_id": dataset_id,
        "stage": stage,
        "latest_bar_at": latest_bar_at,
        "numerical": numerical,
        "categorical": categorical,
        "unavailable_features": unavailable,
        "data_quality": data_quality,
        "limitations": limitations,
    }
    return FeatureSnapshot(
        snapshot_id=stable_identity("feature", payload),
        symbol=symbol,
        decision_at=decision_at,
        market_date=decision_at.astimezone(EASTERN).date().isoformat(),
        universe_id=universe_id,
        dataset_id=dataset_id,
        stage=stage,
        latest_bar_at=latest_bar_at,
        numerical=numerical,
        categorical=categorical,
        unavailable_features=unavailable,
        data_quality=data_quality,
        limitations=limitations,
    )


def _numeric(
    name: str,
    value: float | int | None,
    observed_at: datetime,
    *,
    method: str,
    sample_size: int,
    window_id: str,
    reason: str | None = None,
    source_kind: str = "OHLCV_BAR",
) -> NumericFeature:
    decimal_value = _decimal(value) if value is not None and math.isfinite(float(value)) else None
    return NumericFeature(
        name=name,
        value=decimal_value,
        availability=(
            Availability.AVAILABLE if decimal_value is not None else Availability.INSUFFICIENT_DATA
        ),
        method=method,
        sample_size=sample_size,
        window_id=window_id,
        observed_at=observed_at,
        source_kind=source_kind,
        reason=None if decimal_value is not None else reason,
    )


def _validate_bars(
    symbol: str,
    bars: tuple[MarketBar, ...],
    decision_at: datetime,
) -> None:
    previous: datetime | None = None
    for bar in bars:
        _require_aware(bar.timestamp, f"{symbol}.timestamp")
        if bar.symbol != symbol:
            raise FeatureInputError(f"bar symbol {bar.symbol} does not match {symbol}")
        if bar.timestamp > decision_at:
            raise FeatureInputError(f"{symbol} observation after decision time")
        if previous is not None and bar.timestamp <= previous:
            raise FeatureInputError(f"{symbol} bars must be strictly monotonic and unique")
        previous = bar.timestamp
        prices = (bar.open, bar.high, bar.low, bar.close)
        if any(not math.isfinite(value) or value <= 0 for value in prices):
            raise FeatureInputError(f"{symbol} contains non-positive or non-finite price")
        if bar.high < max(bar.open, bar.close, bar.low):
            raise FeatureInputError(f"{symbol} bar high is inconsistent")
        if bar.low > min(bar.open, bar.close, bar.high):
            raise FeatureInputError(f"{symbol} bar low is inconsistent")
        if bar.volume < 0:
            raise FeatureInputError(f"{symbol} contains negative volume")


def _session_segment_feature(bar: MarketBar, decision_at: datetime) -> CategoricalFeature:
    if bar.timestamp > decision_at:
        raise FeatureInputError("session feature observation after decision time")
    local = bar.timestamp.astimezone(EASTERN).time().replace(tzinfo=None)
    if local < time(9, 30):
        segment = SessionSegment.PREMARKET
    elif local < time(10, 0):
        segment = SessionSegment.OPENING
    elif local < time(12, 0):
        segment = SessionSegment.MORNING
    elif local < time(14, 0):
        segment = SessionSegment.LUNCH
    elif local < time(15, 0):
        segment = SessionSegment.AFTERNOON
    elif local < time(16, 0):
        segment = SessionSegment.POWER_HOUR
    else:
        segment = SessionSegment.AFTER_HOURS
    return CategoricalFeature(
        name="session_segment",
        value=segment.value,
        availability=Availability.AVAILABLE,
        method="exchange_time_bucket_eastern",
        observed_at=bar.timestamp,
    )


def _data_quality(
    bar_count: int,
    latest_at: datetime,
    decision_at: datetime,
    config: FeatureConfig,
) -> DataQuality:
    if bar_count < 2:
        return DataQuality.INSUFFICIENT_DATA
    required = max(
        config.atr_window + 1,
        config.volume_window + 1,
        config.volatility_long_window + 2,
    )
    if bar_count >= required and latest_at == decision_at:
        return DataQuality.HIGH
    if bar_count >= required:
        return DataQuality.MEDIUM
    return DataQuality.LOW


def _period_return(values: list[float], period: int) -> float | None:
    if len(values) <= period or values[-1 - period] <= 0:
        return None
    return (values[-1] / values[-1 - period]) - 1.0


def _one_bar_returns(values: list[float]) -> list[float]:
    return [
        (values[index] / values[index - 1]) - 1.0
        for index in range(1, len(values))
        if values[index - 1] > 0
    ]


def _true_ranges(bars: tuple[MarketBar, ...]) -> list[float]:
    output: list[float] = []
    for index, bar in enumerate(bars):
        if index == 0:
            output.append(bar.high - bar.low)
        else:
            prior_close = bars[index - 1].close
            output.append(
                max(
                    bar.high - bar.low,
                    abs(bar.high - prior_close),
                    abs(bar.low - prior_close),
                )
            )
    return output


def _tail_pstdev(values: list[float], period: int) -> float | None:
    if len(values) < period or period < 2:
        return None
    return pstdev(values[-period:])


def _zscore_against_prior(value: float, prior: list[float]) -> float | None:
    if len(prior) < 2:
        return None
    spread = pstdev(prior)
    return (value - mean(prior)) / spread if spread > 0 else None


def _zscore_against_population(value: float, population: list[float]) -> float | None:
    if len(population) < 2:
        return None
    spread = pstdev(population)
    return (value - mean(population)) / spread if spread > 0 else None


def _percentile(value: float, population: list[float]) -> float:
    less = sum(item < value for item in population)
    equal = sum(item == value for item in population)
    return (less + (equal * 0.5)) / len(population)


def _session_vwap_proxy(bars: tuple[MarketBar, ...]) -> list[float | None]:
    output: list[float | None] = []
    current_session: str | None = None
    cumulative_price_volume = 0.0
    cumulative_volume = 0
    for bar in bars:
        session = _session_id(bar)
        if session != current_session:
            current_session = session
            cumulative_price_volume = 0.0
            cumulative_volume = 0
        typical_price = (bar.high + bar.low + bar.close) / 3.0
        cumulative_price_volume += typical_price * bar.volume
        cumulative_volume += bar.volume
        output.append(
            cumulative_price_volume / cumulative_volume if cumulative_volume > 0 else None
        )
    return output


def _session_id(bar: MarketBar) -> str:
    return bar.exchange_session_id or bar.timestamp.astimezone(EASTERN).date().isoformat()


def _session_gap(bars: tuple[MarketBar, ...]) -> float | None:
    current_session = _session_id(bars[-1])
    first_current_index = len(bars) - 1
    while first_current_index > 0 and _session_id(bars[first_current_index - 1]) == current_session:
        first_current_index -= 1
    if first_current_index == 0:
        return None
    current_open = bars[first_current_index].open
    prior_close = bars[first_current_index - 1].close
    return (current_open / prior_close) - 1.0 if prior_close > 0 else None


def _market_relative_return(
    bars: tuple[MarketBar, ...],
    benchmark_bars: tuple[MarketBar, ...] | None,
    *,
    period: int,
) -> float | None:
    if benchmark_bars is None or len(bars) <= period or len(benchmark_bars) <= period:
        return None
    symbol_slice = bars[-(period + 1) :]
    benchmark_slice = benchmark_bars[-(period + 1) :]
    if [bar.timestamp for bar in symbol_slice] != [bar.timestamp for bar in benchmark_slice]:
        return None
    symbol_return = _period_return([bar.close for bar in symbol_slice], period)
    benchmark_return = _period_return([bar.close for bar in benchmark_slice], period)
    if symbol_return is None or benchmark_return is None:
        return None
    return symbol_return - benchmark_return


def _structure_features(
    bars: tuple[MarketBar, ...],
    lookback: int,
) -> dict[str, float | None]:
    if len(bars) <= lookback:
        return {
            "breakout_signal": None,
            "breakdown_signal": None,
            "failed_breakout_signal": None,
            "failed_breakdown_signal": None,
            "failed_extension_signal": None,
            "exhaustion_signal": None,
        }
    current = bars[-1]
    prior = bars[-(lookback + 1) : -1]
    prior_high = max(bar.high for bar in prior)
    prior_low = min(bar.low for bar in prior)
    breakout = float(current.close > prior_high)
    breakdown = float(current.close < prior_low)
    failed_breakout = float(current.high > prior_high and current.close <= prior_high)
    failed_breakdown = float(current.low < prior_low and current.close >= prior_low)
    current_range = current.high - current.low
    range_position = (current.close - current.low) / current_range if current_range > 0 else 0.5
    failed_extension = float(failed_breakout == 1.0 or failed_breakdown == 1.0)
    exhaustion = float(
        (current.high > prior_high and range_position <= 0.25)
        or (current.low < prior_low and range_position >= 0.75)
    )
    return {
        "breakout_signal": breakout,
        "breakdown_signal": breakdown,
        "failed_breakout_signal": failed_breakout,
        "failed_breakdown_signal": failed_breakdown,
        "failed_extension_signal": failed_extension,
        "exhaustion_signal": exhaustion,
    }


def _directional_efficiency(values: list[float], period: int) -> float | None:
    if len(values) <= period:
        return None
    window = values[-(period + 1) :]
    path = sum(abs(window[index] - window[index - 1]) for index in range(1, len(window)))
    return abs(window[-1] - window[0]) / path if path > 0 else None


def _lag_one_autocorrelation(values: list[float], period: int) -> float | None:
    if len(values) < period + 1 or period < 3:
        return None
    window = values[-(period + 1) :]
    left = window[:-1]
    right = window[1:]
    left_mean = mean(left)
    right_mean = mean(right)
    numerator = sum((a - left_mean) * (b - right_mean) for a, b in zip(left, right, strict=True))
    left_ss = sum((a - left_mean) ** 2 for a in left)
    right_ss = sum((b - right_mean) ** 2 for b in right)
    denominator = math.sqrt(left_ss * right_ss)
    return numerator / denominator if denominator > 0 else None


def _range_persistence(bars: tuple[MarketBar, ...], lookback: int) -> float | None:
    if len(bars) < lookback:
        return None
    recent = bars[-lookback:]
    signs: list[int] = []
    for bar in recent:
        width = bar.high - bar.low
        if width <= 0:
            continue
        signs.append(1 if (bar.close - bar.low) / width >= 0.5 else -1)
    if len(signs) != lookback:
        return None
    dominant = max(signs.count(1), signs.count(-1))
    return dominant / len(signs)


def _minutes_since_open(value: datetime) -> int:
    local = value.astimezone(EASTERN)
    return (local.hour * 60 + local.minute) - (9 * 60 + 30)


def _value(features: list[NumericFeature], name: str) -> float | None:
    feature = next((item for item in features if item.name == name), None)
    return float(feature.value) if feature is not None and feature.value is not None else None


def _decimal(value: float | int) -> Decimal:
    return Decimal(str(round(float(value), 12)))


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise FeatureInputError(f"{field_name} must be timezone-aware")


__all__ = [
    "FeatureConfig",
    "FeatureInputError",
    "build_feature_snapshots",
]
