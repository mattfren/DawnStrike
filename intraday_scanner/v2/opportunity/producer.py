"""Disabled-by-default mounted research producer for immutable opportunity runs."""

from __future__ import annotations

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
from intraday_scanner.storage.test_isolation import (
    is_active_database_path,
    is_explicit_absolute_database_path,
)
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
    INPUT_EVIDENCE_FAILED = "input_evidence_failed"
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
class ProducerFailureReceipt:
    failure_code: ProducerFailureCode
    failed_stage: str
    exception_type: str
    telemetry: tuple[StageTelemetry, ...]
    research_only: bool = True
    broker_execution_enabled: bool = False
    promotion_authority: bool = False

    def __post_init__(self) -> None:
        if not self.failed_stage.strip() or not self.exception_type.strip():
            raise ValueError("producer failure receipt requires bounded stage and type")
        if not self.telemetry or self.telemetry[-1].status is not StageStatus.FAILED:
            raise ValueError("producer failure receipt requires terminal failed telemetry")
        if self.telemetry[-1].failure_code is not self.failure_code:
            raise ValueError("producer failure receipt code does not match terminal telemetry")
        if not self.research_only or self.broker_execution_enabled or self.promotion_authority:
            raise ValueError("producer failure receipts must remain research-only")

    def deterministic_payload(self) -> dict[str, object]:
        return {
            "failure_code": self.failure_code.value,
            "failed_stage": self.failed_stage,
            "exception_type": self.exception_type,
            "telemetry": tuple(item.deterministic_payload() for item in self.telemetry),
            "research_only": self.research_only,
            "broker_execution_enabled": self.broker_execution_enabled,
            "promotion_authority": self.promotion_authority,
        }


class OpportunityProducerError(RuntimeError):
    """Typed failure that preserves the original cause and safe telemetry."""

    def __init__(self, receipt: ProducerFailureReceipt) -> None:
        super().__init__(receipt.failure_code.value)
        self.receipt = receipt


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
    def __init__(
        self,
        *,
        enabled: bool = False,
        evidence_cache: EvidenceIdentityCache[OpportunityAdapterRequest] | None = None,
    ) -> None:
        self.enabled = enabled
        self.evidence_cache = evidence_cache or EvidenceIdentityCache()

    def run_cached(
        self,
        identity: EvidenceCacheIdentity,
        loader: Callable[[], OpportunityAdapterRequest],
        *,
        database_path: str | Path,
        recorded_at: datetime,
        adapter: CurrentOpportunityAdapter | HistoricalOpportunityAdapter | None = None,
    ) -> ProducerReceipt:
        """Load exact retained evidence through the producer-owned identity cache."""

        self._require_enabled()
        started = time.perf_counter()
        try:
            request, cache_hit = self.evidence_cache.get_or_load(identity, loader)
        except Exception as exc:
            telemetry = (
                _failed_stage(
                    "retained_input_evidence",
                    ProducerFailureCode.INPUT_EVIDENCE_FAILED,
                    started,
                ),
            )
            raise _producer_error(
                ProducerFailureCode.INPUT_EVIDENCE_FAILED,
                "retained_input_evidence",
                exc,
                telemetry,
            ) from exc
        input_telemetry = StageTelemetry(
            stage_name="retained_input_evidence",
            status=StageStatus.CACHE_HIT if cache_hit else StageStatus.COMPLETE,
            failure_code=None,
            input_count=1,
            output_count=1,
            duration_ms=(time.perf_counter() - started) * 1000,
        )
        return self._run_loaded(
            request,
            database_path=database_path,
            recorded_at=recorded_at,
            adapter=adapter,
            initial_telemetry=(input_telemetry,),
        )

    def run(
        self,
        request: OpportunityAdapterRequest,
        *,
        database_path: str | Path,
        recorded_at: datetime,
        adapter: CurrentOpportunityAdapter | HistoricalOpportunityAdapter | None = None,
    ) -> ProducerReceipt:
        self._require_enabled()
        return self._run_loaded(
            request,
            database_path=database_path,
            recorded_at=recorded_at,
            adapter=adapter,
        )

    def _require_enabled(self) -> None:
        if self.enabled:
            return
        cause = RuntimeError(ProducerFailureCode.DISABLED.value)
        telemetry = (
            StageTelemetry(
                stage_name="research_only_boundary",
                status=StageStatus.FAILED,
                failure_code=ProducerFailureCode.DISABLED,
                input_count=0,
                output_count=0,
                duration_ms=0.0,
            ),
        )
        raise _producer_error(
            ProducerFailureCode.DISABLED,
            "research_only_boundary",
            cause,
            telemetry,
        ) from cause

    def _run_loaded(
        self,
        request: OpportunityAdapterRequest,
        *,
        database_path: str | Path,
        recorded_at: datetime,
        adapter: CurrentOpportunityAdapter | HistoricalOpportunityAdapter | None = None,
        initial_telemetry: tuple[StageTelemetry, ...] = (),
    ) -> ProducerReceipt:
        path = Path(database_path)
        if not is_explicit_absolute_database_path(database_path):
            cause = ValueError(ProducerFailureCode.EXPLICIT_PATH_REQUIRED.value)
            telemetry = (
                *initial_telemetry,
                StageTelemetry(
                    stage_name="research_database_boundary",
                    status=StageStatus.FAILED,
                    failure_code=ProducerFailureCode.EXPLICIT_PATH_REQUIRED,
                    input_count=1,
                    output_count=0,
                    duration_ms=0.0,
                ),
            )
            raise _producer_error(
                ProducerFailureCode.EXPLICIT_PATH_REQUIRED,
                "research_database_boundary",
                cause,
                telemetry,
            ) from cause
        if is_active_database_path(database_path):
            cause = ValueError(ProducerFailureCode.ACTIVE_PATH_FORBIDDEN.value)
            telemetry = (
                *initial_telemetry,
                StageTelemetry(
                    stage_name="research_database_boundary",
                    status=StageStatus.FAILED,
                    failure_code=ProducerFailureCode.ACTIVE_PATH_FORBIDDEN,
                    input_count=1,
                    output_count=0,
                    duration_ms=0.0,
                ),
            )
            raise _producer_error(
                ProducerFailureCode.ACTIVE_PATH_FORBIDDEN,
                "research_database_boundary",
                cause,
                telemetry,
            ) from cause
        resolved = path.resolve(strict=False)
        stage_telemetry = list(initial_telemetry)
        started = time.perf_counter()
        try:
            result = (adapter or CurrentOpportunityAdapter()).evaluate(request)
        except Exception as exc:
            stage_telemetry.append(
                _failed_stage(
                    "shared_opportunity_pipeline",
                    ProducerFailureCode.PIPELINE_FAILED,
                    started,
                    input_count=request.universe_snapshot.requested_count,
                )
            )
            raise _producer_error(
                ProducerFailureCode.PIPELINE_FAILED,
                "shared_opportunity_pipeline",
                exc,
                tuple(stage_telemetry),
            ) from exc
        stage_telemetry.append(
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
        try:
            store = OpportunityStore(resolved)
            persistence = store.append_run(result, recorded_at=recorded_at)
            replay = OpportunityStore(resolved, read_only=True).load_run(result.run_id)
            if replay is None:
                raise RuntimeError(ProducerFailureCode.PERSISTENCE_FAILED.value)
        except Exception as exc:
            stage_telemetry.append(
                _failed_stage(
                    "immutable_append_and_read_only_replay",
                    ProducerFailureCode.PERSISTENCE_FAILED,
                    started,
                    input_count=1,
                )
            )
            raise _producer_error(
                ProducerFailureCode.PERSISTENCE_FAILED,
                "immutable_append_and_read_only_replay",
                exc,
                tuple(stage_telemetry),
            ) from exc
        stage_telemetry.append(
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
            telemetry=tuple(stage_telemetry),
        )


def _failed_stage(
    stage_name: str,
    failure_code: ProducerFailureCode,
    started: float,
    *,
    input_count: int = 1,
) -> StageTelemetry:
    return StageTelemetry(
        stage_name=stage_name,
        status=StageStatus.FAILED,
        failure_code=failure_code,
        input_count=input_count,
        output_count=0,
        duration_ms=(time.perf_counter() - started) * 1000,
    )


def _producer_error(
    failure_code: ProducerFailureCode,
    failed_stage: str,
    cause: Exception,
    telemetry: tuple[StageTelemetry, ...],
) -> OpportunityProducerError:
    exception_type = type(cause).__name__
    if not exception_type.isidentifier():
        exception_type = "Exception"
    return OpportunityProducerError(
        ProducerFailureReceipt(
            failure_code=failure_code,
            failed_stage=failed_stage,
            exception_type=exception_type,
            telemetry=telemetry,
        )
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
    "OpportunityProducerError",
    "OpportunityResearchProducer",
    "ProducerFailureCode",
    "ProducerFailureReceipt",
    "ProducerReceipt",
    "StageStatus",
    "StageTelemetry",
    "run_opportunity_research",
]
