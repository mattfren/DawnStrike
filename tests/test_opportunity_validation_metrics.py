from __future__ import annotations

import ast
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import ROUND_DOWN, Decimal, Inexact, Rounded, localcontext
from pathlib import Path
from types import SimpleNamespace

import pytest

import intraday_scanner.v2.opportunity.validation_metric_segments as metric_segments
from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.data_truth.intraday import (
    IntradayCoverageReceipt,
    IntradayCoverageStatus,
)
from intraday_scanner.v2.opportunity.models import (
    Availability,
    StrategyDirection,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_resolution import (
    _build_metrics,
    _path_details,
    _resolve_trade_path,
    _touch_interval,
)
from intraday_scanner.v2.opportunity.outcomes import (
    OutcomeCompleteness,
    OutcomeHorizonKind,
    OutcomeMetric,
    OutcomePathStatus,
    OutcomeRecord,
    OutcomeReferencePriceKind,
    build_outcome_horizon,
    build_outcome_observation_series,
)
from intraday_scanner.v2.opportunity.risk import (
    QuoteEvidenceScope,
    RiskMetric,
    RiskValueStatus,
)
from intraday_scanner.v2.opportunity.validation_audit import (
    build_chronological_validation_preparation,
)
from intraday_scanner.v2.opportunity.validation_contracts import (
    build_validation_split_policy,
)
from intraday_scanner.v2.opportunity.validation_corpus import (
    build_validation_corpus,
)
from intraday_scanner.v2.opportunity.validation_metric_calculations import (
    _calculate_metric_values,
    _calculate_one,
    _MetricCalculationInput,
    _session_drawdown,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _timedelta_decimal_seconds,
)
from intraday_scanner.v2.opportunity.validation_metric_population import (
    _cost_limitation,
    _cost_quality,
    _holding_bounds,
    _stress_scenarios_supported,
    build_execution_stress_trade_evidence,
)
from intraday_scanner.v2.opportunity.validation_metric_segments import (
    _BoundValidationMetricRow,
    _build_segments,
    _liquidity_bucket,
    _month_bucket,
    _segment_buckets,
    _volatility_bucket,
    _year_bucket,
)
from intraday_scanner.v2.opportunity.validation_metrics import (
    ExecutionCostEvidenceQuality,
    ExecutionStressScenario,
    TradeMetricDisposition,
    ValidationMetricReportStatus,
    ValidationMetricScopeKind,
    ValidationMetricValueStatus,
    ValidationSegmentDimension,
    ValidationTradingMetric,
    build_validation_trading_metric_policy,
    build_validation_trading_metric_report,
)
from tests import test_opportunity_outcomes as outcome_fixtures
from tests import test_opportunity_validation as validation_fixtures
from tests.test_opportunity_universe_risk import (
    _base_risk_metrics,
    _empirical_evaluation,
    _execution_risk,
    _gate_one,
)

UTC = timezone.utc


@contextmanager
def _hostile_decimal_context(precision: int):
    with localcontext() as context:
        context.prec = precision
        context.rounding = ROUND_DOWN
        context.Emin = -9
        context.Emax = 9
        context.clamp = 1
        context.traps[Inexact] = True
        context.traps[Rounded] = True
        yield context


def _explicit_take_outcome(
    *,
    direction: StrategyDirection = StrategyDirection.LONG,
    path: str = "target",
) -> OutcomeRecord:
    stop = Decimal("98") if direction is StrategyDirection.LONG else Decimal("102")
    target = Decimal("106") if direction is StrategyDirection.LONG else Decimal("94")
    evaluation = replace(
        _empirical_evaluation(
            evaluation_id=f"evaluation-metric-{direction.value}-{path}"
        ),
        direction=direction,
        invalidation_price=stop,
        target_price=target,
    )
    risk = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(stop=stop, target=target),
    )
    take = _gate_one(evaluation, risk)
    assert take.decision is TradeDecisionValue.TAKE
    template = next(
        item
        for item in outcome_fixtures._pending_terminal_batch().outcomes
        if item.direction is StrategyDirection.LONG
    )
    payload = template.to_dict()
    horizon = build_outcome_horizon(
        decision_at=take.decision_at,
        exchange_session_id="XNYS-2026-08-11",
        session_open_at=datetime(2026, 8, 11, 14, 30, tzinfo=UTC),
        session_close_at=datetime(2026, 8, 11, 21, 0, tzinfo=UTC),
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=301,
    )
    bars = {
        (StrategyDirection.LONG, "target"): (
            ("100", "101", "99.5", "100.4"),
            ("101", "106.2", "100.5", "106"),
        ),
        (StrategyDirection.LONG, "stop"): (
            ("100", "101", "99.5", "100.4"),
            ("99", "100.5", "97.8", "98"),
        ),
        (StrategyDirection.LONG, "horizon"): (
            ("100", "101", "99.5", "100.4"),
            ("100.4", "101.5", "99", "101"),
            ("101", "102", "99.5", "101.5"),
            ("101.5", "103", "100", "102"),
            ("102", "104", "101", "103"),
        ),
        (StrategyDirection.SHORT, "target"): (
            ("100", "100.5", "99", "99.6"),
            ("99", "99.5", "93.8", "94"),
        ),
        (StrategyDirection.SHORT, "stop"): (
            ("100", "100.5", "99", "99.6"),
            ("101", "102.2", "99.5", "102"),
        ),
        (StrategyDirection.SHORT, "horizon"): (
            ("100", "100.5", "99", "99.6"),
            ("99.6", "101", "98.5", "99"),
            ("99", "100.5", "98", "98.5"),
            ("98.5", "100", "97", "98"),
            ("98", "99", "95", "97"),
        ),
        (StrategyDirection.LONG, "no_entry"): (
            ("99", "99.5", "98.5", "99"),
            ("99", "99.5", "98.5", "99"),
            ("99", "99.5", "98.5", "99"),
            ("99", "99.5", "98.5", "99"),
            ("99", "99.5", "98.5", "99"),
        ),
        (StrategyDirection.LONG, "ambiguous"): (
            ("100", "106.2", "99.5", "101"),
            ("101", "102", "100", "101"),
            ("101", "102", "100", "101"),
            ("101", "102", "100", "101"),
            ("101", "102", "100", "101"),
        ),
    }[(direction, path)]
    observations = outcome_fixtures._path_observations(take, bars)
    source = observations[0].bar.source_metadata
    coverage = IntradayCoverageReceipt(
        coverage_receipt_id="coverage-validation-metric-take",
        provider=source.provider,
        feed=source.feed,
        entitlement=source.entitlement,
        symbol=take.symbol,
        market_date="2026-08-11",
        exchange_session_id=source.exchange_session_id,
        request_start=source.request_start,
        request_end=source.request_end,
        status=IntradayCoverageStatus.COMPLETE,
        source_metadata=source,
        observed_start=observations[0].interval_start_at,
        observed_end=observations[-1].interval_end_at,
        reason="validation_metric_take_fixture_coverage",
        created_at=source.fetched_at,
    )
    series = build_outcome_observation_series(
        symbol=take.symbol,
        exchange_session_id=source.exchange_session_id,
        decision_at=take.decision_at,
        requested_through_at=observations[-1].interval_end_at,
        coverage_receipt=coverage,
        observations=observations,
        source_identity="validation_metric_take_fixture_source",
        method="retained_post_decision_take_fixture",
    )
    entry_status, path_status = _resolve_trade_path(take, observations)
    path_details = _path_details(take, observations, entry_status, path_status)
    completeness = {
        "horizon": OutcomeCompleteness.COMPLETE,
        "no_entry": OutcomeCompleteness.COMPLETE,
        "ambiguous": OutcomeCompleteness.CENSORED,
    }.get(path, OutcomeCompleteness.PARTIAL)
    complete_source = path in {"horizon", "no_entry", "ambiguous"}
    metrics = _build_metrics(
        decision=take,
        risk=risk,
        reference=observations[0],
        close_observation=observations[-1] if complete_source else None,
        observations=observations,
        completeness=completeness,
        entry_status=entry_status,
        path_status=path_status,
        path=path_details,
    )
    payload.update(
        {
            "evaluation_id": take.evaluation_id,
            "evaluation_content_hash_sha256": take.evaluation.content_hash(),
            "decision_id": take.decision_id,
            "decision_content_hash_sha256": take.content_hash(),
            "decision": take.to_dict(),
            "risk_evidence_id": risk.execution_risk_evidence_id,
            "risk_evidence_content_hash_sha256": risk.content_hash(),
            "risk_evidence": risk.to_dict(),
            "decision_at": take.decision_at.isoformat(),
            "symbol": take.symbol,
            "strategy_id": take.strategy_id,
            "strategy_version": take.strategy_version,
            "direction": take.direction.value,
            "decision_value": take.decision.value,
            "horizon_id": horizon.horizon_id,
            "horizon_content_hash_sha256": horizon.content_hash(),
            "horizon": horizon.to_dict(),
            "source_series_id": series.series_id,
            "source_series_content_hash_sha256": series.content_hash(),
            "source_series": series.to_dict(),
            "source_frozen_at": source.fetched_at.isoformat(),
            "recorded_at": source.fetched_at.isoformat(),
            "completeness": completeness.value,
            "entry_status": entry_status.value,
            "path_status": path_status.value,
            "reference_price_kind": (
                OutcomeReferencePriceKind.FIRST_POST_DECISION_OPEN.value
            ),
            "reference_price": str(observations[0].bar.open_price),
            "reference_observation_id": observations[0].observation_id,
            "reference_observation_content_hash_sha256": observations[0].content_hash(),
            "horizon_close_price": (
                str(observations[-1].bar.close_price) if complete_source else None
            ),
            "horizon_close_observation_id": (
                observations[-1].observation_id if complete_source else None
            ),
            "horizon_close_observation_content_hash_sha256": (
                observations[-1].content_hash() if complete_source else None
            ),
            "modeled_entry_price": (
                str(path_details.entry_price)
                if path_details.entry_price is not None
                else None
            ),
            "modeled_exit_price": (
                str(path_details.exit_price) if path_details.exit_price is not None else None
            ),
            "entry_interval": (
                _touch_interval(path_details.entry_observation).to_dict()
                if path_details.entry_observation is not None
                else None
            ),
            "exit_interval": (
                _touch_interval(path_details.exit_observation).to_dict()
                if path_details.exit_observation is not None
                else None
            ),
            "target_touch_interval": (
                _touch_interval(path_details.target_observation).to_dict()
                if path_details.target_observation is not None
                else None
            ),
            "stop_touch_interval": (
                _touch_interval(path_details.stop_observation).to_dict()
                if path_details.stop_observation is not None
                else None
            ),
            "source_observations": [item.to_dict() for item in observations],
            "source_observation_ids": [item.observation_id for item in observations],
            "source_observation_content_hashes": [
                item.content_hash() for item in observations
            ],
            "metrics": [item.to_dict() for item in metrics],
            "reasons": (
                ["terminal_path_resolved_before_horizon_end"]
                if completeness is OutcomeCompleteness.PARTIAL
                else ([path_status.value] if completeness is OutcomeCompleteness.CENSORED else [])
            ),
            "limitations": ["retrospective_bar_interval_resolution"],
            "promotion_eligible": False,
        }
    )
    return OutcomeRecord.from_dict(outcome_fixtures._rehash_record_payload(payload))


def test_take_target_first_base_2x_3x_stress_is_exact_and_monotone() -> None:
    outcome = _explicit_take_outcome()
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    evidence = build_execution_stress_trade_evidence(outcome, policy=policy)

    assert evidence.disposition is TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE
    assert evidence.cost_evidence_quality is ExecutionCostEvidenceQuality.EMPIRICAL
    assert evidence.gross_r == Decimal("3")
    assert evidence.maximum_favorable_excursion_r == Decimal("3")
    assert evidence.maximum_adverse_excursion_r == Decimal("0")
    assert (
        tuple(item.scenario for item in evidence.stress_scenarios)
        == tuple(ExecutionStressScenario)
    )
    base, cost_2x, cost_3x = evidence.stress_scenarios
    assert (base.per_share_cost, cost_2x.per_share_cost, cost_3x.per_share_cost) == (
        Decimal("0.22"),
        Decimal("0.44"),
        Decimal("0.66"),
    )
    assert (
        base.total_round_trip_cost,
        cost_2x.total_round_trip_cost,
        cost_3x.total_round_trip_cost,
    ) == (Decimal("22.00"), Decimal("44.00"), Decimal("66.00"))
    assert base.after_cost_r_unquantized == next(
        item.value
        for item in outcome.metrics
        if item.metric is OutcomeMetric.SIMULATED_AFTER_COST_R
    )
    assert (
        base.after_cost_r,
        cost_2x.after_cost_r,
        cost_3x.after_cost_r,
    ) == (
        Decimal("2.603603603604"),
        Decimal("2.278688524590"),
        Decimal("2.007518796992"),
    )
    assert (
        cost_3x.after_cost_r_unquantized
        <= cost_2x.after_cost_r_unquantized
        <= base.after_cost_r_unquantized
    )
    assert evidence.promotion_eligible is False
    assert evidence == type(evidence).from_json(evidence.to_json())


@pytest.mark.parametrize(
    ("direction", "path", "expected_path", "expected_gross"),
    (
        (
            StrategyDirection.LONG,
            "stop",
            OutcomePathStatus.STOP_FIRST,
            Decimal("-1"),
        ),
        (
            StrategyDirection.SHORT,
            "stop",
            OutcomePathStatus.STOP_FIRST,
            Decimal("-1"),
        ),
        (
            StrategyDirection.LONG,
            "horizon",
            OutcomePathStatus.HORIZON_EXIT,
            Decimal("1.5"),
        ),
        (
            StrategyDirection.SHORT,
            "horizon",
            OutcomePathStatus.HORIZON_EXIT,
            Decimal("1.5"),
        ),
    ),
)
def test_base_uses_realized_long_short_stop_and_horizon_path(
    direction: StrategyDirection,
    path: str,
    expected_path: OutcomePathStatus,
    expected_gross: Decimal,
) -> None:
    outcome = _explicit_take_outcome(direction=direction, path=path)
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    evidence = build_execution_stress_trade_evidence(outcome, policy=policy)

    assert outcome.path_status is expected_path
    assert evidence.gross_r == expected_gross
    base = evidence.stress_scenarios[0]
    accepted = next(
        item.value
        for item in outcome.metrics
        if item.metric is OutcomeMetric.SIMULATED_AFTER_COST_R
    )
    assert base.after_cost_r_unquantized == accepted
    assert base.after_cost_r_unquantized != (
        outcome.risk_evidence.metric(  # type: ignore[union-attr]
            RiskMetric.AFTER_COST_REWARD_RISK
        ).value
    )


def test_provisional_non_nbbo_and_missing_cost_truth_are_not_stress_scenarios() -> None:
    evaluation = _empirical_evaluation(evaluation_id="metric-cost-quality")
    provisional = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(spread_status=RiskValueStatus.PROVISIONAL),
        quote_scope=QuoteEvidenceScope.PROVISIONAL,
    )
    non_nbbo = _execution_risk(
        evaluation=evaluation,
        quote_scope=QuoteEvidenceScope.NONCONSOLIDATED,
    )
    missing = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(quantity=None),
    )

    assert _cost_quality(provisional) is ExecutionCostEvidenceQuality.PROVISIONAL
    assert (
        _cost_quality(non_nbbo)
        is ExecutionCostEvidenceQuality.NONCONSOLIDATED
    )
    assert _cost_quality(missing) is ExecutionCostEvidenceQuality.UNAVAILABLE
    assert not _stress_scenarios_supported(provisional)
    assert not _stress_scenarios_supported(non_nbbo)
    assert not _stress_scenarios_supported(missing)
    assert provisional.vetoes
    assert non_nbbo.vetoes
    assert missing.vetoes


def test_stress_trade_direct_and_json_reject_consistently_rehashed_projection() -> None:
    evidence = build_execution_stress_trade_evidence(
        _explicit_take_outcome(),
        policy=build_validation_trading_metric_policy(policy_version="wp005-b-v1"),
    )
    payload = evidence.to_dict()
    payload["stress_scenarios"][0]["after_cost_r_unquantized"] = "999"
    payload["trade_evidence_id"] = stable_identity(
        "execution-stress-trade-evidence",
        {key: value for key, value in payload.items() if key != "trade_evidence_id"},
    )
    with pytest.raises(ValueError, match="projection does not recompute"):
        type(evidence).from_dict(payload)
    with pytest.raises(ValueError, match="projection does not recompute"):
        type(evidence).from_json(contract_to_json(payload))


def _bounded_preparation(
    monkeypatch: pytest.MonkeyPatch,
    *,
    locked_oos: bool = False,
    region_embargo_session_count: int = 0,
):
    corpus = validation_fixtures._multi_session_corpus(monkeypatch, count=5)
    policy = build_validation_split_policy(
        policy_version="wp005-b-report-split-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=2 if locked_oos else 3,
        validation_session_count=2,
        locked_oos_session_count=1 if locked_oos else 0,
        locked_oos_required=locked_oos,
        region_embargo_session_count=region_embargo_session_count,
    )
    return build_chronological_validation_preparation(
        corpus,
        split_policy=policy,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    )


def _incomplete_preparation():
    replay = validation_fixtures._stored_replay_for_batch(
        outcome_fixtures._batch(missing_symbol="ABC", missing_index=2)
    )
    survivorship = validation_fixtures._survivorship(replay)
    corpus = build_validation_corpus(
        current_replays=(replay,),
        survivorship_evidence=(survivorship,),
        policy=validation_fixtures._policy(replay),
        frozen_at=replay.outcome_persistence_receipt.persisted_at
        + timedelta(seconds=1),
    )
    split_policy = build_validation_split_policy(
        policy_version="wp005-b-incomplete-diagnostic-v1",
        declared_at=corpus.sessions[0].session_open_at,
        train_research_session_count=0,
        validation_session_count=1,
        locked_oos_session_count=0,
        train_research_required=False,
        locked_oos_required=False,
    )
    return build_chronological_validation_preparation(
        corpus,
        split_policy=split_policy,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    )


def _bound_row_with_outcome(
    template: _BoundValidationMetricRow,
    outcome: OutcomeRecord,
    *,
    policy,
) -> _BoundValidationMetricRow:
    evidence = build_execution_stress_trade_evidence(outcome, policy=policy)
    return replace(
        template,
        row_id=stable_identity(
            "validation-metric-test-row",
            {"source": template.row_id, "outcome": outcome.outcome_id},
        ),
        row_content_hash_sha256=evidence.content_hash(),
        evaluation_id=outcome.evaluation_id,
        evaluation_content_hash_sha256=outcome.decision.evaluation.content_hash(),
        outcome_id=outcome.outcome_id,
        outcome_content_hash_sha256=outcome.content_hash(),
        outcome=outcome,
        trade_evidence=evidence,
        segment_buckets=tuple(
            (
                dimension,
                outcome.direction.value
                if dimension is ValidationSegmentDimension.DIRECTION
                else bucket,
            )
            for dimension, bucket in template.segment_buckets
        ),
    )


def _metric(scope, scenario: ExecutionStressScenario, metric: ValidationTradingMetric):
    return next(
        item
        for item in scope.metrics
        if item.scenario is scenario and item.metric is metric
    )


def _report(preparation, *, seconds: int = 0):
    return build_validation_trading_metric_report(
        preparation,
        policy=build_validation_trading_metric_policy(policy_version="wp005-b-v1"),
        recorded_at=preparation.recorded_at + timedelta(seconds=seconds),
    )


def test_report_recomputes_scopes_zero_trade_metrics_segments_and_roundtrip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation = _bounded_preparation(monkeypatch)
    report = _report(preparation)

    assert report.coverage_row_count == preparation.row_count
    assert tuple(item.kind for item in report.scopes[:2]) == (
        ValidationMetricScopeKind.FINAL_TRAIN_RESEARCH,
        ValidationMetricScopeKind.FINAL_VALIDATION,
    )
    for scope in report.scopes:
        assert len(scope.metrics) == len(ExecutionStressScenario) * len(
            ValidationTradingMetric
        )
        assert tuple((item.scenario, item.metric) for item in scope.metrics) == tuple(
            (scenario, metric)
            for scenario in ExecutionStressScenario
            for metric in ValidationTradingMetric
        )
        if scope.row_ids:
            base = ExecutionStressScenario.BASE
            assert _metric(scope, base, ValidationTradingMetric.TOTAL_TRADES).value == 0
            assert _metric(scope, base, ValidationTradingMetric.WINS).value == 0
            assert _metric(scope, base, ValidationTradingMetric.LOSSES).value == 0
            assert _metric(scope, base, ValidationTradingMetric.BREAKEVENS).value == 0
            assert (
                _metric(scope, base, ValidationTradingMetric.TOTAL_EXECUTION_COST_USD).value
                == 0
            )
            assert _metric(scope, base, ValidationTradingMetric.MEAN_SESSION_R).value == 0
            assert (
                _metric(
                    scope,
                    base,
                    ValidationTradingMetric.SESSION_R_MAX_DRAWDOWN,
                ).value
                == 0
            )
            assert _metric(
                scope, base, ValidationTradingMetric.WIN_RATE
            ).status is ValidationMetricValueStatus.INSUFFICIENT_DATA
        for dimension in ValidationSegmentDimension:
            segments = [item for item in scope.segments if item.dimension is dimension]
            assert sorted(row_id for item in segments for row_id in item.row_ids) == sorted(
                scope.row_ids
            )
    assert report.promotion_eligible is False
    assert type(report).from_json(report.to_json()) == report


def test_report_direct_rejects_metric_population_and_order_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(_bounded_preparation(monkeypatch))
    payload = report.to_dict()
    payload["scopes"][0]["metrics"][0]["value"] = "999"
    payload["report_id"] = stable_identity(
        "validation-trading-metric-report",
        {key: value for key, value in payload.items() if key != "report_id"},
    )
    with pytest.raises(ValueError, match="scopes do not recompute"):
        type(report).from_dict(payload)

    reordered = report.to_dict()
    reordered["scopes"] = list(reversed(reordered["scopes"]))
    reordered["report_id"] = stable_identity(
        "validation-trading-metric-report",
        {key: value for key, value in reordered.items() if key != "report_id"},
    )
    with pytest.raises(ValueError, match="scopes do not recompute"):
        type(report).from_json(contract_to_json(reordered))


def test_locked_oos_is_only_an_exact_exclusion_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(_bounded_preparation(monkeypatch, locked_oos=True))
    locked = tuple(item for item in report.exclusions if item.role.value == "locked_oos")
    assert len(locked) == 1
    excluded_rows = set(locked[0].row_ids)
    assert excluded_rows
    assert all(
        excluded_rows.isdisjoint(scope.row_ids)
        for scope in report.scopes
    )


def test_final_scopes_bind_global_purge_embargo_inventory_and_reject_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(
        _bounded_preparation(monkeypatch, region_embargo_session_count=1)
    )
    final_scopes = tuple(
        item
        for item in report.scopes
        if item.kind
        in {
            ValidationMetricScopeKind.FINAL_TRAIN_RESEARCH,
            ValidationMetricScopeKind.FINAL_VALIDATION,
        }
    )
    global_excluded = tuple(
        item.session_source_id
        for item in report.exclusions
        if item.role.value in {"purged", "embargoed"}
    )
    assert global_excluded
    assert all(item.excluded_session_ids == global_excluded for item in final_scopes)
    assert all(
        len(item.excluded_session_ids) == len(set(item.excluded_session_ids))
        for item in final_scopes
    )
    assert all(
        exclusion.role.value != "locked_oos"
        for exclusion in report.exclusions
        if exclusion.session_source_id in final_scopes[0].excluded_session_ids
    )
    payload = report.to_dict()
    payload["scopes"][0]["excluded_session_ids"] = []
    payload["scopes"][0]["excluded_session_content_hashes"] = []
    payload["report_id"] = stable_identity(
        "validation-trading-metric-report",
        {key: value for key, value in payload.items() if key != "report_id"},
    )
    with pytest.raises(ValueError, match="scopes do not recompute"):
        type(report).from_json(contract_to_json(payload))


def test_report_rejects_consistently_rehashed_metric_projection_tamper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(_bounded_preparation(monkeypatch))
    for field, value in (
        ("numerator_count", 999),
        ("denominator_count", 999),
        ("status", "provisional"),
        ("unit", "usd"),
        ("source_row_ids", ["invented-row"]),
        ("source_trade_evidence_ids", ["invented-trade"]),
    ):
        payload = report.to_dict()
        payload["scopes"][0]["metrics"][0][field] = value
        payload["report_id"] = stable_identity(
            "validation-trading-metric-report",
            {key: item for key, item in payload.items() if key != "report_id"},
        )
        with pytest.raises((TypeError, ValueError)):
            type(report).from_dict(payload)

    for attack in ("invented", "hash", "omission", "reorder"):
        payload = report.to_dict()
        metric = next(
            item
            for scope in payload["scopes"]
            for item in scope["metrics"]
            if len(item["denominator_unit_ids"]) > 1
        )
        if attack == "invented":
            metric["numerator_unit_ids"].append("invented-unit")
            metric["numerator_unit_content_hashes"].append("a" * 64)
            metric["numerator_count"] += 1
        elif attack == "hash":
            metric["denominator_unit_content_hashes"][0] = "b" * 64
        elif attack == "omission":
            metric["denominator_unit_ids"].pop()
            metric["denominator_unit_content_hashes"].pop()
            metric["denominator_count"] -= 1
        else:
            metric["denominator_unit_ids"] = list(
                reversed(metric["denominator_unit_ids"])
            )
            metric["denominator_unit_content_hashes"] = list(
                reversed(metric["denominator_unit_content_hashes"])
            )
        payload["report_id"] = stable_identity(
            "validation-trading-metric-report",
            {key: item for key, item in payload.items() if key != "report_id"},
        )
        with pytest.raises((TypeError, ValueError)):
            type(report).from_dict(payload)


def test_calculation_population_has_exact_scenario_formulas_and_sources() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    evidences = tuple(
        build_execution_stress_trade_evidence(
            _explicit_take_outcome(path=path), policy=policy
        )
        for path in ("target", "stop", "horizon")
    )
    inputs = tuple(
        _MetricCalculationInput(
            row_id=f"row-{index}",
            row_content_hash_sha256=f"{index:064x}",
            session_source_id=f"session-{index}",
            trade_evidence=evidence,
        )
        for index, evidence in enumerate(evidences, start=1)
    )
    values = _calculate_metric_values(
        inputs,
        session_source_ids=tuple(item.session_source_id for item in inputs),
        session_content_hashes=tuple(f"{index + 100:064x}" for index in range(3)),
        scope_status=ValidationMetricReportStatus.AVAILABLE,
        policy=policy,
    )
    base = {
        item.metric: item
        for item in values
        if item.scenario is ExecutionStressScenario.BASE
    }
    assert base[ValidationTradingMetric.TOTAL_TRADES].value == 3
    assert base[ValidationTradingMetric.WINS].value == 2
    assert base[ValidationTradingMetric.LOSSES].value == 1
    assert base[ValidationTradingMetric.BREAKEVENS].value == 0
    assert base[ValidationTradingMetric.WIN_RATE].value == Decimal("0.666666666667")
    assert base[ValidationTradingMetric.TOTAL_EXECUTION_COST_USD].value == Decimal(
        "66.000000000000"
    )
    assert base[ValidationTradingMetric.PROFIT_FACTOR].value is not None
    assert base[ValidationTradingMetric.SESSION_R_SHARPE].value is not None
    assert base[ValidationTradingMetric.SESSION_R_SORTINO].value is not None
    assert all(item.source_row_ids == tuple(entry.row_id for entry in inputs) for item in values)
    row_ids = tuple(item.row_id for item in inputs)
    row_hashes = tuple(item.row_content_hash_sha256 for item in inputs)
    winner_ids = (inputs[0].row_id, inputs[2].row_id)
    winner_hashes = (
        inputs[0].row_content_hash_sha256,
        inputs[2].row_content_hash_sha256,
    )
    loser_ids = (inputs[1].row_id,)
    loser_hashes = (inputs[1].row_content_hash_sha256,)
    wins = base[ValidationTradingMetric.WINS]
    assert (wins.numerator_unit_ids, wins.numerator_unit_content_hashes) == (
        winner_ids,
        winner_hashes,
    )
    assert (wins.denominator_unit_ids, wins.denominator_unit_content_hashes) == (
        row_ids,
        row_hashes,
    )
    profit_factor = base[ValidationTradingMetric.PROFIT_FACTOR]
    assert (
        profit_factor.numerator_unit_ids,
        profit_factor.numerator_unit_content_hashes,
        profit_factor.denominator_unit_ids,
        profit_factor.denominator_unit_content_hashes,
    ) == (winner_ids, winner_hashes, loser_ids, loser_hashes)
    session_metric = base[ValidationTradingMetric.MEAN_SESSION_R]
    assert session_metric.numerator_unit_ids == tuple(
        item.session_source_id for item in inputs
    )
    assert session_metric.numerator_unit_content_hashes == tuple(
        f"{index + 100:064x}" for index in range(3)
    )
    assert session_metric.denominator_unit_ids == session_metric.numerator_unit_ids
    assert (
        session_metric.denominator_unit_content_hashes
        == session_metric.numerator_unit_content_hashes
    )
    expected_stress_membership = tuple(
        (item.trade_evidence_id, item.content_hash()) for item in evidences
    )
    for scenario in ExecutionStressScenario:
        actual_stress_membership = tuple(
            (item.trade_evidence_id, item.content_hash())
            for item in evidences
            if any(value.scenario is scenario for value in item.stress_scenarios)
        )
        assert actual_stress_membership == expected_stress_membership
    for evidence in evidences:
        after_cost = tuple(
            item.after_cost_r_unquantized for item in evidence.stress_scenarios
        )
        assert after_cost[0] >= after_cost[1] >= after_cost[2]


def test_exact_no_fill_and_unresolved_take_are_distinct_population_truth() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    no_fill = build_execution_stress_trade_evidence(
        _explicit_take_outcome(path="no_entry"), policy=policy
    )
    unresolved = build_execution_stress_trade_evidence(
        _explicit_take_outcome(path="ambiguous"), policy=policy
    )
    assert no_fill.disposition is TradeMetricDisposition.EXACT_NO_FILL
    assert no_fill.stress_scenarios == ()
    assert unresolved.disposition is TradeMetricDisposition.UNRESOLVED_TAKE
    assert unresolved.stress_scenarios == ()

    values = _calculate_metric_values(
        (
            _MetricCalculationInput(
                row_id="unresolved-row",
                row_content_hash_sha256="a" * 64,
                session_source_id="unresolved-session",
                trade_evidence=unresolved,
            ),
        ),
        session_source_ids=("unresolved-session",),
        session_content_hashes=("b" * 64,),
        scope_status=ValidationMetricReportStatus.AVAILABLE,
        policy=policy,
    )
    unsupported = {
        ValidationTradingMetric.CAPITAL_MAX_DRAWDOWN,
        ValidationTradingMetric.ANNUALIZED_RETURN,
        ValidationTradingMetric.BENCHMARK_EXCESS_RETURN,
    }
    assert all(item.value is None for item in values)
    assert all(
        item.status
        is (
            ValidationMetricValueStatus.UNAVAILABLE
            if item.metric in unsupported
            else ValidationMetricValueStatus.INCOMPLETE
        )
        for item in values
    )


def test_report_strict_json_unknown_duplicate_and_float_reject(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    report = _report(_bounded_preparation(monkeypatch))
    with pytest.raises(ValueError, match="unknown field"):
        type(report).from_dict({**report.to_dict(), "injected": "value"})
    with pytest.raises(ValueError, match="duplicate JSON key"):
        type(report).from_json(report.to_json()[:-1] + ',"report_id":"duplicate"}')
    floating = report.to_dict()
    floating["scopes"][0]["metrics"][0]["value"] = 0.0
    with pytest.raises(ValueError, match="exact Decimal"):
        type(report).from_dict(floating)


def test_validation_metric_import_firewall_and_facade_surface() -> None:
    root = Path(__file__).parents[1]
    code = """
import sys
import intraday_scanner.v2.opportunity
import intraday_scanner.v2.opportunity.models
import intraday_scanner.v2.opportunity.pipeline
import intraday_scanner.storage.opportunity_store
assert not any(
    name.startswith('intraday_scanner.v2.opportunity.validation_metric')
    or name == 'intraday_scanner.v2.opportunity.validation_metrics'
    for name in sys.modules
)
"""
    subprocess.run([sys.executable, "-c", code], cwd=root, check=True)
    source_files = tuple(
        root.glob("intraday_scanner/v2/opportunity/validation_metric*.py")
    ) + (root / "intraday_scanner/v2/opportunity/validation_metrics.py",)
    forbidden = ("alpha.path_replay", "backtest", "runtime", "broker", "storage")
    for source in source_files:
        tree = ast.parse(source.read_text(encoding="utf-8"))
        imports = tuple(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not any(token in module for module in imports for token in forbidden)

    import intraday_scanner.v2.opportunity.validation_metrics as facade

    assert "ExecutionStressTradeEvidence" not in facade.__all__
    assert "build_execution_stress_trade_evidence" not in facade.__all__
    assert set(facade.__all__) == {
        "ExecutionCostEvidenceQuality",
        "ExecutionStressScenario",
        "TradeMetricDisposition",
        "ValidationMetricReportStatus",
        "ValidationMetricScopeKind",
        "ValidationMetricValueStatus",
        "ValidationSegmentDimension",
        "ValidationTradingMetric",
        "ValidationTradingMetricPolicy",
        "ValidationTradingMetricReport",
        "ValidationTradingMetricUnit",
        "build_validation_trading_metric_policy",
        "build_validation_trading_metric_report",
    }


def test_report_recorded_at_is_distinct_utc_artifact_chronology(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation = _bounded_preparation(monkeypatch)
    equal = _report(preparation)
    later = _report(preparation, seconds=1)
    assert equal.recorded_at == preparation.recorded_at
    assert later.recorded_at == preparation.recorded_at + timedelta(seconds=1)
    assert equal.report_id != later.report_id
    assert type(later).from_json(later.to_json()) == later
    with pytest.raises(ValueError, match="UTC"):
        build_validation_trading_metric_report(
            preparation,
            policy=equal.policy,
            recorded_at=preparation.recorded_at.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="predates"):
        build_validation_trading_metric_report(
            preparation,
            policy=equal.policy,
            recorded_at=preparation.recorded_at - timedelta(microseconds=1),
        )
    payload = equal.to_dict()
    payload["recorded_at"] = (
        preparation.recorded_at - timedelta(microseconds=1)
    ).isoformat()
    payload["report_id"] = stable_identity(
        "validation-trading-metric-report",
        {key: value for key, value in payload.items() if key != "report_id"},
    )
    with pytest.raises(ValueError, match="predates"):
        type(equal).from_dict(payload)
    with pytest.raises(ValueError, match="predates"):
        type(equal).from_json(contract_to_json(payload))


def test_v1_policy_thresholds_are_fixed_even_after_consistent_rehash() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    with pytest.raises(ValueError, match="thresholds are fixed"):
        replace(policy, liquidity_low_percentile=Decimal("0.20"))
    payload = policy.to_dict()
    payload["volatility_expansion_ratio"] = "2.00"
    payload["policy_id"] = stable_identity(
        "validation-trading-metric-policy",
        {key: value for key, value in payload.items() if key != "policy_id"},
    )
    with pytest.raises(ValueError, match="thresholds are fixed"):
        type(policy).from_json(contract_to_json(payload))


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("0.249999", "low"),
        ("0.25", "medium"),
        ("0.749999", "medium"),
        ("0.75", "high"),
    ),
)
def test_liquidity_boundaries_and_calendar_buckets_are_exact(
    value: str,
    expected: str,
) -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    feature = SimpleNamespace(
        availability=Availability.AVAILABLE,
        value=Decimal(value),
    )
    assert _liquidity_bucket(feature, policy) == expected
    for invalid in (Decimal("-0.000001"), Decimal("1.000001")):
        with pytest.raises(ValueError, match=r"outside \[0, 1\]"):
            _liquidity_bucket(
                SimpleNamespace(availability=Availability.AVAILABLE, value=invalid),
                policy,
            )
    december = datetime(2025, 12, 31, 20, tzinfo=UTC)
    january = datetime(2026, 1, 2, 15, tzinfo=UTC)
    assert (_month_bucket(december), _month_bucket(january)) == (
        "2025-12",
        "2026-01",
    )
    assert (_year_bucket(december), _year_bucket(january)) == ("2025", "2026")


def test_session_r_drawdown_is_signed_with_exact_duration() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    drawdown, duration = _session_drawdown(
        (Decimal("2"), Decimal("-1"), Decimal("-2"), Decimal("1")),
        policy,
    )
    assert drawdown == Decimal("-3")
    assert duration == 3
    assert _session_drawdown((Decimal("0"), Decimal("1")), policy) == (
        Decimal("0"),
        0,
    )


def test_unsupported_metrics_override_scope_and_unresolved_states() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    unresolved = build_execution_stress_trade_evidence(
        _explicit_take_outcome(path="ambiguous"), policy=policy
    )
    entry = _MetricCalculationInput(
        row_id="unresolved-row",
        row_content_hash_sha256="a" * 64,
        session_source_id="unresolved-session",
        trade_evidence=unresolved,
    )
    for scope_status in (
        ValidationMetricReportStatus.INCOMPLETE,
        ValidationMetricReportStatus.EXTERNAL_DATA_BLOCKED,
        ValidationMetricReportStatus.AVAILABLE,
    ):
        values = _calculate_metric_values(
            (entry,),
            session_source_ids=(entry.session_source_id,),
            session_content_hashes=("c" * 64,),
            scope_status=scope_status,
            policy=policy,
        )
        for metric, reason in (
            (ValidationTradingMetric.CAPITAL_MAX_DRAWDOWN, "capital_model_not_defined"),
            (ValidationTradingMetric.ANNUALIZED_RETURN, "capital_model_not_defined"),
            (
                ValidationTradingMetric.BENCHMARK_EXCESS_RETURN,
                "future_benchmark_outcomes_not_embedded",
            ),
        ):
            selected = next(
                item
                for item in values
                if item.scenario is ExecutionStressScenario.BASE
                and item.metric is metric
            )
            assert selected.status is ValidationMetricValueStatus.UNAVAILABLE
            assert selected.reason == reason


def test_metric_arithmetic_degenerate_denominators_and_missing_cost_path_truth() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    winner = build_execution_stress_trade_evidence(
        _explicit_take_outcome(path="target"), policy=policy
    )
    source = _MetricCalculationInput(
        row_id="winner-row",
        row_content_hash_sha256="b" * 64,
        session_source_id="one-session",
        trade_evidence=winner,
    )
    values = _calculate_metric_values(
        (source,),
        session_source_ids=(source.session_source_id,),
        session_content_hashes=("c" * 64,),
        scope_status=ValidationMetricReportStatus.AVAILABLE,
        policy=policy,
    )
    base = {
        item.metric: item
        for item in values
        if item.scenario is ExecutionStressScenario.BASE
    }
    assert base[ValidationTradingMetric.PROFIT_FACTOR].value is None
    assert base[ValidationTradingMetric.PROFIT_FACTOR].reason == (
        "loss_denominator_is_zero_or_missing"
    )
    assert base[ValidationTradingMetric.SESSION_R_SHARPE].value is None
    assert base[ValidationTradingMetric.SESSION_R_SORTINO].value is None

    lineage = (
        (source.row_id,),
        (source.row_content_hash_sha256,),
        (winner.trade_evidence_id,),
        (winner.content_hash(),),
    )
    for metric in (
        ValidationTradingMetric.MAXIMUM_FAVORABLE_EXCURSION_R,
        ValidationTradingMetric.MAXIMUM_ADVERSE_EXCURSION_R,
        ValidationTradingMetric.AVERAGE_HOLDING_LOWER_SECONDS,
        ValidationTradingMetric.AVERAGE_HOLDING_UPPER_SECONDS,
    ):
        result = _calculate_one(
            metric,
            scenario=ExecutionStressScenario.BASE,
            inputs=(source,),
            fills=(source,),
            after_cost=(),
            execution_costs=(),
            session_source_ids=(source.session_source_id,),
            session_content_hashes=("c" * 64,),
            cost_blocked=True,
            diagnostic_reason=None,
            policy=policy,
            lineage=lineage,
        )
        assert result.status is ValidationMetricValueStatus.AVAILABLE
        assert result.value is not None
    after_cost = _calculate_one(
        ValidationTradingMetric.EXPECTANCY_R,
        scenario=ExecutionStressScenario.BASE,
        inputs=(source,),
        fills=(source,),
        after_cost=(),
        execution_costs=(),
        session_source_ids=(source.session_source_id,),
        session_content_hashes=("c" * 64,),
        cost_blocked=True,
        diagnostic_reason=None,
        policy=policy,
        lineage=lineage,
    )
    assert after_cost.status is ValidationMetricValueStatus.INCOMPLETE
    assert after_cost.value is None


def test_partial_terminal_take_is_provisional_under_incomplete_parent_while_non_take_does_not_block(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation = _incomplete_preparation()
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    original_rows = metric_segments._bind_rows(preparation, policy)
    assert len(original_rows) >= 2
    partial_take = _bound_row_with_outcome(
        original_rows[0],
        _explicit_take_outcome(path="target"),
        policy=policy,
    )
    projected_rows = (partial_take, *original_rows[1:])
    monkeypatch.setattr(
        metric_segments,
        "_bind_rows",
        lambda _preparation, _policy: projected_rows,
    )

    report = build_validation_trading_metric_report(
        preparation,
        policy=policy,
        recorded_at=preparation.recorded_at,
    )
    assert report.status is ValidationMetricReportStatus.INSUFFICIENT_DATA
    scope = next(item for item in report.scopes if partial_take.row_id in item.row_ids)
    total = _metric(scope, ExecutionStressScenario.BASE, ValidationTradingMetric.TOTAL_TRADES)
    expectancy = _metric(
        scope,
        ExecutionStressScenario.BASE,
        ValidationTradingMetric.EXPECTANCY_R,
    )
    assert partial_take.outcome.completeness is OutcomeCompleteness.PARTIAL
    assert partial_take.trade_evidence.disposition is (
        TradeMetricDisposition.RESOLVED_FILL_COST_COMPLETE
    )
    assert (total.value, expectancy.value) == (
        Decimal("1"),
        partial_take.trade_evidence.stress_scenarios[0].after_cost_r,
    )
    assert total.status is expectancy.status is ValidationMetricValueStatus.PROVISIONAL
    assert total.reason == "bounded_diagnostic_parent_scope_insufficient_data"


def test_unresolved_take_blocks_only_its_local_segment_population(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    resolved_outcome = _explicit_take_outcome(
        direction=StrategyDirection.SHORT,
        path="target",
    )
    unresolved_outcome = _explicit_take_outcome(
        direction=StrategyDirection.LONG,
        path="ambiguous",
    )
    report = _report(_bounded_preparation(monkeypatch))
    policy = report.policy
    resolved = _bound_row_with_outcome(
        report.bound_rows[0],
        resolved_outcome,
        policy=policy,
    )
    unresolved = _bound_row_with_outcome(
        report.bound_rows[1],
        unresolved_outcome,
        policy=policy,
    )
    segments = _build_segments(
        (resolved, unresolved),
        scope_status=ValidationMetricReportStatus.AVAILABLE,
        policy=policy,
    )
    direction = {
        item.bucket: item
        for item in segments
        if item.dimension is ValidationSegmentDimension.DIRECTION
    }
    short_total = _metric(
        direction["short"],
        ExecutionStressScenario.BASE,
        ValidationTradingMetric.TOTAL_TRADES,
    )
    long_total = _metric(
        direction["long"],
        ExecutionStressScenario.BASE,
        ValidationTradingMetric.TOTAL_TRADES,
    )
    assert (short_total.status, short_total.value) == (
        ValidationMetricValueStatus.AVAILABLE,
        Decimal("1"),
    )
    assert (long_total.status, long_total.value, long_total.reason) == (
        ValidationMetricValueStatus.INCOMPLETE,
        None,
        "unresolved_take_execution_truth",
    )


def test_unquantized_tiny_signs_and_cancellation_heavy_arithmetic_are_exact() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    evidence = build_execution_stress_trade_evidence(
        _explicit_take_outcome(), policy=policy
    )
    inputs = tuple(
        _MetricCalculationInput(
            row_id=f"tiny-row-{index}",
            row_content_hash_sha256=f"{index:064x}",
            session_source_id=f"tiny-session-{index}",
            trade_evidence=evidence,
        )
        for index in range(1, 4)
    )
    lineage = (
        tuple(item.row_id for item in inputs),
        tuple(item.row_content_hash_sha256 for item in inputs),
        tuple(item.trade_evidence.trade_evidence_id for item in inputs),
        tuple(item.trade_evidence.content_hash() for item in inputs),
    )
    signs = (Decimal("4E-13"), Decimal("-4E-13"), Decimal("0"))
    expected_counts = {
        ValidationTradingMetric.WINS: Decimal("1"),
        ValidationTradingMetric.LOSSES: Decimal("1"),
        ValidationTradingMetric.BREAKEVENS: Decimal("1"),
    }
    for metric, expected in expected_counts.items():
        value = _calculate_one(
            metric,
            scenario=ExecutionStressScenario.BASE,
            inputs=inputs,
            fills=inputs,
            after_cost=signs,
            execution_costs=(Decimal("0"),) * 3,
            session_source_ids=tuple(item.session_source_id for item in inputs),
            session_content_hashes=tuple(f"{index + 10:064x}" for index in range(3)),
            cost_blocked=False,
            diagnostic_reason=None,
            policy=policy,
            lineage=lineage,
        )
        assert value.value == expected

    cancellation = (Decimal("1E+50"), Decimal("1"), Decimal("-1E+50"))
    results = []
    for precision in (6, 28, 64):
        with _hostile_decimal_context(precision):
            results.append(
                tuple(
                    _calculate_one(
                        metric,
                        scenario=ExecutionStressScenario.BASE,
                        inputs=inputs,
                        fills=inputs,
                        after_cost=cancellation,
                        execution_costs=(Decimal("0"),) * 3,
                        session_source_ids=tuple(
                            item.session_source_id for item in inputs
                        ),
                        session_content_hashes=tuple(
                            f"{index + 20:064x}" for index in range(3)
                        ),
                        cost_blocked=False,
                        diagnostic_reason=None,
                        policy=policy,
                        lineage=lineage,
                    ).value
                    for metric in (
                        ValidationTradingMetric.EXPECTANCY_R,
                        ValidationTradingMetric.PROFIT_FACTOR,
                    )
                )
            )
    assert results[0] == results[1] == results[2] == (
        Decimal("0.333333333333"),
        Decimal("1.000000000000"),
    )
    assert _session_drawdown(cancellation, policy) == (Decimal("-1E+50"), 1)


def test_metric_artifacts_are_byte_stable_across_ambient_decimal_contexts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    outcome = _explicit_take_outcome(path="horizon")
    preparation = _bounded_preparation(monkeypatch)
    evidence_json: list[str] = []
    report_json: list[str] = []
    for precision in (6, 28, 64):
        with _hostile_decimal_context(precision):
            evidence_json.append(
                build_execution_stress_trade_evidence(outcome, policy=policy).to_json()
            )
            report_json.append(
                build_validation_trading_metric_report(
                    preparation,
                    policy=policy,
                    recorded_at=preparation.recorded_at,
                ).to_json()
            )
    assert len(set(evidence_json)) == len(set(report_json)) == 1
    with _hostile_decimal_context(6):
        evidence = build_execution_stress_trade_evidence(outcome, policy=policy)
        assert type(evidence).from_dict(evidence.to_dict()).to_json() == evidence_json[-1]
        assert type(evidence).from_json(evidence_json[-1]).to_json() == evidence_json[-1]
        report = build_validation_trading_metric_report(
            preparation,
            policy=policy,
            recorded_at=preparation.recorded_at,
        )
        assert type(report).from_dict(report.to_dict()).to_json() == report_json[-1]
        assert type(report).from_json(report_json[-1]).to_json() == report_json[-1]


def test_microsecond_holding_conversion_is_exact_under_hostile_ambient_contexts() -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    start = datetime(2026, 8, 11, 14, 30, tzinfo=UTC)
    elapsed = timedelta(hours=6, minutes=29, seconds=59, microseconds=123456)
    source = SimpleNamespace(
        entry_interval=SimpleNamespace(
            interval_start_at=start,
            interval_end_at=start + timedelta(microseconds=111111),
        ),
        exit_interval=SimpleNamespace(
            interval_start_at=start + elapsed - timedelta(microseconds=222222),
            interval_end_at=start + elapsed,
        ),
    )
    results = []
    for precision in (6, 28, 64):
        with _hostile_decimal_context(precision):
            assert _timedelta_decimal_seconds(elapsed, policy) == Decimal(
                "23399.123456"
            )
            results.append(_holding_bounds(source, policy))
    assert results == [
        (Decimal("23398.790123"), Decimal("23399.123456")),
    ] * 3

    invalid = SimpleNamespace(
        entry_interval=source.entry_interval,
        exit_interval=SimpleNamespace(
            interval_start_at=start - timedelta(seconds=2),
            interval_end_at=start - timedelta(seconds=1),
        ),
    )
    with _hostile_decimal_context(6):
        with pytest.raises(ValueError, match="holding interval is invalid"):
            _holding_bounds(invalid, policy)


def test_cost_quality_taxonomy_and_limitations_are_distinct() -> None:
    evaluation = _empirical_evaluation(evaluation_id="metric-cost-taxonomy")
    heuristic = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(spread_status=RiskValueStatus.PROVISIONAL),
        quote_scope=QuoteEvidenceScope.PROVISIONAL,
    )
    nonconsolidated = _execution_risk(
        evaluation=evaluation,
        quote_scope=QuoteEvidenceScope.NONCONSOLIDATED,
    )
    assert _cost_quality(heuristic) is ExecutionCostEvidenceQuality.PROVISIONAL
    assert _cost_quality(nonconsolidated) is (
        ExecutionCostEvidenceQuality.NONCONSOLIDATED
    )
    assert _cost_limitation(_cost_quality(heuristic)) == (
        "take_fill_cost_evidence_provisional"
    )
    assert _cost_limitation(_cost_quality(nonconsolidated)) == (
        "take_fill_cost_evidence_nonconsolidated"
    )


@pytest.mark.parametrize(
    ("value", "expected"),
    (
        ("0", "compression"),
        ("0.70", "compression"),
        ("1.50", "expansion"),
    ),
)
def test_volatility_boundaries_are_exact(value: str, expected: str) -> None:
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    feature = SimpleNamespace(
        availability=Availability.AVAILABLE,
        value=Decimal(value),
    )
    assert _volatility_bucket(feature, policy) == expected


def test_negative_volatility_source_body_rejects_direct_and_json_recomputation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    preparation = _bounded_preparation(monkeypatch)
    policy = build_validation_trading_metric_policy(policy_version="wp005-b-v1")
    valid_report = build_validation_trading_metric_report(
        preparation,
        policy=policy,
        recorded_at=preparation.recorded_at,
    )
    corpus = preparation.audit_receipt.fold_collection.split_plan.corpus
    run = corpus.sessions[0].current_outcome_replays[0].pipeline_result
    rich = run.preparation.rich_snapshots[0]
    feature = rich.numeric("realized_volatility_ratio")
    assert feature is not None
    invalid_feature = replace(feature, value=Decimal("-0.000001"))
    invalid_rich = replace(
        rich,
        numerical=tuple(
            invalid_feature if item.name == invalid_feature.name else item
            for item in rich.numerical
        ),
    )
    parsed_invalid = type(invalid_rich).from_json(invalid_rich.to_json())
    with pytest.raises(ValueError, match="cannot be negative"):
        _segment_buckets(
            valid_report.bound_rows[0].outcome,
            parsed_invalid,
            market_state="normal",
            security_state="normal",
            policy=policy,
        )

    original_bind = metric_segments._bind_rows

    def bind_with_invalid_source(preparation, policy):
        rows = original_bind(preparation, policy)
        _segment_buckets(
            rows[0].outcome,
            parsed_invalid,
            market_state="normal",
            security_state="normal",
            policy=policy,
        )
        return rows

    monkeypatch.setattr(metric_segments, "_bind_rows", bind_with_invalid_source)
    with pytest.raises(ValueError, match="cannot be negative"):
        build_validation_trading_metric_report(
            preparation,
            policy=policy,
            recorded_at=preparation.recorded_at,
        )
