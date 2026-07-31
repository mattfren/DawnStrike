"""Additive shadow fleet allocator for PaperOps experiments.

The allocator never mutates individual strategy accounts. It produces a separate
counterfactual allocation record with stock/ETF cohort ranks and hard limits.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from typing import Any

from intraday_scanner.v2.paper_ops.position_management import (
    BorrowAvailability,
)
from intraday_scanner.v2.strategies import Direction


@dataclass(frozen=True, slots=True)
class FleetAllocatorPolicy:
    policy_version: str = "paperops-shadow-fleet-allocator-v1"
    max_positions: int = 6
    max_symbol_overlap: int = 1
    max_correlation_group_positions: int = 2
    preserve_individual_strategy_accounts: bool = True
    research_only: bool = True
    broker_execution_enabled: bool = False

    @property
    def fingerprint(self) -> str:
        return hashlib.sha256(
            json.dumps(
                asdict(self),
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class FleetCandidate:
    candidate_id: str
    strategy_id: str
    strategy_version: str
    symbol: str
    asset_type: str
    direction: str
    score: float
    risk_amount: float
    notional: float
    correlation_group: str
    individual_account_decision: str
    borrow: BorrowAvailability | None = None


def allocate_shadow_fleet(
    candidates: list[FleetCandidate],
    *,
    policy: FleetAllocatorPolicy | None = None,
) -> dict[str, Any]:
    policy = policy or FleetAllocatorPolicy()
    if not policy.preserve_individual_strategy_accounts:
        raise ValueError("shadow allocator must preserve individual strategy accounts")
    if policy.max_positions < 1:
        raise ValueError("max_positions must be positive")
    ranked, preblocked = rank_by_asset_cohort(candidates)
    selected: list[dict[str, Any]] = []
    blocked = list(preblocked)
    symbol_counts: dict[str, int] = {}
    correlation_counts: dict[str, int] = {}
    for row in ranked:
        candidate = row["candidate"]
        assert isinstance(candidate, FleetCandidate)
        reason = _allocation_block_reason(
            candidate,
            selected_count=len(selected),
            symbol_counts=symbol_counts,
            correlation_counts=correlation_counts,
            policy=policy,
        )
        record = {
            **asdict(candidate),
            "borrow": asdict(candidate.borrow) if candidate.borrow else None,
            "asset_cohort_rank": row["asset_cohort_rank"],
            "policy_version": policy.policy_version,
            "policy_fingerprint": policy.fingerprint,
            "individual_account_preserved": True,
        }
        if reason:
            blocked.append({**record, "fleet_decision": "BLOCKED", "reason": reason})
            continue
        selected.append({**record, "fleet_decision": "SELECTED", "reason": "within_limits"})
        symbol = candidate.symbol.upper()
        symbol_counts[symbol] = symbol_counts.get(symbol, 0) + 1
        group = candidate.correlation_group or "unknown"
        correlation_counts[group] = correlation_counts.get(group, 0) + 1
    return {
        "schema_version": "v2.paperops_shadow_fleet_allocation.v1",
        "policy": {**asdict(policy), "fingerprint": policy.fingerprint},
        "selected": selected,
        "blocked": blocked,
        "diagnostics": {
            "candidate_count": len(candidates),
            "selected_count": len(selected),
            "blocked_count": len(blocked),
            "max_position_saturation": len(selected) >= policy.max_positions,
            "duplicate_symbol_candidates": _duplicate_symbol_count(candidates),
            "selected_symbol_counts": dict(sorted(symbol_counts.items())),
            "selected_correlation_group_counts": dict(
                sorted(correlation_counts.items())
            ),
            "stock_etf_ranked_separately": True,
            "individual_strategy_accounts_mutated": False,
        },
        "research_only": True,
        "broker_execution_enabled": False,
    }


def rank_by_asset_cohort(
    candidates: list[FleetCandidate],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    cohorts: dict[str, list[FleetCandidate]] = {"stock": [], "etf": []}
    blocked: list[dict[str, Any]] = []
    for candidate in candidates:
        asset_type = candidate.asset_type.lower()
        if asset_type not in cohorts:
            blocked.append({
                **asdict(candidate),
                "borrow": asdict(candidate.borrow) if candidate.borrow else None,
                "fleet_decision": "BLOCKED",
                "reason": "unknown_asset_cohort",
                "asset_cohort_rank": None,
            })
            continue
        cohorts[asset_type].append(candidate)
    ranked_by_cohort = {
        cohort: sorted(
            rows,
            key=lambda row: (-row.score, row.symbol.upper(), row.candidate_id),
        )
        for cohort, rows in cohorts.items()
    }
    ranked: list[dict[str, Any]] = []
    longest = max((len(rows) for rows in ranked_by_cohort.values()), default=0)
    for index in range(longest):
        for cohort in ("stock", "etf"):
            rows = ranked_by_cohort[cohort]
            if index < len(rows):
                ranked.append({
                    "candidate": rows[index],
                    "asset_cohort": cohort,
                    "asset_cohort_rank": index + 1,
                })
    return ranked, blocked


def _allocation_block_reason(
    candidate: FleetCandidate,
    *,
    selected_count: int,
    symbol_counts: dict[str, int],
    correlation_counts: dict[str, int],
    policy: FleetAllocatorPolicy,
) -> str:
    if candidate.individual_account_decision != "accepted":
        return "individual_strategy_did_not_accept"
    if candidate.direction == Direction.SHORT:
        borrow = candidate.borrow
        if (
            borrow is None
            or borrow.status != "verified_available"
            or borrow.borrow_cost_bps_per_session is None
            or not borrow.located_at
            or not borrow.source_ref
        ):
            return "short_borrow_not_verified"
    if selected_count >= policy.max_positions:
        return "fleet_max_positions"
    if symbol_counts.get(candidate.symbol.upper(), 0) >= policy.max_symbol_overlap:
        return "duplicate_symbol_overlap"
    group = candidate.correlation_group or "unknown"
    if (
        correlation_counts.get(group, 0)
        >= policy.max_correlation_group_positions
    ):
        return "correlation_group_limit"
    if candidate.risk_amount <= 0 or candidate.notional <= 0:
        return "invalid_risk_or_notional"
    return ""


def _duplicate_symbol_count(candidates: list[FleetCandidate]) -> int:
    counts: dict[str, int] = {}
    for candidate in candidates:
        symbol = candidate.symbol.upper()
        counts[symbol] = counts.get(symbol, 0) + 1
    return sum(count - 1 for count in counts.values() if count > 1)


__all__ = [
    "FleetAllocatorPolicy",
    "FleetCandidate",
    "allocate_shadow_fleet",
    "rank_by_asset_cohort",
]
