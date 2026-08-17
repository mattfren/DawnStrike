"""Strict downstream contracts for retrospective miss analysis.

This module is deliberately unreachable from the real-time opportunity import
graph.  It contains no source lookup, persistence, runtime, or current-clock
behavior.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.opportunity.models import (
    AnomalyType,
    EvidenceKind,
    OpportunityContract,
    StrategyDirection,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_aware,
    _require_hash,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
)


class QualificationSourceAuthorityClaim(str, Enum):
    MARKET_COMPLETE = "market_complete"
    BOUNDED_COHORT = "bounded_cohort"
    NO_AUTHORITY = "no_authority"


class QualificationSourceScopeStatus(str, Enum):
    COMPLETE_MARKET = "complete_market"
    COMPLETE_BOUNDED = "complete_bounded"
    PARTIAL = "partial"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


class SessionRunInventoryStatus(str, Enum):
    COMPLETE_AUTHORITATIVE = "complete_authoritative"
    COMPLETE_BOUNDED = "complete_bounded"
    PARTIAL = "partial"
    PENDING = "pending"
    UNAVAILABLE = "unavailable"


class QualificationMemberStatus(str, Enum):
    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


class QualificationHorizonKind(str, Enum):
    ELAPSED_FROM_ENTRY = "elapsed_from_entry"
    SESSION_CLOSE = "session_close"


class QualificationClaimKind(str, Enum):
    EXECUTABLE_TRADE = "executable_trade"
    PRICE_MOVE_PROXY = "price_move_proxy"
    NONE = "none"


class QualificationExecutionStatus(str, Enum):
    AVAILABLE = "available"
    PROVISIONAL = "provisional"
    UNAVAILABLE = "unavailable"


class QualificationStatus(str, Enum):
    QUALIFIED = "qualified"
    NOT_QUALIFIED = "not_qualified"
    PENDING = "pending"
    CENSORED = "censored"
    UNAVAILABLE = "unavailable"


class QualificationPathStatus(str, Enum):
    TARGET_FIRST = "target_first"
    STOP_FIRST = "stop_first"
    NO_TARGET = "no_target"
    ENTRY_BAR_AMBIGUOUS = "entry_bar_ambiguous"
    SAME_BAR_AMBIGUOUS = "same_bar_ambiguous"
    GAP_THROUGH_AMBIGUOUS = "gap_through_ambiguous"
    HALT_CENSORED = "halt_censored"
    CORPORATE_ACTION_CENSORED = "corporate_action_censored"
    MISSING_BARS = "missing_bars"
    PENDING_HORIZON = "pending_horizon"
    UNSUPPORTED_EVIDENCE = "unsupported_evidence"


class QualificationValueStatus(str, Enum):
    OBSERVED = "observed"
    DERIVED = "derived"
    UNAVAILABLE = "unavailable"


class QualificationMetric(str, Enum):
    REFERENCE_PRICE = "reference_price"
    STOP_PRICE = "stop_price"
    TARGET_PRICE = "target_price"
    STOP_DISTANCE = "stop_distance"
    GROSS_REWARD_RISK = "gross_reward_risk"
    REQUIRED_MOVE_FRACTION = "required_move_fraction"
    PER_SHARE_COST = "per_share_cost"
    AFTER_COST_REWARD_RISK = "after_cost_reward_risk"
    EXECUTABLE_QUANTITY = "executable_quantity"


class QualificationUnit(str, Enum):
    USD_PER_SHARE = "usd_per_share"
    RATIO = "ratio"
    FRACTION = "fraction"
    SHARES = "shares"


class MissCategory(str, Enum):
    UNIVERSE_MISS = "universe_miss"
    DATA_MISS = "data_miss"
    FEATURE_MISS = "feature_miss"
    ANOMALY_MISS = "anomaly_miss"
    REGIME_MISCLASSIFICATION = "regime_misclassification"
    STRATEGY_MISS = "strategy_miss"
    SCORING_MISS = "scoring_miss"
    QUALITY_GATE_MISS = "quality_gate_miss"
    EXECUTION_FILTER = "execution_filter"
    UNKNOWN = "unknown"


class SurfacingState(str, Enum):
    NOT_DISCOVERED = "not_discovered"
    DISCOVERED = "discovered"
    STRATEGY_ELIGIBLE = "strategy_eligible"
    RANKED = "ranked"
    WATCHED = "watched"
    TAKEN = "taken"


class OpportunityDisposition(str, Enum):
    CAUGHT = "caught"
    MISSED = "missed"
    TOO_LATE = "too_late"
    UNKNOWN = "unknown"


class MissSessionDisposition(str, Enum):
    CORRECT_NO_TRADE = "correct_no_trade"
    FALSE_POSITIVE = "false_positive"
    CAUGHT = "caught"
    MISSED = "missed"
    TOO_LATE = "too_late"
    MIXED = "mixed"
    PENDING = "pending"
    CENSORED = "censored"
    UNAVAILABLE = "unavailable"
    UNKNOWN = "unknown"


DIRECTIONAL_DISCOVERY_ANOMALIES_V1: tuple[
    tuple[StrategyDirection, tuple[AnomalyType, ...]], ...
] = (
    (StrategyDirection.LONG, (
        AnomalyType.BREAKOUT,
        AnomalyType.VWAP_PROXY_RECLAIM,
        AnomalyType.MARKET_RELATIVE_STRENGTH,
    )),
    (StrategyDirection.SHORT, (
        AnomalyType.BREAKDOWN,
        AnomalyType.VWAP_PROXY_LOSS,
        AnomalyType.MARKET_RELATIVE_WEAKNESS,
    )),
)

MISS_SCORE_GATE_IDS = (
    "absolute_watch_score",
    "absolute_take_score",
)
MISS_NONEXECUTION_QUALITY_GATE_IDS = (
    "evaluation_eligible",
    "research_watch_lifecycle",
    "data_quality",
    "production_lifecycle",
    "empirical_expectancy_available",
    "expectancy_sample",
    "expectancy_positive",
    "expectancy_uncertainty",
)
MISS_EXECUTION_GATE_IDS = (
    "liquidity",
    "gross_reward_risk",
    "after_cost_reward_risk",
    "risk_policy_minimum_available",
    "execution_risk_vetoes",
    "execution_risk_empirical",
)


_METRIC_UNITS = {
    QualificationMetric.REFERENCE_PRICE: QualificationUnit.USD_PER_SHARE,
    QualificationMetric.STOP_PRICE: QualificationUnit.USD_PER_SHARE,
    QualificationMetric.TARGET_PRICE: QualificationUnit.USD_PER_SHARE,
    QualificationMetric.STOP_DISTANCE: QualificationUnit.USD_PER_SHARE,
    QualificationMetric.GROSS_REWARD_RISK: QualificationUnit.RATIO,
    QualificationMetric.REQUIRED_MOVE_FRACTION: QualificationUnit.FRACTION,
    QualificationMetric.PER_SHARE_COST: QualificationUnit.USD_PER_SHARE,
    QualificationMetric.AFTER_COST_REWARD_RISK: QualificationUnit.RATIO,
    QualificationMetric.EXECUTABLE_QUANTITY: QualificationUnit.SHARES,
}
_OBSERVED_QUALIFICATION_METRICS = {
    QualificationMetric.REFERENCE_PRICE,
    QualificationMetric.EXECUTABLE_QUANTITY,
}


class MissContract(OutcomeContract):
    """Strict JSON/content boundary shared by downstream miss artifacts."""


@dataclass(frozen=True)
class SessionQualificationHorizon(MissContract):
    horizon_id: str
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    entry_anchor_at: datetime
    kind: QualificationHorizonKind
    end_at: datetime
    elapsed_seconds: int | None
    schema_version: str = "v2.opportunity.session_qualification_horizon.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_schema(
            self.schema_version,
            "v2.opportunity.session_qualification_horizon.v1",
        )
        _require_identity(self.horizon_id, "horizon_id")
        _require_sanitized_text(self.exchange_session_id, "exchange_session_id")
        for value, name in (
            (self.session_open_at, "session_open_at"),
            (self.session_close_at, "session_close_at"),
            (self.entry_anchor_at, "entry_anchor_at"),
            (self.end_at, "end_at"),
        ):
            _require_utc(value, name)
        if not self.session_open_at <= self.entry_anchor_at < self.end_at <= self.session_close_at:
            raise ValueError("qualification horizon must lie inside its session")
        if self.kind is QualificationHorizonKind.ELAPSED_FROM_ENTRY:
            _require_positive_int(self.elapsed_seconds, "elapsed_seconds")
            assert self.elapsed_seconds is not None
            if self.end_at != self.entry_anchor_at + timedelta(seconds=self.elapsed_seconds):
                raise ValueError("elapsed qualification horizon end is inconsistent")
        elif self.elapsed_seconds is not None or self.end_at != self.session_close_at:
            raise ValueError("session-close qualification horizon must end at session close")
        expected = stable_identity(
            "session-qualification-horizon",
            _identity_payload(self, "horizon_id"),
        )
        if self.horizon_id != expected:
            raise ValueError("qualification horizon identity does not match content")


@dataclass(frozen=True)
class MissQualificationPolicy(MissContract):
    policy_id: str
    policy_version: str
    expected_bar_interval_seconds: int
    entry_anchor_offset_seconds: int
    stop_distance_fraction: Decimal
    minimum_gross_reward_risk: Decimal
    minimum_after_cost_reward_risk: Decimal
    minimum_executable_quantity_shares: int
    directional_anomaly_mapping_version: str = "directional-discovery-anomalies-v1"
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.miss_qualification_policy.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_schema(
            self.schema_version,
            "v2.opportunity.miss_qualification_policy.v1",
        )
        _require_identity(self.policy_id, "policy_id")
        _require_sanitized_text(self.policy_version, "policy_version")
        _require_positive_int(
            self.expected_bar_interval_seconds,
            "expected_bar_interval_seconds",
        )
        _require_nonnegative_int(
            self.entry_anchor_offset_seconds,
            "entry_anchor_offset_seconds",
        )
        if self.entry_anchor_offset_seconds % self.expected_bar_interval_seconds:
            raise ValueError("entry anchor offset must align to the expected bar interval")
        for value, name in (
            (self.stop_distance_fraction, "stop_distance_fraction"),
            (self.minimum_gross_reward_risk, "minimum_gross_reward_risk"),
            (self.minimum_after_cost_reward_risk, "minimum_after_cost_reward_risk"),
        ):
            _require_positive_decimal(value, name)
        _require_positive_int(
            self.minimum_executable_quantity_shares,
            "minimum_executable_quantity_shares",
        )
        if self.directional_anomaly_mapping_version != "directional-discovery-anomalies-v1":
            raise ValueError("unsupported directional anomaly mapping version")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("miss qualification policy must remain research-only")
        expected = stable_identity(
            "miss-qualification-policy",
            _identity_payload(self, "policy_id"),
        )
        if self.policy_id != expected:
            raise ValueError("miss qualification policy identity does not match content")


@dataclass(frozen=True)
class QualificationNumericEvidence(MissContract):
    metric: QualificationMetric
    unit: QualificationUnit
    value: Decimal | None
    status: QualificationValueStatus
    evidence_kind: EvidenceKind
    observed_at: datetime | None
    source_ids: tuple[str, ...]
    method: str
    reason: str | None = None
    schema_version: str = "v2.opportunity.qualification_numeric_evidence.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        _require_schema(
            self.schema_version,
            "v2.opportunity.qualification_numeric_evidence.v1",
        )
        if self.unit is not _METRIC_UNITS[self.metric]:
            raise ValueError("qualification metric unit does not match metric")
        _require_sanitized_text(self.method, "qualification metric method")
        _require_unique(list(self.source_ids), "qualification metric source")
        for source_id in self.source_ids:
            _require_identity(source_id, "qualification metric source ID")
        if self.status is QualificationValueStatus.UNAVAILABLE:
            if self.value is not None or self.observed_at is not None or self.source_ids:
                raise ValueError("unavailable qualification metric cannot carry value lineage")
            if self.reason is None:
                raise ValueError("unavailable qualification metric requires a reason")
            _require_sanitized_text(self.reason, "qualification metric reason")
        else:
            if type(self.value) is not Decimal or not self.value.is_finite():
                raise ValueError("available qualification metric requires a finite Decimal")
            if self.observed_at is None or not self.source_ids:
                raise ValueError("available qualification metric requires causal lineage")
            _require_utc(self.observed_at, "qualification metric observed_at")
            if self.reason is not None:
                raise ValueError("available qualification metric cannot carry a reason")
            expected_status = (
                QualificationValueStatus.OBSERVED
                if self.metric in _OBSERVED_QUALIFICATION_METRICS
                else QualificationValueStatus.DERIVED
            )
            if self.status is not expected_status:
                raise ValueError("qualification metric provenance role is inconsistent")
        if self.metric is QualificationMetric.EXECUTABLE_QUANTITY and self.value is not None:
            if self.value != self.value.to_integral_value() or self.value <= 0:
                raise ValueError("executable quantity must be positive and integral")


def build_session_qualification_horizon(
    *,
    exchange_session_id: str,
    session_open_at: datetime,
    session_close_at: datetime,
    entry_anchor_offset_seconds: int,
    kind: QualificationHorizonKind,
    elapsed_seconds: int | None = None,
) -> SessionQualificationHorizon:
    _require_nonnegative_int(entry_anchor_offset_seconds, "entry_anchor_offset_seconds")
    _require_utc(session_open_at, "session_open_at")
    _require_utc(session_close_at, "session_close_at")
    entry_anchor = session_open_at + timedelta(seconds=entry_anchor_offset_seconds)
    end_at = (
        entry_anchor + timedelta(seconds=elapsed_seconds)
        if kind is QualificationHorizonKind.ELAPSED_FROM_ENTRY
        and isinstance(elapsed_seconds, int)
        else session_close_at
    )
    values = {
        "exchange_session_id": exchange_session_id,
        "session_open_at": session_open_at,
        "session_close_at": session_close_at,
        "entry_anchor_at": entry_anchor,
        "kind": kind,
        "end_at": end_at,
        "elapsed_seconds": elapsed_seconds,
        "schema_version": "v2.opportunity.session_qualification_horizon.v1",
    }
    return SessionQualificationHorizon(
        horizon_id=stable_identity("session-qualification-horizon", values),
        exchange_session_id=exchange_session_id,
        session_open_at=session_open_at,
        session_close_at=session_close_at,
        entry_anchor_at=entry_anchor,
        kind=kind,
        end_at=end_at,
        elapsed_seconds=elapsed_seconds,
    )


def build_miss_qualification_policy(
    *,
    policy_version: str,
    expected_bar_interval_seconds: int,
    entry_anchor_offset_seconds: int,
    stop_distance_fraction: Decimal,
    minimum_gross_reward_risk: Decimal,
    minimum_after_cost_reward_risk: Decimal,
    minimum_executable_quantity_shares: int,
) -> MissQualificationPolicy:
    values = {
        "policy_version": policy_version,
        "expected_bar_interval_seconds": expected_bar_interval_seconds,
        "entry_anchor_offset_seconds": entry_anchor_offset_seconds,
        "stop_distance_fraction": stop_distance_fraction,
        "minimum_gross_reward_risk": minimum_gross_reward_risk,
        "minimum_after_cost_reward_risk": minimum_after_cost_reward_risk,
        "minimum_executable_quantity_shares": minimum_executable_quantity_shares,
        "directional_anomaly_mapping_version": "directional-discovery-anomalies-v1",
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.miss_qualification_policy.v1",
    }
    return MissQualificationPolicy(
        policy_id=stable_identity("miss-qualification-policy", values),
        policy_version=policy_version,
        expected_bar_interval_seconds=expected_bar_interval_seconds,
        entry_anchor_offset_seconds=entry_anchor_offset_seconds,
        stop_distance_fraction=stop_distance_fraction,
        minimum_gross_reward_risk=minimum_gross_reward_risk,
        minimum_after_cost_reward_risk=minimum_after_cost_reward_risk,
        minimum_executable_quantity_shares=minimum_executable_quantity_shares,
    )


def require_hash(value: str, field_name: str) -> None:
    _require_hash(value, field_name)


def require_identity(value: str, field_name: str) -> None:
    _require_identity(value, field_name)


def require_aware(value: datetime, field_name: str) -> None:
    _require_aware(value, field_name)


def require_sanitized(value: str, field_name: str) -> None:
    _require_sanitized_text(value, field_name)


def require_utc(value: datetime, field_name: str) -> None:
    _require_utc(value, field_name)


def require_schema(value: str, expected: str) -> None:
    _require_schema(value, expected)


def require_unique(values: tuple[str, ...], label: str) -> None:
    _require_unique(list(values), label)


def identity_payload(
    value: OpportunityContract, identity_field: str
) -> dict[str, object]:
    return _identity_payload(value, identity_field)


def _require_positive_decimal(value: Decimal, field_name: str) -> None:
    if type(value) is not Decimal or not value.is_finite() or value <= 0:
        raise ValueError(f"{field_name} must be a finite positive Decimal")


def _require_positive_int(value: int | None, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{field_name} must be a positive integer")


def _require_nonnegative_int(value: int, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field_name} must be a nonnegative integer")


__all__ = [
    "DIRECTIONAL_DISCOVERY_ANOMALIES_V1",
    "MISS_EXECUTION_GATE_IDS",
    "MISS_NONEXECUTION_QUALITY_GATE_IDS",
    "MISS_SCORE_GATE_IDS",
    "MissCategory",
    "MissContract",
    "MissQualificationPolicy",
    "MissSessionDisposition",
    "OpportunityDisposition",
    "QualificationClaimKind",
    "QualificationExecutionStatus",
    "QualificationHorizonKind",
    "QualificationMemberStatus",
    "QualificationMetric",
    "QualificationNumericEvidence",
    "QualificationPathStatus",
    "QualificationSourceAuthorityClaim",
    "QualificationSourceScopeStatus",
    "QualificationStatus",
    "QualificationUnit",
    "QualificationValueStatus",
    "SessionQualificationHorizon",
    "SessionRunInventoryStatus",
    "SurfacingState",
    "build_miss_qualification_policy",
    "build_session_qualification_horizon",
]
