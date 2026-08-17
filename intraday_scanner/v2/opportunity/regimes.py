"""Causal heuristic market and security regime classification."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from intraday_scanner.v2.opportunity.models import (
    Availability,
    DataQuality,
    EvidenceKind,
    FeatureSnapshot,
    MarketRegime,
    NumericFeature,
    RegimeState,
    SecurityRegime,
    stable_identity,
)

REGIME_METHOD_VERSION = "opportunity-regime-heuristic-v1"
_MEASUREMENT_NAMES = (
    "return_long",
    "directional_efficiency",
    "realized_volatility_ratio",
    "range_persistence",
    "return_autocorrelation_lag1",
    "vwap_proxy_slope",
    "breakout_signal",
    "breakdown_signal",
    "failed_extension_signal",
    "exhaustion_signal",
)


def classify_market_regime(
    snapshot: FeatureSnapshot | None,
    *,
    decision_at: datetime | None = None,
) -> MarketRegime:
    """Classify a benchmark snapshot independently of candidate averages."""

    if snapshot is None:
        if decision_at is None:
            raise ValueError("decision_at is required when benchmark snapshot is unavailable")
        payload: dict[str, object] = {
            "state": RegimeState.INSUFFICIENT_DATA,
            "benchmark_symbol": None,
            "decision_at": decision_at,
        }
        return MarketRegime(
            regime_id=stable_identity("market-regime", payload),
            decision_at=decision_at,
            benchmark_symbol=None,
            state=RegimeState.INSUFFICIENT_DATA,
            measurements=(),
            confidence=None,
            evidence_kind=EvidenceKind.HEURISTIC,
            reasons=("benchmark_snapshot_unavailable",),
        )
    state, confidence, reasons, measurements = _classify(snapshot, market=True)
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "state": state,
        "confidence": confidence,
        "reasons": reasons,
    }
    return MarketRegime(
        regime_id=stable_identity("market-regime", payload),
        decision_at=snapshot.decision_at,
        benchmark_symbol=snapshot.symbol,
        state=state,
        measurements=measurements,
        confidence=confidence,
        evidence_kind=EvidenceKind.HEURISTIC,
        reasons=reasons,
    )


def classify_security_regime(snapshot: FeatureSnapshot) -> SecurityRegime:
    """Classify one security separately from the broader market regime."""

    state, confidence, reasons, measurements = _classify(snapshot, market=False)
    payload = {
        "snapshot_id": snapshot.snapshot_id,
        "state": state,
        "confidence": confidence,
        "reasons": reasons,
    }
    return SecurityRegime(
        regime_id=stable_identity("security-regime", payload),
        symbol=snapshot.symbol,
        decision_at=snapshot.decision_at,
        state=state,
        measurements=measurements,
        confidence=confidence,
        evidence_kind=EvidenceKind.HEURISTIC,
        reasons=reasons,
    )


def _classify(
    snapshot: FeatureSnapshot,
    *,
    market: bool,
) -> tuple[RegimeState, Decimal | None, tuple[str, ...], tuple[NumericFeature, ...]]:
    measurements = tuple(
        feature for name in _MEASUREMENT_NAMES if (feature := snapshot.numeric(name)) is not None
    )
    if snapshot.data_quality is DataQuality.INSUFFICIENT_DATA:
        return (
            RegimeState.INSUFFICIENT_DATA,
            None,
            ("snapshot_data_quality_insufficient",),
            measurements,
        )
    long_return = _value(snapshot, "return_long")
    efficiency = _value(snapshot, "directional_efficiency")
    volatility_ratio = _value(snapshot, "realized_volatility_ratio")
    if long_return is None or efficiency is None or volatility_ratio is None:
        return (
            RegimeState.INSUFFICIENT_DATA,
            None,
            ("required_regime_measurement_unavailable",),
            measurements,
        )

    autocorrelation = _value(snapshot, "return_autocorrelation_lag1")
    range_persistence = _value(snapshot, "range_persistence")
    breakout = _value(snapshot, "breakout_signal")
    breakdown = _value(snapshot, "breakdown_signal")
    failed_extension = _value(snapshot, "failed_extension_signal")
    exhaustion = _value(snapshot, "exhaustion_signal")

    if not market and exhaustion is not None and exhaustion >= Decimal("0.5"):
        return RegimeState.EXHAUSTION, Decimal("0.8"), (REGIME_METHOD_VERSION,), measurements
    if not market and failed_extension is not None and failed_extension >= Decimal("0.5"):
        return RegimeState.EXHAUSTION, Decimal("0.7"), (REGIME_METHOD_VERSION,), measurements
    if breakout is not None and breakout >= Decimal("0.5"):
        return RegimeState.BREAKOUT, Decimal("0.8"), (REGIME_METHOD_VERSION,), measurements
    if breakdown is not None and breakdown >= Decimal("0.5"):
        return RegimeState.BREAKDOWN, Decimal("0.8"), (REGIME_METHOD_VERSION,), measurements
    if volatility_ratio >= Decimal("1.5"):
        return (
            RegimeState.VOLATILITY_EXPANSION,
            _clip_confidence(volatility_ratio / Decimal("2")),
            (REGIME_METHOD_VERSION,),
            measurements,
        )
    if volatility_ratio <= Decimal("0.7"):
        return (
            RegimeState.VOLATILITY_COMPRESSION,
            _clip_confidence(Decimal("1") - volatility_ratio / Decimal("2")),
            (REGIME_METHOD_VERSION,),
            measurements,
        )
    if efficiency >= Decimal("0.6") and long_return >= Decimal("0.015"):
        return RegimeState.TREND_UP, Decimal("0.75"), (REGIME_METHOD_VERSION,), measurements
    if efficiency >= Decimal("0.6") and long_return <= Decimal("-0.015"):
        return RegimeState.TREND_DOWN, Decimal("0.75"), (REGIME_METHOD_VERSION,), measurements
    if (
        efficiency <= Decimal("0.45")
        and autocorrelation is not None
        and autocorrelation <= Decimal("-0.25")
    ):
        return (
            RegimeState.MEAN_REVERTING,
            Decimal("0.65"),
            (REGIME_METHOD_VERSION,),
            measurements,
        )
    if efficiency <= Decimal("0.35") and (
        range_persistence is None or range_persistence <= Decimal("0.67")
    ):
        return RegimeState.CHOP, Decimal("0.6"), (REGIME_METHOD_VERSION,), measurements
    return (
        RegimeState.UNKNOWN,
        Decimal("0.25"),
        ("no_heuristic_regime_rule_matched",),
        measurements,
    )


def _value(snapshot: FeatureSnapshot, name: str) -> Decimal | None:
    feature = snapshot.numeric(name)
    if (
        feature is None
        or feature.availability is not Availability.AVAILABLE
        or feature.value is None
    ):
        return None
    return feature.value


def _clip_confidence(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("0.9"), value))


__all__ = ["classify_market_regime", "classify_security_regime"]
