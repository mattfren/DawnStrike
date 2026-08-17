"""Deterministic all-run surfacing and missed-opportunity reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.opportunity.capabilities import CapabilityState
from intraday_scanner.v2.opportunity.discovery import detect_anomalies
from intraday_scanner.v2.opportunity.miss_contracts import (
    DIRECTIONAL_DISCOVERY_ANOMALIES_V1,
    MISS_EXECUTION_GATE_IDS,
    MISS_NONEXECUTION_QUALITY_GATE_IDS,
    MISS_SCORE_GATE_IDS,
    MissCategory,
    MissContract,
    MissSessionDisposition,
    OpportunityDisposition,
    QualificationSourceScopeStatus,
    QualificationStatus,
    SessionRunInventoryStatus,
    SurfacingState,
    identity_payload,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
    require_utc,
)
from intraday_scanner.v2.opportunity.miss_projection import (
    SURFACING_ORDER as _SURFACING_ORDER,
)
from intraday_scanner.v2.opportunity.miss_projection import (
    OpportunityRunProjection,
)
from intraday_scanner.v2.opportunity.miss_qualification import (
    HindsightQualifiedOpportunity,
    QualificationBatch,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    batch_limitations as _batch_limitations,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    first_at as _first_at,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    first_decision_at as _first_decision_at,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    first_rank_at as _first_rank_at,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    opportunity_disposition as _opportunity_disposition,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    record_reasons as _record_reasons,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    summarize_record_dispositions as _summarize_record_dispositions,
)
from intraday_scanner.v2.opportunity.miss_reconciliation_policy import (
    unresolved_qualification_disposition as _unresolved_qualification_disposition,
)
from intraday_scanner.v2.opportunity.miss_replay import SessionReplay
from intraday_scanner.v2.opportunity.models import (
    Availability,
    DataQuality,
    EvaluationStatus,
    TradeDecisionValue,
    stable_identity,
)
from intraday_scanner.v2.opportunity.universe import UniverseMembershipStatus


@dataclass(frozen=True)
class MissedOpportunityRecord(MissContract):
    miss_record_id: str
    opportunity_id: str
    opportunity_content_hash_sha256: str
    opportunity: HindsightQualifiedOpportunity
    session_replay_id: str
    session_replay_content_hash_sha256: str
    session_replay: SessionReplay
    run_projections: tuple[OpportunityRunProjection, ...]
    latest_useful_cutoff_at: datetime
    first_discovered_at: datetime | None
    first_strategy_eligible_at: datetime | None
    first_ranked_at: datetime | None
    first_top_1_at: datetime | None
    first_top_3_at: datetime | None
    first_top_5_at: datetime | None
    first_watched_at: datetime | None
    first_taken_at: datetime | None
    best_on_time_rank_position: int | None
    selected_projection_id: str | None
    selected_projection_content_hash_sha256: str | None
    selected_state: SurfacingState | None
    disposition: OpportunityDisposition
    category: MissCategory | None
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.missed_opportunity_record.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.missed_opportunity_record.v1",
        )
        require_identity(self.miss_record_id, "miss_record_id")
        require_identity(self.opportunity_id, "opportunity_id")
        require_hash(
            self.opportunity_content_hash_sha256,
            "opportunity_content_hash_sha256",
        )
        require_identity(self.session_replay_id, "session_replay_id")
        require_hash(
            self.session_replay_content_hash_sha256,
            "session_replay_content_hash_sha256",
        )
        if (
            self.opportunity_id != self.opportunity.opportunity_id
            or self.opportunity_content_hash_sha256 != self.opportunity.content_hash()
            or self.session_replay_id != self.session_replay.session_replay_id
            or self.session_replay_content_hash_sha256 != self.session_replay.content_hash()
        ):
            raise ValueError("miss record embedded object bindings are inconsistent")
        require_utc(self.latest_useful_cutoff_at, "latest_useful_cutoff_at")
        for value, name in (
            (self.first_discovered_at, "first_discovered_at"),
            (self.first_strategy_eligible_at, "first_strategy_eligible_at"),
            (self.first_ranked_at, "first_ranked_at"),
            (self.first_top_1_at, "first_top_1_at"),
            (self.first_top_3_at, "first_top_3_at"),
            (self.first_top_5_at, "first_top_5_at"),
            (self.first_watched_at, "first_watched_at"),
            (self.first_taken_at, "first_taken_at"),
        ):
            if value is not None and (value.tzinfo is None or value.utcoffset() is None):
                raise ValueError(f"{name} must be timezone-aware")
        selected_pair = (self.selected_projection_id is None) is (
            self.selected_projection_content_hash_sha256 is None
        )
        if not selected_pair:
            raise ValueError("selected projection identity and hash must be paired")
        if self.selected_projection_id is not None:
            require_identity(self.selected_projection_id, "selected_projection_id")
            require_hash(
                self.selected_projection_content_hash_sha256 or "",
                "selected_projection_content_hash_sha256",
            )
        if (self.selected_state is None) is not (self.selected_projection_id is None):
            raise ValueError("selected state and projection must be present together")
        require_unique(tuple(item.run_id for item in self.run_projections), "projected run")
        if self.run_projections != tuple(
            sorted(self.run_projections, key=lambda item: (item.decision_at, item.run_id))
        ):
            raise ValueError("run projections must use canonical session order")
        if self.category is None and self.disposition is not OpportunityDisposition.CAUGHT:
            raise ValueError("uncaught opportunity disposition requires a category")
        if self.category is not None and self.disposition is OpportunityDisposition.CAUGHT:
            raise ValueError("caught opportunity cannot carry a miss category")
        _require_sanitized_values(self.reasons, "miss record reason")
        _require_sanitized_values(self.limitations, "miss record limitation")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("miss record must remain research-only")
        expected = _resolve_record(self.opportunity, self.session_replay)
        _compare_record(self, expected)
        expected_id = stable_identity(
            "missed-opportunity-record",
            identity_payload(self, "miss_record_id"),
        )
        if self.miss_record_id != expected_id:
            raise ValueError("miss record identity does not match content")


@dataclass(frozen=True)
class MissReconciliationBatch(MissContract):
    batch_id: str
    qualification_batch_id: str
    qualification_batch_content_hash_sha256: str
    qualification_batch: QualificationBatch
    session_replay_id: str
    session_replay_content_hash_sha256: str
    session_replay: SessionReplay
    records: tuple[MissedOpportunityRecord, ...]
    session_disposition: MissSessionDisposition
    recorded_at: datetime
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.miss_reconciliation_batch.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.miss_reconciliation_batch.v1",
        )
        require_identity(self.batch_id, "batch_id")
        require_identity(self.qualification_batch_id, "qualification_batch_id")
        require_hash(
            self.qualification_batch_content_hash_sha256,
            "qualification_batch_content_hash_sha256",
        )
        require_identity(self.session_replay_id, "session_replay_id")
        require_hash(
            self.session_replay_content_hash_sha256,
            "session_replay_content_hash_sha256",
        )
        if (
            self.qualification_batch_id != self.qualification_batch.batch_id
            or self.qualification_batch_content_hash_sha256
            != self.qualification_batch.content_hash()
            or self.session_replay_id != self.session_replay.session_replay_id
            or self.session_replay_content_hash_sha256 != self.session_replay.content_hash()
        ):
            raise ValueError("miss batch embedded object bindings are inconsistent")
        _validate_batch_sessions(self.qualification_batch, self.session_replay)
        expected_records = tuple(
            _build_record(item, self.session_replay)
            for item in self.qualification_batch.opportunities
        )
        if self.records != expected_records:
            raise ValueError("miss records do not match qualified opportunity product")
        if self.session_disposition is not _session_disposition(
            self.qualification_batch,
            self.session_replay,
            self.records,
        ):
            raise ValueError("miss session disposition does not recompute")
        expected_recorded_at = max(
            self.qualification_batch.recorded_at,
            self.session_replay.recorded_at,
        )
        if self.recorded_at != expected_recorded_at:
            raise ValueError("miss batch recorded_at does not match source facts")
        if self.limitations != _batch_limitations(
            self.qualification_batch,
            self.session_replay,
        ):
            raise ValueError("miss batch limitations are not canonical")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("miss reconciliation batch must remain research-only")
        expected_id = stable_identity(
            "miss-reconciliation-batch",
            identity_payload(self, "batch_id"),
        )
        if self.batch_id != expected_id:
            raise ValueError("miss reconciliation batch identity does not match content")


@dataclass(frozen=True)
class _ResolvedRecord:
    run_projections: tuple[OpportunityRunProjection, ...]
    latest_useful_cutoff_at: datetime
    first_discovered_at: datetime | None
    first_strategy_eligible_at: datetime | None
    first_ranked_at: datetime | None
    first_top_1_at: datetime | None
    first_top_3_at: datetime | None
    first_top_5_at: datetime | None
    first_watched_at: datetime | None
    first_taken_at: datetime | None
    best_on_time_rank_position: int | None
    selected_projection_id: str | None
    selected_projection_content_hash_sha256: str | None
    selected_state: SurfacingState | None
    disposition: OpportunityDisposition
    category: MissCategory | None
    reasons: tuple[str, ...]
    limitations: tuple[str, ...]


def reconcile_missed_opportunities(
    qualification_batch: QualificationBatch,
    *,
    session_replay: SessionReplay,
) -> MissReconciliationBatch:
    _validate_batch_sessions(qualification_batch, session_replay)
    _validate_regime_replay_bindings(qualification_batch, session_replay)
    records = tuple(
        _build_record(item, session_replay) for item in qualification_batch.opportunities
    )
    disposition = _session_disposition(qualification_batch, session_replay, records)
    recorded_at = max(qualification_batch.recorded_at, session_replay.recorded_at)
    limitations = _batch_limitations(qualification_batch, session_replay)
    values = {
        "qualification_batch_id": qualification_batch.batch_id,
        "qualification_batch_content_hash_sha256": qualification_batch.content_hash(),
        "qualification_batch": qualification_batch,
        "session_replay_id": session_replay.session_replay_id,
        "session_replay_content_hash_sha256": session_replay.content_hash(),
        "session_replay": session_replay,
        "records": records,
        "session_disposition": disposition,
        "recorded_at": recorded_at,
        "limitations": limitations,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.miss_reconciliation_batch.v1",
    }
    return MissReconciliationBatch(
        batch_id=stable_identity("miss-reconciliation-batch", values),
        qualification_batch_id=qualification_batch.batch_id,
        qualification_batch_content_hash_sha256=qualification_batch.content_hash(),
        qualification_batch=qualification_batch,
        session_replay_id=session_replay.session_replay_id,
        session_replay_content_hash_sha256=session_replay.content_hash(),
        session_replay=session_replay,
        records=records,
        session_disposition=disposition,
        recorded_at=recorded_at,
        limitations=limitations,
    )


def _build_record(
    opportunity: HindsightQualifiedOpportunity,
    replay: SessionReplay,
) -> MissedOpportunityRecord:
    resolved = _resolve_record(opportunity, replay)
    values = {
        "opportunity_id": opportunity.opportunity_id,
        "opportunity_content_hash_sha256": opportunity.content_hash(),
        "opportunity": opportunity,
        "session_replay_id": replay.session_replay_id,
        "session_replay_content_hash_sha256": replay.content_hash(),
        "session_replay": replay,
        **resolved.__dict__,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.missed_opportunity_record.v1",
    }
    return MissedOpportunityRecord(
        miss_record_id=stable_identity("missed-opportunity-record", values),
        opportunity_id=opportunity.opportunity_id,
        opportunity_content_hash_sha256=opportunity.content_hash(),
        opportunity=opportunity,
        session_replay_id=replay.session_replay_id,
        session_replay_content_hash_sha256=replay.content_hash(),
        session_replay=replay,
        **resolved.__dict__,
    )


def _resolve_record(
    opportunity: HindsightQualifiedOpportunity,
    replay: SessionReplay,
) -> _ResolvedRecord:
    projections = tuple(
        _project_run(item.pipeline_result, opportunity) for item in replay.current_outcome_replays
    )
    cutoff = opportunity.latest_useful_cutoff_at
    on_time = tuple(item for item in projections if item.decision_at < cutoff)
    selected = (
        min(
            on_time,
            key=lambda item: (
                -_SURFACING_ORDER[item.state],
                item.decision_at,
                item.run_id,
            ),
        )
        if on_time
        else None
    )
    complete = replay.run_inventory.status is SessionRunInventoryStatus.COMPLETE_AUTHORITATIVE
    disposition = _opportunity_disposition(projections, cutoff, complete=complete)
    category = (
        None
        if disposition is OpportunityDisposition.CAUGHT
        else _classify_category(opportunity, replay, selected)
    )
    reasons = _record_reasons(disposition, category, selected, cutoff)
    limitations = tuple(
        dict.fromkeys(
            (
                "retrospective_research_only_not_promotion_evidence",
                *opportunity.assessment.limitations,
                *replay.limitations,
            )
        )
    )
    return _ResolvedRecord(
        run_projections=projections,
        latest_useful_cutoff_at=cutoff,
        first_discovered_at=_first_at(projections, SurfacingState.DISCOVERED),
        first_strategy_eligible_at=_first_at(
            projections,
            SurfacingState.STRATEGY_ELIGIBLE,
        ),
        first_ranked_at=_first_at(projections, SurfacingState.RANKED),
        first_top_1_at=_first_rank_at(projections, 1),
        first_top_3_at=_first_rank_at(projections, 3),
        first_top_5_at=_first_rank_at(projections, 5),
        first_watched_at=_first_decision_at(projections, TradeDecisionValue.WATCH),
        first_taken_at=_first_decision_at(projections, TradeDecisionValue.TAKE),
        best_on_time_rank_position=min(
            (item.rank_position for item in on_time if item.rank_position is not None),
            default=None,
        ),
        selected_projection_id=selected.projection_id if selected else None,
        selected_projection_content_hash_sha256=(selected.content_hash() if selected else None),
        selected_state=selected.state if selected else None,
        disposition=disposition,
        category=category,
        reasons=reasons,
        limitations=limitations,
    )


def _project_run(result, opportunity) -> OpportunityRunProjection:
    symbol = opportunity.symbol
    direction = opportunity.direction
    directional_types = dict(DIRECTIONAL_DISCOVERY_ANOMALIES_V1)[direction]
    candidate = next(
        (
            item
            for item in result.candidates
            if item.symbol == symbol
            and any(
                anomaly.triggered and anomaly.anomaly_type in directional_types
                for anomaly in item.anomalies
            )
        ),
        None,
    )
    evaluations = (
        tuple(
            item
            for item in result.evaluations
            if item.symbol == symbol and item.direction is direction
        )
        if candidate is not None
        else ()
    )
    pairs = tuple(_pair_projection(result, item) for item in evaluations)
    pair = min(pairs, key=_pair_projection_sort_key) if pairs else None
    evaluation = pair[0] if pair is not None else None
    ranked = pair[1] if pair is not None else None
    decision = pair[2] if pair is not None else None
    trace = (
        next((item for item in result.traces if item.evaluation_id == decision.evaluation_id), None)
        if decision is not None
        else None
    )
    if decision is not None and decision.decision is TradeDecisionValue.TAKE:
        state = SurfacingState.TAKEN
    elif decision is not None and decision.decision is TradeDecisionValue.WATCH:
        state = SurfacingState.WATCHED
    elif ranked is not None:
        state = SurfacingState.RANKED
    elif any(item.status is EvaluationStatus.ELIGIBLE for item in evaluations):
        state = SurfacingState.STRATEGY_ELIGIBLE
    elif candidate is not None:
        state = SurfacingState.DISCOVERED
    else:
        state = SurfacingState.NOT_DISCOVERED
    values = {
        "run_id": result.run_id,
        "run_content_hash_sha256": result.content_hash(),
        "decision_at": result.decision_at,
        "symbol": symbol,
        "direction": direction,
        "state": state,
        "candidate_id": candidate.candidate_id if candidate else None,
        "candidate_content_hash_sha256": candidate.content_hash() if candidate else None,
        "evaluation_id": evaluation.evaluation_id if evaluation else None,
        "evaluation_content_hash_sha256": evaluation.content_hash() if evaluation else None,
        "ranked_id": ranked.ranked_id if ranked else None,
        "ranked_content_hash_sha256": ranked.content_hash() if ranked else None,
        "rank_position": ranked.relative_rank if ranked else None,
        "decision_id": decision.decision_id if decision else None,
        "decision_content_hash_sha256": decision.content_hash() if decision else None,
        "decision_value": decision.decision if decision else None,
        "trace_id": trace.trace_id if trace else None,
        "trace_content_hash_sha256": trace.content_hash() if trace else None,
        "schema_version": "v2.opportunity.opportunity_run_projection.v1",
    }
    return OpportunityRunProjection(
        projection_id=stable_identity("opportunity-run-projection", values),
        **values,
    )


def _pair_projection(result, evaluation):
    ranked = next(
        (
            item
            for item in result.ranked_opportunities
            if item.evaluation_id == evaluation.evaluation_id
        ),
        None,
    )
    decision = next(
        (item for item in result.decisions if item.evaluation_id == evaluation.evaluation_id),
        None,
    )
    return evaluation, ranked, decision


def _pair_projection_sort_key(pair) -> tuple[int, int, int, str, str]:
    evaluation, ranked, decision = pair
    if decision is not None and decision.decision is TradeDecisionValue.TAKE:
        state = SurfacingState.TAKEN
    elif decision is not None and decision.decision is TradeDecisionValue.WATCH:
        state = SurfacingState.WATCHED
    elif ranked is not None:
        state = SurfacingState.RANKED
    elif evaluation.status is EvaluationStatus.ELIGIBLE:
        state = SurfacingState.STRATEGY_ELIGIBLE
    else:
        state = SurfacingState.DISCOVERED
    return (
        -_SURFACING_ORDER[state],
        ranked.relative_rank if ranked is not None else 2**31,
        _evaluation_sort_key(evaluation)[0],
        evaluation.strategy_id,
        evaluation.evaluation_id,
    )


def _classify_category(
    opportunity: HindsightQualifiedOpportunity,
    replay: SessionReplay,
    selected: OpportunityRunProjection | None,
) -> MissCategory:
    if selected is None:
        return MissCategory.UNKNOWN
    result = next(
        item.pipeline_result
        for item in replay.current_outcome_replays
        if item.pipeline_result.run_id == selected.run_id
    )
    member = next(
        (
            item
            for item in (
                *result.preparation.universe_snapshot.included_members,
                *result.preparation.universe_snapshot.excluded_members,
            )
            if item.symbol == opportunity.symbol and not item.benchmark_only
        ),
        None,
    )
    if member is None or member.membership_status is not UniverseMembershipStatus.INCLUDED:
        return MissCategory.UNIVERSE_MISS
    cheap = next(
        (item for item in result.cheap_snapshots if item.symbol == opportunity.symbol),
        None,
    )
    if (
        member.data_availability is not CapabilityState.AVAILABLE
        or cheap is None
        or cheap.data_quality is DataQuality.INSUFFICIENT_DATA
    ):
        return MissCategory.DATA_MISS
    directional_types = dict(DIRECTIONAL_DISCOVERY_ANOMALIES_V1)[opportunity.direction]
    candidate = next(
        (
            item
            for item in result.candidates
            if item.symbol == opportunity.symbol
            and any(
                anomaly.triggered and anomaly.anomaly_type in directional_types
                for anomaly in item.anomalies
            )
        ),
        None,
    )
    if candidate is None:
        return _undiscovered_category(cheap, result.preparation.discovery_config, opportunity)
    evaluation = next(
        (item for item in result.evaluations if item.evaluation_id == selected.evaluation_id),
        None,
    )
    rich = next(
        (item for item in result.rich_snapshots if item.symbol == opportunity.symbol),
        None,
    )
    stage_category = _first_stage_category(
        _rich_feature_category(result, evaluation, rich),
        _regime_category(opportunity, result),
    )
    if stage_category is not None:
        return stage_category
    if evaluation is None or evaluation.status is not EvaluationStatus.ELIGIBLE:
        return MissCategory.STRATEGY_MISS
    decision = next(
        (item for item in result.decisions if item.decision_id == selected.decision_id),
        None,
    )
    if decision is None:
        return MissCategory.UNKNOWN
    risk = next(
        (item for item in result.risk_evidence if item.evaluation_id == decision.evaluation_id),
        None,
    )
    return _gate_category(decision, risk)


def _gate_category(decision, risk) -> MissCategory:
    checks = {item.check_id: item for item in decision.gate_checks}
    if any(checks[item].passed is not True for item in MISS_SCORE_GATE_IDS):
        return MissCategory.SCORING_MISS
    if any(checks[item].passed is not True for item in MISS_NONEXECUTION_QUALITY_GATE_IDS):
        return MissCategory.QUALITY_GATE_MISS
    if any(checks[item].passed is not True for item in MISS_EXECUTION_GATE_IDS) or (
        risk is not None and bool(risk.vetoes)
    ):
        return MissCategory.EXECUTION_FILTER
    return MissCategory.UNKNOWN


def _first_stage_category(
    *categories: MissCategory | None,
) -> MissCategory | None:
    return next((item for item in categories if item is not None), None)


def _undiscovered_category(cheap, config, opportunity) -> MissCategory:
    anomalies = detect_anomalies(cheap, config=config)
    directional_types = dict(DIRECTIONAL_DISCOVERY_ANOMALIES_V1)[opportunity.direction]
    directional = tuple(item for item in anomalies if item.anomaly_type in directional_types)
    available = tuple(item for item in directional if item.availability is Availability.AVAILABLE)
    unavailable = tuple(
        item for item in directional if item.availability is not Availability.AVAILABLE
    )
    if unavailable and not available:
        return MissCategory.FEATURE_MISS
    if unavailable and available:
        return MissCategory.UNKNOWN
    if directional and not any(item.triggered for item in directional):
        return MissCategory.ANOMALY_MISS
    return MissCategory.UNKNOWN


def _rich_feature_category(result, evaluation, rich) -> MissCategory | None:
    if rich is None:
        return MissCategory.DATA_MISS
    definitions = {
        (item.strategy_id, item.version): item for item in result.preparation.registry_definitions
    }
    if evaluation is not None:
        missing = tuple(
            reason.split(":", 1)[1]
            for reason in evaluation.reasons
            if reason.startswith("missing_required_feature:")
        )
        if missing:
            definition = definitions[(evaluation.strategy_id, evaluation.strategy_version)]
            if all(
                name in definition.required_features and name in rich.unavailable_features
                for name in missing
            ):
                return MissCategory.FEATURE_MISS
            return MissCategory.UNKNOWN
    return None


def _regime_category(opportunity, result) -> MissCategory | None:
    evidence = tuple(
        item
        for item in opportunity.assessment.source.retrospective_regime_evidence
        if item.run_id == result.run_id and item.symbol == opportunity.symbol
    )
    if not evidence:
        return None
    item = evidence[0]
    security = next(
        (value for value in result.security_regimes if value.symbol == opportunity.symbol),
        None,
    )
    if security is None:
        return None
    if (
        item.market_regime.state != result.market_regime.state
        or item.security_regime.state != security.state
    ):
        return MissCategory.REGIME_MISCLASSIFICATION
    return None


def _session_disposition(
    qualification: QualificationBatch,
    replay: SessionReplay,
    records: tuple[MissedOpportunityRecord, ...],
) -> MissSessionDisposition:
    statuses = {item.status for item in qualification.assessments}
    unresolved = _unresolved_qualification_disposition(statuses)
    if unresolved is not None:
        return unresolved
    if not records:
        if (
            statuses == {QualificationStatus.NOT_QUALIFIED}
            and qualification.source.scope_receipt.scope_status
            is QualificationSourceScopeStatus.COMPLETE_MARKET
            and replay.run_inventory.status is SessionRunInventoryStatus.COMPLETE_AUTHORITATIVE
        ):
            any_positive = any(
                decision.decision in {TradeDecisionValue.WATCH, TradeDecisionValue.TAKE}
                for current in replay.current_outcome_replays
                for decision in current.pipeline_result.decisions
            )
            return (
                MissSessionDisposition.FALSE_POSITIVE
                if any_positive
                else MissSessionDisposition.CORRECT_NO_TRADE
            )
        return MissSessionDisposition.UNKNOWN
    return _summarize_record_dispositions({item.disposition for item in records})


def _validate_batch_sessions(
    qualification: QualificationBatch,
    replay: SessionReplay,
) -> None:
    scope = qualification.source.scope_receipt
    inventory = replay.run_inventory
    if (
        scope.exchange_session_id != inventory.exchange_session_id
        or scope.session_open_at != inventory.session_open_at
        or scope.session_close_at != inventory.session_close_at
    ):
        raise ValueError("qualification and run replay sessions do not match")
    _validate_regime_replay_bindings(qualification, replay)


def _validate_regime_replay_bindings(
    qualification: QualificationBatch,
    replay: SessionReplay,
) -> None:
    runs = {
        item.pipeline_result.run_id: item.pipeline_result.content_hash()
        for item in replay.current_outcome_replays
    }
    if any(
        runs.get(item.run_id) != item.run_content_hash
        for item in qualification.source.retrospective_regime_evidence
    ):
        raise ValueError("retrospective regime evidence does not match stored session runs")


def _compare_record(value: MissedOpportunityRecord, expected: _ResolvedRecord) -> None:
    for name, expected_value in expected.__dict__.items():
        if getattr(value, name) != expected_value:
            raise ValueError(f"miss record {name} does not recompute")


def _evaluation_sort_key(item) -> tuple[int, str, str]:
    priority = {
        EvaluationStatus.ELIGIBLE: 0,
        EvaluationStatus.INSUFFICIENT_DATA: 1,
        EvaluationStatus.REJECTED: 2,
        EvaluationStatus.DISABLED: 3,
    }
    return priority[item.status], item.strategy_id, item.evaluation_id


def _require_sanitized_values(values: tuple[str, ...], label: str) -> None:
    require_unique(values, label)
    for value in values:
        require_sanitized(value, label)


__all__ = [
    "MissReconciliationBatch",
    "MissedOpportunityRecord",
    "OpportunityRunProjection",
    "reconcile_missed_opportunities",
]
