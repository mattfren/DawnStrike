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
from datetime import date, datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.v6.contracts import ALPHAOPS_V6_STRATEGY_VERSION
from intraday_scanner.decisioning.contracts import canonical_json
from intraday_scanner.errors import SnapshotValidationError
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


# This is deliberately a tuple of literal statements rather than formatted
# SQL. The table set is part of the signed daily-learning source contract.
_STRATEGY_LEARNING_TABLE_GENERATION_QUERY_CONTRACT: tuple[
    tuple[str, str, str], ...
] = (
    (
        "portfolio_performance_rows",
        "SELECT count(*), max(rowid) FROM portfolio_performance_rows",
        "SELECT count(*) FROM portfolio_performance_rows",
    ),
    (
        "strategy_decision_receipts",
        "SELECT count(*), max(rowid) FROM strategy_decision_receipts",
        "SELECT count(*) FROM strategy_decision_receipts",
    ),
    (
        "alpha_v6_decisions",
        "SELECT count(*), max(rowid) FROM alpha_v6_decisions",
        "SELECT count(*) FROM alpha_v6_decisions",
    ),
    (
        "research_episode_outcome_bridges",
        "SELECT count(*), max(rowid) FROM research_episode_outcome_bridges",
        "SELECT count(*) FROM research_episode_outcome_bridges",
    ),
    (
        "signal_selections",
        "SELECT count(*), max(rowid) FROM signal_selections",
        "SELECT count(*) FROM signal_selections",
    ),
)


def _strategy_learning_table_generations(
    connection: sqlite3.Connection,
    tables: set[str],
) -> dict[str, dict[str, Any]]:
    """Read row bounds for the fixed daily-learning table allowlist."""

    table_generations: dict[str, dict[str, Any]] = {}
    for table, bounded_query, count_query in (
        _STRATEGY_LEARNING_TABLE_GENERATION_QUERY_CONTRACT
    ):
        if table not in tables:
            table_generations[table] = {
                "exists": False,
                "row_count": 0,
                "max_rowid": None,
            }
            continue
        try:
            count, max_rowid = connection.execute(bounded_query).fetchone()
        except sqlite3.DatabaseError:
            count = connection.execute(count_query).fetchone()[0]
            max_rowid = None
        table_generations[table] = {
            "exists": True,
            "row_count": int(count or 0),
            "max_rowid": int(max_rowid) if max_rowid is not None else None,
        }
    return table_generations


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
    terminal_event_at: str | None = None
    evidence_at: str | None = None

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
            "terminal_event_at": self.terminal_event_at,
            "evidence_at": self.evidence_at,
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
    seen_lifecycles: set[tuple[str, str, str, str, str]] = set()
    for row in included:
        # A portfolio observation can contain both closed and open positions.
        # Expand only explicit child lifecycle evidence; never allocate an
        # aggregate return across an unknown number of trades.
        lifecycle_rows = _expand_lifecycle_rows(row, date_cutoff=cutoff)
        for lifecycle_row in lifecycle_rows:
            if (
                lifecycle_row.get("_lifecycle_child")
                or str(lifecycle_row.get("record_type") or "") == "paper_ops_blotter_lifecycle"
            ):
                lifecycle_key = (
                    _cohort(lifecycle_row),
                    str(lifecycle_row.get("series_role") or "").lower(),
                    str(lifecycle_row.get("strategy_id") or "unknown"),
                    str(lifecycle_row.get("strategy_version") or ""),
                    str(lifecycle_row.get("lifecycle_id") or lifecycle_row.get("record_id") or ""),
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
    _connection: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...]:
    """Load retained performance rows through a query-only SQLite connection."""

    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"strategy-learning database is missing: {path}")
    owns_connection = _connection is None
    connection = _connection or sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        if date_cutoff is None:
            cursor = connection.execute(
                "SELECT * FROM portfolio_performance_rows ORDER BY market_date, record_id"
            )
        else:
            # ``date_cutoff`` may be the exact ISO timestamp supplied to the
            # daily-learning CLI.  SQLite rows are date-keyed, so load the
            # whole cutoff day and let attribution compare immutable event
            # timestamps without losing same-day rows before the cutoff.
            query_cutoff = str(date_cutoff).strip()[:10]
            cursor = connection.execute(
                "SELECT * FROM portfolio_performance_rows "
                "WHERE market_date <= ? ORDER BY market_date, record_id",
                (query_cutoff,),
            )
        loaded = [dict(row) for row in cursor.fetchall()]
        if date_cutoff is not None:
            cutoff_at = _parse_timestamp(date_cutoff)
            if cutoff_at is None:
                raise ValueError("date_cutoff must be an aware ISO datetime")
            # ``reconciled_at`` is the persisted availability boundary for
            # portfolio performance.  Rows materialized after the frozen EOD
            # cutoff must not perturb retry input hashes, even if their
            # market_date is historical.
            loaded = [
                row
                for row in loaded
                if (available := _parse_timestamp(row.get("reconciled_at"))) is not None
                and available <= cutoff_at
            ]
            evidence_timestamp_fields = (
                "terminal_event_at",
                "_terminal_event_at",
                "closed_at",
                "close_time",
                "exit_time",
                "exit_timestamp",
                "resolved_at",
                "completed_at",
                "evidence_at",
                "observed_at",
                "event_at",
                "decision_at",
                "generated_at",
                "created_at",
            )
            loaded = [
                row
                for row in loaded
                if not any(
                    (event_at := _parse_timestamp(row.get(field))) is not None
                    and event_at > cutoff_at
                    for field in evidence_timestamp_fields
                )
            ]
            # Apply the same all-alias point-in-time gate used by attribution
            # before the CLI hashes its database input.  Rows that the
            # normalizer will exclude (including malformed aliases) must not
            # become invisible hash inputs on a retry.
            loaded = [row for row in loaded if _within_cutoff(row, str(date_cutoff))]
        return tuple(loaded)
    finally:
        if owns_connection:
            connection.close()


def load_strategy_decision_receipts_readonly(
    database_path: str | Path,
    *,
    market_date: str,
    date_cutoff: str,
    _connection: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...] | None:
    """Load only hash-valid, point-in-time decision receipts without writes.

    ``None`` means the legacy database has no receipt table.  Once the table is
    present, malformed, forged, or future-dated rows are fail-closed (omitted),
    allowing the daily-learning coverage receipt to expose the resulting gap.
    """

    path = Path(database_path).resolve()
    if not path.is_file():
        raise FileNotFoundError(f"strategy-learning database is missing: {path}")
    cutoff = _parse_timestamp(date_cutoff)
    if cutoff is None:
        raise ValueError("date_cutoff must be an aware ISO datetime")
    owns_connection = _connection is None
    connection = _connection or sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='strategy_decision_receipts'"
        ).fetchone()
        if exists is None:
            return None
        rows = connection.execute(
            "SELECT receipt_id, receipt_hash_sha256, strategy_id, strategy_version, symbol, "
            "market_date, pick_tier, research_pick_eligible, paper_entry_eligible, "
            "source_identity, input_hash_sha256, canonical_json, created_at "
            "FROM strategy_decision_receipts "
            "WHERE market_date = ? ORDER BY created_at, receipt_id",
            (market_date,),
        ).fetchall()
    finally:
        if owns_connection:
            connection.close()

    # Keep loader diagnostics attached to the tuple so the service can expose
    # quarantined rows without accepting them as evidence.  The payloads are
    # private authenticated envelopes, not ordinary JSON mappings.
    class _ReceiptBatch(tuple):
        invalid_reasons: dict[str, int]
        invalid_count: int
        invalid_identities: tuple[str, ...]

        def __new__(cls, values, *, invalid_reasons):
            result = super().__new__(cls, values)
            result.invalid_reasons = dict(sorted(invalid_reasons.items()))
            result.invalid_count = sum(result.invalid_reasons.values())
            result.invalid_identities = ()
            return result

    from intraday_scanner.decisioning.contracts import ConditionResult, StrategyDecisionReceipt
    from intraday_scanner.services.daily_strategy_learning_service import _persisted_receipt

    result: list[dict[str, Any]] = []
    invalid_reasons: dict[str, int] = {}
    invalid_identities: list[str] = []
    current_identity = ""

    def reject(reason: str) -> None:
        invalid_reasons[reason] = invalid_reasons.get(reason, 0) + 1
        if current_identity:
            invalid_identities.append(current_identity)

    for row in rows:
        current_identity = hashlib.sha256(str(row["canonical_json"]).encode("utf-8")).hexdigest()
        try:
            payload = json.loads(str(row["canonical_json"]))
        except (TypeError, ValueError):
            reject("receipt_payload_not_json")
            continue
        if not isinstance(payload, dict):
            reject("receipt_payload_not_object")
            continue
        # The row hash and append-only envelope are not enough by themselves:
        # a caller with write access to SQLite could insert a sparse payload
        # and recompute both values.  Reconstruct the typed receipt contract
        # before allowing this row to become learning evidence.
        try:
            raw_conditions = payload.get("condition_results")
            if not isinstance(raw_conditions, list) or any(
                not isinstance(item, Mapping) for item in raw_conditions
            ):
                raise ValueError("condition_results must be a list of objects")
            typed_receipt = StrategyDecisionReceipt(
                **{
                    **payload,
                    "condition_results": tuple(
                        ConditionResult(**dict(item)) for item in raw_conditions
                    ),
                }
            )
            if typed_receipt.canonical_json() != str(row["canonical_json"]):
                raise ValueError("persisted receipt JSON is not canonical")
        except (TypeError, ValueError, KeyError):
            reject("receipt_schema_invalid")
            continue
        digest = str(payload.get("receipt_hash_sha256") or "")
        receipt_id = str(payload.get("receipt_id") or "")
        body = {
            key: value
            for key, value in payload.items()
            if key not in {"receipt_hash_sha256", "receipt_id"}
        }
        expected = hashlib.sha256(
            json.dumps(
                body,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        ).hexdigest()
        decision_at = _parse_timestamp(payload.get("decision_at"))
        created_at = _parse_timestamp(row["created_at"])
        if created_at is None:
            reject("persisted_created_at_missing_or_unparseable")
            continue
        if created_at > cutoff:
            reject("persisted_created_at_after_cutoff")
            continue
        if (
            not re.fullmatch(r"[0-9a-f]{64}", digest)
            or digest != expected
            or receipt_id != "sdr-" + digest[:24]
            or str(row["receipt_id"]) != receipt_id
            or str(row["receipt_hash_sha256"]) != digest
            or str(row["strategy_id"]) != str(payload.get("strategy_id") or "")
            or str(row["strategy_version"]) != str(payload.get("strategy_version") or "")
            or str(row["market_date"]) != str(payload.get("market_date") or "")
            or payload.get("market_date") != market_date
            or decision_at is None
            or decision_at > cutoff
            or payload.get("research_only") is not True
            or payload.get("broker_execution_enabled") is not False
        ):
            # Preserve one deterministic reason for common corruption while
            # the service performs the complete authenticated check as well.
            if not re.fullmatch(r"[0-9a-f]{64}", digest):
                reject("receipt_hash_missing_or_noncanonical")
            elif digest != expected:
                reject("receipt_hash_mismatch")
            elif receipt_id != "sdr-" + digest[:24]:
                reject("receipt_id_not_derived_from_hash")
            elif decision_at is None:
                reject("decision_at_missing_or_unparseable")
            elif decision_at > cutoff:
                reject("decision_after_cutoff")
            else:
                reject("receipt_envelope_or_safety_mismatch")
            continue
        # Bind the complete persisted row envelope.  Any mismatch is rejected
        # here and again at the central service ingress.
        envelope_fields = {
            field: row[field]
            for field in (
                "receipt_id",
                "receipt_hash_sha256",
                "strategy_id",
                "strategy_version",
                "symbol",
                "market_date",
                "pick_tier",
                "research_pick_eligible",
                "paper_entry_eligible",
                "source_identity",
                "input_hash_sha256",
                "created_at",
            )
        }
        mismatch = False
        for field, envelope_value in envelope_fields.items():
            if field == "created_at":
                continue
            if field not in payload:
                mismatch = True
                break
            if field in {"research_pick_eligible", "paper_entry_eligible"}:
                mismatch = (
                    not isinstance(payload[field], bool)
                    or isinstance(envelope_value, bool)
                    or envelope_value not in (0, 1)
                    or bool(envelope_value) != payload[field]
                )
            else:
                mismatch = (
                    not isinstance(envelope_value, str)
                    or not isinstance(payload[field], str)
                    or envelope_value != payload[field]
                )
            if mismatch:
                break
        if mismatch:
            reject("persisted_envelope_payload_mismatch")
            continue
        result.append(
            _persisted_receipt(
                payload,
                envelope=envelope_fields,
                schema_validated=True,
            )
        )
    batch = _ReceiptBatch(result, invalid_reasons=invalid_reasons)
    batch.invalid_identities = tuple(sorted(invalid_identities))
    return batch


def load_alpha_v6_decisions_readonly(
    database_path: str | Path,
    *,
    market_date: str,
    date_cutoff: str,
    _connection: sqlite3.Connection | None = None,
) -> tuple[dict[str, Any], ...] | None:
    """Load V6 decisions through their own governed, point-in-time lane.

    V6 is not a ``StrategyDecisionReceipt`` cohort.  Rows must pass the
    canonical V6 batch validator, match every persisted envelope column, and
    carry a non-empty ``stored_at`` at or before the frozen cutoff.
    """

    from intraday_scanner.alpha.v6.decision_ledger import validate_decision_batch
    from intraday_scanner.services.daily_strategy_learning_service import _persisted_v6_decision

    path = Path(database_path).resolve()
    cutoff = _parse_timestamp(date_cutoff)
    if not path.is_file():
        raise FileNotFoundError(f"strategy-learning database is missing: {path}")
    if cutoff is None:
        raise ValueError("date_cutoff must be an aware ISO datetime")
    owns_connection = _connection is None
    connection = _connection or sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        connection.execute("PRAGMA query_only = ON")
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='alpha_v6_decisions'"
        ).fetchone()
        if exists is None:
            return None
        columns = {
            str(item[1]) for item in connection.execute("PRAGMA table_info(alpha_v6_decisions)")
        }
        required_columns = {
            "decision_id",
            "scan_id",
            "source_signal_id",
            "shadow_signal_id",
            "market_date",
            "decision_at",
            "ticker",
            "strategy_version",
            "model_version",
            "action",
            "setup_key",
            "regime_key",
            "safety_vetoes_json",
            "input_hash_sha256",
            "source_lineage_hash_sha256",
            "stored_at",
            "payload_json",
        }
        missing_columns = sorted(required_columns - columns)
        if missing_columns:

            class _LegacyV6Batch(tuple):
                invalid_count: int
                invalid_reasons: dict[str, int]
                invalid_identities: tuple[str, ...]

                invalid_count = len(missing_columns)
                invalid_reasons = {
                    f"column_missing_{column}": 1 for column in missing_columns
                }
                invalid_identities = ()

            return _LegacyV6Batch()
        rows = connection.execute(
            "SELECT decision_id, scan_id, source_signal_id, shadow_signal_id, market_date, "
            "decision_at, ticker, strategy_version, model_version, action, setup_key, "
            "regime_key, safety_vetoes_json, input_hash_sha256, source_lineage_hash_sha256, "
            "stored_at, payload_json FROM alpha_v6_decisions WHERE market_date = ? "
            "ORDER BY decision_at, decision_id",
            (market_date,),
        ).fetchall()
    finally:
        if owns_connection:
            connection.close()
    valid: list[dict[str, Any]] = []
    envelope_by_decision: dict[str, dict[str, Any]] = {}
    invalid: list[str] = []
    all_identities: list[str] = []
    identity_by_decision: dict[str, str] = {}
    for row in rows:
        row_identity = hashlib.sha256(str(row["payload_json"]).encode("utf-8")).hexdigest()
        all_identities.append(row_identity)
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError):
            invalid.append("payload_not_json")
            continue
        if not isinstance(payload, dict):
            invalid.append("payload_not_object")
            continue
        available = _parse_timestamp(row["stored_at"])
        decision_at = _parse_timestamp(payload.get("decision_at"))
        if available is None:
            invalid.append("stored_at_missing_or_unparseable")
            continue
        if available > cutoff:
            invalid.append("stored_at_after_cutoff")
            continue
        if payload.get("market_date") != market_date:
            invalid.append("market_date_mismatch")
            continue
        if payload.get("strategy_version") != ALPHAOPS_V6_STRATEGY_VERSION:
            invalid.append("strategy_version_mismatch")
            continue
        if decision_at is None or decision_at > cutoff:
            invalid.append("decision_after_cutoff")
            continue
        envelope_fields = {
            "decision_id": row["decision_id"],
            "scan_id": row["scan_id"],
            "source_signal_id": row["source_signal_id"],
            "shadow_signal_id": row["shadow_signal_id"],
            "market_date": row["market_date"],
            "decision_at": row["decision_at"],
            "ticker": row["ticker"],
            "strategy_version": row["strategy_version"],
            "model_version": row["model_version"],
            "action": row["action"],
            "setup_key": row["setup_key"],
            "regime_key": row["regime_key"],
            "input_hash_sha256": row["input_hash_sha256"],
            "source_lineage_hash_sha256": row["source_lineage_hash_sha256"],
        }
        if any(str(value) != str(payload.get(field)) for field, value in envelope_fields.items()):
            invalid.append("persisted_envelope_payload_mismatch")
            continue
        try:
            stored_vetoes = json.loads(str(row["safety_vetoes_json"]))
        except (TypeError, ValueError):
            invalid.append("safety_vetoes_not_json")
            continue
        if stored_vetoes != payload.get("safety_vetoes"):
            invalid.append("persisted_safety_vetoes_mismatch")
            continue
        valid.append(payload)
        decision_key = str(payload.get("decision_id") or "")
        identity_by_decision[decision_key] = row_identity
        envelope_by_decision[decision_key] = {
            **envelope_fields,
            "safety_vetoes": stored_vetoes,
            "stored_at": str(row["stored_at"]),
            "payload_hash_sha256": row_identity,
        }

    class _V6Batch(tuple):
        invalid_reasons: dict[str, int]
        invalid_count: int
        invalid_identities: tuple[str, ...]

        def __new__(cls, values, *, invalid_reasons):
            result = super().__new__(cls, values)
            result.invalid_reasons = dict(sorted(Counter(invalid_reasons).items()))
            result.invalid_count = sum(result.invalid_reasons.values())
            result.invalid_identities = ()
            return result

    batch = validate_decision_batch(valid)
    if not batch["valid"]:
        invalid_ids = {str(item.get("decision_id") or "") for item in batch["invalid"]}
        invalid.extend(
            str(violation)
            for item in batch["invalid"]
            for violation in item.get("violations") or ()
        )
        valid = [item for item in valid if str(item.get("decision_id") or "") not in invalid_ids]
    accepted_identities = {
        identity_by_decision[str(item.get("decision_id") or "")]
        for item in valid
        if str(item.get("decision_id") or "") in identity_by_decision
    }

    batch_result = _V6Batch(
        [
            _persisted_v6_decision(
                item,
                envelope=envelope_by_decision.get(str(item.get("decision_id") or ""), {}),
            )
            for item in valid
        ],
        invalid_reasons=invalid,
    )
    batch_result.invalid_identities = tuple(
        sorted(identity for identity in all_identities if identity not in accepted_identities)
    )
    return batch_result


def load_strategy_learning_database_snapshot_readonly(
    database_path: str | Path,
    *,
    market_date: str,
    date_cutoff: str,
    _connection: sqlite3.Connection | None = None,
) -> dict[str, Any]:
    """Read all daily-learning SQLite lanes from one point-in-time snapshot.

    The individual loaders remain available for callers that only need one
    lane.  Daily learning uses this coordinator so portfolio rows, persisted
    V5 receipts, and V6 decisions share one SQLite read transaction.  A
    writer that inserts, backdates, or updates a row after ``BEGIN`` cannot
    enter this acquisition; the captured table bounds make that fact explicit
    in the acquisition manifest.
    """

    path = Path(database_path).resolve()
    cutoff = _parse_timestamp(date_cutoff)
    if not path.is_file():
        raise FileNotFoundError(f"strategy-learning database is missing: {path}")
    if cutoff is None:
        raise ValueError("date_cutoff must be an aware ISO datetime")
    owns_connection = _connection is None
    connection = _connection or sqlite3.connect(f"{path.as_uri()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    started_transaction = False
    try:
        connection.execute("PRAGMA query_only = ON")
        if not connection.in_transaction:
            connection.execute("BEGIN")
            started_transaction = True
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        table_generations = _strategy_learning_table_generations(connection, tables)
        rows = (
            load_portfolio_performance_rows_readonly(
                path, date_cutoff=date_cutoff, _connection=connection
            )
            if table_generations["portfolio_performance_rows"]["exists"]
            else ()
        )
        receipts = load_strategy_decision_receipts_readonly(
            path,
            market_date=market_date,
            date_cutoff=date_cutoff,
            _connection=connection,
        )
        v6_decisions = load_alpha_v6_decisions_readonly(
            path,
            market_date=market_date,
            date_cutoff=date_cutoff,
            _connection=connection,
        )
        if table_generations["research_episode_outcome_bridges"]["exists"]:
            raw_bridge_rows = connection.execute(
                """SELECT bridge_id, bridge_hash_sha256, logical_key, selection_id,
                slate_id, slate_content_hash_sha256, episode_id, ticker, market_date,
                selected_at, strategy_id, strategy_version, receipt_id,
                receipt_hash_sha256, outcome_status, learning_eligible,
                source_observation_id, source_observation_hash_sha256,
                source_path_id, source_path_hash_sha256, source_cutoff,
                outcome_artifact_id, outcome_artifact_hash_sha256,
                payload_json, created_at
                FROM research_episode_outcome_bridges
                WHERE market_date = ?
                ORDER BY selected_at ASC, bridge_id ASC""",
                (market_date[:10],),
            ).fetchall()
            from intraday_scanner.services.daily_strategy_learning_service import (
                _persisted_research_bridge,
            )
            from intraday_scanner.services.research_episode_outcome_service import (
                _validate_persisted_research_episode_outcome_bridge,
            )

            validated_bridge_values: list[Mapping[str, Any]] = []
            bridge_invalid_reasons: dict[str, int] = {}
            bridge_invalid_identities: list[str] = []
            expected_selection_count = 0
            expected_contributor_count = 0
            if "signal_selections" in tables:
                expected_selection_rows = connection.execute(
                    """SELECT payload_json FROM signal_selections
                    WHERE cohort = ? AND substr(selected_at, 1, 10) = ?
                    ORDER BY selection_id""",
                    ("research_radar", market_date[:10]),
                ).fetchall()
                expected_selection_count = len(expected_selection_rows)
                for expected_row in expected_selection_rows:
                    try:
                        expected_payload = json.loads(str(expected_row[0]))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        expected_contributor_count += 1
                        continue
                    expected_signal = (
                        expected_payload.get("signal")
                        if isinstance(expected_payload, Mapping)
                        else None
                    )
                    contributors = (
                        expected_signal.get("strategy_contributors")
                        if isinstance(expected_signal, Mapping)
                        else None
                    )
                    expected_contributor_count += (
                        len(contributors)
                        if isinstance(contributors, list) and contributors
                        else 1
                    )
            cutoff_at = cutoff
            for raw_bridge in raw_bridge_rows:
                envelope = dict(raw_bridge)
                payload_text = str(envelope.pop("payload_json"))
                identity = hashlib.sha256(payload_text.encode("utf-8")).hexdigest()
                try:
                    payload = json.loads(payload_text)
                    if not isinstance(payload, dict):
                        raise ValueError("payload is not an object")
                    _validate_persisted_research_episode_outcome_bridge(payload)
                    if payload.get("learning_eligible") is True:
                        authoritative_receipt = connection.execute(
                            """SELECT receipt_hash_sha256, strategy_id,
                            strategy_version, symbol, market_date, canonical_json
                            FROM strategy_decision_receipts WHERE receipt_id = ?""",
                            (str(payload.get("receipt_id") or ""),),
                        ).fetchone()
                        embedded_receipt = payload.get("strategy_decision_receipt")
                        if (
                            authoritative_receipt is None
                            or not isinstance(embedded_receipt, Mapping)
                            or str(authoritative_receipt[0]).lower()
                            != str(payload.get("receipt_hash_sha256") or "").lower()
                            or str(authoritative_receipt[1])
                            != str(payload.get("strategy_id") or "")
                            or str(authoritative_receipt[2])
                            != str(payload.get("strategy_version") or "")
                            or str(authoritative_receipt[3]).upper()
                            != str(payload.get("ticker") or "").upper()
                            or str(authoritative_receipt[4])[:10]
                            != str(payload.get("market_date") or "")[:10]
                            or str(authoritative_receipt[5])
                            != canonical_json(dict(embedded_receipt))
                        ):
                            raise ValueError(
                                "exact persisted strategy decision receipt is absent"
                            )
                    source_cutoff = _parse_timestamp(payload.get("source_cutoff"))
                    created_at = _parse_timestamp(payload.get("created_at"))
                    if source_cutoff is None or source_cutoff > cutoff_at:
                        raise ValueError("source cutoff exceeds learning cutoff")
                    if created_at is None or created_at > cutoff_at:
                        raise ValueError("created_at exceeds learning cutoff")
                    for field, stored in envelope.items():
                        payload_value = payload.get(field)
                        if field == "learning_eligible":
                            if (
                                isinstance(stored, bool)
                                or stored not in (0, 1)
                                or not isinstance(payload_value, bool)
                                or bool(stored) is not payload_value
                            ):
                                raise ValueError("learning_eligible column mismatch")
                        elif field in {
                            "source_observation_id",
                            "source_observation_hash_sha256",
                            "source_path_id",
                            "source_path_hash_sha256",
                            "source_cutoff",
                            "outcome_artifact_id",
                            "outcome_artifact_hash_sha256",
                        }:
                            if ("" if stored is None else str(stored)) != (
                                "" if payload_value is None else str(payload_value)
                            ):
                                raise ValueError(f"{field} column mismatch")
                        elif str(stored) != str(payload_value):
                            raise ValueError(f"{field} column mismatch")
                    validated_bridge_values.append(
                        _persisted_research_bridge(payload, envelope=envelope)
                    )
                except (TypeError, ValueError, KeyError, SnapshotValidationError) as exc:
                    reason = f"bridge_integrity_failure:{type(exc).__name__}:{exc}"
                    bridge_invalid_reasons[reason] = bridge_invalid_reasons.get(reason, 0) + 1
                    bridge_invalid_identities.append(identity)

            bridge_values: list[Mapping[str, Any]] = []
            revisions_by_logical_key: dict[str, list[Mapping[str, Any]]] = {}
            for value in validated_bridge_values:
                logical_key = str(value.get("logical_key") or "")
                logical_base = (
                    logical_key[:-3] if logical_key.endswith("-r2") else logical_key
                )
                revisions_by_logical_key.setdefault(logical_base, []).append(value)
            for logical_base, revisions in sorted(revisions_by_logical_key.items()):
                by_key = {
                    str(value.get("logical_key") or ""): value for value in revisions
                }
                base = by_key.get(logical_base)
                r2 = by_key.get(logical_base + "-r2")
                topology_valid = len(by_key) == len(revisions) and (
                    (len(revisions) == 1 and base is not None)
                    or (
                        len(revisions) == 2
                        and base is not None
                        and r2 is not None
                        and base.get("learning_eligible") is not True
                        and str(base.get("outcome_status") or "").upper()
                        in {"MISSING", "INELIGIBLE"}
                        and r2.get("learning_eligible") is True
                        and str(r2.get("source_outcome_status") or "").upper()
                        == "COMPLETE_SOURCED"
                    )
                )
                if topology_valid:
                    if r2 is not None:
                        bridge_values.append(r2)
                    elif base is not None:
                        bridge_values.append(base)
                    else:
                        raise AssertionError("valid bridge topology has no base row")
                    continue
                reason = "bridge_revision_topology_invalid"
                bridge_invalid_reasons[reason] = (
                    bridge_invalid_reasons.get(reason, 0) + 1
                )
                bridge_invalid_identities.append(
                    hashlib.sha256(logical_base.encode("utf-8")).hexdigest()
                )

            class _BridgeBatch(tuple):
                def __new__(
                    cls,
                    values,
                    *,
                    invalid_reasons,
                    invalid_identities,
                    expected_selection_count,
                    expected_contributor_count,
                ):
                    result = super().__new__(cls, values)
                    result.invalid_reasons = dict(sorted(invalid_reasons.items()))
                    result.invalid_count = sum(result.invalid_reasons.values())
                    result.invalid_identities = tuple(invalid_identities)
                    result.expected_selection_count = int(expected_selection_count)
                    result.expected_contributor_count = int(expected_contributor_count)
                    result.persisted_history_count = len(validated_bridge_values)
                    return result

            bridge_rows = _BridgeBatch(
                bridge_values,
                invalid_reasons=bridge_invalid_reasons,
                invalid_identities=bridge_invalid_identities,
                expected_selection_count=expected_selection_count,
                expected_contributor_count=expected_contributor_count,
            )
        else:
            bridge_rows = None
        generation = {
            "database_path": str(path),
            "transaction": (
                "sqlite_begin_read_mode_ro"
                if owns_connection
                else "sqlite_existing_query_only_transaction"
            ),
            "table_bounds": table_generations,
            "data_version": int(connection.execute("PRAGMA data_version").fetchone()[0]),
        }
        return {
            "portfolio_rows": rows,
            "decision_receipts": receipts,
            "v6_decisions": v6_decisions,
            "research_episode_outcomes": bridge_rows,
            "generation": generation,
        }
    finally:
        if started_transaction and not owns_connection:
            connection.rollback()
        if owns_connection:
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
    item["_terminal_event_at"] = terminal_time or None
    entry_time = _first_timestamp(item, ("fill_time", "opened_at", "signal_time"))
    item["_entry_event_date"] = (entry_time[:10] if entry_time else signal_date) or None
    item["_entry_event_at"] = entry_time or None
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
        item["eligibility_reason"] = "paper_ops_blotter_integrity_warning; attribution_quarantined"
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
    has_outcome = (
        status in _CLOSED_STATES
        or status in _OPEN_STATES
        or status
        in {
            "missing_outcome",
            "quarantined",
        }
    )
    has_counts = (_safe_nonnegative_int(item.get("trade_count")) or 0) > 0 or (
        _safe_nonnegative_int(item.get("open_position_count")) or 0
    ) > 0
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


def _parse_timestamp(value: Any) -> datetime | None:
    """Parse an aware ISO timestamp for point-in-time comparisons."""

    text = str(value or "").strip()
    if not text or "T" not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _cutoff_parts(cutoff: str | None) -> tuple[str, datetime | None]:
    text = str(cutoff or "").strip()
    if not text:
        return "", None
    return text[:10], _parse_timestamp(text)


def _event_after_cutoff(value: Any, cutoff: str | None) -> bool:
    """Return true when an event is after, or unorderable against, cutoff."""

    if not cutoff:
        return False
    cutoff_date, cutoff_at = _cutoff_parts(cutoff)
    event = str(value or "").strip()
    if not event or len(event) < 10:
        return False
    event_date = event[:10]
    if event_date > cutoff_date:
        return True
    if event_date < cutoff_date or cutoff_at is None:
        return False
    event_at = _parse_timestamp(event)
    # A same-day date-only or malformed timestamp cannot be placed before an
    # exact cutoff.  Quarantine it instead of allowing current-state evidence
    # to leak into a point-in-time run.
    return event_at is None or event_at > cutoff_at


def _within_cutoff(row: dict[str, Any], cutoff: str | None) -> bool:
    if cutoff is None:
        return True
    if row.get("_point_in_time_unreconstructable"):
        return False
    cutoff_date, cutoff_at = _cutoff_parts(cutoff)
    payload = row.get("_payload")
    payload = payload if isinstance(payload, Mapping) else {}

    def values(fields: Iterable[str]) -> tuple[str, ...]:
        return tuple(
            str(value).strip()
            for field in fields
            for value in (row.get(field), payload.get(field))
            if value not in (None, "") and str(value).strip()
        )

    date_value = str(row.get("market_date") or payload.get("market_date") or "").strip()
    try:
        date.fromisoformat(date_value)
    except ValueError:
        return False
    if date_value > cutoff_date:
        return False
    event_date_fields = (
        "_entry_event_date",
        "_terminal_event_date",
    )
    for event_date in values(event_date_fields):
        try:
            date.fromisoformat(event_date[:10])
        except ValueError:
            return False
        if event_date[:10] > cutoff_date:
            return False

    timestamp_fields = (
        "_entry_event_at",
        "fill_time",
        "opened_at",
        "signal_time",
        "_terminal_event_at",
        "close_time",
        "closed_at",
        "exit_time",
        "exit_timestamp",
        "terminal_event_at",
        "resolved_at",
        "completed_at",
        "evidence_at",
        "observed_at",
        "event_at",
        "decision_at",
        "generated_at",
        "created_at",
    )
    # Every populated alias is independently ordered.  A valid first alias
    # cannot mask a malformed or future conflicting alias later in the row.
    for event in values(timestamp_fields):
        parsed = _parse_timestamp(event)
        if parsed is None:
            return False
        if cutoff_at is not None and parsed > cutoff_at:
            return False
    return True


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
_FILL_TRUTH_EXEMPT_RECORD_TYPES = frozenset(
    {
        "account_observation",
        "account_aggregate",
        "benchmark_observation",
        "benchmark_aggregate",
        "portfolio_observation",
        "portfolio_aggregate",
    }
)


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
        mixed = (_safe_nonnegative_int(row.get("trade_count")) or 0) > 0 and (
            _safe_nonnegative_int(row.get("open_position_count")) or 0
        ) > 0
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
        entry_time = _first_timestamp(child_dict, ("fill_time", "opened_at", "signal_time"))
        terminal_time = _first_timestamp(
            child_dict,
            ("close_time", "closed_at", "exit_time", "exit_timestamp"),
        )
        if entry_time:
            expanded["_entry_event_at"] = entry_time
        if terminal_time:
            expanded["_terminal_event_at"] = terminal_time
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
    # FillTruth receipt it remains unresolved unless an immutable terminal
    # close event is present; that explicit close is still provisional and
    # ineligible at attribution time.  A source-bar hash is market-data
    # provenance, never FillTruth.
    if child.get("fill_id") and not _has_committed_fill_truth(child):
        has_terminal_event = any(
            str(child.get(field) or "").strip()
            for field in ("closed_at", "close_time", "exit_time", "exit_timestamp")
        )
        if not has_terminal_event:
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
    """Return false until a governed FillTruth join is actually available.

    A caller-supplied status, digest, or even a self-hashed receipt proves
    only that the caller serialized those values.  The current PaperOps
    materializer exposes no committed FillTruth source or resolver token, so
    accepting any mapping here would let an attacker manufacture learning
    eligibility.  Keep every raw attribution row provisional until a future
    trusted materializer supplies an authenticated join through this boundary.
    """

    del value
    return False


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
        if _event_after_cutoff(child.get(field), cutoff):
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
    record_type = _optional_text(row, payload, "record_type")
    fill_evidence_present = any(
        _optional_text(row, payload, field)
        for field in ("fill_id", "fill_price", "quantity_filled")
    )
    # Closed trade/lifecycle/official-forward rows are not learnable from a
    # return number alone.  The only schemas allowed to omit trade FillTruth
    # are explicit governed account/portfolio/benchmark aggregates.  An
    # omitted record_type is intentionally treated as a trade-like record so
    # an official row cannot smuggle an aggregate return through this gate.
    # ``record_type`` is caller-controlled data and cannot authenticate an
    # aggregate.  Until a private readonly materializer supplies an
    # authenticated aggregate envelope, every closed return (including
    # portfolio/account/benchmark-looking mappings) requires governed
    # FillTruth and remains provisional.
    aggregate_fill_exempt = False
    requires_fill_truth = (
        status in _CLOSED_STATES and return_pct is not None and not aggregate_fill_exempt
    )
    provisional_fill = (
        requires_fill_truth or fill_evidence_present or bool(fill_truth_status)
    ) and not _has_committed_fill_truth({**dict(payload), **row})
    if fill_truth_status and not _has_committed_fill_truth({**dict(payload), **row}):
        provisional_fill = True
    if provisional_fill and not fill_truth_status:
        fill_truth_status = "missing_committed_fill_truth"
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
        record_type=record_type,
        terminal_event_at=_first_text(
            row,
            payload,
            (
                "_terminal_event_at",
                "terminal_event_at",
                "closed_at",
                "close_time",
                "exit_time",
                "exit_timestamp",
            ),
        ),
        evidence_at=_first_text(
            row,
            payload,
            ("evidence_at", "observed_at", "event_at", "decision_at"),
        ),
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
    "load_strategy_decision_receipts_readonly",
    "load_portfolio_performance_rows_readonly",
    "summarize_strategy_misses",
]

# Explicit alias for callers that already use the retained-row vocabulary.
from_portfolio_rows = attribute_strategy_misses
