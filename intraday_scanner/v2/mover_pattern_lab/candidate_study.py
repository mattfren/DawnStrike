"""All-candidate, point-in-time mover outcome study.

This module deliberately has no database, network, user-interface, notification,
or broker dependency.  It labels every supplied prospective snapshot under one
fixed paper-observation policy so rejected and skipped strategy candidates can
be studied alongside emitted signals without selection bias.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime, time, timedelta
from statistics import mean
from typing import Any

from intraday_scanner.market_calendar import market_session
from intraday_scanner.v2.data import MarketBar

from .contracts import (
    EVIDENCE_MODES,
    MARKET_TZ,
    UNIVERSE_SELECTION_METHODS,
    ProspectiveMoverSnapshot,
)

SCHEMA_VERSION = "v2.mover_candidate_study.v1"
FIXED_HORIZONS_MINUTES = (5, 15, 30, 60)
SPLIT_NAMES = frozenset({"discovery", "validation", "locked_test"})
DESCRIPTIVE_EOD_ROLE = "descriptive_eod_movers"
REALIZED_EOD_KIND = "realized_eod_gainers"
RTH_OPEN = time(9, 30)


@dataclass(frozen=True)
class CandidateStudyAssumptions:
    """Explicit fill and cost assumptions shared by every candidate."""

    bar_interval_minutes: int
    slippage_bps: float
    fee_bps: float

    def __post_init__(self) -> None:
        if self.bar_interval_minutes <= 0:
            raise ValueError("bar_interval_minutes must be positive")
        if any(
            horizon % self.bar_interval_minutes
            for horizon in FIXED_HORIZONS_MINUTES
        ):
            raise ValueError(
                "bar_interval_minutes must divide every fixed 5/15/30/60m horizon"
            )
        if not math.isfinite(self.slippage_bps) or self.slippage_bps < 0:
            raise ValueError("slippage_bps must be finite and non-negative")
        if not math.isfinite(self.fee_bps) or self.fee_bps < 0:
            raise ValueError("fee_bps must be finite and non-negative")

    def to_dict(self) -> dict[str, Any]:
        return {
            "bar_interval_minutes": self.bar_interval_minutes,
            "slippage_bps": self.slippage_bps,
            "fee_bps": self.fee_bps,
        }


@dataclass(frozen=True)
class CandidateUniverseDenominator:
    """Caller-attested complete candidate universe at one exact cutoff."""

    market_date: str
    feature_cutoff_at: datetime
    expected_symbols: tuple[str, ...]
    source_ref: str
    expected_symbols_complete: bool
    evidence_mode: str
    system_received_at: datetime | None
    universe_selection_method: str
    denominator_id: str

    def __post_init__(self) -> None:
        cutoff = _aware_datetime(self.feature_cutoff_at, "feature_cutoff_at")
        if cutoff.astimezone(MARKET_TZ).date().isoformat() != self.market_date:
            raise ValueError("denominator cutoff must match market_date")
        if (
            not self.expected_symbols
            or self.expected_symbols
            != tuple(sorted({_symbol(value) for value in self.expected_symbols}))
        ):
            raise ValueError("expected_symbols must be nonempty, unique, and sorted")
        if not self.source_ref.strip():
            raise ValueError("universe denominator source_ref is required")
        if not isinstance(self.expected_symbols_complete, bool):
            raise ValueError("expected_symbols_complete must be an explicit boolean")
        if self.evidence_mode not in EVIDENCE_MODES:
            raise ValueError("universe denominator evidence_mode is invalid")
        if self.universe_selection_method not in UNIVERSE_SELECTION_METHODS:
            raise ValueError("universe denominator selection method is invalid")
        received = (
            _aware_datetime(self.system_received_at, "system_received_at")
            if self.system_received_at is not None
            else None
        )
        if self.evidence_mode == "forward_observation":
            if received is None:
                raise ValueError(
                    "forward universe denominator requires system_received_at"
                )
            if received > cutoff:
                raise ValueError(
                    "forward universe denominator must be system-received by cutoff"
                )
            if received.astimezone(MARKET_TZ).date().isoformat() != self.market_date:
                raise ValueError(
                    "forward universe receipt must match market_date"
                )
        elif received is not None:
            raise ValueError(
                "historical denominator cannot claim a forward system receipt"
            )
        payload = {
            "schema_version": f"{SCHEMA_VERSION}.universe_denominator",
            "market_date": self.market_date,
            "feature_cutoff_at": cutoff.isoformat(),
            "expected_symbols": list(self.expected_symbols),
            "source_ref": self.source_ref,
            "expected_symbols_complete": self.expected_symbols_complete,
            "evidence_mode": self.evidence_mode,
            "system_received_at": _iso(received),
            "universe_selection_method": self.universe_selection_method,
        }
        if self.denominator_id != _fingerprint(payload):
            raise ValueError("denominator_id does not match immutable content")

    @classmethod
    def create(
        cls,
        *,
        market_date: str,
        feature_cutoff_at: datetime,
        expected_symbols: Iterable[str],
        source_ref: str,
        expected_symbols_complete: bool,
        evidence_mode: str = "historical_replay",
        system_received_at: datetime | None = None,
        universe_selection_method: str = "live_intraday_scan",
    ) -> CandidateUniverseDenominator:
        cutoff = _aware_datetime(feature_cutoff_at, "feature_cutoff_at")
        symbols = tuple(sorted({_symbol(value) for value in expected_symbols}))
        if not symbols:
            raise ValueError("expected_symbols cannot be empty")
        if cutoff.astimezone(MARKET_TZ).date().isoformat() != market_date:
            raise ValueError("denominator cutoff must match market_date")
        if not source_ref.strip():
            raise ValueError("universe denominator source_ref is required")
        payload = {
            "schema_version": f"{SCHEMA_VERSION}.universe_denominator",
            "market_date": market_date,
            "feature_cutoff_at": cutoff.isoformat(),
            "expected_symbols": list(symbols),
            "source_ref": source_ref,
            "expected_symbols_complete": expected_symbols_complete,
            "evidence_mode": evidence_mode,
            "system_received_at": _iso(system_received_at),
            "universe_selection_method": universe_selection_method,
        }
        return cls(
            market_date=market_date,
            feature_cutoff_at=cutoff,
            expected_symbols=symbols,
            source_ref=source_ref,
            expected_symbols_complete=expected_symbols_complete,
            evidence_mode=evidence_mode,
            system_received_at=system_received_at,
            universe_selection_method=universe_selection_method,
            denominator_id=_fingerprint(payload),
        )

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> CandidateUniverseDenominator:
        expected = row.get("expected_symbols")
        if not isinstance(expected, Sequence) or isinstance(expected, (str, bytes)):
            raise ValueError("expected_symbols must be a sequence")
        complete = row.get("expected_symbols_complete")
        if not isinstance(complete, bool):
            raise ValueError("expected_symbols_complete must be an explicit boolean")
        denominator = cls.create(
            market_date=str(row.get("market_date") or ""),
            feature_cutoff_at=_aware_datetime(
                row.get("feature_cutoff_at"), "feature_cutoff_at"
            ),
            expected_symbols=(str(value) for value in expected),
            source_ref=str(row.get("source_ref") or ""),
            expected_symbols_complete=complete,
            evidence_mode=str(
                row.get("evidence_mode") or "historical_replay"
            ),
            system_received_at=(
                _aware_datetime(
                    row.get("system_received_at"),
                    "system_received_at",
                )
                if row.get("system_received_at")
                else None
            ),
            universe_selection_method=str(
                row.get("universe_selection_method") or "live_intraday_scan"
            ),
        )
        supplied_id = str(row.get("denominator_id") or "").strip()
        if supplied_id and supplied_id != denominator.denominator_id:
            raise ValueError("supplied denominator_id does not match immutable content")
        return denominator

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": f"{SCHEMA_VERSION}.universe_denominator",
            "denominator_id": self.denominator_id,
            "market_date": self.market_date,
            "feature_cutoff_at": self.feature_cutoff_at.isoformat(),
            "expected_symbols": list(self.expected_symbols),
            "source_ref": self.source_ref,
            "expected_symbols_complete": self.expected_symbols_complete,
            "evidence_mode": self.evidence_mode,
            "system_received_at": _iso(self.system_received_at),
            "universe_selection_method": self.universe_selection_method,
        }


@dataclass(frozen=True)
class CandidateSplitAssignment:
    """Immutable, caller-supplied chronological split assignment."""

    assignments: tuple[tuple[str, str], ...]
    source_ref: str
    assignment_id: str

    def __post_init__(self) -> None:
        if not self.source_ref.strip():
            raise ValueError("split assignment source_ref is required")
        if (
            not self.assignments
            or self.assignments != tuple(sorted(self.assignments))
            or len({key for key, _ in self.assignments}) != len(self.assignments)
        ):
            raise ValueError("split assignments must be nonempty, unique, and sorted")
        if any(not key or split not in SPLIT_NAMES for key, split in self.assignments):
            raise ValueError("split assignments contain invalid IDs or split names")
        payload = {
            "schema_version": f"{SCHEMA_VERSION}.split_assignment",
            "assignments": [list(item) for item in self.assignments],
            "source_ref": self.source_ref,
        }
        if self.assignment_id != _fingerprint(payload):
            raise ValueError("assignment_id does not match immutable content")

    @classmethod
    def create(
        cls,
        assignments: Mapping[str, str],
        *,
        source_ref: str,
    ) -> CandidateSplitAssignment:
        if not source_ref.strip():
            raise ValueError("split assignment source_ref is required")
        frozen = tuple(
            sorted((str(snapshot_id), str(split)) for snapshot_id, split in assignments.items())
        )
        if not frozen or any(not snapshot_id for snapshot_id, _ in frozen):
            raise ValueError("split assignments require nonblank snapshot IDs")
        invalid = sorted({split for _, split in frozen} - SPLIT_NAMES)
        if invalid:
            raise ValueError("invalid split names: " + ", ".join(invalid))
        payload = {
            "schema_version": f"{SCHEMA_VERSION}.split_assignment",
            "assignments": [list(item) for item in frozen],
            "source_ref": source_ref,
        }
        return cls(
            assignments=frozen,
            source_ref=source_ref,
            assignment_id=_fingerprint(payload),
        )

    def as_mapping(self) -> dict[str, str]:
        return dict(self.assignments)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": f"{SCHEMA_VERSION}.split_assignment",
            "assignment_id": self.assignment_id,
            "source_ref": self.source_ref,
            "assignments": {key: value for key, value in self.assignments},
        }


@dataclass(frozen=True)
class CandidateOutcome:
    """Immutable strategy-independent outcome for one candidate snapshot."""

    outcome_id: str
    label_id: str
    snapshot_id: str
    market_date: str
    symbol: str
    evidence_mode: str
    feature_cutoff_at: datetime
    split: str
    denominator_id: str
    denominator_member: bool
    status: str
    pending_reason: str | None
    missing_expected_bar_at: datetime | None
    entry_at: datetime | None
    entry_bar_close_at: datetime | None
    entry_reference: float | None
    entry_fill: float | None
    gross_return_5m_pct: float | None
    gross_return_15m_pct: float | None
    gross_return_30m_pct: float | None
    gross_return_60m_pct: float | None
    after_cost_return_5m_pct: float | None
    after_cost_return_15m_pct: float | None
    after_cost_return_30m_pct: float | None
    after_cost_return_60m_pct: float | None
    official_close_at: datetime
    gross_close_return_pct: float | None
    after_cost_close_return_pct: float | None
    mfe_pct: float | None
    mae_pct: float | None
    candidate_return_rank: int | None
    candidate_return_population: int | None
    eod_mover_matched: bool | None
    eod_mover_rank: int | None
    eod_join_status: str
    bar_evidence_sha256: str
    bars_source_ref: str
    source_bar_count: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": f"{SCHEMA_VERSION}.candidate_outcome",
            "outcome_id": self.outcome_id,
            "label_id": self.label_id,
            "snapshot_id": self.snapshot_id,
            "market_date": self.market_date,
            "symbol": self.symbol,
            "evidence_mode": self.evidence_mode,
            "feature_cutoff_at": self.feature_cutoff_at.isoformat(),
            "split": self.split,
            "denominator_id": self.denominator_id,
            "denominator_member": self.denominator_member,
            "status": self.status,
            "pending_reason": self.pending_reason,
            "missing_expected_bar_at": _iso(self.missing_expected_bar_at),
            "entry_at": _iso(self.entry_at),
            "entry_bar_close_at": _iso(self.entry_bar_close_at),
            "entry_reference": self.entry_reference,
            "entry_fill": self.entry_fill,
            "gross_return_5m_pct": self.gross_return_5m_pct,
            "gross_return_15m_pct": self.gross_return_15m_pct,
            "gross_return_30m_pct": self.gross_return_30m_pct,
            "gross_return_60m_pct": self.gross_return_60m_pct,
            "after_cost_return_5m_pct": self.after_cost_return_5m_pct,
            "after_cost_return_15m_pct": self.after_cost_return_15m_pct,
            "after_cost_return_30m_pct": self.after_cost_return_30m_pct,
            "after_cost_return_60m_pct": self.after_cost_return_60m_pct,
            "official_close_at": self.official_close_at.isoformat(),
            "gross_close_return_pct": self.gross_close_return_pct,
            "after_cost_close_return_pct": self.after_cost_close_return_pct,
            "mfe_pct": self.mfe_pct,
            "mae_pct": self.mae_pct,
            "candidate_return_rank": self.candidate_return_rank,
            "candidate_return_population": self.candidate_return_population,
            "eod_mover_matched": self.eod_mover_matched,
            "eod_mover_rank": self.eod_mover_rank,
            "eod_join_status": self.eod_join_status,
            "bar_evidence_sha256": self.bar_evidence_sha256,
            "bars_source_ref": self.bars_source_ref,
            "source_bar_count": self.source_bar_count,
            "research_only": True,
            "broker_execution_enabled": False,
        }


@dataclass(frozen=True)
class CandidateCoverage:
    market_date: str
    feature_cutoff_at: datetime
    denominator_id: str
    expected_count: int
    observed_count: int
    complete_outcome_count: int
    missing_symbols: tuple[str, ...]
    unexpected_symbols: tuple[str, ...]
    snapshot_coverage_pct: float
    complete_outcome_coverage_pct: float
    expected_symbols_complete: bool
    snapshot_coverage_complete: bool
    outcome_coverage_complete: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_date": self.market_date,
            "feature_cutoff_at": self.feature_cutoff_at.isoformat(),
            "denominator_id": self.denominator_id,
            "expected_count": self.expected_count,
            "observed_count": self.observed_count,
            "complete_outcome_count": self.complete_outcome_count,
            "missing_symbols": list(self.missing_symbols),
            "unexpected_symbols": list(self.unexpected_symbols),
            "snapshot_coverage_pct": self.snapshot_coverage_pct,
            "complete_outcome_coverage_pct": self.complete_outcome_coverage_pct,
            "expected_symbols_complete": self.expected_symbols_complete,
            "snapshot_coverage_complete": self.snapshot_coverage_complete,
            "outcome_coverage_complete": self.outcome_coverage_complete,
        }


@dataclass(frozen=True)
class MoverControlComparison:
    market_date: str
    feature_cutoff_at: datetime
    applicable: bool
    reason: str
    eod_list_complete: bool
    candidate_universe_complete: bool
    matched_candidate_count: int
    nonmatched_control_count: int
    matched_complete_count: int
    nonmatched_complete_count: int
    matched_mean_after_cost_close_return_pct: float | None
    control_mean_after_cost_close_return_pct: float | None
    matched_minus_control_pct: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "market_date": self.market_date,
            "feature_cutoff_at": self.feature_cutoff_at.isoformat(),
            "applicable": self.applicable,
            "reason": self.reason,
            "eod_list_complete": self.eod_list_complete,
            "candidate_universe_complete": self.candidate_universe_complete,
            "matched_candidate_count": self.matched_candidate_count,
            "nonmatched_control_count": self.nonmatched_control_count,
            "matched_complete_count": self.matched_complete_count,
            "nonmatched_complete_count": self.nonmatched_complete_count,
            "matched_mean_after_cost_close_return_pct": (
                self.matched_mean_after_cost_close_return_pct
            ),
            "control_mean_after_cost_close_return_pct": (
                self.control_mean_after_cost_close_return_pct
            ),
            "matched_minus_control_pct": self.matched_minus_control_pct,
        }


@dataclass(frozen=True)
class DiscoveryCorrelation:
    feature: str
    sample_count: int
    spearman_rho: float | None
    status: str
    split: str
    assignment_id: str
    population: str = "all_candidate_snapshots"

    def to_dict(self) -> dict[str, Any]:
        return {
            "feature": self.feature,
            "sample_count": self.sample_count,
            "spearman_rho": self.spearman_rho,
            "status": self.status,
            "split": self.split,
            "assignment_id": self.assignment_id,
            "population": self.population,
        }


@dataclass(frozen=True)
class CandidateStudyResult:
    study_id: str
    evidence_mode: str
    assumptions: CandidateStudyAssumptions
    split_assignment_id: str
    outcomes: tuple[CandidateOutcome, ...]
    coverage: tuple[CandidateCoverage, ...]
    mover_control_comparisons: tuple[MoverControlComparison, ...]
    discovery_correlations: tuple[DiscoveryCorrelation, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION,
            "study_id": self.study_id,
            "evidence_mode": self.evidence_mode,
            "assumptions": self.assumptions.to_dict(),
            "split_assignment_id": self.split_assignment_id,
            "outcomes": [row.to_dict() for row in self.outcomes],
            "coverage": [row.to_dict() for row in self.coverage],
            "mover_control_comparisons": [
                row.to_dict() for row in self.mover_control_comparisons
            ],
            "discovery_correlations": [
                row.to_dict() for row in self.discovery_correlations
            ],
            "research_only": True,
            "broker_execution_enabled": False,
        }


def study_all_candidates(
    *,
    snapshots: Iterable[ProspectiveMoverSnapshot | Mapping[str, Any]],
    bars: Iterable[MarketBar] | Mapping[str, Iterable[MarketBar]],
    universe_denominators: Iterable[
        CandidateUniverseDenominator | Mapping[str, Any]
    ],
    split_assignment: CandidateSplitAssignment,
    assumptions: CandidateStudyAssumptions,
    bars_source_ref: str,
    descriptive_eod_movers: Iterable[Mapping[str, Any]] = (),
) -> CandidateStudyResult:
    """Label every supplied candidate and build unbiased discovery summaries."""

    if not bars_source_ref.strip():
        raise ValueError("bars_source_ref is required")
    parsed_snapshots = tuple(
        row
        if isinstance(row, ProspectiveMoverSnapshot)
        else ProspectiveMoverSnapshot.from_mapping(row)
        for row in snapshots
    )
    snapshot_ids = [row.snapshot_id for row in parsed_snapshots]
    if len(snapshot_ids) != len(set(snapshot_ids)):
        raise ValueError("snapshot IDs must be unique")
    evidence_modes = {row.evidence_mode for row in parsed_snapshots}
    if len(evidence_modes) != 1:
        raise ValueError(
            "candidate study runs must contain exactly one evidence_mode"
        )
    evidence_mode = next(iter(evidence_modes))
    assignment = split_assignment.as_mapping()
    if set(assignment) != set(snapshot_ids):
        missing = sorted(set(snapshot_ids) - set(assignment))
        extra = sorted(set(assignment) - set(snapshot_ids))
        raise ValueError(
            "split assignment must exactly cover supplied snapshots; "
            f"missing={missing}, extra={extra}"
        )
    splits_by_date: dict[str, set[str]] = defaultdict(set)
    for snapshot in parsed_snapshots:
        splits_by_date[snapshot.market_date].add(assignment[snapshot.snapshot_id])
    if any(len(values) != 1 for values in splits_by_date.values()):
        raise ValueError("same-day candidate cohorts must share one frozen split")
    split_order = {"discovery": 0, "validation": 1, "locked_test": 2}
    chronological_splits = [
        next(iter(splits_by_date[market_date]))
        for market_date in sorted(splits_by_date)
    ]
    if [split_order[value] for value in chronological_splits] != sorted(
        split_order[value] for value in chronological_splits
    ):
        raise ValueError("candidate splits must be chronological and cannot regress")

    denominators = tuple(_denominator(row) for row in universe_denominators)
    denominator_by_key: dict[tuple[str, datetime], CandidateUniverseDenominator] = {}
    for denominator in denominators:
        key = _group_key(denominator.market_date, denominator.feature_cutoff_at)
        if key in denominator_by_key:
            raise ValueError(f"duplicate universe denominator: {key}")
        denominator_by_key[key] = denominator
    if not denominator_by_key:
        raise ValueError("at least one universe denominator is required")
    snapshot_group_keys = {
        _group_key(snapshot.market_date, snapshot.feature_cutoff_at)
        for snapshot in parsed_snapshots
    }
    if set(denominator_by_key) != snapshot_group_keys:
        raise ValueError(
            "universe denominators must exactly match supplied candidate cohorts"
        )

    seen_candidates: set[tuple[str, datetime, str]] = set()
    for snapshot in parsed_snapshots:
        key = _group_key(snapshot.market_date, snapshot.feature_cutoff_at)
        if key not in denominator_by_key:
            raise ValueError(f"missing universe denominator for {key}")
        denominator = denominator_by_key[key]
        if denominator.evidence_mode != snapshot.evidence_mode:
            raise ValueError("universe denominator evidence_mode mismatch")
        if (
            denominator.universe_selection_method
            != snapshot.universe_selection_method
        ):
            raise ValueError("universe denominator selection method mismatch")
        identity = (key[0], key[1], snapshot.symbol)
        if identity in seen_candidates:
            raise ValueError(f"duplicate candidate snapshot for {identity}")
        seen_candidates.add(identity)

    bars_by_symbol = _normalize_bars(bars)
    eod = _prepare_eod_lists(descriptive_eod_movers)
    raw_outcomes: list[CandidateOutcome] = []
    snapshot_by_id = {row.snapshot_id: row for row in parsed_snapshots}
    for snapshot in sorted(
        parsed_snapshots,
        key=lambda row: (row.market_date, row.feature_cutoff_at, row.symbol),
    ):
        denominator = denominator_by_key[
            _group_key(snapshot.market_date, snapshot.feature_cutoff_at)
        ]
        outcome = _label_candidate(
            snapshot,
            bars_by_symbol.get(snapshot.symbol, ()),
            denominator=denominator,
            split=assignment[snapshot.snapshot_id],
            assumptions=assumptions,
            bars_source_ref=bars_source_ref,
        )
        raw_outcomes.append(_join_eod(outcome, eod.get(snapshot.market_date)))

    ranked = _rank_candidate_outcomes(raw_outcomes, denominator_by_key)
    coverage = _coverage(denominators, ranked)
    coverage_by_key = {
        _group_key(row.market_date, row.feature_cutoff_at): row for row in coverage
    }
    comparisons = _mover_control_comparisons(ranked, coverage_by_key, eod)
    correlations = _discovery_correlations(
        ranked,
        snapshot_by_id,
        split_assignment=split_assignment,
    )
    finalized = tuple(_with_content_label_id(row) for row in ranked)
    payload = {
        "schema_version": SCHEMA_VERSION,
        "evidence_mode": evidence_mode,
        "assumptions": assumptions.to_dict(),
        "split_assignment_id": split_assignment.assignment_id,
        "outcomes": [row.to_dict() for row in finalized],
        "coverage": [row.to_dict() for row in coverage],
        "mover_control_comparisons": [row.to_dict() for row in comparisons],
        "discovery_correlations": [row.to_dict() for row in correlations],
    }
    return CandidateStudyResult(
        study_id=_fingerprint(payload),
        evidence_mode=evidence_mode,
        assumptions=assumptions,
        split_assignment_id=split_assignment.assignment_id,
        outcomes=finalized,
        coverage=coverage,
        mover_control_comparisons=comparisons,
        discovery_correlations=correlations,
    )


def _label_candidate(
    snapshot: ProspectiveMoverSnapshot,
    bars: tuple[MarketBar, ...],
    *,
    denominator: CandidateUniverseDenominator,
    split: str,
    assumptions: CandidateStudyAssumptions,
    bars_source_ref: str,
) -> CandidateOutcome:
    session_day = date.fromisoformat(snapshot.market_date)
    session = market_session(session_day)
    if not session.is_trading_day or session.close_time_et is None:
        raise ValueError(f"snapshot is not on a published trading session: {session_day}")
    cutoff = snapshot.feature_cutoff_at.astimezone(MARKET_TZ)
    interval = timedelta(minutes=assumptions.bar_interval_minutes)
    _validate_cutoff_alignment(cutoff, assumptions.bar_interval_minutes)
    source_captured_at = (
        snapshot.source_captured_at.astimezone(MARKET_TZ)
        if snapshot.evidence_mode == "forward_observation"
        and snapshot.source_captured_at is not None
        else None
    )
    entry_open_at = _eligible_entry_open_at(
        cutoff=cutoff,
        source_captured_at=source_captured_at,
        interval=interval,
    )
    close_at = datetime.combine(
        session_day,
        time.fromisoformat(session.close_time_et),
        tzinfo=MARKET_TZ,
    )
    if (close_at - cutoff).total_seconds() % interval.total_seconds() != 0:
        raise ValueError("bar interval does not align cutoff to official close")

    relevant = tuple(
        bar
        for bar in bars
        if bar.symbol.upper() == snapshot.symbol
        and entry_open_at < bar.timestamp.astimezone(MARKET_TZ) <= close_at
        and bar.timestamp.astimezone(MARKET_TZ).date() == session_day
    )
    _validate_relevant_bars(relevant, snapshot.symbol, entry_open_at, interval)
    by_close = {bar.timestamp.astimezone(MARKET_TZ): bar for bar in relevant}
    evidence = [_bar_payload(bar) for bar in relevant]
    evidence_sha = _fingerprint(evidence)
    next_close = entry_open_at + interval
    next_bar = by_close.get(next_close)
    denominator_member = snapshot.symbol in denominator.expected_symbols
    base_identity = {
        "schema_version": f"{SCHEMA_VERSION}.candidate_observation_identity",
        "snapshot": snapshot.to_dict(),
        "assumptions": assumptions.to_dict(),
        "bars_source_ref": bars_source_ref,
        "bar_evidence_sha256": evidence_sha,
        "split": split,
        "denominator_id": denominator.denominator_id,
    }
    outcome_id = _fingerprint(base_identity)

    empty = _empty_outcome(
        outcome_id=outcome_id,
        snapshot=snapshot,
        split=split,
        denominator=denominator,
        denominator_member=denominator_member,
        close_at=close_at,
        evidence_sha=evidence_sha,
        bars_source_ref=bars_source_ref,
        source_bar_count=len(relevant),
    )
    if next_bar is None:
        return replace(
            empty,
            status="pending_missing_entry_bar",
            pending_reason="next_expected_bar_missing",
            missing_expected_bar_at=next_close,
        )

    entry_at = next_bar.timestamp.astimezone(MARKET_TZ) - interval
    if source_captured_at is not None and entry_at < source_captured_at:
        raise ValueError("forward candidate entry cannot predate source capture")
    entry_reference = next_bar.open
    slip = assumptions.slippage_bps / 10_000.0
    entry_fill = entry_reference * (1.0 + slip)
    horizon_values: dict[int, tuple[float | None, float | None]] = {}
    first_missing: datetime | None = None
    expected_at = next_close
    contiguous: list[MarketBar] = []
    while expected_at <= close_at:
        bar = by_close.get(expected_at)
        if bar is None:
            first_missing = expected_at
            break
        contiguous.append(bar)
        expected_at += interval
    contiguous_by_close = {
        bar.timestamp.astimezone(MARKET_TZ): bar for bar in contiguous
    }
    for horizon in FIXED_HORIZONS_MINUTES:
        endpoint_at = entry_at + timedelta(minutes=horizon)
        endpoint = contiguous_by_close.get(endpoint_at)
        horizon_values[horizon] = (
            _gross_return(entry_reference, endpoint.close) if endpoint else None,
            _after_cost_return(entry_fill, endpoint.close, assumptions)
            if endpoint
            else None,
        )

    if first_missing is not None:
        return replace(
            empty,
            status="pending_incomplete_session_grid",
            pending_reason="missing_expected_bar_before_official_close",
            missing_expected_bar_at=first_missing,
            entry_at=entry_at,
            entry_bar_close_at=next_close,
            entry_reference=_rounded(entry_reference),
            entry_fill=_rounded(entry_fill),
            gross_return_5m_pct=horizon_values[5][0],
            gross_return_15m_pct=horizon_values[15][0],
            gross_return_30m_pct=horizon_values[30][0],
            gross_return_60m_pct=horizon_values[60][0],
            after_cost_return_5m_pct=horizon_values[5][1],
            after_cost_return_15m_pct=horizon_values[15][1],
            after_cost_return_30m_pct=horizon_values[30][1],
            after_cost_return_60m_pct=horizon_values[60][1],
        )

    close_bar = contiguous_by_close[close_at]
    high_water = max(bar.high for bar in contiguous)
    low_water = min(bar.low for bar in contiguous)
    return replace(
        empty,
        status="complete",
        entry_at=entry_at,
        entry_bar_close_at=next_close,
        entry_reference=_rounded(entry_reference),
        entry_fill=_rounded(entry_fill),
        gross_return_5m_pct=horizon_values[5][0],
        gross_return_15m_pct=horizon_values[15][0],
        gross_return_30m_pct=horizon_values[30][0],
        gross_return_60m_pct=horizon_values[60][0],
        after_cost_return_5m_pct=horizon_values[5][1],
        after_cost_return_15m_pct=horizon_values[15][1],
        after_cost_return_30m_pct=horizon_values[30][1],
        after_cost_return_60m_pct=horizon_values[60][1],
        gross_close_return_pct=_gross_return(entry_reference, close_bar.close),
        after_cost_close_return_pct=_after_cost_return(
            entry_fill, close_bar.close, assumptions
        ),
        mfe_pct=_rounded(max(0.0, (high_water / entry_fill - 1.0) * 100.0)),
        mae_pct=_rounded(min(0.0, (low_water / entry_fill - 1.0) * 100.0)),
    )


def _empty_outcome(
    *,
    outcome_id: str,
    snapshot: ProspectiveMoverSnapshot,
    split: str,
    denominator: CandidateUniverseDenominator,
    denominator_member: bool,
    close_at: datetime,
    evidence_sha: str,
    bars_source_ref: str,
    source_bar_count: int,
) -> CandidateOutcome:
    return CandidateOutcome(
        outcome_id=outcome_id,
        label_id="",
        snapshot_id=snapshot.snapshot_id,
        market_date=snapshot.market_date,
        symbol=snapshot.symbol,
        evidence_mode=snapshot.evidence_mode,
        feature_cutoff_at=snapshot.feature_cutoff_at,
        split=split,
        denominator_id=denominator.denominator_id,
        denominator_member=denominator_member,
        status="pending",
        pending_reason=None,
        missing_expected_bar_at=None,
        entry_at=None,
        entry_bar_close_at=None,
        entry_reference=None,
        entry_fill=None,
        gross_return_5m_pct=None,
        gross_return_15m_pct=None,
        gross_return_30m_pct=None,
        gross_return_60m_pct=None,
        after_cost_return_5m_pct=None,
        after_cost_return_15m_pct=None,
        after_cost_return_30m_pct=None,
        after_cost_return_60m_pct=None,
        official_close_at=close_at,
        gross_close_return_pct=None,
        after_cost_close_return_pct=None,
        mfe_pct=None,
        mae_pct=None,
        candidate_return_rank=None,
        candidate_return_population=None,
        eod_mover_matched=None,
        eod_mover_rank=None,
        eod_join_status="eod_list_unavailable_or_unproven",
        bar_evidence_sha256=evidence_sha,
        bars_source_ref=bars_source_ref,
        source_bar_count=source_bar_count,
    )


@dataclass(frozen=True)
class _PreparedEodList:
    ranks: tuple[tuple[str, int], ...]
    list_complete: bool
    status: str

    def rank_mapping(self) -> dict[str, int]:
        return dict(self.ranks)


def _prepare_eod_lists(
    rows: Iterable[Mapping[str, Any]],
) -> dict[str, _PreparedEodList]:
    grouped: dict[str, list[Mapping[str, Any]]] = defaultdict(list)
    for row in rows:
        market_date = str(row.get("market_date") or row.get("date") or "")[:10]
        if market_date:
            grouped[market_date].append(row)
    output: dict[str, _PreparedEodList] = {}
    for market_date, day_rows in grouped.items():
        valid: list[tuple[str, int, int, bool]] = []
        all_rows_valid = True
        for row in day_rows:
            source_complete = row.get("source_complete")
            role_valid = row.get("dataset_role") == DESCRIPTIVE_EOD_ROLE
            kind_valid = row.get("source_snapshot_kind") == REALIZED_EOD_KIND
            source_ref = str(row.get("source_ref") or "").strip()
            symbol_raw = row.get("symbol") or row.get("ticker")
            rank_raw = row.get("mover_rank") or row.get("rank")
            expected_raw = row.get("expected_row_count")
            row_valid = (
                source_complete is True
                and row.get("source_coverage_complete") is True
                and row.get("eod_label_eligible") is True
                and row.get("prospective_signal_eligible") is False
                and role_valid
                and kind_valid
                and row.get("ingestion_channel") == "local_operator_csv"
                and str(row.get("source_artifact_ref") or "").strip()
                == source_ref
                and bool(source_ref)
                and str(row.get("corporate_action_status") or "")
                in {"verified_clear", "verified_adjusted", "adjusted"}
                and bool(str(row.get("corporate_action_source_ref") or "").strip())
                and _after_close_capture_valid(row, market_date)
                and _eod_system_receipt_valid(row)
                and symbol_raw is not None
                and _positive_int(rank_raw) is not None
                and _positive_int(expected_raw) is not None
                and isinstance(row.get("list_coverage_complete"), bool)
            )
            all_rows_valid = all_rows_valid and row_valid
            if row_valid:
                valid.append(
                    (
                        _symbol(str(symbol_raw)),
                        _positive_int(rank_raw) or 0,
                        _positive_int(expected_raw) or 0,
                        row.get("list_coverage_complete") is True,
                    )
                )
        ranks = [(symbol, rank) for symbol, rank, _, _ in valid]
        expected_counts = {expected for _, _, expected, _ in valid}
        source_refs = {str(row.get("source_ref") or "") for row in day_rows}
        expected_count = next(iter(expected_counts)) if len(expected_counts) == 1 else 0
        rank_values = {rank for _, rank in ranks}
        symbols = {symbol for symbol, _ in ranks}
        complete = bool(
            all_rows_valid
            and valid
            and all(row_complete for _, _, _, row_complete in valid)
            and len(expected_counts) == 1
            and len(source_refs) == 1
            and len(valid) == expected_count
            and len(symbols) == expected_count
            and rank_values == set(range(1, expected_count + 1))
        )
        status = (
            "complete_verified_descriptive_eod_list"
            if complete
            else "incomplete_or_invalid_descriptive_eod_list"
        )
        output[market_date] = _PreparedEodList(
            ranks=tuple(sorted(set(ranks))),
            list_complete=complete,
            status=status,
        )
    return output


def _after_close_capture_valid(
    row: Mapping[str, Any],
    market_date: str,
) -> bool:
    try:
        market_day = date.fromisoformat(market_date)
        session = market_session(market_day)
        if not session.is_trading_day or session.close_time_et is None:
            return False
        extracted_at = _aware_datetime(row.get("extracted_at"), "extracted_at")
        local = extracted_at.astimezone(MARKET_TZ)
        return (
            local.date() == market_day
            and local.time() >= time.fromisoformat(session.close_time_et)
        )
    except (TypeError, ValueError):
        return False


def _eod_system_receipt_valid(row: Mapping[str, Any]) -> bool:
    try:
        extracted_at = _aware_datetime(row.get("extracted_at"), "extracted_at")
        system_received_at = _aware_datetime(
            row.get("system_received_at"),
            "system_received_at",
        )
    except (TypeError, ValueError):
        return False
    return extracted_at <= system_received_at


def _join_eod(
    outcome: CandidateOutcome,
    eod: _PreparedEodList | None,
) -> CandidateOutcome:
    if eod is None:
        return outcome
    rank = eod.rank_mapping().get(outcome.symbol)
    if rank is not None:
        return replace(
            outcome,
            eod_mover_matched=True,
            eod_mover_rank=rank,
            eod_join_status=(
                "matched_verified_complete_eod_list"
                if eod.list_complete
                else "matched_verified_row_in_incomplete_eod_list"
            ),
        )
    if eod.list_complete:
        return replace(
            outcome,
            eod_mover_matched=False,
            eod_join_status="nonmatch_known_from_complete_eod_list",
        )
    return replace(outcome, eod_join_status=eod.status)


def _rank_candidate_outcomes(
    outcomes: list[CandidateOutcome],
    denominators: Mapping[tuple[str, datetime], CandidateUniverseDenominator],
) -> tuple[CandidateOutcome, ...]:
    grouped: dict[tuple[str, datetime], list[CandidateOutcome]] = defaultdict(list)
    for row in outcomes:
        grouped[_group_key(row.market_date, row.feature_cutoff_at)].append(row)
    ranked: list[CandidateOutcome] = []
    for key, rows in grouped.items():
        denominator = denominators[key]
        expected = set(denominator.expected_symbols)
        population_truth_complete = bool(
            denominator.expected_symbols_complete
            and {row.symbol for row in rows} == expected
            and len(rows) == len(expected)
            and all(
                row.status == "complete"
                and row.after_cost_close_return_pct is not None
                for row in rows
            )
        )
        complete = (
            sorted(
                rows,
                key=lambda row: (
                    -float(row.after_cost_close_return_pct or 0.0),
                    row.symbol,
                    row.outcome_id,
                ),
            )
            if population_truth_complete
            else []
        )
        rank_by_id = {row.outcome_id: index for index, row in enumerate(complete, 1)}
        population = len(complete)
        for row in rows:
            rank = rank_by_id.get(row.outcome_id)
            ranked.append(
                replace(
                    row,
                    candidate_return_rank=rank,
                    candidate_return_population=population if rank is not None else None,
                )
            )
    return tuple(
        sorted(
            ranked,
            key=lambda row: (row.market_date, row.feature_cutoff_at, row.symbol),
        )
    )


def _coverage(
    denominators: tuple[CandidateUniverseDenominator, ...],
    outcomes: tuple[CandidateOutcome, ...],
) -> tuple[CandidateCoverage, ...]:
    grouped: dict[tuple[str, datetime], list[CandidateOutcome]] = defaultdict(list)
    for row in outcomes:
        grouped[_group_key(row.market_date, row.feature_cutoff_at)].append(row)
    output: list[CandidateCoverage] = []
    for denominator in sorted(
        denominators, key=lambda row: (row.market_date, row.feature_cutoff_at)
    ):
        rows = grouped.get(
            _group_key(denominator.market_date, denominator.feature_cutoff_at), []
        )
        expected = set(denominator.expected_symbols)
        observed = {row.symbol for row in rows}
        complete = {
            row.symbol
            for row in rows
            if row.status == "complete" and row.symbol in expected
        }
        missing = tuple(sorted(expected - observed))
        unexpected = tuple(sorted(observed - expected))
        expected_count = len(expected)
        observed_expected_count = len(observed & expected)
        snapshot_complete = bool(
            denominator.expected_symbols_complete and not missing and not unexpected
        )
        outcome_complete = bool(snapshot_complete and complete == expected)
        output.append(
            CandidateCoverage(
                market_date=denominator.market_date,
                feature_cutoff_at=denominator.feature_cutoff_at,
                denominator_id=denominator.denominator_id,
                expected_count=expected_count,
                observed_count=len(observed),
                complete_outcome_count=len(complete),
                missing_symbols=missing,
                unexpected_symbols=unexpected,
                snapshot_coverage_pct=_rounded(
                    observed_expected_count / expected_count * 100.0
                ),
                complete_outcome_coverage_pct=_rounded(
                    len(complete) / expected_count * 100.0
                ),
                expected_symbols_complete=denominator.expected_symbols_complete,
                snapshot_coverage_complete=snapshot_complete,
                outcome_coverage_complete=outcome_complete,
            )
        )
    return tuple(output)


def _mover_control_comparisons(
    outcomes: tuple[CandidateOutcome, ...],
    coverage: Mapping[tuple[str, datetime], CandidateCoverage],
    eod: Mapping[str, _PreparedEodList],
) -> tuple[MoverControlComparison, ...]:
    grouped: dict[tuple[str, datetime], list[CandidateOutcome]] = defaultdict(list)
    for row in outcomes:
        grouped[_group_key(row.market_date, row.feature_cutoff_at)].append(row)
    output: list[MoverControlComparison] = []
    for key, rows in sorted(grouped.items()):
        day_list = eod.get(key[0])
        eod_complete = bool(day_list and day_list.list_complete)
        universe_complete = coverage[key].outcome_coverage_complete
        matched = [row for row in rows if row.eod_mover_matched is True]
        controls = [row for row in rows if row.eod_mover_matched is False]
        matched_complete = [
            row
            for row in matched
            if row.status == "complete" and row.after_cost_close_return_pct is not None
        ]
        controls_complete = [
            row
            for row in controls
            if row.status == "complete" and row.after_cost_close_return_pct is not None
        ]
        applicable = bool(
            eod_complete
            and universe_complete
            and matched_complete
            and controls_complete
        )
        if not eod_complete:
            reason = "eod_list_coverage_not_proven_complete"
        elif not universe_complete:
            reason = "candidate_universe_outcome_coverage_not_proven_complete"
        elif not matched_complete or not controls_complete:
            reason = "matched_and_control_complete_outcomes_required"
        else:
            reason = "complete_verified_eod_labels_and_candidate_universe"
        matched_mean = (
            mean(_present_float(row.after_cost_close_return_pct) for row in matched_complete)
            if matched_complete
            else None
        )
        control_mean = (
            mean(_present_float(row.after_cost_close_return_pct) for row in controls_complete)
            if controls_complete
            else None
        )
        output.append(
            MoverControlComparison(
                market_date=key[0],
                feature_cutoff_at=key[1],
                applicable=applicable,
                reason=reason,
                eod_list_complete=eod_complete,
                candidate_universe_complete=universe_complete,
                matched_candidate_count=len(matched),
                nonmatched_control_count=len(controls),
                matched_complete_count=len(matched_complete),
                nonmatched_complete_count=len(controls_complete),
                matched_mean_after_cost_close_return_pct=(
                    _rounded(matched_mean) if applicable and matched_mean is not None else None
                ),
                control_mean_after_cost_close_return_pct=(
                    _rounded(control_mean) if applicable and control_mean is not None else None
                ),
                matched_minus_control_pct=(
                    _rounded(matched_mean - control_mean)
                    if applicable and matched_mean is not None and control_mean is not None
                    else None
                ),
            )
        )
    return tuple(output)


CORRELATION_FEATURES = (
    "gap_pct",
    "same_clock_rvol",
    "cumulative_volume",
    "cumulative_dollar_volume",
    "spread_pct",
    "price_vs_vwap_pct",
    "distance_from_opening_high_pct",
)


def _discovery_correlations(
    outcomes: tuple[CandidateOutcome, ...],
    snapshots: Mapping[str, ProspectiveMoverSnapshot],
    *,
    split_assignment: CandidateSplitAssignment,
) -> tuple[DiscoveryCorrelation, ...]:
    discovery = [
        row
        for row in outcomes
        if row.split == "discovery"
        and row.status == "complete"
        and row.after_cost_close_return_pct is not None
    ]
    output: list[DiscoveryCorrelation] = []
    for feature in CORRELATION_FEATURES:
        pairs: list[tuple[float, float]] = []
        for outcome in discovery:
            value = _snapshot_feature(snapshots[outcome.snapshot_id], feature)
            if value is not None and math.isfinite(value):
                pairs.append(
                    (value, _present_float(outcome.after_cost_close_return_pct))
                )
        rho = _spearman(pairs) if len(pairs) >= 3 else None
        status = (
            "calculated"
            if rho is not None
            else "insufficient_complete_discovery_candidates"
        )
        output.append(
            DiscoveryCorrelation(
                feature=feature,
                sample_count=len(pairs),
                spearman_rho=_rounded(rho) if rho is not None else None,
                status=status,
                split="discovery",
                assignment_id=split_assignment.assignment_id,
            )
        )
    return tuple(output)


def _snapshot_feature(
    snapshot: ProspectiveMoverSnapshot,
    feature: str,
) -> float | None:
    if feature == "gap_pct":
        return snapshot.gap_pct
    if feature == "price_vs_vwap_pct":
        running_vwap = snapshot.running_vwap
        if running_vwap in {None, 0}:
            return None
        return (snapshot.price / _present_float(running_vwap) - 1.0) * 100.0
    if feature == "distance_from_opening_high_pct":
        opening_high = snapshot.opening_range_high
        if opening_high in {None, 0}:
            return None
        return (
            snapshot.price / _present_float(opening_high) - 1.0
        ) * 100.0
    value = getattr(snapshot, feature, None)
    return float(value) if isinstance(value, (int, float)) else None


def _spearman(pairs: list[tuple[float, float]]) -> float | None:
    x = [pair[0] for pair in pairs]
    y = [pair[1] for pair in pairs]
    rx = _ranks(x)
    ry = _ranks(y)
    avg_x = mean(rx)
    avg_y = mean(ry)
    numerator = sum((a - avg_x) * (b - avg_y) for a, b in zip(rx, ry, strict=True))
    denominator = math.sqrt(
        sum((a - avg_x) ** 2 for a in rx)
        * sum((b - avg_y) ** 2 for b in ry)
    )
    return numerator / denominator if denominator else None


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=values.__getitem__)
    ranks = [0.0] * len(values)
    index = 0
    while index < len(order):
        end = index + 1
        while end < len(order) and values[order[end]] == values[order[index]]:
            end += 1
        rank = (index + end - 1) / 2.0 + 1.0
        for position in order[index:end]:
            ranks[position] = rank
        index = end
    return ranks


def _normalize_bars(
    bars: Iterable[MarketBar] | Mapping[str, Iterable[MarketBar]],
) -> dict[str, tuple[MarketBar, ...]]:
    flat: list[MarketBar] = []
    if isinstance(bars, Mapping):
        for values in bars.values():
            flat.extend(values)
    else:
        flat.extend(bars)
    grouped: dict[str, list[MarketBar]] = defaultdict(list)
    seen: set[tuple[str, datetime]] = set()
    for bar in flat:
        if not isinstance(bar, MarketBar):
            raise TypeError("bars must contain MarketBar objects")
        _aware_datetime(bar.timestamp, "bar timestamp")
        symbol = _symbol(bar.symbol)
        identity = (symbol, bar.timestamp)
        if identity in seen:
            raise ValueError(f"duplicate bar timestamp: {symbol} {bar.timestamp.isoformat()}")
        seen.add(identity)
        if min(bar.open, bar.high, bar.low, bar.close) <= 0:
            raise ValueError(f"{symbol}: OHLC values must be positive")
        if bar.high < max(bar.open, bar.close, bar.low):
            raise ValueError(f"{symbol}: inconsistent high")
        if bar.low > min(bar.open, bar.close, bar.high):
            raise ValueError(f"{symbol}: inconsistent low")
        if bar.volume <= 0:
            raise ValueError(f"{symbol}: volume must be positive for an executable bar")
        grouped[symbol].append(bar)
    return {
        symbol: tuple(sorted(values, key=lambda row: row.timestamp))
        for symbol, values in grouped.items()
    }


def _validate_relevant_bars(
    bars: tuple[MarketBar, ...],
    symbol: str,
    cutoff: datetime,
    interval: timedelta,
) -> None:
    step_seconds = interval.total_seconds()
    for bar in bars:
        local = bar.timestamp.astimezone(MARKET_TZ)
        elapsed = (local - cutoff).total_seconds()
        if elapsed <= 0 or elapsed % step_seconds != 0:
            raise ValueError(
                f"{symbol}: off-grid bar-close timestamp {local.isoformat()}"
            )


def _eligible_entry_open_at(
    *,
    cutoff: datetime,
    source_captured_at: datetime | None,
    interval: timedelta,
) -> datetime:
    if source_captured_at is None or source_captured_at <= cutoff:
        return cutoff
    steps = math.ceil(
        (source_captured_at - cutoff).total_seconds()
        / interval.total_seconds()
    )
    return cutoff + steps * interval


def _validate_cutoff_alignment(cutoff: datetime, interval_minutes: int) -> None:
    session_open = datetime.combine(cutoff.date(), RTH_OPEN, tzinfo=MARKET_TZ)
    elapsed = (cutoff - session_open).total_seconds()
    if elapsed < 0 or elapsed % (interval_minutes * 60) != 0:
        raise ValueError("feature cutoff is not aligned to the declared bar interval")


def _bar_payload(bar: MarketBar) -> dict[str, Any]:
    return {
        "symbol": bar.symbol.upper(),
        "timestamp": bar.timestamp.isoformat(),
        "open": bar.open,
        "high": bar.high,
        "low": bar.low,
        "close": bar.close,
        "volume": bar.volume,
    }


def _gross_return(entry_reference: float, exit_reference: float) -> float:
    return _rounded((exit_reference / entry_reference - 1.0) * 100.0)


def _after_cost_return(
    entry_fill: float,
    exit_reference: float,
    assumptions: CandidateStudyAssumptions,
) -> float:
    slippage = assumptions.slippage_bps / 10_000.0
    fee_rate = assumptions.fee_bps / 10_000.0
    exit_fill = exit_reference * (1.0 - slippage)
    fees = entry_fill * fee_rate + exit_fill * fee_rate
    return _rounded((exit_fill - entry_fill - fees) / entry_fill * 100.0)


def _with_content_label_id(row: CandidateOutcome) -> CandidateOutcome:
    payload = row.to_dict()
    payload.pop("label_id", None)
    return replace(row, label_id=_fingerprint(payload))


def _denominator(
    row: CandidateUniverseDenominator | Mapping[str, Any],
) -> CandidateUniverseDenominator:
    return (
        row
        if isinstance(row, CandidateUniverseDenominator)
        else CandidateUniverseDenominator.from_mapping(row)
    )


def _group_key(market_date: str, cutoff: datetime) -> tuple[str, datetime]:
    return market_date, cutoff.astimezone(MARKET_TZ)


def _aware_datetime(value: Any, field: str) -> datetime:
    if isinstance(value, datetime):
        parsed = value
    else:
        parsed = datetime.fromisoformat(str(value or "").replace("Z", "+00:00"))
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed


def _symbol(value: str) -> str:
    symbol = str(value).strip().upper()
    if not symbol:
        raise ValueError("symbol cannot be blank")
    return symbol


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 and str(number) == str(value).strip() else None


def _fingerprint(payload: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _rounded(value: float) -> float:
    return round(float(value), 8)


def _present_float(value: float | None) -> float:
    if value is None:
        raise ValueError("required numeric value is missing")
    return float(value)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


__all__ = [
    "CandidateCoverage",
    "CandidateOutcome",
    "CandidateSplitAssignment",
    "CandidateStudyAssumptions",
    "CandidateStudyResult",
    "CandidateUniverseDenominator",
    "DESCRIPTIVE_EOD_ROLE",
    "DiscoveryCorrelation",
    "FIXED_HORIZONS_MINUTES",
    "MoverControlComparison",
    "REALIZED_EOD_KIND",
    "study_all_candidates",
]
