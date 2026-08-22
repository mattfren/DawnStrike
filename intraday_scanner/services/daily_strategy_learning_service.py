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
        context: "DailyLearningContext",
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
    }


__all__ = [
    "DAILY_LEARNING_SCHEMA",
    "PROPOSAL_SCHEMA",
    "DailyLearningContext",
    "EmptyEvidenceAnalyzer",
    "MappingEvidenceAnalyzer",
    "StrategyEvidenceAnalyzer",
    "run_daily_strategy_learning",
]
