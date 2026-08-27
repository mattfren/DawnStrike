"""Deterministic pick policy for strategy decision receipts."""

# ruff: noqa: E501

from __future__ import annotations

import math
from collections.abc import Iterable
from typing import Any

from intraday_scanner.decisioning.condition_registry import registry_for_strategy
from intraday_scanner.decisioning.contracts import (
    ConditionCategory,
    ConditionResult,
    ConditionStatus,
    PickTier,
)

_PASSING = {
    ConditionStatus.PASS,
    ConditionStatus.RESOLVED_FROM_SOURCE,
    ConditionStatus.NOT_APPLICABLE,
}
_BLOCKING_BAD = {
    ConditionStatus.FAIL,
    ConditionStatus.STALE,
    ConditionStatus.CONFLICT,
    ConditionStatus.MISSING_DISCLOSED,
}


def _append_unique(target: list[str], value: str) -> None:
    if value not in target:
        target.append(value)


def evaluate_policy(
    strategy_id: str,
    condition_results: Iterable[ConditionResult],
    *,
    reward_risk_ratio: float | None,
    base_score: float,
    score_threshold: float = 0.0,
    paper_entry: bool = True,
    score_available: bool = True,
) -> dict[str, Any]:
    specs = {spec.condition_id: spec for spec in registry_for_strategy(strategy_id)}
    results = tuple(condition_results)
    result_ids = [result.condition_id for result in results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("duplicate condition IDs are not allowed")
    unknown = [result.condition_id for result in results if result.condition_id not in specs]
    if unknown:
        raise ValueError(f"results contain unknown conditions: {unknown}")
    by_id = {result.condition_id: result for result in results}
    all_failures: list[str] = []
    safety_failures: list[str] = []
    data_failures: list[str] = []
    score_failures: list[str] = []
    disclosed: list[str] = []
    for spec in specs.values():
        result = by_id.get(spec.condition_id)
        if result is None:
            if spec.blocking_for_research_pick:
                _append_unique(all_failures, spec.condition_id)
                (
                    safety_failures
                    if spec.category == ConditionCategory.HARD_RISK
                    else data_failures
                ).append(spec.condition_id)
            else:
                _append_unique(disclosed, spec.condition_id)
            continue
        if (
            result.status == ConditionStatus.NOT_APPLICABLE
            and spec.category
            in {
                ConditionCategory.HARD_MARKET,
                ConditionCategory.HARD_RISK,
                ConditionCategory.STRATEGY_CORE,
            }
            and spec.condition_id != "valid_target_when_required"
        ):
            _append_unique(all_failures, spec.condition_id)
            target = (
                safety_failures if spec.category == ConditionCategory.HARD_RISK else data_failures
            )
            _append_unique(target, spec.condition_id)
            continue
        if (
            strategy_id in {"gap_up_continuation", "gap_up_continuation_atr"}
            and spec.condition_id == "corporate_action_basis"
            and result.status == ConditionStatus.FAIL
        ):
            _append_unique(all_failures, spec.condition_id)
            _append_unique(data_failures, spec.condition_id)
            continue
        if result.status in _BLOCKING_BAD:
            if spec.blocking_for_research_pick:
                _append_unique(all_failures, spec.condition_id)
                if spec.category == ConditionCategory.HARD_RISK:
                    _append_unique(safety_failures, spec.condition_id)
                else:
                    _append_unique(data_failures, spec.condition_id)
            else:
                _append_unique(disclosed, spec.condition_id)
    if (
        reward_risk_ratio is None
        or not math.isfinite(reward_risk_ratio)
        or reward_risk_ratio < 1.50
    ):
        _append_unique(all_failures, "reward_risk_at_least_1_50")
        _append_unique(safety_failures, "reward_risk_at_least_1_50")
    if not score_available:
        _append_unique(all_failures, "deterministic_score_missing")
        _append_unique(data_failures, "deterministic_score_missing")
    elif not math.isfinite(base_score) or base_score < score_threshold:
        _append_unique(all_failures, "deterministic_score_threshold")
        _append_unique(score_failures, "deterministic_score_threshold")
    research_eligible = not all_failures
    paper_blockers = [
        spec.condition_id
        for spec in specs.values()
        if spec.blocking_for_paper_entry
        and (
            by_id.get(spec.condition_id) is None or by_id[spec.condition_id].status not in _PASSING
        )
    ]
    if not paper_entry:
        _append_unique(paper_blockers, "paper_entry_policy")
    if strategy_id == "bullish_fvg_continuation":
        proxy = by_id.get("daily_ohlc_proxy")
        if proxy is not None and proxy.status != ConditionStatus.NOT_APPLICABLE:
            _append_unique(paper_blockers, "daily_ohlc_proxy")
            _append_unique(disclosed, "daily_ohlc_proxy")
    if strategy_id == "cross_sectional_relative_strength":
        sector = by_id.get("sector_industry")
        if sector is None or sector.status in {
            ConditionStatus.FAIL,
            ConditionStatus.MISSING_DISCLOSED,
            ConditionStatus.STALE,
            ConditionStatus.CONFLICT,
        }:
            _append_unique(disclosed, "sector_industry:UNKNOWN")
    paper_eligible = bool(research_eligible and paper_entry and not paper_blockers)
    if safety_failures:
        tier = PickTier.BLOCKED_SAFETY
    elif data_failures:
        tier = PickTier.BLOCKED_DATA
    elif score_failures:
        tier = PickTier.NO_EDGE
    elif not research_eligible:
        tier = PickTier.NO_EDGE
    elif paper_blockers:
        tier = PickTier.CONDITIONAL_PICK
    elif disclosed:
        tier = PickTier.PICK_WITH_DISCLOSED_GAPS
    else:
        tier = PickTier.QUALIFIED_PICK
    return {
        "research_pick_eligible": research_eligible,
        "paper_entry_eligible": paper_eligible,
        "pick_tier": tier,
        "first_blocking_failure": all_failures[0] if all_failures else None,
        "all_blocking_failures": tuple(dict.fromkeys(all_failures)),
        "disclosed_gaps": tuple(dict.fromkeys(disclosed)),
        "paper_blockers": tuple(dict.fromkeys(paper_blockers)),
    }


def condition_result_from_value(
    condition_id: str, value: Any, *, reason: str = ""
) -> ConditionResult:
    if value is True:
        return ConditionResult(
            condition_id,
            ConditionStatus.PASS,
            observed_value=True,
            reason=reason or "condition passed",
        )
    if value is False:
        return ConditionResult(
            condition_id,
            ConditionStatus.FAIL,
            observed_value=False,
            reason=reason or "condition failed",
        )
    return ConditionResult(
        condition_id,
        ConditionStatus.MISSING_DISCLOSED,
        observed_value=None,
        reason=reason or "evidence missing",
    )


__all__ = ["condition_result_from_value", "evaluate_policy"]
