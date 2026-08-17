"""Standalone directional run projection contract for miss reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    SurfacingState,
    identity_payload,
    require_hash,
    require_identity,
    require_schema,
)
from intraday_scanner.v2.opportunity.models import (
    StrategyDirection,
    TradeDecisionValue,
    stable_identity,
)

SURFACING_ORDER = {
    SurfacingState.NOT_DISCOVERED: 0,
    SurfacingState.DISCOVERED: 1,
    SurfacingState.STRATEGY_ELIGIBLE: 2,
    SurfacingState.RANKED: 3,
    SurfacingState.WATCHED: 4,
    SurfacingState.TAKEN: 5,
}


@dataclass(frozen=True)
class OpportunityRunProjection(MissContract):
    projection_id: str
    run_id: str
    run_content_hash_sha256: str
    decision_at: datetime
    symbol: str
    direction: StrategyDirection
    state: SurfacingState
    candidate_id: str | None
    candidate_content_hash_sha256: str | None
    evaluation_id: str | None
    evaluation_content_hash_sha256: str | None
    ranked_id: str | None
    ranked_content_hash_sha256: str | None
    rank_position: int | None
    decision_id: str | None
    decision_content_hash_sha256: str | None
    decision_value: TradeDecisionValue | None
    trace_id: str | None
    trace_content_hash_sha256: str | None
    schema_version: str = "v2.opportunity.opportunity_run_projection.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.opportunity_run_projection.v1",
        )
        for value, name in (
            (self.projection_id, "projection_id"),
            (self.run_id, "run_id"),
            (self.symbol, "symbol"),
        ):
            require_identity(value, name)
        require_hash(self.run_content_hash_sha256, "run_content_hash_sha256")
        if self.direction not in {StrategyDirection.LONG, StrategyDirection.SHORT}:
            raise ValueError("run projection requires exact LONG or SHORT direction")
        if self.decision_at.tzinfo is None or self.decision_at.utcoffset() is None:
            raise ValueError("run projection decision_at must be timezone-aware")
        _require_optional_pair(
            self.candidate_id,
            self.candidate_content_hash_sha256,
            "candidate",
        )
        _require_optional_pair(
            self.evaluation_id,
            self.evaluation_content_hash_sha256,
            "evaluation",
        )
        _require_optional_pair(self.ranked_id, self.ranked_content_hash_sha256, "rank")
        _require_optional_pair(
            self.decision_id,
            self.decision_content_hash_sha256,
            "decision",
        )
        _require_optional_pair(self.trace_id, self.trace_content_hash_sha256, "trace")
        if (self.rank_position is None) is not (self.ranked_id is None):
            raise ValueError("rank position and rank identity must be present together")
        if self.rank_position is not None and (
            isinstance(self.rank_position, bool) or self.rank_position <= 0
        ):
            raise ValueError("run projection rank position must be positive")
        if (self.decision_value is None) is not (self.decision_id is None):
            raise ValueError("decision value and identity must be present together")
        _validate_projection_shape(self)
        expected = stable_identity(
            "opportunity-run-projection",
            identity_payload(self, "projection_id"),
        )
        if self.projection_id != expected:
            raise ValueError("opportunity run projection identity does not match content")


def _validate_projection_shape(value: OpportunityRunProjection) -> None:
    if value.state is SurfacingState.NOT_DISCOVERED and any(
        item is not None
        for item in (
            value.candidate_id,
            value.evaluation_id,
            value.ranked_id,
            value.decision_id,
        )
    ):
        raise ValueError("not-discovered run projection cannot carry downstream artifacts")
    if SURFACING_ORDER[value.state] >= SURFACING_ORDER[SurfacingState.DISCOVERED]:
        if value.candidate_id is None:
            raise ValueError("discovered run projection requires candidate evidence")
    if SURFACING_ORDER[value.state] >= SURFACING_ORDER[SurfacingState.STRATEGY_ELIGIBLE]:
        if value.evaluation_id is None:
            raise ValueError("eligible run projection requires evaluation evidence")
    if SURFACING_ORDER[value.state] >= SURFACING_ORDER[SurfacingState.RANKED]:
        if value.ranked_id is None:
            raise ValueError("ranked run projection requires rank evidence")
    if (
        value.state is SurfacingState.WATCHED
        and value.decision_value is not TradeDecisionValue.WATCH
    ):
        raise ValueError("watched projection requires WATCH decision")
    if value.state is SurfacingState.TAKEN and value.decision_value is not TradeDecisionValue.TAKE:
        raise ValueError("taken projection requires TAKE decision")
    if value.decision_id is not None and value.trace_id is None:
        raise ValueError("decision projection requires exact pair trace")


def _require_optional_pair(
    identity: str | None,
    content_hash: str | None,
    label: str,
) -> None:
    if (identity is None) is not (content_hash is None):
        raise ValueError(f"{label} identity and hash must be paired")
    if identity is not None:
        require_identity(identity, f"{label}_id")
        require_hash(content_hash or "", f"{label}_content_hash_sha256")


__all__ = ["OpportunityRunProjection", "SURFACING_ORDER"]
