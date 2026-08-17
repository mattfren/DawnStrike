from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.opportunity.capabilities import (
    CapabilityState,
    build_provider_capability_receipt,
)
from intraday_scanner.v2.opportunity.discovery import DiscoveryConfig
from intraday_scanner.v2.opportunity.expectancy import build_expectancy_evidence
from intraday_scanner.v2.opportunity.features import FeatureConfig
from intraday_scanner.v2.opportunity.models import (
    AnomalyEvidence,
    AnomalyType,
    Availability,
    DataQuality,
    DecisionTrace,
    EvaluationStatus,
    EvidenceKind,
    FeatureSnapshot,
    FeatureStage,
    MarketRegime,
    NumericFeature,
    OpportunityCandidate,
    RankedOpportunity,
    RegimeState,
    SecurityRegime,
    StrategyDirection,
    StrategyEvaluation,
    StrategyValidationState,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.pipeline import (
    PipelineResult,
    PipelineRiskPolicy,
    PreparedOpportunityPipeline,
    build_pipeline_risk_policy,
    build_strategy_expectancy_binding,
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.quality_gate import (
    QualityGateConfig,
    apply_quality_gate,
    build_decision_run_context,
)
from intraday_scanner.v2.opportunity.ranking import RankingConfig, rank_opportunities
from intraday_scanner.v2.opportunity.registry import (
    StrategyRegistry,
    build_default_registry,
    evaluate_strategy,
    evaluator_behavior_hash,
)
from intraday_scanner.v2.opportunity.risk import (
    ExecutionRiskEvidence,
    QuoteEvidenceScope,
    RiskMetric,
    RiskSafetyCategory,
    RiskValueStatus,
    build_execution_risk_evidence,
    build_risk_numeric_evidence,
    build_risk_safety_evidence,
)
from intraday_scanner.v2.opportunity.universe import (
    SafetyStatus,
    SecurityType,
    UniverseMemberFact,
    UniversePolicy,
    build_universe_snapshot,
)

EASTERN = ZoneInfo("America/New_York")
NOW = datetime(2026, 8, 11, 9, 34, tzinfo=EASTERN)

BASE_VALUES = {
    "close_price": Decimal("100"),
    "atr_prior": Decimal("2"),
    "relative_volume": Decimal("2"),
    "return_short": Decimal("0.02"),
    "range_position": Decimal("0.8"),
    "vwap_proxy_displacement": Decimal("0.02"),
    "vwap_proxy_reclaim": Decimal("1"),
    "minutes_since_open": Decimal("30"),
    "breakout_signal": Decimal("1"),
    "breakdown_signal": Decimal("0"),
    "failed_extension_signal": Decimal("1"),
    "exhaustion_signal": Decimal("1"),
    "failed_breakout_signal": Decimal("1"),
    "failed_breakdown_signal": Decimal("0"),
    "market_relative_strength": Decimal("0.02"),
    "cross_section_liquidity_percentile": Decimal("0.8"),
}

ANOMALIES = (
    AnomalyType.RELATIVE_VOLUME,
    AnomalyType.VWAP_PROXY_RECLAIM,
    AnomalyType.VWAP_PROXY_DISPLACEMENT,
    AnomalyType.BREAKOUT,
    AnomalyType.FAILED_EXTENSION,
    AnomalyType.EXHAUSTION,
    AnomalyType.MARKET_RELATIVE_STRENGTH,
)


def _snapshot(
    *,
    symbol: str = "ABC",
    overrides: dict[str, Decimal] | None = None,
    unavailable: tuple[str, ...] = (),
) -> FeatureSnapshot:
    values = {**BASE_VALUES, **(overrides or {})}
    numerical = tuple(
        NumericFeature(
            name=name,
            value=None if name in unavailable else value,
            availability=(
                Availability.INSUFFICIENT_DATA if name in unavailable else Availability.AVAILABLE
            ),
            method="fixture",
            sample_size=0 if name in unavailable else 20,
            window_id="fixture",
            observed_at=NOW,
            source_kind="OHLCV_BAR",
            reason="fixture_missing" if name in unavailable else None,
        )
        for name, value in values.items()
    )
    return FeatureSnapshot(
        snapshot_id=f"snapshot:{symbol}:{','.join(unavailable)}:{hash(tuple(values.items()))}",
        symbol=symbol,
        decision_at=NOW,
        market_date="2026-08-11",
        universe_id="u",
        dataset_id="d",
        stage=FeatureStage.RICH,
        latest_bar_at=NOW,
        numerical=numerical,
        categorical=(),
        unavailable_features=tuple(sorted(unavailable)),
        data_quality=DataQuality.HIGH,
    )


def _candidate(
    snapshot: FeatureSnapshot, anomaly_types: tuple[AnomalyType, ...] = ANOMALIES
) -> OpportunityCandidate:
    anomalies = tuple(
        AnomalyEvidence(
            anomaly_type=anomaly_type,
            triggered=True,
            strength=Decimal("0.8"),
            availability=Availability.AVAILABLE,
            evidence_kind=EvidenceKind.HEURISTIC,
            threshold=Decimal("0.5"),
            threshold_source="fixture",
            method="fixture",
            sample_size=20,
            feature_names=(anomaly_type.value,),
        )
        for anomaly_type in anomaly_types
    )
    return OpportunityCandidate(
        candidate_id=f"candidate:{snapshot.symbol}",
        symbol=snapshot.symbol,
        decision_at=NOW,
        feature_snapshot_id=snapshot.snapshot_id,
        anomalies=anomalies,
        discovery_reasons=("fixture",),
        discovery_rank=1,
    )


def _regimes(symbol: str = "ABC") -> tuple[MarketRegime, SecurityRegime]:
    market = MarketRegime(
        regime_id="market",
        decision_at=NOW,
        benchmark_symbol="SPY",
        state=RegimeState.UNKNOWN,
        measurements=(),
        confidence=Decimal("0.25"),
        evidence_kind=EvidenceKind.HEURISTIC,
        reasons=("fixture",),
    )
    security = SecurityRegime(
        regime_id=f"security:{symbol}",
        symbol=symbol,
        decision_at=NOW,
        state=RegimeState.UNKNOWN,
        measurements=(),
        confidence=Decimal("0.25"),
        evidence_kind=EvidenceKind.HEURISTIC,
        reasons=("fixture",),
    )
    return market, security


def _evaluate(strategy_id: str, snapshot: FeatureSnapshot | None = None) -> StrategyEvaluation:
    current = snapshot or _snapshot()
    market, security = _regimes(current.symbol)
    return evaluate_strategy(
        build_default_registry().get(strategy_id),
        _candidate(current),
        current,
        market,
        security,
    )


def _apply_gate(
    ranked: RankedOpportunity,
    evaluation: StrategyEvaluation,
    *,
    config: QualityGateConfig | None = None,
):
    active_config = config or QualityGateConfig()
    context = build_decision_run_context(
        (evaluation,),
        (ranked,),
        risk_by_evaluation={},
        config=active_config,
    )
    return apply_quality_gate(
        ranked,
        evaluation,
        decision_context=context,
        config=active_config,
    )


@pytest.mark.parametrize(
    "strategy_id",
    (
        "DS-MOM-001",
        "DS-MOM-002",
        "DS-MOM-003",
        "DS-MR-001",
        "DS-REV-001",
        "DS-REV-002",
        "DS-RS-001",
    ),
)
def test_every_supported_ds_family_has_eligible_case(strategy_id: str) -> None:
    assert _evaluate(strategy_id).status is EvaluationStatus.ELIGIBLE


@pytest.mark.parametrize(
    ("strategy_id", "overrides"),
    (
        ("DS-MOM-001", {"relative_volume": Decimal("1")}),
        ("DS-MOM-002", {"vwap_proxy_reclaim": Decimal("0")}),
        ("DS-MOM-003", {"minutes_since_open": Decimal("120")}),
        ("DS-MR-001", {"vwap_proxy_displacement": Decimal("0.005")}),
        (
            "DS-REV-001",
            {"failed_extension_signal": Decimal("0"), "exhaustion_signal": Decimal("0")},
        ),
        (
            "DS-REV-002",
            {"failed_breakout_signal": Decimal("0"), "failed_breakdown_signal": Decimal("0")},
        ),
        ("DS-RS-001", {"market_relative_strength": Decimal("0.005")}),
    ),
)
def test_every_supported_ds_family_has_rejected_case(
    strategy_id: str,
    overrides: dict[str, Decimal],
) -> None:
    assert (
        _evaluate(strategy_id, _snapshot(overrides=overrides)).status is EvaluationStatus.REJECTED
    )


@pytest.mark.parametrize(
    "strategy_id",
    (
        "DS-MOM-001",
        "DS-MOM-002",
        "DS-MOM-003",
        "DS-MR-001",
        "DS-REV-001",
        "DS-REV-002",
        "DS-RS-001",
    ),
)
def test_every_supported_ds_family_has_insufficient_case(strategy_id: str) -> None:
    definition = build_default_registry().get(strategy_id)
    snapshot = _snapshot(unavailable=(definition.required_features[0],))
    market, security = _regimes()
    result = evaluate_strategy(definition, _candidate(snapshot), snapshot, market, security)
    assert result.status is EvaluationStatus.INSUFFICIENT_DATA
    assert result.reasons[0].startswith("missing_required_feature:")


def test_order_flow_strategies_are_disabled_without_aggressor_evidence() -> None:
    registry = build_default_registry()
    for strategy_id in ("DS-OF-001", "DS-OF-002"):
        definition = registry.get(strategy_id)
        assert definition.lifecycle is StrategyValidationState.DISABLED
        assert "aggressor_side_trade_evidence" in (definition.disabled_reason or "")
        assert _evaluate(strategy_id).status is EvaluationStatus.DISABLED


def test_strategy_threshold_metadata_and_definition_hash_bind_evaluation_rules() -> None:
    registry = build_default_registry()
    required = {
        "DS-MOM-001": {
            "minimum_relative_volume",
            "minimum_return_short",
            "minimum_range_position",
            "reward_multiple",
        },
        "DS-MOM-002": {"minimum_vwap_reclaim", "minimum_return_short", "reward_multiple"},
        "DS-MOM-003": {
            "signal_threshold",
            "minimum_minutes_since_open",
            "maximum_minutes_since_open",
            "minimum_relative_volume",
            "reward_multiple",
        },
        "DS-MR-001": {"minimum_absolute_vwap_displacement", "reward_multiple"},
        "DS-REV-001": {"signal_threshold", "range_midpoint", "reward_multiple"},
        "DS-REV-002": {"signal_threshold", "reward_multiple"},
        "DS-RS-001": {
            "minimum_market_relative_strength",
            "minimum_return_short",
            "reward_multiple",
        },
    }
    for strategy_id, names in required.items():
        definition = registry.get(strategy_id)
        assert names == {item.name for item in definition.parameters}
        assert len(definition.evaluator_code_hash) == 64
        assert definition.evaluator_code_hash == evaluator_behavior_hash(definition.evaluator_id)

    with pytest.raises(ValueError, match="code hash"):
        StrategyRegistry((replace(registry.get("DS-MOM-001"), evaluator_code_hash="0" * 64),))

    original = registry.get("DS-MOM-001")
    modified = replace(
        original,
        parameters=tuple(
            replace(item, value=Decimal("3")) if item.name == "minimum_relative_volume" else item
            for item in original.parameters
        ),
    )
    snapshot = _snapshot()
    candidate = _candidate(snapshot)
    market, security = _regimes()
    original_result = evaluate_strategy(original, candidate, snapshot, market, security)
    modified_result = evaluate_strategy(modified, candidate, snapshot, market, security)
    assert original_result.status is EvaluationStatus.ELIGIBLE
    assert modified_result.status is EvaluationStatus.REJECTED
    assert original_result.strategy_definition_hash != modified_result.strategy_definition_hash
    assert original_result.evaluation_id != modified_result.evaluation_id
    assert original_result.evaluator_id == modified_result.evaluator_id


def test_pair_evaluation_retains_every_eligible_rejected_insufficient_and_disabled_pair() -> None:
    snapshot = _snapshot(
        overrides={"relative_volume": Decimal("1")},
        unavailable=("vwap_proxy_reclaim",),
    )
    candidate = _candidate(snapshot)
    market, security = _regimes()
    results = build_default_registry().evaluate_all(candidate, snapshot, market, security)
    assert len(results) == 9
    assert {(item.strategy_id, item.status) for item in results}
    assert sum(item.status is EvaluationStatus.DISABLED for item in results) == 2
    assert any(item.status is EvaluationStatus.INSUFFICIENT_DATA for item in results)
    assert any(item.status is EvaluationStatus.REJECTED for item in results)
    assert any(item.status is EvaluationStatus.ELIGIBLE for item in results)


def test_candidate_feature_identity_mismatch_fails_closed() -> None:
    snapshot = _snapshot()
    candidate = replace(_candidate(snapshot), feature_snapshot_id="different")
    market, security = _regimes()
    with pytest.raises(ValueError, match="snapshot identity"):
        evaluate_strategy(
            build_default_registry().get("DS-MOM-001"),
            candidate,
            snapshot,
            market,
            security,
        )


def test_expectancy_formula_preserves_zero_r_as_breakeven() -> None:
    evidence = build_expectancy_evidence(
        (Decimal("1"), Decimal("-1"), Decimal("0")),
        cohort_id="cohort",
        min_sample_size=3,
    )
    assert evidence.win_probability == Decimal("1") / Decimal("3")
    assert evidence.expectancy_r == Decimal("0")


def test_ranking_is_pair_based_stable_and_does_not_emit_decisions() -> None:
    first = _evaluate("DS-MOM-001")
    second = replace(first, evaluation_id="evaluation:xyz", symbol="XYZ")
    config = RankingConfig(
        direction_repeat_penalty=Decimal("0"),
        strategy_family_repeat_penalty=Decimal("0"),
        sector_repeat_penalty=Decimal("0"),
        correlation_repeat_penalty=Decimal("0"),
    )
    ranked = rank_opportunities((second, first), config=config)
    assert [item.symbol for item in ranked] == ["ABC", "XYZ"]
    assert [item.relative_rank for item in ranked] == [1, 2]
    assert ranked[0].components
    assert not hasattr(ranked[0], "decision")
    assert "empirical_expectancy_unavailable_not_zero" in ranked[0].limitations


def test_ranking_applies_nonzero_concentration_penalties() -> None:
    first = _evaluate("DS-MOM-001")
    second = replace(first, evaluation_id="evaluation:xyz", symbol="XYZ")
    ranked = rank_opportunities(
        (second, first),
        sector_by_symbol={"ABC": "semiconductors", "XYZ": "semiconductors"},
        correlation_cluster_by_symbol={"ABC": "cluster-1", "XYZ": "cluster-1"},
    )
    assert ranked[0].concentration_penalty == 0
    assert ranked[1].concentration_penalty > 0
    assert "sector:semiconductors" in ranked[1].concentration_labels
    assert "correlation_cluster:cluster-1" in ranked[1].concentration_labels


def test_absolute_gate_requires_risk_even_for_watch_and_rank_one_can_pass() -> None:
    evaluation = _evaluate("DS-MOM-001")
    ranked = rank_opportunities((evaluation,))[0]
    watched = _apply_gate(ranked, evaluation)
    assert watched.decision is TradeDecisionValue.INSUFFICIENT_DATA
    assert "execution_risk_evidence_unavailable" in watched.limitations
    assert "production_lifecycle" in watched.vetoes
    passed = _apply_gate(
        ranked,
        evaluation,
        config=QualityGateConfig(
            minimum_watch_score=Decimal("0.99"), minimum_take_score=Decimal("1")
        ),
    )
    assert ranked.relative_rank == 1
    assert passed.decision is TradeDecisionValue.PASS


def test_gate_missing_evidence_and_invalid_directional_geometry_fail_closed() -> None:
    evaluation = _evaluate("DS-MOM-001")
    missing = replace(evaluation, liquidity_score=None)
    missing_ranked = rank_opportunities((missing,))[0]
    assert (
        _apply_gate(missing_ranked, missing).decision
        is TradeDecisionValue.INSUFFICIENT_DATA
    )

    bad_long = replace(evaluation, invalidation_price=Decimal("101"))
    bad_long_ranked = rank_opportunities((bad_long,))[0]
    assert (
        _apply_gate(bad_long_ranked, bad_long).decision
        is TradeDecisionValue.INSUFFICIENT_DATA
    )

    short = replace(
        evaluation,
        evaluation_id="short",
        direction=StrategyDirection.SHORT,
        invalidation_price=Decimal("102"),
        target_price=Decimal("101"),
    )
    short_ranked = rank_opportunities((short,))[0]
    assert _apply_gate(short_ranked, short).decision is TradeDecisionValue.INSUFFICIENT_DATA
    with pytest.raises(ValueError, match="pair metadata"):
        _apply_gate(replace(short_ranked, symbol="WRONG"), short)


def _bar(symbol: str, minute: int, volume: int) -> MarketBar:
    timestamp = datetime(2026, 8, 11, 9, minute, tzinfo=EASTERN)
    return MarketBar(
        symbol=symbol,
        timestamp=timestamp,
        open=100,
        high=101,
        low=99,
        close=100,
        volume=volume,
        exchange_session_id="2026-08-11",
    )


def _pipeline_dataset() -> MarketDataset:
    volumes = {
        "ABC": (100, 100, 100, 100, 1000),
        "DEF": (100, 100, 100, 100, 100),
        "GHI": (200, 200, 200, 200, 200),
        "SPY": (1000, 1000, 1000, 1000, 1000),
    }
    return MarketDataset(
        dataset_id="pipeline-fixture",
        source_kind="fixture",
        timeframe="1m",
        bars_by_symbol={
            symbol: tuple(_bar(symbol, 30 + index, volume) for index, volume in enumerate(values))
            for symbol, values in volumes.items()
        },
    )


def _pipeline_universe(
    dataset: MarketDataset,
    *,
    requested_symbols: tuple[str, ...] = ("ABC", "DEF", "GHI"),
    benchmark_available: bool = True,
):
    receipt = build_provider_capability_receipt(
        provider="bounded-pipeline-fixture",
        feed="fixture-bars",
        entitlement_identity="fixture-read-only",
        decision_at=NOW,
        observed_at=NOW,
        bars=CapabilityState.AVAILABLE,
        trades=CapabilityState.UNSUPPORTED,
        quotes=CapabilityState.UNSUPPORTED,
        consolidated_nbbo=CapabilityState.UNSUPPORTED,
        aggressor_classification=CapabilityState.UNSUPPORTED,
        corporate_actions=CapabilityState.AVAILABLE,
        halts=CapabilityState.AVAILABLE,
        historical_coverage=CapabilityState.AVAILABLE,
        coverage_start=NOW - timedelta(days=1),
        coverage_end=NOW,
        source_identity="bounded-pipeline-capability",
        method="fixture capability declaration",
        limitations=("bounded fixture capability",),
    )
    fact_symbols = (*requested_symbols, "SPY")
    facts = tuple(
        UniverseMemberFact(
            symbol=symbol,
            security_type=SecurityType.COMMON_STOCK,
            venue="XNYS",
            first_seen_at=NOW - timedelta(days=30),
            observed_at=NOW,
            data_availability=(
                CapabilityState.UNKNOWN
                if symbol == "SPY" and not benchmark_available
                else CapabilityState.AVAILABLE
            ),
            halt_status=(
                SafetyStatus.UNKNOWN
                if symbol == "SPY" and not benchmark_available
                else SafetyStatus.CLEAR
            ),
            corporate_action_status=(
                SafetyStatus.UNKNOWN
                if symbol == "SPY" and not benchmark_available
                else SafetyStatus.CLEAR
            ),
            observed_price=Decimal("10"),
            average_daily_dollar_volume=Decimal("2000000"),
            provider_receipt_ids=(receipt.capability_receipt_id,),
        )
        for symbol in fact_symbols
    )
    return build_universe_snapshot(
        dataset,
        decision_at=NOW,
        as_of=NOW,
        policy=UniversePolicy(
            policy_id="bounded-pipeline-policy",
            version="1.0.0",
            minimum_price=Decimal("1"),
            minimum_average_daily_dollar_volume=Decimal("1000000"),
        ),
        member_facts=facts,
        capability_receipts=(receipt,),
        requested_symbols=requested_symbols,
        benchmark_symbol="SPY",
        source_identity="bounded-pipeline-universe",
    )


def _pipeline_risk_policy() -> PipelineRiskPolicy:
    return build_pipeline_risk_policy(
        policy_version="bounded-pipeline-risk-v1",
        account_identity="bounded-paper-account",
        risk_cap_identity="bounded-fixed-fractional-policy",
        concentration_identity="bounded-aggregate-book",
        minimum_after_cost_reward_risk=Decimal("1.5"),
    )


def _two_candidate_dataset() -> MarketDataset:
    dataset = _pipeline_dataset()
    closes = (100.0, 100.1, 100.2, 100.3, 100.4, 100.5, 101.0, 104.0)
    bars = {
        symbol: tuple(
            MarketBar(
                symbol=symbol,
                timestamp=datetime(2026, 8, 11, 9, 27 + index, tzinfo=EASTERN),
                open=close - 0.1,
                high=close + 0.1,
                low=close - 0.5,
                close=close,
                volume=volume,
                exchange_session_id="2026-08-11",
            )
            for index, (close, volume) in enumerate(zip(closes, volumes, strict=True))
        )
        for symbol, volumes in {
            "ABC": (1000, 1000, 1000, 1000, 1000, 1000, 1000, 10000),
            "DEF": (1000, 1000, 1000, 1000, 1000, 1000, 1000, 10000),
            "GHI": (1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000),
            "SPY": (1000, 1000, 1000, 1000, 1000, 1000, 1000, 1000),
        }.items()
    }
    return replace(dataset, bars_by_symbol=bars)


def _execution_risk_for(
    evaluation: StrategyEvaluation,
    *,
    spread_bps: Decimal = Decimal("5"),
    minimum_after_cost_reward_risk: Decimal | None = Decimal("1.5"),
) -> ExecutionRiskEvidence:
    capability = build_provider_capability_receipt(
        provider="bounded-risk-fixture",
        feed="synthetic-sip",
        entitlement_identity="fixture-read-only",
        decision_at=NOW,
        observed_at=NOW,
        bars=CapabilityState.AVAILABLE,
        trades=CapabilityState.AVAILABLE,
        quotes=CapabilityState.AVAILABLE,
        consolidated_nbbo=CapabilityState.AVAILABLE,
        aggressor_classification=CapabilityState.UNSUPPORTED,
        corporate_actions=CapabilityState.AVAILABLE,
        halts=CapabilityState.AVAILABLE,
        historical_coverage=CapabilityState.AVAILABLE,
        coverage_start=NOW - timedelta(days=1),
        coverage_end=NOW,
        source_identity="bounded-risk-capability",
        method="fixture capability declaration",
        limitations=("bounded fixture capability",),
    )
    halt = build_risk_safety_evidence(
        category=RiskSafetyCategory.HALT,
        symbol=evaluation.symbol,
        status=SafetyStatus.CLEAR,
        observed_at=NOW,
        source_identity="bounded-safety-fixture",
        method="fixture safety observation",
        capability_receipt_id=capability.capability_receipt_id,
    )
    action = build_risk_safety_evidence(
        category=RiskSafetyCategory.CORPORATE_ACTION,
        symbol=evaluation.symbol,
        status=SafetyStatus.CLEAR,
        observed_at=NOW,
        source_identity="bounded-safety-fixture",
        method="fixture safety observation",
        capability_receipt_id=capability.capability_receipt_id,
    )
    values = {
        RiskMetric.ENTRY_PRICE: evaluation.entry_price,
        RiskMetric.STOP_PRICE: evaluation.invalidation_price,
        RiskMetric.TARGET_PRICE: evaluation.target_price,
        RiskMetric.SPREAD_BPS: spread_bps,
        RiskMetric.ENTRY_SLIPPAGE_BPS: Decimal("2"),
        RiskMetric.EXIT_SLIPPAGE_BPS: Decimal("2"),
        RiskMetric.ROUND_TRIP_FEE_PER_SHARE: Decimal("0.01"),
        RiskMetric.QUANTITY: Decimal("100"),
        RiskMetric.ACCOUNT_EQUITY: Decimal("100000"),
        RiskMetric.RISK_FRACTION: Decimal("0.01"),
        RiskMetric.AGGREGATE_CONCENTRATION: Decimal("0.10"),
        RiskMetric.MAX_AGGREGATE_CONCENTRATION: Decimal("0.25"),
        RiskMetric.MAX_QUOTE_AGE: Decimal("5"),
        RiskMetric.MIN_AFTER_COST_REWARD_RISK: minimum_after_cost_reward_risk,
    }
    base_metrics = tuple(
        build_risk_numeric_evidence(
            metric=metric,
            value=value,
            status=(
                RiskValueStatus.UNAVAILABLE
                if value is None
                else RiskValueStatus.OBSERVED
            ),
            capability_state=(
                CapabilityState.UNAVAILABLE
                if value is None
                else CapabilityState.AVAILABLE
            ),
            evidence_kind=EvidenceKind.EMPIRICAL,
            observed_at=(
                NOW - timedelta(seconds=1)
                if metric is RiskMetric.SPREAD_BPS
                else NOW
            ),
            source_identity={
                RiskMetric.ACCOUNT_EQUITY: "bounded-paper-account",
                RiskMetric.RISK_FRACTION: "bounded-fixed-fractional-policy",
                RiskMetric.AGGREGATE_CONCENTRATION: "bounded-aggregate-book",
                RiskMetric.MAX_AGGREGATE_CONCENTRATION: "bounded-aggregate-book",
            }.get(metric, "bounded-risk-fixture"),
            method="fixture point-in-time observation",
            reason=("fixture value unavailable" if value is None else None),
        )
        for metric, value in values.items()
    )
    return build_execution_risk_evidence(
        evaluation=evaluation,
        capability_receipts=(capability,),
        quote_capability_receipt_id=capability.capability_receipt_id,
        quote_scope=QuoteEvidenceScope.NBBO,
        halt_evidence=halt,
        corporate_action_evidence=action,
        account_identity="bounded-paper-account",
        risk_cap_identity="bounded-fixed-fractional-policy",
        concentration_identity="bounded-aggregate-book",
        base_metrics=base_metrics,
        limitations=("bounded synthetic execution-risk fixture",),
    )


def test_prepare_pipeline_uses_authoritative_universe_and_round_trips() -> None:
    dataset = _pipeline_dataset()
    universe = _pipeline_universe(dataset)
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=universe,
        registry=StrategyRegistry(()),
    )

    assert tuple(item.symbol for item in prepared.cheap_snapshots) == universe.eligible_symbols
    assert tuple(item.symbol for item in prepared.candidates) == ("ABC",)
    assert prepared.benchmark_snapshot is not None
    assert prepared.benchmark_snapshot.symbol == "SPY"
    assert "SPY" not in {item.symbol for item in prepared.candidates}
    assert prepared.evaluations == ()
    assert prepared.ranked_opportunities == ()
    assert PreparedOpportunityPipeline.from_json(prepared.to_json()) == prepared


def test_prepare_pipeline_rejects_dataset_config_and_expectancy_identity_tamper() -> None:
    dataset = _pipeline_dataset()
    universe = _pipeline_universe(dataset)
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=universe,
        registry=StrategyRegistry(()),
    )
    with pytest.raises(ValueError, match="dataset_id"):
        prepare_opportunity_pipeline(
            replace(dataset, dataset_id="other-dataset"),
            universe_snapshot=universe,
            registry=StrategyRegistry(()),
        )
    changed_bars = dict(dataset.bars_by_symbol)
    changed_bars["ABC"] = (
        *changed_bars["ABC"][:-1],
        replace(changed_bars["ABC"][-1], close=101.0),
    )
    with pytest.raises(ValueError, match="dataset content"):
        prepare_opportunity_pipeline(
            replace(dataset, bars_by_symbol=changed_bars),
            universe_snapshot=universe,
            registry=StrategyRegistry(()),
        )
    with pytest.raises(ValueError, match="feature.*identity|prepared config"):
        PreparedOpportunityPipeline.from_dict(
            {**prepared.to_dict(), "feature_config_identity": "feature-config:tampered"}
        )

    definition = build_default_registry().definitions[0]
    expectancy = build_expectancy_evidence(
        (Decimal("1"), Decimal("-1")) * 60,
        cohort_id="bounded-pipeline-expectancy",
        min_sample_size=100,
    )
    binding = build_strategy_expectancy_binding(
        decision_at=NOW,
        strategy_definition=definition,
        evidence=expectancy,
        observed_at=NOW,
        source_identity="bounded-expectancy-fixture",
        method="fixture cohort calculation",
    )
    with pytest.raises(ValueError, match="unknown strategy version"):
        prepare_opportunity_pipeline(
            dataset,
            universe_snapshot=universe,
            registry=StrategyRegistry(()),
            expectancy_bindings=(binding,),
        )
    with pytest.raises(ValueError, match="observed after"):
        build_strategy_expectancy_binding(
            decision_at=NOW,
            strategy_definition=definition,
            evidence=expectancy,
            observed_at=NOW + timedelta(microseconds=1),
            source_identity="bounded-expectancy-fixture",
            method="fixture cohort calculation",
        )
    with pytest.raises(ValueError, match="version ambiguity"):
        prepare_opportunity_pipeline(
            dataset,
            universe_snapshot=universe,
            registry=StrategyRegistry(
                (definition, replace(definition, version="2.0.0"))
            ),
        )


def test_prepare_pipeline_preserves_explicit_empty_universe() -> None:
    dataset = _pipeline_dataset()
    universe = _pipeline_universe(dataset, requested_symbols=())
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=universe,
        registry=StrategyRegistry(()),
    )

    assert prepared.cheap_snapshots == ()
    assert prepared.candidates == ()
    assert prepared.evaluations == ()
    assert prepared.ranked_opportunities == ()
    assert prepared.benchmark_snapshot is not None
    assert prepared.benchmark_snapshot.symbol == "SPY"


def _two_symbol_two_strategy_preparation() -> PreparedOpportunityPipeline:
    dataset = _two_candidate_dataset()
    default = build_default_registry()
    momentum = default.get("DS-MOM-001")
    disabled = default.get("DS-OF-001")
    expectancy = build_expectancy_evidence(
        (Decimal("1"),) * 120 + (Decimal("-1"),) * 80,
        cohort_id="bounded-two-symbol-momentum",
        min_sample_size=100,
    )
    binding = build_strategy_expectancy_binding(
        decision_at=NOW,
        strategy_definition=momentum,
        evidence=expectancy,
        observed_at=NOW,
        source_identity="bounded-expectancy-fixture",
        method="fixture cohort calculation",
    )
    return prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(
            dataset,
            requested_symbols=("ABC", "DEF"),
        ),
        registry=StrategyRegistry((momentum, disabled)),
        expectancy_bindings=(binding,),
        sector_by_symbol={"ABC": "technology", "DEF": "industrials"},
        correlation_cluster_by_symbol={"ABC": "cluster-a", "DEF": "cluster-b"},
    )


def test_two_symbol_two_strategy_pipeline_reconciles_every_pair_and_round_trips() -> None:
    prepared = _two_symbol_two_strategy_preparation()
    assert [item.symbol for item in prepared.candidates] == ["ABC", "DEF"]
    assert len(prepared.evaluations) == 4
    assert [item.status for item in prepared.evaluations].count(
        EvaluationStatus.ELIGIBLE
    ) == 2
    assert [item.status for item in prepared.evaluations].count(
        EvaluationStatus.DISABLED
    ) == 2
    eligible = tuple(
        item for item in prepared.evaluations if item.status is EvaluationStatus.ELIGIBLE
    )
    risks = {item.evaluation_id: _execution_risk_for(item) for item in eligible}

    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risks,
        risk_policy=_pipeline_risk_policy(),
    )

    assert tuple(item.evaluation_id for item in result.decisions) == tuple(
        item.evaluation_id for item in prepared.evaluations
    )
    assert tuple(item.evaluation_id for item in result.traces) == tuple(
        item.evaluation_id for item in prepared.evaluations
    )
    assert len(result.ranked_opportunities) == len(eligible)
    assert result.decision_context is not None
    assert all(
        item.decision_run_id == result.decision_context.decision_run_id
        for item in result.decisions
    )
    expected_rank_inputs = tuple(item.evaluation_id for item in eligible)
    expected_rank_outputs = tuple(
        item.ranked_id for item in prepared.ranked_opportunities
    )
    assert all(item.global_rank_input_ids == expected_rank_inputs for item in result.traces)
    assert all(item.global_ranked_ids == expected_rank_outputs for item in result.traces)
    assert all(item.stages[0].output_count == 2 for item in result.traces)
    nonrankable = tuple(item for item in result.traces if item.ranked_id is None)
    assert len(nonrankable) == 2
    assert all(item.risk_evidence_id is None for item in nonrankable)
    assert all(item.final_decision is TradeDecisionValue.PASS for item in nonrankable)
    assert PipelineResult.from_json(result.to_json()) == result
    second = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risks,
        risk_policy=_pipeline_risk_policy(),
    )
    assert second.to_json() == result.to_json()
    assert second.run_id == result.run_id


def _finalized_two_strategy_pipeline(
    *,
    spread_bps: Decimal = Decimal("5"),
) -> tuple[
    PreparedOpportunityPipeline,
    dict[str, ExecutionRiskEvidence],
    PipelineResult,
]:
    prepared = _two_symbol_two_strategy_preparation()
    risks = {
        item.evaluation_id: _execution_risk_for(item, spread_bps=spread_bps)
        for item in prepared.evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    }
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risks,
        risk_policy=_pipeline_risk_policy(),
    )
    return prepared, risks, result


def test_pipeline_finalizer_rejects_missing_extra_malformed_and_policy_mismatched_risk() -> (
    None
):
    prepared, risks, _result = _finalized_two_strategy_pipeline()
    first_key = next(iter(risks))
    missing = dict(risks)
    missing.pop(first_key)
    with pytest.raises(ValueError, match="exactly follow eligible evaluation order"):
        run_opportunity_pipeline(
            prepared,
            risk_by_evaluation=missing,
            risk_policy=_pipeline_risk_policy(),
        )
    with pytest.raises(ValueError, match="unknown, missing, or noneligible"):
        run_opportunity_pipeline(
            prepared,
            risk_by_evaluation={**risks, "unknown-evaluation": risks[first_key]},
            risk_policy=_pipeline_risk_policy(),
        )
    with pytest.raises(TypeError, match="ExecutionRiskEvidence"):
        run_opportunity_pipeline(
            prepared,
            risk_by_evaluation={first_key: object()},  # type: ignore[dict-item]
            risk_policy=_pipeline_risk_policy(),
        )
    wrong_account_policy = build_pipeline_risk_policy(
        policy_version="bounded-pipeline-risk-v1",
        account_identity="different-paper-account",
        risk_cap_identity="bounded-fixed-fractional-policy",
        concentration_identity="bounded-aggregate-book",
        minimum_after_cost_reward_risk=Decimal("1.5"),
    )
    with pytest.raises(ValueError, match="identities do not match"):
        run_opportunity_pipeline(
            prepared,
            risk_by_evaluation=risks,
            risk_policy=wrong_account_policy,
        )
    with pytest.raises(ValueError, match="gate minimum"):
        run_opportunity_pipeline(
            prepared,
            risk_by_evaluation=risks,
            risk_policy=_pipeline_risk_policy(),
            gate_config=QualityGateConfig(
                minimum_after_cost_reward_risk=Decimal("2")
            ),
        )
    threshold_policy = build_pipeline_risk_policy(
        policy_version="bounded-pipeline-risk-v2",
        account_identity="bounded-paper-account",
        risk_cap_identity="bounded-fixed-fractional-policy",
        concentration_identity="bounded-aggregate-book",
        minimum_after_cost_reward_risk=Decimal("2"),
    )
    with pytest.raises(ValueError, match="execution risk minimum"):
        run_opportunity_pipeline(
            prepared,
            risk_by_evaluation=risks,
            risk_policy=threshold_policy,
            gate_config=QualityGateConfig(
                minimum_after_cost_reward_risk=Decimal("2")
            ),
        )


def test_unavailable_risk_policy_threshold_reconciles_to_one_decision_per_pair() -> None:
    prepared = _two_symbol_two_strategy_preparation()
    risks = {
        item.evaluation_id: _execution_risk_for(
            item,
            minimum_after_cost_reward_risk=None,
        )
        for item in prepared.evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    }

    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risks,
        risk_policy=_pipeline_risk_policy(),
    )

    assert len(result.decisions) == len(prepared.evaluations)
    assert len(result.traces) == len(prepared.evaluations)
    eligible_decisions = tuple(
        item
        for item in result.decisions
        if item.evaluation.status is EvaluationStatus.ELIGIBLE
    )
    assert len(eligible_decisions) == 2
    assert all(
        item.decision is TradeDecisionValue.INSUFFICIENT_DATA
        for item in eligible_decisions
    )
    for decision in eligible_decisions:
        checks = {item.check_id: item for item in decision.gate_checks}
        assert checks["risk_policy_minimum_available"].passed is None
        assert checks["execution_risk_empirical"].passed is False
        assert "minimum_after_cost_r_unavailable" in decision.vetoes
    assert PipelineResult.from_json(result.to_json()) == result


def test_excluded_unknown_benchmark_is_retained_but_never_used_for_regime() -> None:
    dataset = _two_candidate_dataset()
    universe = _pipeline_universe(
        dataset,
        requested_symbols=("ABC", "DEF"),
        benchmark_available=False,
    )
    assert universe.benchmark_member is not None
    assert universe.benchmark_member.membership_status.value == "excluded"
    assert universe.benchmark_member.as_of <= universe.decision_at
    default = build_default_registry()
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=universe,
        registry=StrategyRegistry(
            (default.get("DS-MOM-001"), default.get("DS-OF-001"))
        ),
    )

    assert prepared.benchmark_snapshot is None
    assert prepared.market_regime.state is RegimeState.INSUFFICIENT_DATA
    assert "benchmark_membership_excluded" in prepared.market_regime.reasons
    assert "benchmark_data_unknown" in prepared.market_regime.reasons
    assert all(item.symbol != "SPY" for item in prepared.candidates)
    for snapshot in prepared.rich_snapshots:
        market_relative = snapshot.numeric("market_relative_strength")
        assert market_relative is not None
        assert market_relative.availability is Availability.INSUFFICIENT_DATA
        assert market_relative.value is None
        assert market_relative.reason is not None
        assert "benchmark" in market_relative.reason
    assert all(
        snapshot.numeric("market_relative_strength") is None
        for snapshot in prepared.cheap_snapshots
    )
    assert all(
        anomaly.anomaly_type
        not in {
            AnomalyType.MARKET_RELATIVE_STRENGTH,
            AnomalyType.MARKET_RELATIVE_WEAKNESS,
        }
        for candidate in prepared.candidates
        for anomaly in candidate.anomalies
    )
    assert not any(
        item.status is EvaluationStatus.ELIGIBLE for item in prepared.evaluations
    )
    assert prepared.ranked_opportunities == ()
    assert any(
        item.startswith("benchmark_unavailable:benchmark_membership_excluded")
        for item in prepared.limitations
    )
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    assert len(result.decisions) == len(prepared.evaluations)
    assert len(result.traces) == len(prepared.evaluations)
    assert result.preparation.universe_snapshot.benchmark_member == universe.benchmark_member
    assert PreparedOpportunityPipeline.from_json(prepared.to_json()) == prepared
    assert PipelineResult.from_json(result.to_json()) == result


@pytest.mark.parametrize(
    "mutation",
    ("omit", "inject", "cross_pair", "rank_content", "evaluation_content"),
)
def test_pair_trace_direct_contract_rejects_canonical_stage_and_pair_tamper(
    mutation: str,
) -> None:
    _prepared, _risks, result = _finalized_two_strategy_pipeline()
    payload = result.traces[0].to_dict()
    if mutation == "omit":
        payload["stages"] = payload["stages"][:-1]
    elif mutation == "inject":
        payload["stages"] = [*payload["stages"], payload["stages"][-1]]
    elif mutation == "cross_pair":
        payload["final_trade_decision"] = result.traces[-1].final_trade_decision.to_dict()
    elif mutation == "rank_content":
        assert result.traces[0].ranked is not None
        ranked_payload = result.traces[0].ranked.to_dict()
        ranked_payload["concentration_penalty"] = "0.01"
        ranked_payload["final_score"] = str(
            result.traces[0].ranked.base_score - Decimal("0.01")
        )
        rebound_rank = RankedOpportunity.from_dict(ranked_payload)
        payload["ranked"] = rebound_rank.to_dict()
        payload["ranked_content_hash"] = rebound_rank.content_hash()
    else:
        evaluation_payload = result.traces[0].evaluation.to_dict()
        evaluation_payload["reasons"] = [
            *evaluation_payload["reasons"],
            "consistent-rehash-tamper",
        ]
        rebound_evaluation = StrategyEvaluation.from_dict(evaluation_payload)
        payload["evaluation"] = rebound_evaluation.to_dict()
        payload["evaluation_content_hash"] = rebound_evaluation.content_hash()
    payload["trace_id"] = stable_identity(
        "decision-trace",
        {key: value for key, value in payload.items() if key != "trace_id"},
    )
    with pytest.raises(ValueError):
        DecisionTrace.from_dict(payload)


@pytest.mark.parametrize(
    "field",
    ("dataset_id", "universe_snapshot_id", "decisions", "traces", "run_id"),
)
def test_pipeline_result_direct_contract_rejects_identity_and_set_tamper(
    field: str,
) -> None:
    _prepared, _risks, result = _finalized_two_strategy_pipeline()
    payload = result.to_dict()
    if field in {"dataset_id", "universe_snapshot_id", "run_id"}:
        payload[field] = f"{payload[field]}-tampered"
    else:
        payload[field] = payload[field][:-1]
    with pytest.raises(ValueError):
        PipelineResult.from_dict(payload)


def test_different_valid_risk_receipts_change_decision_context_trace_and_run_ids() -> None:
    _prepared, _risks, first = _finalized_two_strategy_pipeline(
        spread_bps=Decimal("5")
    )
    _prepared, _risks, second = _finalized_two_strategy_pipeline(
        spread_bps=Decimal("6")
    )

    assert first.decision_context is not None
    assert second.decision_context is not None
    assert first.decision_context.decision_run_id != second.decision_context.decision_run_id
    assert tuple(item.decision_id for item in first.decisions) != tuple(
        item.decision_id for item in second.decisions
    )
    assert tuple(item.trace_id for item in first.traces) != tuple(
        item.trace_id for item in second.traces
    )
    assert first.run_id != second.run_id


def test_discovery_precedes_strategy_when_registry_is_empty_and_rich_work_is_candidate_only() -> (
    None
):
    dataset = _pipeline_dataset()
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(dataset),
        registry=StrategyRegistry(()),
    )
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    assert [candidate.symbol for candidate in result.candidates] == ["ABC"]
    assert [snapshot.symbol for snapshot in result.rich_snapshots] == ["ABC"]
    assert result.evaluations == ()
    assert result.ranked_opportunities == ()
    assert result.decisions == ()
    assert result.benchmark_snapshot is not None
    assert result.traces == ()


def test_pipeline_is_byte_deterministic_and_explicit_empty_universe_stays_empty() -> None:
    dataset = _pipeline_dataset()
    universe = _pipeline_universe(dataset)
    first_prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=universe,
        registry=StrategyRegistry(()),
    )
    second_prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=universe,
        registry=StrategyRegistry(()),
    )
    first = run_opportunity_pipeline(
        first_prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    second = run_opportunity_pipeline(
        second_prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    assert first.to_json() == second.to_json()
    assert first.run_id == second.run_id
    assert tuple(trace.trace_id for trace in first.traces) == tuple(
        trace.trace_id for trace in second.traces
    )

    empty_prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(dataset, requested_symbols=()),
        registry=StrategyRegistry(()),
    )
    empty = run_opportunity_pipeline(
        empty_prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    assert empty.cheap_snapshots == ()
    assert empty.rich_snapshots == ()
    assert empty.benchmark_snapshot is not None
    assert empty.candidates == ()
    assert empty.traces == ()
    assert PipelineResult.from_json(empty.to_json()) == empty


def test_invalid_discovery_threshold_configuration_fails_closed() -> None:
    with pytest.raises(ValueError, match="finite"):
        DiscoveryConfig(relative_volume=Decimal("NaN"))
    with pytest.raises(ValueError, match="positive"):
        DiscoveryConfig(relative_volume=Decimal("0"))
    assert DiscoveryConfig(low_liquidity_percentile=Decimal("0"))


@pytest.mark.parametrize(
    "kwargs",
    (
        {"volume_window": 1},
        {"volatility_short_window": 1},
        {"volatility_long_window": 1},
        {"min_cross_section_size": 1},
        {"config_version": " "},
    ),
)
def test_invalid_feature_window_configuration_fails_early(kwargs: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        FeatureConfig(**kwargs)  # type: ignore[arg-type]


def test_pipeline_rejects_malformed_expectancy_binding() -> None:
    dataset = _pipeline_dataset()
    with pytest.raises(TypeError, match="StrategyExpectancyBinding"):
        prepare_opportunity_pipeline(
            dataset,
            universe_snapshot=_pipeline_universe(dataset),
            registry=StrategyRegistry(()),
            expectancy_bindings=(object(),),  # type: ignore[arg-type]
        )
