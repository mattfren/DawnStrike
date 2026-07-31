"""Scan result contracts for Dawnstrike v2."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.contracts.common import StrategyId, StrategyVersion, Symbol
from intraday_scanner.v2.contracts.serialization import ContractMixin


class SignalDirection(str, Enum):
    LONG = "long"
    SHORT = "short"
    FLAT = "flat"
    WATCH = "watch"


class SignalStatus(str, Enum):
    CANDIDATE = "candidate"
    WATCHLIST = "watchlist"
    BLOCKED = "blocked"
    NO_CLEAN_EDGE = "no_clean_edge"
    OUTCOME_NEEDED = "outcome_needed"
    CLOSED = "closed"


@dataclass(frozen=True)
class ScoreComponent(ContractMixin):
    name: str
    value: Decimal
    max_value: Decimal | None = None
    weight: Decimal | None = None
    explanation: str | None = None
    schema_version: str = "v2.score_component.v1"


@dataclass(frozen=True)
class SetupScore(ContractMixin):
    total: Decimal
    grade: str
    components: tuple[ScoreComponent, ...]
    schema_version: str = "v2.setup_score.v1"


@dataclass(frozen=True)
class SignalEvidence(ContractMixin):
    evidence_id: str
    label: str
    summary: str
    source_refs: tuple[str, ...] = ()
    confidence: Decimal | None = None
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.signal_evidence.v1"


@dataclass(frozen=True)
class ScanCandidate(ContractMixin):
    candidate_id: str
    symbol: Symbol
    direction: SignalDirection
    status: SignalStatus
    strategy_id: StrategyId
    strategy_version: StrategyVersion
    setup_score: SetupScore
    evidence: tuple[SignalEvidence, ...]
    generated_at: datetime
    rank: int | None = None
    entry_trigger: Decimal | None = None
    invalidation_level: Decimal | None = None
    target_price: Decimal | None = None
    risk_flags: tuple[str, ...] = ()
    schema_version: str = "v2.scan_candidate.v1"


@dataclass(frozen=True)
class ScanResult(ContractMixin):
    run_id: str
    created_at: datetime
    strategy_id: StrategyId
    strategy_version: StrategyVersion
    data_snapshot_id: str
    candidates: tuple[ScanCandidate, ...]
    blocked_candidates: tuple[ScanCandidate, ...] = ()
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.scan_result.v1"
