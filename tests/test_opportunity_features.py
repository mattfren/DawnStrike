from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.opportunity.features import (
    FeatureConfig,
    FeatureInputError,
    build_feature_snapshots,
)
from intraday_scanner.v2.opportunity.models import (
    Availability,
    DataQuality,
    FeatureSnapshot,
    FeatureStage,
    NumericFeature,
    RegimeState,
)
from intraday_scanner.v2.opportunity.regimes import (
    classify_market_regime,
    classify_security_regime,
)

EASTERN = ZoneInfo("America/New_York")
DECISION = datetime(2026, 8, 11, 9, 34, tzinfo=EASTERN)


def _bar(
    symbol: str,
    timestamp: datetime,
    close: float,
    volume: int,
    *,
    open_price: float | None = None,
    session: str | None = None,
) -> MarketBar:
    open_value = close if open_price is None else open_price
    return MarketBar(
        symbol=symbol,
        timestamp=timestamp,
        open=open_value,
        high=max(open_value, close) + 1,
        low=min(open_value, close) - 1,
        close=close,
        volume=volume,
        exchange_session_id=session,
    )


def _series(
    symbol: str,
    closes: tuple[float, ...],
    volumes: tuple[int, ...],
    *,
    current_open: float | None = None,
    offset_seconds: int = 0,
) -> tuple[MarketBar, ...]:
    previous = datetime(2026, 8, 10, 15, 59, tzinfo=EASTERN) + timedelta(seconds=offset_seconds)
    current_times = tuple(
        datetime(2026, 8, 11, 9, 30 + index, tzinfo=EASTERN) + timedelta(seconds=offset_seconds)
        for index in range(len(closes) - 1)
    )
    bars = [
        _bar(symbol, previous, closes[0], volumes[0], session="2026-08-10"),
    ]
    for index, timestamp in enumerate(current_times, start=1):
        bars.append(
            _bar(
                symbol,
                timestamp,
                closes[index],
                volumes[index],
                open_price=current_open if index == 1 else closes[index - 1],
                session="2026-08-11",
            )
        )
    return tuple(bars)


def _dataset(*, misaligned_benchmark: bool = False) -> MarketDataset:
    bars = {
        "ABC": _series(
            "ABC",
            (100, 111, 112, 113, 114, 116),
            (100, 100, 100, 100, 100, 400),
            current_open=110,
        ),
        "DEF": _series(
            "DEF",
            (50, 50, 50, 50, 50, 50),
            (100, 100, 100, 100, 100, 100),
            current_open=50,
        ),
        "GHI": _series(
            "GHI",
            (25, 25, 25, 25, 25, 25),
            (200, 200, 200, 200, 200, 200),
            current_open=25,
        ),
        "SPY": _series(
            "SPY",
            (400, 401, 402, 403, 404, 405),
            (1000, 1000, 1000, 1000, 1000, 1000),
            current_open=401,
            offset_seconds=-30 if misaligned_benchmark else 0,
        ),
    }
    return MarketDataset(
        dataset_id="fixture-dataset",
        source_kind="fixture",
        timeframe="1m",
        bars_by_symbol=bars,
    )


def _value(snapshot: FeatureSnapshot, name: str) -> Decimal | None:
    feature = snapshot.numeric(name)
    assert feature is not None
    return feature.value


def test_hand_calculated_price_volume_gap_and_acceleration_features() -> None:
    config = FeatureConfig(short_return_period=2, acceleration_period=1)
    snapshot = build_feature_snapshots(
        _dataset(),
        decision_at=DECISION,
        universe_id="universe",
        stage=FeatureStage.CHEAP,
        symbols=("ABC", "DEF", "GHI"),
        benchmark_symbol="SPY",
        config=config,
    )[0]
    assert _value(snapshot, "gap_return") == Decimal("0.1")
    assert _value(snapshot, "return_short") == Decimal(str(round(116 / 113 - 1, 12)))
    expected_acceleration = (116 / 114 - 1) - (114 / 113 - 1)
    assert _value(snapshot, "price_acceleration") == Decimal(str(round(expected_acceleration, 12)))
    assert _value(snapshot, "relative_volume") == Decimal("4.0")
    assert _value(snapshot, "volume_acceleration") == Decimal("3.0")
    assert _value(snapshot, "cross_section_relative_volume_percentile") == Decimal("0.833333333333")


def test_vwap_proxy_label_benchmark_alignment_and_order_flow_truth() -> None:
    snapshot = build_feature_snapshots(
        _dataset(),
        decision_at=DECISION,
        universe_id="universe",
        stage=FeatureStage.RICH,
        symbols=("ABC", "DEF", "GHI"),
        benchmark_symbol="SPY",
    )[0]
    vwap = snapshot.numeric("session_vwap_proxy")
    assert vwap is not None
    assert vwap.source_kind == "OHLCV_TYPICAL_PRICE_VWAP_PROXY"
    session_bars = _dataset().bars_by_symbol["ABC"][1:]
    expected = sum(((bar.high + bar.low + bar.close) / 3) * bar.volume for bar in session_bars)
    expected /= sum(bar.volume for bar in session_bars)
    assert vwap.value == Decimal(str(round(expected, 12)))
    assert _value(snapshot, "market_relative_strength") is not None
    true_cvd = snapshot.numeric("true_cvd")
    assert true_cvd is not None
    assert true_cvd.availability is Availability.UNSUPPORTED
    assert true_cvd.value is None
    assert "aggressor_side_trade_evidence_required" in (true_cvd.reason or "")

    misaligned = build_feature_snapshots(
        _dataset(misaligned_benchmark=True),
        decision_at=DECISION,
        universe_id="universe",
        stage=FeatureStage.RICH,
        symbols=("ABC",),
        benchmark_symbol="SPY",
    )[0]
    relative = misaligned.numeric("market_relative_strength")
    assert relative is not None
    assert relative.availability is Availability.INSUFFICIENT_DATA
    assert relative.value is None


def test_insufficient_samples_are_unavailable_not_zero() -> None:
    timestamp = datetime(2026, 8, 11, 9, 30, tzinfo=EASTERN)
    dataset = MarketDataset(
        dataset_id="short",
        source_kind="fixture",
        timeframe="1m",
        bars_by_symbol={"ABC": (_bar("ABC", timestamp, 10, 100),)},
    )
    snapshot = build_feature_snapshots(
        dataset,
        decision_at=timestamp,
        universe_id="u",
        stage=FeatureStage.CHEAP,
        symbols=("ABC",),
    )[0]
    for name in ("return_short", "relative_volume", "volume_rolling_zscore"):
        feature = snapshot.numeric(name)
        assert feature is not None
        assert feature.value is None
        assert feature.availability is Availability.INSUFFICIENT_DATA
    assert snapshot.data_quality is DataQuality.INSUFFICIENT_DATA


@pytest.mark.parametrize("case", ("naive", "future", "duplicate", "non_positive"))
def test_invalid_bar_inputs_fail_closed(case: str) -> None:
    first = datetime(2026, 8, 11, 9, 30, tzinfo=UTC)
    second = first + timedelta(minutes=1)
    if case == "naive":
        bars = (_bar("ABC", first.replace(tzinfo=None), 10, 100),)
    elif case == "future":
        bars = (_bar("ABC", second, 10, 100),)
    elif case == "duplicate":
        bars = (_bar("ABC", first, 10, 100), _bar("ABC", first, 11, 100))
    else:
        bars = (MarketBar("ABC", first, 0, 1, 0.5, 0.75, 100),)
    dataset = MarketDataset("bad", "fixture", "1m", {"ABC": bars})
    with pytest.raises(FeatureInputError):
        build_feature_snapshots(
            dataset,
            decision_at=first,
            universe_id="u",
            stage=FeatureStage.CHEAP,
            symbols=("ABC",),
        )


def _regime_snapshot(
    **values: Decimal | None,
) -> FeatureSnapshot:
    numerical = tuple(
        NumericFeature(
            name=name,
            value=value,
            availability=(
                Availability.AVAILABLE if value is not None else Availability.INSUFFICIENT_DATA
            ),
            method="fixture",
            sample_size=8 if value is not None else 0,
            window_id="fixture",
            observed_at=DECISION,
            source_kind="OHLCV_BAR",
            reason=None if value is not None else "fixture_missing",
        )
        for name, value in values.items()
    )
    unavailable = tuple(sorted(item.name for item in numerical if item.value is None))
    return FeatureSnapshot(
        snapshot_id=f"snapshot:{hash(tuple(values.items()))}",
        symbol="ABC",
        decision_at=DECISION,
        market_date="2026-08-11",
        universe_id="u",
        dataset_id="d",
        stage=FeatureStage.RICH,
        latest_bar_at=DECISION,
        numerical=numerical,
        categorical=(),
        unavailable_features=unavailable,
        data_quality=(
            DataQuality.INSUFFICIENT_DATA
            if any(value is None for value in values.values())
            else DataQuality.HIGH
        ),
    )


@pytest.mark.parametrize(
    ("expected", "values"),
    (
        (
            RegimeState.TREND_UP,
            {
                "return_long": Decimal("0.03"),
                "directional_efficiency": Decimal("0.8"),
                "realized_volatility_ratio": Decimal("1"),
            },
        ),
        (
            RegimeState.TREND_DOWN,
            {
                "return_long": Decimal("-0.03"),
                "directional_efficiency": Decimal("0.8"),
                "realized_volatility_ratio": Decimal("1"),
            },
        ),
        (
            RegimeState.MEAN_REVERTING,
            {
                "return_long": Decimal("0"),
                "directional_efficiency": Decimal("0.2"),
                "realized_volatility_ratio": Decimal("1"),
                "return_autocorrelation_lag1": Decimal("-0.5"),
            },
        ),
        (
            RegimeState.VOLATILITY_EXPANSION,
            {
                "return_long": Decimal("0"),
                "directional_efficiency": Decimal("0.5"),
                "realized_volatility_ratio": Decimal("2"),
            },
        ),
        (
            RegimeState.VOLATILITY_COMPRESSION,
            {
                "return_long": Decimal("0"),
                "directional_efficiency": Decimal("0.5"),
                "realized_volatility_ratio": Decimal("0"),
            },
        ),
        (
            RegimeState.CHOP,
            {
                "return_long": Decimal("0"),
                "directional_efficiency": Decimal("0.2"),
                "realized_volatility_ratio": Decimal("1"),
                "return_autocorrelation_lag1": Decimal("0.1"),
                "range_persistence": Decimal("0.5"),
            },
        ),
        (
            RegimeState.BREAKOUT,
            {
                "return_long": Decimal("0.02"),
                "directional_efficiency": Decimal("0.7"),
                "realized_volatility_ratio": Decimal("1"),
                "breakout_signal": Decimal("1"),
            },
        ),
        (
            RegimeState.BREAKDOWN,
            {
                "return_long": Decimal("-0.02"),
                "directional_efficiency": Decimal("0.7"),
                "realized_volatility_ratio": Decimal("1"),
                "breakdown_signal": Decimal("1"),
            },
        ),
        (
            RegimeState.EXHAUSTION,
            {
                "return_long": Decimal("0"),
                "directional_efficiency": Decimal("0.5"),
                "realized_volatility_ratio": Decimal("1"),
                "exhaustion_signal": Decimal("1"),
            },
        ),
        (
            RegimeState.INSUFFICIENT_DATA,
            {
                "return_long": None,
                "directional_efficiency": Decimal("0.5"),
                "realized_volatility_ratio": Decimal("1"),
            },
        ),
    ),
)
def test_security_regime_fixture_matrix(
    expected: RegimeState,
    values: dict[str, Decimal | None],
) -> None:
    assert classify_security_regime(_regime_snapshot(**values)).state is expected


def test_market_and_security_regimes_are_separate_receipts() -> None:
    snapshot = _regime_snapshot(
        return_long=Decimal("0.03"),
        directional_efficiency=Decimal("0.8"),
        realized_volatility_ratio=Decimal("1"),
    )
    market = classify_market_regime(snapshot)
    security = classify_security_regime(snapshot)
    assert market.state is security.state is RegimeState.TREND_UP
    assert market.regime_id != security.regime_id
