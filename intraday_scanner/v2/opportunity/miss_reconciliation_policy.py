"""Pure timing and disposition policy for missed-opportunity reconciliation."""

from __future__ import annotations

from datetime import datetime

from intraday_scanner.v2.opportunity.miss_contracts import (
    MissCategory,
    MissSessionDisposition,
    OpportunityDisposition,
    QualificationStatus,
    SurfacingState,
)
from intraday_scanner.v2.opportunity.miss_projection import (
    SURFACING_ORDER,
    OpportunityRunProjection,
)
from intraday_scanner.v2.opportunity.miss_qualification import QualificationBatch
from intraday_scanner.v2.opportunity.miss_replay import SessionReplay
from intraday_scanner.v2.opportunity.models import TradeDecisionValue


def opportunity_disposition(
    projections: tuple[OpportunityRunProjection, ...],
    cutoff: datetime,
    *,
    complete: bool,
) -> OpportunityDisposition:
    caught = {TradeDecisionValue.WATCH, TradeDecisionValue.TAKE}
    if any(item.decision_at < cutoff and item.decision_value in caught for item in projections):
        return OpportunityDisposition.CAUGHT
    if not complete:
        return OpportunityDisposition.UNKNOWN
    if any(item.decision_at >= cutoff and item.decision_value in caught for item in projections):
        return OpportunityDisposition.TOO_LATE
    return OpportunityDisposition.MISSED


def unresolved_qualification_disposition(
    statuses: set[QualificationStatus],
) -> MissSessionDisposition | None:
    if QualificationStatus.PENDING in statuses:
        return MissSessionDisposition.PENDING
    if QualificationStatus.CENSORED in statuses:
        return MissSessionDisposition.CENSORED
    if QualificationStatus.UNAVAILABLE in statuses:
        return MissSessionDisposition.UNAVAILABLE
    return None


def summarize_record_dispositions(
    dispositions: set[OpportunityDisposition],
) -> MissSessionDisposition:
    if OpportunityDisposition.UNKNOWN in dispositions:
        return MissSessionDisposition.UNKNOWN
    missed = OpportunityDisposition.MISSED in dispositions
    late = OpportunityDisposition.TOO_LATE in dispositions
    caught = OpportunityDisposition.CAUGHT in dispositions
    if len(dispositions) > 1:
        return MissSessionDisposition.MIXED
    if missed:
        return MissSessionDisposition.MISSED
    if late:
        return MissSessionDisposition.TOO_LATE
    if caught:
        return MissSessionDisposition.CAUGHT
    return MissSessionDisposition.UNKNOWN


def first_at(
    projections: tuple[OpportunityRunProjection, ...],
    minimum_state: SurfacingState,
) -> datetime | None:
    return min(
        (
            item.decision_at
            for item in projections
            if SURFACING_ORDER[item.state] >= SURFACING_ORDER[minimum_state]
        ),
        default=None,
    )


def first_decision_at(
    projections: tuple[OpportunityRunProjection, ...],
    decision: TradeDecisionValue,
) -> datetime | None:
    return min(
        (item.decision_at for item in projections if item.decision_value is decision),
        default=None,
    )


def first_rank_at(
    projections: tuple[OpportunityRunProjection, ...],
    maximum_rank: int,
) -> datetime | None:
    return min(
        (
            item.decision_at
            for item in projections
            if item.rank_position is not None and item.rank_position <= maximum_rank
        ),
        default=None,
    )


def record_reasons(
    disposition: OpportunityDisposition,
    category: MissCategory | None,
    selected: OpportunityRunProjection | None,
    cutoff: datetime,
) -> tuple[str, ...]:
    values = [f"opportunity_disposition:{disposition.value}"]
    if category is not None:
        values.append(f"miss_category:{category.value}")
    if selected is not None:
        values.append(f"selected_stage:{selected.state.value}")
    values.append(f"latest_useful_cutoff:{cutoff.isoformat()}")
    return tuple(values)


def batch_limitations(
    qualification: QualificationBatch,
    replay: SessionReplay,
) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(
            (
                "retrospective_research_only_not_promotion_evidence",
                "stored_current_outcome_heads_no_live_lookup",
                *qualification.limitations,
                *replay.limitations,
            )
        )
    )


__all__ = [
    "batch_limitations",
    "first_at",
    "first_decision_at",
    "first_rank_at",
    "opportunity_disposition",
    "record_reasons",
    "summarize_record_dispositions",
    "unresolved_qualification_disposition",
]
