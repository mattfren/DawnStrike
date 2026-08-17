"""Strategy-independent anomaly detection for market-first opportunity discovery."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from intraday_scanner.v2.opportunity.models import (
    AnomalyEvidence,
    AnomalyType,
    Availability,
    EvidenceKind,
    FeatureSnapshot,
    OpportunityCandidate,
    stable_identity,
)


@dataclass(frozen=True)
class DiscoveryConfig:
    relative_volume: Decimal = Decimal("1.5")
    volume_acceleration: Decimal = Decimal("0.35")
    price_acceleration: Decimal = Decimal("0.01")
    gap_absolute: Decimal = Decimal("0.02")
    normalized_range: Decimal = Decimal("1.25")
    volatility_expansion: Decimal = Decimal("1.4")
    volatility_compression: Decimal = Decimal("0.7")
    vwap_displacement_absolute: Decimal = Decimal("0.015")
    market_relative_absolute: Decimal = Decimal("0.015")
    low_liquidity_percentile: Decimal = Decimal("0.1")
    threshold_version: str = "opportunity-discovery-heuristic-v1"

    def __post_init__(self) -> None:
        for name in (
            "relative_volume",
            "volume_acceleration",
            "price_acceleration",
            "gap_absolute",
            "normalized_range",
            "volatility_expansion",
            "volatility_compression",
            "vwap_displacement_absolute",
            "market_relative_absolute",
        ):
            value = getattr(self, name)
            if not value.is_finite() or value <= 0:
                raise ValueError(f"{name} must be finite and positive")
        if not self.low_liquidity_percentile.is_finite():
            raise ValueError("low_liquidity_percentile must be finite")
        if self.low_liquidity_percentile < 0:
            raise ValueError("low_liquidity_percentile cannot be negative")
        if self.low_liquidity_percentile > 1:
            raise ValueError("low_liquidity_percentile cannot exceed one")
        if not self.threshold_version.strip():
            raise ValueError("threshold_version cannot be blank")


DEFAULT_DISCOVERY_CONFIG = DiscoveryConfig()


def detect_anomalies(
    snapshot: FeatureSnapshot,
    *,
    config: DiscoveryConfig = DEFAULT_DISCOVERY_CONFIG,
) -> tuple[AnomalyEvidence, ...]:
    """Evaluate every supported anomaly family without importing strategy code."""

    checks = (
        _greater(
            snapshot,
            AnomalyType.RELATIVE_VOLUME,
            "relative_volume",
            config.relative_volume,
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.VOLUME_ACCELERATION,
            "volume_acceleration",
            config.volume_acceleration,
            config,
        ),
        _absolute(
            snapshot,
            AnomalyType.PRICE_ACCELERATION,
            "price_acceleration",
            config.price_acceleration,
            config,
        ),
        _absolute(
            snapshot,
            AnomalyType.GAP,
            "gap_return",
            config.gap_absolute,
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.RANGE_EXPANSION,
            "normalized_range_atr",
            config.normalized_range,
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.VOLATILITY_EXPANSION,
            "realized_volatility_ratio",
            config.volatility_expansion,
            config,
        ),
        _less(
            snapshot,
            AnomalyType.VOLATILITY_COMPRESSION,
            "realized_volatility_ratio",
            config.volatility_compression,
            config,
        ),
        _absolute(
            snapshot,
            AnomalyType.VWAP_PROXY_DISPLACEMENT,
            "vwap_proxy_displacement",
            config.vwap_displacement_absolute,
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.VWAP_PROXY_RECLAIM,
            "vwap_proxy_reclaim",
            Decimal("0.5"),
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.VWAP_PROXY_LOSS,
            "vwap_proxy_loss",
            Decimal("0.5"),
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.MARKET_RELATIVE_STRENGTH,
            "market_relative_strength",
            config.market_relative_absolute,
            config,
        ),
        _less(
            snapshot,
            AnomalyType.MARKET_RELATIVE_WEAKNESS,
            "market_relative_strength",
            -config.market_relative_absolute,
            config,
            strength_absolute=True,
        ),
        _greater(
            snapshot,
            AnomalyType.PRICE_VOLUME_DIVERGENCE,
            "price_volume_divergence",
            Decimal("0.5"),
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.BREAKOUT,
            "breakout_signal",
            Decimal("0.5"),
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.BREAKDOWN,
            "breakdown_signal",
            Decimal("0.5"),
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.FAILED_EXTENSION,
            "failed_extension_signal",
            Decimal("0.5"),
            config,
        ),
        _greater(
            snapshot,
            AnomalyType.EXHAUSTION,
            "exhaustion_signal",
            Decimal("0.5"),
            config,
        ),
        _less(
            snapshot,
            AnomalyType.LIQUIDITY,
            "cross_section_liquidity_percentile",
            config.low_liquidity_percentile,
            config,
        ),
        _unsupported(
            AnomalyType.CATALYST_ABNORMAL_RESPONSE,
            "point_in_time_catalyst_and_response_evidence_required",
            config,
        ),
        _unsupported(
            AnomalyType.TRUE_ORDER_FLOW,
            "aggressor_side_trade_evidence_required_no_ohlcv_approximation",
            config,
        ),
    )
    return checks


def discover_candidates(
    snapshots: tuple[FeatureSnapshot, ...],
    *,
    config: DiscoveryConfig = DEFAULT_DISCOVERY_CONFIG,
) -> tuple[OpportunityCandidate, ...]:
    """Create candidates from anomalies alone; strategy definitions are not inputs."""

    discovered: list[tuple[FeatureSnapshot, tuple[AnomalyEvidence, ...], Decimal]] = []
    for snapshot in snapshots:
        triggered = tuple(
            item for item in detect_anomalies(snapshot, config=config) if item.triggered
        )
        if not triggered:
            continue
        strength = max(
            (item.strength for item in triggered if item.strength is not None),
            default=Decimal("0"),
        )
        discovered.append((snapshot, triggered, strength))

    discovered.sort(key=lambda item: (-item[2], item[0].symbol, item[0].snapshot_id))
    candidates: list[OpportunityCandidate] = []
    for rank, (snapshot, anomalies, _) in enumerate(discovered, start=1):
        payload = {
            "symbol": snapshot.symbol,
            "decision_at": snapshot.decision_at,
            "feature_snapshot_id": snapshot.snapshot_id,
            "anomalies": anomalies,
        }
        candidates.append(
            OpportunityCandidate(
                candidate_id=stable_identity("candidate", payload),
                symbol=snapshot.symbol,
                decision_at=snapshot.decision_at,
                feature_snapshot_id=snapshot.snapshot_id,
                anomalies=anomalies,
                discovery_reasons=tuple(
                    f"{item.anomaly_type.value}:{item.method}" for item in anomalies
                ),
                discovery_rank=rank,
            )
        )
    return tuple(candidates)


def enrich_candidates(
    candidates: tuple[OpportunityCandidate, ...],
    rich_snapshots: tuple[FeatureSnapshot, ...],
    *,
    config: DiscoveryConfig = DEFAULT_DISCOVERY_CONFIG,
) -> tuple[OpportunityCandidate, ...]:
    """Attach candidate-only rich anomaly evidence without consulting strategies."""

    snapshots_by_symbol = {snapshot.symbol: snapshot for snapshot in rich_snapshots}
    enriched: list[OpportunityCandidate] = []
    for candidate in candidates:
        snapshot = snapshots_by_symbol.get(candidate.symbol)
        if snapshot is None:
            raise ValueError(f"missing rich snapshot for candidate {candidate.symbol}")
        if snapshot.decision_at != candidate.decision_at:
            raise ValueError("candidate and rich snapshot decision times differ")
        combined = {item.anomaly_type: item for item in candidate.anomalies}
        for anomaly in detect_anomalies(snapshot, config=config):
            if anomaly.triggered:
                combined[anomaly.anomaly_type] = anomaly
        anomalies = tuple(sorted(combined.values(), key=lambda item: item.anomaly_type.value))
        payload = {
            "symbol": candidate.symbol,
            "decision_at": candidate.decision_at,
            "feature_snapshot_id": snapshot.snapshot_id,
            "anomalies": anomalies,
            "discovery_origin_id": candidate.candidate_id,
        }
        enriched.append(
            OpportunityCandidate(
                candidate_id=stable_identity("candidate", payload),
                symbol=candidate.symbol,
                decision_at=candidate.decision_at,
                feature_snapshot_id=snapshot.snapshot_id,
                anomalies=anomalies,
                discovery_reasons=tuple(
                    dict.fromkeys(
                        (
                            *candidate.discovery_reasons,
                            *(f"{item.anomaly_type.value}:{item.method}" for item in anomalies),
                        )
                    )
                ),
                discovery_rank=candidate.discovery_rank,
            )
        )
    return tuple(enriched)


def _greater(
    snapshot: FeatureSnapshot,
    anomaly_type: AnomalyType,
    feature_name: str,
    threshold: Decimal,
    config: DiscoveryConfig,
) -> AnomalyEvidence:
    return _from_feature(
        snapshot,
        anomaly_type,
        feature_name,
        threshold,
        config,
        triggered=lambda value: value >= threshold,
        strength=lambda value: _ratio_strength(value, threshold),
    )


def _less(
    snapshot: FeatureSnapshot,
    anomaly_type: AnomalyType,
    feature_name: str,
    threshold: Decimal,
    config: DiscoveryConfig,
    *,
    strength_absolute: bool = False,
) -> AnomalyEvidence:
    def measure(value: Decimal) -> Decimal:
        if strength_absolute:
            return _ratio_strength(abs(value), abs(threshold))
        if threshold > 0:
            return _clip01((threshold - value) / threshold)
        return _ratio_strength(abs(value), abs(threshold))

    return _from_feature(
        snapshot,
        anomaly_type,
        feature_name,
        threshold,
        config,
        triggered=lambda value: value <= threshold,
        strength=measure,
    )


def _absolute(
    snapshot: FeatureSnapshot,
    anomaly_type: AnomalyType,
    feature_name: str,
    threshold: Decimal,
    config: DiscoveryConfig,
) -> AnomalyEvidence:
    return _from_feature(
        snapshot,
        anomaly_type,
        feature_name,
        threshold,
        config,
        triggered=lambda value: abs(value) >= threshold,
        strength=lambda value: _ratio_strength(abs(value), threshold),
    )


def _from_feature(
    snapshot: FeatureSnapshot,
    anomaly_type: AnomalyType,
    feature_name: str,
    threshold: Decimal,
    config: DiscoveryConfig,
    *,
    triggered: object,
    strength: object,
) -> AnomalyEvidence:
    feature = snapshot.numeric(feature_name)
    if feature is None:
        return AnomalyEvidence(
            anomaly_type=anomaly_type,
            triggered=False,
            strength=None,
            availability=Availability.INSUFFICIENT_DATA,
            evidence_kind=EvidenceKind.HEURISTIC,
            threshold=threshold,
            threshold_source=config.threshold_version,
            method="feature_not_computed_at_stage",
            sample_size=0,
            feature_names=(feature_name,),
            reasons=("feature_not_computed_at_stage",),
        )
    if feature.availability is not Availability.AVAILABLE or feature.value is None:
        return AnomalyEvidence(
            anomaly_type=anomaly_type,
            triggered=False,
            strength=None,
            availability=feature.availability,
            evidence_kind=EvidenceKind.HEURISTIC,
            threshold=threshold,
            threshold_source=config.threshold_version,
            method=feature.method,
            sample_size=feature.sample_size,
            feature_names=(feature_name,),
            reasons=(feature.reason or "feature_unavailable",),
        )
    trigger_function = triggered
    strength_function = strength
    if not callable(trigger_function) or not callable(strength_function):
        raise TypeError("anomaly predicates must be callable")
    is_triggered = bool(trigger_function(feature.value))
    measured_strength = Decimal(str(strength_function(feature.value)))
    return AnomalyEvidence(
        anomaly_type=anomaly_type,
        triggered=is_triggered,
        strength=_clip01(measured_strength),
        availability=Availability.AVAILABLE,
        evidence_kind=EvidenceKind.HEURISTIC,
        threshold=threshold,
        threshold_source=config.threshold_version,
        method=feature.method,
        sample_size=feature.sample_size,
        feature_names=(feature_name,),
        reasons=() if is_triggered else ("threshold_not_met",),
    )


def _unsupported(
    anomaly_type: AnomalyType,
    reason: str,
    config: DiscoveryConfig,
) -> AnomalyEvidence:
    return AnomalyEvidence(
        anomaly_type=anomaly_type,
        triggered=False,
        strength=None,
        availability=Availability.UNSUPPORTED,
        evidence_kind=EvidenceKind.HEURISTIC,
        threshold=None,
        threshold_source=config.threshold_version,
        method="capability_gate",
        sample_size=0,
        feature_names=(),
        reasons=(reason,),
    )


def _ratio_strength(value: Decimal, threshold: Decimal) -> Decimal:
    if threshold <= 0:
        return Decimal("0")
    return _clip01(value / (threshold * Decimal("2")))


def _clip01(value: Decimal) -> Decimal:
    return max(Decimal("0"), min(Decimal("1"), value))


__all__ = [
    "DEFAULT_DISCOVERY_CONFIG",
    "DiscoveryConfig",
    "detect_anomalies",
    "discover_candidates",
    "enrich_candidates",
]
