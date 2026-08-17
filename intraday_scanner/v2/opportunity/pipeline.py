"""Deterministic staged orchestration for the research-only opportunity core."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.opportunity.capabilities import CapabilityState
from intraday_scanner.v2.opportunity.catalyst import InjectedCatalystAdapter
from intraday_scanner.v2.opportunity.discovery import (
    DEFAULT_DISCOVERY_CONFIG,
    DiscoveryConfig,
    discover_candidates,
    enrich_candidates,
)
from intraday_scanner.v2.opportunity.features import (
    DEFAULT_FEATURE_CONFIG,
    FeatureConfig,
    build_feature_snapshots,
)
from intraday_scanner.v2.opportunity.models import (
    DecisionRunContext,
    DecisionTrace,
    EvaluationStatus,
    EvidenceKind,
    ExpectancyEvidence,
    FeatureSnapshot,
    FeatureStage,
    MarketRegime,
    OpportunityCandidate,
    OpportunityContract,
    RankComponent,
    RankedOpportunity,
    RegimeState,
    SecurityRegime,
    StageTraceEntry,
    StrategyDefinition,
    StrategyEvaluation,
    StrategyExpectancyBinding,
    TradeDecision,
    stable_identity,
)
from intraday_scanner.v2.opportunity.quality_gate import (
    DEFAULT_QUALITY_GATE_CONFIG,
    QualityGateConfig,
    reconcile_trade_decisions,
)
from intraday_scanner.v2.opportunity.ranking import (
    DEFAULT_RANKING_CONFIG,
    RankingConfig,
    rank_opportunities,
)
from intraday_scanner.v2.opportunity.regimes import (
    classify_market_regime,
    classify_security_regime,
)
from intraday_scanner.v2.opportunity.registry import (
    StrategyRegistry,
    build_default_registry,
)
from intraday_scanner.v2.opportunity.risk import (
    ExecutionRiskEvidence,
    RiskMetric,
    RiskValueStatus,
)
from intraday_scanner.v2.opportunity.universe import (
    SafetyStatus,
    UniverseEligibility,
    UniverseMember,
    UniverseMembershipStatus,
    UniverseSnapshot,
    market_dataset_content_id,
)


@dataclass(frozen=True)
class PreparedOpportunityPipeline(OpportunityContract):
    preparation_id: str
    decision_at: datetime
    dataset_id: str
    dataset_content_id: str
    universe_snapshot_id: str
    universe_snapshot_content_hash: str
    universe_snapshot: UniverseSnapshot
    universe_provider_receipt_ids: tuple[str, ...]
    registry_definitions: tuple[StrategyDefinition, ...]
    expectancy_bindings: tuple[StrategyExpectancyBinding, ...]
    feature_config: FeatureConfig
    feature_config_identity: str
    feature_config_version: str
    discovery_config: DiscoveryConfig
    discovery_config_identity: str
    discovery_config_version: str
    ranking_config: RankingConfig
    ranking_config_identity: str
    ranking_config_version: str
    sector_bindings: tuple[tuple[str, str], ...]
    correlation_cluster_bindings: tuple[tuple[str, str], ...]
    cheap_snapshots: tuple[FeatureSnapshot, ...]
    rich_snapshots: tuple[FeatureSnapshot, ...]
    benchmark_snapshot: FeatureSnapshot | None
    candidates: tuple[OpportunityCandidate, ...]
    market_regime: MarketRegime
    security_regimes: tuple[SecurityRegime, ...]
    evaluations: tuple[StrategyEvaluation, ...]
    ranked_opportunities: tuple[RankedOpportunity, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.prepared_pipeline.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.preparation_id, "preparation_id"),
            (self.dataset_id, "dataset_id"),
            (self.dataset_content_id, "dataset_content_id"),
            (self.universe_snapshot_id, "universe_snapshot_id"),
            (self.universe_snapshot_content_hash, "universe_snapshot_content_hash"),
            (self.feature_config_identity, "feature_config_identity"),
            (self.feature_config_version, "feature_config_version"),
            (self.discovery_config_identity, "discovery_config_identity"),
            (self.discovery_config_version, "discovery_config_version"),
            (self.ranking_config_identity, "ranking_config_identity"),
            (self.ranking_config_version, "ranking_config_version"),
        ):
            _require_text(value, name)
        if not self.research_only:
            raise ValueError("prepared opportunity pipeline must remain research_only")
        _validate_prepared_universe(self)
        _validate_prepared_configs(self)
        definitions = _validated_registry_definitions(self.registry_definitions)
        _validate_expectancy_bindings(
            self.expectancy_bindings,
            definitions,
            self.decision_at,
        )
        sectors = _validated_symbol_bindings(
            self.sector_bindings,
            allowed_symbols=set(self.universe_snapshot.eligible_symbols),
            label="sector",
        )
        correlations = _validated_symbol_bindings(
            self.correlation_cluster_bindings,
            allowed_symbols=set(self.universe_snapshot.eligible_symbols),
            label="correlation cluster",
        )
        _validate_prepared_outputs(self, definitions)
        expected_ranks = rank_opportunities(
            self.evaluations,
            sector_by_symbol=sectors,
            correlation_cluster_by_symbol=correlations,
            config=self.ranking_config,
        )
        if self.ranked_opportunities != expected_ranks:
            raise ValueError("prepared ranks do not match evaluations and ranking inputs")
        _require_unique(self.limitations, "prepared pipeline limitation")
        expected_id = stable_identity("opportunity-preparation", _preparation_payload(self))
        if self.preparation_id != expected_id:
            raise ValueError("prepared pipeline identity does not match content")


def build_strategy_expectancy_binding(
    *,
    decision_at: datetime,
    strategy_definition: StrategyDefinition,
    evidence: ExpectancyEvidence,
    observed_at: datetime,
    source_identity: str,
    method: str,
    limitations: tuple[str, ...] = (),
) -> StrategyExpectancyBinding:
    """Bind expectancy evidence to one exact registered strategy version."""

    values = {
        "decision_at": decision_at,
        "strategy_id": strategy_definition.strategy_id,
        "strategy_version": strategy_definition.version,
        "strategy_definition_hash": strategy_definition.content_hash(),
        "observed_at": observed_at,
        "source_identity": source_identity,
        "method": method,
        "evidence_id": evidence.evidence_id,
        "evidence_content_hash": evidence.content_hash(),
        "evidence": evidence,
        "limitations": limitations,
        "schema_version": "v2.opportunity.strategy_expectancy_binding.v1",
    }
    return StrategyExpectancyBinding(
        binding_id=stable_identity("strategy-expectancy", values),
        decision_at=decision_at,
        strategy_id=strategy_definition.strategy_id,
        strategy_version=strategy_definition.version,
        strategy_definition_hash=strategy_definition.content_hash(),
        observed_at=observed_at,
        source_identity=source_identity,
        method=method,
        evidence_id=evidence.evidence_id,
        evidence_content_hash=evidence.content_hash(),
        evidence=evidence,
        limitations=limitations,
    )


def prepare_opportunity_pipeline(
    dataset: MarketDataset,
    *,
    universe_snapshot: UniverseSnapshot,
    registry: StrategyRegistry | None = None,
    expectancy_bindings: tuple[StrategyExpectancyBinding, ...] = (),
    sector_by_symbol: Mapping[str, str] | None = None,
    correlation_cluster_by_symbol: Mapping[str, str] | None = None,
    feature_config: FeatureConfig = DEFAULT_FEATURE_CONFIG,
    discovery_config: DiscoveryConfig = DEFAULT_DISCOVERY_CONFIG,
    ranking_config: RankingConfig = DEFAULT_RANKING_CONFIG,
    catalyst_adapter: InjectedCatalystAdapter | None = None,
) -> PreparedOpportunityPipeline:
    """Prepare exact evaluations and ranks before evaluation-bound risk exists."""

    _validate_universe_dataset_input(dataset, universe_snapshot)
    definitions = tuple(
        sorted(
            (registry or build_default_registry()).definitions,
            key=lambda item: (item.strategy_id, item.version),
        )
    )
    active_registry = StrategyRegistry(definitions)
    definitions_by_key = _validated_registry_definitions(definitions)
    if any(
        not isinstance(item, StrategyExpectancyBinding) for item in expectancy_bindings
    ):
        raise TypeError("expectancy bindings must be StrategyExpectancyBinding")
    bindings = tuple(
        sorted(
            expectancy_bindings,
            key=lambda item: (item.strategy_id, item.strategy_version, item.binding_id),
        )
    )
    expectancy_by_key = _validate_expectancy_bindings(
        bindings,
        definitions_by_key,
        universe_snapshot.decision_at,
    )
    sectors = _normalize_symbol_bindings(
        sector_by_symbol,
        allowed_symbols=set(universe_snapshot.eligible_symbols),
        label="sector",
    )
    correlations = _normalize_symbol_bindings(
        correlation_cluster_by_symbol,
        allowed_symbols=set(universe_snapshot.eligible_symbols),
        label="correlation cluster",
    )
    benchmark_member = universe_snapshot.benchmark_member
    usable_benchmark_symbol = (
        benchmark_member.symbol
        if benchmark_member is not None and _benchmark_member_is_usable(benchmark_member)
        else None
    )
    cheap_snapshots = build_feature_snapshots(
        dataset,
        decision_at=universe_snapshot.decision_at,
        universe_id=universe_snapshot.universe_snapshot_id,
        stage=FeatureStage.CHEAP,
        symbols=universe_snapshot.eligible_symbols,
        benchmark_symbol=usable_benchmark_symbol,
        config=feature_config,
        catalyst_adapter=catalyst_adapter,
    )
    discovered = discover_candidates(cheap_snapshots, config=discovery_config)
    candidate_symbols = tuple(item.symbol for item in discovered)
    rich_snapshots = (
        build_feature_snapshots(
            dataset,
            decision_at=universe_snapshot.decision_at,
            universe_id=universe_snapshot.universe_snapshot_id,
            stage=FeatureStage.RICH,
            symbols=candidate_symbols,
            benchmark_symbol=usable_benchmark_symbol,
            config=feature_config,
            catalyst_adapter=catalyst_adapter,
        )
        if candidate_symbols
        else ()
    )
    candidates = enrich_candidates(discovered, rich_snapshots, config=discovery_config)
    benchmark_snapshot = (
        build_feature_snapshots(
            dataset,
            decision_at=universe_snapshot.decision_at,
            universe_id=universe_snapshot.universe_snapshot_id,
            stage=FeatureStage.RICH,
            symbols=(usable_benchmark_symbol,),
            config=feature_config,
            catalyst_adapter=catalyst_adapter,
        )[0]
        if usable_benchmark_symbol is not None
        else None
    )
    market_regime = _classify_prepared_market_regime(
        benchmark_snapshot,
        benchmark_member=benchmark_member,
        decision_at=universe_snapshot.decision_at,
    )
    security_regimes = tuple(
        classify_security_regime(snapshot) for snapshot in rich_snapshots
    )
    snapshots_by_symbol = {item.symbol: item for item in rich_snapshots}
    security_by_symbol = {item.symbol: item for item in security_regimes}
    evidence_by_strategy = {
        strategy_id: binding.evidence
        for (strategy_id, _version), binding in expectancy_by_key.items()
    }
    evaluations = tuple(
        evaluation
        for candidate in candidates
        for evaluation in active_registry.evaluate_all(
            candidate,
            snapshots_by_symbol[candidate.symbol],
            market_regime,
            security_by_symbol[candidate.symbol],
            evidence_by_strategy,
        )
    )
    ranked = rank_opportunities(
        evaluations,
        sector_by_symbol=dict(sectors),
        correlation_cluster_by_symbol=dict(correlations),
        config=ranking_config,
    )
    limitations = tuple(
        dict.fromkeys(
            (
                "research_only_no_persistence_or_mounted_integration",
                "bounded_caller_supplied_universe_not_full_market",
                "heuristic_thresholds_not_validated_statistics",
                "execution_risk_evidence_supplied_only_at_finalize",
                "true_order_flow_unsupported_without_aggressor_side_trade_evidence",
                "session_vwap_is_ohlcv_typical_price_proxy",
                *(
                    tuple(
                        f"benchmark_unavailable:{reason}"
                        for reason in _benchmark_unavailable_reasons(benchmark_member)
                    )
                    if benchmark_snapshot is None
                    else ()
                ),
            )
        )
    )
    values = {
        "decision_at": universe_snapshot.decision_at,
        "dataset_id": dataset.dataset_id,
        "dataset_content_id": market_dataset_content_id(dataset),
        "universe_snapshot_id": universe_snapshot.universe_snapshot_id,
        "universe_snapshot_content_hash": universe_snapshot.content_hash(),
        "universe_snapshot": universe_snapshot,
        "universe_provider_receipt_ids": universe_snapshot.provider_receipt_ids,
        "registry_definitions": definitions,
        "expectancy_bindings": bindings,
        "feature_config": feature_config,
        "feature_config_identity": stable_identity("feature-config", feature_config),
        "feature_config_version": feature_config.config_version,
        "discovery_config": discovery_config,
        "discovery_config_identity": stable_identity("discovery-config", discovery_config),
        "discovery_config_version": discovery_config.threshold_version,
        "ranking_config": ranking_config,
        "ranking_config_identity": stable_identity("ranking-config", ranking_config),
        "ranking_config_version": ranking_config.config_version,
        "sector_bindings": sectors,
        "correlation_cluster_bindings": correlations,
        "cheap_snapshots": cheap_snapshots,
        "rich_snapshots": rich_snapshots,
        "benchmark_snapshot": benchmark_snapshot,
        "candidates": candidates,
        "market_regime": market_regime,
        "security_regimes": security_regimes,
        "evaluations": evaluations,
        "ranked_opportunities": ranked,
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.prepared_pipeline.v1",
    }
    return PreparedOpportunityPipeline(
        preparation_id=stable_identity("opportunity-preparation", values),
        decision_at=universe_snapshot.decision_at,
        dataset_id=dataset.dataset_id,
        dataset_content_id=market_dataset_content_id(dataset),
        universe_snapshot_id=universe_snapshot.universe_snapshot_id,
        universe_snapshot_content_hash=universe_snapshot.content_hash(),
        universe_snapshot=universe_snapshot,
        universe_provider_receipt_ids=universe_snapshot.provider_receipt_ids,
        registry_definitions=definitions,
        expectancy_bindings=bindings,
        feature_config=feature_config,
        feature_config_identity=stable_identity("feature-config", feature_config),
        feature_config_version=feature_config.config_version,
        discovery_config=discovery_config,
        discovery_config_identity=stable_identity("discovery-config", discovery_config),
        discovery_config_version=discovery_config.threshold_version,
        ranking_config=ranking_config,
        ranking_config_identity=stable_identity("ranking-config", ranking_config),
        ranking_config_version=ranking_config.config_version,
        sector_bindings=sectors,
        correlation_cluster_bindings=correlations,
        cheap_snapshots=cheap_snapshots,
        rich_snapshots=rich_snapshots,
        benchmark_snapshot=benchmark_snapshot,
        candidates=candidates,
        market_regime=market_regime,
        security_regimes=security_regimes,
        evaluations=evaluations,
        ranked_opportunities=ranked,
        limitations=limitations,
    )


def _validate_universe_dataset_input(
    dataset: MarketDataset,
    snapshot: UniverseSnapshot,
) -> None:
    if snapshot.dataset_id != dataset.dataset_id:
        raise ValueError("universe snapshot dataset_id does not match dataset")
    if snapshot.dataset_content_id != market_dataset_content_id(dataset):
        raise ValueError("universe snapshot dataset content does not match dataset")
    if any(
        bar.timestamp > snapshot.decision_at
        for bars in dataset.bars_by_symbol.values()
        for bar in bars
    ):
        raise ValueError("market dataset contains observations after decision_at")


def _validated_registry_definitions(
    definitions: tuple[StrategyDefinition, ...],
) -> dict[tuple[str, str], StrategyDefinition]:
    if definitions != tuple(
        sorted(definitions, key=lambda item: (item.strategy_id, item.version))
    ):
        raise ValueError("registry definitions must use canonical strategy-version order")
    definitions_by_key = {
        (item.strategy_id, item.version): item for item in definitions
    }
    if len(definitions_by_key) != len(definitions):
        raise ValueError("duplicate registry strategy-version definition")
    strategy_ids = [item.strategy_id for item in definitions]
    if len(strategy_ids) != len(set(strategy_ids)):
        raise ValueError("strategy version ambiguity in prepared registry")
    StrategyRegistry(definitions)
    return definitions_by_key


def _validate_expectancy_bindings(
    bindings: tuple[StrategyExpectancyBinding, ...],
    definitions: Mapping[tuple[str, str], StrategyDefinition],
    decision_at: datetime,
) -> dict[tuple[str, str], StrategyExpectancyBinding]:
    expected_order = tuple(
        sorted(
            bindings,
            key=lambda item: (item.strategy_id, item.strategy_version, item.binding_id),
        )
    )
    if bindings != expected_order:
        raise ValueError("expectancy bindings must use canonical strategy-version order")
    result: dict[tuple[str, str], StrategyExpectancyBinding] = {}
    for binding in bindings:
        if not isinstance(binding, StrategyExpectancyBinding):
            raise TypeError("expectancy bindings must be StrategyExpectancyBinding")
        key = (binding.strategy_id, binding.strategy_version)
        if key in result:
            raise ValueError("duplicate strategy-version expectancy binding")
        definition = definitions.get(key)
        if definition is None:
            raise ValueError("expectancy binding references unknown strategy version")
        if binding.decision_at != decision_at:
            raise ValueError("expectancy binding decision_at does not match pipeline")
        if binding.observed_at > decision_at:
            raise ValueError("expectancy evidence cannot be observed after decision_at")
        if binding.strategy_definition_hash != definition.content_hash():
            raise ValueError("expectancy binding strategy definition hash mismatch")
        result[key] = binding
    return result


def _normalize_symbol_bindings(
    values: Mapping[str, str] | None,
    *,
    allowed_symbols: set[str],
    label: str,
) -> tuple[tuple[str, str], ...]:
    return tuple(
        sorted(
            _validated_symbol_bindings(
                tuple((symbol, value) for symbol, value in (values or {}).items()),
                allowed_symbols=allowed_symbols,
                label=label,
            ).items()
        )
    )


def _validated_symbol_bindings(
    bindings: tuple[tuple[str, str], ...],
    *,
    allowed_symbols: set[str],
    label: str,
) -> dict[str, str]:
    if bindings != tuple(sorted(bindings)):
        raise ValueError(f"{label} bindings must use canonical order")
    result: dict[str, str] = {}
    for symbol, value in bindings:
        _require_text(symbol, f"{label} symbol")
        _require_sanitized_text(value, label)
        if symbol in result:
            raise ValueError(f"duplicate {label} symbol")
        if symbol not in allowed_symbols:
            raise ValueError(f"{label} binding references symbol outside universe")
        result[symbol] = value
    return result


def _validate_prepared_universe(prepared: PreparedOpportunityPipeline) -> None:
    snapshot = prepared.universe_snapshot
    if (
        prepared.universe_snapshot_id != snapshot.universe_snapshot_id
        or prepared.universe_snapshot_content_hash != snapshot.content_hash()
    ):
        raise ValueError("prepared universe identity does not match embedded snapshot")
    if prepared.decision_at != snapshot.decision_at:
        raise ValueError("prepared decision_at does not match universe snapshot")
    if (
        prepared.dataset_id != snapshot.dataset_id
        or prepared.dataset_content_id != snapshot.dataset_content_id
    ):
        raise ValueError("prepared dataset identity does not match universe snapshot")
    if prepared.universe_provider_receipt_ids != snapshot.provider_receipt_ids:
        raise ValueError("prepared universe capability IDs do not match snapshot")


def _validate_prepared_configs(prepared: PreparedOpportunityPipeline) -> None:
    expected = (
        stable_identity("feature-config", prepared.feature_config),
        prepared.feature_config.config_version,
        stable_identity("discovery-config", prepared.discovery_config),
        prepared.discovery_config.threshold_version,
        stable_identity("ranking-config", prepared.ranking_config),
        prepared.ranking_config.config_version,
    )
    actual = (
        prepared.feature_config_identity,
        prepared.feature_config_version,
        prepared.discovery_config_identity,
        prepared.discovery_config_version,
        prepared.ranking_config_identity,
        prepared.ranking_config_version,
    )
    if actual != expected:
        raise ValueError("prepared config identity or version does not match embedded config")


def _benchmark_member_is_usable(member: UniverseMember) -> bool:
    return (
        member.membership_status is UniverseMembershipStatus.INCLUDED
        and member.eligibility is UniverseEligibility.ELIGIBLE
        and member.data_availability is CapabilityState.AVAILABLE
        and member.halt_status is SafetyStatus.CLEAR
        and member.corporate_action_status is SafetyStatus.CLEAR
    )


def _benchmark_unavailable_reasons(member: UniverseMember | None) -> tuple[str, ...]:
    if member is None:
        return ("benchmark_member_unavailable",)
    reasons: list[str] = []
    if member.membership_status is not UniverseMembershipStatus.INCLUDED:
        reasons.append(f"benchmark_membership_{member.membership_status.value}")
    if member.eligibility is not UniverseEligibility.ELIGIBLE:
        reasons.append(f"benchmark_eligibility_{member.eligibility.value}")
    if member.data_availability is not CapabilityState.AVAILABLE:
        reasons.append(f"benchmark_data_{member.data_availability.value}")
    if member.halt_status is not SafetyStatus.CLEAR:
        reasons.append(f"benchmark_halt_{member.halt_status.value}")
    if member.corporate_action_status is not SafetyStatus.CLEAR:
        reasons.append(
            f"benchmark_corporate_action_{member.corporate_action_status.value}"
        )
    reasons.extend(
        f"benchmark_exclusion:{reason}" for reason in member.exclusion_reason_codes
    )
    if not reasons:
        reasons.append("benchmark_member_unavailable")
    return tuple(dict.fromkeys(reasons))


def _classify_prepared_market_regime(
    benchmark_snapshot: FeatureSnapshot | None,
    *,
    benchmark_member: UniverseMember | None,
    decision_at: datetime,
) -> MarketRegime:
    if benchmark_snapshot is not None:
        if benchmark_member is None or not _benchmark_member_is_usable(benchmark_member):
            raise ValueError("benchmark snapshot cannot use excluded or unavailable member")
        return classify_market_regime(benchmark_snapshot, decision_at=decision_at)
    reasons = _benchmark_unavailable_reasons(benchmark_member)
    values = {
        "decision_at": decision_at,
        "benchmark_member_id": (
            benchmark_member.member_id if benchmark_member is not None else None
        ),
        "benchmark_member_content_hash": (
            benchmark_member.content_hash() if benchmark_member is not None else None
        ),
        "state": RegimeState.INSUFFICIENT_DATA,
        "reasons": reasons,
    }
    return MarketRegime(
        regime_id=stable_identity("market-regime", values),
        decision_at=decision_at,
        benchmark_symbol=None,
        state=RegimeState.INSUFFICIENT_DATA,
        measurements=(),
        confidence=None,
        evidence_kind=EvidenceKind.HEURISTIC,
        reasons=reasons,
    )


def _validate_prepared_outputs(
    prepared: PreparedOpportunityPipeline,
    definitions: Mapping[tuple[str, str], StrategyDefinition],
) -> None:
    eligible_symbols = prepared.universe_snapshot.eligible_symbols
    if tuple(item.symbol for item in prepared.cheap_snapshots) != eligible_symbols:
        raise ValueError("cheap snapshots do not exactly match eligible universe symbols")
    for snapshot in (*prepared.cheap_snapshots, *prepared.rich_snapshots):
        if (
            snapshot.decision_at != prepared.decision_at
            or snapshot.dataset_id != prepared.dataset_id
            or snapshot.universe_id != prepared.universe_snapshot_id
        ):
            raise ValueError("feature snapshot does not match prepared run identity")
    candidate_symbols = tuple(item.symbol for item in prepared.candidates)
    if tuple(item.symbol for item in prepared.rich_snapshots) != candidate_symbols:
        raise ValueError("rich snapshots do not exactly match candidate symbols")
    if len(set(candidate_symbols)) != len(candidate_symbols):
        raise ValueError("duplicate prepared candidate symbol")
    if set(candidate_symbols) - set(eligible_symbols):
        raise ValueError("candidate symbol is outside authoritative universe")
    candidate_ids = [item.candidate_id for item in prepared.candidates]
    if len(candidate_ids) != len(set(candidate_ids)):
        raise ValueError("duplicate prepared candidate identity")
    rich_by_symbol = {item.symbol: item for item in prepared.rich_snapshots}
    for candidate in prepared.candidates:
        if (
            candidate.decision_at != prepared.decision_at
            or candidate.feature_snapshot_id
            != rich_by_symbol[candidate.symbol].snapshot_id
        ):
            raise ValueError("candidate does not match prepared rich feature snapshot")
    benchmark_member = prepared.universe_snapshot.benchmark_member
    benchmark_usable = (
        benchmark_member is not None and _benchmark_member_is_usable(benchmark_member)
    )
    if benchmark_usable is not (prepared.benchmark_snapshot is not None):
        raise ValueError(
            "benchmark feature snapshot presence does not match eligible benchmark truth"
        )
    if prepared.benchmark_snapshot is not None:
        assert benchmark_member is not None
        benchmark = prepared.benchmark_snapshot
        if (
            benchmark.symbol != benchmark_member.symbol
            or benchmark.symbol in set(eligible_symbols)
            or benchmark.decision_at != prepared.decision_at
            or benchmark.dataset_id != prepared.dataset_id
            or benchmark.universe_id != prepared.universe_snapshot_id
        ):
            raise ValueError("benchmark snapshot is not separately bound to universe benchmark")
    expected_market_regime = _classify_prepared_market_regime(
        prepared.benchmark_snapshot,
        benchmark_member=benchmark_member,
        decision_at=prepared.decision_at,
    )
    if prepared.market_regime != expected_market_regime:
        raise ValueError("market regime does not match usable benchmark truth")
    if tuple(item.symbol for item in prepared.security_regimes) != candidate_symbols:
        raise ValueError("security regimes do not exactly match candidates")
    if any(item.decision_at != prepared.decision_at for item in prepared.security_regimes):
        raise ValueError("security regime decision_at does not match preparation")
    bindings = {
        (item.strategy_id, item.strategy_version): item
        for item in prepared.expectancy_bindings
    }
    expectancy_by_strategy = {
        strategy_id: binding.evidence
        for (strategy_id, _version), binding in bindings.items()
    }
    registry = StrategyRegistry(tuple(definitions.values()))
    security_by_symbol = {item.symbol: item for item in prepared.security_regimes}
    expected_evaluations = tuple(
        evaluation
        for candidate in prepared.candidates
        for evaluation in registry.evaluate_all(
            candidate,
            rich_by_symbol[candidate.symbol],
            prepared.market_regime,
            security_by_symbol[candidate.symbol],
            expectancy_by_strategy,
        )
    )
    if prepared.evaluations != expected_evaluations:
        raise ValueError("prepared evaluations do not match registry inputs")


def _preparation_payload(prepared: PreparedOpportunityPipeline) -> dict[str, object]:
    return {
        name: value
        for name, value in prepared.__dict__.items()
        if name != "preparation_id"
    }


_PRIVATE_VALUE = re.compile(
    r"(?i)(?:api[_-]?key|secret|access[_-]?token|token|password|authorization)"
    r"\s*[:=]\s*\S+|\bbearer\s+\S+|https?://[^\s]*(?:@|api[_-]?key=|token=|secret=)"
    r"|(?:[A-Za-z]:[\\/]|\\\\|/Users/|/home/)"
    r"|(?:localhost|127\.0\.0\.1|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+)"
)


def _require_text(value: str, field_name: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} cannot be blank")


def _require_sanitized_text(value: str, field_name: str) -> None:
    _require_text(value, field_name)
    if _PRIVATE_VALUE.search(value):
        raise ValueError(f"{field_name} contains a private or secret value")


def _require_unique(values: tuple[str, ...], label: str) -> None:
    if len(values) != len(set(values)):
        raise ValueError(f"duplicate {label}")


@dataclass(frozen=True)
class PipelineRiskPolicy(OpportunityContract):
    risk_policy_id: str
    policy_version: str
    account_identity: str
    risk_cap_identity: str
    concentration_identity: str
    minimum_after_cost_reward_risk: Decimal
    research_only: bool = True
    schema_version: str = "v2.opportunity.pipeline_risk_policy.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.risk_policy_id, "risk_policy_id"),
            (self.policy_version, "policy_version"),
            (self.account_identity, "account_identity"),
            (self.risk_cap_identity, "risk_cap_identity"),
            (self.concentration_identity, "concentration_identity"),
        ):
            _require_sanitized_text(value, name)
        if (
            not self.minimum_after_cost_reward_risk.is_finite()
            or self.minimum_after_cost_reward_risk <= 0
        ):
            raise ValueError("minimum_after_cost_reward_risk must be finite and positive")
        if not self.research_only:
            raise ValueError("pipeline risk policy must remain research_only")
        expected = stable_identity("pipeline-risk-policy", _risk_policy_payload(self))
        if self.risk_policy_id != expected:
            raise ValueError("pipeline risk policy identity does not match content")


def build_pipeline_risk_policy(
    *,
    policy_version: str,
    account_identity: str,
    risk_cap_identity: str,
    concentration_identity: str,
    minimum_after_cost_reward_risk: Decimal,
) -> PipelineRiskPolicy:
    values = {
        "policy_version": policy_version,
        "account_identity": account_identity,
        "risk_cap_identity": risk_cap_identity,
        "concentration_identity": concentration_identity,
        "minimum_after_cost_reward_risk": minimum_after_cost_reward_risk,
        "research_only": True,
        "schema_version": "v2.opportunity.pipeline_risk_policy.v1",
    }
    return PipelineRiskPolicy(
        risk_policy_id=stable_identity("pipeline-risk-policy", values),
        policy_version=policy_version,
        account_identity=account_identity,
        risk_cap_identity=risk_cap_identity,
        concentration_identity=concentration_identity,
        minimum_after_cost_reward_risk=minimum_after_cost_reward_risk,
    )


@dataclass(frozen=True)
class PipelineResult(OpportunityContract):
    run_id: str
    decision_at: datetime
    dataset_id: str
    dataset_content_id: str
    universe_snapshot_id: str
    universe_snapshot_content_hash: str
    preparation: PreparedOpportunityPipeline
    risk_policy: PipelineRiskPolicy
    gate_config: QualityGateConfig
    gate_config_identity: str
    gate_config_version: str
    risk_evidence: tuple[ExecutionRiskEvidence, ...]
    decision_context: DecisionRunContext | None
    decisions: tuple[TradeDecision, ...]
    traces: tuple[DecisionTrace, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    schema_version: str = "v2.opportunity.pipeline_result.v2"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, name in (
            (self.run_id, "run_id"),
            (self.dataset_id, "dataset_id"),
            (self.dataset_content_id, "dataset_content_id"),
            (self.universe_snapshot_id, "universe_snapshot_id"),
            (self.universe_snapshot_content_hash, "universe_snapshot_content_hash"),
            (self.gate_config_identity, "gate_config_identity"),
            (self.gate_config_version, "gate_config_version"),
        ):
            _require_text(value, name)
        if not self.research_only:
            raise ValueError("opportunity pipeline result must remain research_only")
        if (
            self.decision_at != self.preparation.decision_at
            or self.dataset_id != self.preparation.dataset_id
            or self.dataset_content_id != self.preparation.dataset_content_id
            or self.universe_snapshot_id != self.preparation.universe_snapshot_id
            or self.universe_snapshot_content_hash
            != self.preparation.universe_snapshot_content_hash
        ):
            raise ValueError("pipeline result does not match prepared dataset/universe identity")
        _validate_gate_and_risk_policy(self.gate_config, self.risk_policy)
        if (
            self.gate_config_identity
            != stable_identity("quality-gate-config", self.gate_config)
            or self.gate_config_version != self.gate_config.config_version
        ):
            raise ValueError("pipeline result gate config identity does not match content")
        risk_map = _ordered_risk_mapping(self.preparation, self.risk_evidence)
        _validate_pipeline_risk_policy(self.risk_policy, risk_map)
        expected_decisions = reconcile_trade_decisions(
            self.preparation.evaluations,
            self.preparation.ranked_opportunities,
            risk_by_evaluation=risk_map,
            config=self.gate_config,
        )
        if self.decisions != expected_decisions:
            raise ValueError("pipeline decisions do not exactly reconcile prepared evaluations")
        expected_context = self.decisions[0].decision_context if self.decisions else None
        if self.decision_context != expected_context:
            raise ValueError("pipeline decision context does not match reconciled decisions")
        if self.decision_context is not None and any(
            item.decision_context != self.decision_context for item in self.decisions
        ):
            raise ValueError("pipeline decisions do not share one decision context")
        expected_traces = _build_pair_traces(
            self.preparation,
            risk_map,
            self.decisions,
        )
        if self.traces != expected_traces:
            raise ValueError("pipeline traces do not exactly reconcile pair decisions")
        _require_unique(self.limitations, "pipeline result limitation")
        expected_id = stable_identity("opportunity-run", _pipeline_run_payload(self))
        if self.run_id != expected_id:
            raise ValueError("opportunity run identity does not match content")

    @property
    def cheap_snapshots(self) -> tuple[FeatureSnapshot, ...]:
        return self.preparation.cheap_snapshots

    @property
    def rich_snapshots(self) -> tuple[FeatureSnapshot, ...]:
        return self.preparation.rich_snapshots

    @property
    def benchmark_snapshot(self) -> FeatureSnapshot | None:
        return self.preparation.benchmark_snapshot

    @property
    def candidates(self) -> tuple[OpportunityCandidate, ...]:
        return self.preparation.candidates

    @property
    def market_regime(self) -> MarketRegime:
        return self.preparation.market_regime

    @property
    def security_regimes(self) -> tuple[SecurityRegime, ...]:
        return self.preparation.security_regimes

    @property
    def evaluations(self) -> tuple[StrategyEvaluation, ...]:
        return self.preparation.evaluations

    @property
    def ranked_opportunities(self) -> tuple[RankedOpportunity, ...]:
        return self.preparation.ranked_opportunities


def run_opportunity_pipeline(
    preparation: PreparedOpportunityPipeline,
    *,
    risk_by_evaluation: Mapping[str, ExecutionRiskEvidence],
    risk_policy: PipelineRiskPolicy,
    gate_config: QualityGateConfig = DEFAULT_QUALITY_GATE_CONFIG,
) -> PipelineResult:
    """Finalize one prepared run with exact evaluation-bound execution risk."""

    _validate_gate_and_risk_policy(gate_config, risk_policy)
    if any(not isinstance(key, str) for key in risk_by_evaluation):
        raise TypeError("risk mapping keys must be evaluation ID strings")
    if any(
        not isinstance(value, ExecutionRiskEvidence)
        for value in risk_by_evaluation.values()
    ):
        raise TypeError("risk mapping values must be ExecutionRiskEvidence")
    risk_map = dict(risk_by_evaluation)
    ordered_risk = tuple(
        risk_map[item.evaluation_id]
        for item in preparation.evaluations
        if item.status is EvaluationStatus.ELIGIBLE
        and item.evaluation_id in risk_map
    )
    validated_map = _ordered_risk_mapping(preparation, ordered_risk)
    if set(validated_map) != set(risk_map):
        raise ValueError("risk mapping contains unknown, missing, or noneligible keys")
    for key, value in risk_map.items():
        if value.evaluation_id != key or validated_map.get(key) != value:
            raise ValueError("risk mapping key does not match exact receipt")
    _validate_pipeline_risk_policy(risk_policy, validated_map)
    decisions = reconcile_trade_decisions(
        preparation.evaluations,
        preparation.ranked_opportunities,
        risk_by_evaluation=validated_map,
        config=gate_config,
    )
    decision_context = decisions[0].decision_context if decisions else None
    traces = _build_pair_traces(preparation, validated_map, decisions)
    limitations = tuple(
        dict.fromkeys(
            (
                *preparation.limitations,
                "all_pair_decisions_and_execution_risk_integrated_in_unmounted_core",
                "no_live_execution_broker_network_or_persistence_path",
            )
        )
    )
    values = _pipeline_run_values(
        preparation=preparation,
        risk_policy=risk_policy,
        gate_config=gate_config,
        risk_evidence=ordered_risk,
        decision_context=decision_context,
        decisions=decisions,
        traces=traces,
        limitations=limitations,
    )
    return PipelineResult(
        run_id=stable_identity("opportunity-run", values),
        decision_at=preparation.decision_at,
        dataset_id=preparation.dataset_id,
        dataset_content_id=preparation.dataset_content_id,
        universe_snapshot_id=preparation.universe_snapshot_id,
        universe_snapshot_content_hash=preparation.universe_snapshot_content_hash,
        preparation=preparation,
        risk_policy=risk_policy,
        gate_config=gate_config,
        gate_config_identity=stable_identity("quality-gate-config", gate_config),
        gate_config_version=gate_config.config_version,
        risk_evidence=ordered_risk,
        decision_context=decision_context,
        decisions=decisions,
        traces=traces,
        limitations=limitations,
    )


def _ordered_risk_mapping(
    preparation: PreparedOpportunityPipeline,
    risk_evidence: tuple[ExecutionRiskEvidence, ...],
) -> dict[str, ExecutionRiskEvidence]:
    eligible = tuple(
        item for item in preparation.evaluations if item.status is EvaluationStatus.ELIGIBLE
    )
    if tuple(item.evaluation_id for item in risk_evidence) != tuple(
        item.evaluation_id for item in eligible
    ):
        raise ValueError("risk evidence must exactly follow eligible evaluation order")
    if len({item.execution_risk_evidence_id for item in risk_evidence}) != len(risk_evidence):
        raise ValueError("duplicate execution risk evidence identity")
    result = {item.evaluation_id: item for item in risk_evidence}
    if len(result) != len(risk_evidence):
        raise ValueError("duplicate execution risk evaluation identity")
    for evaluation in eligible:
        receipt = result[evaluation.evaluation_id]
        if (
            receipt.evaluation_content_hash != evaluation.content_hash()
            or receipt.symbol != evaluation.symbol
            or receipt.strategy_id != evaluation.strategy_id
            or receipt.strategy_version != evaluation.strategy_version
            or receipt.direction is not evaluation.direction
            or receipt.decision_at != evaluation.decision_at
        ):
            raise ValueError("execution risk receipt does not match prepared evaluation")
        if any(item.observed_at > preparation.decision_at for item in receipt.metrics):
            raise ValueError("execution risk metric cannot be observed after decision_at")
    return result


def _validate_gate_and_risk_policy(
    gate_config: QualityGateConfig,
    risk_policy: PipelineRiskPolicy,
) -> None:
    if gate_config.minimum_after_cost_reward_risk != (
        risk_policy.minimum_after_cost_reward_risk
    ):
        raise ValueError("gate minimum after-cost R does not match pipeline risk policy")


def _validate_pipeline_risk_policy(
    policy: PipelineRiskPolicy,
    risk_map: Mapping[str, ExecutionRiskEvidence],
) -> None:
    for receipt in risk_map.values():
        if (
            receipt.account_identity != policy.account_identity
            or receipt.risk_cap_identity != policy.risk_cap_identity
            or receipt.concentration_identity != policy.concentration_identity
        ):
            raise ValueError("execution risk identities do not match pipeline risk policy")
        minimum = receipt.metric(RiskMetric.MIN_AFTER_COST_REWARD_RISK)
        if (
            minimum.status is not RiskValueStatus.UNAVAILABLE
            and minimum.value != policy.minimum_after_cost_reward_risk
        ):
            raise ValueError("execution risk minimum after-cost R does not match policy")


def _build_pair_traces(
    preparation: PreparedOpportunityPipeline,
    risk_map: Mapping[str, ExecutionRiskEvidence],
    decisions: tuple[TradeDecision, ...],
) -> tuple[DecisionTrace, ...]:
    if not preparation.evaluations:
        if risk_map or decisions:
            raise ValueError("empty preparation cannot have risk, decisions, or traces")
        return ()
    candidates = {item.candidate_id: item for item in preparation.candidates}
    cheap = {item.symbol: item for item in preparation.cheap_snapshots}
    rich = {item.symbol: item for item in preparation.rich_snapshots}
    security = {item.symbol: item for item in preparation.security_regimes}
    ranks = {item.evaluation_id: item for item in preparation.ranked_opportunities}
    decision_map = {item.evaluation_id: item for item in decisions}
    if set(decision_map) != {item.evaluation_id for item in preparation.evaluations}:
        raise ValueError("trace decisions do not reconcile prepared evaluations")
    universe = preparation.universe_snapshot
    members = {
        item.symbol: item for item in (*universe.included_members, *universe.excluded_members)
    }
    requested_member_ids = tuple(
        members[symbol].member_id for symbol in universe.requested_symbols
    )
    included_member_ids = tuple(item.member_id for item in universe.included_members)
    excluded_member_ids = tuple(item.member_id for item in universe.excluded_members)
    global_rank_inputs = tuple(
        item.evaluation_id
        for item in preparation.evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    )
    global_ranked_ids = tuple(
        item.ranked_id for item in preparation.ranked_opportunities
    )
    traces: list[DecisionTrace] = []
    for evaluation in preparation.evaluations:
        candidate = candidates[evaluation.candidate_id]
        ranked = ranks.get(evaluation.evaluation_id)
        risk = risk_map.get(evaluation.evaluation_id)
        decision = decision_map[evaluation.evaluation_id]
        member = members[evaluation.symbol]
        stages = (
            _stage(
                1,
                "authoritative_universe_snapshot",
                (
                    universe.universe_snapshot_id,
                    universe.content_hash(),
                    *universe.provider_receipt_ids,
                    *requested_member_ids,
                ),
                included_member_ids,
                reasons=(
                    f"requested_count:{universe.requested_count}",
                    f"included_count:{universe.included_count}",
                    f"excluded_count:{universe.excluded_count}",
                ),
                limitations=universe.limitations,
            ),
            _stage(
                2,
                "cheap_features",
                (preparation.dataset_content_id, member.member_id),
                (cheap[evaluation.symbol].snapshot_id,),
                limitations=cheap[evaluation.symbol].limitations,
            ),
            _stage(
                3,
                "strategy_independent_discovery",
                (cheap[evaluation.symbol].snapshot_id,),
                (candidate.candidate_id,),
                reasons=candidate.discovery_reasons,
            ),
            _stage(
                4,
                "candidate_rich_features",
                (candidate.candidate_id,),
                (rich[evaluation.symbol].snapshot_id,),
                limitations=rich[evaluation.symbol].limitations,
            ),
            _stage(
                5,
                "market_and_security_regimes",
                (
                    rich[evaluation.symbol].snapshot_id,
                    *((preparation.benchmark_snapshot.snapshot_id,)
                      if preparation.benchmark_snapshot is not None else ()),
                ),
                (preparation.market_regime.regime_id, security[evaluation.symbol].regime_id),
                reasons=(
                    *preparation.market_regime.reasons,
                    *security[evaluation.symbol].reasons,
                ),
            ),
            _stage(
                6,
                "strategy_pair_evaluation",
                (
                    candidate.candidate_id,
                    preparation.market_regime.regime_id,
                    security[evaluation.symbol].regime_id,
                    evaluation.strategy_definition_hash,
                    evaluation.evaluator_code_hash,
                    *((evaluation.expectancy.evidence_id,)
                      if evaluation.expectancy is not None else ()),
                ),
                (evaluation.evaluation_id,),
                reasons=evaluation.reasons,
            ),
            _stage(
                7,
                "global_pair_ranking",
                global_rank_inputs,
                global_ranked_ids,
                score_components=ranked.components if ranked is not None else (),
                limitations=ranked.limitations if ranked is not None else (),
            ),
            _stage(
                8,
                "absolute_quality_gate",
                (
                    evaluation.evaluation_id,
                    decision.decision_run_id,
                    *((ranked.ranked_id,) if ranked is not None else ()),
                    *((risk.execution_risk_evidence_id,) if risk is not None else ()),
                ),
                (decision.decision_id,),
                reasons=decision.rationale,
                limitations=decision.limitations,
            ),
        )
        trace_limitations = tuple(
            dict.fromkeys(
                limitation for stage in stages for limitation in stage.limitations
            )
        )
        values = {
            "universe_snapshot_id": universe.universe_snapshot_id,
            "universe_snapshot_content_hash": universe.content_hash(),
            "universe_provider_receipt_ids": universe.provider_receipt_ids,
            "universe_requested_member_ids": requested_member_ids,
            "universe_included_member_ids": included_member_ids,
            "universe_excluded_member_ids": excluded_member_ids,
            "universe_requested_count": universe.requested_count,
            "universe_included_count": universe.included_count,
            "universe_excluded_count": universe.excluded_count,
            "universe_member_id": member.member_id,
            "evaluation_id": evaluation.evaluation_id,
            "evaluation_content_hash": evaluation.content_hash(),
            "symbol": evaluation.symbol,
            "strategy_id": evaluation.strategy_id,
            "strategy_version": evaluation.strategy_version,
            "direction": evaluation.direction,
            "decision_at": evaluation.decision_at,
            "candidate_id": evaluation.candidate_id,
            "ranked_id": ranked.ranked_id if ranked is not None else None,
            "ranked_content_hash": ranked.content_hash() if ranked is not None else None,
            "risk_evidence_id": (
                risk.execution_risk_evidence_id if risk is not None else None
            ),
            "risk_evidence_content_hash": risk.content_hash() if risk is not None else None,
            "decision_run_id": decision.decision_run_id,
            "global_rank_input_ids": global_rank_inputs,
            "global_ranked_ids": global_ranked_ids,
            "evaluation": evaluation,
            "ranked": ranked,
            "stages": stages,
            "final_decision_id": decision.decision_id,
            "final_decision_content_hash": decision.content_hash(),
            "final_decision": decision.decision,
            "final_trade_decision": decision,
            "limitations": trace_limitations,
            "schema_version": "v2.opportunity.decision_trace.v2",
        }
        traces.append(
            DecisionTrace(
                trace_id=stable_identity("decision-trace", values),
                universe_snapshot_id=universe.universe_snapshot_id,
                universe_snapshot_content_hash=universe.content_hash(),
                universe_provider_receipt_ids=universe.provider_receipt_ids,
                universe_requested_member_ids=requested_member_ids,
                universe_included_member_ids=included_member_ids,
                universe_excluded_member_ids=excluded_member_ids,
                universe_requested_count=universe.requested_count,
                universe_included_count=universe.included_count,
                universe_excluded_count=universe.excluded_count,
                universe_member_id=member.member_id,
                evaluation_id=evaluation.evaluation_id,
                evaluation_content_hash=evaluation.content_hash(),
                symbol=evaluation.symbol,
                strategy_id=evaluation.strategy_id,
                strategy_version=evaluation.strategy_version,
                direction=evaluation.direction,
                decision_at=evaluation.decision_at,
                candidate_id=evaluation.candidate_id,
                ranked_id=ranked.ranked_id if ranked is not None else None,
                ranked_content_hash=(ranked.content_hash() if ranked is not None else None),
                risk_evidence_id=(
                    risk.execution_risk_evidence_id if risk is not None else None
                ),
                risk_evidence_content_hash=(
                    risk.content_hash() if risk is not None else None
                ),
                decision_run_id=decision.decision_run_id,
                global_rank_input_ids=global_rank_inputs,
                global_ranked_ids=global_ranked_ids,
                evaluation=evaluation,
                ranked=ranked,
                stages=stages,
                final_decision_id=decision.decision_id,
                final_decision_content_hash=decision.content_hash(),
                final_decision=decision.decision,
                final_trade_decision=decision,
                limitations=trace_limitations,
            )
        )
    return tuple(traces)


def _stage(
    ordinal: int,
    name: str,
    input_ids: tuple[str, ...],
    output_ids: tuple[str, ...],
    *,
    reasons: tuple[str, ...] = (),
    score_components: tuple[RankComponent, ...] = (),
    limitations: tuple[str, ...] = (),
) -> StageTraceEntry:
    return StageTraceEntry(
        ordinal=ordinal,
        stage_name=name,
        input_ids=input_ids,
        output_ids=output_ids,
        input_count=len(input_ids),
        output_count=len(output_ids),
        reasons=tuple(dict.fromkeys(reasons)),
        score_components=score_components,
        limitations=tuple(dict.fromkeys(limitations)),
    )


def _risk_policy_payload(policy: PipelineRiskPolicy) -> dict[str, object]:
    return {
        name: value for name, value in policy.__dict__.items() if name != "risk_policy_id"
    }


def _pipeline_run_values(
    *,
    preparation: PreparedOpportunityPipeline,
    risk_policy: PipelineRiskPolicy,
    gate_config: QualityGateConfig,
    risk_evidence: tuple[ExecutionRiskEvidence, ...],
    decision_context: DecisionRunContext | None,
    decisions: tuple[TradeDecision, ...],
    traces: tuple[DecisionTrace, ...],
    limitations: tuple[str, ...],
) -> dict[str, object]:
    benchmark = preparation.benchmark_snapshot
    return {
        "decision_at": preparation.decision_at,
        "dataset_id": preparation.dataset_id,
        "dataset_content_id": preparation.dataset_content_id,
        "universe_snapshot_id": preparation.universe_snapshot_id,
        "universe_snapshot_content_hash": preparation.universe_snapshot_content_hash,
        "preparation_id": preparation.preparation_id,
        "preparation_content_hash": preparation.content_hash(),
        "benchmark_snapshot_id": benchmark.snapshot_id if benchmark is not None else None,
        "benchmark_snapshot_content_hash": benchmark.content_hash() if benchmark else None,
        "benchmark_member_id": (
            preparation.universe_snapshot.benchmark_member.member_id
            if preparation.universe_snapshot.benchmark_member is not None
            else None
        ),
        "registry_definitions": tuple(
            (
                item.strategy_id,
                item.version,
                item.content_hash(),
                item.evaluator_id,
                item.evaluator_code_hash,
            )
            for item in preparation.registry_definitions
        ),
        "feature_config": (
            preparation.feature_config_identity,
            preparation.feature_config_version,
        ),
        "discovery_config": (
            preparation.discovery_config_identity,
            preparation.discovery_config_version,
        ),
        "ranking_config": (
            preparation.ranking_config_identity,
            preparation.ranking_config_version,
        ),
        "gate_config": (
            stable_identity("quality-gate-config", gate_config),
            gate_config.config_version,
        ),
        "risk_policy": (
            risk_policy.risk_policy_id,
            risk_policy.content_hash(),
            risk_policy.policy_version,
        ),
        "universe_provider_receipt_ids": preparation.universe_provider_receipt_ids,
        "risk_provider_receipts": tuple(
            tuple(
                (item.capability_receipt_id, item.content_hash())
                for item in receipt.capability_receipts
            )
            for receipt in risk_evidence
        ),
        "expectancy_bindings": tuple(
            (item.binding_id, item.content_hash())
            for item in preparation.expectancy_bindings
        ),
        "evaluations": tuple(
            (item.evaluation_id, item.content_hash()) for item in preparation.evaluations
        ),
        "ranks": tuple(
            (item.ranked_id, item.content_hash())
            for item in preparation.ranked_opportunities
        ),
        "risks": tuple(
            (item.execution_risk_evidence_id, item.content_hash())
            for item in risk_evidence
        ),
        "decision_context": (
            (
                decision_context.decision_run_id,
                decision_context.content_hash(),
            )
            if decision_context is not None
            else None
        ),
        "decisions": tuple(
            (item.decision_id, item.content_hash()) for item in decisions
        ),
        "traces": tuple((item.trace_id, item.content_hash()) for item in traces),
        "limitations": limitations,
        "research_only": True,
        "schema_version": "v2.opportunity.pipeline_result.v2",
    }


def _pipeline_run_payload(result: PipelineResult) -> dict[str, object]:
    return _pipeline_run_values(
        preparation=result.preparation,
        risk_policy=result.risk_policy,
        gate_config=result.gate_config,
        risk_evidence=result.risk_evidence,
        decision_context=result.decision_context,
        decisions=result.decisions,
        traces=result.traces,
        limitations=result.limitations,
    )


__all__ = [
    "PipelineResult",
    "PipelineRiskPolicy",
    "PreparedOpportunityPipeline",
    "build_pipeline_risk_policy",
    "build_strategy_expectancy_binding",
    "prepare_opportunity_pipeline",
    "run_opportunity_pipeline",
]
