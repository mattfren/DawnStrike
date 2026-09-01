"""Canonical bounded product projection for persisted opportunity research."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from zoneinfo import ZoneInfo

from intraday_scanner.v2.opportunity.models import TradeDecisionValue
from intraday_scanner.v2.opportunity.pipeline import PipelineResult

MAX_OPPORTUNITY_ROWS = 5
MAX_TEXT_ITEMS = 24
MAX_ANOMALIES = 20
MAX_TEXT_LENGTH = 240
MARKET_TIMEZONE = ZoneInfo("America/New_York")
NO_QUALIFYING_MESSAGE = "NO QUALIFYING TRADE CURRENTLY EXISTS."
NOT_AVAILABLE = "Not available"


class OpportunityProjectionState(str, Enum):
    DISABLED = "DISABLED"
    DATA_UNAVAILABLE = "DATA_UNAVAILABLE"
    NO_QUALIFYING = "NO_QUALIFYING"
    QUALIFYING = "QUALIFYING"


class OpportunityProjectionReason(str, Enum):
    FEATURE_DISABLED = "FEATURE_DISABLED"
    DATABASE_MISSING = "DATABASE_MISSING"
    SCHEMA_UNSUPPORTED = "SCHEMA_UNSUPPORTED"
    NO_PERSISTED_RUN = "NO_PERSISTED_RUN"
    DATABASE_INVALID = "DATABASE_INVALID"
    REPLAY_FAILED = "REPLAY_FAILED"
    READ_FAILED = "READ_FAILED"


_UNAVAILABLE_MESSAGES = {
    OpportunityProjectionReason.DATABASE_MISSING: (
        "Opportunity data is unavailable because no persisted research store was found."
    ),
    OpportunityProjectionReason.SCHEMA_UNSUPPORTED: (
        "Opportunity data is unavailable because the persisted research schema is not supported."
    ),
    OpportunityProjectionReason.NO_PERSISTED_RUN: (
        "Opportunity data is unavailable because no verified persisted run exists."
    ),
    OpportunityProjectionReason.DATABASE_INVALID: (
        "Opportunity data is unavailable because the persisted research store "
        "could not be verified."
    ),
    OpportunityProjectionReason.REPLAY_FAILED: (
        "Opportunity data is unavailable because the latest persisted run did not verify."
    ),
    OpportunityProjectionReason.READ_FAILED: (
        "Opportunity data is unavailable because the read-only research store could not be read."
    ),
}

_UNSAFE_TEXT = re.compile(
    r"(?i)(?:"
    r"[A-Za-z]:[\\/]|\\\\|/(?:Users|home|var|opt|tmp)/|https?://|"
    r"(?:api[_-]?key|secret|access[_-]?token|password|authorization)\s*[:=]|"
    r"\bbearer\s+|\b(?:select|insert|update|delete|pragma|drop\s+table|create\s+table)\b|"
    r"[<>]|[\x00-\x08\x0b\x0c\x0e-\x1f]"
    r")"
)


@dataclass(frozen=True)
class OpportunityAnomalyProjection:
    name: str
    strength: Decimal | None
    evidence_kind: str

    def __post_init__(self) -> None:
        _validate_text(self.name, "anomaly name")
        _validate_text(self.evidence_kind, "anomaly evidence kind")
        _validate_decimal(self.strength, "anomaly strength")

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "strength": _decimal_json(self.strength),
            "evidence_kind": self.evidence_kind,
        }


@dataclass(frozen=True)
class OpportunityRowProjection:
    rank: int
    symbol: str
    strategy_id: str
    strategy_version: str
    direction: str
    decision: str
    lifecycle: str
    evidence_kind: str
    validation_wording: str
    market_regime: str
    market_regime_evidence_kind: str
    security_regime: str
    security_regime_evidence_kind: str
    triggered_anomalies: tuple[OpportunityAnomalyProjection, ...]
    liquidity_score: Decimal | None
    liquidity_evidence_kind: str | None
    why: tuple[str, ...]
    risks: tuple[str, ...]
    vetoes: tuple[str, ...]
    entry_price: Decimal | None
    invalidation_price: Decimal | None
    target_price: Decimal | None
    limitations: tuple[str, ...]
    research_only: bool = True
    order_execution_enabled: bool = False

    def __post_init__(self) -> None:
        if self.rank < 1:
            raise ValueError("projection rank must be positive")
        if not self.research_only or self.order_execution_enabled:
            raise ValueError("opportunity projection rows must remain research-only")
        for value, label in (
            (self.symbol, "symbol"),
            (self.strategy_id, "strategy ID"),
            (self.strategy_version, "strategy version"),
            (self.direction, "direction"),
            (self.decision, "decision"),
            (self.lifecycle, "lifecycle"),
            (self.evidence_kind, "evidence kind"),
            (self.validation_wording, "validation wording"),
            (self.market_regime, "market regime"),
            (self.market_regime_evidence_kind, "market regime evidence kind"),
            (self.security_regime, "security regime"),
            (self.security_regime_evidence_kind, "security regime evidence kind"),
        ):
            _validate_text(value, label)
        if self.decision not in {
            TradeDecisionValue.WATCH.value,
            TradeDecisionValue.TAKE.value,
        }:
            raise ValueError("qualifying projection rows must be WATCH or TAKE")
        if self.liquidity_evidence_kind is not None:
            _validate_text(self.liquidity_evidence_kind, "liquidity evidence kind")
        for numeric_value, numeric_label in (
            (self.liquidity_score, "liquidity score"),
            (self.entry_price, "entry price"),
            (self.invalidation_price, "invalidation price"),
            (self.target_price, "target price"),
        ):
            _validate_decimal(numeric_value, numeric_label)
        if len(self.triggered_anomalies) > MAX_ANOMALIES:
            raise ValueError("projection anomaly count exceeds bound")
        for values, label in (
            (self.why, "why"),
            (self.risks, "risk"),
            (self.vetoes, "veto"),
            (self.limitations, "limitation"),
        ):
            if len(values) > MAX_TEXT_ITEMS or len(values) != len(set(values)):
                raise ValueError(f"projection {label} collection is not canonical")
            for value in values:
                _validate_text(value, label)

    def to_dict(self) -> dict[str, object]:
        return {
            "rank": self.rank,
            "symbol": self.symbol,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "direction": self.direction,
            "decision": self.decision,
            "lifecycle": self.lifecycle,
            "evidence_kind": self.evidence_kind,
            "validation_wording": self.validation_wording,
            "market_regime": self.market_regime,
            "market_regime_evidence_kind": self.market_regime_evidence_kind,
            "security_regime": self.security_regime,
            "security_regime_evidence_kind": self.security_regime_evidence_kind,
            "triggered_anomalies": [item.to_dict() for item in self.triggered_anomalies],
            "liquidity_score": _decimal_json(self.liquidity_score),
            "liquidity_evidence_kind": self.liquidity_evidence_kind,
            "why": list(self.why),
            "risks": list(self.risks),
            "vetoes": list(self.vetoes),
            "entry_price": _decimal_json(self.entry_price),
            "invalidation_price": _decimal_json(self.invalidation_price),
            "target_price": _decimal_json(self.target_price),
            "limitations": list(self.limitations),
            "research_only": self.research_only,
            "order_execution_enabled": self.order_execution_enabled,
        }


@dataclass(frozen=True)
class OpportunityProjection:
    state: OpportunityProjectionState
    reason_code: OpportunityProjectionReason | None
    message: str
    source_run_id: str | None
    as_of: datetime | None
    rows: tuple[OpportunityRowProjection, ...]
    research_only: bool = True
    order_execution_enabled: bool = False
    schema_version: str = "dawnstrike.opportunity_projection.v1"

    def __post_init__(self) -> None:
        if not self.research_only or self.order_execution_enabled:
            raise ValueError("opportunity projection must remain research-only")
        _validate_text(self.message, "projection message")
        _validate_text(self.schema_version, "projection schema version")
        if self.source_run_id is not None:
            _validate_text(self.source_run_id, "source run ID")
        if len(self.rows) > MAX_OPPORTUNITY_ROWS:
            raise ValueError("opportunity projection exceeds row bound")
        ranks = tuple(row.rank for row in self.rows)
        if ranks != tuple(sorted(ranks)) or len(ranks) != len(set(ranks)):
            raise ValueError("opportunity projection rows must preserve rank order")
        if self.state is OpportunityProjectionState.DISABLED:
            if self.reason_code is not OpportunityProjectionReason.FEATURE_DISABLED:
                raise ValueError("disabled projection requires FEATURE_DISABLED")
            if self.rows or self.source_run_id is not None or self.as_of is not None:
                raise ValueError("disabled projection cannot expose source data")
        elif self.state is OpportunityProjectionState.DATA_UNAVAILABLE:
            if self.reason_code not in _UNAVAILABLE_MESSAGES:
                raise ValueError("unavailable projection requires a bounded reason")
            if self.rows or self.source_run_id is not None or self.as_of is not None:
                raise ValueError("unavailable projection cannot expose unverified source data")
        elif self.state is OpportunityProjectionState.NO_QUALIFYING:
            if self.reason_code is not None or self.message != NO_QUALIFYING_MESSAGE:
                raise ValueError("no-qualifying projection requires the exact no-trade sentence")
            if self.rows or self.source_run_id is None or self.as_of is None:
                raise ValueError("no-qualifying projection requires one verified run")
        elif self.state is OpportunityProjectionState.QUALIFYING:
            if self.reason_code is not None or not self.rows:
                raise ValueError("qualifying projection requires bounded rows")
            if self.source_run_id is None or self.as_of is None:
                raise ValueError("qualifying projection requires one verified run")

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "state": self.state.value,
            "reason_code": self.reason_code.value if self.reason_code is not None else None,
            "message": self.message,
            "source_run_id": self.source_run_id,
            "as_of": self.as_of.isoformat() if self.as_of is not None else None,
            "market_date": (
                self.as_of.astimezone(MARKET_TIMEZONE).date().isoformat()
                if self.as_of is not None
                else None
            ),
            "rows": [row.to_dict() for row in self.rows],
            "row_count": len(self.rows),
            "max_rows": MAX_OPPORTUNITY_ROWS,
            "research_only": self.research_only,
            "order_execution_enabled": self.order_execution_enabled,
        }

    def to_json(self) -> str:
        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )

    def content_hash(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def disabled_projection() -> OpportunityProjection:
    return OpportunityProjection(
        state=OpportunityProjectionState.DISABLED,
        reason_code=OpportunityProjectionReason.FEATURE_DISABLED,
        message="Opportunity projection disabled.",
        source_run_id=None,
        as_of=None,
        rows=(),
    )


def unavailable_projection(reason: OpportunityProjectionReason) -> OpportunityProjection:
    if reason not in _UNAVAILABLE_MESSAGES:
        raise ValueError("reason is not a data-unavailable reason")
    return OpportunityProjection(
        state=OpportunityProjectionState.DATA_UNAVAILABLE,
        reason_code=reason,
        message=_UNAVAILABLE_MESSAGES[reason],
        source_run_id=None,
        as_of=None,
        rows=(),
    )


def build_opportunity_projection(result: PipelineResult) -> OpportunityProjection:
    """Project one already verified pipeline result without recalculating decisions."""

    if not isinstance(result, PipelineResult):
        raise TypeError("result must be PipelineResult")
    if not result.research_only:
        raise ValueError("source result must remain research-only")
    qualifying = tuple(
        sorted(
            (
                decision
                for decision in result.decisions
                if decision.decision
                in {TradeDecisionValue.WATCH, TradeDecisionValue.TAKE}
            ),
            key=lambda decision: (
                decision.ranked.relative_rank if decision.ranked is not None else 10**9,
                decision.symbol,
                decision.strategy_id,
                decision.strategy_version,
                decision.decision_id,
            ),
        )
    )
    if not qualifying:
        return OpportunityProjection(
            state=OpportunityProjectionState.NO_QUALIFYING,
            reason_code=None,
            message=NO_QUALIFYING_MESSAGE,
            source_run_id=_safe_text(result.run_id),
            as_of=result.decision_at,
            rows=(),
        )

    candidate_by_symbol = {item.symbol: item for item in result.candidates}
    security_by_symbol = {item.symbol: item for item in result.security_regimes}
    definition_by_key = {
        (item.strategy_id, item.version): item
        for item in result.preparation.registry_definitions
    }
    risk_by_evaluation = {
        item.evaluation_id: item for item in result.risk_evidence
    }
    rich_by_symbol = {item.symbol: item for item in result.rich_snapshots}
    rows: list[OpportunityRowProjection] = []
    for decision in qualifying[:MAX_OPPORTUNITY_ROWS]:
        if decision.ranked is None:
            raise ValueError("qualifying persisted decision is missing its canonical rank")
        candidate = candidate_by_symbol.get(decision.symbol)
        security = security_by_symbol.get(decision.symbol)
        definition = definition_by_key.get(
            (decision.strategy_id, decision.strategy_version)
        )
        risk = risk_by_evaluation.get(decision.evaluation_id)
        snapshot = rich_by_symbol.get(decision.symbol)
        anomalies = tuple(
            OpportunityAnomalyProjection(
                name=_safe_text(item.anomaly_type.value),
                strength=item.strength,
                evidence_kind=_safe_text(item.evidence_kind.value),
            )
            for item in (candidate.anomalies if candidate is not None else ())
            if item.triggered
        )[:MAX_ANOMALIES]
        failed_gate_reasons = tuple(
            check.reason for check in decision.gate_checks if check.passed is not True
        )
        why = _bounded_unique(
            (
                *decision.rationale,
                *decision.evaluation.reasons,
                *(candidate.discovery_reasons if candidate else ()),
            )
        )
        risks = _bounded_unique(
            (
                *(definition.failure_modes if definition is not None else ()),
                *failed_gate_reasons,
                *(risk.limitations if risk is not None else ()),
            )
        )
        vetoes = _bounded_unique(
            (*decision.vetoes, *(risk.vetoes if risk is not None else ()))
        )
        limitations = _bounded_unique(
            (
                *result.limitations,
                *result.preparation.limitations,
                *decision.limitations,
                *decision.ranked.limitations,
                *(snapshot.limitations if snapshot is not None else ()),
                *(risk.limitations if risk is not None else ()),
            )
        )
        rows.append(
            OpportunityRowProjection(
                rank=decision.ranked.relative_rank,
                symbol=_safe_text(decision.symbol),
                strategy_id=_safe_text(decision.strategy_id),
                strategy_version=_safe_text(decision.strategy_version),
                direction=_safe_text(decision.direction.value),
                decision=_safe_text(decision.decision.value),
                lifecycle=_safe_text(decision.lifecycle.value),
                evidence_kind=_safe_text(
                    definition.evidence_kind.value if definition is not None else NOT_AVAILABLE
                ),
                validation_wording=_validation_wording(decision.lifecycle.value),
                market_regime=_safe_text(result.market_regime.state.value),
                market_regime_evidence_kind=_safe_text(
                    result.market_regime.evidence_kind.value
                ),
                security_regime=_safe_text(
                    security.state.value if security is not None else NOT_AVAILABLE
                ),
                security_regime_evidence_kind=_safe_text(
                    security.evidence_kind.value if security is not None else NOT_AVAILABLE
                ),
                triggered_anomalies=anomalies,
                liquidity_score=decision.evaluation.liquidity_score,
                liquidity_evidence_kind=(
                    "heuristic"
                    if decision.evaluation.liquidity_score is not None
                    else None
                ),
                why=why,
                risks=risks,
                vetoes=vetoes,
                entry_price=decision.evaluation.entry_price,
                invalidation_price=decision.evaluation.invalidation_price,
                target_price=decision.evaluation.target_price,
                limitations=limitations,
            )
        )
    return OpportunityProjection(
        state=OpportunityProjectionState.QUALIFYING,
        reason_code=None,
        message=(
            "Persisted research opportunities only. "
            "A displayed decision is not order authorization."
        ),
        source_run_id=_safe_text(result.run_id),
        as_of=result.decision_at,
        rows=tuple(rows),
    )


def _validation_wording(lifecycle: str) -> str:
    return {
        "experimental": "Experimental research; not validated.",
        "research_pass": "Research review passed; not validated for live use.",
        "validation_pass": "Validation-stage lifecycle; no live-use claim.",
        "oos_pass": "OOS-stage lifecycle; no live-use claim.",
        "paper_trading": "Paper-trading lifecycle; no live execution.",
        "production_eligible": "Production-eligible lifecycle; no order authorization.",
        "degraded": "Degraded lifecycle; evidence limitations apply.",
        "disabled": "Disabled lifecycle; not eligible for action.",
        "rejected": "Rejected lifecycle; not eligible for action.",
    }.get(lifecycle, "Validation status is not available.")


def _bounded_unique(values: Iterable[str]) -> tuple[str, ...]:
    return tuple(
        dict.fromkeys(_safe_text(value) for value in values)
    )[:MAX_TEXT_ITEMS]


def _safe_text(value: object) -> str:
    normalized = " ".join(str(value).split())
    if not normalized or _UNSAFE_TEXT.search(normalized):
        return NOT_AVAILABLE
    return normalized[:MAX_TEXT_LENGTH]


def _validate_text(value: str, label: str) -> None:
    if not value or len(value) > MAX_TEXT_LENGTH or _UNSAFE_TEXT.search(value):
        raise ValueError(f"projection {label} is not bounded public text")


def _validate_decimal(value: Decimal | None, label: str) -> None:
    if value is not None and not value.is_finite():
        raise ValueError(f"projection {label} must be finite when available")


def _decimal_json(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


__all__ = [
    "MAX_OPPORTUNITY_ROWS",
    "NO_QUALIFYING_MESSAGE",
    "NOT_AVAILABLE",
    "OpportunityAnomalyProjection",
    "OpportunityProjection",
    "OpportunityProjectionReason",
    "OpportunityProjectionState",
    "OpportunityRowProjection",
    "build_opportunity_projection",
    "disabled_projection",
    "unavailable_projection",
]
