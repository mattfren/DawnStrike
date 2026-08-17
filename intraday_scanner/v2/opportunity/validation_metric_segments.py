"""Exact validation scope, source, and segment population projections."""

from __future__ import annotations

import hashlib
from calendar import day_name
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal

from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.models import Availability, FeatureSnapshot
from intraday_scanner.v2.opportunity.outcome_records import OutcomeRecord
from intraday_scanner.v2.opportunity.validation_audit import (
    ChronologicalValidationPreparationReceipt,
)
from intraday_scanner.v2.opportunity.validation_contracts import (
    SplitRole,
    SurvivorshipEvidenceStatus,
    ValidationCorpusStatus,
    ValidationPreparationStatus,
)
from intraday_scanner.v2.opportunity.validation_metric_calculations import (
    _calculate_metric_values,
    _MetricCalculationInput,
    _ValidationTradingMetricValue,
)
from intraday_scanner.v2.opportunity.validation_metric_contracts import (
    ValidationMetricReportStatus,
    ValidationMetricScopeKind,
    ValidationSegmentDimension,
    ValidationTradingMetricPolicy,
)
from intraday_scanner.v2.opportunity.validation_metric_population import (
    ExecutionStressTradeEvidence,
    build_execution_stress_trade_evidence,
)


@dataclass(frozen=True)
class _BoundValidationMetricRow:
    row_id: str
    row_content_hash_sha256: str
    session_source_id: str
    session_content_hash_sha256: str
    run_id: str
    run_content_hash_sha256: str
    evaluation_id: str
    evaluation_content_hash_sha256: str
    outcome_id: str
    outcome_content_hash_sha256: str
    outcome: OutcomeRecord
    trade_evidence: ExecutionStressTradeEvidence
    segment_buckets: tuple[tuple[ValidationSegmentDimension, str], ...]


@dataclass(frozen=True)
class _ValidationMetricSegment:
    dimension: ValidationSegmentDimension
    bucket: str
    row_ids: tuple[str, ...]
    row_content_hashes: tuple[str, ...]
    metrics: tuple[_ValidationTradingMetricValue, ...]


@dataclass(frozen=True)
class _ValidationMetricScope:
    scope_id: str
    kind: ValidationMetricScopeKind
    fold_id: str | None
    fold_ordinal: int | None
    session_source_ids: tuple[str, ...]
    session_content_hashes: tuple[str, ...]
    row_ids: tuple[str, ...]
    row_content_hashes: tuple[str, ...]
    excluded_session_ids: tuple[str, ...]
    excluded_session_content_hashes: tuple[str, ...]
    status: ValidationMetricReportStatus
    metrics: tuple[_ValidationTradingMetricValue, ...]
    segments: tuple[_ValidationMetricSegment, ...]
    limitations: tuple[str, ...]


@dataclass(frozen=True)
class _ValidationMetricExclusion:
    session_source_id: str
    session_content_hash_sha256: str
    role: SplitRole
    row_ids: tuple[str, ...]
    row_content_hashes: tuple[str, ...]
    reason: str


def _derive_metric_report_projections(
    preparation: ChronologicalValidationPreparationReceipt,
    policy: ValidationTradingMetricPolicy,
) -> tuple[
    tuple[_BoundValidationMetricRow, ...],
    tuple[_ValidationMetricScope, ...],
    tuple[_ValidationMetricExclusion, ...],
    ValidationMetricReportStatus,
    tuple[str, ...],
]:
    bound_rows = _bind_rows(preparation, policy)
    status = _report_status(preparation)
    scopes = _build_scopes(preparation, policy, bound_rows, status)
    exclusions = _build_exclusions(preparation, bound_rows)
    limitations = _report_limitations(preparation, status)
    return bound_rows, scopes, exclusions, status, limitations


def _bind_rows(
    preparation: ChronologicalValidationPreparationReceipt,
    policy: ValidationTradingMetricPolicy,
) -> tuple[_BoundValidationMetricRow, ...]:
    corpus = preparation.audit_receipt.fold_collection.split_plan.corpus
    replay_by_id = {
        replay.replay_id: (session, replay)
        for session in corpus.sessions
        for replay in session.current_outcome_replays
    }
    result: list[_BoundValidationMetricRow] = []
    for row in corpus.rows:
        binding = replay_by_id.get(row.replay_id)
        if binding is None:
            raise ValueError("validation metric row references unknown replay")
        session, replay = binding
        run = replay.pipeline_result
        outcome = next(
            (item for item in replay.outcome_batch.outcomes if item.outcome_id == row.outcome_id),
            None,
        )
        if outcome is None:
            raise ValueError("validation metric row references unknown outcome")
        evaluation = outcome.decision.evaluation
        if (
            row.session_source_id != session.session_source_id
            or row.run_id != run.run_id
            or row.run_content_hash_sha256 != run.content_hash()
            or row.evaluation_id != evaluation.evaluation_id
            or row.evaluation_content_hash_sha256 != evaluation.content_hash()
            or row.decision_id != outcome.decision_id
            or row.decision_content_hash_sha256 != outcome.decision.content_hash()
            or row.outcome_content_hash_sha256 != outcome.content_hash()
        ):
            raise ValueError("validation metric row binding does not match exact source bodies")
        rich = next(
            (
                item
                for item in run.preparation.rich_snapshots
                if item.snapshot_id == evaluation.feature_snapshot_id
            ),
            None,
        )
        if rich is None or rich.symbol != outcome.symbol:
            raise ValueError("validation metric row lacks exact rich feature snapshot")
        security_regime = next(
            (item for item in run.security_regimes if item.symbol == outcome.symbol),
            None,
        )
        if security_regime is None:
            raise ValueError("validation metric row lacks exact security regime")
        evidence = build_execution_stress_trade_evidence(outcome, policy=policy)
        result.append(
            _BoundValidationMetricRow(
                row_id=row.row_id,
                row_content_hash_sha256=_row_projection_hash(row),
                session_source_id=session.session_source_id,
                session_content_hash_sha256=session.content_hash(),
                run_id=run.run_id,
                run_content_hash_sha256=run.content_hash(),
                evaluation_id=evaluation.evaluation_id,
                evaluation_content_hash_sha256=evaluation.content_hash(),
                outcome_id=outcome.outcome_id,
                outcome_content_hash_sha256=outcome.content_hash(),
                outcome=outcome,
                trade_evidence=evidence,
                segment_buckets=_segment_buckets(
                    outcome,
                    rich,
                    market_state=run.market_regime.state.value,
                    security_state=security_regime.state.value,
                    policy=policy,
                ),
            )
        )
    return tuple(result)


def _build_scopes(
    preparation: ChronologicalValidationPreparationReceipt,
    policy: ValidationTradingMetricPolicy,
    bound_rows: tuple[_BoundValidationMetricRow, ...],
    report_status: ValidationMetricReportStatus,
) -> tuple[_ValidationMetricScope, ...]:
    collection = preparation.audit_receipt.fold_collection
    plan = collection.split_plan
    allocations = plan.allocations
    specifications: list[
        tuple[
            ValidationMetricScopeKind,
            str | None,
            int | None,
            tuple[str, ...],
            tuple[str, ...],
        ]
    ] = [
        (
            ValidationMetricScopeKind.FINAL_TRAIN_RESEARCH,
            None,
            None,
            tuple(
                item.session_source_id
                for item in allocations
                if item.role is SplitRole.TRAIN_RESEARCH
            ),
            tuple(
                item.session_source_id
                for item in allocations
                if item.role in {SplitRole.PURGED, SplitRole.EMBARGOED}
            ),
        ),
        (
            ValidationMetricScopeKind.FINAL_VALIDATION,
            None,
            None,
            tuple(
                item.session_source_id
                for item in allocations
                if item.role is SplitRole.VALIDATION
            ),
            tuple(
                item.session_source_id
                for item in allocations
                if item.role in {SplitRole.PURGED, SplitRole.EMBARGOED}
            ),
        ),
    ]
    for fold in collection.folds:
        excluded = tuple((*fold.purged_session_ids, *fold.embargoed_session_ids))
        specifications.extend(
            (
                (
                    ValidationMetricScopeKind.FOLD_TRAIN,
                    fold.fold_id,
                    fold.ordinal,
                    fold.train_session_ids,
                    excluded,
                ),
                (
                    ValidationMetricScopeKind.FOLD_VALIDATION,
                    fold.fold_id,
                    fold.ordinal,
                    fold.validation_session_ids,
                    excluded,
                ),
            )
        )
    return tuple(
        _build_scope(
            preparation,
            policy,
            bound_rows,
            report_status,
            kind=kind,
            fold_id=fold_id,
            fold_ordinal=fold_ordinal,
            session_ids=session_ids,
            excluded_session_ids=excluded,
        )
        for kind, fold_id, fold_ordinal, session_ids, excluded in specifications
    )


def _build_scope(
    preparation: ChronologicalValidationPreparationReceipt,
    policy: ValidationTradingMetricPolicy,
    bound_rows: tuple[_BoundValidationMetricRow, ...],
    report_status: ValidationMetricReportStatus,
    *,
    kind: ValidationMetricScopeKind,
    fold_id: str | None,
    fold_ordinal: int | None,
    session_ids: tuple[str, ...],
    excluded_session_ids: tuple[str, ...],
) -> _ValidationMetricScope:
    if len(excluded_session_ids) != len(set(excluded_session_ids)):
        raise ValueError("scope exclusion inventory must use unique canonical order")
    corpus = preparation.audit_receipt.fold_collection.split_plan.corpus
    session_map = {item.session_source_id: item for item in corpus.sessions}
    rows = tuple(item for item in bound_rows if item.session_source_id in session_ids)
    calculation_inputs = tuple(_calculation_input(item) for item in rows)
    status = report_status if session_ids else ValidationMetricReportStatus.INSUFFICIENT_DATA
    metrics = _calculate_metric_values(
        calculation_inputs,
        session_source_ids=session_ids,
        session_content_hashes=tuple(
            session_map[item].content_hash() for item in session_ids
        ),
        scope_status=status,
        policy=policy,
    )
    segments = _build_segments(
        rows,
        scope_status=status,
        policy=policy,
    )
    limitations = _scope_limitations(status, kind)
    values = {
        "kind": kind,
        "fold_id": fold_id,
        "fold_ordinal": fold_ordinal,
        "session_source_ids": session_ids,
        "session_content_hashes": tuple(session_map[item].content_hash() for item in session_ids),
        "row_ids": tuple(item.row_id for item in rows),
        "row_content_hashes": tuple(item.row_content_hash_sha256 for item in rows),
        "excluded_session_ids": excluded_session_ids,
        "excluded_session_content_hashes": tuple(
            session_map[item].content_hash() for item in excluded_session_ids
        ),
        "status": status,
        "metrics": metrics,
        "segments": segments,
        "limitations": limitations,
    }
    return _ValidationMetricScope(
        scope_id=_scope_identity(values),
        kind=kind,
        fold_id=fold_id,
        fold_ordinal=fold_ordinal,
        session_source_ids=session_ids,
        session_content_hashes=values["session_content_hashes"],  # type: ignore[arg-type]
        row_ids=values["row_ids"],  # type: ignore[arg-type]
        row_content_hashes=values["row_content_hashes"],  # type: ignore[arg-type]
        excluded_session_ids=excluded_session_ids,
        excluded_session_content_hashes=values[
            "excluded_session_content_hashes"
        ],  # type: ignore[arg-type]
        status=status,
        metrics=metrics,
        segments=segments,
        limitations=limitations,
    )


def _build_segments(
    rows: tuple[_BoundValidationMetricRow, ...],
    *,
    scope_status: ValidationMetricReportStatus,
    policy: ValidationTradingMetricPolicy,
) -> tuple[_ValidationMetricSegment, ...]:
    result: list[_ValidationMetricSegment] = []
    for dimension in policy.segment_dimensions:
        buckets = sorted(
            {dict(item.segment_buckets)[dimension] for item in rows}
        )
        covered: list[str] = []
        for bucket in buckets:
            selected = tuple(
                item for item in rows if dict(item.segment_buckets)[dimension] == bucket
            )
            covered.extend(item.row_id for item in selected)
            session_ids = tuple(dict.fromkeys(item.session_source_id for item in selected))
            session_hashes = tuple(
                next(
                    item.session_content_hash_sha256
                    for item in selected
                    if item.session_source_id == session_id
                )
                for session_id in session_ids
            )
            result.append(
                _ValidationMetricSegment(
                    dimension=dimension,
                    bucket=bucket,
                    row_ids=tuple(item.row_id for item in selected),
                    row_content_hashes=tuple(
                        item.row_content_hash_sha256 for item in selected
                    ),
                    metrics=_calculate_metric_values(
                        tuple(_calculation_input(item) for item in selected),
                        session_source_ids=session_ids,
                        session_content_hashes=session_hashes,
                        scope_status=scope_status,
                        policy=policy,
                    ),
                )
            )
        expected_rows = tuple(item.row_id for item in rows)
        if len(covered) != len(expected_rows) or set(covered) != set(expected_rows):
            raise ValueError("segment dimension does not exactly partition scope rows")
    return tuple(result)


def _build_exclusions(
    preparation: ChronologicalValidationPreparationReceipt,
    bound_rows: tuple[_BoundValidationMetricRow, ...],
) -> tuple[_ValidationMetricExclusion, ...]:
    plan = preparation.audit_receipt.fold_collection.split_plan
    session_map = {
        item.session_source_id: item for item in plan.corpus.sessions
    }
    result = []
    for allocation in plan.allocations:
        if allocation.role in {SplitRole.TRAIN_RESEARCH, SplitRole.VALIDATION}:
            continue
        rows = tuple(
            item for item in bound_rows if item.session_source_id == allocation.session_source_id
        )
        result.append(
            _ValidationMetricExclusion(
                session_source_id=allocation.session_source_id,
                session_content_hash_sha256=session_map[
                    allocation.session_source_id
                ].content_hash(),
                role=allocation.role,
                row_ids=tuple(item.row_id for item in rows),
                row_content_hashes=tuple(item.row_content_hash_sha256 for item in rows),
                reason=f"{allocation.role.value}_excluded_from_metric_populations",
            )
        )
    return tuple(result)


def _segment_buckets(
    outcome: OutcomeRecord,
    rich: FeatureSnapshot,
    *,
    market_state: str,
    security_state: str,
    policy: ValidationTradingMetricPolicy,
) -> tuple[tuple[ValidationSegmentDimension, str], ...]:
    evaluation = outcome.decision.evaluation
    session_segment = rich.category("session_segment")
    catalyst = rich.category("catalyst_state")
    liquidity = rich.numeric("cross_section_liquidity_percentile")
    volatility = rich.numeric("realized_volatility_ratio")
    return (
        (ValidationSegmentDimension.DIRECTION, outcome.direction.value),
        (
            ValidationSegmentDimension.STRATEGY,
            ":".join(
                (
                    outcome.strategy_id,
                    outcome.strategy_version,
                    evaluation.strategy_definition_hash,
                )
            ),
        ),
        (ValidationSegmentDimension.SECURITY_REGIME, security_state),
        (ValidationSegmentDimension.MARKET_STATE, market_state),
        (ValidationSegmentDimension.REGIME_PAIR, f"{market_state}:{security_state}"),
        (
            ValidationSegmentDimension.TIME_OF_DAY,
            _categorical_bucket(session_segment),
        ),
        (
            ValidationSegmentDimension.WEEKDAY,
            day_name[outcome.decision_at.weekday()].lower(),
        ),
        (
            ValidationSegmentDimension.MONTH,
            _month_bucket(outcome.decision_at),
        ),
        (ValidationSegmentDimension.YEAR, _year_bucket(outcome.decision_at)),
        (
            ValidationSegmentDimension.LIQUIDITY_BUCKET,
            _liquidity_bucket(liquidity, policy),
        ),
        (
            ValidationSegmentDimension.VOLATILITY_BUCKET,
            _volatility_bucket(volatility, policy),
        ),
        (ValidationSegmentDimension.CATALYST, _categorical_bucket(catalyst)),
    )


def _categorical_bucket(feature) -> str:
    if (
        feature is None
        or feature.availability is not Availability.AVAILABLE
        or feature.value is None
    ):
        return "unavailable"
    return feature.value


def _liquidity_bucket(feature, policy: ValidationTradingMetricPolicy) -> str:
    value = _numeric_feature_value(feature)
    if value is None:
        return "unavailable"
    if not Decimal("0") <= value <= Decimal("1"):
        raise ValueError("liquidity percentile lies outside [0, 1]")
    if value < policy.liquidity_low_percentile:
        return "low"
    if value >= policy.liquidity_high_percentile:
        return "high"
    return "medium"


def _volatility_bucket(feature, policy: ValidationTradingMetricPolicy) -> str:
    value = _numeric_feature_value(feature)
    if value is None:
        return "unavailable"
    if value < 0:
        raise ValueError("realized volatility ratio cannot be negative")
    if value <= policy.volatility_compression_ratio:
        return "compression"
    if value >= policy.volatility_expansion_ratio:
        return "expansion"
    return "normal"


def _numeric_feature_value(feature) -> Decimal | None:
    if (
        feature is None
        or feature.availability is not Availability.AVAILABLE
        or type(feature.value) is not Decimal
    ):
        return None
    return feature.value


def _month_bucket(decision_at: datetime) -> str:
    return f"{decision_at.year:04d}-{decision_at.month:02d}"


def _year_bucket(decision_at: datetime) -> str:
    return f"{decision_at.year:04d}"


def _calculation_input(item: _BoundValidationMetricRow) -> _MetricCalculationInput:
    return _MetricCalculationInput(
        row_id=item.row_id,
        row_content_hash_sha256=item.row_content_hash_sha256,
        session_source_id=item.session_source_id,
        trade_evidence=item.trade_evidence,
    )


def _row_projection_hash(row) -> str:
    return hashlib.sha256(contract_to_json(row.__dict__).encode("utf-8")).hexdigest()


def _scope_identity(values: Mapping[str, object]) -> str:
    return hashlib.sha256(contract_to_json(values).encode("utf-8")).hexdigest()


def _report_status(
    preparation: ChronologicalValidationPreparationReceipt,
) -> ValidationMetricReportStatus:
    corpus = preparation.audit_receipt.fold_collection.split_plan.corpus
    if preparation.status is ValidationPreparationStatus.EXTERNAL_DATA_BLOCKED:
        return ValidationMetricReportStatus.EXTERNAL_DATA_BLOCKED
    if preparation.status is ValidationPreparationStatus.INSUFFICIENT_DATA:
        return ValidationMetricReportStatus.INSUFFICIENT_DATA
    if preparation.status is ValidationPreparationStatus.FAILED:
        return ValidationMetricReportStatus.INCOMPLETE
    if corpus.status is ValidationCorpusStatus.INCOMPLETE:
        return ValidationMetricReportStatus.INCOMPLETE
    if any(
        item.survivorship_status is not SurvivorshipEvidenceStatus.POINT_IN_TIME
        for item in corpus.sessions
    ):
        return ValidationMetricReportStatus.PROVISIONAL
    return ValidationMetricReportStatus.AVAILABLE


def _scope_limitations(
    status: ValidationMetricReportStatus,
    kind: ValidationMetricScopeKind,
) -> tuple[str, ...]:
    values = {"research_only_no_oos_or_promotion_claim", f"scope:{kind.value}"}
    if status is not ValidationMetricReportStatus.AVAILABLE:
        values.add(f"scope_status:{status.value}")
    return tuple(sorted(values))


def _report_limitations(
    preparation: ChronologicalValidationPreparationReceipt,
    status: ValidationMetricReportStatus,
) -> tuple[str, ...]:
    values = {
        *preparation.limitations,
        "research_only_no_oos_edge_or_promotion_claim",
        "capital_compounding_and_benchmark_outcomes_unavailable",
    }
    if status is not ValidationMetricReportStatus.AVAILABLE:
        values.add(f"metric_report_status:{status.value}")
    return tuple(sorted(values))
