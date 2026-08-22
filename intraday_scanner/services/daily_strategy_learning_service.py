"""Deterministic, research-only daily strategy-learning orchestration.

This module deliberately stops at evidence inventory and unapplied challenger
proposals.  A miss-attribution implementation can be supplied through the
``StrategyEvidenceAnalyzer`` protocol without changing this safety boundary.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any, Protocol

from intraday_scanner.v2.strategies import StrategySpec, build_strategy_catalog
from intraday_scanner.v2.strategies.catalog import describe_strategy

DAILY_LEARNING_SCHEMA = "dawnstrike.strategy_learning_daily.v1"
PROPOSAL_SCHEMA = "dawnstrike.strategy_remediation_proposals.v1"
_UNRESOLVED_STATUSES = frozenset(
    {
        "MISSING",
        "UNRESOLVED",
        "PENDING",
        "TERMINAL_MISSING",
        "RECONCILIATION_PENDING",
        "CENSORED_UNRESOLVED",
    }
)


class StrategyEvidenceAnalyzer(Protocol):
    """Injection boundary for the causal backtest/miss module."""

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DailyLearningContext:
    market_date: str
    cutoff: str
    source_identity: str
    code_sha: str
    source_hash_sha256: str

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.market_date)
        except ValueError as exc:
            raise ValueError("market_date must be an ISO date (YYYY-MM-DD)") from exc
        if not self.source_identity.strip():
            raise ValueError("source_identity is required to freeze the evidence boundary")
        if not self.code_sha.strip():
            raise ValueError("code_sha is required to freeze code identity")
        try:
            cutoff = datetime.fromisoformat(self.cutoff.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("cutoff must be an ISO datetime") from exc
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must include a timezone")
        if len(self.source_hash_sha256) != 64:
            raise ValueError("source_hash_sha256 must be a SHA-256 hex digest")


class EmptyEvidenceAnalyzer:
    """Safe default until the causal miss-attribution module is connected."""

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]:
        del strategy, context
        return {"status": "NO_ANALYSIS", "outcomes": [], "misses": [], "proposals": []}


class MappingEvidenceAnalyzer:
    """Adapter for a JSON mapping keyed by strategy ID, useful for CLI/replay tests."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]:
        del context
        value = self._payload.get(strategy.strategy_id, self._payload.get("default", {}))
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"evidence for {strategy.strategy_id} must be an object")
        return value


class AttributionReportAnalyzer:
    """Adapt deterministic strategy-attribution output into the daily loop.

    Only closed rows enter the outcome list. Open marks, no-trades, missing
    truth, and conflicts remain miss/evidence records and cannot become return
    labels. Remediation hypotheses remain unapplied research proposals.
    """

    def __init__(self, report: Any) -> None:
        payload = report.to_dict() if hasattr(report, "to_dict") else report
        if not isinstance(payload, Mapping):
            raise ValueError("attribution report must be an object")
        rows = payload.get("rows", ())
        summaries = payload.get("summaries", ())
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("attribution report rows must be a list")
        if not isinstance(summaries, Sequence) or isinstance(summaries, (str, bytes)):
            raise ValueError("attribution report summaries must be a list")
        self._schema = str(payload.get("schema_version") or "unknown_attribution_contract")
        self._rows = tuple(dict(row) for row in rows if isinstance(row, Mapping))
        self._summaries = tuple(
            dict(summary) for summary in summaries if isinstance(summary, Mapping)
        )

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]:
        del context
        rows = tuple(
            row
            for row in self._rows
            if row.get("strategy_id") == strategy.strategy_id
            and row.get("strategy_version") in {None, "", strategy.version}
        )
        summaries = tuple(
            summary
            for summary in self._summaries
            if summary.get("strategy_id") == strategy.strategy_id
            and summary.get("strategy_version") in {None, "", strategy.version}
        )
        outcomes = [
            {
                **row,
                "status": "RESOLVED",
            }
            for row in rows
            if str(row.get("state")) == "closed"
        ]
        misses = [
            dict(row)
            for row in rows
            if str(row.get("classification")) not in {"closed_win", "closed_flat"}
        ]
        proposals: list[dict[str, Any]] = []
        if strategy.status not in {"benchmark", "baseline"}:
            grouped: dict[str, dict[str, Any]] = {}
            for summary in summaries:
                eligibility = summary.get("eligibility")
                eligible_count = (
                    int(eligibility.get("eligible_count") or 0)
                    if isinstance(eligibility, Mapping)
                    else 0
                )
                hypotheses = summary.get("remediation_hypotheses", ())
                if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, (str, bytes)):
                    continue
                for hypothesis in hypotheses:
                    if not isinstance(hypothesis, Mapping):
                        continue
                    root_cause = str(hypothesis.get("hypothesis_id") or "unknown_evidence")
                    current = grouped.setdefault(
                        root_cause,
                        {
                            "root_cause_category": root_cause,
                            "supporting_miss_count": 0,
                            "eligible_sample_count": 0,
                            "hypothesis": str(hypothesis.get("action") or "Collect evidence."),
                            "controlled_change": {
                                "scope": "research_challenger_only",
                                "component": root_cause,
                            },
                            "evidence_cohorts": [],
                            "evidence_hashes": [],
                        },
                    )
                    current["supporting_miss_count"] += int(hypothesis.get("trigger_count") or 0)
                    current["eligible_sample_count"] += eligible_count
                    cohort = summary.get("cohort")
                    if cohort and cohort not in current["evidence_cohorts"]:
                        current["evidence_cohorts"].append(cohort)
                    for evidence_hash in summary.get("evidence_hashes", ()):
                        if evidence_hash not in current["evidence_hashes"]:
                            current["evidence_hashes"].append(evidence_hash)
            proposals = [grouped[key] for key in sorted(grouped)]
        return {
            "status": "ATTRIBUTED" if rows else "NO_RETAINED_ROWS",
            "evidence_contract": self._schema,
            "outcomes": outcomes,
            "misses": misses,
            "proposals": proposals,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _write_json_idempotent(path: Path, payload: Mapping[str, Any]) -> bool:
    encoded = _canonical_json(payload) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != encoded:
            raise ValueError(f"immutable daily-learning artifact changed: {path}")
        return True
    path.write_text(encoded, encoding="utf-8")
    return False


def _as_sequence(value: Any, field: str, strategy_id: str) -> Sequence[Mapping[str, Any]]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} for {strategy_id} must be a list")
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}[{index}] for {strategy_id} must be an object")
        rows.append(item)
    return rows


def _date_is_after(value: Any, market_date: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return date.fromisoformat(value[:10]) > date.fromisoformat(market_date)
    except ValueError:
        return False


def _normalize_analysis(
    strategy: StrategySpec,
    context: DailyLearningContext,
    raw: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcomes: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    excluded_unresolved = 0
    excluded_future = 0
    missing_return = 0

    for row in _as_sequence(raw.get("outcomes"), "outcomes", strategy.strategy_id):
        status = str(row.get("status", "")).upper()
        if status in _UNRESOLVED_STATUSES:
            excluded_unresolved += 1
            continue
        if _date_is_after(row.get("market_date"), context.market_date):
            excluded_future += 1
            continue
        normalized = dict(row)
        normalized.pop("synthetic_return", None)
        if "return_pct" not in normalized and "net_return_pct" not in normalized:
            missing_return += 1
        outcomes.append(normalized)

    for row in _as_sequence(raw.get("misses"), "misses", strategy.strategy_id):
        if _date_is_after(row.get("market_date"), context.market_date):
            excluded_future += 1
            continue
        misses.append(dict(row))

    proposals: list[dict[str, Any]] = []
    for raw_proposal in _as_sequence(
        raw.get("proposals", raw.get("remediation_proposals")),
        "proposals",
        strategy.strategy_id,
    ):
        proposal = dict(raw_proposal)
        proposal["strategy_id"] = strategy.strategy_id
        proposal["strategy_version"] = strategy.version
        proposal["status"] = "PROPOSED_NOT_APPLIED"
        proposal["applied"] = False
        proposal["automatic_policy_change"] = False
        proposal["automatic_promotion"] = False
        proposal["research_only"] = True
        proposal["broker_execution_enabled"] = False
        proposal["missing_outcomes_are_zero"] = False
        proposal.pop("proposal_id", None)
        proposal["proposal_id"] = "rem-" + _sha256(proposal)[:24]
        proposals.append(proposal)

    evidence = {
        "status": str(raw.get("status", "ANALYZED")),
        "outcomes": outcomes,
        "misses": misses,
        "counts": {
            "outcomes_retained": len(outcomes),
            "misses_retained": len(misses),
            "proposals_retained": len(proposals),
            "unresolved_outcomes_excluded": excluded_unresolved,
            "future_evidence_excluded": excluded_future,
            "outcomes_without_return_excluded_from_return_metrics": missing_return,
        },
        "evidence_contract": str(raw.get("evidence_contract", "injected_unattributed_v1")),
    }
    return evidence, proposals


def run_daily_strategy_learning(
    *,
    market_date: str,
    cutoff: str,
    source_identity: str,
    code_sha: str,
    out_dir: str | Path,
    source_hash_sha256: str | None = None,
    analyzer: StrategyEvidenceAnalyzer | None = None,
    decision_receipts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inventory the catalog and write one immutable research-only daily run."""

    source_hash = source_hash_sha256 or hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
    context = DailyLearningContext(
        market_date=market_date,
        cutoff=cutoff,
        source_identity=source_identity,
        code_sha=code_sha,
        source_hash_sha256=source_hash,
    )
    analyzer = analyzer or EmptyEvidenceAnalyzer()
    strategies = sorted(build_strategy_catalog(), key=lambda item: (item.strategy_id, item.version))
    inventory: list[dict[str, Any]] = []
    strategy_evidence: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    for strategy in strategies:
        descriptor = describe_strategy(strategy)
        descriptor["strategy_version"] = strategy.version
        descriptor["strategy_definition_hash_sha256"] = _sha256(descriptor)
        inventory.append(descriptor)
        raw = analyzer.analyze(strategy, context)
        if not isinstance(raw, Mapping):
            raise ValueError(f"analyzer result for {strategy.strategy_id} must be an object")
        evidence, strategy_proposals = _normalize_analysis(strategy, context, raw)
        strategy_evidence.append(
            {
                "strategy_id": strategy.strategy_id,
                "strategy_version": strategy.version,
                "evidence": evidence,
            }
        )
        proposals.extend(strategy_proposals)

    receipt_learning = _aggregate_decision_receipts(decision_receipts or ())

    immutable_identity = {
        "schema_version": DAILY_LEARNING_SCHEMA,
        "market_date": context.market_date,
        "cutoff": context.cutoff,
        "source_identity": context.source_identity,
        "source_hash_sha256": context.source_hash_sha256,
        "code_sha": context.code_sha,
        "catalog": [
            {
                "strategy_id": item["strategy_id"],
                "version": item["version"],
                "strategy_definition_hash_sha256": item["strategy_definition_hash_sha256"],
            }
            for item in inventory
        ],
        "evidence_hash_sha256": _sha256(strategy_evidence),
        "decision_receipt_hash_sha256": _sha256(receipt_learning),
    }
    run_id = "dslearn-" + _sha256(immutable_identity)[:24]
    root = Path(out_dir) / context.market_date
    proposal_payload = {
        "schema_version": PROPOSAL_SCHEMA,
        "run_id": run_id,
        "market_date": context.market_date,
        "cutoff": context.cutoff,
        "proposals": proposals,
        "research_only": True,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }
    proposal_payload["artifact_sha256"] = _sha256(proposal_payload)
    receipt = {
        **immutable_identity,
        "run_id": run_id,
        "strategy_count": len(inventory),
        "catalog": inventory,
        "strategy_evidence": strategy_evidence,
        "decision_receipt_learning": receipt_learning,
        "proposal_count": len(proposals),
        "artifacts": {
            "remediation_proposals": str(root / "remediation_proposals.json"),
        },
        "research_only": True,
        "daily_fit_performed": False,
        "challenger_evaluation_performed": False,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "champion_mutated": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
        "same_day_unresolved_excluded": True,
        "artifact_contract": "immutable_hash_bound_receipt_v1",
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    receipt_path = root / "daily_learning_receipt.json"
    proposal_path = root / "remediation_proposals.json"
    reused_receipt = _write_json_idempotent(receipt_path, receipt)
    reused_proposals = _write_json_idempotent(proposal_path, proposal_payload)
    return {
        "status": "complete",
        "run_id": run_id,
        "market_date": context.market_date,
        "strategy_count": len(inventory),
        "proposal_count": len(proposals),
        "receipt_path": str(receipt_path),
        "proposals_path": str(proposal_path),
        "idempotent_reused": reused_receipt and reused_proposals,
        "research_only": True,
        "daily_fit_performed": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "decision_receipt_learning": receipt_learning,
    }


__all__ = [
    "DAILY_LEARNING_SCHEMA",
    "PROPOSAL_SCHEMA",
    "DailyLearningContext",
    "EmptyEvidenceAnalyzer",
    "AttributionReportAnalyzer",
    "MappingEvidenceAnalyzer",
    "StrategyEvidenceAnalyzer",
    "run_daily_strategy_learning",
]


def _aggregate_decision_receipts(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize receipt evidence without changing any policy automatically.

    Outcome labels are accepted only when an upstream source explicitly supplies
    them. Missing, open, or conflicting outcomes stay visible and never become
    a zero-return label.
    """

    by_condition: dict[tuple[str, str, str, str, bool, bool, str], dict[str, Any]] = {}
    by_strategy: dict[tuple[str, str], dict[str, Any]] = {}
    tier_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    resolved_gaps: dict[tuple[str, str, str], dict[str, Any]] = {}
    disclosed_gap_outcomes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    winner_exclusions: dict[tuple[str, str, str], dict[str, Any]] = {}
    authoritative_contradictions: dict[tuple[str, str, str], dict[str, Any]] = {}
    blocking_counts: dict[tuple[str, str, str], int] = {}

    valid_receipt_count = 0
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        valid_receipt_count += 1
        strategy_id = str(receipt.get("strategy_id") or "UNKNOWN")
        strategy_version = str(receipt.get("strategy_version") or "UNKNOWN")
        tier = str(receipt.get("pick_tier") or "UNKNOWN")
        research_eligible = bool(receipt.get("research_pick_eligible"))
        paper_eligible = bool(receipt.get("paper_entry_eligible"))
        outcome_state = _receipt_outcome_state(receipt)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        outcome_counts[outcome_state] = outcome_counts.get(outcome_state, 0) + 1

        strategy_key = (strategy_id, strategy_version)
        strategy_row = by_strategy.setdefault(
            strategy_key,
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "receipt_count": 0,
                "tier_counts": {},
                "outcome_state_counts": {},
                "research_pick_eligible_count": 0,
                "paper_entry_eligible_count": 0,
            },
        )
        strategy_row["receipt_count"] += 1
        strategy_row["tier_counts"][tier] = strategy_row["tier_counts"].get(tier, 0) + 1
        strategy_row["outcome_state_counts"][outcome_state] = (
            strategy_row["outcome_state_counts"].get(outcome_state, 0) + 1
        )
        strategy_row["research_pick_eligible_count"] += int(research_eligible)
        strategy_row["paper_entry_eligible_count"] += int(paper_eligible)

        blocking_ids = {
            str(item)
            for item in receipt.get("all_blocking_failures") or ()
            if str(item).strip()
        }
        disclosed_ids = {
            str(item)
            for item in receipt.get("disclosed_gaps") or ()
            if str(item).strip()
        }
        for condition_id in blocking_ids:
            blocking_key = (strategy_id, strategy_version, condition_id)
            blocking_counts[blocking_key] = blocking_counts.get(blocking_key, 0) + 1

        condition_results = receipt.get("condition_results") or ()
        if not isinstance(condition_results, Sequence) or isinstance(
            condition_results, (str, bytes)
        ):
            condition_results = ()
        for raw in condition_results:
            if not isinstance(raw, Mapping):
                continue
            condition_id = str(raw.get("condition_id") or "").strip()
            if not condition_id:
                continue
            status = str(raw.get("status") or "UNKNOWN")
            key = (
                strategy_id,
                strategy_version,
                condition_id,
                status,
                research_eligible,
                paper_eligible,
                outcome_state,
            )
            row = by_condition.setdefault(
                key,
                {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "condition_id": condition_id,
                    "condition_status": status,
                    "pick_tier": tier,
                    "research_pick_eligible": research_eligible,
                    "paper_entry_eligible": paper_eligible,
                    "outcome_state": outcome_state,
                    "receipt_count": 0,
                    "blocking_candidate_count": 0,
                    "disclosed_gap_count": 0,
                    "ai_resolved_count": 0,
                },
            )
            row["receipt_count"] += 1
            row["blocking_candidate_count"] += int(condition_id in blocking_ids)
            row["disclosed_gap_count"] += int(condition_id in disclosed_ids)
            is_ai_resolved = status == "RESOLVED_FROM_SOURCE" and str(
                raw.get("resolver_id") or ""
            ) not in {"", "deterministic"}
            row["ai_resolved_count"] += int(is_ai_resolved)

            if is_ai_resolved:
                resolved_key = (strategy_id, strategy_version, condition_id)
                resolved_row = resolved_gaps.setdefault(
                    resolved_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "resolved_count": 0,
                    },
                )
                resolved_row["resolved_count"] += 1

            if condition_id in disclosed_ids and outcome_state in {"WIN", "LOSS"}:
                gap_key = (strategy_id, strategy_version, condition_id, outcome_state)
                gap_row = disclosed_gap_outcomes.setdefault(
                    gap_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "outcome_state": outcome_state,
                        "count": 0,
                    },
                )
                gap_row["count"] += 1

            if outcome_state == "WIN" and condition_id in blocking_ids:
                winner_key = (strategy_id, strategy_version, condition_id)
                winner_row = winner_exclusions.setdefault(
                    winner_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "eventual_winner_count": 0,
                    },
                )
                winner_row["eventual_winner_count"] += 1

            if raw.get("ai_claim_contradicted") is True or raw.get(
                "contradicted_by_authoritative_source"
            ) is True:
                contradiction_key = (strategy_id, strategy_version, condition_id)
                contradiction_row = authoritative_contradictions.setdefault(
                    contradiction_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "authoritative_contradiction_count": 0,
                    },
                )
                contradiction_row["authoritative_contradiction_count"] += 1

        for raw in receipt.get("contradicted_claims") or ():
            if not isinstance(raw, Mapping):
                continue
            condition_id = str(raw.get("condition_id") or "").strip()
            if not condition_id or raw.get("authoritative") is not True:
                continue
            contradiction_key = (strategy_id, strategy_version, condition_id)
            contradiction_row = authoritative_contradictions.setdefault(
                contradiction_key,
                {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "condition_id": condition_id,
                    "authoritative_contradiction_count": 0,
                },
            )
            contradiction_row["authoritative_contradiction_count"] += 1

    blocking_rows = [
        {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "condition_id": condition_id,
            "blocking_candidate_count": count,
        }
        for (strategy_id, strategy_version, condition_id), count in blocking_counts.items()
    ]
    blocking_rows.sort(
        key=lambda row: (
            -int(row["blocking_candidate_count"]),
            str(row["strategy_id"]),
            str(row["strategy_version"]),
            str(row["condition_id"]),
        )
    )
    legacy_conditions: dict[str, dict[str, Any]] = {}
    for observation in by_condition.values():
        condition_id = str(observation["condition_id"])
        summary = legacy_conditions.setdefault(
            condition_id,
            {"condition_id": condition_id, "status_counts": {}, "receipt_count": 0},
        )
        status = str(observation["condition_status"])
        summary["status_counts"][status] = (
            summary["status_counts"].get(status, 0) + int(observation["receipt_count"])
        )
        summary["receipt_count"] += int(observation["receipt_count"])
    return {
        "receipt_count": valid_receipt_count,
        "tier_counts": tier_counts,
        "outcome_state_counts": outcome_counts,
        "strategies": [by_strategy[key] for key in sorted(by_strategy)],
        "conditions": [legacy_conditions[key] for key in sorted(legacy_conditions)],
        "condition_observations": [by_condition[key] for key in sorted(by_condition)],
        "conditions_most_frequently_blocking": blocking_rows,
        "ai_resolvable_gaps_successfully_resolved": [
            resolved_gaps[key] for key in sorted(resolved_gaps)
        ],
        "disclosed_gap_outcomes": [
            disclosed_gap_outcomes[key] for key in sorted(disclosed_gap_outcomes)
        ],
        "conditions_that_excluded_eventual_winners": [
            winner_exclusions[key] for key in sorted(winner_exclusions)
        ],
        "ai_claims_later_contradicted": [
            authoritative_contradictions[key]
            for key in sorted(authoritative_contradictions)
        ],
        "research_only": True,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }


def _receipt_outcome_state(receipt: Mapping[str, Any]) -> str:
    raw = receipt.get("outcome_state")
    if raw is None:
        raw = receipt.get("outcome_status")
    if raw is None:
        raw = receipt.get("outcome")
    if isinstance(raw, Mapping):
        raw = raw.get("state") or raw.get("status") or raw.get("classification")
    value = str(raw or "").strip().upper()
    if value in {"WIN", "WON", "CLOSED_WIN", "PROFIT", "PROFITABLE"}:
        return "WIN"
    if value in {"LOSS", "LOST", "CLOSED_LOSS", "LOSSING", "UNPROFITABLE"}:
        return "LOSS"
    if value in {"FLAT", "CLOSED_FLAT", "BREAKEVEN", "BREAK_EVEN"}:
        return "FLAT"
    if value in {"OPEN", "PENDING", "UNRESOLVED", "MISSING", "UNKNOWN", ""}:
        return "MISSING_OUTCOME"
    return value
