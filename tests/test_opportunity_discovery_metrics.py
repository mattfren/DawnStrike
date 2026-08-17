from __future__ import annotations

import subprocess
import sys
from dataclasses import fields, replace
from decimal import ROUND_DOWN, ROUND_UP, Decimal, getcontext
from functools import lru_cache
from pathlib import Path

import pytest

import tests.test_opportunity_missed as missed_fixtures
from intraday_scanner.v2.opportunity.miss_contracts import (
    QualificationClaimKind,
    QualificationHorizonKind,
    QualificationStatus,
)
from intraday_scanner.v2.opportunity.miss_metric_contracts import (
    DiscoveryMetricDefinition,
    DiscoveryMetricName,
    DiscoveryMetricPolicy,
    build_discovery_metric_policy,
    canonical_metric_definitions,
    quantize_metric_fraction,
)
from intraday_scanner.v2.opportunity.miss_metric_matching import (
    DiscoveryMetricSessionEvidence,
    build_discovery_metric_session_evidence,
)
from intraday_scanner.v2.opportunity.miss_metric_reconciliation import (
    DiscoveryMetricReport,
    SessionDiscoveryMetricReport,
    reconcile_discovery_metrics,
    reconcile_session_discovery_metrics,
)
from intraday_scanner.v2.opportunity.miss_qualification import (
    qualify_session_opportunities,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import (
    reconcile_missed_opportunities,
)
from intraday_scanner.v2.opportunity.miss_sources import (
    HindsightQualificationSource,
    QualificationMemberEvidence,
    QualificationSourceAuthority,
    build_qualification_source_scope_receipt,
)
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcomes import (
    build_outcome_bar_evidence,
    build_outcome_observation_dataset,
    build_outcome_observation_series,
)
from intraday_scanner.v2.opportunity.pipeline import (
    build_strategy_expectancy_binding,
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.registry import (
    StrategyRegistry,
    build_default_registry,
)
from intraday_scanner.v2.opportunity.universe import SafetyStatus
from tests.test_opportunity_missed import (
    _authority,
    _authority_artifact,
    _current_outcome_replay,
    _execution_risk_for,
    _finalized_two_strategy_pipeline,
    _hash,
    _horizon,
    _label_pipeline_result,
    _member,
    _pipeline_risk_policy,
    _pipeline_universe,
    _policy,
    _qualified_batch,
    _series,
    _session_replay,
    _source,
    _stored_replay_for_batch,
    _two_candidate_dataset,
    _watch_current_outcome_replay,
)


def _metric_policy():
    return build_discovery_metric_policy(
        policy_version="wp004-b-v1",
        qualification_policy=_policy(),
        horizon_kind=QualificationHorizonKind.ELAPSED_FROM_ENTRY,
        elapsed_seconds=300,
    )


def _miss_batch(*, multiple_runs: bool = False):
    currents = (
        (_current_outcome_replay(), _watch_current_outcome_replay())
        if multiple_runs
        else (_current_outcome_replay(),)
    )
    return reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=_session_replay(current_outcome_replays=currents),
    )


def _session_miss_batch(session_id: str, *, caught: bool = False):
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = session_id
    try:
        currents = (
            (_current_outcome_replay(), _watch_current_outcome_replay())
            if caught
            else (_current_outcome_replay(),)
        )
        return reconcile_missed_opportunities(
            _qualified_batch(),
            session_replay=_session_replay(current_outcome_replays=currents),
        )
    finally:
        missed_fixtures.SESSION_ID = previous


def _negative_miss_batch():
    qualification = qualify_session_opportunities(
        _source(
            bars=(
                ("100", "100.2", "99.8", "100"),
                ("100", "100.2", "99.8", "100"),
                ("100", "100.2", "99.8", "100"),
                ("100", "100.2", "99.8", "100"),
                ("100", "100.2", "99.8", "100"),
            )
        ),
        policy=_policy(),
        horizons=(_horizon(),),
    )
    assert all(
        item.status is QualificationStatus.NOT_QUALIFIED
        for item in qualification.assessments
    )
    return reconcile_missed_opportunities(
        qualification,
        session_replay=_session_replay(),
    )


def _bars_for_truth(kind: str):
    if kind == "long":
        return (
            ("100", "100.5", "99.5", "100.1"),
            ("100.2", "102.1", "100", "102"),
            ("102", "102.2", "101.8", "102"),
            ("102", "102.2", "101.8", "102"),
            ("102", "102.2", "101.8", "102"),
        )
    if kind == "short":
        return (
            ("100", "100.5", "99.5", "99.9"),
            ("99.8", "100", "97.9", "98"),
            ("98", "98.2", "97.8", "98"),
            ("98", "98.2", "97.8", "98"),
            ("98", "98.2", "97.8", "98"),
        )
    return (
        ("100", "100.2", "99.8", "100"),
        ("100", "100.2", "99.8", "100"),
        ("100", "100.2", "99.8", "100"),
        ("100", "100.2", "99.8", "100"),
        ("100", "100.2", "99.8", "100"),
    )


def _clone_series(symbol: str, kind: str):
    original = _series(bars=_bars_for_truth(kind))
    metadata = replace(
        original.coverage_receipt.source_metadata,
        raw_artifact_hash_sha256=_hash(f"metric-raw-{symbol}-{kind}"),
        normalized_artifact_hash_sha256=_hash(f"metric-normalized-{symbol}-{kind}"),
    )
    observations = tuple(
        build_outcome_bar_evidence(
            bar=replace(item.bar, symbol=symbol, source_metadata=metadata),
            interval_start_at=item.interval_start_at,
            interval_end_at=item.interval_end_at,
        )
        for item in original.observations
    )
    coverage = replace(
        original.coverage_receipt,
        coverage_receipt_id=f"coverage-{symbol.lower()}",
        symbol=symbol,
        source_metadata=metadata,
        observed_start=observations[0].interval_start_at,
        observed_end=observations[-1].interval_end_at,
        artifact_manifest_ids=(f"manifest-{symbol.lower()}",),
    )
    return build_outcome_observation_series(
        symbol=symbol,
        exchange_session_id=missed_fixtures.SESSION_ID,
        decision_at=original.decision_at,
        requested_through_at=original.requested_through_at,
        first_expected_interval_start_at=original.first_expected_interval_start_at,
        coverage_receipt=coverage,
        observations=observations,
        source_identity="fixture_outcome_source",
        method="retained_session_minute_bars",
    )


def _multi_symbol_source(truth_by_symbol: dict[str, str]) -> HindsightQualificationSource:
    symbols = tuple(sorted(truth_by_symbol))
    series = tuple(_clone_series(symbol, truth_by_symbol[symbol]) for symbol in symbols)
    first = _source(bars=_bars_for_truth("flat"))
    dataset = build_outcome_observation_dataset(
        decision_at=series[0].decision_at,
        frozen_at=first.frozen_at,
        series=series,
    )
    artifact = _authority_artifact()
    authority_template = _authority(artifact)
    authority_values = {
        name: value
        for name, value in authority_template.__dict__.items()
        if name != "authority_id"
    }
    authority_values["cohort_symbols"] = symbols
    authority = QualificationSourceAuthority(
        authority_id=stable_identity("qualification-source-authority", authority_values),
        **authority_values,
    )
    scope = build_qualification_source_scope_receipt(
        exchange_session_id=missed_fixtures.SESSION_ID,
        session_open_at=missed_fixtures.SESSION_OPEN,
        session_close_at=missed_fixtures.SESSION_CLOSE,
        membership_as_of_at=authority.membership_as_of_at,
        query_started_at=missed_fixtures.SESSION_CLOSE,
        query_ended_at=missed_fixtures.FETCHED_AT,
        observed_through_at=missed_fixtures.SESSION_CLOSE,
        fetched_at=missed_fixtures.FETCHED_AT,
        authority=authority,
        requested_symbols=symbols,
        provider_receipts=first.scope_receipt.provider_receipts,
        source_artifacts=(artifact,),
    )
    member_template = _member(artifact)
    members = []
    for symbol in symbols:
        member_values = {
            name: value
            for name, value in member_template.__dict__.items()
            if name != "member_id"
        }
        member_values["symbol"] = symbol
        members.append(
            QualificationMemberEvidence(
                member_id=stable_identity("qualification-member", member_values),
                **member_values,
            )
        )
    source_hashes = tuple(
        sorted({artifact.content_hash_sha256, *dataset.source_artifact_hashes})
    )
    values = {
        "scope_receipt": scope,
        "members": tuple(members),
        "observation_dataset": dataset,
        "missing_series_symbols": (),
        "execution_evidence": (),
        "retrospective_regime_evidence": (),
        "frozen_at": missed_fixtures.FETCHED_AT,
        "recorded_at": missed_fixtures.FETCHED_AT,
        "source_artifact_hashes": source_hashes,
        "limitations": (),
        "research_only": True,
        "schema_version": "v2.opportunity.hindsight_qualification_source.v1",
    }
    return HindsightQualificationSource(
        source_id=stable_identity("hindsight-qualification-source", values),
        **values,
    )


@lru_cache(maxsize=1)
def _six_rank_current_replay():
    symbols = ("ABC", "DEF", "GHI", "JKL", "MNO", "PQR")
    dataset = _two_candidate_dataset()
    bars = dict(dataset.bars_by_symbol)
    for symbol in symbols[2:]:
        bars[symbol] = tuple(replace(item, symbol=symbol) for item in bars["ABC"])
    changed = replace(dataset, bars_by_symbol=bars)
    default = build_default_registry()
    registry = StrategyRegistry((default.get("DS-MOM-001"),))
    prepared = prepare_opportunity_pipeline(
        changed,
        universe_snapshot=_pipeline_universe(changed, requested_symbols=symbols),
        registry=registry,
        expectancy_bindings=_finalized_two_strategy_pipeline()[0].expectancy_bindings,
        sector_by_symbol={symbol: f"sector-{symbol}" for symbol in symbols},
        correlation_cluster_by_symbol={
            symbol: f"cluster-{symbol}" for symbol in symbols
        },
    )
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={
            item.evaluation_id: _execution_risk_for(item)
            for item in prepared.evaluations
            if item.status.value == "eligible"
        },
        risk_policy=_pipeline_risk_policy(),
    )
    assert tuple(item.relative_rank for item in result.ranked_opportunities) == tuple(
        range(1, 7)
    )
    return _stored_replay_for_batch(_label_pipeline_result(result))


def _run_prepared_metric_pipeline(prepared):
    return run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={
            item.evaluation_id: _execution_risk_for(item)
            for item in prepared.evaluations
            if item.status.value == "eligible"
        },
        risk_policy=_pipeline_risk_policy(),
    )


@lru_cache(maxsize=1)
def _multi_strategy_current_replay():
    symbols = ("ABC", "DEF", "GHI")
    dataset = _two_candidate_dataset()
    bars = dict(dataset.bars_by_symbol)
    bars["GHI"] = tuple(replace(item, symbol="GHI") for item in bars["ABC"])
    changed = replace(dataset, bars_by_symbol=bars)
    default = build_default_registry()
    primary = default.get("DS-MOM-001")
    alternate = replace(
        primary,
        strategy_id="DS-MOM-001-ALT",
        name="Metric fixture duplicate evaluator",
        description="Independent strategy identity for matching dedup proof",
    )
    registry = StrategyRegistry((primary, alternate))
    universe = _pipeline_universe(changed, requested_symbols=symbols)
    evidence = _finalized_two_strategy_pipeline()[0].expectancy_bindings[0].evidence
    bindings = tuple(
        build_strategy_expectancy_binding(
            decision_at=universe.decision_at,
            strategy_definition=definition,
            evidence=evidence,
            observed_at=universe.decision_at,
            source_identity="metric-fixture",
            method="fixture cohort calculation",
        )
        for definition in registry.definitions
    )
    prepared = prepare_opportunity_pipeline(
        changed,
        universe_snapshot=universe,
        registry=registry,
        expectancy_bindings=bindings,
        sector_by_symbol={symbol: f"sector-{symbol}" for symbol in symbols},
        correlation_cluster_by_symbol={
            symbol: f"cluster-{symbol}" for symbol in symbols
        },
    )
    result = _run_prepared_metric_pipeline(prepared)
    assert tuple(item.relative_rank for item in result.ranked_opportunities) == tuple(
        range(1, 7)
    )
    return _stored_replay_for_batch(_label_pipeline_result(result))


@lru_cache(maxsize=1)
def _short_current_replay():
    symbols = ("ABC", "DEF")
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
    definition = build_default_registry().get("DS-MOM-003")
    registry = StrategyRegistry((definition,))
    universe = _pipeline_universe(changed, requested_symbols=symbols)
    evidence = _finalized_two_strategy_pipeline()[0].expectancy_bindings[0].evidence
    binding = build_strategy_expectancy_binding(
        decision_at=universe.decision_at,
        strategy_definition=definition,
        evidence=evidence,
        observed_at=universe.decision_at,
        source_identity="metric-fixture",
        method="fixture cohort calculation",
    )
    prepared = prepare_opportunity_pipeline(
        changed,
        universe_snapshot=universe,
        registry=registry,
        expectancy_bindings=(binding,),
        sector_by_symbol={symbol: f"sector-{symbol}" for symbol in symbols},
        correlation_cluster_by_symbol={
            symbol: f"cluster-{symbol}" for symbol in symbols
        },
    )
    result = _run_prepared_metric_pipeline(prepared)
    assert all(item.direction.value == "short" for item in result.ranked_opportunities)
    return _stored_replay_for_batch(_label_pipeline_result(result))


def _rank_truth_miss_batch(session_id: str, *, repeated: bool = False):
    current = _six_rank_current_replay()
    repeated_current = _watch_current_outcome_replay() if repeated else None
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = session_id
    try:
        truth = {
            "ABC": "long",
            "DEF": "flat",
            "GHI": "long",
            "JKL": "flat",
            "MNO": "long",
            "PQR": "flat",
        }
        qualification = qualify_session_opportunities(
            _multi_symbol_source(truth),
            policy=_policy(),
            horizons=(_horizon(),),
        )
        currents = (
            (current, repeated_current)
            if repeated
            else (current,)
        )
        replay = _session_replay(current_outcome_replays=currents)
        return reconcile_missed_opportunities(
            qualification,
            session_replay=replay,
        )
    finally:
        missed_fixtures.SESSION_ID = previous


def _long_short_miss_batch(session_id: str):
    short_current = _short_current_replay()
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = session_id
    try:
        qualification = qualify_session_opportunities(
            _multi_symbol_source({"ABC": "long", "DEF": "short"}),
            policy=_policy(),
            horizons=(_horizon(),),
        )
        return reconcile_missed_opportunities(
            qualification,
            session_replay=_session_replay(
                current_outcome_replays=(
                    _current_outcome_replay(),
                    short_current,
                )
            ),
        )
    finally:
        missed_fixtures.SESSION_ID = previous


def _multi_strategy_miss_batch(session_id: str):
    current = _multi_strategy_current_replay()
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = session_id
    try:
        qualification = qualify_session_opportunities(
            _multi_symbol_source({"ABC": "long", "DEF": "flat", "GHI": "flat"}),
            policy=_policy(),
            horizons=(_horizon(),),
        )
        return reconcile_missed_opportunities(
            qualification,
            session_replay=_session_replay(current_outcome_replays=(current,)),
        )
    finally:
        missed_fixtures.SESSION_ID = previous


def _negative_session_miss_batch(session_id: str):
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = session_id
    try:
        return _negative_miss_batch()
    finally:
        missed_fixtures.SESSION_ID = previous


def _executable_session_miss_batch(session_id: str):
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = session_id
    try:
        qualification = qualify_session_opportunities(
            _source(bars=_bars_for_truth("long"), with_execution=True),
            policy=_policy(),
            horizons=(_horizon(),),
        )
        return reconcile_missed_opportunities(
            qualification,
            session_replay=_session_replay(),
        )
    finally:
        missed_fixtures.SESSION_ID = previous


def _cutoff_equality_miss_batch():
    current = _current_outcome_replay()
    decision_at = current.pipeline_result.decision_at.astimezone(
        missed_fixtures.UTC
    )
    previous = (
        missed_fixtures.SESSION_ID,
        missed_fixtures.SESSION_OPEN,
        missed_fixtures.SESSION_CLOSE,
        missed_fixtures.FETCHED_AT,
    )
    missed_fixtures.SESSION_ID = "XNYS-2026-08-11-CUTOFF-EQUALITY"
    missed_fixtures.SESSION_OPEN = decision_at
    missed_fixtures.SESSION_CLOSE = decision_at + missed_fixtures.timedelta(minutes=5)
    missed_fixtures.FETCHED_AT = missed_fixtures.SESSION_CLOSE + missed_fixtures.timedelta(
        minutes=1
    )
    try:
        qualification = qualify_session_opportunities(
            _source(bars=_bars_for_truth("long")),
            policy=_policy(),
            horizons=(_horizon(),),
        )
        return reconcile_missed_opportunities(
            qualification,
            session_replay=_session_replay(current_outcome_replays=(current,)),
        )
    finally:
        (
            missed_fixtures.SESSION_ID,
            missed_fixtures.SESSION_OPEN,
            missed_fixtures.SESSION_CLOSE,
            missed_fixtures.FETCHED_AT,
        ) = previous


def _pending_source():
    previous_fetched_at = missed_fixtures.FETCHED_AT
    observed_through_at = missed_fixtures.SESSION_OPEN + missed_fixtures.timedelta(
        minutes=2
    )
    missed_fixtures.FETCHED_AT = observed_through_at
    try:
        artifact = _authority_artifact()
        authority = _authority(artifact)
        capability = missed_fixtures.build_provider_capability_receipt(
            provider="fixture-provider",
            feed="fixture-bars",
            entitlement_identity="fixture-research",
            decision_at=observed_through_at,
            observed_at=observed_through_at,
            bars=missed_fixtures.CapabilityState.AVAILABLE,
            trades=missed_fixtures.CapabilityState.UNKNOWN,
            quotes=missed_fixtures.CapabilityState.UNKNOWN,
            consolidated_nbbo=missed_fixtures.CapabilityState.UNKNOWN,
            aggressor_classification=missed_fixtures.CapabilityState.UNKNOWN,
            corporate_actions=missed_fixtures.CapabilityState.AVAILABLE,
            halts=missed_fixtures.CapabilityState.AVAILABLE,
            historical_coverage=missed_fixtures.CapabilityState.AVAILABLE,
            coverage_start=missed_fixtures.SESSION_OPEN,
            coverage_end=observed_through_at,
            source_identity=artifact.source_identity,
            method="partial_fixture_capability",
            limitations=("session_observation_pending",),
        )
        scope = build_qualification_source_scope_receipt(
            exchange_session_id=missed_fixtures.SESSION_ID,
            session_open_at=missed_fixtures.SESSION_OPEN,
            session_close_at=missed_fixtures.SESSION_CLOSE,
            membership_as_of_at=authority.membership_as_of_at,
            query_started_at=missed_fixtures.SESSION_OPEN,
            query_ended_at=observed_through_at,
            observed_through_at=observed_through_at,
            fetched_at=observed_through_at,
            authority=authority,
            requested_symbols=("ABC",),
            provider_receipts=(capability,),
            source_artifacts=(artifact,),
            limitations=("session_observation_pending",),
        )
        dataset = build_outcome_observation_dataset(
            decision_at=missed_fixtures.SESSION_OPEN
            - missed_fixtures.timedelta(microseconds=1),
            frozen_at=observed_through_at,
            series=(),
        )
        values = {
            "scope_receipt": scope,
            "members": (_member(artifact),),
            "observation_dataset": dataset,
            "missing_series_symbols": ("ABC",),
            "execution_evidence": (),
            "retrospective_regime_evidence": (),
            "frozen_at": observed_through_at,
            "recorded_at": observed_through_at,
            "source_artifact_hashes": (artifact.content_hash_sha256,),
            "limitations": ("session_observation_pending",),
            "research_only": True,
            "schema_version": "v2.opportunity.hindsight_qualification_source.v1",
        }
        return HindsightQualificationSource(
            source_id=stable_identity("hindsight-qualification-source", values),
            **values,
        )
    finally:
        missed_fixtures.FETCHED_AT = previous_fetched_at


def _nonconclusive_miss_batch(status: QualificationStatus, session_id: str):
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = session_id
    try:
        if status is QualificationStatus.PENDING:
            source = _pending_source()
        elif status is QualificationStatus.CENSORED:
            source = _source(
                bars=_bars_for_truth("flat"),
                halt_status=SafetyStatus.BLOCKED,
            )
        else:
            source = _source(
                bars=_bars_for_truth("flat"),
                halt_status=SafetyStatus.UNKNOWN,
            )
        qualification = qualify_session_opportunities(
            source,
            policy=_policy(),
            horizons=(_horizon(),),
        )
        assert status in {item.status for item in qualification.assessments}
        return reconcile_missed_opportunities(
            qualification,
            session_replay=_session_replay(),
        )
    finally:
        missed_fixtures.SESSION_ID = previous


def _reidentified(contract, identity_field: str, namespace: str, **changes):
    values = {
        field.name: getattr(contract, field.name)
        for field in fields(contract)
        if field.name != identity_field
    }
    values.update(changes)
    return type(contract)(
        **{
            identity_field: stable_identity(namespace, values),
            **values,
        }
    )


def test_canonical_metric_definitions_and_policy_round_trip() -> None:
    definitions = canonical_metric_definitions()
    assert tuple(item.name for item in definitions) == tuple(DiscoveryMetricName)
    assert tuple(item.top_k for item in definitions) == (
        None,
        1,
        3,
        5,
        1,
        3,
        5,
        None,
        None,
    )
    assert all(item.decimal_precision == 64 for item in definitions)
    assert all(item.fraction_scale == 12 for item in definitions)
    assert all(item.fraction_quantizer == Decimal("0.000000000001") for item in definitions)
    policy = _metric_policy()
    assert policy.accepted_claim_kinds == (
        QualificationClaimKind.EXECUTABLE_TRADE,
        QualificationClaimKind.PRICE_MOVE_PROXY,
    )
    assert DiscoveryMetricPolicy.from_json(policy.to_json()) == policy
    assert all(
        DiscoveryMetricDefinition.from_json(item.to_json()) == item
        for item in definitions
    )


def test_metric_fraction_rounding_ignores_ambient_decimal_context() -> None:
    original_precision = getcontext().prec
    original_rounding = getcontext().rounding
    try:
        getcontext().prec = 3
        getcontext().rounding = ROUND_DOWN
        one_third = quantize_metric_fraction(1, 3)
        getcontext().prec = 7
        getcontext().rounding = ROUND_UP
        two_thirds = quantize_metric_fraction(2, 3)
    finally:
        getcontext().prec = original_precision
        getcontext().rounding = original_rounding
    assert one_third == Decimal("0.333333333333")
    assert two_thirds == Decimal("0.666666666667")
    assert quantize_metric_fraction(0, 3) == Decimal("0E-12")
    with pytest.raises(ValueError):
        quantize_metric_fraction(0, 0)


def test_rehashed_alternate_metric_quantization_rejects() -> None:
    definition = canonical_metric_definitions()[0]
    with pytest.raises(ValueError, match="canonical v1 semantics"):
        _reidentified(
            definition,
            "definition_id",
            "discovery-metric-definition",
            fraction_scale=11,
            fraction_quantizer=Decimal("0.00000000001"),
        )


def test_selected_horizon_matching_uses_full_embedded_batch() -> None:
    qualification = qualify_session_opportunities(
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
        horizons=(_horizon(elapsed_seconds=120), _horizon()),
    )
    miss_batch = reconcile_missed_opportunities(
        qualification,
        session_replay=_session_replay(),
    )
    evidence = build_discovery_metric_session_evidence(
        miss_batch,
        policy=_metric_policy(),
    )
    assert len(evidence.miss_batch.qualification_batch.horizons) == 2
    assert all(item.horizon_id == evidence.selected_horizon_id for item in evidence.units)
    assert all(item.assessment.horizon.elapsed_seconds == 300 for item in evidence.units)
    assert DiscoveryMetricSessionEvidence.from_json(evidence.to_json()) == evidence


def test_matching_scans_all_pairs_and_deduplicates_to_strategy_agnostic_units() -> None:
    evidence = build_discovery_metric_session_evidence(
        _miss_batch(multiple_runs=True),
        policy=_metric_policy(),
    )
    assert tuple((item.symbol, item.direction.value) for item in evidence.units) == (
        ("ABC", "long"),
        ("ABC", "short"),
    )
    long_unit = evidence.units[0]
    assert long_unit.qualification_status is QualificationStatus.QUALIFIED
    assert len({item.run_id for item in long_unit.predictions}) == 2
    assert len({item.evaluation_id for item in long_unit.predictions}) == len(
        long_unit.predictions
    )
    assert long_unit.best_on_time_rank_position == min(
        item.rank_position
        for item in long_unit.predictions
        if item.on_time and item.rank_position is not None
    )


def test_ineligible_or_no_assessment_predictions_are_unmatched_not_negative() -> None:
    evidence = build_discovery_metric_session_evidence(
        _miss_batch(),
        policy=_metric_policy(),
    )
    assert tuple(
        item.qualification_status for item in evidence.units
    ) == (QualificationStatus.QUALIFIED, QualificationStatus.NOT_QUALIFIED)
    assert evidence.unmatched_predictions
    assert all(item.symbol == "DEF" for item in evidence.unmatched_predictions)
    assert "unmatched_prediction_truth" in evidence.limitations


def test_public_session_rejects_rehashed_invented_private_prediction_and_unit() -> None:
    from intraday_scanner.v2.opportunity import metrics as metric_facade
    from intraday_scanner.v2.opportunity import miss_metric_matching

    assert "DiscoveryMetricUnitEvidence" not in metric_facade.__all__
    assert "DiscoveryMetricUnitEvidence" not in miss_metric_matching.__all__
    assert not hasattr(metric_facade, "DiscoveryMetricUnitEvidence")
    evidence = build_discovery_metric_session_evidence(
        _miss_batch(),
        policy=_metric_policy(),
    )
    unit = evidence.units[0]
    prediction = unit.predictions[0]
    forged_prediction = _reidentified(
        prediction,
        "prediction_evidence_id",
        "discovery-prediction-evidence",
        run_id="invented-run",
        run_content_hash_sha256=_hash("invented-run"),
        evaluation_id="invented-evaluation",
        evaluation_content_hash_sha256=_hash("invented-evaluation"),
    )
    forged_unit = _reidentified(
        unit,
        "unit_evidence_id",
        "discovery-metric-unit-evidence",
        predictions=(forged_prediction,),
    )
    assert forged_unit.predictions[0].run_id == "invented-run"
    forged_units = (forged_unit, *evidence.units[1:])
    with pytest.raises(ValueError, match="units does not recompute"):
        _reidentified(
            evidence,
            "session_evidence_id",
            "discovery-metric-session-evidence",
            units=forged_units,
        )


def test_core_imports_do_not_load_metric_modules() -> None:
    root = Path(__file__).resolve().parents[1]
    script = """
import importlib
import sys
modules = (
    'intraday_scanner.v2.opportunity',
    'intraday_scanner.v2.opportunity.models',
    'intraday_scanner.v2.opportunity.features',
    'intraday_scanner.v2.opportunity.pipeline',
    'intraday_scanner.storage.opportunity_store',
    'intraday_scanner.storage.opportunity_outcome_store',
)
for name in modules:
    importlib.import_module(name)
loaded = tuple(name for name in sys.modules if '.miss_metric' in name or name.endswith('.metrics'))
assert loaded == (), loaded
importlib.import_module('intraday_scanner.v2.opportunity.metrics')
assert 'intraday_scanner.v2.opportunity.metrics' in sys.modules
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=root,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


def _value(report, name: DiscoveryMetricName):
    return next(item for item in report.values if item.definition.name is name)


def test_session_metric_formulas_are_hand_calculable_and_exact() -> None:
    report = reconcile_session_discovery_metrics(
        _miss_batch(),
        policy=_metric_policy(),
    )
    expected = {
        DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL: (0, 1, Decimal("0E-12")),
        DiscoveryMetricName.TOP_1_RECALL: (1, 1, Decimal("1.000000000000")),
        DiscoveryMetricName.TOP_3_RECALL: (1, 1, Decimal("1.000000000000")),
        DiscoveryMetricName.TOP_5_RECALL: (1, 1, Decimal("1.000000000000")),
        DiscoveryMetricName.PRECISION_AT_1: (1, 1, Decimal("1.000000000000")),
        DiscoveryMetricName.FALSE_POSITIVE_RATE: (0, 1, Decimal("0E-12")),
        DiscoveryMetricName.NO_TRADE_ACCURACY: (0, 1, Decimal("0E-12")),
    }
    assert tuple(item.definition.name for item in report.values) == tuple(
        DiscoveryMetricName
    )
    for name, calculation in expected.items():
        metric = _value(report, name)
        assert metric.status.value == "available"
        assert (metric.numerator_count, metric.denominator_count, metric.value) == calculation
        assert len(metric.numerator_unit_ids) == calculation[0]
        assert len(metric.denominator_unit_ids) == calculation[1]
    assert _value(report, DiscoveryMetricName.PRECISION_AT_3).status.value == "unavailable"
    assert _value(report, DiscoveryMetricName.PRECISION_AT_5).status.value == "unavailable"
    assert report.qualified_executable_trade_count == 0
    assert report.qualified_price_move_proxy_count == 1
    assert SessionDiscoveryMetricReport.from_json(report.to_json()) == report


def test_all_nine_metrics_and_rank_boundaries_use_production_pairs() -> None:
    report = reconcile_session_discovery_metrics(
        _rank_truth_miss_batch("XNYS-2026-08-11-RANK-MATRIX", repeated=True),
        policy=_metric_policy(),
    )
    expected = {
        DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL: (
            "available",
            3,
            3,
            Decimal("1.000000000000"),
        ),
        DiscoveryMetricName.TOP_1_RECALL: (
            "available",
            1,
            3,
            Decimal("0.333333333333"),
        ),
        DiscoveryMetricName.TOP_3_RECALL: (
            "available",
            2,
            3,
            Decimal("0.666666666667"),
        ),
        DiscoveryMetricName.TOP_5_RECALL: (
            "available",
            3,
            3,
            Decimal("1.000000000000"),
        ),
        DiscoveryMetricName.PRECISION_AT_1: (
            "available",
            1,
            1,
            Decimal("1.000000000000"),
        ),
        DiscoveryMetricName.PRECISION_AT_3: (
            "available",
            2,
            3,
            Decimal("0.666666666667"),
        ),
        DiscoveryMetricName.PRECISION_AT_5: (
            "available",
            3,
            5,
            Decimal("0.600000000000"),
        ),
        DiscoveryMetricName.FALSE_POSITIVE_RATE: (
            "available",
            3,
            9,
            Decimal("0.333333333333"),
        ),
        DiscoveryMetricName.NO_TRADE_ACCURACY: (
            "insufficient",
            0,
            0,
            None,
        ),
    }
    assert {
        item.definition.name: (
            item.status.value,
            item.numerator_count,
            item.denominator_count,
            item.value,
        )
        for item in report.values
    } == expected

    long_units = tuple(
        item
        for item in report.session_evidence.units
        if item.direction.value == "long"
    )
    assert tuple(item.best_on_time_rank_position for item in long_units) == tuple(
        range(1, 7)
    )
    assert tuple(
        item.best_on_time_rank_position <= 1 for item in long_units
    ) == (True, False, False, False, False, False)
    assert tuple(
        item.best_on_time_rank_position <= 3 for item in long_units
    ) == (True, True, True, False, False, False)
    assert tuple(
        item.best_on_time_rank_position <= 5 for item in long_units
    ) == (True, True, True, True, True, False)
    assert all(
        len({prediction.run_id for prediction in item.predictions}) >= 1
        for item in long_units
    )
    assert len(_value(report, DiscoveryMetricName.TOP_5_RECALL).denominator_unit_ids) == 3


def test_equal_ranking_inputs_have_unique_deterministic_rank_tie_breaks() -> None:
    result = _six_rank_current_replay().pipeline_result
    ranks = result.ranked_opportunities
    tied = ranks[1:]
    assert len({item.final_score for item in tied}) == 1
    assert tuple(item.symbol for item in tied) == tuple(
        sorted(item.symbol for item in tied)
    )
    assert tuple(item.relative_rank for item in ranks) == tuple(range(1, 7))
    rebuilt = _six_rank_current_replay().pipeline_result
    assert tuple(
        (item.symbol, item.strategy_id, item.relative_rank, item.ranked_id)
        for item in rebuilt.ranked_opportunities
    ) == tuple(
        (item.symbol, item.strategy_id, item.relative_rank, item.ranked_id)
        for item in ranks
    )


def test_surface_false_positive_is_matched_not_unmatched_or_unevaluated() -> None:
    report = reconcile_session_discovery_metrics(
        _rank_truth_miss_batch("XNYS-2026-08-11-FALSE-POSITIVE"),
        policy=_metric_policy(),
    )
    false_positive = _value(report, DiscoveryMetricName.FALSE_POSITIVE_RATE)
    precision = _value(report, DiscoveryMetricName.PRECISION_AT_3)
    assert (false_positive.numerator_count, false_positive.denominator_count) == (3, 9)
    assert false_positive.value == Decimal("0.333333333333")
    assert (precision.numerator_count, precision.denominator_count) == (2, 3)
    assert precision.value == Decimal("0.666666666667")
    negative_units = tuple(
        item
        for item in report.session_evidence.units
        if item.qualification_status is QualificationStatus.NOT_QUALIFIED
    )
    surfaced = tuple(item for item in negative_units if item.on_time_watch_or_take)
    unevaluated = tuple(item for item in negative_units if not item.predictions)
    assert len(surfaced) == 3
    assert len(unevaluated) == 6
    assert report.session_evidence.unmatched_predictions == ()


def test_long_short_multiple_qualified_and_repeated_multistrategy_runs_deduplicate() -> None:
    report = reconcile_session_discovery_metrics(
        _long_short_miss_batch("XNYS-2026-08-11-LONG-SHORT"),
        policy=_metric_policy(),
    )
    qualified = tuple(
        item
        for item in report.session_evidence.units
        if item.qualification_status is QualificationStatus.QUALIFIED
    )
    assert {(item.symbol, item.direction.value) for item in qualified} == {
        ("ABC", "long"),
        ("DEF", "short"),
    }
    stored_strategies = {
        evaluation.strategy_id
        for replay in report.session_evidence.miss_batch.session_replay.current_outcome_replays
        for evaluation in replay.pipeline_result.evaluations
    }
    assert len(stored_strategies) >= 2
    assert len(report.session_evidence.miss_batch.session_replay.current_outcome_replays) == 2
    assert len({item.unit_evidence_id for item in report.session_evidence.units}) == len(
        report.session_evidence.units
    )
    assert _value(report, DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL).denominator_count == 2
    long_unit = next(item for item in qualified if item.direction.value == "long")
    short_unit = next(item for item in qualified if item.direction.value == "short")
    assert long_unit.predictions
    assert short_unit.predictions


def test_two_matching_strategies_collapse_to_one_strategy_agnostic_unit() -> None:
    report = reconcile_session_discovery_metrics(
        _multi_strategy_miss_batch("XNYS-2026-08-11-MULTI-STRATEGY"),
        policy=_metric_policy(),
    )
    unit = next(
        item
        for item in report.session_evidence.units
        if item.symbol == "ABC" and item.direction.value == "long"
    )
    result = report.session_evidence.miss_batch.session_replay.current_outcome_replays[
        0
    ].pipeline_result
    strategy_by_evaluation = {
        item.evaluation_id: item.strategy_id for item in result.evaluations
    }
    assert {
        strategy_by_evaluation[item.evaluation_id] for item in unit.predictions
    } == {"DS-MOM-001", "DS-MOM-001-ALT"}
    assert len(unit.predictions) == 2
    assert _value(report, DiscoveryMetricName.TOP_5_RECALL).denominator_count == 1


def test_complete_negative_session_has_honest_zero_denominators_and_no_trade() -> None:
    report = reconcile_session_discovery_metrics(
        _negative_miss_batch(),
        policy=_metric_policy(),
    )
    recall = _value(report, DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL)
    assert recall.status.value == "insufficient"
    assert (recall.numerator_count, recall.denominator_count, recall.value) == (0, 0, None)
    precision = _value(report, DiscoveryMetricName.PRECISION_AT_1)
    assert (precision.numerator_count, precision.denominator_count, precision.value) == (
        0,
        1,
        Decimal("0E-12"),
    )
    false_positive = _value(report, DiscoveryMetricName.FALSE_POSITIVE_RATE)
    assert (false_positive.numerator_count, false_positive.denominator_count) == (0, 2)
    no_trade = _value(report, DiscoveryMetricName.NO_TRADE_ACCURACY)
    assert (no_trade.numerator_count, no_trade.denominator_count, no_trade.value) == (
        1,
        1,
        Decimal("1.000000000000"),
    )


def test_multi_session_no_trade_accuracy_counts_correct_and_false_no_trade() -> None:
    batches = (
        _session_miss_batch("XNYS-2026-08-11-FALSE-NO-TRADE"),
        _negative_session_miss_batch("XNYS-2026-08-11-CORRECT-NO-TRADE-1"),
        _negative_session_miss_batch("XNYS-2026-08-11-CORRECT-NO-TRADE-2"),
    )
    report = reconcile_discovery_metrics(batches, policy=_metric_policy())
    metric = _value(report, DiscoveryMetricName.NO_TRADE_ACCURACY)
    assert (metric.numerator_count, metric.denominator_count, metric.value) == (
        2,
        3,
        Decimal("0.666666666667"),
    )
    assert set(metric.denominator_unit_ids) == {
        item.session_evidence_id for item in report.session_reports
    }


def test_multi_session_precision_fpr_and_top_recall_are_micro_aggregates() -> None:
    batches = (
        _rank_truth_miss_batch("XNYS-2026-08-11-MICRO-1"),
        _rank_truth_miss_batch("XNYS-2026-08-11-MICRO-2"),
    )
    report = reconcile_discovery_metrics(batches, policy=_metric_policy())
    expected = {
        DiscoveryMetricName.TOP_1_RECALL: (2, 6, Decimal("0.333333333333")),
        DiscoveryMetricName.TOP_3_RECALL: (4, 6, Decimal("0.666666666667")),
        DiscoveryMetricName.PRECISION_AT_3: (4, 6, Decimal("0.666666666667")),
        DiscoveryMetricName.PRECISION_AT_5: (6, 10, Decimal("0.600000000000")),
        DiscoveryMetricName.FALSE_POSITIVE_RATE: (6, 18, Decimal("0.333333333333")),
    }
    for name, exact in expected.items():
        metric = _value(report, name)
        assert (metric.numerator_count, metric.denominator_count, metric.value) == exact
        assert len(metric.numerator_unit_ids) == exact[0]
        assert len(metric.denominator_unit_ids) == exact[1]


def test_mixed_complete_and_incomplete_sessions_fail_closed_without_exclusion() -> None:
    complete = _session_miss_batch("XNYS-2026-08-11-MIXED-COMPLETE")
    previous = missed_fixtures.SESSION_ID
    missed_fixtures.SESSION_ID = "XNYS-2026-08-11-MIXED-INCOMPLETE"
    try:
        incomplete = reconcile_missed_opportunities(
            _qualified_batch(),
            session_replay=_session_replay(authoritative=False),
        )
    finally:
        missed_fixtures.SESSION_ID = previous
    report = reconcile_discovery_metrics(
        (complete, incomplete),
        policy=_metric_policy(),
    )
    assert len(report.session_reports) == 2
    assert all(item.status.value == "unavailable" for item in report.values)
    assert all(item.denominator_count is None for item in report.values)


@pytest.mark.parametrize(
    "status",
    (
        QualificationStatus.PENDING,
        QualificationStatus.CENSORED,
        QualificationStatus.UNAVAILABLE,
    ),
)
def test_mixed_nonconclusive_qualification_sessions_retain_exact_blockers(status) -> None:
    complete = _session_miss_batch(f"XNYS-2026-08-11-COMPLETE-{status.value}")
    nonconclusive = _nonconclusive_miss_batch(
        status,
        f"XNYS-2026-08-11-NONCONCLUSIVE-{status.value}",
    )
    report = reconcile_discovery_metrics(
        (complete, nonconclusive),
        policy=_metric_policy(),
    )
    blocking_assessments = {
        item.assessment_id
        for item in report.session_reports[1].session_evidence.units
        if item.qualification_status
        not in {QualificationStatus.QUALIFIED, QualificationStatus.NOT_QUALIFIED}
    }
    assert blocking_assessments
    assert all(item.status.value == "unavailable" for item in report.values)
    assert all(
        blocking_assessments.issubset(item.blocking_evidence_ids)
        for item in report.values
    )


def test_cutoff_equality_is_late_for_rank_and_watch_matching() -> None:
    report = reconcile_session_discovery_metrics(
        _cutoff_equality_miss_batch(),
        policy=_metric_policy(),
    )
    long_unit = next(
        item for item in report.session_evidence.units if item.direction.value == "long"
    )
    assert long_unit.predictions
    assert all(
        item.decision_at == long_unit.latest_useful_cutoff_at
        for item in long_unit.predictions
    )
    assert all(not item.on_time for item in long_unit.predictions)
    assert long_unit.best_on_time_rank_position is None
    assert not long_unit.on_time_watch_or_take
    assert _value(report, DiscoveryMetricName.TOP_1_RECALL).numerator_count == 0


def test_combined_report_retains_executable_and_proxy_claim_counts() -> None:
    report = reconcile_discovery_metrics(
        (
            _session_miss_batch("XNYS-2026-08-11-PROXY-CLAIM"),
            _executable_session_miss_batch("XNYS-2026-08-11-EXECUTABLE-CLAIM"),
        ),
        policy=_metric_policy(),
    )
    assert report.qualified_executable_trade_count == 1
    assert report.qualified_price_move_proxy_count == 1
    recall = _value(report, DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL)
    assert recall.denominator_executable_trade_count == 1
    assert recall.denominator_price_move_proxy_count == 1


def test_incomplete_inventory_makes_all_metric_populations_unavailable() -> None:
    miss_batch = reconcile_missed_opportunities(
        _qualified_batch(),
        session_replay=_session_replay(authoritative=False),
    )
    report = reconcile_session_discovery_metrics(
        miss_batch,
        policy=_metric_policy(),
    )
    assert all(item.status.value == "unavailable" for item in report.values)
    assert all(
        item.numerator_count is item.denominator_count is item.value is None
        for item in report.values
    )
    assert all(item.blocking_evidence_ids for item in report.values)


@pytest.mark.parametrize(
    ("caught_count", "expected"),
    ((1, "0.333333333333"), (2, "0.666666666667")),
)
def test_multi_session_recall_is_exact_micro_aggregate(caught_count, expected) -> None:
    batches = tuple(
        _session_miss_batch(
            f"XNYS-2026-08-11-METRIC-{index}",
            caught=index < caught_count,
        )
        for index in range(3)
    )
    report = reconcile_discovery_metrics(batches, policy=_metric_policy())
    recall = _value(report, DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL)
    assert (recall.numerator_count, recall.denominator_count, recall.value) == (
        caught_count,
        3,
        Decimal(expected),
    )
    assert len(set(recall.denominator_unit_ids)) == 3
    assert report.qualified_price_move_proxy_count == 3
    assert report.recorded_at == max(item.recorded_at for item in report.session_reports)
    assert DiscoveryMetricReport.from_json(report.to_json()) == report


def test_multi_session_rejects_duplicate_current_session_inputs() -> None:
    batch = _miss_batch()
    with pytest.raises(ValueError, match="metric report session"):
        reconcile_discovery_metrics((batch, batch), policy=_metric_policy())


def test_empty_multi_session_cohort_is_explicitly_insufficient() -> None:
    report = reconcile_discovery_metrics((), policy=_metric_policy())
    assert report.session_reports == ()
    assert report.recorded_at is None
    assert all(item.status.value == "insufficient" for item in report.values)
    assert all(item.reason == "empty_metric_cohort" for item in report.values)
    assert DiscoveryMetricReport.from_json(report.to_json()) == report


def test_report_rejects_consistently_rehashed_metric_formula_and_status_tamper() -> None:
    report = reconcile_session_discovery_metrics(
        _miss_batch(),
        policy=_metric_policy(),
    )
    original = report.values[0]
    forged_value = _reidentified(
        original,
        "metric_value_id",
        "discovery-metric-value",
        numerator_count=1,
        value=Decimal("1.000000000000"),
        numerator_unit_ids=original.denominator_unit_ids,
        numerator_price_move_proxy_count=1,
    )
    forged_values = (forged_value, *report.values[1:])
    with pytest.raises(ValueError, match="values does not recompute"):
        _reidentified(
            report,
            "report_id",
            "session-discovery-metric-report",
            values=forged_values,
        )


def test_report_strict_json_and_cohort_order_identity_tamper_rejects() -> None:
    report = reconcile_discovery_metrics(
        (
            _rank_truth_miss_batch("XNYS-2026-08-11-TAMPER-1"),
            _rank_truth_miss_batch("XNYS-2026-08-11-TAMPER-2"),
        ),
        policy=_metric_policy(),
    )
    top = report.to_dict()
    top["unknown_metric_field"] = "injected"
    with pytest.raises(ValueError, match="unknown field"):
        DiscoveryMetricReport.from_dict(top)

    floating = report.to_dict()
    floating["values"][0]["value"] = 0.5  # type: ignore[index]
    with pytest.raises((TypeError, ValueError)):
        DiscoveryMetricReport.from_dict(floating)

    encoded = report.to_json()
    duplicate = encoded.replace(
        '"report_id":',
        '"report_id":"duplicate","report_id":',
        1,
    )
    with pytest.raises(ValueError, match="duplicate JSON key"):
        DiscoveryMetricReport.from_json(duplicate)

    with pytest.raises(ValueError, match="canonical order"):
        _reidentified(
            report,
            "report_id",
            "discovery-metric-report",
            session_reports=tuple(reversed(report.session_reports)),
        )
    with pytest.raises(ValueError, match="cohort identity"):
        _reidentified(
            report,
            "report_id",
            "discovery-metric-report",
            cohort_id="discovery-metric-cohort:000000000000000000000000",
        )


def test_session_evidence_and_metric_population_omission_rehash_rejects() -> None:
    report = reconcile_session_discovery_metrics(
        _rank_truth_miss_batch("XNYS-2026-08-11-SESSION-TAMPER"),
        policy=_metric_policy(),
    )
    evidence = report.session_evidence
    with pytest.raises(ValueError, match="units does not recompute"):
        _reidentified(
            evidence,
            "session_evidence_id",
            "discovery-metric-session-evidence",
            units=evidence.units[:-1],
        )

    precision = _value(report, DiscoveryMetricName.PRECISION_AT_5)
    forged = _reidentified(
        precision,
        "metric_value_id",
        "discovery-metric-value",
        numerator_count=2,
        denominator_count=4,
        value=Decimal("0.500000000000"),
        numerator_unit_ids=precision.numerator_unit_ids[:-1],
        denominator_unit_ids=precision.denominator_unit_ids[:-1],
        numerator_price_move_proxy_count=2,
        denominator_price_move_proxy_count=2,
    )
    values = tuple(
        forged if item.definition.name is DiscoveryMetricName.PRECISION_AT_5 else item
        for item in report.values
    )
    with pytest.raises(ValueError, match="values does not recompute"):
        _reidentified(
            report,
            "report_id",
            "session-discovery-metric-report",
            values=values,
        )

    unavailable_report = reconcile_session_discovery_metrics(
        _miss_batch(),
        policy=_metric_policy(),
    )
    unavailable = _value(unavailable_report, DiscoveryMetricName.PRECISION_AT_3)
    forged_status = _reidentified(
        unavailable,
        "metric_value_id",
        "discovery-metric-value",
        status=type(unavailable.status).INSUFFICIENT,
        numerator_count=0,
        denominator_count=0,
        numerator_executable_trade_count=0,
        numerator_price_move_proxy_count=0,
        denominator_executable_trade_count=0,
        denominator_price_move_proxy_count=0,
        blocking_evidence_ids=(),
        reason="no_top_k_predictions",
    )
    forged_values = tuple(
        forged_status if item.definition.name is DiscoveryMetricName.PRECISION_AT_3 else item
        for item in unavailable_report.values
    )
    with pytest.raises(ValueError, match="values does not recompute"):
        _reidentified(
            unavailable_report,
            "report_id",
            "session-discovery-metric-report",
            values=forged_values,
        )


def test_future_mutation_changes_metric_not_stored_run_identities() -> None:
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
    original_miss = reconcile_missed_opportunities(
        original_qualification,
        session_replay=replay,
    )
    changed_miss = reconcile_missed_opportunities(
        changed_qualification,
        session_replay=replay,
    )
    original = reconcile_session_discovery_metrics(
        original_miss,
        policy=_metric_policy(),
    )
    changed = reconcile_session_discovery_metrics(
        changed_miss,
        policy=_metric_policy(),
    )
    stored = replay.current_outcome_replays[0].pipeline_result
    original_run_ids = (
        stored.run_id,
        tuple(item.evaluation_id for item in stored.evaluations),
        tuple(item.ranked_id for item in stored.ranked_opportunities),
        tuple(item.decision_id for item in stored.decisions),
        tuple(item.trace_id for item in stored.traces),
    )
    assert original.report_id != changed.report_id
    assert original.session_evidence_id != changed.session_evidence_id
    assert original.session_evidence.miss_batch.session_replay == replay
    assert changed.session_evidence.miss_batch.session_replay == replay
    unchanged = changed.session_evidence.miss_batch.session_replay.current_outcome_replays[
        0
    ].pipeline_result
    assert original_run_ids == (
        unchanged.run_id,
        tuple(item.evaluation_id for item in unchanged.evaluations),
        tuple(item.ranked_id for item in unchanged.ranked_opportunities),
        tuple(item.decision_id for item in unchanged.decisions),
        tuple(item.trace_id for item in unchanged.traces),
    )
