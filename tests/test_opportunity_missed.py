from __future__ import annotations

import ast
import hashlib
import subprocess
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner.storage.opportunity_outcome_inventory import (
    build_outcome_inventory,
    build_outcome_receipt,
    outcome_inventory_hash,
)
from intraday_scanner.v2.data_truth import (
    CorporateActionRecord,
    IntradayBar,
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
    IntradaySourceMetadata,
    MarketQuote,
    PriceAdjustmentBasis,
)
from intraday_scanner.v2.opportunity.capabilities import (
    CapabilityState,
    build_provider_capability_receipt,
)
from intraday_scanner.v2.opportunity.miss_contracts import (
    MissCategory,
    MissSessionDisposition,
    OpportunityDisposition,
    QualificationClaimKind,
    QualificationExecutionStatus,
    QualificationHorizonKind,
    QualificationMemberStatus,
    QualificationMetric,
    QualificationPathStatus,
    QualificationSourceAuthorityClaim,
    QualificationStatus,
    QualificationValueStatus,
    SessionRunInventoryStatus,
    SurfacingState,
    build_miss_qualification_policy,
    build_session_qualification_horizon,
)
from intraday_scanner.v2.opportunity.miss_qualification import (
    QualificationAssessment,
    QualificationBatch,
    qualify_session_opportunities,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import (
    MissedOpportunityRecord,
    MissReconciliationBatch,
    OpportunityRunProjection,
    _classify_category,
    _first_rank_at,
    _first_stage_category,
    _gate_category,
    _opportunity_disposition,
    _project_run,
    _regime_category,
    _rich_feature_category,
    _summarize_record_dispositions,
    _undiscovered_category,
    _unresolved_qualification_disposition,
    reconcile_missed_opportunities,
)
from intraday_scanner.v2.opportunity.miss_replay import (
    SessionReplay,
    SessionRunInventoryEvidence,
    build_session_replay,
    build_session_run_inventory,
)
from intraday_scanner.v2.opportunity.miss_sources import (
    HindsightQualificationSource,
    QualificationExecutionEvidence,
    QualificationMemberEvidence,
    QualificationSourceArtifact,
    QualificationSourceAuthority,
    RetrospectiveRegimeEvidence,
    _regime_measurement_hash,
    _regime_observed_at,
    build_qualification_source_scope_receipt,
)
from intraday_scanner.v2.opportunity.models import (
    EvaluationStatus,
    EvidenceKind,
    StrategyDirection,
    StrategyValidationState,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_persistence import CurrentOutcomeReplay
from intraday_scanner.v2.opportunity.outcomes import (
    OutcomeHorizonKind,
    build_outcome_bar_evidence,
    build_outcome_horizon,
    build_outcome_label_policy,
    build_outcome_observation_dataset,
    build_outcome_observation_series,
    label_pipeline_outcomes,
)
from intraday_scanner.v2.opportunity.pipeline import (
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.regimes import (
    classify_market_regime,
    classify_security_regime,
)
from intraday_scanner.v2.opportunity.registry import (
    StrategyRegistry,
    build_default_registry,
)
from intraday_scanner.v2.opportunity.risk import QuoteEvidenceScope
from intraday_scanner.v2.opportunity.universe import (
    SafetyStatus,
    SecurityType,
)
from tests.test_opportunity_outcomes import (
    _batch as _outcome_batch,
)
from tests.test_opportunity_outcomes import (
    _receipt as _run_persistence_receipt,
)
from tests.test_opportunity_outcomes import (
    _source_series as _outcome_source_series,
)
from tests.test_opportunity_pipeline import (
    _candidate,
    _evaluate,
    _execution_risk_for,
    _finalized_two_strategy_pipeline,
    _pipeline_risk_policy,
    _pipeline_universe,
    _snapshot,
    _two_candidate_dataset,
)
from tests.test_opportunity_universe_risk import (
    GATE_CONFIG,
    _empirical_evaluation,
    _execution_risk,
    _gate_one,
)

UTC = timezone.utc
SESSION_ID = "XNYS-2026-08-11"
SESSION_OPEN = datetime(2026, 8, 11, 14, 30, tzinfo=UTC)
SESSION_CLOSE = SESSION_OPEN + timedelta(minutes=5)
FETCHED_AT = SESSION_CLOSE + timedelta(minutes=1)


@lru_cache(maxsize=1)
def _current_outcome_replay() -> CurrentOutcomeReplay:
    return _stored_replay_for_batch(_outcome_batch())


def _stored_replay_for_batch(batch) -> CurrentOutcomeReplay:
    inventory = build_outcome_inventory(
        batch,
        predecessor_receipt=None,
        predecessor_by_pair={},
    )
    receipt = build_outcome_receipt(
        batch,
        persisted_at=batch.recorded_at + timedelta(seconds=1),
        inventory=inventory,
        inventory_hash=outcome_inventory_hash(inventory),
        predecessor=None,
    )
    values = {
        "pipeline_result": batch.pipeline_result,
        "run_persistence_receipt": batch.persistence_receipt,
        "outcome_persistence_receipt": receipt,
        "outcome_batch": batch,
        "full_chain": (receipt,),
        "research_only": True,
        "schema_version": "v2.opportunity.current_outcome_replay.v1",
    }
    return CurrentOutcomeReplay(
        replay_id=stable_identity("current-opportunity-outcome-replay", values),
        **values,
    )


def _label_pipeline_result(result):
    decision_utc = result.decision_at.astimezone(UTC)
    symbols = tuple(sorted({item.symbol for item in result.evaluations}))
    series = tuple(_outcome_source_series(result, symbol) for symbol in symbols)
    source_session_ids = {item.exchange_session_id for item in series}
    if len(source_session_ids) != 1:
        raise AssertionError("fixture outcome series must use one exchange session")
    horizon = build_outcome_horizon(
        decision_at=result.decision_at,
        exchange_session_id=next(iter(source_session_ids)),
        session_open_at=decision_utc - timedelta(minutes=4),
        session_close_at=decision_utc + timedelta(hours=6, minutes=26),
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=301,
    )
    dataset = build_outcome_observation_dataset(
        decision_at=result.decision_at,
        frozen_at=horizon.end_at + timedelta(seconds=1),
        series=series,
    )
    return label_pipeline_outcomes(
        pipeline_result=result,
        persistence_receipt=_run_persistence_receipt(result, decision_utc),
        source_dataset=dataset,
        policy=build_outcome_label_policy(
            policy_version="wp004-multi-run-fixture-v1",
            expected_bar_interval_seconds=60,
        ),
        horizons=(horizon,),
        recorded_at=dataset.frozen_at,
    )


def _session_replay(
    *,
    current_outcome_replays: tuple[CurrentOutcomeReplay, ...] | None = None,
    authoritative: bool = True,
    scope_complete: bool = True,
    capability_state: CapabilityState = CapabilityState.AVAILABLE,
    limitations: tuple[str, ...] = (),
) -> SessionReplay:
    currents = current_outcome_replays or (_current_outcome_replay(),)
    query_start = min(
        item.pipeline_result.decision_at.astimezone(UTC) for item in currents
    ) - timedelta(seconds=1)
    fetched_at = max(
        FETCHED_AT,
        *(item.outcome_persistence_receipt.persisted_at for item in currents),
    )
    inventory = build_session_run_inventory(
        exchange_session_id=SESSION_ID,
        session_open_at=SESSION_OPEN,
        session_close_at=SESSION_CLOSE,
        current_outcome_replays=currents,
        source_identity="fixture_session_run_inventory",
        source_version="v1",
        method="stored_current_replay_query",
        capability_state=capability_state,
        authoritative=authoritative,
        scope_complete=scope_complete,
        query_started_at=query_start,
        query_ended_at=SESSION_CLOSE,
        observed_through_at=SESSION_CLOSE,
        fetched_at=fetched_at,
        limitations=limitations,
    )
    return build_session_replay(
        inventory,
        current_outcome_replays=currents,
    )


def _qualified_batch() -> QualificationBatch:
    return qualify_session_opportunities(
        _source(
            bars=(
                ("100", "100.5", "99.5", "100.1"),
                ("100.2", "102.1", "100", "102"),
                ("102", "102.2", "101.8", "102"),
                ("102", "102.2", "101.8", "102"),
                ("102", "102.2", "101.8", "102"),
            )
        ),
        policy=_policy(),
        horizons=(_horizon(),),
    )


def _short_qualified_batch() -> QualificationBatch:
    return qualify_session_opportunities(
        _source(
            bars=(
                ("100", "100.5", "99.5", "99.9"),
                ("99.8", "100", "97.9", "98"),
                ("98", "98.2", "97.8", "98"),
                ("98", "98.2", "97.8", "98"),
                ("98", "98.2", "97.8", "98"),
            )
        ),
        policy=_policy(),
        horizons=(_horizon(),),
    )


@lru_cache(maxsize=1)
def _short_candidate_pipeline_result():
    dataset = _two_candidate_dataset()
    closes = (104.0, 101.0, 100.5, 100.4, 100.3, 100.2, 100.1, 96.0)
    bars = {
        symbol: tuple(
            replace(
                bar,
                open=close + 0.1,
                high=close + 0.5,
                low=close - 0.1,
                close=close,
            )
            for bar, close in zip(values, closes, strict=True)
        )
        for symbol, values in dataset.bars_by_symbol.items()
    }
    changed = replace(dataset, bars_by_symbol=bars)
    prepared = prepare_opportunity_pipeline(
        changed,
        universe_snapshot=_pipeline_universe(
            changed,
            requested_symbols=("ABC", "DEF"),
        ),
        registry=StrategyRegistry(()),
    )
    return run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )


@lru_cache(maxsize=1)
def _watch_pipeline_result():
    dataset = _two_candidate_dataset()
    bars = dict(dataset.bars_by_symbol)
    bars["GHI"] = tuple(replace(item, symbol="GHI") for item in bars["ABC"])
    changed = replace(dataset, bars_by_symbol=bars)
    template = _finalized_two_strategy_pipeline()[0]
    default = build_default_registry()
    prepared = prepare_opportunity_pipeline(
        changed,
        universe_snapshot=_pipeline_universe(
            changed,
            requested_symbols=("ABC", "DEF", "GHI"),
        ),
        registry=StrategyRegistry((default.get("DS-MOM-001"), default.get("DS-OF-001"))),
        expectancy_bindings=template.expectancy_bindings,
        sector_by_symbol={
            "ABC": "technology",
            "DEF": "industrials",
            "GHI": "healthcare",
        },
        correlation_cluster_by_symbol={
            "ABC": "cluster-a",
            "DEF": "cluster-b",
            "GHI": "cluster-c",
        },
    )
    risks = {
        item.evaluation_id: _execution_risk_for(item)
        for item in prepared.evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    }
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risks,
        risk_policy=_pipeline_risk_policy(),
    )
    assert any(item.decision is TradeDecisionValue.WATCH for item in result.decisions)
    return result


@lru_cache(maxsize=1)
def _watch_current_outcome_replay() -> CurrentOutcomeReplay:
    return _stored_replay_for_batch(_label_pipeline_result(_watch_pipeline_result()))


def _retrospective_regime_evidence(result) -> RetrospectiveRegimeEvidence:
    def utc_snapshot(symbol: str):
        value = _snapshot(symbol=symbol)
        observed_at = value.decision_at.astimezone(UTC)
        return replace(
            value,
            decision_at=observed_at,
            latest_bar_at=observed_at,
            numerical=tuple(
                replace(item, observed_at=item.observed_at.astimezone(UTC))
                for item in value.numerical
            ),
            categorical=tuple(
                replace(item, observed_at=item.observed_at.astimezone(UTC))
                for item in value.categorical
            ),
        )

    benchmark = utc_snapshot("SPY")
    security = utc_snapshot("ABC")
    market_regime = classify_market_regime(benchmark)
    security_regime = classify_security_regime(security)
    template = SimpleNamespace(
        benchmark_snapshot=benchmark,
        security_snapshot=security,
        market_regime=market_regime,
        security_regime=security_regime,
        method="independent_retrospective_snapshot_classification",
    )
    artifact = QualificationSourceArtifact(
        artifact_id="retrospective-regime-artifact",
        content_hash_sha256=_regime_measurement_hash(template),
        source_identity="retrospective-regime-fixture",
        fetched_at=FETCHED_AT,
    )
    values = {
        "run_id": result.run_id,
        "run_content_hash": result.content_hash(),
        "symbol": "ABC",
        "benchmark_symbol": "SPY",
        "decision_at": result.decision_at.astimezone(UTC),
        "benchmark_snapshot": benchmark,
        "security_snapshot": security,
        "market_regime": market_regime,
        "security_regime": security_regime,
        "observed_at": _regime_observed_at(template),
        "fetched_at": FETCHED_AT,
        "source_artifact": artifact,
        "method": "independent_retrospective_snapshot_classification",
        "limitations": ("bounded_regime_fixture",),
        "schema_version": "v2.opportunity.retrospective_regime_evidence.v1",
    }
    return RetrospectiveRegimeEvidence(
        regime_evidence_id=stable_identity("retrospective-regime", values),
        **values,
    )


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _policy():
    return build_miss_qualification_policy(
        policy_version="wp004-a-v1",
        expected_bar_interval_seconds=60,
        entry_anchor_offset_seconds=0,
        stop_distance_fraction=Decimal("0.01"),
        minimum_gross_reward_risk=Decimal("2"),
        minimum_after_cost_reward_risk=Decimal("1.5"),
        minimum_executable_quantity_shares=10,
    )


def _horizon(*, elapsed_seconds: int = 300):
    return build_session_qualification_horizon(
        exchange_session_id=SESSION_ID,
        session_open_at=SESSION_OPEN,
        session_close_at=SESSION_CLOSE,
        entry_anchor_offset_seconds=0,
        kind=QualificationHorizonKind.ELAPSED_FROM_ENTRY,
        elapsed_seconds=elapsed_seconds,
    )


def _metadata(symbol: str, *, basis_suffix: str = "base") -> IntradaySourceMetadata:
    return IntradaySourceMetadata(
        provider="fixture-provider",
        feed="fixture-bars",
        entitlement="fixture-research",
        exchange_session_id=SESSION_ID,
        request_start=SESSION_OPEN,
        request_end=SESSION_CLOSE,
        fetched_at=FETCHED_AT,
        code_sha=_hash("fixture-code"),
        raw_artifact_hash_sha256=_hash(f"raw-{symbol}-{basis_suffix}"),
        normalized_artifact_hash_sha256=_hash(f"normalized-{symbol}-{basis_suffix}"),
        retention_status="retained_fixture",
    )


def _series(
    *,
    bars: tuple[tuple[str, str, str, str], ...],
    coverage_status: IntradayCoverageStatus = IntradayCoverageStatus.COMPLETE,
    missing_intervals: tuple[tuple[datetime, datetime], ...] = (),
    bases: tuple[PriceAdjustmentBasis, ...] | None = None,
    corporate_actions: tuple[CorporateActionRecord, ...] = (),
):
    metadata = _metadata("ABC")
    observations = []
    for index, (open_value, high_value, low_value, close_value) in enumerate(bars):
        start = SESSION_OPEN + timedelta(minutes=index)
        end = start + timedelta(minutes=1)
        if any(start == gap_start and end == gap_end for gap_start, gap_end in missing_intervals):
            continue
        basis = bases[index] if bases is not None else PriceAdjustmentBasis.UNADJUSTED
        observations.append(
            build_outcome_bar_evidence(
                bar=IntradayBar(
                    symbol="ABC",
                    exchange_session_id=SESSION_ID,
                    timestamp=end,
                    open_price=Decimal(open_value),
                    high_price=Decimal(high_value),
                    low_price=Decimal(low_value),
                    close_price=Decimal(close_value),
                    volume=1000 + index,
                    vwap=Decimal(close_value),
                    price_adjustment_basis=basis,
                    source_metadata=metadata,
                    trade_count=100 + index,
                ),
                interval_start_at=start,
                interval_end_at=end,
            )
        )
    coverage = IntradayCoverageReceipt(
        coverage_receipt_id="coverage-abc",
        provider=metadata.provider,
        feed=metadata.feed,
        entitlement=metadata.entitlement,
        symbol="ABC",
        market_date="2026-08-11",
        exchange_session_id=SESSION_ID,
        request_start=SESSION_OPEN,
        request_end=SESSION_CLOSE,
        status=coverage_status,
        source_metadata=metadata,
        observed_start=observations[0].interval_start_at if observations else None,
        observed_end=observations[-1].interval_end_at if observations else None,
        missing_intervals=missing_intervals,
        artifact_manifest_ids=("manifest-abc",),
        reason="fixture_coverage",
        created_at=FETCHED_AT,
    )
    return build_outcome_observation_series(
        symbol="ABC",
        exchange_session_id=SESSION_ID,
        decision_at=SESSION_OPEN - timedelta(microseconds=1),
        requested_through_at=SESSION_CLOSE,
        first_expected_interval_start_at=SESSION_OPEN,
        coverage_receipt=coverage,
        observations=tuple(observations),
        corporate_actions=corporate_actions,
        source_identity="fixture_outcome_source",
        method="retained_session_minute_bars",
    )


def _authority_artifact() -> QualificationSourceArtifact:
    return QualificationSourceArtifact(
        artifact_id="fixture-authority-artifact",
        content_hash_sha256=_hash("fixture-authority-body"),
        source_identity="fixture-authority-source",
        fetched_at=FETCHED_AT,
    )


def _authority(artifact: QualificationSourceArtifact) -> QualificationSourceAuthority:
    values = {
        "authority_identity": "fixture-market-roster",
        "authority_version": "v1",
        "capability_state": CapabilityState.AVAILABLE,
        "claim": QualificationSourceAuthorityClaim.MARKET_COMPLETE,
        "membership_as_of_at": SESSION_OPEN - timedelta(days=1),
        "cohort_symbols": ("ABC",),
        "artifact_ids": (artifact.artifact_id,),
        "artifact_hashes": (artifact.content_hash_sha256,),
        "limitations": (),
        "schema_version": "v2.opportunity.qualification_source_authority.v1",
    }
    return QualificationSourceAuthority(
        authority_id=stable_identity("qualification-source-authority", values),
        **values,
    )


def _member(artifact: QualificationSourceArtifact) -> QualificationMemberEvidence:
    values = {
        "symbol": "ABC",
        "security_type": SecurityType.COMMON_STOCK,
        "status": QualificationMemberStatus.ELIGIBLE,
        "exchange_session_id": SESSION_ID,
        "membership_as_of_at": SESSION_OPEN - timedelta(days=1),
        "effective_from_at": SESSION_OPEN - timedelta(days=100),
        "effective_through_at": None,
        "observed_at": SESSION_OPEN - timedelta(days=1),
        "fetched_at": FETCHED_AT,
        "source_identity": artifact.source_identity,
        "source_artifact_ids": (artifact.artifact_id,),
        "source_artifact_hashes": (artifact.content_hash_sha256,),
        "halt_status": SafetyStatus.CLEAR,
        "corporate_action_status": SafetyStatus.CLEAR,
        "reasons": (),
        "limitations": (),
        "schema_version": "v2.opportunity.qualification_member_evidence.v1",
    }
    return QualificationMemberEvidence(
        member_id=stable_identity("qualification-member", values),
        **values,
    )


def _execution(reference) -> QualificationExecutionEvidence:
    metadata = replace(
        reference.bar.source_metadata,
        feed="fixture-nbbo",
        raw_artifact_hash_sha256=_hash("quote-raw"),
        normalized_artifact_hash_sha256=_hash("quote-normalized"),
    )
    quote = MarketQuote(
        symbol="ABC",
        exchange_session_id=SESSION_ID,
        timestamp=SESSION_OPEN + timedelta(seconds=30),
        feed=metadata.feed,
        bid_price=Decimal("99.95"),
        ask_price=Decimal("100.05"),
        bid_size=100,
        ask_size=100,
        bid_exchange="XNYS",
        ask_exchange="XNAS",
        price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
        source_metadata=metadata,
    )
    values = {
        "symbol": "ABC",
        "direction": StrategyDirection.LONG,
        "exchange_session_id": SESSION_ID,
        "reference_observation": reference,
        "quote": quote,
        "quote_scope": QuoteEvidenceScope.NBBO,
        "spread_bps": Decimal("10"),
        "entry_slippage_bps": Decimal("1"),
        "exit_slippage_bps": Decimal("1"),
        "round_trip_fee_per_share": Decimal("0.01"),
        "executable_quantity_shares": 100,
        "status": QualificationExecutionStatus.AVAILABLE,
        "evidence_kind": EvidenceKind.EMPIRICAL,
        "observed_at": quote.timestamp,
        "source_identity": "fixture-provider:fixture-nbbo:fixture-research",
        "source_artifact_id": stable_identity("qualification-quote-source", metadata),
        "source_artifact_hash_sha256": metadata.normalized_artifact_hash_sha256,
        "method": "retained_nbbo_at_reference",
        "reason": None,
        "limitations": (),
        "schema_version": "v2.opportunity.qualification_execution_evidence.v1",
    }
    return QualificationExecutionEvidence(
        execution_evidence_id=stable_identity("qualification-execution", values),
        **values,
    )


def _source(
    *,
    bars: tuple[tuple[str, str, str, str], ...],
    coverage_status: IntradayCoverageStatus = IntradayCoverageStatus.COMPLETE,
    missing_intervals: tuple[tuple[datetime, datetime], ...] = (),
    bases: tuple[PriceAdjustmentBasis, ...] | None = None,
    member_status: QualificationMemberStatus = QualificationMemberStatus.ELIGIBLE,
    halt_status: SafetyStatus = SafetyStatus.CLEAR,
    corporate_action_status: SafetyStatus = SafetyStatus.CLEAR,
    with_execution: bool = False,
    corporate_actions: tuple[CorporateActionRecord, ...] = (),
) -> HindsightQualificationSource:
    series = _series(
        bars=bars,
        coverage_status=coverage_status,
        missing_intervals=missing_intervals,
        bases=bases,
        corporate_actions=corporate_actions,
    )
    dataset = build_outcome_observation_dataset(
        decision_at=SESSION_OPEN - timedelta(microseconds=1),
        frozen_at=FETCHED_AT,
        series=(series,),
    )
    artifact = _authority_artifact()
    authority = _authority(artifact)
    capability = build_provider_capability_receipt(
        provider="fixture-provider",
        feed="fixture-bars",
        entitlement_identity="fixture-research",
        decision_at=FETCHED_AT,
        observed_at=FETCHED_AT,
        bars=CapabilityState.AVAILABLE,
        trades=CapabilityState.UNKNOWN,
        quotes=CapabilityState.AVAILABLE,
        consolidated_nbbo=CapabilityState.AVAILABLE,
        aggressor_classification=CapabilityState.UNKNOWN,
        corporate_actions=CapabilityState.AVAILABLE,
        halts=CapabilityState.AVAILABLE,
        historical_coverage=CapabilityState.AVAILABLE,
        coverage_start=SESSION_OPEN,
        coverage_end=SESSION_CLOSE,
        source_identity=artifact.source_identity,
        method="retained_fixture_capability",
        limitations=(),
    )
    scope = build_qualification_source_scope_receipt(
        exchange_session_id=SESSION_ID,
        session_open_at=SESSION_OPEN,
        session_close_at=SESSION_CLOSE,
        membership_as_of_at=authority.membership_as_of_at,
        query_started_at=SESSION_CLOSE,
        query_ended_at=FETCHED_AT,
        observed_through_at=SESSION_CLOSE,
        fetched_at=FETCHED_AT,
        authority=authority,
        requested_symbols=("ABC",),
        provider_receipts=(capability,),
        source_artifacts=(artifact,),
    )
    member_values = {
        name: value for name, value in _member(artifact).__dict__.items() if name != "member_id"
    }
    member_values.update(
        {
            "status": member_status,
            "halt_status": halt_status,
            "corporate_action_status": corporate_action_status,
            "reasons": (
                () if member_status is QualificationMemberStatus.ELIGIBLE else ("fixture",)
            ),
        }
    )
    member = QualificationMemberEvidence(
        member_id=stable_identity("qualification-member", member_values),
        **member_values,
    )
    execution = (_execution(series.observations[0]),) if with_execution else ()
    expected_hashes = tuple(
        sorted(
            {
                artifact.content_hash_sha256,
                *dataset.source_artifact_hashes,
                *(
                    item.source_artifact_hash_sha256
                    for item in execution
                    if item.source_artifact_hash_sha256 is not None
                ),
            }
        )
    )
    values = {
        "scope_receipt": scope,
        "members": (member,),
        "observation_dataset": dataset,
        "missing_series_symbols": (),
        "execution_evidence": execution,
        "retrospective_regime_evidence": (),
        "frozen_at": FETCHED_AT,
        "recorded_at": FETCHED_AT,
        "source_artifact_hashes": expected_hashes,
        "limitations": (),
        "research_only": True,
        "schema_version": "v2.opportunity.hindsight_qualification_source.v1",
    }
    return HindsightQualificationSource(
        source_id=stable_identity("hindsight-qualification-source", values),
        **values,
    )


def _assessment(batch: QualificationBatch, direction: StrategyDirection):
    return next(item for item in batch.assessments if item.direction is direction)


def _corporate_action(effective_at: datetime) -> CorporateActionRecord:
    metadata = _metadata("ABC")
    return CorporateActionRecord(
        symbol="ABC",
        mapped_symbol="ABC",
        action_type="split",
        effective_at=effective_at,
        exchange_session_id=SESSION_ID,
        price_adjustment_basis=PriceAdjustmentBasis.UNADJUSTED,
        source_metadata=metadata,
        details={"ratio": "2_to_1"},
    )


@pytest.mark.parametrize(
    ("direction", "bars", "expected"),
    (
        (
            StrategyDirection.LONG,
            (
                ("100", "100.5", "99.5", "100.1"),
                ("100.2", "102.1", "100", "102"),
                ("102", "102.2", "101.8", "102"),
                ("102", "102.2", "101.8", "102"),
                ("102", "102.2", "101.8", "102"),
            ),
            QualificationPathStatus.TARGET_FIRST,
        ),
        (
            StrategyDirection.SHORT,
            (
                ("100", "100.5", "99.5", "99.9"),
                ("99.8", "100", "97.9", "98"),
                ("98", "98.2", "97.8", "98"),
                ("98", "98.2", "97.8", "98"),
                ("98", "98.2", "97.8", "98"),
            ),
            QualificationPathStatus.TARGET_FIRST,
        ),
        (
            StrategyDirection.LONG,
            (
                ("100", "100.5", "99.5", "100"),
                ("100", "100.2", "98.9", "99"),
                ("99", "99.2", "98.8", "99"),
                ("99", "99.2", "98.8", "99"),
                ("99", "99.2", "98.8", "99"),
            ),
            QualificationPathStatus.STOP_FIRST,
        ),
    ),
)
def test_qualification_resolves_directional_paths(direction, bars, expected) -> None:
    batch = qualify_session_opportunities(
        _source(bars=bars), policy=_policy(), horizons=(_horizon(),)
    )
    assessment = _assessment(batch, direction)
    assert assessment.path_status is expected
    assert assessment.status is (
        QualificationStatus.QUALIFIED
        if expected is QualificationPathStatus.TARGET_FIRST
        else QualificationStatus.NOT_QUALIFIED
    )


@pytest.mark.parametrize(
    ("direction", "first_bar"),
    (
        (StrategyDirection.LONG, ("100", "102.1", "99.5", "101")),
        (StrategyDirection.LONG, ("100", "100.5", "98.9", "99")),
        (StrategyDirection.SHORT, ("100", "100.5", "97.9", "99")),
        (StrategyDirection.SHORT, ("100", "101.1", "99.5", "101")),
    ),
)
def test_any_entry_bar_level_touch_is_censored(direction, first_bar) -> None:
    quiet = (("100", "100.5", "99.5", "100"),) * 4
    batch = qualify_session_opportunities(
        _source(bars=(first_bar, *quiet)),
        policy=_policy(),
        horizons=(_horizon(),),
    )
    assessment = _assessment(batch, direction)
    assert assessment.status is QualificationStatus.CENSORED
    assert assessment.path_status is QualificationPathStatus.ENTRY_BAR_AMBIGUOUS
    assert assessment.claim_kind is QualificationClaimKind.NONE


def test_execution_formula_separates_executable_from_price_proxy() -> None:
    bars = (
        ("100", "100.5", "99.5", "100.1"),
        ("100.2", "102.1", "100", "102"),
        ("102", "102.2", "101.8", "102"),
        ("102", "102.2", "101.8", "102"),
        ("102", "102.2", "101.8", "102"),
    )
    proxy = _assessment(
        qualify_session_opportunities(_source(bars=bars), policy=_policy(), horizons=(_horizon(),)),
        StrategyDirection.LONG,
    )
    executable = _assessment(
        qualify_session_opportunities(
            _source(bars=bars, with_execution=True),
            policy=_policy(),
            horizons=(_horizon(),),
        ),
        StrategyDirection.LONG,
    )
    assert proxy.claim_kind is QualificationClaimKind.PRICE_MOVE_PROXY
    assert executable.claim_kind is QualificationClaimKind.EXECUTABLE_TRADE
    metrics = {item.metric: item for item in executable.metrics}
    assert metrics[QualificationMetric.PER_SHARE_COST].value == Decimal("0.13")
    assert metrics[QualificationMetric.AFTER_COST_REWARD_RISK].value == (
        Decimal("2") - Decimal("0.13")
    ) / (Decimal("1") + Decimal("0.13"))
    required = metrics[QualificationMetric.REQUIRED_MOVE_FRACTION]
    assert required.value == Decimal("0.02")
    assert required.status is QualificationValueStatus.DERIVED
    assert required.source_ids == (executable.source_observations[0].observation_id,)


def test_known_halt_gap_and_unknown_safety_fail_closed() -> None:
    bars = (("100", "100.5", "99.5", "100"),) * 5
    gap = ((SESSION_OPEN + timedelta(minutes=1), SESSION_OPEN + timedelta(minutes=2)),)
    halted = _assessment(
        qualify_session_opportunities(
            _source(
                bars=bars,
                coverage_status=IntradayCoverageStatus.KNOWN_HALT_GAPS,
                missing_intervals=gap,
            ),
            policy=_policy(),
            horizons=(_horizon(),),
        ),
        StrategyDirection.LONG,
    )
    unknown = _assessment(
        qualify_session_opportunities(
            _source(bars=bars, halt_status=SafetyStatus.UNKNOWN),
            policy=_policy(),
            horizons=(_horizon(),),
        ),
        StrategyDirection.LONG,
    )
    assert halted.path_status is QualificationPathStatus.HALT_CENSORED
    assert halted.status is QualificationStatus.CENSORED
    assert unknown.path_status is QualificationPathStatus.UNSUPPORTED_EVIDENCE
    assert unknown.status is QualificationStatus.UNAVAILABLE


def test_horizon_end_action_censors_but_later_action_preserves_terminal_path() -> None:
    quiet = (("100", "100.5", "99.5", "100"),) * 5
    action = _corporate_action(SESSION_CLOSE)
    censored = _assessment(
        qualify_session_opportunities(
            _source(bars=quiet, corporate_actions=(action,)),
            policy=_policy(),
            horizons=(_horizon(),),
        ),
        StrategyDirection.LONG,
    )
    terminal_bars = (
        ("100", "100.5", "99.5", "100"),
        ("100", "102.1", "99.8", "102"),
        ("102", "102.2", "101.8", "102"),
        ("102", "102.2", "101.8", "102"),
        ("102", "102.2", "101.8", "102"),
    )
    terminal = _assessment(
        qualify_session_opportunities(
            _source(bars=terminal_bars, corporate_actions=(action,)),
            policy=_policy(),
            horizons=(_horizon(),),
        ),
        StrategyDirection.LONG,
    )
    assert censored.path_status is QualificationPathStatus.CORPORATE_ACTION_CENSORED
    assert censored.status is QualificationStatus.CENSORED
    assert terminal.path_status is QualificationPathStatus.TARGET_FIRST
    assert terminal.status is QualificationStatus.QUALIFIED


@pytest.mark.parametrize(
    "bases",
    (
        (PriceAdjustmentBasis.UNKNOWN,) * 5,
        (
            PriceAdjustmentBasis.UNADJUSTED,
            PriceAdjustmentBasis.SPLIT_ADJUSTED,
            PriceAdjustmentBasis.SPLIT_ADJUSTED,
            PriceAdjustmentBasis.SPLIT_ADJUSTED,
            PriceAdjustmentBasis.SPLIT_ADJUSTED,
        ),
    ),
)
def test_unknown_or_mixed_adjustment_bases_are_unavailable(bases) -> None:
    bars = (("100", "100.5", "99.5", "100"),) * 5
    assessment = _assessment(
        qualify_session_opportunities(
            _source(bars=bars, bases=bases),
            policy=_policy(),
            horizons=(_horizon(),),
        ),
        StrategyDirection.LONG,
    )
    assert assessment.status is QualificationStatus.UNAVAILABLE
    assert assessment.path_status is QualificationPathStatus.UNSUPPORTED_EVIDENCE
    assert all(item.value is None for item in assessment.metrics)


def test_assessment_and_batch_direct_contracts_recompute_exactly() -> None:
    bars = (
        ("100", "100.5", "99.5", "100"),
        ("100", "102.1", "99.8", "102"),
        ("102", "102.2", "101.8", "102"),
        ("102", "102.2", "101.8", "102"),
        ("102", "102.2", "101.8", "102"),
    )
    batch = qualify_session_opportunities(
        _source(bars=bars), policy=_policy(), horizons=(_horizon(),)
    )
    assert QualificationBatch.from_json(batch.to_json()) == batch
    assessment = _assessment(batch, StrategyDirection.LONG)
    assert QualificationAssessment.from_json(assessment.to_json()) == assessment
    foreign_values = {
        name: value for name, value in assessment.member.__dict__.items() if name != "member_id"
    }
    foreign_values["symbol"] = "XYZ"
    foreign_member = QualificationMemberEvidence(
        member_id=stable_identity("qualification-member", foreign_values),
        **foreign_values,
    )
    with pytest.raises(ValueError, match="exact source member"):
        replace(assessment, member=foreign_member)
    with pytest.raises(ValueError, match="recorded_at must equal"):
        replace(batch, recorded_at=batch.recorded_at + timedelta(seconds=1))
    with pytest.raises(ValueError, match="limitations are not canonical"):
        replace(batch, limitations=("caller_authored",))


def test_horizon_alignment_is_exact_and_requires_full_first_interval() -> None:
    source = _source(bars=(("100", "100.5", "99.5", "100"),) * 5)
    misaligned = build_session_qualification_horizon(
        exchange_session_id=SESSION_ID,
        session_open_at=SESSION_OPEN,
        session_close_at=SESSION_CLOSE,
        entry_anchor_offset_seconds=0,
        kind=QualificationHorizonKind.ELAPSED_FROM_ENTRY,
        elapsed_seconds=61,
    )
    with pytest.raises(ValueError, match="align to expected bar intervals"):
        qualify_session_opportunities(source, policy=_policy(), horizons=(misaligned,))
    too_short = build_session_qualification_horizon(
        exchange_session_id=SESSION_ID,
        session_open_at=SESSION_OPEN,
        session_close_at=SESSION_CLOSE,
        entry_anchor_offset_seconds=0,
        kind=QualificationHorizonKind.ELAPSED_FROM_ENTRY,
        elapsed_seconds=30,
    )
    with pytest.raises(ValueError, match="complete entry interval"):
        qualify_session_opportunities(source, policy=_policy(), horizons=(too_short,))


def test_complete_scope_and_member_artifacts_require_exact_source_truth() -> None:
    artifact = _authority_artifact()
    with pytest.raises(ValueError, match="requires bound source artifacts"):
        values = {
            "authority_identity": "fixture-market-roster",
            "authority_version": "v1",
            "capability_state": CapabilityState.AVAILABLE,
            "claim": QualificationSourceAuthorityClaim.MARKET_COMPLETE,
            "membership_as_of_at": SESSION_OPEN - timedelta(days=1),
            "cohort_symbols": ("ABC",),
            "artifact_ids": (),
            "artifact_hashes": (),
            "limitations": (),
            "schema_version": "v2.opportunity.qualification_source_authority.v1",
        }
        QualificationSourceAuthority(
            authority_id=stable_identity("qualification-source-authority", values),
            **values,
        )
    source = _source(bars=(("100", "100.5", "99.5", "100"),) * 5)
    foreign_values = {
        name: value for name, value in source.members[0].__dict__.items() if name != "member_id"
    }
    foreign_values["source_artifact_hashes"] = (_hash("foreign"),)
    foreign_member = QualificationMemberEvidence(
        member_id=stable_identity("qualification-member", foreign_values),
        **foreign_values,
    )
    values = {name: value for name, value in source.__dict__.items() if name != "source_id"}
    values["members"] = (foreign_member,)
    with pytest.raises(ValueError, match="outside source scope"):
        HindsightQualificationSource(
            source_id=stable_identity("hindsight-qualification-source", values),
            **values,
        )
    assert artifact.artifact_id == source.scope_receipt.source_artifacts[0].artifact_id


def _projection_at(
    decision_at: datetime,
    decision: TradeDecisionValue | None,
    *,
    rank_position: int = 1,
) -> OpportunityRunProjection:
    state = {
        TradeDecisionValue.WATCH: SurfacingState.WATCHED,
        TradeDecisionValue.TAKE: SurfacingState.TAKEN,
        None: SurfacingState.NOT_DISCOVERED,
    }[decision]
    present = decision is not None
    values = {
        "run_id": (
            f"run-{decision_at.timestamp()}-{decision.value if decision else 'none'}"
            f"-rank-{rank_position}"
        ),
        "run_content_hash_sha256": _hash(f"run-{decision_at}-{decision}"),
        "decision_at": decision_at,
        "symbol": "ABC",
        "direction": StrategyDirection.LONG,
        "state": state,
        "candidate_id": "candidate-abc" if present else None,
        "candidate_content_hash_sha256": _hash("candidate-abc") if present else None,
        "evaluation_id": "evaluation-abc-long" if present else None,
        "evaluation_content_hash_sha256": _hash("evaluation-abc-long") if present else None,
        "ranked_id": f"ranked-abc-long-{rank_position}" if present else None,
        "ranked_content_hash_sha256": (
            _hash(f"ranked-abc-long-{rank_position}") if present else None
        ),
        "rank_position": rank_position if present else None,
        "decision_id": "decision-abc-long" if present else None,
        "decision_content_hash_sha256": _hash("decision-abc-long") if present else None,
        "decision_value": decision,
        "trace_id": "trace-abc-long" if present else None,
        "trace_content_hash_sha256": _hash("trace-abc-long") if present else None,
        "schema_version": "v2.opportunity.opportunity_run_projection.v1",
    }
    return OpportunityRunProjection(
        projection_id=stable_identity("opportunity-run-projection", values),
        **values,
    )


def test_session_inventory_and_replay_bind_exact_current_stored_heads() -> None:
    replay = _session_replay(limitations=("bounded_fixture_inventory",))

    assert replay.run_inventory.status is SessionRunInventoryStatus.COMPLETE_AUTHORITATIVE
    assert replay.current_outcome_replays == (_current_outcome_replay(),)
    assert SessionReplay.from_json(replay.to_json()) == replay
    assert (
        SessionRunInventoryEvidence.from_json(replay.run_inventory.to_json())
        == replay.run_inventory
    )
    with pytest.raises(ValueError, match="limitations must match"):
        replace(replay.run_inventory, limitations=())


def test_multi_run_inventory_reconciles_exact_set_and_stable_stage_tie() -> None:
    first = _current_outcome_replay()
    _prepared, _risks, changed_result = _finalized_two_strategy_pipeline(spread_bps=Decimal("6"))
    second = _stored_replay_for_batch(_label_pipeline_result(changed_result))
    assert first.pipeline_result.run_id != second.pipeline_result.run_id
    replay = _session_replay(current_outcome_replays=(second, first))

    result = reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=replay,
    )
    record = result.records[0]
    expected_run = min(first.pipeline_result.run_id, second.pipeline_result.run_id)

    assert len(replay.run_inventory.bindings) == 2
    assert len(record.run_projections) == 2
    assert record.selected_projection_id == next(
        item.projection_id for item in record.run_projections if item.run_id == expected_run
    )
    assert SessionReplay.from_json(replay.to_json()) == replay
    assert MissReconciliationBatch.from_json(result.to_json()) == result
    with pytest.raises(ValueError, match="authoritative run inventory"):
        replace(replay, current_outcome_replays=(first,))


def test_bounded_inventory_cannot_conclude_a_miss() -> None:
    result = reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=_session_replay(authoritative=False),
    )

    assert result.session_disposition is MissSessionDisposition.UNKNOWN
    assert len(result.records) == 1
    assert result.records[0].disposition is OpportunityDisposition.UNKNOWN


def test_reconciliation_is_standalone_recomputable_and_quality_gate_causal() -> None:
    result = reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=_session_replay(),
    )
    record = result.records[0]

    assert result.session_disposition is MissSessionDisposition.MISSED
    assert record.disposition is OpportunityDisposition.MISSED
    assert record.category is MissCategory.QUALITY_GATE_MISS
    assert record.selected_state is SurfacingState.RANKED
    assert record.best_on_time_rank_position == 1
    assert record.first_top_1_at == record.first_ranked_at
    assert record.first_top_3_at == record.first_ranked_at
    assert record.first_top_5_at == record.first_ranked_at
    assert not record.promotion_eligible
    assert MissedOpportunityRecord.from_json(record.to_json()) == record
    assert MissReconciliationBatch.from_json(result.to_json()) == result
    with pytest.raises(ValueError, match="best_on_time_rank_position does not recompute"):
        replace(record, best_on_time_rank_position=2)
    with pytest.raises(ValueError, match="first_top_1_at does not recompute"):
        replace(record, first_top_1_at=record.first_top_1_at + timedelta(seconds=1))


@pytest.mark.parametrize("decision", (TradeDecisionValue.WATCH, TradeDecisionValue.TAKE))
def test_cutoff_equality_is_too_late_never_on_time(decision) -> None:
    cutoff = SESSION_OPEN
    exact = _projection_at(cutoff, decision)
    before = _projection_at(cutoff - timedelta(microseconds=1), decision)
    absent = _projection_at(cutoff, None)

    assert _opportunity_disposition((exact,), cutoff, complete=True) is (
        OpportunityDisposition.TOO_LATE
    )
    assert _opportunity_disposition((before,), cutoff, complete=True) is (
        OpportunityDisposition.CAUGHT
    )
    assert _opportunity_disposition((absent,), cutoff, complete=True) is (
        OpportunityDisposition.MISSED
    )


def test_top_rank_surfacing_boundaries_are_independent_and_all_session() -> None:
    projections = (
        _projection_at(
            SESSION_OPEN + timedelta(seconds=1), TradeDecisionValue.WATCH, rank_position=6
        ),
        _projection_at(
            SESSION_OPEN + timedelta(seconds=2), TradeDecisionValue.WATCH, rank_position=5
        ),
        _projection_at(
            SESSION_OPEN + timedelta(seconds=3), TradeDecisionValue.WATCH, rank_position=3
        ),
        _projection_at(
            SESSION_OPEN + timedelta(seconds=4), TradeDecisionValue.WATCH, rank_position=1
        ),
    )

    assert _first_rank_at(projections, 1) == SESSION_OPEN + timedelta(seconds=4)
    assert _first_rank_at(projections, 3) == SESSION_OPEN + timedelta(seconds=3)
    assert _first_rank_at(projections, 5) == SESSION_OPEN + timedelta(seconds=2)


@pytest.mark.parametrize(
    "dispositions",
    (
        {OpportunityDisposition.CAUGHT, OpportunityDisposition.MISSED},
        {OpportunityDisposition.CAUGHT, OpportunityDisposition.TOO_LATE},
        {OpportunityDisposition.MISSED, OpportunityDisposition.TOO_LATE},
    ),
)
def test_heterogeneous_opportunity_dispositions_are_always_mixed(dispositions) -> None:
    assert _summarize_record_dispositions(dispositions) is MissSessionDisposition.MIXED


@pytest.mark.parametrize(
    ("statuses", "expected"),
    (
        (
            {QualificationStatus.QUALIFIED, QualificationStatus.PENDING},
            MissSessionDisposition.PENDING,
        ),
        (
            {QualificationStatus.QUALIFIED, QualificationStatus.CENSORED},
            MissSessionDisposition.CENSORED,
        ),
        (
            {QualificationStatus.QUALIFIED, QualificationStatus.UNAVAILABLE},
            MissSessionDisposition.UNAVAILABLE,
        ),
    ),
)
def test_unresolved_qualification_status_dominates_qualified_records(statuses, expected) -> None:
    assert _unresolved_qualification_disposition(statuses) is expected


def test_complete_unresolved_qualification_does_not_become_correct_no_trade() -> None:
    qualification = qualify_session_opportunities(
        _source(
            bars=(("100", "100.5", "99.5", "100"),) * 5,
            halt_status=SafetyStatus.UNKNOWN,
        ),
        policy=_policy(),
        horizons=(_horizon(),),
    )
    result = reconcile_missed_opportunities(
        qualification,
        session_replay=_session_replay(),
    )

    assert result.records == ()
    assert result.session_disposition is MissSessionDisposition.UNAVAILABLE


def test_complete_authoritative_negative_session_is_correct_no_trade() -> None:
    qualification = qualify_session_opportunities(
        _source(bars=(("100", "100.5", "99.5", "100"),) * 5),
        policy=_policy(),
        horizons=(_horizon(),),
    )
    assert {item.status for item in qualification.assessments} == {
        QualificationStatus.NOT_QUALIFIED
    }

    result = reconcile_missed_opportunities(
        qualification,
        session_replay=_session_replay(),
    )

    assert result.records == ()
    assert result.session_disposition is MissSessionDisposition.CORRECT_NO_TRADE


def test_complete_authoritative_negative_session_with_watch_is_false_positive() -> None:
    qualification = qualify_session_opportunities(
        _source(bars=(("100", "100.5", "99.5", "100"),) * 5),
        policy=_policy(),
        horizons=(_horizon(),),
    )
    assert {item.status for item in qualification.assessments} == {
        QualificationStatus.NOT_QUALIFIED
    }
    watch = _watch_current_outcome_replay()
    authoritative = _session_replay(current_outcome_replays=(watch,))

    result = reconcile_missed_opportunities(
        qualification,
        session_replay=authoritative,
    )

    assert result.records == ()
    assert result.session_disposition is MissSessionDisposition.FALSE_POSITIVE

    bounded = reconcile_missed_opportunities(
        qualification,
        session_replay=_session_replay(
            current_outcome_replays=(watch,),
            authoritative=False,
        ),
    )
    assert bounded.session_disposition is MissSessionDisposition.UNKNOWN

    unresolved = qualify_session_opportunities(
        _source(
            bars=(("100", "100.5", "99.5", "100"),) * 5,
            halt_status=SafetyStatus.UNKNOWN,
        ),
        policy=_policy(),
        horizons=(_horizon(),),
    )
    unavailable = reconcile_missed_opportunities(
        unresolved,
        session_replay=authoritative,
    )
    assert unavailable.session_disposition is MissSessionDisposition.UNAVAILABLE


def test_directional_discovery_does_not_credit_opposite_anomaly_family() -> None:
    short_result = reconcile_missed_opportunities(
        _short_qualified_batch(),
        session_replay=_session_replay(),
    )
    short_record = short_result.records[0]
    assert short_record.opportunity.direction is StrategyDirection.SHORT
    assert short_record.selected_state is SurfacingState.NOT_DISCOVERED
    assert short_record.category is MissCategory.FEATURE_MISS

    long_opportunity = _qualified_batch().opportunities[0]
    long_projection = _project_run(
        _short_candidate_pipeline_result(),
        long_opportunity,
    )
    assert long_projection.direction is StrategyDirection.LONG
    assert long_projection.state is SurfacingState.NOT_DISCOVERED
    assert long_projection.candidate_id is None


def test_same_direction_pair_selection_never_synthesizes_cross_pair_take() -> None:
    pass_evaluation = _empirical_evaluation(
        evaluation_id="evaluation-pass-rank-one",
        symbol="ABC",
    )
    take_evaluation = _empirical_evaluation(
        evaluation_id="evaluation-take-rank-two",
        symbol="ABC",
    )
    pass_config = replace(
        GATE_CONFIG,
        minimum_watch_score=Decimal("0.99"),
        minimum_take_score=Decimal("1"),
        config_version="fixture-pass-gate-v1",
    )
    passed = _gate_one(
        pass_evaluation,
        _execution_risk(evaluation=pass_evaluation),
        config=pass_config,
    )
    taken = _gate_one(
        take_evaluation,
        _execution_risk(evaluation=take_evaluation),
    )
    assert passed.decision is TradeDecisionValue.PASS
    assert taken.decision is TradeDecisionValue.TAKE
    pass_rank = replace(passed.ranked, relative_rank=1)
    take_rank = replace(taken.ranked, relative_rank=2)
    traces = (
        SimpleNamespace(
            evaluation_id=pass_evaluation.evaluation_id,
            trace_id="trace-pass-rank-one",
            content_hash=lambda: _hash("trace-pass-rank-one"),
        ),
        SimpleNamespace(
            evaluation_id=take_evaluation.evaluation_id,
            trace_id="trace-take-rank-two",
            content_hash=lambda: _hash("trace-take-rank-two"),
        ),
    )
    result = SimpleNamespace(
        run_id="same-direction-pair-run",
        decision_at=taken.decision_at,
        candidates=(_candidate(_snapshot(symbol="ABC")),),
        evaluations=(pass_evaluation, take_evaluation),
        ranked_opportunities=(pass_rank, take_rank),
        decisions=(passed, taken),
        traces=traces,
        content_hash=lambda: _hash("same-direction-pair-run"),
    )

    projection = _project_run(result, _qualified_batch().opportunities[0])

    assert projection.state is SurfacingState.TAKEN
    assert projection.evaluation_id == take_evaluation.evaluation_id
    assert projection.ranked_id == take_rank.ranked_id
    assert projection.rank_position == 2
    assert projection.decision_id == taken.decision_id
    assert projection.trace_id == "trace-take-rank-two"


def _result_for_surfacing_state(state: SurfacingState):
    lifecycle = (
        StrategyValidationState.EXPERIMENTAL
        if state is SurfacingState.WATCHED
        else StrategyValidationState.PRODUCTION_ELIGIBLE
    )
    evaluation = _empirical_evaluation(
        lifecycle=lifecycle,
        evaluation_id=f"evaluation-surfacing-{state.value}",
        symbol="ABC",
    )
    candidate = replace(
        _candidate(_snapshot(symbol="ABC")),
        candidate_id=evaluation.candidate_id,
        feature_snapshot_id=evaluation.feature_snapshot_id,
    )
    risk = _execution_risk(evaluation=evaluation)
    if state is SurfacingState.TAKEN:
        decision = _gate_one(evaluation, risk)
    elif state is SurfacingState.WATCHED:
        decision = _gate_one(evaluation, risk)
    elif state is SurfacingState.RANKED:
        decision = _gate_one(
            evaluation,
            risk,
            config=replace(
                GATE_CONFIG,
                minimum_watch_score=Decimal("0.99"),
                minimum_take_score=Decimal("1"),
                config_version="surfacing-ranked-pass-v1",
            ),
        )
    else:
        decision = None
    ranked = decision.ranked if decision is not None else None
    trace = (
        SimpleNamespace(
            evaluation_id=evaluation.evaluation_id,
            trace_id=f"trace-surfacing-{state.value}",
            content_hash=lambda: _hash(f"trace-surfacing-{state.value}"),
        )
        if decision is not None
        else None
    )
    return SimpleNamespace(
        run_id=f"run-surfacing-{state.value}",
        decision_at=evaluation.decision_at,
        candidates=() if state is SurfacingState.NOT_DISCOVERED else (candidate,),
        evaluations=(
            ()
            if state in {SurfacingState.NOT_DISCOVERED, SurfacingState.DISCOVERED}
            else (evaluation,)
        ),
        ranked_opportunities=(ranked,) if ranked is not None else (),
        decisions=(decision,) if decision is not None else (),
        traces=(trace,) if trace is not None else (),
        content_hash=lambda: _hash(f"run-surfacing-{state.value}"),
    )


@pytest.mark.parametrize("state", tuple(SurfacingState))
def test_surfacing_state_matrix_uses_one_coherent_pair(state) -> None:
    projection = _project_run(
        _result_for_surfacing_state(state),
        _qualified_batch().opportunities[0],
    )

    assert projection.state is state
    if state in {SurfacingState.WATCHED, SurfacingState.TAKEN}:
        assert projection.evaluation_id is not None
        assert projection.ranked_id is not None
        assert projection.decision_id is not None
        assert projection.trace_id is not None


def test_category_taxonomy_and_precedence_matrix_uses_exact_gate_evidence() -> None:
    opportunity = _qualified_batch().opportunities[0]
    short_opportunity = _short_qualified_batch().opportunities[0]
    current_replay = _session_replay()
    current = current_replay.current_outcome_replays[0].pipeline_result
    selected = (
        reconcile_missed_opportunities(_qualified_batch(), session_replay=current_replay)
        .records[0]
        .run_projections[0]
    )

    def result_copy(**updates):
        values = dict(current.__dict__)
        for name in (
            "cheap_snapshots",
            "candidates",
            "evaluations",
            "rich_snapshots",
            "ranked_opportunities",
            "decisions",
            "risk_evidence",
            "security_regimes",
            "market_regime",
        ):
            values[name] = getattr(current, name)
        values.update(updates)
        values["content_hash"] = current.content_hash
        return SimpleNamespace(**values)

    def preparation_copy(universe_snapshot):
        values = dict(current.preparation.__dict__)
        values["universe_snapshot"] = universe_snapshot
        return SimpleNamespace(**values)

    def classify(result, projection):
        replay = SimpleNamespace(current_outcome_replays=(SimpleNamespace(pipeline_result=result),))
        return _classify_category(opportunity, replay, projection)

    no_member_universe = SimpleNamespace(included_members=(), excluded_members=())
    assert (
        classify(
            result_copy(preparation=preparation_copy(no_member_universe)),
            selected,
        )
        is MissCategory.UNIVERSE_MISS
    )

    exact_member = next(
        item
        for item in current.preparation.universe_snapshot.included_members
        if item.symbol == "ABC"
    )
    unavailable_member = SimpleNamespace(
        **{**exact_member.__dict__, "data_availability": CapabilityState.UNKNOWN}
    )
    unavailable_universe = SimpleNamespace(
        included_members=(unavailable_member,), excluded_members=()
    )
    assert (
        classify(
            result_copy(preparation=preparation_copy(unavailable_universe)),
            selected,
        )
        is MissCategory.DATA_MISS
    )

    cheap = next(item for item in current.cheap_snapshots if item.symbol == "ABC")
    feature_category = _undiscovered_category(
        cheap,
        current.preparation.discovery_config,
        short_opportunity,
    )
    assert feature_category is MissCategory.FEATURE_MISS
    anomaly_category = _undiscovered_category(
        _snapshot(
            symbol="ABC",
            overrides={"vwap_proxy_loss": Decimal("0")},
        ),
        current.preparation.discovery_config,
        short_opportunity,
    )
    assert anomaly_category is MissCategory.ANOMALY_MISS
    assert (
        _undiscovered_category(
            _snapshot(symbol="ABC"),
            current.preparation.discovery_config,
            short_opportunity,
        )
        is MissCategory.UNKNOWN
    )

    missing_rich = _snapshot(symbol="ABC", unavailable=("atr_prior",))
    missing_evaluation = _evaluate("DS-MOM-001", missing_rich)
    rich_feature_category = _rich_feature_category(
        current,
        missing_evaluation,
        missing_rich,
    )
    assert rich_feature_category is MissCategory.FEATURE_MISS
    selected_evaluation = next(
        item for item in current.evaluations if item.evaluation_id == selected.evaluation_id
    )
    selected_rich = next(item for item in current.rich_snapshots if item.symbol == "ABC")
    assert (
        _rich_feature_category(
            current,
            selected_evaluation,
            selected_rich,
        )
        is None
    )
    sibling_result = result_copy(
        evaluations=(*current.evaluations, missing_evaluation),
    )
    assert classify(sibling_result, selected) is MissCategory.QUALITY_GATE_MISS

    regime_evidence = _retrospective_regime_evidence(current)
    regime_opportunity = SimpleNamespace(
        symbol="ABC",
        assessment=SimpleNamespace(
            source=SimpleNamespace(retrospective_regime_evidence=(regime_evidence,))
        ),
    )
    regime_category = _regime_category(regime_opportunity, current)
    assert regime_category is MissCategory.REGIME_MISCLASSIFICATION
    assert (
        _first_stage_category(
            rich_feature_category,
            regime_category,
        )
        is MissCategory.FEATURE_MISS
    )

    strategy_result = result_copy(evaluations=(), ranked_opportunities=(), decisions=())
    strategy_projection = _project_run(strategy_result, opportunity)
    assert strategy_projection.state is SurfacingState.DISCOVERED
    assert classify(strategy_result, strategy_projection) is MissCategory.STRATEGY_MISS

    feature_batch = reconcile_missed_opportunities(
        _short_qualified_batch(),
        session_replay=current_replay,
    )
    assert feature_batch.records[0].category is MissCategory.FEATURE_MISS
    assert (
        MissedOpportunityRecord.from_json(feature_batch.records[0].to_json())
        == feature_batch.records[0]
    )
    assert MissReconciliationBatch.from_json(feature_batch.to_json()) == feature_batch

    production = _empirical_evaluation(
        evaluation_id="category-production-evaluation",
        symbol="ABC",
    )
    risk = _execution_risk(evaluation=production)
    scoring = _gate_one(
        production,
        risk,
        config=replace(
            GATE_CONFIG,
            minimum_watch_score=Decimal("0.99"),
            minimum_take_score=Decimal("1"),
            config_version="category-scoring-v1",
        ),
    )
    assert _gate_category(scoring, risk) is MissCategory.SCORING_MISS

    quality = next(
        item
        for item in current.decisions
        if item.symbol == "ABC" and item.direction is StrategyDirection.LONG
    )
    quality_risk = next(
        item for item in current.risk_evidence if item.evaluation_id == quality.evaluation_id
    )
    assert _gate_category(quality, quality_risk) is MissCategory.QUALITY_GATE_MISS

    execution = _gate_one(production, None)
    assert _gate_category(execution, None) is MissCategory.EXECUTION_FILTER

    taken = _gate_one(production, risk)
    assert taken.decision is TradeDecisionValue.TAKE
    assert _gate_category(taken, risk) is MissCategory.UNKNOWN

    assert {
        MissCategory.UNIVERSE_MISS,
        MissCategory.DATA_MISS,
        MissCategory.FEATURE_MISS,
        MissCategory.ANOMALY_MISS,
        MissCategory.REGIME_MISCLASSIFICATION,
        MissCategory.STRATEGY_MISS,
        MissCategory.SCORING_MISS,
        MissCategory.QUALITY_GATE_MISS,
        MissCategory.EXECUTION_FILTER,
        MissCategory.UNKNOWN,
    } == set(MissCategory)


def test_consistently_rehashed_record_projection_tamper_rejects() -> None:
    record = reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=_session_replay(),
    ).records[0]
    projection = record.run_projections[0]
    changed_projection_values = {
        name: value for name, value in projection.__dict__.items() if name != "projection_id"
    }
    changed_projection_values["rank_position"] = 2
    changed_projection = OpportunityRunProjection(
        projection_id=stable_identity(
            "opportunity-run-projection",
            changed_projection_values,
        ),
        **changed_projection_values,
    )
    payload = record.to_dict()
    payload["run_projections"] = [changed_projection.to_dict()]
    payload["miss_record_id"] = stable_identity(
        "missed-opportunity-record",
        {key: value for key, value in payload.items() if key != "miss_record_id"},
    )
    with pytest.raises(ValueError, match="run_projections does not recompute"):
        MissedOpportunityRecord.from_dict(payload)

    for updates in (
        {"state": SurfacingState.STRATEGY_ELIGIBLE},
        {
            "evaluation_id": "evaluation-foreign-pair",
            "evaluation_content_hash_sha256": _hash("evaluation-foreign-pair"),
        },
    ):
        values = {
            name: value for name, value in projection.__dict__.items() if name != "projection_id"
        }
        values.update(updates)
        changed = OpportunityRunProjection(
            projection_id=stable_identity("opportunity-run-projection", values),
            **values,
        )
        changed_payload = record.to_dict()
        changed_payload["run_projections"] = [changed.to_dict()]
        changed_payload["miss_record_id"] = stable_identity(
            "missed-opportunity-record",
            {key: value for key, value in changed_payload.items() if key != "miss_record_id"},
        )
        with pytest.raises(ValueError, match="run_projections does not recompute"):
            MissedOpportunityRecord.from_dict(changed_payload)


def test_record_and_batch_reject_rehashed_category_disposition_and_schema_tamper() -> None:
    batch = reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=_session_replay(),
    )
    record_payload = batch.records[0].to_dict()
    record_payload["category"] = MissCategory.UNKNOWN.value
    record_payload["miss_record_id"] = stable_identity(
        "missed-opportunity-record",
        {key: value for key, value in record_payload.items() if key != "miss_record_id"},
    )
    with pytest.raises(ValueError, match="category does not recompute"):
        MissedOpportunityRecord.from_dict(record_payload)

    batch_payload = batch.to_dict()
    batch_payload["session_disposition"] = MissSessionDisposition.CAUGHT.value
    batch_payload["batch_id"] = stable_identity(
        "miss-reconciliation-batch",
        {key: value for key, value in batch_payload.items() if key != "batch_id"},
    )
    with pytest.raises(ValueError, match="session disposition does not recompute"):
        MissReconciliationBatch.from_dict(batch_payload)

    schema_payload = batch.records[0].to_dict()
    schema_payload["schema_version"] = "v2.opportunity.missed_opportunity_record.v999"
    schema_payload["miss_record_id"] = stable_identity(
        "missed-opportunity-record",
        {key: value for key, value in schema_payload.items() if key != "miss_record_id"},
    )
    with pytest.raises(ValueError, match="schema_version"):
        MissedOpportunityRecord.from_dict(schema_payload)


def test_miss_deserialization_rejects_top_level_and_deep_unknown_fields() -> None:
    batch = reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=_session_replay(),
    )
    top = batch.to_dict()
    top["caller_asserted_label"] = "missed"
    with pytest.raises(ValueError, match="unknown field"):
        MissReconciliationBatch.from_dict(top)

    deep = batch.records[0].to_dict()
    deep["session_replay"]["run_inventory"]["source_receipt"][  # type: ignore[index]
        "api_key"
    ] = "secret"
    with pytest.raises(ValueError, match="unknown field"):
        MissedOpportunityRecord.from_dict(deep)


def test_future_observation_mutation_changes_only_downstream_miss_identities() -> None:
    original_qualification = _qualified_batch()
    changed_qualification = qualify_session_opportunities(
        _source(
            bars=(
                ("100", "100.5", "99.5", "100.1"),
                ("100.2", "102.1", "100", "102"),
                ("102", "102.2", "101.8", "102"),
                ("102", "102.2", "101.8", "102"),
                ("102", "103.2", "101.8", "103"),
            )
        ),
        policy=_policy(),
        horizons=(_horizon(),),
    )
    replay = _session_replay()
    original = reconcile_missed_opportunities(
        original_qualification,
        session_replay=replay,
    )
    changed = reconcile_missed_opportunities(
        changed_qualification,
        session_replay=replay,
    )
    stored = replay.current_outcome_replays[0].pipeline_result
    stored_ids = (
        stored.run_id,
        stored.preparation.preparation_id,
        stored.preparation.universe_snapshot.universe_snapshot_id,
        tuple(item.evaluation_id for item in stored.evaluations),
        tuple(item.ranked_id for item in stored.ranked_opportunities),
        tuple(item.decision_id for item in stored.decisions),
        tuple(item.trace_id for item in stored.traces),
    )

    assert original_qualification.source.source_id != changed_qualification.source.source_id
    assert original_qualification.assessments[0].assessment_id != (
        changed_qualification.assessments[0].assessment_id
    )
    assert original_qualification.opportunities[0].opportunity_id != (
        changed_qualification.opportunities[0].opportunity_id
    )
    assert original.records[0].miss_record_id != changed.records[0].miss_record_id
    assert original.batch_id != changed.batch_id
    assert original.session_replay == changed.session_replay == replay
    unchanged = changed.session_replay.current_outcome_replays[0].pipeline_result
    assert stored_ids == (
        unchanged.run_id,
        unchanged.preparation.preparation_id,
        unchanged.preparation.universe_snapshot.universe_snapshot_id,
        tuple(item.evaluation_id for item in unchanged.evaluations),
        tuple(item.ranked_id for item in unchanged.ranked_opportunities),
        tuple(item.decision_id for item in unchanged.decisions),
        tuple(item.trace_id for item in unchanged.traces),
    )


def test_core_imports_do_not_load_any_miss_module_and_facade_is_explicit() -> None:
    modules = (
        "intraday_scanner.v2.opportunity",
        "intraday_scanner.v2.opportunity.models",
        "intraday_scanner.v2.opportunity.features",
        "intraday_scanner.v2.opportunity.discovery",
        "intraday_scanner.v2.opportunity.regimes",
        "intraday_scanner.v2.opportunity.registry",
        "intraday_scanner.v2.opportunity.ranking",
        "intraday_scanner.v2.opportunity.risk",
        "intraday_scanner.v2.opportunity.quality_gate",
        "intraday_scanner.v2.opportunity.pipeline",
        "intraday_scanner.storage.opportunity_store",
        "intraday_scanner.storage.opportunity_outcome_store",
    )
    script = (
        "import importlib,sys;"
        f"mods={modules!r};"
        "[importlib.import_module(x) for x in mods];"
        "print(sorted(x for x in sys.modules if "
        "x.startswith('intraday_scanner.v2.opportunity.miss')))"
    )
    completed = subprocess.run(
        [sys.executable, "-c", script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == "[]"


def test_every_miss_module_has_downstream_only_imports() -> None:
    root = Path("intraday_scanner/v2/opportunity")
    forbidden = (
        "alpha.path_replay",
        "backtest",
        "runtime",
        "dashboard",
        "broker",
        "network",
        "scheduler",
    )
    paths = tuple(sorted(root.glob("miss*.py")))
    assert paths
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = tuple(
            node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
        ) + tuple(
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any(blocked in module for module in imported for blocked in forbidden), path
