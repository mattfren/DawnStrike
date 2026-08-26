"""Deterministic, research-only attribution for strategy misses.

This module consumes already-retained performance rows.  It does not query
sources, write state, change a strategy policy, promote a model, or execute a
broker order.  In particular, an open mark-to-market row is never treated as a
closed outcome and missing truth is never converted to zero.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any

from intraday_scanner.performance.contracts import Cohort, normalize_cohort, safe_float


class AttributionState(str, Enum):
    """Mutually exclusive evidence state for one retained row."""

    CLOSED = "closed"
    OPEN_MTM = "open_mtm"
    NO_TRADE = "no_trade"
    MISSING_OUTCOME = "missing_outcome"
    CONFLICTING_OUTCOME = "conflicting_outcome"
    UNKNOWN = "unknown"


class Eligibility(str, Enum):
    """Whether the row can train a closed-outcome learner."""

    ELIGIBLE = "eligible"
    INELIGIBLE = "ineligible"
    UNKNOWN = "unknown"


_HASH_FIELDS = (
    "source_hash_sha256",
    "input_hash_sha256",
    "source_bar_hash_sha256",
    "source_artifact_hash_sha256",
    "body_sha256",
    "benchmark_hash_sha256",
    "fill_truth_hash_sha256",
    "fill_truth_receipt_hash_sha256",
    "committed_fill_truth_hash",
    "close_receipt_hash_sha256",
    "ledger_source_hash_sha256",
    "data_snapshot_id",
    "fill_data_snapshot_id",
    "close_data_snapshot_id",
)
_CONFIG_FIELDS = (
    "config_identity",
    "strategy_config_hash",
    "strategy_config_sha256",
    "config_hash_sha256",
    "strategy_fingerprint",
    "strategy_definition_hash_sha256",
    "strategy_semantics_fingerprint",
    "strategy_identity_hash_sha256",
)
_TEXT_FIELDS = (
    "miss_category",
    "category",
    "failure_category",
    "reason_category",
    "gate",
    "gate_id",
    "reason",
    "miss_reason",
    "no_trade_reason",
    "decision_reason",
    "failure_reason",
    "outcome_status",
    "activation_status",
    "terminal_state",
)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_PAPER_OPS_FORWARD_COHORTS = {
    "official_forward",
    "official_forward_paper",
    "shadow_challenger",
}
_PAPER_OPS_POINT_IN_TIME_LIMITATIONS = (
    "paper_ops_open_pending_point_in_time_is_limited_to_entry_evidence_at_or_before_cutoff",
    "paper_ops_terminal_lifecycle_events_after_cutoff_are_omitted_not_back_projected",
    "paper_ops_current_materialized_ledger_cannot_reconstruct_historical_open_state_without_entry_and_terminal_timestamps",
)


@dataclass(frozen=True, slots=True)
class StrategyMissAttributionRow:
    """A deterministic attribution result for one input record."""

    record_id: str | None
    market_date: str | None
    cohort: str
    strategy_id: str
    strategy_version: str | None
    config_identity: str | None
    execution_policy_version: str | None
    state: AttributionState
    eligibility: Eligibility
    classification: str
    categories: tuple[str, ...]
    reasons: tuple[str, ...]
    return_pct: float | None
    benchmark_return_pct: float | None
    evidence_hashes: tuple[str, ...]
    # Optional lifecycle identity.  Aggregate rows from older exports retain
    # ``None``; explicit child lifecycle records are independently attributable.
    lifecycle_id: str | None = None
    episode_id: str | None = None
    fill_truth_status: str | None = None
    eligibility_reason: str | None = None
    # Source identity is retained so a consumer can distinguish exact PaperOps
    # lifecycles from legacy/aggregate observations without parsing evidence
    # hashes or record IDs.
    series_role: str | None = None
    record_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "market_date": self.market_date,
            "cohort": self.cohort,
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "config_identity": self.config_identity,
            "execution_policy_version": self.execution_policy_version,
            "state": self.state.value,
            "eligibility": self.eligibility.value,
            "classification": self.classification,
            "categories": list(self.categories),
            "reasons": list(self.reasons),
            "return_pct": self.return_pct,
            "benchmark_return_pct": self.benchmark_return_pct,
            "evidence_hashes": list(self.evidence_hashes),
            "lifecycle_id": self.lifecycle_id,
            "episode_id": self.episode_id,
            "fill_truth_status": self.fill_truth_status,
            "eligibility_reason": self.eligibility_reason,
            "series_role": self.series_role,
            "record_type": self.record_type,
        }


@dataclass(frozen=True, slots=True)
class StrategyMissAttributionSummary:
    """Per-strategy, per-cohort machine-readable miss summary."""

    strategy_id: str
    strategy_version: str | None
    config_identity: str | None
    execution_policy_version: str | None
    cohort: str
    date_cutoff: str | None
    row_count: int
    eligible_count: int
    ineligible_count: int
    unknown_eligibility_count: int
    closed_count: int
    open_mtm_count: int
    no_trade_count: int
    missing_outcome_count: int
    conflicting_outcome_count: int
    provisional_closed_count: int
    closed_win_count: int
    closed_loss_count: int
    closed_flat_count: int
    closed_return_sum_pct: float | None
    open_mtm_return_sum_pct: float | None
    opportunity_cost_count: int
    opportunity_cost_return_sum_pct: float | None
    classification_counts: tuple[tuple[str, int], ...]
    category_counts: tuple[tuple[str, int], ...]
    evidence_hashes: tuple[str, ...]
    remediation_hypotheses: tuple[dict[str, Any], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "strategy_id": self.strategy_id,
            "strategy_version": self.strategy_version,
            "config_identity": self.config_identity,
            "execution_policy_version": self.execution_policy_version,
            "cohort": self.cohort,
            "date_cutoff": self.date_cutoff,
            "row_count": self.row_count,
            "eligibility": {
                "eligible_count": self.eligible_count,
                "ineligible_count": self.ineligible_count,
                "unknown_count": self.unknown_eligibility_count,
            },
            "states": {
                "closed": self.closed_count,
                "open_mtm": self.open_mtm_count,
                "no_trade": self.no_trade_count,
                "missing_outcome": self.missing_outcome_count,
                "conflicting_outcome": self.conflicting_outcome_count,
            },
            "closed_outcomes": {
                "wins": self.closed_win_count,
                "losses": self.closed_loss_count,
                "flats": self.closed_flat_count,
                "provisional_count": self.provisional_closed_count,
                "return_sum_pct": self.closed_return_sum_pct,
            },
            "open_mtm_return_sum_pct": self.open_mtm_return_sum_pct,
            "opportunity_cost": {
                "count": self.opportunity_cost_count,
                "benchmark_return_sum_pct": self.opportunity_cost_return_sum_pct,
            },
            "classification_counts": dict(self.classification_counts),
            "category_counts": dict(self.category_counts),
            "evidence_hashes": list(self.evidence_hashes),
            "remediation_hypotheses": [dict(item) for item in self.remediation_hypotheses],
        }


@dataclass(frozen=True, slots=True)
class StrategyMissAttributionReport:
    """Complete deterministic report.  It is intentionally research-only."""

    schema_version: str
    date_cutoff: str | None
    input_row_count: int
    included_row_count: int
    excluded_after_cutoff_count: int
    rows: tuple[StrategyMissAttributionRow, ...]
    summaries: tuple[StrategyMissAttributionSummary, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    policy_changes: tuple[str, ...] = ()
    point_in_time_limitations: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "date_cutoff": self.date_cutoff,
            "input_row_count": self.input_row_count,
            "included_row_count": self.included_row_count,
            "excluded_after_cutoff_count": self.excluded_after_cutoff_count,
            "research_only": self.research_only,
            "promotion_eligible": self.promotion_eligible,
            "policy_changes": list(self.policy_changes),
            "point_in_time_limitations": list(self.point_in_time_limitations),
            "rows": [row.to_dict() for row in self.rows],
            "summaries": [summary.to_dict() for summary in self.summaries],
        }


def attribute_strategy_misses(
    rows: Iterable[Mapping[str, Any] | Any],
    *,
    date_cutoff: str | None = None,
    paper_ops_rows: Iterable[Mapping[str, Any] | Any] | None = None,
) -> StrategyMissAttributionReport:
    """Attribute retained portfolio/per-strategy rows without inventing truth.

    ``rows`` may be dictionaries, ``PerformanceRow`` instances, or any object
    exposing ``to_dict()``.  Benchmark rows are used only as an explicit,
    same-cohort/date benchmark join when a no-trade row lacks its own benchmark
    value.  Conflicting benchmark values remain unknown.
    """

    normalized = [_mapping(row) for row in rows]
    point_in_time_limitations: tuple[str, ...] = ()
    if paper_ops_rows is not None:
        blotter = [
            _blotter_mapping(row)
            for row in paper_ops_rows
            if str(_mapping(row).get("mode") or "forward").lower() == "forward"
            and str(_mapping(row).get("series_role") or "").strip().lower()
            in {"champion", "challenger"}
        ]
        blotter_series = {
            (
                str(item.get("strategy_id") or ""),
                str(item.get("strategy_version") or ""),
                str(item.get("series_role") or "").lower(),
            )
            for item in blotter
            if str(item.get("lifecycle_status") or "").lower()
            in {"closed", "open", "pending", "blocked", "quarantined"}
        }
        blotter_pairs = {(strategy, version) for strategy, version, _role in blotter_series}
        # Portfolio aggregates are not a second outcome source once an exact
        # forward PaperOps lifecycle series exists.  Matching by date is
        # incorrect: an aggregate's signal date and a blotter close date are
        # different immutable events, and a date join leaves duplicate closes.
        # Benchmarks, replay, and unrelated historical cohorts remain intact.
        normalized = [
            item
            for item in normalized
            if not _superseded_forward_aggregate(item, blotter_series, blotter_pairs)
        ]
        normalized.extend(blotter)
        point_in_time_limitations = _PAPER_OPS_POINT_IN_TIME_LIMITATIONS
    cutoff = date_cutoff or _derived_cutoff(normalized)
    included = [row for row in normalized if _within_cutoff(row, cutoff)]
    excluded = len(normalized) - len(included)
    benchmarks, benchmark_hashes, benchmark_conflicts = _benchmark_index(included)

    attributed: list[StrategyMissAttributionRow] = []
    seen_lifecycles: set[tuple[str, str, str]] = set()
    for row in included:
        # A portfolio observation can contain both closed and open positions.
        # Expand only explicit child lifecycle evidence; never allocate an
        # aggregate return across an unknown number of trades.
        lifecycle_rows = _expand_lifecycle_rows(row, date_cutoff=cutoff)
        for lifecycle_row in lifecycle_rows:
            if lifecycle_row.get("_lifecycle_child") or str(
                lifecycle_row.get("record_type") or ""
            ) == "paper_ops_blotter_lifecycle":
                lifecycle_key = (
                    _cohort(lifecycle_row),
                    str(lifecycle_row.get("series_role") or "").lower(),
                    str(lifecycle_row.get("strategy_id") or "unknown"),
                    str(lifecycle_row.get("strategy_version") or ""),
                    str(
                        lifecycle_row.get("lifecycle_id")
                        or lifecycle_row.get("record_id")
                        or ""
                    ),
                )
                if lifecycle_key in seen_lifecycles:
                    continue
                seen_lifecycles.add(lifecycle_key)
            attributed.append(
                _attribute_row(
                    lifecycle_row,
                    benchmarks=benchmarks,
                    benchmark_hashes=benchmark_hashes,
                    benchmark_conflicts=benchmark_conflicts,
                )
            )
    attributed.sort(key=_row_sort_key)
    summaries = _build_summaries(attributed, cutoff)
    return StrategyMissAttributionReport(
        schema_version="dawnstrike.strategy_miss_attribution.v2",
        date_cutoff=cutoff,
        input_row_count=len(normalized),
        included_row_count=len(included),
        excluded_after_cutoff_count=excluded,
        rows=tuple(attributed),
        summaries=tuple(summaries),
        point_in_time_limitations=point_in_time_limitations,
    )


def summarize_strategy_misses(
    rows: Iterable[Mapping[str, Any] | Any],
    *,
    date_cutoff: str | None = None,
) -> tuple[StrategyMissAttributionSummary, ...]:
    """Convenience facade returning only deterministic per-strategy summaries."""

    return attribute_strategy_misses(rows, date_cutoff=date_cutoff).summaries


def load_portfolio_performance_rows_readonly(
    database_path: str | Path,
    *,
    date_cutoff: str | None = None,
) -> tuple[dict[str, Any], ...]:
    """Load retained performance rows through a query-only SQLite connection."""

    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"strategy-learning database is missing: {path}")
    connection = sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        if date_cutoff is None:
            cursor = connection.execute(
                "SELECT * FROM portfolio_performance_rows ORDER BY market_date, record_id"
            )
        else:
            cursor = connection.execute(
                "SELECT * FROM portfolio_performance_rows "
                "WHERE market_date <= ? ORDER BY market_date, record_id",
                (date_cutoff,),
            )
        return tuple(dict(row) for row in cursor.fetchall())
    finally:
        connection.close()


def _mapping(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    if isinstance(row, Mapping):
        result = dict(row)
    elif hasattr(row, "to_dict"):
        result = dict(row.to_dict())
    else:
        result = {
            name: getattr(row, name)
            for name in getattr(row, "__dataclass_fields__", {})
            if hasattr(row, name)
        }
    payload = result.get("payload_json")
    if isinstance(payload, str) and payload.strip():
        try:
            embedded = json.loads(payload)
        except (TypeError, ValueError):
            embedded = {}
        if isinstance(embedded, Mapping):
            result["_payload"] = dict(embedded)
    elif isinstance(payload, Mapping):
        result["_payload"] = dict(payload)
    return result


def _blotter_mapping(row: Mapping[str, Any] | Any) -> dict[str, Any]:
    """Project one read-only PaperOps blotter row into attribution truth."""

    item = _mapping(row)
    status = str(item.get("lifecycle_status") or "").strip().lower()
    role = str(item.get("series_role") or "").strip().lower()
    signal_date = str(item.get("signal_date") or item.get("market_date") or "")[:10]
    terminal_time = _first_timestamp(
        item,
        ("close_time", "closed_at", "exit_time", "exit_timestamp"),
    )
    terminal_date = terminal_time[:10] if terminal_time else ""
    # A closed lifecycle is dated by its immutable terminal event, never by
    # the signal/entry day.  This is what makes an Aug-21 point-in-time run
    # exclude a close first observed on Aug-25.
    item["market_date"] = terminal_date if status == "closed" and terminal_date else signal_date
    item["_terminal_event_date"] = terminal_date or None
    entry_time = _first_timestamp(item, ("fill_time", "opened_at", "signal_time"))
    item["_entry_event_date"] = (entry_time[:10] if entry_time else signal_date) or None
    if status == "closed" and not terminal_date:
        item["_point_in_time_unreconstructable"] = True
    item["ticker"] = str(item.get("symbol") or item.get("ticker") or "").upper()
    item["record_id"] = str(
        item.get("record_id") or item.get("position_id") or item.get("order_id") or ""
    )
    item["record_type"] = "paper_ops_blotter_lifecycle"
    # The materializer currently exposes series_role rather than a cohort
    # column.  Preserve a supplied cohort; otherwise derive the governed
    # champion/challenger cohort without collapsing them into one bucket.
    if role == "champion":
        item["cohort"] = str(item.get("cohort") or "official_forward_paper")
    elif role == "challenger":
        item["cohort"] = str(item.get("cohort") or "shadow_challenger")
    else:
        item["cohort"] = str(item.get("cohort") or "unknown")
    item["series_role"] = role or None
    item["record_status"] = {
        "closed": "closed",
        "open": "open_mtm",
        "pending": "missing_outcome",
        "blocked": "no_trade",
    }.get(status, "missing_outcome")
    if status == "closed":
        item["return_pct"] = safe_float(item.get("trade_return_pct"))
    else:
        item["return_pct"] = None
    item["open_position_count"] = 1 if status == "open" else 0
    item["trade_count"] = 1 if status == "closed" else 0
    item["lifecycle_id"] = str(
        item.get("close_id") or item.get("position_id") or item.get("order_id") or item["record_id"]
    )
    if not item["record_id"]:
        item["record_id"] = item["lifecycle_id"]
    item["episode_id"] = str(item.get("episode_id") or "") or None
    # BLOTTER_FIELDS has no committed FillTruth field.  A ledger/file hash is
    # source provenance, not a FillTruth receipt, so every materialized row is
    # explicitly provisional until a governed CommitBridge join is supplied.
    supplied_fill_hash = str(item.get("fill_truth_hash_sha256") or "").strip()
    if supplied_fill_hash:
        item["untrusted_fill_truth_hash"] = supplied_fill_hash
    item["fill_truth_hash_sha256"] = ""
    item["fill_truth_status"] = "missing_committed_fill_truth"
    item["eligibility_reason"] = (
        "missing_committed_fill_truth; provisional_only; governed_commitbridge_join_unavailable"
    )
    item["source_hash_sha256"] = str(
        item.get("ledger_source_hash_sha256")
        or item.get("source_hash_sha256")
        or item.get("close_data_snapshot_id")
        or item.get("data_snapshot_id")
        or ""
    )
    item["input_hash_sha256"] = str(item.get("fill_data_snapshot_id") or "")
    item["_lifecycle_child"] = True
    warnings = item.get("blotter_warnings")
    if isinstance(warnings, (list, tuple)) and warnings:
        item["_blotter_integrity_failed"] = True
        item["record_status"] = "quarantined"
        item["return_pct"] = None
        item["eligibility_reason"] = (
            "paper_ops_blotter_integrity_warning; attribution_quarantined"
        )
    return item


def _superseded_forward_aggregate(
    item: Mapping[str, Any],
    blotter_series: set[tuple[str, str, str]],
    blotter_pairs: set[tuple[str, str]],
) -> bool:
    """Return whether a forward aggregate is superseded by exact blotter truth."""

    if str(item.get("record_type") or "") == "paper_ops_blotter_lifecycle":
        return False
    cohort = _cohort(dict(item))
    if cohort not in _PAPER_OPS_FORWARD_COHORTS:
        return False
    strategy = str(item.get("strategy_id") or "")
    version = str(item.get("strategy_version") or "")
    if not strategy or (strategy, version) not in blotter_pairs:
        return False
    status = str(item.get("record_status") or item.get("outcome_status") or "").lower()
    has_outcome = status in _CLOSED_STATES or status in _OPEN_STATES or status in {
        "missing_outcome",
        "quarantined",
    }
    has_counts = (
        (_safe_nonnegative_int(item.get("trade_count")) or 0) > 0
        or (_safe_nonnegative_int(item.get("open_position_count")) or 0) > 0
    )
    if not (has_outcome or has_counts):
        return False
    role = _aggregate_series_role(item, cohort)
    # Never suppress a different role merely because the strategy/version
    # pair exists in the blotter.  An omitted aggregate role is inferred only
    # from its governed cohort above; exact role matching keeps champion and
    # challenger populations separate.
    return (strategy, version, role) in blotter_series


def _aggregate_series_role(item: Mapping[str, Any], cohort: str) -> str:
    role = str(item.get("series_role") or "").strip().lower()
    if role in {"champion", "challenger"}:
        return role
    if cohort in {"official_forward", "official_forward_paper"}:
        return "champion"
    if cohort == "shadow_challenger":
        return "challenger"
    return ""


def _first_timestamp(item: Mapping[str, Any], fields: Iterable[str]) -> str:
    for field in fields:
        value = str(item.get(field) or "").strip()
        if value:
            return value
    return ""


def _derived_cutoff(rows: list[dict[str, Any]]) -> str | None:
    dates = [str(row.get("market_date") or "").strip() for row in rows]
    dates = [value for value in dates if value]
    return max(dates) if dates else None


def _within_cutoff(row: dict[str, Any], cutoff: str | None) -> bool:
    if cutoff is None:
        return True
    if row.get("_point_in_time_unreconstructable"):
        return False
    date = str(row.get("market_date") or "").strip()
    if not date or date > cutoff:
        return False
    entry_date = str(row.get("_entry_event_date") or "").strip()
    terminal_date = str(row.get("_terminal_event_date") or "").strip()
    # A materialized current open/pending row is usable only when its entry is
    # known by the cutoff and no terminal event is known after it.  We do not
    # back-project a future close into a historical open state.
    return not (
        (entry_date and entry_date > cutoff)
        or (terminal_date and terminal_date > cutoff)
    )


def _benchmark_index(
    rows: list[dict[str, Any]],
) -> tuple[
    dict[tuple[str, str], float | None],
    dict[tuple[str, str], tuple[str, ...]],
    set[tuple[str, str]],
]:
    values: dict[tuple[str, str], set[float]] = defaultdict(set)
    hashes: dict[tuple[str, str], set[str]] = defaultdict(set)
    for row in rows:
        if str(row.get("strategy_id") or "") != "benchmark_buy_hold_equal_weight":
            continue
        key = (_cohort(row), str(row.get("market_date") or ""))
        value = safe_float(row.get("return_pct"))
        if value is not None:
            values[key].add(value)
        hashes[key].update(_evidence_hashes(row))
    result: dict[tuple[str, str], float | None] = {}
    conflicts: set[tuple[str, str]] = set()
    for key, candidates in values.items():
        if len(candidates) == 1:
            result[key] = next(iter(candidates))
        else:
            result[key] = None
            conflicts.add(key)
    return result, {key: tuple(sorted(value)) for key, value in hashes.items()}, conflicts


_LIFECYCLE_LIST_FIELDS = (
    "trade_lifecycles",
    "lifecycle_trades",
    "trade_records",
    "closed_trades",
    "open_trades",
    "closed_positions",
    "open_positions",
    "positions",
    "trades",
)
_CLOSED_STATES = {"closed", "realized", "complete", "resolved", "filled_and_closed"}
_OPEN_STATES = {"open", "held", "unrealized", "open_mtm", "pending"}


def _expand_lifecycle_rows(
    row: dict[str, Any],
    *,
    date_cutoff: str | None,
) -> list[dict[str, Any]]:
    """Return one row per explicit lifecycle, preserving aggregate fallback.

    Older PaperOps exports contain only portfolio-level counts.  Such a mixed
    row is retained as a single ``mixed_lifecycle_unresolved`` observation; it
    is deliberately not relabeled as open or closed.  Once a source provides
    explicit child records, each child is attributed exactly once by its stable
    lifecycle/trade/position ID (or a canonical content key for legacy rows).
    """

    payload = row.get("_payload")
    payload = payload if isinstance(payload, Mapping) else {}
    source: list[Mapping[str, Any]] = []
    # Prefer the canonical combined list.  Otherwise concatenate named lists,
    # while de-duplicating by immutable lifecycle IDs below.
    combined = payload.get("trade_lifecycles") or payload.get("lifecycle_trades")
    if isinstance(combined, list):
        source = [item for item in combined if isinstance(item, Mapping)]
    else:
        for field in _LIFECYCLE_LIST_FIELDS:
            values = payload.get(field)
            if isinstance(values, list):
                source.extend(item for item in values if isinstance(item, Mapping))
            values = row.get(field)
            if isinstance(values, list):
                source.extend(item for item in values if isinstance(item, Mapping))
    if not source:
        # A nested list may be carried directly by a dataclass-like row.
        for field in _LIFECYCLE_LIST_FIELDS:
            values = row.get(field)
            if isinstance(values, list):
                source.extend(item for item in values if isinstance(item, Mapping))
    if not source:
        mixed = (
            _safe_nonnegative_int(row.get("trade_count")) or 0
        ) > 0 and (_safe_nonnegative_int(row.get("open_position_count")) or 0) > 0
        if mixed:
            unresolved = dict(row)
            unresolved["_aggregate_mixed_lifecycle"] = True
            unresolved["_payload"] = {
                **dict(payload),
                "record_status": "missing_outcome",
                "lifecycle_reason": "mixed_lifecycle_child_evidence_missing",
            }
            unresolved["record_status"] = "missing_outcome"
            unresolved["return_pct"] = None
            return [unresolved]
        return [row]

    output: list[dict[str, Any]] = []
    seen: set[str] = set()
    parent_id = str(row.get("record_id") or payload.get("record_id") or "aggregate")
    for index, child in enumerate(source):
        child_dict = dict(child)
        lifecycle_id = _lifecycle_id(child_dict, parent_id, index)
        if lifecycle_id in seen:
            continue
        seen.add(lifecycle_id)
        expanded = dict(row)
        expanded.update(child_dict)
        expanded["record_id"] = lifecycle_id
        expanded["lifecycle_id"] = lifecycle_id
        expanded["_lifecycle_child"] = True
        # Child payload is retained and includes parent source identity.  This
        # gives retries the same row identity without mutating the source.
        expanded["_payload"] = {
            **dict(payload),
            **child_dict,
            "lifecycle_id": lifecycle_id,
            "parent_record_id": parent_id,
        }
        state = _lifecycle_state(child_dict)
        if state:
            expanded["record_status"] = state
            # Parent aggregate counts must never leak into a child state.  A
            # closed child is eligible even when sibling positions remain open.
            expanded["open_position_count"] = 1 if state == "open_mtm" else 0
            expanded["trade_count"] = 1 if state == "closed" else 0
            if state == "missing_outcome":
                expanded["return_pct"] = None
        if "return_pct" not in child_dict:
            expanded["return_pct"] = None
        if expanded.get("return_pct") is None:
            for key in ("net_return_pct", "realized_return_pct", "gross_return_pct"):
                if child_dict.get(key) is not None:
                    expanded["return_pct"] = child_dict[key]
                    break
        if _child_close_after_cutoff(child_dict, date_cutoff):
            # A late close is future evidence relative to this run.  The
            # materialized source cannot safely reconstruct the historical
            # open/pending state, so quarantine it instead of back-projecting
            # a current state or learning from its future return.
            expanded["record_status"] = "quarantined"
            expanded["open_position_count"] = 0
            expanded["trade_count"] = 0
            expanded["return_pct"] = None
            expanded["_payload"]["outcome_status"] = "quarantined"
            expanded["_payload"]["outcome_reason"] = "close_after_cutoff"
        output.append(expanded)
    return output or [row]


def _lifecycle_id(child: Mapping[str, Any], parent_id: str, index: int) -> str:
    for field in ("lifecycle_id", "trade_id", "position_id", "episode_id", "record_id"):
        value = str(child.get(field) or "").strip()
        if value:
            return value
    # A content identity avoids duplicate attribution when a retry repeats a
    # legacy child without an explicit ID.
    basis = json.dumps(dict(child), sort_keys=True, separators=(",", ":"), default=str)
    digest = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:24]
    return f"lifecycle:{digest or index}"


def _lifecycle_state(child: Mapping[str, Any]) -> str:
    value = child.get("record_status")
    if value is None:
        value = child.get("status") or child.get("lifecycle_state") or child.get("outcome_status")
    state = str(getattr(value, "value", value) or "").strip().lower()
    # A fill/entry event is not a closed trade.  Without a governed committed
    # FillTruth receipt it remains unresolved, even when a retry supplied a
    # price or a stale child status says ``closed``.  A source-bar hash is
    # market-data provenance, never FillTruth.
    if child.get("fill_id") and not _has_committed_fill_truth(child):
        return "missing_outcome"
    if state in _CLOSED_STATES:
        return "closed"
    if state in _OPEN_STATES:
        return "open_mtm"
    if state in {"missing", "missing_outcome", "unresolved", "quarantined", "data_unavailable"}:
        return "missing_outcome"
    if state in {"filled", "entry_filled", "fill", "entry"}:
        return "missing_outcome"
    # Presence of a close timestamp is explicit close evidence even if older
    # exports omitted a status field.
    if any(
        str(child.get(field) or "").strip()
        for field in ("closed_at", "close_time", "exit_time", "exit_timestamp")
    ):
        return "closed"
    return state


def _has_committed_fill_truth(value: Mapping[str, Any]) -> bool:
    """Recognize only an explicitly verified FillTruth CommitBridge record."""

    status = str(value.get("fill_truth_status") or "").strip().lower()
    if status not in {"committed", "verified", "complete"}:
        return False
    fill_hash = str(
        value.get("fill_truth_hash_sha256")
        or value.get("committed_fill_truth_hash")
        or ""
    ).strip()
    if not _SHA256.fullmatch(fill_hash):
        return False
    # There is no FillTruth field in the current PaperOps blotter contract.
    # Future governed joins must carry an explicit verification marker and a
    # receipt object; a bare hash/status pair is intentionally insufficient.
    receipt = value.get("fill_truth_receipt") or value.get("fill_truth_commit_receipt")
    if not isinstance(receipt, Mapping):
        return False
    contract = str(
        receipt.get("contract")
        or receipt.get("schema")
        or receipt.get("schema_version")
        or ""
    ).lower()
    return (
        bool(value.get("fill_truth_contract_verified") is True)
        and "filltruth" in contract.replace("_", "")
        and bool(
            receipt.get("committed") is True
            or receipt.get("status") in {"committed", "verified"}
        )
    )


def _child_close_after_cutoff(child: Mapping[str, Any], cutoff: str | None) -> bool:
    if not cutoff:
        return False
    for field in (
        "closed_at",
        "close_time",
        "exit_time",
        "exit_timestamp",
        "close_date",
        "exit_date",
        "trade_date",
        "market_date",
    ):
        value = str(child.get(field) or "").strip()
        if value and value[:10] > cutoff:
            return True
    return False


def _attribute_row(
    row: dict[str, Any],
    *,
    benchmarks: dict[tuple[str, str], float | None],
    benchmark_hashes: dict[tuple[str, str], tuple[str, ...]],
    benchmark_conflicts: set[tuple[str, str]],
) -> StrategyMissAttributionRow:
    raw_payload = row.get("_payload")
    payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
    strategy_id = str(row.get("strategy_id") or payload.get("strategy_id") or "unknown")
    strategy_version = _optional_text(row, payload, "strategy_version")
    config_identity = _first_text(row, payload, _CONFIG_FIELDS)
    execution_policy_version = _optional_text(row, payload, "execution_policy_version")
    cohort = _cohort(row)
    market_date = _optional_text(row, payload, "market_date")
    record_id = _optional_text(row, payload, "record_id")
    status = _status(row, payload)
    return_pct = safe_float(row.get("return_pct"))
    benchmark = safe_float(row.get("benchmark_return_pct"))
    benchmark_key = (cohort, market_date or "")
    benchmark_conflict = benchmark_key in benchmark_conflicts
    if benchmark is None and not benchmark_conflict:
        benchmark = benchmarks.get(benchmark_key)
    hashes = set(_evidence_hashes(row))
    if benchmark is not None:
        hashes.update(benchmark_hashes.get(benchmark_key, ()))
    open_count = _safe_nonnegative_int(row.get("open_position_count"))
    no_trade = status in {"no_trade", "not_triggered", "not_selected", "cash"}
    open_mtm = status in {"unrealized", "open", "open_mtm"} or bool(open_count and open_count > 0)
    if "open_position_count" not in row and row.get("unrealized_pnl_cents") is not None:
        open_mtm = True
    missing = status in {
        "missing",
        "missing_outcome",
        "terminal_missing",
        "pending",
        "quarantined",
        "data_unavailable",
    }
    explicit_conflict = _truthy(row, payload, ("conflicting_outcome", "outcome_conflict"))
    fill_truth_status = _optional_text(row, payload, "fill_truth_status")
    fill_evidence_present = any(
        _optional_text(row, payload, field)
        for field in ("fill_id", "fill_price", "quantity_filled")
    )
    provisional_fill = fill_evidence_present and not _has_committed_fill_truth(
        {**dict(payload), **row}
    )
    if fill_truth_status and not _has_committed_fill_truth({**dict(payload), **row}):
        provisional_fill = True
    eligibility_reason = _optional_text(row, payload, "eligibility_reason")
    conflict = explicit_conflict or benchmark_conflict
    if no_trade and (return_pct is not None and return_pct != 0.0):
        conflict = True
    if missing and return_pct is not None:
        conflict = True
    if status in {"unrealized", "open", "open_mtm"} and open_count == 0:
        conflict = True

    reasons, explicit_categories = _reason_evidence(row, payload)
    categories = set(explicit_categories)
    if conflict:
        state = AttributionState.CONFLICTING_OUTCOME
        eligibility = Eligibility.INELIGIBLE
        classification = "conflicting_outcome"
        categories.update(("conflicting_outcome", "data_unavailable"))
    elif no_trade:
        state = AttributionState.NO_TRADE
        if benchmark is not None and benchmark > 0:
            classification = "positive_benchmark_no_trade"
            categories.update(("no_trade", "opportunity_cost"))
            # A positive comparator day quantifies opportunity cost, but it is
            # not a causal counterfactual return for the strategy's rejected or
            # absent setup. Keep it outside closed-outcome learning until an
            # eligible path replay exists.
            eligibility = Eligibility.UNKNOWN
        else:
            classification = "no_trade"
            categories.add("no_trade")
            eligibility = Eligibility.UNKNOWN if benchmark is None else Eligibility.ELIGIBLE
    elif missing:
        state = AttributionState.MISSING_OUTCOME
        eligibility = Eligibility.INELIGIBLE
        classification = "data_unavailable"
        categories.update(("missing_outcome", "data_unavailable"))
    elif open_mtm:
        state = AttributionState.OPEN_MTM
        eligibility = Eligibility.UNKNOWN
        if return_pct is None:
            classification = "open_mtm_unknown"
        elif return_pct < 0:
            classification = "open_mtm_loss"
        elif return_pct > 0:
            classification = "open_mtm_gain"
        else:
            classification = "open_mtm_flat"
        categories.add("open_mtm")
    elif status in {"realized", "closed", "complete", "resolved"} and return_pct is not None:
        state = AttributionState.CLOSED
        eligibility = Eligibility.INELIGIBLE if provisional_fill else Eligibility.ELIGIBLE
        if provisional_fill:
            classification = "closed_provisional"
            categories.update(("closed_provisional", "missing_fill_truth", "data_unavailable"))
        elif return_pct < 0:
            classification = "false_positive"
            categories.update(("closed_loss", "false_positive"))
        elif return_pct > 0:
            classification = "closed_win"
            categories.add("closed_win")
        else:
            classification = "closed_flat"
            categories.add("closed_flat")
    elif return_pct is None:
        state = AttributionState.MISSING_OUTCOME
        eligibility = Eligibility.INELIGIBLE
        classification = "data_unavailable"
        categories.update(("missing_outcome", "data_unavailable"))
    else:
        state = AttributionState.UNKNOWN
        eligibility = Eligibility.UNKNOWN
        classification = "unknown"
        categories.add("unknown")

    # A row can carry explicit gate evidence in addition to its primary state.
    if not categories:
        categories.add("unknown")
    return StrategyMissAttributionRow(
        record_id=record_id,
        market_date=market_date,
        cohort=cohort,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        config_identity=config_identity,
        execution_policy_version=execution_policy_version,
        state=state,
        eligibility=eligibility,
        classification=classification,
        categories=tuple(sorted(categories)),
        reasons=tuple(sorted(set(reasons))),
        return_pct=return_pct,
        benchmark_return_pct=benchmark,
        evidence_hashes=tuple(sorted(hashes)),
        lifecycle_id=_optional_text(row, payload, "lifecycle_id"),
        episode_id=_optional_text(row, payload, "episode_id"),
        fill_truth_status=fill_truth_status,
        eligibility_reason=eligibility_reason,
        series_role=_optional_text(row, payload, "series_role"),
        record_type=_optional_text(row, payload, "record_type"),
    )


def _build_summaries(
    rows: list[StrategyMissAttributionRow],
    cutoff: str | None,
) -> list[StrategyMissAttributionSummary]:
    grouped: dict[
        tuple[str, str | None, str | None, str | None, str],
        list[StrategyMissAttributionRow],
    ] = defaultdict(list)
    for row in rows:
        grouped[
            (
                row.strategy_id,
                row.strategy_version,
                row.config_identity,
                row.execution_policy_version,
                row.cohort,
            )
        ].append(row)
    summaries: list[StrategyMissAttributionSummary] = []
    for key, group in sorted(
        grouped.items(),
        key=lambda item: tuple(str(value or "") for value in item[0]),
    ):
        strategy_id, version, config, execution_policy, cohort = key
        classifications = Counter(row.classification for row in group)
        categories = Counter(category for row in group for category in row.categories)
        closed_all = [
            row
            for row in group
            if row.state is AttributionState.CLOSED and row.return_pct is not None
        ]
        # A closed lifecycle count is retained for reconciliation, but only
        # committed/eligible closes contribute to outcome metrics.  Provisional
        # closes (notably the current blotter's missing FillTruth) must never
        # influence return sums, win/loss counts, or learner headlines.
        closed = [row for row in closed_all if row.eligibility is Eligibility.ELIGIBLE]
        open_rows = [
            row
            for row in group
            if row.state is AttributionState.OPEN_MTM and row.return_pct is not None
        ]
        opportunity = [
            row
            for row in group
            if "opportunity_cost" in row.categories and row.benchmark_return_pct is not None
        ]
        closed_returns = [row.return_pct for row in closed if row.return_pct is not None]
        open_returns = [row.return_pct for row in open_rows if row.return_pct is not None]
        benchmark_returns = [
            row.benchmark_return_pct for row in opportunity if row.benchmark_return_pct is not None
        ]
        remediation = _remediation_hypotheses(classifications, categories)
        summaries.append(
            StrategyMissAttributionSummary(
                strategy_id=strategy_id,
                strategy_version=version,
                config_identity=config,
                execution_policy_version=execution_policy,
                cohort=cohort,
                date_cutoff=cutoff,
                row_count=len(group),
                eligible_count=sum(row.eligibility is Eligibility.ELIGIBLE for row in group),
                ineligible_count=sum(row.eligibility is Eligibility.INELIGIBLE for row in group),
                unknown_eligibility_count=sum(
                    row.eligibility is Eligibility.UNKNOWN for row in group
                ),
                closed_count=sum(row.state is AttributionState.CLOSED for row in group),
                open_mtm_count=sum(row.state is AttributionState.OPEN_MTM for row in group),
                no_trade_count=sum(row.state is AttributionState.NO_TRADE for row in group),
                missing_outcome_count=sum(
                    row.state is AttributionState.MISSING_OUTCOME for row in group
                ),
                conflicting_outcome_count=sum(
                    row.state is AttributionState.CONFLICTING_OUTCOME for row in group
                ),
                closed_win_count=sum(row.classification == "closed_win" for row in closed),
                closed_loss_count=sum(row.classification == "false_positive" for row in closed),
                closed_flat_count=sum(row.classification == "closed_flat" for row in closed),
                closed_return_sum_pct=_sum_or_none(closed_returns),
                open_mtm_return_sum_pct=_sum_or_none(open_returns),
                opportunity_cost_count=len(opportunity),
                opportunity_cost_return_sum_pct=_sum_or_none(benchmark_returns),
                classification_counts=tuple(sorted(classifications.items())),
                category_counts=tuple(sorted(categories.items())),
                evidence_hashes=tuple(
                    sorted({item for row in group for item in row.evidence_hashes})
                ),
                remediation_hypotheses=remediation,
                provisional_closed_count=len(closed_all) - len(closed),
            )
        )
    return summaries


def _remediation_hypotheses(
    classifications: Counter[str],
    categories: Counter[str],
) -> tuple[dict[str, Any], ...]:
    rules = (
        (
            "opportunity_cost",
            "opportunity_cost",
            "Capture and review the explicit non-entry gate before changing strategy logic.",
        ),
        (
            "open_mtm",
            "outcome_lifecycle",
            "Separate open mark-to-market observations from closed outcomes before learning.",
        ),
        (
            "data_unavailable",
            "data_quality",
            "Repair missing, quarantined, or conflicting outcome evidence before retraining.",
        ),
        (
            "false_positive",
            "false_positive",
            "Review closed losing entries by regime, risk, and entry timing; "
            "do not infer a universal rule from one loss.",
        ),
        (
            "risk",
            "risk",
            "Review risk-veto and stop/target sizing evidence; preserve vetoes "
            "until an out-of-sample test passes.",
        ),
        (
            "rank",
            "rank",
            "Review rank threshold and cross-sectional capacity evidence before "
            "changing selection.",
        ),
        (
            "capacity",
            "capacity",
            "Review capacity and allocation rejection evidence; do not treat "
            "rejected rows as zero return.",
        ),
        (
            "entry_not_triggered",
            "entry_not_triggered",
            "Review trigger distance and confirmation timing; retain "
            "non-triggered status as distinct from loss.",
        ),
    )
    output: list[dict[str, Any]] = []
    for trigger, hypothesis_id, action in rules:
        count = categories.get(trigger, 0)
        if count:
            output.append(
                {"hypothesis_id": hypothesis_id, "trigger_count": count, "action": action}
            )
    if not output:
        output.append(
            {
                "hypothesis_id": "unknown_evidence",
                "trigger_count": classifications.get("unknown", 0),
                "action": (
                    "Collect explicit gate, rank, risk, capacity, and outcome evidence; "
                    "preserve unknowns."
                ),
            }
        )
    return tuple(output)


def _reason_evidence(
    row: dict[str, Any], payload: Mapping[str, Any]
) -> tuple[tuple[str, ...], set[str]]:
    values: list[str] = []
    categories: set[str] = set()
    for field in _TEXT_FIELDS:
        value = row.get(field, payload.get(field))
        if value is None or value == "":
            continue
        text = str(value).strip()
        if text:
            values.append(f"{field}:{text}")
            categories.update(_category_tokens(text))
    bool_fields = {
        "risk": ("risk_veto", "risk_rejected", "risk_failure"),
        "rank": ("rank_rejected", "rank_failure"),
        "capacity": ("capacity_rejected", "capacity_failure"),
        "entry_not_triggered": ("entry_not_triggered", "not_triggered"),
        "data_unavailable": ("data_unavailable", "missing_data", "source_unavailable"),
        "false_positive": ("false_positive",),
        "profitable_miss": ("profitable_miss",),
    }
    for category, fields in bool_fields.items():
        if _truthy(row, payload, fields):
            categories.add(category)
            values.append(f"{category}:explicit")
    return tuple(values), categories


def _category_tokens(value: str) -> set[str]:
    text = re.sub(r"[^a-z0-9_]+", " ", value.lower())
    result: set[str] = set()
    terms = {
        "risk": ("risk", "stop", "veto", "drawdown"),
        "rank": ("rank", "score", "top_n", "top n"),
        "capacity": ("capacity", "allocation", "position limit"),
        "entry_not_triggered": (
            "not triggered",
            "entry not triggered",
            "never entered",
            "no trigger",
        ),
        "data_unavailable": (
            "missing",
            "unavailable",
            "no data",
            "coverage gap",
            "quarantine",
            "terminal missing",
        ),
        "false_positive": ("false positive", "stop first", "loss", "losing"),
        "profitable_miss": ("profitable miss", "opportunity cost", "missed"),
    }
    for category, needles in terms.items():
        if any(needle in text for needle in needles):
            result.add(category)
    return result


def _status(row: dict[str, Any], payload: Mapping[str, Any]) -> str:
    value = row.get("record_status")
    if value is None:
        value = payload.get("record_status") or payload.get("outcome_status")
    return str(getattr(value, "value", value) or "").strip().lower()


def _cohort(row: dict[str, Any]) -> str:
    value = row.get("cohort") or row.get("evidence_cohort")
    text = str(getattr(value, "value", value) or "").strip().lower()
    known = {
        "official_telegram",
        "official_forward",
        "official_forward_paper",
        "research",
        "alphaops",
        "alphaops_research",
        "alphaops_signal_research",
        "algorithm_selected",
        "backtest",
        "historical_backtest",
        "shadow",
        "shadow_challenger",
    }
    if text not in known:
        return text or "unknown"
    try:
        return normalize_cohort(value, default=Cohort.ALPHAOPS_SIGNAL_RESEARCH).value
    except (AttributeError, TypeError, ValueError):
        return str(value or "unknown").strip().lower() or "unknown"


def _optional_text(row: Mapping[str, Any], payload: Mapping[str, Any], field: str) -> str | None:
    value = row.get(field)
    if value is None:
        value = payload.get(field)
    text = str(value).strip() if value is not None else ""
    return text or None


def _first_text(
    row: Mapping[str, Any], payload: Mapping[str, Any], fields: Iterable[str]
) -> str | None:
    for field in fields:
        result = _optional_text(row, payload, field)
        if result:
            return result
    return None


def _evidence_hashes(row: Mapping[str, Any]) -> tuple[str, ...]:
    raw_payload = row.get("_payload")
    payload: Mapping[str, Any] = raw_payload if isinstance(raw_payload, Mapping) else {}
    values = {str(row.get(field) or payload.get(field)).strip() for field in _HASH_FIELDS}
    return tuple(sorted(value for value in values if value and value != "None"))


def _truthy(row: Mapping[str, Any], payload: Mapping[str, Any], fields: Iterable[str]) -> bool:
    for field in fields:
        value = row.get(field)
        if value is None:
            value = payload.get(field)
        if isinstance(value, str):
            if value.strip().lower() in {"1", "true", "yes", "y", "on"}:
                return True
        elif bool(value):
            return True
    return False


def _safe_nonnegative_int(value: Any) -> int | None:
    try:
        parsed = int(value) if value is not None else None
    except (TypeError, ValueError):
        return None
    return parsed if parsed is not None and parsed >= 0 else None


def _sum_or_none(values: Iterable[float | None]) -> float | None:
    values = list(values)
    if not values:
        return None
    return round(sum(value for value in values if value is not None), 10)


def _row_sort_key(row: StrategyMissAttributionRow) -> tuple[str, str, str, str, str]:
    return (
        row.cohort,
        row.strategy_id,
        row.market_date or "",
        row.record_id or "",
        row.classification,
    )


__all__ = [
    "AttributionState",
    "Eligibility",
    "StrategyMissAttributionReport",
    "StrategyMissAttributionRow",
    "StrategyMissAttributionSummary",
    "attribute_strategy_misses",
    "from_portfolio_rows",
    "load_portfolio_performance_rows_readonly",
    "summarize_strategy_misses",
]

# Explicit alias for callers that already use the retained-row vocabulary.
from_portfolio_rows = attribute_strategy_misses
