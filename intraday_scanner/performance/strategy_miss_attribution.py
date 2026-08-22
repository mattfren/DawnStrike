"""Deterministic, research-only attribution for strategy misses.

This module consumes already-retained performance rows.  It does not query
sources, write state, change a strategy policy, promote a model, or execute a
broker order.  In particular, an open mark-to-market row is never treated as a
closed outcome and missing truth is never converted to zero.
"""

from __future__ import annotations

import json
import re
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import Enum
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
            "rows": [row.to_dict() for row in self.rows],
            "summaries": [summary.to_dict() for summary in self.summaries],
        }


def attribute_strategy_misses(
    rows: Iterable[Mapping[str, Any] | Any],
    *,
    date_cutoff: str | None = None,
) -> StrategyMissAttributionReport:
    """Attribute retained portfolio/per-strategy rows without inventing truth.

    ``rows`` may be dictionaries, ``PerformanceRow`` instances, or any object
    exposing ``to_dict()``.  Benchmark rows are used only as an explicit,
    same-cohort/date benchmark join when a no-trade row lacks its own benchmark
    value.  Conflicting benchmark values remain unknown.
    """

    normalized = [_mapping(row) for row in rows]
    cutoff = date_cutoff or _derived_cutoff(normalized)
    included = [row for row in normalized if _within_cutoff(row, cutoff)]
    excluded = len(normalized) - len(included)
    benchmarks, benchmark_hashes, benchmark_conflicts = _benchmark_index(included)

    attributed: list[StrategyMissAttributionRow] = []
    for row in included:
        attributed.append(
            _attribute_row(
                row,
                benchmarks=benchmarks,
                benchmark_hashes=benchmark_hashes,
                benchmark_conflicts=benchmark_conflicts,
            )
        )
    attributed.sort(key=_row_sort_key)
    summaries = _build_summaries(attributed, cutoff)
    return StrategyMissAttributionReport(
        schema_version="dawnstrike.strategy_miss_attribution.v1",
        date_cutoff=cutoff,
        input_row_count=len(normalized),
        included_row_count=len(included),
        excluded_after_cutoff_count=excluded,
        rows=tuple(attributed),
        summaries=tuple(summaries),
    )


def summarize_strategy_misses(
    rows: Iterable[Mapping[str, Any] | Any],
    *,
    date_cutoff: str | None = None,
) -> tuple[StrategyMissAttributionSummary, ...]:
    """Convenience facade returning only deterministic per-strategy summaries."""

    return attribute_strategy_misses(rows, date_cutoff=date_cutoff).summaries


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


def _derived_cutoff(rows: list[dict[str, Any]]) -> str | None:
    dates = [str(row.get("market_date") or "").strip() for row in rows]
    dates = [value for value in dates if value]
    return max(dates) if dates else None


def _within_cutoff(row: dict[str, Any], cutoff: str | None) -> bool:
    if cutoff is None:
        return True
    date = str(row.get("market_date") or "").strip()
    return bool(date) and date <= cutoff


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


def _attribute_row(
    row: dict[str, Any],
    *,
    benchmarks: dict[tuple[str, str], float | None],
    benchmark_hashes: dict[tuple[str, str], tuple[str, ...]],
    benchmark_conflicts: set[tuple[str, str]],
) -> StrategyMissAttributionRow:
    payload = row.get("_payload") if isinstance(row.get("_payload"), Mapping) else {}
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
            classification = "profitable_miss"
            categories.update(("no_trade", "opportunity_cost", "profitable_miss"))
            eligibility = Eligibility.ELIGIBLE
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
        eligibility = Eligibility.ELIGIBLE
        if return_pct < 0:
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
    for key, group in sorted(grouped.items()):
        strategy_id, version, config, execution_policy, cohort = key
        classifications = Counter(row.classification for row in group)
        categories = Counter(category for row in group for category in row.categories)
        closed = [
            row
            for row in group
            if row.state is AttributionState.CLOSED and row.return_pct is not None
        ]
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
                closed_win_count=classifications.get("closed_win", 0),
                closed_loss_count=classifications.get("false_positive", 0),
                closed_flat_count=classifications.get("closed_flat", 0),
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
    payload = row.get("_payload") if isinstance(row.get("_payload"), Mapping) else {}
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


def _sum_or_none(values: list[float | None]) -> float | None:
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
    "summarize_strategy_misses",
]

# Explicit alias for callers that already use the retained-row vocabulary.
from_portfolio_rows = attribute_strategy_misses
