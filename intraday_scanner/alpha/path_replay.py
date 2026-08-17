"""Pure, deterministic canonical path truth for one decision and evidence set."""

from __future__ import annotations

import hashlib
import hmac
import json
import math
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from enum import Enum
from typing import Any

PATH_REPLAY_SCHEMA_VERSION = "dawnstrike.path_truth.v2"
PATH_REPLAY_POLICY_VERSION = "canonical-ordered-path-v2"
ELIGIBILITY_POLICY_VERSION = "dawnstrike.alphaops-v6-eligibility.v2"
REPLAY_BINDING_SCHEMA_VERSION = "dawnstrike.path_replay_binding.v1"
FUTURE_EVIDENCE_RECEIPT_SCHEMA_VERSION = (
    "dawnstrike.future_evidence_receipt.v1"
)
FUTURE_EVIDENCE_RECEIPT_ID_PREFIX = "future-evidence-v1-"
ENTRY_RECEIPT_SCHEMA_VERSION = "dawnstrike.path_entry_receipt.v1"
ENTRY_RECEIPT_ID_PREFIX = "path-entry-v1-"
ENTRY_MODE_ALREADY_ENTERED = "ALREADY_ENTERED_AT_DECISION"
DEFAULT_BAR_INTERVAL = timedelta(minutes=1)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_FUTURE_EVIDENCE_BODY_KEYS = frozenset(
    {
        "schema_version",
        "subject",
        "raw_artifact_identity",
        "raw_bar_hash_sha256",
        "bar_count",
        "first_bar_at",
        "last_bar_at",
        "coverage_start",
        "coverage_end",
        "coverage_complete",
    }
)
_FUTURE_EVIDENCE_RECEIPT_KEYS = frozenset(
    {*_FUTURE_EVIDENCE_BODY_KEYS, "receipt_id", "receipt_hash_sha256"}
)
_ENTRY_RECEIPT_BODY_KEYS = frozenset(
    {
        "schema_version",
        "entry_mode",
        "raw_entry_price",
        "effective_at",
        "source_observation_id",
        "source_bar_hash_sha256",
        "source_observed_at",
        "source_bar_completed_at",
        "replay_origin",
    }
)
_ENTRY_RECEIPT_KEYS = frozenset(
    {*_ENTRY_RECEIPT_BODY_KEYS, "receipt_id", "receipt_hash_sha256"}
)
PATH_REPLAY_ENVELOPE_KEYS = frozenset(
    {
        "cohort",
        "selection_id",
        "signal_id",
        "market_date",
        "artifact_identity",
        "artifact_hash_sha256",
        "retrospective_research_eligible",
        "prospective_promotion_eligible",
        "created_at",
    }
)
PATH_REPLAY_INPUT_MANIFEST_KEYS = frozenset(
    {
        "path_replay_schema_version",
        "path_replay_policy_version",
        "path_replay_policy_hash_sha256",
        "eligibility_policy_version",
        "input_contract_markers",
        "input_contract_violations",
        "decision_at",
        "session_close",
        "trigger",
        "target",
        "stop",
        "bar_interval_seconds",
        "bars",
        "halt_intervals",
        "ordered_feed_identity",
        "ordered_feed_hash_sha256",
        "ordered_feed_complete",
        "ordered_coverage_start",
        "ordered_coverage_end",
        "ordered_events",
        "source_artifact_identity",
        "source_artifact_hash_sha256",
        "source_coverage_complete",
        "source_conflict",
        "corporate_action_unresolved",
        "replay_binding",
        "future_evidence_receipt",
        "entry_mode",
        "entry_receipt",
    }
)
_INPUT_SENTINEL_SCHEMA = "dawnstrike.path_input_violation.v1"
_INPUT_SENTINEL_FALLBACKS: dict[str, object] = {
    "decision_at": None,
    "session_close": None,
    "trigger": None,
    "target": None,
    "stop": None,
    "bar_interval_seconds": DEFAULT_BAR_INTERVAL.total_seconds(),
    "bars": [],
    "halt_intervals": [],
    "ordered_feed_complete": False,
    "source_artifact_identity": None,
    "source_coverage_complete": False,
    "source_conflict": False,
    "corporate_action_unresolved": False,
    "replay_binding": None,
    "future_evidence_receipt": None,
    "entry_mode": None,
    "entry_receipt": None,
}


class PathEvent(str, Enum):
    """The sole public vocabulary for a competing path event or censor."""

    TARGET = "TARGET"
    STOP = "STOP"
    TIMEOUT = "TIMEOUT"
    HALT = "HALT"
    LIQUIDITY_FAILURE = "LIQUIDITY_FAILURE"
    ENTRY_INTERVAL_CENSORED = "ENTRY_INTERVAL_CENSORED"
    SAME_INTERVAL_CENSORED = "SAME_INTERVAL_CENSORED"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    CORPORATE_ACTION_UNRESOLVED = "CORPORATE_ACTION_UNRESOLVED"


class PathTruthStatus(str, Enum):
    """Detailed canonical statuses plus distinct persisted legacy values."""

    RESOLVED_TARGET_FIRST = "RESOLVED_TARGET_FIRST"
    RESOLVED_STOP_FIRST = "RESOLVED_STOP_FIRST"
    RIGHT_CENSORED_SESSION_CLOSE = "RIGHT_CENSORED_SESSION_CLOSE"
    ENTRY_INTERVAL_CENSORED = "ENTRY_INTERVAL_CENSORED"
    TARGET_STOP_INTERVAL_CENSORED = "TARGET_STOP_INTERVAL_CENSORED"
    MISSING_INTERVAL_CENSORED = "MISSING_INTERVAL_CENSORED"
    HALT_CENSORED = "HALT_CENSORED"
    NOT_TRIGGERED = "NOT_TRIGGERED"
    MISSING_DECISION_TIME = "MISSING_DECISION_TIME"
    MISSING_LEVELS = "MISSING_LEVELS"
    MISSING_BARS = "MISSING_BARS"
    CORPORATE_ACTION_UNRESOLVED = "CORPORATE_ACTION_UNRESOLVED"
    SOURCE_CONFLICT = "SOURCE_CONFLICT"
    DATA_INELIGIBLE = "DATA_INELIGIBLE"

    # These v1 strings remain audit-visible and must never alias a v2 status.
    SAME_MINUTE_AMBIGUOUS = "SAME_MINUTE_AMBIGUOUS"
    ENTRY_BAR_AMBIGUOUS = "ENTRY_BAR_AMBIGUOUS"
    KNOWN_HALT_WINDOW = "KNOWN_HALT_WINDOW"


CANONICAL_RETURN_STATUSES = frozenset(
    {
        PathTruthStatus.RESOLVED_TARGET_FIRST.value,
        PathTruthStatus.RESOLVED_STOP_FIRST.value,
        PathTruthStatus.RIGHT_CENSORED_SESSION_CLOSE.value,
    }
)
_RETURN_EVENT_BY_STATUS = {
    PathTruthStatus.RESOLVED_TARGET_FIRST.value: PathEvent.TARGET.value,
    PathTruthStatus.RESOLVED_STOP_FIRST.value: PathEvent.STOP.value,
    PathTruthStatus.RIGHT_CENSORED_SESSION_CLOSE.value: PathEvent.TIMEOUT.value,
}


def _policy_hash() -> str:
    payload = {
        "path_replay_schema_version": PATH_REPLAY_SCHEMA_VERSION,
        "path_replay_policy_version": PATH_REPLAY_POLICY_VERSION,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "bar_interval_seconds": DEFAULT_BAR_INTERVAL.total_seconds(),
        "path_events": sorted(event.value for event in PathEvent),
        "gap_stop_execution": "worse_executable_open",
        "interval_ordering": "censor_without_bound_ordered_evidence",
        "seeded_entry_mode": ENTRY_MODE_ALREADY_ENTERED,
    }
    encoded = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
    return hashlib.sha256(encoded).hexdigest()


PATH_REPLAY_POLICY_HASH_SHA256 = _policy_hash()


@dataclass(frozen=True)
class PathReplayResult:
    path_replay_schema_version: str
    path_replay_policy_version: str
    path_replay_policy_hash_sha256: str
    eligibility_policy_version: str
    replay_input_manifest: dict[str, Any]
    replay_input_hash_sha256: str
    replay_truth_hash_sha256: str
    path_replay_id: str
    path_truth_status: PathTruthStatus
    path_event: PathEvent | None
    conservative_policy_result: str | None
    source_artifact_identity: str | None
    source_artifact_hash_sha256: str | None
    source_coverage_complete: bool
    source_conflict: bool
    corporate_action_unresolved: bool
    ordered_evidence_identity: str | None
    ordered_evidence_hash_sha256: str | None
    ordered_evidence_complete: bool
    entry_time: datetime | None
    entry_price: float | None
    target_touched_at: datetime | None
    stop_touched_at: datetime | None
    exit_time: datetime | None
    exit_price: float | None
    event_time_precision: str | None
    event_interval_start: datetime | None
    event_interval_end: datetime | None
    mfe_price: float | None
    mfe_at: datetime | None
    mae_price: float | None
    mae_at: datetime | None
    post_entry_bar_count: int
    entry_bar_excluded: bool
    sequence_complete_through_exit: bool
    excursion_exact: bool
    bounds: dict[str, float | None]
    notes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        truth_hash = _hash(self._truth_payload())
        replay_id_hash = _hash(
            {
                "path_replay_schema_version": self.path_replay_schema_version,
                "path_replay_policy_version": self.path_replay_policy_version,
                "path_replay_policy_hash_sha256": self.path_replay_policy_hash_sha256,
                "replay_input_hash_sha256": self.replay_input_hash_sha256,
                "replay_truth_hash_sha256": truth_hash,
            }
        )
        object.__setattr__(self, "replay_truth_hash_sha256", truth_hash)
        object.__setattr__(self, "path_replay_id", f"path-v2-{replay_id_hash}")

    @property
    def schema_version(self) -> str:
        return self.path_replay_schema_version

    @property
    def policy_version(self) -> str:
        return self.path_replay_policy_version

    def _truth_payload(self) -> dict[str, Any]:
        return {
            "path_truth_status": self.path_truth_status.value,
            "path_event": self.path_event.value if self.path_event else None,
            "conservative_policy_result": self.conservative_policy_result,
            "source_artifact_identity": self.source_artifact_identity,
            "source_artifact_hash_sha256": self.source_artifact_hash_sha256,
            "source_coverage_complete": self.source_coverage_complete,
            "source_conflict": self.source_conflict,
            "corporate_action_unresolved": self.corporate_action_unresolved,
            "ordered_evidence_identity": self.ordered_evidence_identity,
            "ordered_evidence_hash_sha256": self.ordered_evidence_hash_sha256,
            "ordered_evidence_complete": self.ordered_evidence_complete,
            "entry_time": _iso(self.entry_time),
            "entry_price": self.entry_price,
            "target_touched_at": _iso(self.target_touched_at),
            "stop_touched_at": _iso(self.stop_touched_at),
            "exit_time": _iso(self.exit_time),
            "exit_price": self.exit_price,
            "event_time_precision": self.event_time_precision,
            "event_interval_start": _iso(self.event_interval_start),
            "event_interval_end": _iso(self.event_interval_end),
            "mfe_price": self.mfe_price,
            "mfe_at": _iso(self.mfe_at),
            "mae_price": self.mae_price,
            "mae_at": _iso(self.mae_at),
            "post_entry_bar_count": self.post_entry_bar_count,
            "entry_bar_excluded": self.entry_bar_excluded,
            "sequence_complete_through_exit": self.sequence_complete_through_exit,
            "excursion_exact": self.excursion_exact,
            "bounds": dict(self.bounds),
            "notes": list(self.notes),
        }

    def to_dict(self) -> dict[str, Any]:
        payload = {
            "path_replay_schema_version": self.path_replay_schema_version,
            "path_replay_policy_version": self.path_replay_policy_version,
            "path_replay_policy_hash_sha256": self.path_replay_policy_hash_sha256,
            "eligibility_policy_version": self.eligibility_policy_version,
            "replay_input_manifest": self.replay_input_manifest,
            "replay_input_hash_sha256": self.replay_input_hash_sha256,
            "replay_truth_hash_sha256": self.replay_truth_hash_sha256,
            "path_replay_id": self.path_replay_id,
            "path_truth_status": self.path_truth_status.value,
            "path_event": self.path_event.value if self.path_event else None,
            "conservative_policy_result": self.conservative_policy_result,
            "source_artifact_identity": self.source_artifact_identity,
            "source_artifact_hash_sha256": self.source_artifact_hash_sha256,
            "source_coverage_complete": self.source_coverage_complete,
            "source_conflict": self.source_conflict,
            "corporate_action_unresolved": self.corporate_action_unresolved,
            "ordered_evidence_identity": self.ordered_evidence_identity,
            "ordered_evidence_hash_sha256": self.ordered_evidence_hash_sha256,
            "ordered_evidence_complete": self.ordered_evidence_complete,
            "entry_time": _iso(self.entry_time),
            "entry_price": self.entry_price,
            "target_touched_at": _iso(self.target_touched_at),
            "stop_touched_at": _iso(self.stop_touched_at),
            "exit_time": _iso(self.exit_time),
            "exit_price": self.exit_price,
            "event_time_precision": self.event_time_precision,
            "event_interval_start": _iso(self.event_interval_start),
            "event_interval_end": _iso(self.event_interval_end),
            "mfe_price": self.mfe_price,
            "mfe_at": _iso(self.mfe_at),
            "mae_price": self.mae_price,
            "mae_at": _iso(self.mae_at),
            "post_entry_bar_count": self.post_entry_bar_count,
            "entry_bar_excluded": self.entry_bar_excluded,
            "sequence_complete_through_exit": self.sequence_complete_through_exit,
            "excursion_exact": self.excursion_exact,
            "bounds": dict(self.bounds),
            "notes": list(self.notes),
        }
        payload["replay_receipt_hash_sha256"] = _hash(payload)
        return payload


@dataclass(frozen=True)
class _ReplayContext:
    input_manifest: dict[str, Any]
    input_hash: str
    replay_id: str
    source_artifact_identity: str | None
    source_artifact_hash_sha256: str | None
    source_coverage_complete: bool
    source_conflict: bool
    corporate_action_unresolved: bool
    ordered_evidence_identity: str | None
    ordered_evidence_hash_sha256: str | None
    ordered_evidence_complete: bool


@dataclass(frozen=True)
class _OrderedPoint:
    observed_at: datetime
    event_type: str
    trade_price: float | None
    bid: float | None
    ask: float | None

    @property
    def entry_price(self) -> float | None:
        return self.trade_price if self.event_type == "TRADE" else self.ask

    @property
    def exit_price(self) -> float | None:
        return self.trade_price if self.event_type == "TRADE" else self.bid


def resolve_path(
    bars: object,
    *,
    decision_at: object,
    trigger: object,
    target: object,
    stop: object,
    source_conflict: object = False,
    corporate_action_unresolved: object = False,
    halt_intervals: object = (),
    session_close: object = None,
    ordered_events: object = (),
    ordered_evidence_complete: object = False,
    ordered_evidence_identity: object = None,
    ordered_evidence_hash_sha256: object = None,
    ordered_evidence_start: object = None,
    ordered_evidence_end: object = None,
    source_artifact_identity: object = None,
    source_artifact_hash_sha256: object = None,
    source_coverage_complete: object = False,
    replay_binding: object = None,
    future_evidence_receipt: object = None,
    entry_mode: object = None,
    entry_receipt: object = None,
    bar_interval: object = DEFAULT_BAR_INTERVAL,
) -> PathReplayResult:
    """Normalize untrusted evidence once, then resolve its canonical manifest."""

    manifest = _normalize_replay_input_manifest(
        bars,
        decision_at=decision_at,
        trigger=trigger,
        target=target,
        stop=stop,
        source_conflict=source_conflict,
        corporate_action_unresolved=corporate_action_unresolved,
        halt_intervals=halt_intervals,
        session_close=session_close,
        ordered_events=ordered_events,
        ordered_evidence_complete=ordered_evidence_complete,
        ordered_evidence_identity=ordered_evidence_identity,
        ordered_evidence_hash_sha256=ordered_evidence_hash_sha256,
        ordered_evidence_start=ordered_evidence_start,
        ordered_evidence_end=ordered_evidence_end,
        source_artifact_identity=source_artifact_identity,
        source_artifact_hash_sha256=source_artifact_hash_sha256,
        source_coverage_complete=source_coverage_complete,
        replay_binding=replay_binding,
        future_evidence_receipt=future_evidence_receipt,
        entry_mode=entry_mode,
        entry_receipt=entry_receipt,
        bar_interval=bar_interval,
    )
    return _resolve_manifest(manifest)


def _resolve_normalized_path(
    bars: list[Any] | tuple[Any, ...],
    *,
    decision_at: datetime | None,
    trigger: float | None,
    target: float | None,
    stop: float | None,
    source_conflict: bool,
    corporate_action_unresolved: bool,
    halt_intervals: tuple[tuple[datetime, datetime], ...],
    session_close: datetime | None,
    ordered_events: tuple[Any, ...],
    ordered_evidence_complete: bool,
    ordered_evidence_identity: str | None,
    ordered_evidence_hash_sha256: str | None,
    ordered_evidence_start: datetime | None,
    ordered_evidence_end: datetime | None,
    source_artifact_identity: str | None,
    source_artifact_hash_sha256: str | None,
    source_coverage_complete: bool,
    entry_mode: str | None,
    entry_receipt: dict[str, object] | None,
    bar_interval: timedelta,
    _input_manifest: dict[str, Any],
) -> PathReplayResult:
    """Resolve already-normalized long-side path truth."""

    normalized_decision_at = _utc_datetime(decision_at)
    normalized_session_close = _utc_datetime(session_close)
    normalized_halts = _normalized_halts(halt_intervals)
    seeded_entry_at = (
        _parsed_datetime(entry_receipt.get("effective_at"))
        if entry_mode == ENTRY_MODE_ALREADY_ENTERED
        and isinstance(entry_receipt, dict)
        else None
    )
    replay_scope_start = seeded_entry_at or normalized_decision_at
    scoped_ordered_events = tuple(
        event
        for event in ordered_events
        if _in_replay_scope(
            _timestamp(event),
            replay_scope_start,
            normalized_session_close,
        )
    )
    normalized_ordered_identity = ordered_evidence_identity
    normalized_ordered_hash = ordered_evidence_hash_sha256
    if _valid_sha(ordered_evidence_hash_sha256) and hmac.compare_digest(
        str(ordered_evidence_hash_sha256),
        _ordered_hash(ordered_events),
    ):
        normalized_ordered_hash = _ordered_hash(scoped_ordered_events)
    normalized_ordered_start = _utc_datetime(ordered_evidence_start)
    normalized_ordered_end = _utc_datetime(ordered_evidence_end)
    if normalized_ordered_start is not None and normalized_decision_at is not None:
        normalized_ordered_start = max(
            normalized_ordered_start,
            replay_scope_start or normalized_decision_at,
        )
    if normalized_ordered_end is not None and normalized_session_close is not None:
        normalized_ordered_end = min(normalized_ordered_end, normalized_session_close)
    if not ordered_evidence_complete:
        scoped_ordered_events = ()
        normalized_ordered_identity = None
        normalized_ordered_hash = None
        normalized_ordered_start = None
        normalized_ordered_end = None
    context = _context_from_manifest(_input_manifest)
    if source_conflict:
        return _empty(
            context,
            PathTruthStatus.SOURCE_CONFLICT,
            PathEvent.SOURCE_CONFLICT,
            "source hashes conflict",
        )
    if corporate_action_unresolved:
        return _empty(
            context,
            PathTruthStatus.CORPORATE_ACTION_UNRESOLVED,
            PathEvent.CORPORATE_ACTION_UNRESOLVED,
            "corporate action mapping is unresolved",
        )
    if normalized_decision_at is None:
        return _empty(
            context,
            PathTruthStatus.MISSING_DECISION_TIME,
            None,
            "decision timestamp is missing",
        )
    trigger_value = _number(trigger)
    target_value = _number(target)
    stop_value = _number(stop)
    if trigger is None or target is None or stop is None:
        return _empty(
            context,
            PathTruthStatus.MISSING_LEVELS,
            None,
            "trigger, target, and stop are required",
        )
    if (
        trigger_value is None
        or target_value is None
        or stop_value is None
        or min(trigger_value, target_value, stop_value) <= 0.0
        or bar_interval != DEFAULT_BAR_INTERVAL
        or (
            normalized_session_close is not None
            and normalized_session_close <= normalized_decision_at
        )
    ):
        return _empty(
            context,
            PathTruthStatus.DATA_INELIGIBLE,
            None,
            "bar interval must be exactly 60 seconds",
        )
    if (
        not bars
        and seeded_entry_at is None
        and normalized_session_close is not None
        and _halt_timestamps_valid(halt_intervals)
        and _fully_covered_by_halts(
            normalized_decision_at,
            normalized_session_close,
            normalized_halts,
        )
    ):
        return _pre_entry_censor(
            context,
            observed_at=normalized_decision_at,
            interval=normalized_session_close - normalized_decision_at,
            note="a sourced halt covers the full observed session",
            halt=True,
        )
    if (
        normalized_session_close is not None
        and (normalized_session_close - normalized_decision_at) % bar_interval
        != timedelta(0)
    ):
        return _pre_entry_censor(
            context,
            observed_at=normalized_decision_at,
            interval=normalized_session_close - normalized_decision_at,
            note="session close is not aligned to the canonical bar interval",
        )

    if not _bar_timestamps_valid(bars) or not _halt_timestamps_valid(halt_intervals):
        return _empty(
            context,
            PathTruthStatus.SOURCE_CONFLICT,
            PathEvent.SOURCE_CONFLICT,
            "source evidence contains a malformed or non-UTC-aware timestamp",
        )
    timestamped = _timestamped_bars(
        bars,
        decision_at=normalized_decision_at,
        session_close=normalized_session_close,
    )
    if not timestamped:
        if seeded_entry_at is not None and isinstance(entry_receipt, dict):
            seeded_entry_price = _number(entry_receipt.get("raw_entry_price"))
            assert seeded_entry_price is not None
            halt = bool(
                normalized_session_close is not None
                and _fully_covered_by_halts(
                    normalized_decision_at,
                    normalized_session_close,
                    normalized_halts,
                )
            )
            return _censored(
                context,
                status=(
                    PathTruthStatus.HALT_CENSORED
                    if halt
                    else PathTruthStatus.MISSING_INTERVAL_CENSORED
                ),
                event=PathEvent.HALT if halt else PathEvent.LIQUIDITY_FAILURE,
                entry_at=seeded_entry_at,
                entry_price=seeded_entry_price,
                interval_start=normalized_decision_at,
                interval_end=normalized_session_close,
                post_entry_bar_count=0,
                bounds=_bounds_with_prices([], (seeded_entry_price,)),
                note="path after the authenticated entry is unavailable",
            )
        if normalized_session_close is not None and _fully_covered_by_halts(
            normalized_decision_at,
            normalized_session_close,
            normalized_halts,
        ):
            return _pre_entry_censor(
                context,
                observed_at=normalized_decision_at,
                interval=normalized_session_close - normalized_decision_at,
                note="a sourced halt covers the full observed session",
                halt=True,
            )
        return _empty(
            context,
            PathTruthStatus.MISSING_BARS,
            None,
            "no bars are available after the decision",
        )
    if _duplicate_timestamps(timestamped):
        return _empty(
            context,
            PathTruthStatus.SOURCE_CONFLICT,
            PathEvent.SOURCE_CONFLICT,
            "multiple bars claim one interval",
        )

    ordered = _ordered_points(scoped_ordered_events)
    ordered_is_bound = _ordered_evidence_is_bound(
        scoped_ordered_events,
        complete=ordered_evidence_complete,
        identity=normalized_ordered_identity,
        claimed_hash=normalized_ordered_hash,
        coverage_start=normalized_ordered_start,
        coverage_end=normalized_ordered_end,
    )
    if ordered_evidence_complete and not ordered_is_bound:
        return _empty(
            context,
            PathTruthStatus.SOURCE_CONFLICT,
            PathEvent.SOURCE_CONFLICT,
            "claimed complete ordered evidence lacks one valid bound coverage window",
        )
    if ordered_is_bound and _ordered_conflicts_with_bars(
        ordered,
        timestamped,
        interval=bar_interval,
        decision_at=normalized_decision_at,
        session_close=normalized_session_close,
    ):
        return _empty(
            context,
            PathTruthStatus.SOURCE_CONFLICT,
            PathEvent.SOURCE_CONFLICT,
            "ordered evidence contradicts its bound OHLC intervals",
        )
    ordered_feed_type = ordered[0].event_type if ordered_is_bound and ordered else None
    if seeded_entry_at is not None and isinstance(entry_receipt, dict):
        entry_price = _number(entry_receipt.get("raw_entry_price"))
        assert entry_price is not None
        if target_value <= entry_price or stop_value >= entry_price:
            return _empty(
                context,
                PathTruthStatus.DATA_INELIGIBLE,
                None,
                "saved levels are dislocated from authenticated entry",
                entry_time=seeded_entry_at,
                entry_price=entry_price,
            )
        if seeded_entry_at < normalized_decision_at:
            partial_is_bound = bool(
                ordered_is_bound
                and _coverage_encloses(
                    normalized_ordered_start,
                    normalized_ordered_end,
                    seeded_entry_at,
                    normalized_decision_at,
                )
            )
            if not partial_is_bound:
                partial_halt = _fully_covered_by_halts(
                    seeded_entry_at,
                    normalized_decision_at,
                    normalized_halts,
                )
                return _censored(
                    context,
                    status=(
                        PathTruthStatus.HALT_CENSORED
                        if partial_halt
                        else PathTruthStatus.MISSING_INTERVAL_CENSORED
                    ),
                    event=(
                        PathEvent.HALT
                        if partial_halt
                        else PathEvent.LIQUIDITY_FAILURE
                    ),
                    entry_at=seeded_entry_at,
                    entry_price=entry_price,
                    interval_start=seeded_entry_at,
                    interval_end=normalized_decision_at,
                    post_entry_bar_count=0,
                    bounds=_bounds_with_prices([], (entry_price,)),
                    note=(
                        "a sourced halt covers the partial entry interval"
                        if partial_halt
                        else "the partial entry interval lacks complete ordered evidence"
                    ),
                )
            partial_points = [
                point
                for point in ordered
                if seeded_entry_at <= point.observed_at < normalized_decision_at
            ]
            terminal = _ordered_terminal(
                partial_points,
                after=seeded_entry_at,
                target=target_value,
                stop=stop_value,
            )
            if terminal is not None:
                event, event_at, exit_price, visited = terminal
                return _terminal(
                    context,
                    status=_status_for_event(event),
                    event=event,
                    event_at=event_at,
                    exit_price=exit_price,
                    entry_at=seeded_entry_at,
                    entry_price=entry_price,
                    target_at=event_at if event is PathEvent.TARGET else None,
                    stop_at=event_at if event is PathEvent.STOP else None,
                    event_precision="EXACT",
                    interval_start=seeded_entry_at,
                    interval_end=normalized_decision_at,
                    evidence_bars=[],
                    evidence_times=[],
                    terminal_points=visited,
                    excursion_exact=True,
                    entry_bar_excluded=False,
                )
        return _resolve_seeded_entry_path(
            context=context,
            timestamped=timestamped,
            decision_at=normalized_decision_at,
            session_close=normalized_session_close,
            halts=normalized_halts,
            ordered=ordered,
            ordered_is_bound=ordered_is_bound,
            ordered_feed_type=ordered_feed_type,
            ordered_start=normalized_ordered_start,
            ordered_end=normalized_ordered_end,
            entry_at=seeded_entry_at,
            entry_price=entry_price,
            target=target_value,
            stop=stop_value,
            bar_interval=bar_interval,
        )
    trigger_index: int | None = None
    expected_at = normalized_decision_at
    for index, (observed_at, bar) in enumerate(timestamped):
        if observed_at != expected_at:
            halt = _fully_covered_by_halts(expected_at, observed_at, normalized_halts)
            return _pre_entry_censor(
                context,
                observed_at=expected_at,
                interval=observed_at - expected_at,
                note=(
                    "a sourced halt censors the path before activation"
                    if halt
                    else "a missing interval censors the path before activation"
                ),
                halt=halt,
            )
        if _overlaps_halt(observed_at, observed_at + bar_interval, normalized_halts):
            return _pre_entry_censor(
                context,
                observed_at=observed_at,
                interval=bar_interval,
                note="a sourced halt overlaps the potential activation interval",
                halt=True,
            )
        if not _complete_ohlc(bar):
            return _pre_entry_censor(
                context,
                observed_at=observed_at,
                interval=bar_interval,
                note="an incomplete or nonfinite OHLC interval censors the path",
            )
        high = _number(_value(bar, "high"))
        interval_points = _points_in_interval(
            ordered,
            start=observed_at,
            end=observed_at + bar_interval,
        )
        interval_is_ordered = ordered_is_bound and _coverage_encloses(
            normalized_ordered_start,
            normalized_ordered_end,
            observed_at,
            observed_at + bar_interval,
        )
        ohlc_trigger = high is not None and high >= trigger_value
        ordered_trigger = any(
            point.entry_price is not None and point.entry_price >= trigger_value
            for point in interval_points
        )
        if interval_is_ordered and ordered_feed_type == "TRADE":
            if ordered_trigger != ohlc_trigger:
                return _empty(
                    context,
                    PathTruthStatus.SOURCE_CONFLICT,
                    PathEvent.SOURCE_CONFLICT,
                    "complete trade evidence does not reproduce the OHLC trigger",
                )
            trigger_reached = ordered_trigger
        elif interval_is_ordered and ordered_feed_type == "QUOTE":
            trigger_reached = ordered_trigger
        else:
            trigger_reached = ohlc_trigger
        if trigger_reached:
            trigger_index = index
            break
        expected_at = observed_at + bar_interval
    if trigger_index is None:
        tail_start = timestamped[-1][0] + bar_interval
        if (
            normalized_session_close is not None
            and tail_start < normalized_session_close
            and _fully_covered_by_halts(
                tail_start,
                normalized_session_close,
                normalized_halts,
            )
        ):
            return _pre_entry_censor(
                context,
                observed_at=tail_start,
                interval=normalized_session_close - tail_start,
                note="a sourced halt covers the no-trigger session tail",
                halt=True,
            )
        if (
            normalized_session_close is None
            or not timestamped
            or timestamped[-1][0] + bar_interval != normalized_session_close
        ):
            return _pre_entry_censor(
                context,
                observed_at=expected_at,
                interval=bar_interval,
                note="not-triggered truth lacks complete cadence through session close",
            )
        return _empty(
            context,
            PathTruthStatus.NOT_TRIGGERED,
            None,
            "complete session evidence proved the trigger was not reached",
        )

    entry_at, trigger_bar = timestamped[trigger_index]
    entry_open = _number(_value(trigger_bar, "open"))
    assert entry_open is not None
    interval_points = _points_in_interval(
        ordered,
        start=entry_at,
        end=entry_at + bar_interval,
    )
    trigger_interval_is_ordered = ordered_is_bound and _coverage_encloses(
        normalized_ordered_start,
        normalized_ordered_end,
        entry_at,
        entry_at + bar_interval,
    )
    quote_trigger_interval = (
        trigger_interval_is_ordered and ordered_feed_type == "QUOTE"
    )
    entry_is_interval_open = entry_open >= trigger_value and not quote_trigger_interval
    entry_price = max(trigger_value, entry_open)
    entry_exact_at = entry_at if entry_is_interval_open else None
    if trigger_interval_is_ordered and entry_is_interval_open:
        open_point = next(
            (
                point
                for point in interval_points
                if point.observed_at == entry_at
                and point.entry_price is not None
                and point.entry_price >= trigger_value
            ),
            None,
        )
        if open_point is None:
            return _empty(
                context,
                PathTruthStatus.SOURCE_CONFLICT,
                PathEvent.SOURCE_CONFLICT,
                "complete ordered evidence does not reproduce the OHLC open activation",
            )
    if not entry_is_interval_open and trigger_interval_is_ordered:
        entry_point = next(
            (
                point
                for point in interval_points
                if (price := point.entry_price) is not None and price >= trigger_value
            ),
            None,
        )
        if entry_point is not None:
            entry_exact_at = entry_point.observed_at
            entry_price = float(entry_point.entry_price or trigger_value)
        else:
            return _empty(
                context,
                PathTruthStatus.SOURCE_CONFLICT,
                PathEvent.SOURCE_CONFLICT,
                "complete ordered evidence does not reproduce the OHLC trigger",
            )
    if target_value <= entry_price or stop_value >= entry_price:
        return _empty(
            context,
            PathTruthStatus.DATA_INELIGIBLE,
            None,
            "saved levels are dislocated from entry",
            entry_time=entry_exact_at or entry_at,
            entry_price=entry_price,
        )

    trigger_high = _number(_value(trigger_bar, "high"))
    trigger_low = _number(_value(trigger_bar, "low"))
    assert trigger_high is not None and trigger_low is not None
    if quote_trigger_interval and entry_exact_at is not None:
        executable_prices = [
            point.exit_price
            for point in interval_points
            if point.observed_at >= entry_exact_at and point.exit_price is not None
        ]
        entry_target_touch = any(price >= target_value for price in executable_prices)
        entry_stop_touch = any(price <= stop_value for price in executable_prices)
    else:
        entry_target_touch = trigger_high >= target_value
        entry_stop_touch = trigger_low <= stop_value
    if entry_target_touch or entry_stop_touch:
        ordered_proves_only_pre_entry_touches = False
        if trigger_interval_is_ordered and entry_exact_at is not None:
            terminal = _ordered_terminal(
                interval_points,
                after=entry_exact_at,
                target=target_value,
                stop=stop_value,
            )
            barriers_complete = _ordered_barriers_complete(
                interval_points,
                target=target_value,
                stop=stop_value,
                require_target=entry_target_touch,
                require_stop=entry_stop_touch,
            )
            if not barriers_complete:
                return _empty(
                    context,
                    PathTruthStatus.SOURCE_CONFLICT,
                    PathEvent.SOURCE_CONFLICT,
                    "complete ordered evidence does not reproduce an OHLC entry-interval touch",
                )
            if terminal is not None:
                event, event_at, exit_price, visited = terminal
                return _terminal(
                    context,
                    status=_status_for_event(event),
                    event=event,
                    event_at=event_at,
                    exit_price=exit_price,
                    entry_at=entry_exact_at,
                    entry_price=entry_price,
                    target_at=event_at if event is PathEvent.TARGET else None,
                    stop_at=event_at if event is PathEvent.STOP else None,
                    event_precision="EXACT",
                    interval_start=entry_at,
                    interval_end=entry_at + bar_interval,
                    evidence_bars=[],
                    evidence_times=[],
                    terminal_points=visited,
                    excursion_exact=True,
                    entry_bar_excluded=False,
                )
            ordered_proves_only_pre_entry_touches = True
        if (
            not ordered_proves_only_pre_entry_touches
            and entry_is_interval_open
            and entry_target_touch != entry_stop_touch
        ):
            event = PathEvent.TARGET if entry_target_touch else PathEvent.STOP
            return _terminal(
                context,
                status=_status_for_event(event),
                event=event,
                event_at=entry_at,
                exit_price=target_value if event is PathEvent.TARGET else stop_value,
                entry_at=entry_at,
                entry_price=entry_price,
                target_at=entry_at if entry_target_touch else None,
                stop_at=entry_at if entry_stop_touch else None,
                event_precision="INTERVAL",
                interval_start=entry_at,
                interval_end=entry_at + bar_interval,
                evidence_bars=[trigger_bar],
                evidence_times=[entry_at],
                terminal_points=(),
                excursion_exact=False,
                entry_bar_excluded=False,
            )
        if not ordered_proves_only_pre_entry_touches:
            same_interval = (
                entry_is_interval_open and entry_target_touch and entry_stop_touch
            )
            return _censored(
                context,
                status=(
                    PathTruthStatus.TARGET_STOP_INTERVAL_CENSORED
                    if same_interval
                    else PathTruthStatus.ENTRY_INTERVAL_CENSORED
                ),
                event=(
                    PathEvent.SAME_INTERVAL_CENSORED
                    if same_interval
                    else PathEvent.ENTRY_INTERVAL_CENSORED
                ),
                entry_at=entry_exact_at or entry_at,
                entry_price=entry_price,
                target_at=entry_at if entry_target_touch else None,
                stop_at=entry_at if entry_stop_touch else None,
                interval_start=entry_at,
                interval_end=entry_at + bar_interval,
                post_entry_bar_count=0,
                bounds={"mfe_upper": trigger_high, "mae_lower": trigger_low},
                note="entry and terminal ordering is unresolved within one interval",
            )

    entry_time = entry_exact_at or entry_at
    evidence_bars: list[Any] = [trigger_bar]
    evidence_times: list[datetime] = [entry_at]
    post_entry = timestamped[trigger_index + 1 :]
    if not post_entry:
        if normalized_session_close == entry_at + bar_interval:
            return _timeout(
                context,
                entry_at=entry_time,
                entry_price=entry_price,
                close_at=normalized_session_close,
                close_price=_number(_value(trigger_bar, "close")),
                evidence_bars=evidence_bars,
                evidence_times=evidence_times,
                excursion_exact=False,
                entry_bar_excluded=entry_exact_at is None,
            )
        tail_start = entry_at + bar_interval
        tail_halt = (
            normalized_session_close is not None
            and _fully_covered_by_halts(
                tail_start,
                normalized_session_close,
                normalized_halts,
            )
        )
        return _censored(
            context,
            status=(
                PathTruthStatus.HALT_CENSORED
                if tail_halt
                else PathTruthStatus.MISSING_INTERVAL_CENSORED
            ),
            event=PathEvent.HALT if tail_halt else PathEvent.LIQUIDITY_FAILURE,
            entry_at=entry_time,
            entry_price=entry_price,
            interval_start=entry_at + bar_interval,
            interval_end=normalized_session_close,
            post_entry_bar_count=0,
            bounds=_bounds(evidence_bars),
            note="path after the entry interval is unavailable",
        )

    prior_at = entry_at
    for current_at, bar in post_entry:
        expected_at = prior_at + bar_interval
        if current_at != expected_at:
            halt = _fully_covered_by_halts(expected_at, current_at, normalized_halts)
            return _censored(
                context,
                status=(
                    PathTruthStatus.HALT_CENSORED
                    if halt
                    else PathTruthStatus.MISSING_INTERVAL_CENSORED
                ),
                event=PathEvent.HALT if halt else PathEvent.LIQUIDITY_FAILURE,
                entry_at=entry_time,
                entry_price=entry_price,
                interval_start=expected_at,
                interval_end=current_at,
                post_entry_bar_count=len(evidence_bars) - 1,
                bounds=_bounds(evidence_bars),
                note=(
                    "a sourced halt censors the path through resume"
                    if halt
                    else "a missing interval censors all later path evidence"
                ),
            )
        if _overlaps_halt(current_at, current_at + bar_interval, normalized_halts):
            return _censored(
                context,
                status=PathTruthStatus.HALT_CENSORED,
                event=PathEvent.HALT,
                entry_at=entry_time,
                entry_price=entry_price,
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                post_entry_bar_count=len(evidence_bars) - 1,
                bounds=_bounds(evidence_bars),
                note="a sourced halt censors the path through resume",
            )
        if not _complete_ohlc(bar):
            return _censored(
                context,
                status=PathTruthStatus.MISSING_INTERVAL_CENSORED,
                event=PathEvent.LIQUIDITY_FAILURE,
                entry_at=entry_time,
                entry_price=entry_price,
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                post_entry_bar_count=len(evidence_bars) - 1,
                bounds=_bounds(evidence_bars),
                note="an incomplete or nonfinite OHLC interval censors the path",
            )

        open_price = _number(_value(bar, "open"))
        high = _number(_value(bar, "high"))
        low = _number(_value(bar, "low"))
        assert open_price is not None and high is not None and low is not None
        interval_points = _points_in_interval(
            ordered,
            start=current_at,
            end=current_at + bar_interval,
        )
        interval_is_ordered = ordered_is_bound and _coverage_encloses(
            normalized_ordered_start,
            normalized_ordered_end,
            current_at,
            current_at + bar_interval,
        )
        quote_interval = interval_is_ordered and ordered_feed_type == "QUOTE"
        if not quote_interval and open_price <= stop_value:
            return _terminal(
                context,
                status=PathTruthStatus.RESOLVED_STOP_FIRST,
                event=PathEvent.STOP,
                event_at=current_at,
                exit_price=open_price,
                entry_at=entry_time,
                entry_price=entry_price,
                target_at=None,
                stop_at=current_at,
                event_precision="EXACT",
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                evidence_bars=evidence_bars,
                evidence_times=evidence_times,
                terminal_points=((open_price, current_at),),
                excursion_exact=False,
                entry_bar_excluded=entry_exact_at is None,
            )
        if not quote_interval and open_price >= target_value:
            return _terminal(
                context,
                status=PathTruthStatus.RESOLVED_TARGET_FIRST,
                event=PathEvent.TARGET,
                event_at=current_at,
                exit_price=open_price,
                entry_at=entry_time,
                entry_price=entry_price,
                target_at=current_at,
                stop_at=None,
                event_precision="EXACT",
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                evidence_bars=evidence_bars,
                evidence_times=evidence_times,
                terminal_points=((open_price, current_at),),
                excursion_exact=False,
                entry_bar_excluded=entry_exact_at is None,
            )

        if quote_interval:
            executable_prices = [
                point.exit_price
                for point in interval_points
                if point.exit_price is not None
            ]
            target_touch = any(price >= target_value for price in executable_prices)
            stop_touch = any(price <= stop_value for price in executable_prices)
        else:
            target_touch = high >= target_value
            stop_touch = low <= stop_value
        if (target_touch or stop_touch) and interval_is_ordered:
            terminal = _ordered_terminal(
                interval_points,
                after=current_at - timedelta(microseconds=1),
                target=target_value,
                stop=stop_value,
            )
            barriers_complete = _ordered_barriers_complete(
                interval_points,
                target=target_value,
                stop=stop_value,
                require_target=target_touch,
                require_stop=stop_touch,
            )
            if terminal is not None and barriers_complete:
                event, event_at, exit_price, _visited = terminal
                full_ordered_path = _coverage_encloses(
                    normalized_ordered_start,
                    normalized_ordered_end,
                    entry_time,
                    current_at + bar_interval,
                )
                excursion_points = (
                    _ordered_excursion_points(
                        ordered,
                        start=entry_time,
                        end=event_at,
                    )
                    if full_ordered_path
                    else ()
                )
                return _terminal(
                    context,
                    status=_status_for_event(event),
                    event=event,
                    event_at=event_at,
                    exit_price=exit_price,
                    entry_at=entry_time,
                    entry_price=entry_price,
                    target_at=event_at if event is PathEvent.TARGET else None,
                    stop_at=event_at if event is PathEvent.STOP else None,
                    event_precision="EXACT",
                    interval_start=current_at,
                    interval_end=current_at + bar_interval,
                    evidence_bars=evidence_bars,
                    evidence_times=evidence_times,
                    terminal_points=excursion_points,
                    excursion_exact=full_ordered_path,
                    entry_bar_excluded=entry_exact_at is None,
                )
            return _empty(
                context,
                PathTruthStatus.SOURCE_CONFLICT,
                PathEvent.SOURCE_CONFLICT,
                "complete ordered evidence does not reproduce an OHLC barrier touch",
            )
        if target_touch and stop_touch:
            return _censored(
                context,
                status=PathTruthStatus.TARGET_STOP_INTERVAL_CENSORED,
                event=PathEvent.SAME_INTERVAL_CENSORED,
                entry_at=entry_time,
                entry_price=entry_price,
                target_at=current_at,
                stop_at=current_at,
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                post_entry_bar_count=len(evidence_bars),
                bounds=_bounds([*evidence_bars, bar]),
                note="target and stop share one interval without complete order",
            )
        if target_touch or stop_touch:
            event = PathEvent.TARGET if target_touch else PathEvent.STOP
            return _terminal(
                context,
                status=_status_for_event(event),
                event=event,
                event_at=current_at,
                exit_price=target_value if target_touch else stop_value,
                entry_at=entry_time,
                entry_price=entry_price,
                target_at=current_at if target_touch else None,
                stop_at=current_at if stop_touch else None,
                event_precision="INTERVAL",
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                evidence_bars=[*evidence_bars, bar],
                evidence_times=[*evidence_times, current_at],
                terminal_points=(),
                excursion_exact=False,
                entry_bar_excluded=entry_exact_at is None,
            )
        evidence_bars.append(bar)
        evidence_times.append(current_at)
        prior_at = current_at

    if (
        normalized_session_close is None
        or evidence_times[-1] + bar_interval != normalized_session_close
    ):
        tail_start = evidence_times[-1] + bar_interval
        tail_halt = (
            normalized_session_close is not None
            and tail_start < normalized_session_close
            and _fully_covered_by_halts(
                tail_start,
                normalized_session_close,
                normalized_halts,
            )
        )
        return _censored(
            context,
            status=(
                PathTruthStatus.HALT_CENSORED
                if tail_halt
                else PathTruthStatus.MISSING_INTERVAL_CENSORED
            ),
            event=PathEvent.HALT if tail_halt else PathEvent.LIQUIDITY_FAILURE,
            entry_at=entry_time,
            entry_price=entry_price,
            interval_start=evidence_times[-1] + bar_interval,
            interval_end=normalized_session_close,
            post_entry_bar_count=len(evidence_bars) - 1,
            bounds=_bounds(evidence_bars),
            note="verified session-close boundary is unavailable",
        )
    return _timeout(
        context,
        entry_at=entry_time,
        entry_price=entry_price,
        close_at=normalized_session_close,
        close_price=_number(_value(evidence_bars[-1], "close")),
        evidence_bars=evidence_bars,
        evidence_times=evidence_times,
        excursion_exact=False,
        entry_bar_excluded=entry_exact_at is None,
    )


def _resolve_seeded_entry_path(
    *,
    context: _ReplayContext,
    timestamped: list[tuple[datetime, Any]],
    decision_at: datetime,
    session_close: datetime | None,
    halts: tuple[tuple[datetime, datetime], ...],
    ordered: list[_OrderedPoint],
    ordered_is_bound: bool,
    ordered_feed_type: str | None,
    ordered_start: datetime | None,
    ordered_end: datetime | None,
    entry_at: datetime,
    entry_price: float,
    target: float,
    stop: float,
    bar_interval: timedelta,
) -> PathReplayResult:
    """Resolve lifecycle evidence after an authenticated external entry."""

    evidence_bars: list[Any] = []
    evidence_times: list[datetime] = []
    prior_at = decision_at - bar_interval
    for current_at, bar in timestamped:
        expected_at = prior_at + bar_interval
        if current_at != expected_at:
            halt = _fully_covered_by_halts(expected_at, current_at, halts)
            return _censored(
                context,
                status=(
                    PathTruthStatus.HALT_CENSORED
                    if halt
                    else PathTruthStatus.MISSING_INTERVAL_CENSORED
                ),
                event=PathEvent.HALT if halt else PathEvent.LIQUIDITY_FAILURE,
                entry_at=entry_at,
                entry_price=entry_price,
                interval_start=expected_at,
                interval_end=current_at,
                post_entry_bar_count=max(0, len(evidence_bars) - 1),
                bounds=_bounds_with_prices(evidence_bars, (entry_price,)),
                note=(
                    "a sourced halt censors the path through resume"
                    if halt
                    else "a missing interval censors all later path evidence"
                ),
            )
        if _overlaps_halt(current_at, current_at + bar_interval, halts):
            return _censored(
                context,
                status=PathTruthStatus.HALT_CENSORED,
                event=PathEvent.HALT,
                entry_at=entry_at,
                entry_price=entry_price,
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                post_entry_bar_count=max(0, len(evidence_bars) - 1),
                bounds=_bounds_with_prices(evidence_bars, (entry_price,)),
                note="a sourced halt censors the authenticated entered path",
            )
        if not _complete_ohlc(bar):
            return _censored(
                context,
                status=PathTruthStatus.MISSING_INTERVAL_CENSORED,
                event=PathEvent.LIQUIDITY_FAILURE,
                entry_at=entry_at,
                entry_price=entry_price,
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                post_entry_bar_count=max(0, len(evidence_bars) - 1),
                bounds=_bounds_with_prices(evidence_bars, (entry_price,)),
                note="an incomplete or nonfinite OHLC interval censors the path",
            )

        open_price = _number(_value(bar, "open"))
        high = _number(_value(bar, "high"))
        low = _number(_value(bar, "low"))
        assert open_price is not None and high is not None and low is not None
        interval_points = _points_in_interval(
            ordered,
            start=current_at,
            end=current_at + bar_interval,
        )
        interval_is_ordered = ordered_is_bound and _coverage_encloses(
            ordered_start,
            ordered_end,
            current_at,
            current_at + bar_interval,
        )
        quote_interval = interval_is_ordered and ordered_feed_type == "QUOTE"
        if not quote_interval and open_price <= stop:
            return _terminal(
                context,
                status=PathTruthStatus.RESOLVED_STOP_FIRST,
                event=PathEvent.STOP,
                event_at=current_at,
                exit_price=open_price,
                entry_at=entry_at,
                entry_price=entry_price,
                target_at=None,
                stop_at=current_at,
                event_precision="EXACT",
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                evidence_bars=evidence_bars,
                evidence_times=evidence_times,
                terminal_points=((open_price, current_at),),
                excursion_exact=False,
                entry_bar_excluded=False,
            )
        if not quote_interval and open_price >= target:
            return _terminal(
                context,
                status=PathTruthStatus.RESOLVED_TARGET_FIRST,
                event=PathEvent.TARGET,
                event_at=current_at,
                exit_price=open_price,
                entry_at=entry_at,
                entry_price=entry_price,
                target_at=current_at,
                stop_at=None,
                event_precision="EXACT",
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                evidence_bars=evidence_bars,
                evidence_times=evidence_times,
                terminal_points=((open_price, current_at),),
                excursion_exact=False,
                entry_bar_excluded=False,
            )

        if quote_interval:
            executable_prices = [
                point.exit_price
                for point in interval_points
                if point.exit_price is not None
            ]
            target_touch = any(price >= target for price in executable_prices)
            stop_touch = any(price <= stop for price in executable_prices)
        else:
            target_touch = high >= target
            stop_touch = low <= stop
        if (target_touch or stop_touch) and interval_is_ordered:
            terminal = _ordered_terminal(
                interval_points,
                after=current_at - timedelta(microseconds=1),
                target=target,
                stop=stop,
            )
            barriers_complete = _ordered_barriers_complete(
                interval_points,
                target=target,
                stop=stop,
                require_target=target_touch,
                require_stop=stop_touch,
            )
            if terminal is not None and barriers_complete:
                event, event_at, exit_price, _visited = terminal
                full_ordered_path = _coverage_encloses(
                    ordered_start,
                    ordered_end,
                    entry_at,
                    current_at + bar_interval,
                )
                excursion_points = (
                    _ordered_excursion_points(
                        ordered,
                        start=entry_at,
                        end=event_at,
                    )
                    if full_ordered_path
                    else ()
                )
                return _terminal(
                    context,
                    status=_status_for_event(event),
                    event=event,
                    event_at=event_at,
                    exit_price=exit_price,
                    entry_at=entry_at,
                    entry_price=entry_price,
                    target_at=event_at if event is PathEvent.TARGET else None,
                    stop_at=event_at if event is PathEvent.STOP else None,
                    event_precision="EXACT",
                    interval_start=current_at,
                    interval_end=current_at + bar_interval,
                    evidence_bars=evidence_bars,
                    evidence_times=evidence_times,
                    terminal_points=excursion_points,
                    excursion_exact=full_ordered_path,
                    entry_bar_excluded=False,
                )
            return _empty(
                context,
                PathTruthStatus.SOURCE_CONFLICT,
                PathEvent.SOURCE_CONFLICT,
                "complete ordered evidence does not reproduce an OHLC barrier touch",
                entry_time=entry_at,
                entry_price=entry_price,
            )
        if target_touch and stop_touch:
            return _censored(
                context,
                status=PathTruthStatus.TARGET_STOP_INTERVAL_CENSORED,
                event=PathEvent.SAME_INTERVAL_CENSORED,
                entry_at=entry_at,
                entry_price=entry_price,
                target_at=current_at,
                stop_at=current_at,
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                post_entry_bar_count=len(evidence_bars),
                bounds=_bounds_with_prices(
                    [*evidence_bars, bar],
                    (entry_price,),
                ),
                note="target and stop share one interval without complete order",
            )
        if target_touch or stop_touch:
            event = PathEvent.TARGET if target_touch else PathEvent.STOP
            return _terminal(
                context,
                status=_status_for_event(event),
                event=event,
                event_at=current_at,
                exit_price=target if target_touch else stop,
                entry_at=entry_at,
                entry_price=entry_price,
                target_at=current_at if target_touch else None,
                stop_at=current_at if stop_touch else None,
                event_precision="INTERVAL",
                interval_start=current_at,
                interval_end=current_at + bar_interval,
                evidence_bars=[*evidence_bars, bar],
                evidence_times=[*evidence_times, current_at],
                terminal_points=(),
                excursion_exact=False,
                entry_bar_excluded=False,
            )
        evidence_bars.append(bar)
        evidence_times.append(current_at)
        prior_at = current_at

    tail_start = evidence_times[-1] + bar_interval
    if session_close is None or tail_start != session_close:
        tail_halt = bool(
            session_close is not None
            and tail_start < session_close
            and _fully_covered_by_halts(tail_start, session_close, halts)
        )
        return _censored(
            context,
            status=(
                PathTruthStatus.HALT_CENSORED
                if tail_halt
                else PathTruthStatus.MISSING_INTERVAL_CENSORED
            ),
            event=PathEvent.HALT if tail_halt else PathEvent.LIQUIDITY_FAILURE,
            entry_at=entry_at,
            entry_price=entry_price,
            interval_start=tail_start,
            interval_end=session_close,
            post_entry_bar_count=max(0, len(evidence_bars) - 1),
            bounds=_bounds_with_prices(evidence_bars, (entry_price,)),
            note="verified session-close boundary is unavailable",
        )
    return _timeout(
        context,
        entry_at=entry_at,
        entry_price=entry_price,
        close_at=session_close,
        close_price=_number(_value(evidence_bars[-1], "close")),
        evidence_bars=evidence_bars,
        evidence_times=evidence_times,
        excursion_exact=False,
        entry_bar_excluded=False,
    )


def canonical_path_contract_valid(payload: object) -> bool:
    """Rerun one embedded manifest and require exact canonical output equality."""

    if not isinstance(payload, dict):
        return False
    try:
        return _canonical_path_contract_valid_unchecked(payload)
    except (AttributeError, OverflowError, RecursionError, TypeError, ValueError):
        return False


def _canonical_path_contract_valid_unchecked(payload: dict[str, Any]) -> bool:
    manifest = payload.get("replay_input_manifest")
    if not isinstance(manifest, dict):
        return False
    input_hash = str(payload.get("replay_input_hash_sha256") or "")
    if not _valid_sha(input_hash) or not hmac.compare_digest(input_hash, _hash(manifest)):
        return False
    expected = _resolve_manifest(manifest).to_dict()
    expected_keys = set(expected)
    if not expected_keys.issubset(payload) or not set(payload).issubset(
        expected_keys | PATH_REPLAY_ENVELOPE_KEYS
    ):
        return False
    actual = {key: payload[key] for key in expected}
    receipt_hash = actual.get("replay_receipt_hash_sha256")
    receipt_body = {
        key: value
        for key, value in actual.items()
        if key != "replay_receipt_hash_sha256"
    }
    if not _valid_sha(receipt_hash) or not hmac.compare_digest(
        str(receipt_hash),
        _hash(receipt_body),
    ):
        return False
    return hmac.compare_digest(_canonical_json(actual), _canonical_json(expected))


def _resolve_manifest(manifest: dict[str, Any]) -> PathReplayResult:
    """Resolve one normalized manifest without re-normalizing public inputs."""

    if not _input_manifest_shape_valid(manifest):
        raise ValueError("invalid canonical replay input manifest shape")
    if not _manifest_roundtrip_valid(manifest):
        raise ValueError("replay input manifest is not in canonical normal form")
    context = _context_from_manifest(manifest)
    violations = manifest.get("input_contract_violations")
    markers = manifest.get("input_contract_markers")
    if not isinstance(violations, list) or any(
        not isinstance(item, str) or not item for item in violations
    ):
        raise ValueError("invalid input-contract violation manifest")
    if violations != sorted(set(violations)):
        raise ValueError("input-contract violations are not canonical")
    if not isinstance(markers, list) or [marker["code"] for marker in markers] != violations:
        raise ValueError("input-contract markers do not match violations")
    if context.source_conflict:
        return _empty(
            context,
            PathTruthStatus.SOURCE_CONFLICT,
            PathEvent.SOURCE_CONFLICT,
            "source hashes conflict",
        )
    if context.corporate_action_unresolved:
        return _empty(
            context,
            PathTruthStatus.CORPORATE_ACTION_UNRESOLVED,
            PathEvent.CORPORATE_ACTION_UNRESOLVED,
            "corporate action mapping is unresolved",
        )
    if violations:
        evidence_violation = any(
            marker["category"] == "evidence" for marker in markers
        )
        return _empty(
            context,
            (
                PathTruthStatus.SOURCE_CONFLICT
                if evidence_violation
                else PathTruthStatus.DATA_INELIGIBLE
            ),
            PathEvent.SOURCE_CONFLICT if evidence_violation else None,
            "input contract violations: " + ", ".join(violations),
        )
    kwargs = _resolve_kwargs_from_manifest(manifest)
    if kwargs is None:
        raise ValueError("invalid canonical replay input manifest")
    return _resolve_normalized_path(
        **kwargs,
        _input_manifest=manifest,
    )


def _input_manifest_shape_valid(manifest: dict[str, Any]) -> bool:
    if set(manifest) != PATH_REPLAY_INPUT_MANIFEST_KEYS:
        return False
    if not (
        manifest["path_replay_schema_version"] == PATH_REPLAY_SCHEMA_VERSION
        and manifest["path_replay_policy_version"] == PATH_REPLAY_POLICY_VERSION
        and manifest["path_replay_policy_hash_sha256"]
        == PATH_REPLAY_POLICY_HASH_SHA256
        and manifest["eligibility_policy_version"] == ELIGIBILITY_POLICY_VERSION
        and isinstance(manifest["input_contract_violations"], list)
        and isinstance(manifest["input_contract_markers"], list)
        and isinstance(manifest["ordered_events"], list)
    ):
        return False
    if not (
        (
            type(manifest["bar_interval_seconds"]) is float
            and manifest["bar_interval_seconds"]
            == DEFAULT_BAR_INTERVAL.total_seconds()
        )
        or _is_input_contract_sentinel(manifest["bar_interval_seconds"])
    ):
        return False
    if any(
        not isinstance(manifest[key], list)
        and not _is_input_contract_sentinel(manifest[key])
        for key in ("bars", "halt_intervals")
    ):
        return False
    replay_binding = manifest["replay_binding"]
    if not (
        replay_binding is None
        or _is_input_contract_sentinel(replay_binding)
        or _canonical_replay_binding_valid(replay_binding)
    ):
        return False
    future_receipt = manifest["future_evidence_receipt"]
    if not (
        future_receipt is None
        or _is_input_contract_sentinel(future_receipt)
        or _canonical_future_evidence_receipt_shape_valid(future_receipt)
    ):
        return False
    entry_mode = manifest["entry_mode"]
    entry_receipt = manifest["entry_receipt"]
    if entry_mode is None:
        if entry_receipt is not None and not _is_input_contract_sentinel(
            entry_receipt
        ):
            return False
    elif entry_mode != ENTRY_MODE_ALREADY_ENTERED:
        return False
    elif not _canonical_entry_receipt_shape_valid(
        entry_receipt,
        decision_at=_parsed_datetime(manifest.get("decision_at")),
        replay_binding=(replay_binding if isinstance(replay_binding, dict) else None),
    ):
        return False
    if any(
        type(manifest[key]) is not bool
        and not _is_input_contract_sentinel(manifest[key])
        for key in (
            "source_conflict",
            "corporate_action_unresolved",
            "source_coverage_complete",
            "ordered_feed_complete",
        )
    ):
        return False
    optional_strings = (
        "decision_at",
        "session_close",
        "ordered_feed_identity",
        "ordered_feed_hash_sha256",
        "ordered_coverage_start",
        "ordered_coverage_end",
        "source_artifact_identity",
        "source_artifact_hash_sha256",
    )
    if any(
        manifest[key] is not None
        and not isinstance(manifest[key], str)
        and not _is_input_contract_sentinel(manifest[key])
        for key in optional_strings
    ):
        return False
    try:
        derived_markers, derived_violations = _derive_input_contract(manifest)
    except (KeyError, TypeError, ValueError):
        return False
    if not (
        manifest["input_contract_markers"] == derived_markers
        and manifest["input_contract_violations"] == derived_violations
    ):
        return False
    return all(
        manifest[key] is None
        or type(manifest[key]) is float
        or _is_input_contract_sentinel(manifest[key])
        for key in ("trigger", "target", "stop")
    )


def _manifest_roundtrip_valid(manifest: dict[str, Any]) -> bool:
    sanitized = {
        key: (
            _sentinel_fallback(key)
            if _is_input_contract_sentinel(value)
            else value
        )
        for key, value in manifest.items()
    }
    sanitized["input_contract_markers"] = []
    sanitized["input_contract_violations"] = []
    recanonicalized = _recanonicalize_manifest(sanitized)
    return hmac.compare_digest(
        _canonical_json(recanonicalized),
        _canonical_json(sanitized),
    )


def _sentinel_fallback(field: str) -> object:
    fallback = _INPUT_SENTINEL_FALLBACKS[field]
    return list(fallback) if isinstance(fallback, list) else fallback


def _recanonicalize_manifest(manifest: dict[str, Any]) -> dict[str, Any]:
    """Round-trip a manifest through the sole public canonicalizer."""

    raw_bars: object = manifest.get("bars")
    if isinstance(raw_bars, list):
        raw_bars = [
            {
                "observed_at": _manifest_datetime_input(row.get("observed_at")),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
            }
            if isinstance(row, dict)
            else row
            for row in raw_bars
        ]
    raw_halts: object = manifest.get("halt_intervals")
    if isinstance(raw_halts, list):
        raw_halts = [
            tuple(_manifest_datetime_input(value) for value in row)
            if isinstance(row, list)
            else row
            for row in raw_halts
        ]
    raw_events: object = manifest.get("ordered_events")
    if isinstance(raw_events, list):
        raw_events = [
            {
                "observed_at": _manifest_datetime_input(row.get("observed_at")),
                "event_type": row.get("event_type"),
                **{
                    key: row[key]
                    for key in ("price", "bid", "ask")
                    if key in row
                },
            }
            if isinstance(row, dict)
            else row
            for row in raw_events
        ]
    raw_interval: object = manifest.get("bar_interval_seconds")
    if (
        isinstance(raw_interval, (int, float))
        and not isinstance(raw_interval, bool)
    ):
        try:
            raw_interval = timedelta(seconds=raw_interval)
        except OverflowError:
            pass
    recanonicalized = _normalize_replay_input_manifest(
        raw_bars,
        decision_at=_manifest_datetime_input(manifest.get("decision_at")),
        trigger=manifest.get("trigger"),
        target=manifest.get("target"),
        stop=manifest.get("stop"),
        source_conflict=manifest.get("source_conflict"),
        corporate_action_unresolved=manifest.get(
            "corporate_action_unresolved"
        ),
        halt_intervals=raw_halts,
        session_close=_manifest_datetime_input(manifest.get("session_close")),
        ordered_events=raw_events,
        ordered_evidence_complete=manifest.get("ordered_feed_complete"),
        ordered_evidence_identity=manifest.get("ordered_feed_identity"),
        ordered_evidence_hash_sha256=manifest.get("ordered_feed_hash_sha256"),
        ordered_evidence_start=_manifest_datetime_input(
            manifest.get("ordered_coverage_start")
        ),
        ordered_evidence_end=_manifest_datetime_input(
            manifest.get("ordered_coverage_end")
        ),
        source_artifact_identity=manifest.get("source_artifact_identity"),
        source_artifact_hash_sha256=manifest.get(
            "source_artifact_hash_sha256"
        ),
        source_coverage_complete=manifest.get("source_coverage_complete"),
        replay_binding=manifest.get("replay_binding"),
        future_evidence_receipt=manifest.get("future_evidence_receipt"),
        entry_mode=manifest.get("entry_mode"),
        entry_receipt=manifest.get("entry_receipt"),
        bar_interval=raw_interval,
    )
    return recanonicalized


def _manifest_datetime_input(value: object) -> object:
    if not isinstance(value, str):
        return value
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return value


def _context_from_manifest(manifest: dict[str, Any]) -> _ReplayContext:
    input_hash = _hash(manifest)
    events = tuple(
        {
            **row,
            "observed_at": _parsed_datetime(row.get("observed_at")),
        }
        for row in manifest.get("ordered_events", [])
        if isinstance(row, dict)
    )
    identity = manifest.get("ordered_feed_identity")
    claimed_hash = manifest.get("ordered_feed_hash_sha256")
    coverage_start = _parsed_datetime(manifest.get("ordered_coverage_start"))
    coverage_end = _parsed_datetime(manifest.get("ordered_coverage_end"))
    source_identity = manifest.get("source_artifact_identity")
    source_hash = manifest.get("source_artifact_hash_sha256")
    source_conflict = manifest.get("source_conflict") is True
    corporate_action = manifest.get("corporate_action_unresolved") is True
    return _ReplayContext(
        input_manifest=manifest,
        input_hash=input_hash,
        replay_id=f"path-v2-{input_hash}",
        source_artifact_identity=(
            source_identity if isinstance(source_identity, str) else None
        ),
        source_artifact_hash_sha256=(source_hash if isinstance(source_hash, str) else None),
        source_coverage_complete=manifest.get("source_coverage_complete") is True,
        source_conflict=source_conflict,
        corporate_action_unresolved=corporate_action,
        ordered_evidence_identity=identity if isinstance(identity, str) else None,
        ordered_evidence_hash_sha256=(
            claimed_hash if isinstance(claimed_hash, str) else None
        ),
        ordered_evidence_complete=_ordered_evidence_is_bound(
            events,
            complete=manifest.get("ordered_feed_complete") is True,
            identity=identity if isinstance(identity, str) else None,
            claimed_hash=claimed_hash if isinstance(claimed_hash, str) else None,
            coverage_start=coverage_start,
            coverage_end=coverage_end,
        ),
    )


def canonical_path_return_eligible(payload: object) -> bool:
    """Require a valid current receipt plus complete sourced return truth."""

    if not isinstance(payload, dict) or not canonical_path_contract_valid(payload):
        return False
    status = str(payload.get("path_truth_status") or "")
    manifest = payload.get("replay_input_manifest")
    if not isinstance(manifest, dict):
        return False
    decision_at = _parsed_datetime(manifest.get("decision_at"))
    session_close = _parsed_datetime(manifest.get("session_close"))
    replay_binding = manifest.get("replay_binding")
    future_receipt = manifest.get("future_evidence_receipt")
    receipt_subject = (
        future_receipt.get("subject")
        if isinstance(future_receipt, dict)
        else None
    )
    binding_kind = (
        replay_binding.get("origin", {}).get("kind")
        if isinstance(replay_binding, dict)
        and isinstance(replay_binding.get("origin"), dict)
        else None
    )
    entry_mode = manifest.get("entry_mode")
    entry_receipt = manifest.get("entry_receipt")
    seeded_entry_valid = bool(
        entry_mode == ENTRY_MODE_ALREADY_ENTERED
        and _canonical_entry_receipt_shape_valid(
            entry_receipt,
            decision_at=decision_at,
            replay_binding=(
                replay_binding if isinstance(replay_binding, dict) else None
            ),
        )
    )
    return bool(
        status in CANONICAL_RETURN_STATUSES
        and payload.get("path_event") == _RETURN_EVENT_BY_STATUS.get(status)
        and _canonical_replay_binding_valid(replay_binding)
        and _canonical_future_evidence_receipt_shape_valid(future_receipt)
        and isinstance(replay_binding, dict)
        and replay_binding.get("subject") == receipt_subject
        and isinstance(future_receipt, dict)
        and payload.get("source_artifact_identity")
        == future_receipt.get("receipt_id")
        and payload.get("source_artifact_hash_sha256")
        == future_receipt.get("receipt_hash_sha256")
        and str(payload.get("source_artifact_identity") or "").strip()
        and _valid_sha(payload.get("source_artifact_hash_sha256"))
        and payload.get("source_coverage_complete") is True
        and future_receipt.get("coverage_complete") is True
        and payload.get("source_conflict") is False
        and payload.get("corporate_action_unresolved") is False
        and payload.get("sequence_complete_through_exit") is True
        and decision_at is not None
        and session_close is not None
        and session_close > decision_at
        and (
            binding_kind != "alpha_paper_enter_intent"
            or seeded_entry_valid
        )
        and (
            entry_mode is None
            or seeded_entry_valid
        )
    )


def _resolve_kwargs_from_manifest(manifest: dict[str, Any]) -> dict[str, Any] | None:
    if not (
        manifest.get("path_replay_schema_version") == PATH_REPLAY_SCHEMA_VERSION
        and manifest.get("path_replay_policy_version") == PATH_REPLAY_POLICY_VERSION
        and manifest.get("path_replay_policy_hash_sha256")
        == PATH_REPLAY_POLICY_HASH_SHA256
        and manifest.get("eligibility_policy_version") == ELIGIBILITY_POLICY_VERSION
        and isinstance(manifest.get("bars"), list)
        and isinstance(manifest.get("halt_intervals"), list)
        and isinstance(manifest.get("ordered_events"), list)
        and isinstance(manifest.get("input_contract_violations"), list)
        and all(
            type(manifest.get(key)) is bool
            for key in (
                "source_conflict",
                "corporate_action_unresolved",
                "source_coverage_complete",
                "ordered_feed_complete",
            )
        )
    ):
        return None
    interval_seconds = _number(manifest.get("bar_interval_seconds"))
    if interval_seconds != DEFAULT_BAR_INTERVAL.total_seconds():
        return None
    bars: list[dict[str, Any]] = []
    for row in manifest["bars"]:
        if not isinstance(row, dict):
            return None
        bars.append(
            {
                "observed_at": _parsed_datetime(row.get("observed_at")),
                "open": row.get("open"),
                "high": row.get("high"),
                "low": row.get("low"),
                "close": row.get("close"),
            }
        )
    halts: list[tuple[datetime | None, datetime | None]] = []
    for row in manifest["halt_intervals"]:
        if not isinstance(row, list) or len(row) != 2:
            return None
        halts.append((_parsed_datetime(row[0]), _parsed_datetime(row[1])))
    events: list[dict[str, Any]] = []
    for row in manifest["ordered_events"]:
        if not isinstance(row, dict):
            return None
        events.append(
            {
                "observed_at": _parsed_datetime(row.get("observed_at")),
                "event_type": row.get("event_type"),
                **{
                    key: row[key]
                    for key in ("price", "bid", "ask")
                    if key in row
                },
            }
        )
    return {
        "bars": tuple(bars),
        "decision_at": _parsed_datetime(manifest.get("decision_at")),
        "trigger": _decoded_manifest_number(manifest.get("trigger")),
        "target": _decoded_manifest_number(manifest.get("target")),
        "stop": _decoded_manifest_number(manifest.get("stop")),
        "source_conflict": manifest["source_conflict"],
        "corporate_action_unresolved": manifest["corporate_action_unresolved"],
        "halt_intervals": tuple(halts),
        "session_close": _parsed_datetime(manifest.get("session_close")),
        "ordered_events": tuple(events),
        "ordered_evidence_complete": manifest["ordered_feed_complete"],
        "ordered_evidence_identity": manifest.get("ordered_feed_identity"),
        "ordered_evidence_hash_sha256": manifest.get("ordered_feed_hash_sha256"),
        "ordered_evidence_start": _parsed_datetime(
            manifest.get("ordered_coverage_start")
        ),
        "ordered_evidence_end": _parsed_datetime(
            manifest.get("ordered_coverage_end")
        ),
        "source_artifact_identity": manifest.get("source_artifact_identity"),
        "source_artifact_hash_sha256": manifest.get(
            "source_artifact_hash_sha256"
        ),
        "source_coverage_complete": manifest["source_coverage_complete"],
        "entry_mode": manifest.get("entry_mode"),
        "entry_receipt": manifest.get("entry_receipt"),
        "bar_interval": timedelta(seconds=interval_seconds),
    }


def _decoded_manifest_number(value: object) -> float | None:
    if isinstance(value, dict) and "invalid_numeric" in value:
        return float("nan")
    return _number(value)


def _legacy_path_payload_coherent(payload: dict[str, Any]) -> bool:
    """Validate the complete current path contract independently of booleans."""

    receipt_hash = str(payload.get("replay_receipt_hash_sha256") or "")
    receipt_body = {
        key: value
        for key, value in payload.items()
        if key != "replay_receipt_hash_sha256"
    }
    if not _valid_sha(receipt_hash) or not hmac.compare_digest(
        receipt_hash,
        _hash(receipt_body),
    ):
        return False
    status = str(payload.get("path_truth_status") or "")
    event = str(payload.get("path_event") or "")
    replay_id = str(payload.get("path_replay_id") or "")
    input_hash = str(payload.get("replay_input_hash_sha256") or "")
    entry_at = _parsed_datetime(payload.get("entry_time"))
    exit_at = _parsed_datetime(payload.get("exit_time"))
    precision = str(payload.get("event_time_precision") or "")
    entry_price = _number(payload.get("entry_price"))
    exit_price = _number(payload.get("exit_price"))
    if not (
        payload.get("path_replay_schema_version") == PATH_REPLAY_SCHEMA_VERSION
        and payload.get("path_replay_policy_version") == PATH_REPLAY_POLICY_VERSION
        and payload.get("path_replay_policy_hash_sha256")
        == PATH_REPLAY_POLICY_HASH_SHA256
        and payload.get("eligibility_policy_version") == ELIGIBILITY_POLICY_VERSION
        and _valid_sha(input_hash)
        and replay_id == f"path-v2-{input_hash}"
        and status in CANONICAL_RETURN_STATUSES
        and event == _RETURN_EVENT_BY_STATUS.get(status)
        and str(payload.get("source_artifact_identity") or "").strip()
        and _valid_sha(payload.get("source_artifact_hash_sha256"))
        and payload.get("source_coverage_complete") is True
        and payload.get("source_conflict") is False
        and payload.get("corporate_action_unresolved") is False
        and payload.get("sequence_complete_through_exit") is True
        and entry_at is not None
        and exit_at is not None
        and exit_at >= entry_at
        and entry_price is not None
        and entry_price > 0.0
        and exit_price is not None
        and exit_price > 0.0
        and precision in {"EXACT", "INTERVAL"}
    ):
        return False
    if precision == "INTERVAL":
        start = _parsed_datetime(payload.get("event_interval_start"))
        end = _parsed_datetime(payload.get("event_interval_end"))
        if start is None or end is None or end <= start or exit_at != start:
            return False
    else:
        start = _parsed_datetime(payload.get("event_interval_start"))
        end = _parsed_datetime(payload.get("event_interval_end"))
        if start is None or end is None:
            return False
        if event != PathEvent.TIMEOUT.value and not (start <= exit_at < end):
            return False
        if event == PathEvent.TIMEOUT.value and not (start == end == exit_at):
            return False
    assert entry_price is not None and exit_price is not None
    if event == PathEvent.TARGET.value and exit_price < entry_price:
        return False
    if event == PathEvent.STOP.value and exit_price >= entry_price:
        return False
    exact = payload.get("excursion_exact") is True
    bounds = payload.get("bounds")
    if exact:
        mfe = _number(payload.get("mfe_price"))
        mae = _number(payload.get("mae_price"))
        mfe_at = _parsed_datetime(payload.get("mfe_at"))
        mae_at = _parsed_datetime(payload.get("mae_at"))
        return bool(
            mfe is not None
            and mae is not None
            and mfe_at is not None
            and mae_at is not None
            and entry_at <= mfe_at <= exit_at
            and entry_at <= mae_at <= exit_at
            and mae <= min(entry_price, exit_price)
            and mfe >= max(entry_price, exit_price)
            and isinstance(bounds, dict)
            and not bounds
        )
    if payload.get("mfe_price") is not None or payload.get("mae_price") is not None:
        return False
    if not isinstance(bounds, dict):
        return False
    upper = _number(bounds.get("mfe_upper"))
    lower = _number(bounds.get("mae_lower"))
    return bool(
        upper is not None
        and lower is not None
        and lower <= min(entry_price, exit_price)
        and upper >= max(entry_price, exit_price)
        and lower <= upper
    )


def _base(context: _ReplayContext) -> dict[str, Any]:
    return {
        "path_replay_schema_version": PATH_REPLAY_SCHEMA_VERSION,
        "path_replay_policy_version": PATH_REPLAY_POLICY_VERSION,
        "path_replay_policy_hash_sha256": PATH_REPLAY_POLICY_HASH_SHA256,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "replay_input_manifest": context.input_manifest,
        "replay_input_hash_sha256": context.input_hash,
        "replay_truth_hash_sha256": "",
        "path_replay_id": context.replay_id,
        "source_artifact_identity": context.source_artifact_identity,
        "source_artifact_hash_sha256": context.source_artifact_hash_sha256,
        "source_coverage_complete": context.source_coverage_complete,
        "source_conflict": context.source_conflict,
        "corporate_action_unresolved": context.corporate_action_unresolved,
        "ordered_evidence_identity": context.ordered_evidence_identity,
        "ordered_evidence_hash_sha256": context.ordered_evidence_hash_sha256,
        "ordered_evidence_complete": context.ordered_evidence_complete,
    }


def _terminal(
    context: _ReplayContext,
    *,
    status: PathTruthStatus,
    event: PathEvent,
    event_at: datetime,
    exit_price: float,
    entry_at: datetime,
    entry_price: float,
    target_at: datetime | None,
    stop_at: datetime | None,
    event_precision: str,
    interval_start: datetime,
    interval_end: datetime,
    evidence_bars: list[Any],
    evidence_times: list[datetime],
    terminal_points: tuple[tuple[float, datetime], ...],
    excursion_exact: bool,
    entry_bar_excluded: bool,
) -> PathReplayResult:
    prices = list(terminal_points) if excursion_exact else []
    prices.append((entry_price, entry_at))
    prices.append((exit_price, event_at))
    mfe, mfe_at, mae, mae_at = (
        _extrema(prices) if excursion_exact else (None, None, None, None)
    )
    return PathReplayResult(
        **_base(context),
        path_truth_status=status,
        path_event=event,
        conservative_policy_result=(
            "stop_first" if event is PathEvent.STOP else "target_first"
        ),
        entry_time=entry_at,
        entry_price=entry_price,
        target_touched_at=target_at,
        stop_touched_at=stop_at,
        exit_time=event_at,
        exit_price=exit_price,
        event_time_precision=event_precision,
        event_interval_start=interval_start,
        event_interval_end=interval_end,
        mfe_price=mfe,
        mfe_at=mfe_at,
        mae_price=mae,
        mae_at=mae_at,
        post_entry_bar_count=max(0, len(evidence_bars) - 1),
        entry_bar_excluded=entry_bar_excluded,
        sequence_complete_through_exit=True,
        excursion_exact=excursion_exact,
        bounds=(
            {}
            if excursion_exact
            else _bounds_with_prices(
                evidence_bars,
                (entry_price, exit_price, *(price for price, _ in terminal_points)),
            )
        ),
        notes=("canonical event resolved without later-bar leakage",),
    )


def _timeout(
    context: _ReplayContext,
    *,
    entry_at: datetime,
    entry_price: float,
    close_at: datetime,
    close_price: float | None,
    evidence_bars: list[Any],
    evidence_times: list[datetime],
    excursion_exact: bool,
    entry_bar_excluded: bool,
) -> PathReplayResult:
    if close_price is None:
        return _censored(
            context,
            status=PathTruthStatus.MISSING_INTERVAL_CENSORED,
            event=PathEvent.LIQUIDITY_FAILURE,
            entry_at=entry_at,
            entry_price=entry_price,
            interval_start=evidence_times[-1],
            interval_end=close_at,
            post_entry_bar_count=max(0, len(evidence_bars) - 1),
            bounds=_bounds(evidence_bars),
            note="verified session-close price is missing",
        )
    prices = _exact_points(evidence_bars, evidence_times)
    prices.extend(((entry_price, entry_at), (close_price, close_at)))
    mfe, mfe_at, mae, mae_at = (
        _extrema(prices) if excursion_exact else (None, None, None, None)
    )
    return PathReplayResult(
        **_base(context),
        path_truth_status=PathTruthStatus.RIGHT_CENSORED_SESSION_CLOSE,
        path_event=PathEvent.TIMEOUT,
        conservative_policy_result="session_close",
        entry_time=entry_at,
        entry_price=entry_price,
        target_touched_at=None,
        stop_touched_at=None,
        exit_time=close_at,
        exit_price=close_price,
        event_time_precision="EXACT",
        event_interval_start=close_at,
        event_interval_end=close_at,
        mfe_price=mfe,
        mfe_at=mfe_at,
        mae_price=mae,
        mae_at=mae_at,
        post_entry_bar_count=max(0, len(evidence_bars) - 1),
        entry_bar_excluded=entry_bar_excluded,
        sequence_complete_through_exit=True,
        excursion_exact=excursion_exact,
        bounds=(
            {}
            if excursion_exact
            else _bounds_with_prices(evidence_bars, (entry_price, close_price))
        ),
        notes=("activated path right-censored at verified session close",),
    )


def _censored(
    context: _ReplayContext,
    *,
    status: PathTruthStatus,
    event: PathEvent,
    entry_at: datetime,
    entry_price: float,
    interval_start: datetime | None,
    interval_end: datetime | None,
    post_entry_bar_count: int,
    bounds: dict[str, float | None],
    note: str,
    target_at: datetime | None = None,
    stop_at: datetime | None = None,
) -> PathReplayResult:
    return PathReplayResult(
        **_base(context),
        path_truth_status=status,
        path_event=event,
        conservative_policy_result=None,
        entry_time=entry_at,
        entry_price=entry_price,
        target_touched_at=target_at,
        stop_touched_at=stop_at,
        exit_time=None,
        exit_price=None,
        event_time_precision="INTERVAL",
        event_interval_start=interval_start,
        event_interval_end=interval_end,
        mfe_price=None,
        mfe_at=None,
        mae_price=None,
        mae_at=None,
        post_entry_bar_count=post_entry_bar_count,
        entry_bar_excluded=True,
        sequence_complete_through_exit=False,
        excursion_exact=False,
        bounds=bounds,
        notes=(note,),
    )


def _pre_entry_censor(
    context: _ReplayContext,
    *,
    observed_at: datetime,
    interval: timedelta,
    note: str,
    halt: bool = False,
) -> PathReplayResult:
    result = _empty(
        context,
        PathTruthStatus.HALT_CENSORED
        if halt
        else PathTruthStatus.MISSING_INTERVAL_CENSORED,
        PathEvent.HALT if halt else PathEvent.LIQUIDITY_FAILURE,
        note,
    )
    return PathReplayResult(
        **{
            **result.__dict__,
            "event_time_precision": "INTERVAL",
            "event_interval_start": observed_at,
            "event_interval_end": observed_at + interval,
        }
    )


def _empty(
    context: _ReplayContext,
    status: PathTruthStatus,
    event: PathEvent | None,
    note: str,
    *,
    entry_time: datetime | None = None,
    entry_price: float | None = None,
) -> PathReplayResult:
    return PathReplayResult(
        **_base(context),
        path_truth_status=status,
        path_event=event,
        conservative_policy_result=None,
        entry_time=entry_time,
        entry_price=entry_price,
        target_touched_at=None,
        stop_touched_at=None,
        exit_time=None,
        exit_price=None,
        event_time_precision=None,
        event_interval_start=None,
        event_interval_end=None,
        mfe_price=None,
        mfe_at=None,
        mae_price=None,
        mae_at=None,
        post_entry_bar_count=0,
        entry_bar_excluded=True,
        sequence_complete_through_exit=False,
        excursion_exact=False,
        bounds={},
        notes=(note,),
    )


def _status_for_event(event: PathEvent) -> PathTruthStatus:
    return (
        PathTruthStatus.RESOLVED_TARGET_FIRST
        if event is PathEvent.TARGET
        else PathTruthStatus.RESOLVED_STOP_FIRST
    )


def _ordered_terminal(
    points: list[_OrderedPoint],
    *,
    after: datetime,
    target: float,
    stop: float,
) -> tuple[PathEvent, datetime, float, tuple[tuple[float, datetime], ...]] | None:
    visited: list[tuple[float, datetime]] = []
    for point in points:
        if point.observed_at < after:
            continue
        price = point.exit_price
        if price is None:
            continue
        visited.append((price, point.observed_at))
        if price <= stop:
            return PathEvent.STOP, point.observed_at, price, tuple(visited)
        if price >= target:
            return PathEvent.TARGET, point.observed_at, target, tuple(visited)
    return None


def _ordered_excursion_points(
    points: list[_OrderedPoint],
    *,
    start: datetime,
    end: datetime,
) -> tuple[tuple[float, datetime], ...]:
    return tuple(
        (price, point.observed_at)
        for point in points
        if start <= point.observed_at <= end
        and (price := point.exit_price) is not None
    )


def _ordered_barriers_complete(
    points: list[_OrderedPoint],
    *,
    target: float,
    stop: float,
    require_target: bool,
    require_stop: bool,
) -> bool:
    prices = [point.exit_price for point in points if point.exit_price is not None]
    return bool(
        (not require_target or any(price >= target for price in prices))
        and (not require_stop or any(price <= stop for price in prices))
    )


def _ordered_points(events: tuple[Any, ...]) -> list[_OrderedPoint]:
    output: list[_OrderedPoint] = []
    for event in events:
        observed_at = _timestamp(event)
        event_type = str(_value(event, "event_type") or "").upper()
        if observed_at is None or event_type not in {"TRADE", "QUOTE"}:
            continue
        output.append(
            _OrderedPoint(
                observed_at=observed_at,
                event_type=event_type,
                trade_price=_number(_value(event, "price")),
                bid=_number(_value(event, "bid")),
                ask=_number(_value(event, "ask")),
            )
        )
    return sorted(output, key=lambda point: point.observed_at)


def _ordered_evidence_is_bound(
    events: tuple[Any, ...],
    *,
    complete: bool,
    identity: str | None,
    claimed_hash: str | None,
    coverage_start: datetime | None,
    coverage_end: datetime | None,
) -> bool:
    normalized_start = _utc_datetime(coverage_start)
    normalized_end = _utc_datetime(coverage_end)
    return bool(
        complete
        and str(identity or "").strip()
        and _valid_sha(claimed_hash)
        and normalized_start is not None
        and normalized_end is not None
        and normalized_start < normalized_end
        and _ordered_events_valid(events)
        and hmac.compare_digest(str(claimed_hash), _ordered_hash(events))
    )


def _ordered_events_valid(events: tuple[Any, ...]) -> bool:
    timestamps: set[datetime] = set()
    event_types: set[str] = set()
    for event in events:
        observed_at = _timestamp(event)
        event_type = str(_value(event, "event_type") or "").upper()
        if observed_at is None or observed_at in timestamps:
            return False
        timestamps.add(observed_at)
        event_types.add(event_type)
        if event_type == "TRADE":
            price = _number(_value(event, "price"))
            if price is None or price <= 0.0:
                return False
        elif event_type == "QUOTE":
            bid = _number(_value(event, "bid"))
            ask = _number(_value(event, "ask"))
            if bid is None or ask is None or bid <= 0.0 or ask <= 0.0 or bid > ask:
                return False
        else:
            return False
    return bool(events) and len(event_types) == 1


def _ordered_hash(events: tuple[Any, ...]) -> str:
    canonical = sorted(
        (_canonical_ordered_event(event) for event in events),
        key=lambda event: (str(event.get("observed_at") or ""), json.dumps(event)),
    )
    return _hash(canonical)


def _canonical_ordered_event(value: Any) -> dict[str, Any]:
    output: dict[str, Any] = {
        "event_type": str(_value(value, "event_type") or "").upper(),
        "observed_at": _iso(_timestamp(value)),
    }
    for key in ("price", "bid", "ask"):
        if _value(value, key) is not None:
            output[key] = _number(_value(value, key))
    return output


def _points_in_interval(
    points: list[_OrderedPoint], *, start: datetime, end: datetime
) -> list[_OrderedPoint]:
    return [point for point in points if start <= point.observed_at < end]


def _ordered_conflicts_with_bars(
    points: list[_OrderedPoint],
    bars: list[tuple[datetime, Any]],
    *,
    interval: timedelta,
    decision_at: datetime,
    session_close: datetime | None,
) -> bool:
    for point in points:
        if point.observed_at < decision_at:
            continue
        if session_close is not None and point.observed_at >= session_close:
            continue
        matching = next(
            (
                bar
                for observed_at, bar in bars
                if observed_at <= point.observed_at < observed_at + interval
            ),
            None,
        )
        if matching is None:
            return True
        high = _number(_value(matching, "high"))
        low = _number(_value(matching, "low"))
        if high is None or low is None:
            return True
        if point.event_type == "QUOTE":
            continue
        if (
            point.trade_price is None
            or point.trade_price < low
            or point.trade_price > high
        ):
            return True
    return False


def _normalize_replay_input_manifest(
    bars: object,
    *,
    decision_at: object,
    trigger: object,
    target: object,
    stop: object,
    source_conflict: object,
    corporate_action_unresolved: object,
    halt_intervals: object,
    session_close: object,
    ordered_events: object,
    ordered_evidence_complete: object,
    ordered_evidence_identity: object,
    ordered_evidence_hash_sha256: object,
    ordered_evidence_start: object,
    ordered_evidence_end: object,
    source_artifact_identity: object,
    source_artifact_hash_sha256: object,
    source_coverage_complete: object,
    replay_binding: object,
    future_evidence_receipt: object,
    entry_mode: object,
    entry_receipt: object,
    bar_interval: object,
) -> dict[str, Any]:
    """Create one JSON-safe canonical manifest from an untrusted public call."""

    violations: list[str] = []
    normalized_decision = _normalize_datetime_input(
        decision_at,
        field="decision_at",
        violations=violations,
    )
    normalized_decision = _require_interval_representable_datetime(
        normalized_decision,
        field="decision_at",
        violations=violations,
    )
    normalized_close = _normalize_datetime_input(
        session_close,
        field="session_close",
        violations=violations,
    )
    normalized_close = _require_interval_representable_datetime(
        normalized_close,
        field="session_close",
        violations=violations,
    )
    normalized_trigger = _normalize_numeric_input(
        trigger,
        field="trigger",
        violations=violations,
    )
    normalized_target = _normalize_numeric_input(
        target,
        field="target",
        violations=violations,
    )
    normalized_stop = _normalize_numeric_input(
        stop,
        field="stop",
        violations=violations,
    )
    normalized_source_conflict = _normalize_bool_input(
        source_conflict,
        field="source_conflict",
        violations=violations,
    )
    normalized_corporate_action = _normalize_bool_input(
        corporate_action_unresolved,
        field="corporate_action_unresolved",
        violations=violations,
    )
    normalized_source_coverage = _normalize_bool_input(
        source_coverage_complete,
        field="source_coverage_complete",
        violations=violations,
    )
    normalized_ordered_complete = _normalize_bool_input(
        ordered_evidence_complete,
        field="ordered_evidence_complete",
        violations=violations,
    )
    normalized_source_identity = _normalize_optional_text_input(
        source_artifact_identity,
        field="source_artifact_identity",
        violations=violations,
    )
    normalized_source_hash = _normalize_optional_sha_input(
        source_artifact_hash_sha256,
        field="source_artifact_hash_sha256",
        violations=violations,
    )
    if (normalized_source_identity is None) != (normalized_source_hash is None):
        violations.append("source_artifact:identity_hash_pair_required")
        normalized_source_identity = None
        normalized_source_hash = None
    normalized_replay_binding = _normalize_replay_binding_input(
        replay_binding,
        violations=violations,
    )
    normalized_entry_mode, normalized_entry_receipt = (
        _normalize_entry_receipt_input(
            entry_mode,
            entry_receipt,
            decision_at=normalized_decision,
            replay_binding=normalized_replay_binding,
            violations=violations,
        )
    )
    ordered_scope_start = normalized_decision
    if normalized_entry_receipt is not None:
        entry_effective_at = _parsed_datetime(
            normalized_entry_receipt["effective_at"]
        )
        if entry_effective_at is not None:
            ordered_scope_start = entry_effective_at

    if type(bar_interval) is not timedelta or bar_interval != DEFAULT_BAR_INTERVAL:
        violations.append("bar_interval:expected_exact_60_second_timedelta")
    interval_seconds = DEFAULT_BAR_INTERVAL.total_seconds()
    normalized_bars = _normalize_bar_inputs(
        bars,
        decision_at=normalized_decision,
        session_close=normalized_close,
        violations=violations,
    )
    normalized_halts = _normalize_halt_inputs(
        halt_intervals,
        decision_at=ordered_scope_start,
        session_close=normalized_close,
        violations=violations,
    )
    normalized_future_receipt = _normalize_future_evidence_receipt_input(
        future_evidence_receipt,
        bars=normalized_bars,
        decision_at=normalized_decision,
        session_close=normalized_close,
        source_artifact_identity=normalized_source_identity,
        source_artifact_hash_sha256=normalized_source_hash,
        source_coverage_complete=normalized_source_coverage,
        violations=violations,
    )
    if (
        normalized_replay_binding is not None
        and normalized_future_receipt is not None
        and normalized_replay_binding["subject"]
        != normalized_future_receipt["subject"]
    ):
        violations.append("replay_binding:future_evidence_receipt_mismatch")

    normalized_ordered_identity: str | None = None
    normalized_ordered_hash: str | None = None
    normalized_ordered_start: datetime | None = None
    normalized_ordered_end: datetime | None = None
    normalized_events: list[dict[str, Any]] = []
    if normalized_ordered_complete:
        normalized_ordered_identity = _normalize_required_text_input(
            ordered_evidence_identity,
            field="ordered_evidence_identity",
            violations=violations,
        )
        claimed_hash = _normalize_required_sha_input(
            ordered_evidence_hash_sha256,
            field="ordered_evidence_hash_sha256",
            violations=violations,
        )
        normalized_ordered_start = _normalize_required_datetime_input(
            ordered_evidence_start,
            field="ordered_evidence_start",
            violations=violations,
        )
        normalized_ordered_end = _normalize_required_datetime_input(
            ordered_evidence_end,
            field="ordered_evidence_end",
            violations=violations,
        )
        normalized_events, full_hash = _normalize_ordered_event_inputs(
            ordered_events,
            decision_at=ordered_scope_start,
            session_close=normalized_close,
            violations=violations,
        )
        if (
            claimed_hash is not None
            and hmac.compare_digest(claimed_hash, full_hash)
        ):
            normalized_ordered_hash = _hash(normalized_events)
        else:
            normalized_ordered_hash = claimed_hash
        if normalized_ordered_start is not None and ordered_scope_start is not None:
            normalized_ordered_start = max(
                normalized_ordered_start,
                ordered_scope_start,
            )
        if normalized_ordered_end is not None and normalized_close is not None:
            normalized_ordered_end = min(normalized_ordered_end, normalized_close)

    normalized_violations = sorted(set(violations))
    manifest: dict[str, Any] = {
        "path_replay_schema_version": PATH_REPLAY_SCHEMA_VERSION,
        "path_replay_policy_version": PATH_REPLAY_POLICY_VERSION,
        "path_replay_policy_hash_sha256": PATH_REPLAY_POLICY_HASH_SHA256,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "input_contract_markers": [],
        "input_contract_violations": [],
        "decision_at": _iso(normalized_decision),
        "session_close": _iso(normalized_close),
        "trigger": normalized_trigger,
        "target": normalized_target,
        "stop": normalized_stop,
        "bar_interval_seconds": interval_seconds,
        "bars": normalized_bars,
        "halt_intervals": normalized_halts,
        "ordered_feed_identity": normalized_ordered_identity,
        "ordered_feed_hash_sha256": normalized_ordered_hash,
        "ordered_feed_complete": normalized_ordered_complete,
        "ordered_coverage_start": _iso(normalized_ordered_start),
        "ordered_coverage_end": _iso(normalized_ordered_end),
        "ordered_events": normalized_events,
        "source_artifact_identity": normalized_source_identity,
        "source_artifact_hash_sha256": normalized_source_hash,
        "source_coverage_complete": normalized_source_coverage,
        "source_conflict": normalized_source_conflict,
        "corporate_action_unresolved": normalized_corporate_action,
        "replay_binding": normalized_replay_binding,
        "future_evidence_receipt": normalized_future_receipt,
        "entry_mode": normalized_entry_mode,
        "entry_receipt": normalized_entry_receipt,
    }
    _embed_input_contract_sentinels(manifest, normalized_violations)
    markers, derived_violations = _derive_input_contract(manifest)
    manifest["input_contract_markers"] = markers
    manifest["input_contract_violations"] = derived_violations
    return manifest


def _input_contract_markers(violations: list[str]) -> list[dict[str, str]]:
    evidence_prefixes = (
        "bars[",
        "halt_intervals[",
        "ordered_events",
        "ordered_evidence_identity",
        "ordered_evidence_hash",
        "ordered_evidence_start",
        "ordered_evidence_end",
    )
    return [
        {
            "category": (
                "evidence" if code.startswith(evidence_prefixes) else "parameter"
            ),
            "code": code,
        }
        for code in violations
    ]


def _embed_input_contract_sentinels(
    manifest: dict[str, Any],
    violations: list[str],
) -> None:
    grouped: dict[str, list[str]] = {}
    for code in violations:
        grouped.setdefault(_violation_manifest_field(code), []).append(code)
    if "ordered_feed_complete" in grouped:
        manifest.update(
            {
                "ordered_feed_identity": None,
                "ordered_feed_hash_sha256": None,
                "ordered_coverage_start": None,
                "ordered_coverage_end": None,
                "ordered_events": [],
            }
        )
    for field, codes in grouped.items():
        category = (
            "evidence"
            if any(
                marker["category"] == "evidence"
                for marker in _input_contract_markers(codes)
            )
            else "parameter"
        )
        manifest[field] = {
            "category": category,
            "codes": sorted(set(codes)),
            "input_contract_sentinel": _INPUT_SENTINEL_SCHEMA,
            "kinds": sorted({code.rsplit(":", 1)[-1] for code in codes}),
        }


def _violation_manifest_field(code: str) -> str:
    if code.startswith("bars"):
        return "bars"
    if code.startswith("halt_intervals"):
        return "halt_intervals"
    if code.startswith(("ordered_events", "ordered_evidence")):
        return "ordered_feed_complete"
    if code.startswith("source_artifact"):
        return "source_artifact_identity"
    if code.startswith("replay_binding"):
        return "replay_binding"
    if code.startswith("future_evidence_receipt"):
        return "future_evidence_receipt"
    if code.startswith("entry_receipt"):
        return "entry_receipt"
    return {
        "bar_interval": "bar_interval_seconds",
        "corporate_action_unresolved": "corporate_action_unresolved",
        "decision_at": "decision_at",
        "session_close": "session_close",
        "source_conflict": "source_conflict",
        "source_coverage_complete": "source_coverage_complete",
        "stop": "stop",
        "target": "target",
        "trigger": "trigger",
    }[code.split(":", 1)[0]]


def _derive_input_contract(
    manifest: dict[str, Any],
) -> tuple[list[dict[str, str]], list[str]]:
    codes: list[str] = []
    for field, value in manifest.items():
        if not _is_input_contract_sentinel(value):
            continue
        assert isinstance(value, dict)
        sentinel_codes = value["codes"]
        assert isinstance(sentinel_codes, list)
        if any(_violation_manifest_field(code) != field for code in sentinel_codes):
            raise ValueError("input-contract sentinel is bound to the wrong field")
        expected_categories = {
            marker["category"] for marker in _input_contract_markers(sentinel_codes)
        }
        if expected_categories != {value["category"]}:
            raise ValueError("input-contract sentinel has the wrong category")
        codes.extend(sentinel_codes)
    violations = sorted(set(codes))
    return _input_contract_markers(violations), violations


def _is_input_contract_sentinel(value: object) -> bool:
    if not isinstance(value, dict) or (
        value.get("input_contract_sentinel") != _INPUT_SENTINEL_SCHEMA
    ):
        return False
    if set(value) != {"category", "codes", "input_contract_sentinel", "kinds"}:
        return False
    codes = value.get("codes")
    kinds = value.get("kinds")
    return bool(
        value.get("category") in {"evidence", "parameter"}
        and isinstance(codes, list)
        and codes
        and codes == sorted(set(codes))
        and all(isinstance(code, str) and code for code in codes)
        and isinstance(kinds, list)
        and kinds == sorted({code.rsplit(":", 1)[-1] for code in codes})
    )


def _normalize_replay_binding_input(
    value: object,
    *,
    violations: list[str],
) -> dict[str, object] | None:
    if value is None:
        return None
    starting_count = len(violations)
    if not isinstance(value, dict) or set(value) != {
        "schema_version",
        "subject",
        "origin",
    }:
        violations.append("replay_binding:invalid_keys")
        return None
    subject = value.get("subject")
    origin = value.get("origin")
    if value.get("schema_version") != REPLAY_BINDING_SCHEMA_VERSION:
        violations.append("replay_binding:schema_version")
    if not isinstance(subject, dict) or set(subject) != {"symbol", "market_date"}:
        violations.append("replay_binding:subject_keys")
        return None
    if not isinstance(origin, dict) or set(origin) != {
        "kind",
        "id",
        "lineage",
        "context_hash_sha256",
    }:
        violations.append("replay_binding:origin_keys")
        return None
    symbol = subject.get("symbol")
    market_date = subject.get("market_date")
    if not (
        isinstance(symbol, str)
        and symbol
        and symbol == symbol.strip()
        and symbol == symbol.upper()
    ):
        violations.append("replay_binding:symbol")
    if not _canonical_date_text(market_date):
        violations.append("replay_binding:market_date")
    kind = origin.get("kind")
    origin_id = origin.get("id")
    context_hash = origin.get("context_hash_sha256")
    if kind not in {
        "alpha_v6_shadow_decision",
        "alpha_paper_selection",
        "alpha_paper_enter_intent",
    }:
        violations.append("replay_binding:origin_kind")
    if not _strict_nonblank_text(origin_id):
        violations.append("replay_binding:origin_id")
    if not _valid_sha(context_hash):
        violations.append("replay_binding:context_hash_sha256")
    lineage = origin.get("lineage")
    expected_lineage = (
        {"decision_id", "scan_id", "source_signal_id", "shadow_signal_id"}
        if kind == "alpha_v6_shadow_decision"
        else {"selection_id", "scan_id", "signal_id", "intent_id"}
        if kind == "alpha_paper_enter_intent"
        else {"selection_id", "scan_id", "signal_id"}
        if kind == "alpha_paper_selection"
        else set()
    )
    if not isinstance(lineage, dict) or set(lineage) != expected_lineage:
        violations.append("replay_binding:lineage_keys")
        return None
    for field in sorted(expected_lineage):
        if not _strict_nonblank_text(lineage.get(field)):
            violations.append(f"replay_binding:lineage_{field}")
    primary_id_key = (
        "decision_id"
        if kind == "alpha_v6_shadow_decision"
        else "intent_id"
        if kind == "alpha_paper_enter_intent"
        else "selection_id"
    )
    if lineage.get(primary_id_key) != origin_id:
        violations.append("replay_binding:origin_lineage_id_mismatch")
    if len(violations) != starting_count:
        return None
    assert isinstance(symbol, str)
    assert isinstance(market_date, str)
    assert isinstance(kind, str)
    assert isinstance(origin_id, str)
    assert isinstance(context_hash, str)
    return {
        "schema_version": REPLAY_BINDING_SCHEMA_VERSION,
        "subject": {"symbol": symbol, "market_date": market_date},
        "origin": {
            "kind": kind,
            "id": origin_id,
            "lineage": {field: lineage[field] for field in sorted(expected_lineage)},
            "context_hash_sha256": context_hash,
        },
    }


def _canonical_replay_binding_valid(value: object) -> bool:
    violations: list[str] = []
    normalized = _normalize_replay_binding_input(value, violations=violations)
    if violations or normalized is None:
        return False
    try:
        return hmac.compare_digest(_canonical_json(value), _canonical_json(normalized))
    except (TypeError, ValueError):
        return False


def _normalize_entry_receipt_input(
    entry_mode: object,
    value: object,
    *,
    decision_at: datetime | None,
    replay_binding: dict[str, object] | None,
    violations: list[str],
) -> tuple[str | None, dict[str, object] | None]:
    """Validate one immutable already-entered receipt without repairing it."""

    if entry_mode is None and value is None:
        return None, None
    valid = bool(
        entry_mode == ENTRY_MODE_ALREADY_ENTERED
        and isinstance(value, dict)
        and set(value) == _ENTRY_RECEIPT_KEYS
        and decision_at is not None
        and replay_binding is not None
    )
    if not valid:
        violations.append("entry_receipt:invalid_contract")
        return None, None
    assert isinstance(value, dict)
    assert replay_binding is not None
    effective_at = _parsed_datetime(value.get("effective_at"))
    observed_at = _parsed_datetime(value.get("source_observed_at"))
    completed_at = _parsed_datetime(value.get("source_bar_completed_at"))
    price = value.get("raw_entry_price")
    binding_origin = replay_binding.get("origin")
    expected_origin = (
        {
            key: binding_origin[key]
            for key in ("kind", "id", "lineage")
        }
        if isinstance(binding_origin, dict)
        and all(key in binding_origin for key in ("kind", "id", "lineage"))
        else None
    )
    receipt_id = value.get("receipt_id")
    receipt_hash = value.get("receipt_hash_sha256")
    body = {key: value.get(key) for key in _ENTRY_RECEIPT_BODY_KEYS}
    digest = _hash(body)
    try:
        replay_start = _ceil_interval_boundary(effective_at)
    except (OverflowError, ValueError):
        replay_start = None
    valid = bool(
        value.get("schema_version") == ENTRY_RECEIPT_SCHEMA_VERSION
        and value.get("entry_mode") == ENTRY_MODE_ALREADY_ENTERED
        and type(price) is float
        and math.isfinite(price)
        and price > 0.0
        and _canonical_utc_datetime_text(value.get("effective_at"))
        and _canonical_utc_datetime_text(value.get("source_observed_at"))
        and _canonical_utc_datetime_text(value.get("source_bar_completed_at"))
        and effective_at is not None
        and observed_at is not None
        and completed_at is not None
        and replay_start == decision_at
        and observed_at <= completed_at <= effective_at
        and _strict_nonblank_text(value.get("source_observation_id"))
        and _valid_sha(value.get("source_bar_hash_sha256"))
        and expected_origin is not None
        and hmac.compare_digest(
            _canonical_json(value.get("replay_origin")),
            _canonical_json(expected_origin),
        )
        and isinstance(receipt_id, str)
        and receipt_id == f"{ENTRY_RECEIPT_ID_PREFIX}{digest}"
        and _valid_sha(receipt_hash)
        and hmac.compare_digest(str(receipt_hash), digest)
    )
    if not valid:
        violations.append("entry_receipt:invalid_contract")
        return None, None
    normalized = {
        **body,
        "receipt_id": receipt_id,
        "receipt_hash_sha256": receipt_hash,
    }
    try:
        if not hmac.compare_digest(
            _canonical_json(value),
            _canonical_json(normalized),
        ):
            violations.append("entry_receipt:invalid_contract")
            return None, None
    except (TypeError, ValueError):
        violations.append("entry_receipt:invalid_contract")
        return None, None
    return ENTRY_MODE_ALREADY_ENTERED, normalized


def _canonical_entry_receipt_shape_valid(
    value: object,
    *,
    decision_at: datetime | None,
    replay_binding: dict[str, object] | None,
) -> bool:
    violations: list[str] = []
    mode, normalized = _normalize_entry_receipt_input(
        ENTRY_MODE_ALREADY_ENTERED,
        value,
        decision_at=decision_at,
        replay_binding=replay_binding,
        violations=violations,
    )
    return bool(
        not violations
        and mode == ENTRY_MODE_ALREADY_ENTERED
        and normalized is not None
    )


def _ceil_interval_boundary(value: datetime | None) -> datetime | None:
    normalized = _utc_datetime(value)
    if normalized is None:
        return None
    floor = normalized.replace(second=0, microsecond=0)
    return floor if floor == normalized else floor + DEFAULT_BAR_INTERVAL


def _normalize_future_evidence_receipt_input(
    value: object,
    *,
    bars: list[dict[str, Any]],
    decision_at: datetime | None,
    session_close: datetime | None,
    source_artifact_identity: str | None,
    source_artifact_hash_sha256: str | None,
    source_coverage_complete: bool,
    violations: list[str],
) -> dict[str, object] | None:
    if value is None:
        return None
    starting_count = len(violations)
    if not isinstance(value, dict) or set(value) != _FUTURE_EVIDENCE_RECEIPT_KEYS:
        violations.append("future_evidence_receipt:invalid_keys")
        return None
    subject = value.get("subject")
    if not isinstance(subject, dict) or set(subject) != {"symbol", "market_date"}:
        violations.append("future_evidence_receipt:subject_keys")
        return None
    symbol = subject.get("symbol")
    market_date = subject.get("market_date")
    raw_identity = value.get("raw_artifact_identity")
    if value.get("schema_version") != FUTURE_EVIDENCE_RECEIPT_SCHEMA_VERSION:
        violations.append("future_evidence_receipt:schema_version")
    if not (
        isinstance(symbol, str)
        and symbol
        and symbol == symbol.strip()
        and symbol == symbol.upper()
    ):
        violations.append("future_evidence_receipt:symbol")
    if not _canonical_date_text(market_date):
        violations.append("future_evidence_receipt:market_date")
    if not _strict_nonblank_text(raw_identity):
        violations.append("future_evidence_receipt:raw_artifact_identity")
    if not _valid_sha(value.get("raw_bar_hash_sha256")):
        violations.append("future_evidence_receipt:raw_bar_hash_sha256")
    if type(value.get("bar_count")) is not int or int(value["bar_count"]) <= 0:
        violations.append("future_evidence_receipt:bar_count")
    for field in (
        "first_bar_at",
        "last_bar_at",
        "coverage_start",
        "coverage_end",
    ):
        if not _canonical_utc_datetime_text(value.get(field)):
            violations.append(f"future_evidence_receipt:{field}")
    if type(value.get("coverage_complete")) is not bool:
        violations.append("future_evidence_receipt:coverage_complete")
    receipt_id = value.get("receipt_id")
    receipt_hash = value.get("receipt_hash_sha256")
    if not (
        isinstance(receipt_id, str)
        and receipt_id.startswith(FUTURE_EVIDENCE_RECEIPT_ID_PREFIX)
        and _valid_sha(receipt_id.removeprefix(FUTURE_EVIDENCE_RECEIPT_ID_PREFIX))
    ):
        violations.append("future_evidence_receipt:receipt_id")
    if not _valid_sha(receipt_hash):
        violations.append("future_evidence_receipt:receipt_hash_sha256")
    if (
        len(violations) != starting_count
        or decision_at is None
        or session_close is None
        or not bars
    ):
        if decision_at is None:
            violations.append("future_evidence_receipt:decision_at_required")
        if session_close is None:
            violations.append("future_evidence_receipt:session_close_required")
        if not bars:
            violations.append("future_evidence_receipt:bars_required")
        return None

    assert isinstance(symbol, str)
    assert isinstance(market_date, str)
    assert isinstance(raw_identity, str)
    first_bar_at = bars[0]["observed_at"]
    last_bar_at = bars[-1]["observed_at"]
    expected_body: dict[str, object] = {
        "schema_version": FUTURE_EVIDENCE_RECEIPT_SCHEMA_VERSION,
        "subject": {"symbol": symbol, "market_date": market_date},
        "raw_artifact_identity": raw_identity,
        "raw_bar_hash_sha256": _hash(bars),
        "bar_count": len(bars),
        "first_bar_at": first_bar_at,
        "last_bar_at": last_bar_at,
        "coverage_start": _iso(decision_at),
        "coverage_end": _iso(session_close),
        "coverage_complete": source_coverage_complete,
    }
    expected_hash = _hash(expected_body)
    expected = {
        **expected_body,
        "receipt_id": f"{FUTURE_EVIDENCE_RECEIPT_ID_PREFIX}{expected_hash}",
        "receipt_hash_sha256": expected_hash,
    }
    if not hmac.compare_digest(_canonical_json(value), _canonical_json(expected)):
        violations.append("future_evidence_receipt:content_mismatch")
    if source_artifact_identity != expected["receipt_id"]:
        violations.append("future_evidence_receipt:source_identity_mismatch")
    if source_artifact_hash_sha256 != expected_hash:
        violations.append("future_evidence_receipt:source_hash_mismatch")
    if market_date != decision_at.date().isoformat():
        violations.append("future_evidence_receipt:decision_date_mismatch")
    return expected if len(violations) == starting_count else None


def _canonical_future_evidence_receipt_shape_valid(value: object) -> bool:
    if not isinstance(value, dict) or set(value) != _FUTURE_EVIDENCE_RECEIPT_KEYS:
        return False
    subject = value.get("subject")
    if not isinstance(subject, dict) or set(subject) != {"symbol", "market_date"}:
        return False
    body = {key: value[key] for key in _FUTURE_EVIDENCE_BODY_KEYS}
    digest = _hash(body)
    return bool(
        value.get("schema_version") == FUTURE_EVIDENCE_RECEIPT_SCHEMA_VERSION
        and isinstance(subject.get("symbol"), str)
        and subject["symbol"]
        and subject["symbol"] == subject["symbol"].strip().upper()
        and _canonical_date_text(subject.get("market_date"))
        and _strict_nonblank_text(value.get("raw_artifact_identity"))
        and _valid_sha(value.get("raw_bar_hash_sha256"))
        and type(value.get("bar_count")) is int
        and value["bar_count"] > 0
        and all(
            _canonical_utc_datetime_text(value.get(field))
            for field in (
                "first_bar_at",
                "last_bar_at",
                "coverage_start",
                "coverage_end",
            )
        )
        and type(value.get("coverage_complete")) is bool
        and value.get("receipt_id")
        == f"{FUTURE_EVIDENCE_RECEIPT_ID_PREFIX}{digest}"
        and value.get("receipt_hash_sha256") == digest
    )


def _canonical_utc_datetime_text(value: object) -> bool:
    parsed = _parsed_datetime(value)
    return bool(
        isinstance(value, str)
        and parsed is not None
        and parsed.utcoffset() == timedelta(0)
        and parsed.isoformat() == value
    )


def _canonical_date_text(value: object) -> bool:
    if not isinstance(value, str) or not value or value != value.strip():
        return False
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return False
    return parsed.isoformat() == value


def _strict_nonblank_text(value: object) -> bool:
    return isinstance(value, str) and bool(value) and value == value.strip()


def _normalize_datetime_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> datetime | None:
    if value is None:
        return None
    if not isinstance(value, datetime) or value.tzinfo is None:
        violations.append(f"{field}:expected_aware_datetime")
        return None
    try:
        if value.utcoffset() is None:
            violations.append(f"{field}:expected_aware_datetime")
            return None
        return value.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        violations.append(f"{field}:datetime_out_of_range")
        return None


def _normalize_required_datetime_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> datetime | None:
    normalized = _normalize_datetime_input(
        value,
        field=field,
        violations=violations,
    )
    if value is None:
        violations.append(f"{field}:required")
    return normalized


def _require_interval_representable_datetime(
    value: datetime | None,
    *,
    field: str,
    violations: list[str],
) -> datetime | None:
    if value is None:
        return None
    try:
        value - DEFAULT_BAR_INTERVAL
        value + DEFAULT_BAR_INTERVAL
    except OverflowError:
        violations.append(f"{field}:interval_arithmetic_out_of_range")
        return None
    return value


def _normalize_numeric_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> float | None:
    if value is None:
        return None
    normalized = _number(value)
    if normalized is None:
        violations.append(f"{field}:expected_finite_number")
        return None
    return normalized


def _normalize_bool_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> bool:
    if type(value) is not bool:
        violations.append(f"{field}:expected_bool")
        return False
    return value


def _normalize_optional_text_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        violations.append(f"{field}:expected_nonblank_string")
        return None
    return value.strip()


def _normalize_required_text_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> str | None:
    normalized = _normalize_optional_text_input(
        value,
        field=field,
        violations=violations,
    )
    if value is None:
        violations.append(f"{field}:required")
    return normalized


def _normalize_optional_sha_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _valid_sha(value):
        violations.append(f"{field}:expected_lowercase_sha256")
        return None
    return value


def _normalize_required_sha_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> str | None:
    normalized = _normalize_optional_sha_input(
        value,
        field=field,
        violations=violations,
    )
    if value is None:
        violations.append(f"{field}:required")
    return normalized


def _normalize_bar_inputs(
    bars: object,
    *,
    decision_at: datetime | None,
    session_close: datetime | None,
    violations: list[str],
) -> list[dict[str, Any]]:
    if not isinstance(bars, (list, tuple)):
        violations.append("bars:expected_list_or_tuple")
        return []
    output: list[dict[str, Any]] = []
    for index, row in enumerate(bars):
        observed_at = _normalize_required_datetime_input(
            _safe_value(row, "observed_at"),
            field=f"bars[{index}].observed_at",
            violations=violations,
        )
        if observed_at is not None and not _in_replay_scope(
            observed_at,
            decision_at,
            session_close,
        ):
            continue
        observed_at = _require_interval_representable_datetime(
            observed_at,
            field=f"bars[{index}].observed_at",
            violations=violations,
        )
        canonical: dict[str, Any] = {"observed_at": _iso(observed_at)}
        for key in ("open", "high", "low", "close"):
            canonical[key] = _normalize_ohlc_input(
                _safe_value(row, key),
                field=f"bars[{index}].{key}",
                violations=violations,
            )
        output.append(canonical)
    output.sort(key=lambda row: (str(row["observed_at"]), _canonical_json(row)))
    return output


def _normalize_ohlc_input(
    value: object,
    *,
    field: str,
    violations: list[str],
) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        violations.append(f"{field}:expected_number")
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        violations.append(f"{field}:number_out_of_range")
        return None
    if parsed != parsed or abs(parsed) == float("inf"):
        violations.append(f"{field}:nonfinite_number")
        return None
    return parsed


def _normalize_halt_inputs(
    halt_intervals: object,
    *,
    decision_at: datetime | None,
    session_close: datetime | None,
    violations: list[str],
) -> list[list[str]]:
    if not isinstance(halt_intervals, (list, tuple)):
        violations.append("halt_intervals:expected_list_or_tuple")
        return []
    output: list[list[str]] = []
    for index, row in enumerate(halt_intervals):
        if not isinstance(row, (list, tuple)) or len(row) != 2:
            violations.append(f"halt_intervals[{index}]:expected_pair")
            continue
        start = _normalize_required_datetime_input(
            row[0],
            field=f"halt_intervals[{index}].start",
            violations=violations,
        )
        end = _normalize_required_datetime_input(
            row[1],
            field=f"halt_intervals[{index}].end",
            violations=violations,
        )
        if start is None or end is None:
            continue
        if start >= end:
            violations.append(f"halt_intervals[{index}]:nonpositive_interval")
            continue
        if decision_at is not None:
            start = max(start, decision_at)
        if session_close is not None:
            end = min(end, session_close)
        if start < end:
            output.append([start.isoformat(), end.isoformat()])
    output.sort(key=lambda row: (row[0], row[1]))
    return output


def _normalize_ordered_event_inputs(
    ordered_events: object,
    *,
    decision_at: datetime | None,
    session_close: datetime | None,
    violations: list[str],
) -> tuple[list[dict[str, Any]], str]:
    if not isinstance(ordered_events, (list, tuple)):
        violations.append("ordered_events:expected_list_or_tuple")
        return [], _hash([])
    output: list[dict[str, Any]] = []
    full_claim: list[dict[str, Any]] = []
    for index, row in enumerate(ordered_events):
        observed_at = _normalize_required_datetime_input(
            _safe_value(row, "observed_at"),
            field=f"ordered_events[{index}].observed_at",
            violations=violations,
        )
        raw_type = _safe_value(row, "event_type")
        full_claim.append(
            _ordered_event_claim_payload(
                row,
                observed_at=observed_at,
                raw_event_type=raw_type,
            )
        )
        if observed_at is not None and not _in_replay_scope(
            observed_at,
            decision_at,
            session_close,
        ):
            continue
        if not isinstance(raw_type, str) or raw_type.upper() not in {"TRADE", "QUOTE"}:
            violations.append(f"ordered_events[{index}].event_type:unsupported")
            event_type = "INVALID"
        else:
            event_type = raw_type.upper()
        event: dict[str, Any] = {
            "event_type": event_type,
            "observed_at": _iso(observed_at),
        }
        keys = ("price",) if event_type == "TRADE" else ("bid", "ask")
        for key in keys:
            raw_value = _safe_value(row, key)
            event[key] = _normalize_numeric_input(
                raw_value,
                field=f"ordered_events[{index}].{key}",
                violations=violations,
            )
        output.append(event)
    output.sort(key=lambda row: (str(row["observed_at"]), _canonical_json(row)))
    full_claim.sort(key=lambda row: (str(row["observed_at"]), _canonical_json(row)))
    return output, _hash(full_claim)


def _ordered_event_claim_payload(
    row: object,
    *,
    observed_at: datetime | None,
    raw_event_type: object,
) -> dict[str, Any]:
    event: dict[str, Any] = {
        "event_type": (
            raw_event_type.upper() if isinstance(raw_event_type, str) else None
        ),
        "observed_at": _iso(observed_at),
    }
    for key in ("price", "bid", "ask"):
        value = _safe_value(row, key)
        if value is not None:
            event[key] = _ordered_claim_value(value)
    return event


def _ordered_claim_value(value: object) -> object:
    if isinstance(value, bool) or isinstance(value, str):
        return value
    if isinstance(value, (int, float)):
        normalized = _number(value)
        return value if normalized is not None else {
            "invalid_numeric": type(value).__name__
        }
    return {"invalid_type": type(value).__name__}


def _safe_value(value: object, key: str) -> object:
    try:
        return _value(value, key)
    except Exception:  # pragma: no cover - hostile adapter objects are fail-closed.
        return None


def _canonical_replay_input_manifest(
    bars: list[Any] | tuple[Any, ...],
    *,
    decision_at: datetime | None,
    trigger: float | None,
    target: float | None,
    stop: float | None,
    source_conflict: bool,
    corporate_action_unresolved: bool,
    halt_intervals: tuple[tuple[datetime, datetime], ...],
    session_close: datetime | None,
    ordered_events: tuple[Any, ...],
    ordered_evidence_complete: bool,
    ordered_evidence_identity: str | None,
    ordered_evidence_hash_sha256: str | None,
    ordered_evidence_start: datetime | None,
    ordered_evidence_end: datetime | None,
    source_artifact_identity: str | None,
    source_artifact_hash_sha256: str | None,
    source_coverage_complete: bool,
    replay_binding: dict[str, object] | None,
    future_evidence_receipt: dict[str, object] | None,
    bar_interval: timedelta,
) -> dict[str, Any]:
    scoped_bars = [
        _canonical_bar(bar)
        for bar in bars
        if _in_replay_scope(_timestamp(bar), decision_at, session_close)
    ]
    scoped_bars.sort(
        key=lambda bar: (str(bar.get("observed_at") or ""), json.dumps(bar))
    )
    scoped_events = [
        _canonical_ordered_event(event)
        for event in ordered_events
        if _in_replay_scope(_timestamp(event), decision_at, session_close)
    ]
    scoped_events.sort(
        key=lambda event: (str(event.get("observed_at") or ""), json.dumps(event))
    )
    scoped_halts: list[list[str | None]] = []
    for raw_start, raw_end in halt_intervals:
        start = _utc_datetime(raw_start)
        end = _utc_datetime(raw_end)
        if start is None or end is None:
            scoped_halts.append([None, None])
            continue
        if decision_at is not None:
            start = max(start, decision_at)
        if session_close is not None:
            end = min(end, session_close)
        if start < end:
            scoped_halts.append([_iso(start), _iso(end)])
    scoped_halts.sort(key=lambda interval: (str(interval[0]), str(interval[1])))
    coverage_start = _utc_datetime(ordered_evidence_start)
    coverage_end = _utc_datetime(ordered_evidence_end)
    if coverage_start is not None and decision_at is not None:
        coverage_start = max(coverage_start, decision_at)
    if coverage_end is not None and session_close is not None:
        coverage_end = min(coverage_end, session_close)
    return {
        "path_replay_schema_version": PATH_REPLAY_SCHEMA_VERSION,
        "path_replay_policy_version": PATH_REPLAY_POLICY_VERSION,
        "path_replay_policy_hash_sha256": PATH_REPLAY_POLICY_HASH_SHA256,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "decision_at": _iso(decision_at),
        "session_close": _iso(session_close),
        "trigger": _manifest_number(trigger),
        "target": _manifest_number(target),
        "stop": _manifest_number(stop),
        "bar_interval_seconds": bar_interval.total_seconds(),
        "bars": scoped_bars,
        "halt_intervals": scoped_halts,
        "ordered_feed_identity": _text(ordered_evidence_identity),
        "ordered_feed_hash_sha256": _text(ordered_evidence_hash_sha256),
        "ordered_feed_complete": ordered_evidence_complete is True,
        "ordered_coverage_start": _iso(coverage_start),
        "ordered_coverage_end": _iso(coverage_end),
        "ordered_events": scoped_events,
        "source_artifact_identity": _text(source_artifact_identity),
        "source_artifact_hash_sha256": _text(source_artifact_hash_sha256),
        "source_coverage_complete": source_coverage_complete is True,
        "source_conflict": source_conflict is True,
        "corporate_action_unresolved": corporate_action_unresolved is True,
        "replay_binding": replay_binding,
        "future_evidence_receipt": future_evidence_receipt,
    }


def _manifest_number(value: object) -> object:
    if value is None:
        return None
    parsed = _number(value)
    if parsed is not None:
        return parsed
    return {"invalid_numeric": str(value)}


def _in_replay_scope(
    observed_at: datetime | None,
    decision_at: datetime | None,
    session_close: datetime | None,
) -> bool:
    if observed_at is None or decision_at is None:
        return True
    return observed_at >= decision_at and (
        session_close is None or observed_at < session_close
    )


def _replay_context(
    bars: list[Any] | tuple[Any, ...],
    *,
    decision_at: datetime | None,
    trigger: float | None,
    target: float | None,
    stop: float | None,
    source_conflict: bool,
    corporate_action_unresolved: bool,
    halt_intervals: tuple[tuple[datetime, datetime], ...],
    session_close: datetime | None,
    ordered_events: tuple[Any, ...],
    ordered_evidence_complete: bool,
    ordered_evidence_identity: str | None,
    ordered_evidence_hash_sha256: str | None,
    ordered_evidence_start: datetime | None,
    ordered_evidence_end: datetime | None,
    source_artifact_identity: str | None,
    source_artifact_hash_sha256: str | None,
    source_coverage_complete: bool,
    replay_binding: dict[str, object] | None,
    future_evidence_receipt: dict[str, object] | None,
    bar_interval: timedelta,
) -> _ReplayContext:
    manifest = _canonical_replay_input_manifest(
        bars,
        decision_at=decision_at,
        trigger=trigger,
        target=target,
        stop=stop,
        source_conflict=source_conflict,
        corporate_action_unresolved=corporate_action_unresolved,
        halt_intervals=halt_intervals,
        session_close=session_close,
        ordered_events=ordered_events,
        ordered_evidence_complete=ordered_evidence_complete,
        ordered_evidence_identity=ordered_evidence_identity,
        ordered_evidence_hash_sha256=ordered_evidence_hash_sha256,
        ordered_evidence_start=ordered_evidence_start,
        ordered_evidence_end=ordered_evidence_end,
        source_artifact_identity=source_artifact_identity,
        source_artifact_hash_sha256=source_artifact_hash_sha256,
        source_coverage_complete=source_coverage_complete,
        replay_binding=replay_binding,
        future_evidence_receipt=future_evidence_receipt,
        bar_interval=bar_interval,
    )
    input_hash = _hash(manifest)
    return _ReplayContext(
        input_manifest=manifest,
        input_hash=input_hash,
        replay_id=f"path-v2-{input_hash}",
        source_artifact_identity=_text(source_artifact_identity),
        source_artifact_hash_sha256=_text(source_artifact_hash_sha256),
        source_coverage_complete=source_coverage_complete is True,
        source_conflict=source_conflict,
        corporate_action_unresolved=corporate_action_unresolved,
        ordered_evidence_identity=_text(ordered_evidence_identity),
        ordered_evidence_hash_sha256=_text(ordered_evidence_hash_sha256),
        ordered_evidence_complete=_ordered_evidence_is_bound(
            ordered_events,
            complete=ordered_evidence_complete,
            identity=ordered_evidence_identity,
            claimed_hash=ordered_evidence_hash_sha256,
            coverage_start=ordered_evidence_start,
            coverage_end=ordered_evidence_end,
        ),
    )


def _timestamped_bars(
    bars: list[Any] | tuple[Any, ...],
    *,
    decision_at: datetime,
    session_close: datetime | None,
) -> list[tuple[datetime, Any]]:
    output = [
        (timestamp, bar)
        for bar in bars
        if (timestamp := _timestamp(bar)) is not None and timestamp >= decision_at
        and (session_close is None or timestamp < session_close)
    ]
    return sorted(output, key=lambda item: item[0])


def _duplicate_timestamps(rows: list[tuple[datetime, Any]]) -> bool:
    return len({timestamp for timestamp, _ in rows}) != len(rows)


def _complete_ohlc(bar: Any) -> bool:
    values = [_number(_value(bar, key)) for key in ("open", "high", "low", "close")]
    if any(value is None for value in values):
        return False
    open_price, high, low, close = (float(value) for value in values if value is not None)
    return bool(
        low > 0.0
        and low <= min(open_price, close)
        and high >= max(open_price, close)
    )


def _exact_points(
    bars: list[Any], times: list[datetime]
) -> list[tuple[float, datetime]]:
    output: list[tuple[float, datetime]] = []
    for bar, observed_at in zip(bars, times, strict=True):
        high = _number(_value(bar, "high"))
        low = _number(_value(bar, "low"))
        if high is not None:
            output.append((high, observed_at))
        if low is not None:
            output.append((low, observed_at))
    return output


def _extrema(
    prices: list[tuple[float, datetime]],
) -> tuple[float | None, datetime | None, float | None, datetime | None]:
    if not prices:
        return None, None, None, None
    mfe, mfe_at = max(prices, key=lambda item: item[0])
    mae, mae_at = min(prices, key=lambda item: item[0])
    return mfe, mfe_at, mae, mae_at


def _bounds(bars: list[Any]) -> dict[str, float | None]:
    highs = [
        value
        for bar in bars
        if (value := _number(_value(bar, "high"))) is not None
    ]
    lows = [
        value
        for bar in bars
        if (value := _number(_value(bar, "low"))) is not None
    ]
    return {
        "mfe_upper": max(highs) if highs else None,
        "mae_lower": min(lows) if lows else None,
    }


def _bounds_with_prices(
    bars: list[Any], prices: tuple[float, ...]
) -> dict[str, float | None]:
    bounds = _bounds(bars)
    finite = [price for price in prices if _number(price) is not None]
    upper_values = [value for value in (bounds["mfe_upper"], *finite) if value is not None]
    lower_values = [value for value in (bounds["mae_lower"], *finite) if value is not None]
    return {
        "mfe_upper": max(upper_values) if upper_values else None,
        "mae_lower": min(lower_values) if lower_values else None,
    }


def _fully_covered_by_halts(
    start: datetime,
    end: datetime,
    intervals: tuple[tuple[datetime, datetime], ...],
) -> bool:
    if start >= end:
        return False
    cursor = start
    for halt_start, halt_end in sorted(intervals):
        if halt_end <= cursor:
            continue
        if halt_start > cursor:
            return False
        cursor = max(cursor, halt_end)
        if cursor >= end:
            return True
    return False


def _coverage_encloses(
    raw_start: datetime | None,
    raw_end: datetime | None,
    required_start: datetime,
    required_end: datetime,
) -> bool:
    start = _utc_datetime(raw_start)
    end = _utc_datetime(raw_end)
    return bool(
        start is not None
        and end is not None
        and start <= required_start
        and end >= required_end
    )


def _overlaps_halt(
    start: datetime,
    end: datetime,
    intervals: tuple[tuple[datetime, datetime], ...],
) -> bool:
    return any(start < halt_end and end > halt_start for halt_start, halt_end in intervals)


def _canonical_bar(value: Any) -> dict[str, Any]:
    return {
        "observed_at": _iso(_timestamp(value)),
        "open": _number(_value(value, "open")),
        "high": _number(_value(value, "high")),
        "low": _number(_value(value, "low")),
        "close": _number(_value(value, "close")),
    }


def _value(value: Any, key: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(key)
    return getattr(value, key, None)


def _timestamp(value: Any) -> datetime | None:
    item = _value(value, "observed_at")
    return _utc_datetime(item if isinstance(item, datetime) else None)


def _utc_datetime(value: datetime | None) -> datetime | None:
    if value is None or value.tzinfo is None or value.utcoffset() is None:
        return None
    return value.astimezone(timezone.utc)


def _bar_timestamps_valid(bars: list[Any] | tuple[Any, ...]) -> bool:
    return all(_timestamp(bar) is not None for bar in bars)


def _halt_timestamps_valid(
    intervals: tuple[tuple[datetime, datetime], ...],
) -> bool:
    return all(
        _utc_datetime(start) is not None
        and _utc_datetime(end) is not None
        and _utc_datetime(start) < _utc_datetime(end)  # type: ignore[operator]
        for start, end in intervals
    )


def _normalized_halts(
    intervals: tuple[tuple[datetime, datetime], ...],
) -> tuple[tuple[datetime, datetime], ...]:
    output: list[tuple[datetime, datetime]] = []
    for start, end in intervals:
        normalized_start = _utc_datetime(start)
        normalized_end = _utc_datetime(end)
        if normalized_start is not None and normalized_end is not None:
            output.append((normalized_start, normalized_end))
    return tuple(sorted(output))


def _parsed_datetime(value: object) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        parsed = float(value)
    except (OverflowError, TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and bool(_SHA256.fullmatch(value))


def _text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _hash(value: object) -> str:
    encoded = _canonical_json(value).encode()
    return hashlib.sha256(encoded).hexdigest()


def _canonical_json(value: object) -> str:
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


__all__ = [
    "CANONICAL_RETURN_STATUSES",
    "ELIGIBILITY_POLICY_VERSION",
    "PATH_REPLAY_POLICY_HASH_SHA256",
    "PATH_REPLAY_POLICY_VERSION",
    "PATH_REPLAY_SCHEMA_VERSION",
    "PathEvent",
    "PathReplayResult",
    "PathTruthStatus",
    "canonical_path_contract_valid",
    "canonical_path_return_eligible",
    "resolve_path",
]
