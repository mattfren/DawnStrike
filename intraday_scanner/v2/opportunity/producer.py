"""Disabled-by-default mounted research producer for immutable opportunity runs."""

from __future__ import annotations

import os
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Generic, TypeVar

from intraday_scanner.storage.opportunity_store import (
    OpportunityPersistenceReceipt,
    OpportunityStore,
)
from intraday_scanner.storage.test_isolation import ACTIVE_DATABASE
from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.opportunity.catalyst import InjectedCatalystAdapter
from intraday_scanner.v2.opportunity.discovery import (
    DEFAULT_DISCOVERY_CONFIG,
    DiscoveryConfig,
)
from intraday_scanner.v2.opportunity.features import DEFAULT_FEATURE_CONFIG, FeatureConfig
from intraday_scanner.v2.opportunity.models import StrategyExpectancyBinding
from intraday_scanner.v2.opportunity.pipeline import (
    PipelineResult,
    PipelineRiskPolicy,
    PreparedOpportunityPipeline,
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.quality_gate import (
    DEFAULT_QUALITY_GATE_CONFIG,
    QualityGateConfig,
)
from intraday_scanner.v2.opportunity.ranking import DEFAULT_RANKING_CONFIG, RankingConfig
from intraday_scanner.v2.opportunity.registry import StrategyRegistry
from intraday_scanner.v2.opportunity.risk import ExecutionRiskEvidence
from intraday_scanner.v2.opportunity.universe import UniverseSnapshot

T = TypeVar("T")


class StageStatus(str, Enum):
    COMPLETE = "COMPLETE"
    CACHE_HIT = "CACHE_HIT"
    FAILED = "FAILED"


class ProducerFailureCode(str, Enum):
    DISABLED = "producer_disabled"
    ACTIVE_PATH_FORBIDDEN = "active_database_forbidden"
    EXPLICIT_PATH_REQUIRED = "explicit_non_active_database_required"
    PIPELINE_FAILED = "pipeline_failed"
    PERSISTENCE_FAILED = "persistence_failed"


@dataclass(frozen=True)
class StageTelemetry:
    stage_name: str
    status: StageStatus
    failure_code: ProducerFailureCode | None
    input_count: int
    output_count: int
    duration_ms: float

    def __post_init__(self) -> None:
        if not self.stage_name.strip() or self.input_count < 0 or self.output_count < 0:
            raise ValueError("stage telemetry requires a name and non-negative counts")
        if self.duration_ms < 0:
            raise ValueError("stage duration cannot be negative")
        if self.status is StageStatus.FAILED and self.failure_code is None:
            raise ValueError("failed stage telemetry requires a bounded failure code")
        if self.status is not StageStatus.FAILED and self.failure_code is not None:
            raise ValueError("successful stage telemetry cannot carry a failure code")

    def deterministic_payload(self) -> dict[str, object]:
        """Return identity-bearing fields; observational duration is excluded."""

        return {
            "stage_name": self.stage_name,
            "status": self.status.value,
            "failure_code": self.failure_code.value if self.failure_code else None,
            "input_count": self.input_count,
            "output_count": self.output_count,
        }


@dataclass(frozen=True)
class EvidenceCacheIdentity:
    provider_identity: str
    source_identity: str
    payload_hash_sha256: str
    observed_at: datetime
    available_at: datetime
    decision_cutoff: datetime

    def __post_init__(self) -> None:
        if not self.provider_identity.strip() or not self.source_identity.strip():
            raise ValueError("cache evidence provider and source identities are required")
        if len(self.payload_hash_sha256) != 64:
            raise ValueError("cache evidence payload hash must be SHA-256")
        int(self.payload_hash_sha256, 16)
        for value in (self.observed_at, self.available_at, self.decision_cutoff):
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("cache evidence times must be timezone-aware")
        if self.observed_at > self.available_at or self.available_at > self.decision_cutoff:
            raise ValueError("cache evidence must be causal at its exact cutoff")


class EvidenceIdentityCache(Generic[T]):
    """Process-local cache that cannot reuse across evidence identity or cutoff."""

    def __init__(self) -> None:
        self._values: dict[EvidenceCacheIdentity, T] = {}

    def get_or_load(
        self,
        identity: EvidenceCacheIdentity,
        loader: Callable[[], T],
    ) -> tuple[T, bool]:
        if identity in self._values:
            return self._values[identity], True
        value = loader()
        self._values[identity] = value
        return value, False

    def invalidate(self, identity: EvidenceCacheIdentity) -> bool:
        return self._values.pop(identity, None) is not None


RiskFactory = Callable[
    [PreparedOpportunityPipeline],
    tuple[Mapping[str, ExecutionRiskEvidence], PipelineRiskPolicy],
]


@dataclass(frozen=True)
class OpportunityAdapterRequest:
    dataset: MarketDataset
    universe_snapshot: UniverseSnapshot
    risk_factory: RiskFactory
    registry: StrategyRegistry | None = None
    expectancy_bindings: tuple[StrategyExpectancyBinding, ...] = ()
    sector_by_symbol: Mapping[str, str] | None = None
    correlation_cluster_by_symbol: Mapping[str, str] | None = None
    catalyst_adapter: InjectedCatalystAdapter | None = None
    feature_config: FeatureConfig = DEFAULT_FEATURE_CONFIG
    discovery_config: DiscoveryConfig = DEFAULT_DISCOVERY_CONFIG
    ranking_config: RankingConfig = DEFAULT_RANKING_CONFIG
    gate_config: QualityGateConfig = DEFAULT_QUALITY_GATE_CONFIG


class CurrentOpportunityAdapter:
    def evaluate(self, request: OpportunityAdapterRequest) -> PipelineResult:
        return _evaluate_shared_rules(request)


class HistoricalOpportunityAdapter:
    def evaluate(self, request: OpportunityAdapterRequest) -> PipelineResult:
        return _evaluate_shared_rules(request)


def _evaluate_shared_rules(request: OpportunityAdapterRequest) -> PipelineResult:
    prepared = prepare_opportunity_pipeline(
        request.dataset,
        universe_snapshot=request.universe_snapshot,
        registry=request.registry,
        expectancy_bindings=request.expectancy_bindings,
        sector_by_symbol=request.sector_by_symbol,
        correlation_cluster_by_symbol=request.correlation_cluster_by_symbol,
        catalyst_adapter=request.catalyst_adapter,
        feature_config=request.feature_config,
        discovery_config=request.discovery_config,
        ranking_config=request.ranking_config,
    )
    risk_by_evaluation, risk_policy = request.risk_factory(prepared)
    return run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risk_by_evaluation,
        risk_policy=risk_policy,
        gate_config=request.gate_config,
    )


@dataclass(frozen=True)
class ProducerReceipt:
    pipeline_result: PipelineResult
    persistence_receipt: OpportunityPersistenceReceipt
    replay: PipelineResult
    telemetry: tuple[StageTelemetry, ...]
    research_only: bool = True
    broker_execution_enabled: bool = False
    promotion_authority: bool = False

    def __post_init__(self) -> None:
        if self.pipeline_result.to_json() != self.replay.to_json():
            raise ValueError("persisted opportunity replay is not byte-equivalent")
        if not self.research_only or self.broker_execution_enabled or self.promotion_authority:
            raise ValueError("opportunity producer must remain research-only")


class OpportunityResearchProducer:
    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled

    def run(
        self,
        request: OpportunityAdapterRequest,
        *,
        database_path: str | Path,
        recorded_at: datetime,
        adapter: CurrentOpportunityAdapter | HistoricalOpportunityAdapter | None = None,
    ) -> ProducerReceipt:
        if not self.enabled:
            raise RuntimeError(ProducerFailureCode.DISABLED.value)
        path = Path(database_path)
        if not path.is_absolute():
            raise ValueError(ProducerFailureCode.EXPLICIT_PATH_REQUIRED.value)
        resolved = path.resolve(strict=False)
        if os.path.normcase(str(resolved)) == os.path.normcase(str(ACTIVE_DATABASE)):
            raise ValueError(ProducerFailureCode.ACTIVE_PATH_FORBIDDEN.value)
        telemetry: list[StageTelemetry] = []
        started = time.perf_counter()
        result = (adapter or CurrentOpportunityAdapter()).evaluate(request)
        telemetry.append(
            StageTelemetry(
                stage_name="shared_opportunity_pipeline",
                status=StageStatus.COMPLETE,
                failure_code=None,
                input_count=request.universe_snapshot.requested_count,
                output_count=len(result.decisions),
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        started = time.perf_counter()
        store = OpportunityStore(resolved)
        persistence = store.append_run(result, recorded_at=recorded_at)
        replay = OpportunityStore(resolved, read_only=True).load_run(result.run_id)
        if replay is None:
            raise RuntimeError(ProducerFailureCode.PERSISTENCE_FAILED.value)
        telemetry.append(
            StageTelemetry(
                stage_name="immutable_append_and_read_only_replay",
                status=StageStatus.COMPLETE,
                failure_code=None,
                input_count=1,
                output_count=1,
                duration_ms=(time.perf_counter() - started) * 1000,
            )
        )
        return ProducerReceipt(
            pipeline_result=result,
            persistence_receipt=persistence,
            replay=replay,
            telemetry=tuple(telemetry),
        )


def run_opportunity_research(
    request: OpportunityAdapterRequest,
    *,
    database_path: str | Path,
    recorded_at: datetime,
    enabled: bool = False,
) -> ProducerReceipt:
    """Explicit operator surface; callers must opt in and name a non-active DB."""

    return OpportunityResearchProducer(enabled=enabled).run(
        request,
        database_path=database_path,
        recorded_at=recorded_at,
    )


__all__ = [
    "CurrentOpportunityAdapter",
    "EvidenceCacheIdentity",
    "EvidenceIdentityCache",
    "HistoricalOpportunityAdapter",
    "OpportunityAdapterRequest",
    "OpportunityResearchProducer",
    "ProducerFailureCode",
    "ProducerReceipt",
    "StageStatus",
    "StageTelemetry",
    "run_opportunity_research",
]
