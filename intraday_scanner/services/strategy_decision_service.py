"""Deterministic construction and persistence boundary for strategy receipts."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from typing import Any

from intraday_scanner.alpha.plan_constructor import (
    COMPLETE,
    construct_alphaops_v5_plan,
    is_valid_alphaops_v5_plan,
)
from intraday_scanner.alpha.v5_policy import (
    DEFAULT_V5_POLICY,
    modeled_alphaops_v5_plan_metrics,
)
from intraday_scanner.decisioning.condition_registry import registry_for_strategy
from intraday_scanner.decisioning.contracts import (
    ConditionResult,
    ConditionStatus,
    StrategyDecisionReceipt,
    canonical_json,
)
from intraday_scanner.decisioning.policy import evaluate_policy

_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")


class StrategyDecisionService:
    """Build receipts without allowing contextual research to become market data."""

    def __init__(
        self,
        *,
        code_sha: str,
        source_identity: str,
        policy_version: str = "strategy-decision-policy-v1",
        score_threshold: float = 0.0,
    ) -> None:
        if not code_sha.strip() or not source_identity.strip():
            raise ValueError("code_sha and source_identity are required")
        self.code_sha = code_sha
        self.source_identity = source_identity
        self.policy_version = policy_version
        self.score_threshold = score_threshold

    def build_receipt(
        self,
        candidate: Mapping[str, Any],
        *,
        condition_overrides: Mapping[str, Any] | None = None,
        decision_at: str | None = None,
        research_only: bool = True,
    ) -> StrategyDecisionReceipt:
        if not research_only:
            raise ValueError("strategy decision receipts must remain research-only")
        strategy_id = str(candidate.get("strategy_id") or "").strip()
        if not strategy_id:
            raise ValueError("candidate strategy_id is required")
        specs = registry_for_strategy(strategy_id)
        # AlphaOps v5 levels must be frozen and source-bound before any
        # receipt condition (including reward/risk) is evaluated.  Keep this
        # in the receipt boundary as a second line of defence for callers that
        # do not use the normal alpha cycle.
        candidate_payload = dict(candidate)
        if strategy_id == "alphaops_v5":
            plan = construct_alphaops_v5_plan(candidate_payload, decision_at=decision_at)
            plan_integrity = True
            declared_plan = candidate.get("alphaops_market_structure_plan")
            expected_contract = plan.to_dict()
            plan_integrity = is_valid_alphaops_v5_plan(expected_contract)
            if "alphaops_market_structure_plan" in candidate:
                declared_hash = (
                    declared_plan.get("plan_hash_sha256")
                    if isinstance(declared_plan, Mapping)
                    else None
                )
                plan_integrity = (
                    isinstance(declared_plan, Mapping)
                    and is_valid_alphaops_v5_plan(declared_plan)
                    and canonical_json(dict(declared_plan)) == canonical_json(expected_contract)
                    and declared_hash == expected_contract["plan_hash_sha256"]
                )
            if "plan_hash_sha256" in candidate:
                plan_integrity = plan_integrity and (
                    candidate.get("plan_hash_sha256") == plan.plan_hash_sha256
                )
            candidate_payload["alphaops_market_structure_plan"] = plan.to_dict()
            candidate_payload["plan_hash_sha256"] = plan.plan_hash_sha256
            candidate_payload["market_structure_plan"] = plan.status == COMPLETE and plan_integrity
            candidate_payload["entry_observation_provenance"] = (
                plan.status == COMPLETE and plan_integrity
            )
            candidate_payload["stop_observation_provenance"] = (
                plan.status == COMPLETE and plan_integrity
            )
            candidate_payload["target_observation_provenance"] = (
                plan.status == COMPLETE and plan_integrity
            )
            prior_levels = (
                _number(candidate.get("entry_reference") or candidate.get("entry_watch_level")),
                _number(candidate.get("stop") or candidate.get("invalidation_level")),
                _number(candidate.get("target") or candidate.get("target_1")),
            )
            candidate_payload["plan_levels_frozen"] = (
                plan.status == COMPLETE
                and plan_integrity
                and (
                    prior_levels == (None, None, None)
                    or prior_levels == (plan.entry, plan.stop, plan.target)
                )
            )
            if plan.status == COMPLETE:
                # The values are intentionally copied from the frozen plan;
                # no post-freeze level selection occurs in this service.
                candidate_payload["entry_reference"] = plan.entry
                candidate_payload["entry_watch_level"] = plan.entry
                candidate_payload["stop"] = plan.stop
                candidate_payload["invalidation_level"] = plan.stop
                candidate_payload["target"] = plan.target
                candidate_payload["target_1"] = plan.target
                candidate_payload["target_basis_kind"] = plan.target_basis_kind
                candidate_payload["target_derived_from_risk"] = False
        candidate = candidate_payload
        now = (
            datetime.now(UTC).replace(microsecond=0).isoformat()
            if decision_at is None
            else str(decision_at).strip()
        )
        symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
        if not _SYMBOL.fullmatch(symbol):
            raise ValueError("candidate symbol must be a valid uppercase ticker")
        market_date_value = candidate.get("market_date")
        market_date = now[:10] if market_date_value is None else str(market_date_value).strip()
        overrides = dict(candidate.get("condition_results") or {})
        overrides.update(dict(condition_overrides or {}))
        if strategy_id == "alphaops_v5":
            # These conditions are constructor-owned. Caller overrides cannot
            # turn an invalid or mismatched plan into a complete receipt.
            for condition_id in {
                "market_structure_plan",
                "entry_observation_provenance",
                "stop_observation_provenance",
                "target_observation_provenance",
                "plan_levels_frozen",
            }:
                overrides.pop(condition_id, None)
        known_condition_ids = {spec.condition_id for spec in specs}
        unknown_overrides = sorted(set(overrides) - known_condition_ids)
        if unknown_overrides:
            raise ValueError(f"condition overrides contain unknown conditions: {unknown_overrides}")
        results = tuple(
            self._result_for(
                candidate,
                strategy_id,
                spec.condition_id,
                getattr(spec.category, "value", str(spec.category)),
                overrides,
            )
            for spec in specs
        )
        base_score_value = _first_present(candidate, "base_strategy_score", "score", "alpha_score")
        base_score = _number(base_score_value)
        score_adjustment = _number(_first_present(candidate, "score_adjustment"), 0.0) or 0.0
        explicit_final_score = _number(_first_present(candidate, "final_score"))
        final_score = (
            explicit_final_score
            if explicit_final_score is not None
            else (base_score + score_adjustment if base_score is not None else 0.0)
        )
        entry = _number(
            _first_present(
                candidate,
                "entry_reference",
                "entry_trigger",
                "breakout_trigger",
                "entry_watch_level",
                "premarket_price",
                "price",
            )
        )
        stop = _number(
            _first_present(candidate, "stop", "invalidation_level", "invalidation", "exit_line")
        )
        target = _number(_first_present(candidate, "target", "first_target", "target_1"))
        rr = _number(candidate.get("reward_risk_ratio"))
        if (
            rr is None
            and entry is not None
            and stop is not None
            and target is not None
            and entry != stop
        ):
            rr = abs(target - entry) / abs(entry - stop)
        plan_hash = ""
        gross_reward_risk = rr
        after_cost_reward_risk: float | None = None
        stop_distance_pct: float | None = None
        paper_entry_blockers: list[str] = []
        paper_entry_allowed = True
        if strategy_id == "alphaops_v5":
            strict_plan = candidate.get("alphaops_market_structure_plan")
            metrics = modeled_alphaops_v5_plan_metrics(
                dict(strict_plan) if isinstance(strict_plan, Mapping) else {}
            )
            plan_hash = str(
                strict_plan.get("plan_hash_sha256") if isinstance(strict_plan, Mapping) else ""
            )
            gross_reward_risk = _number(metrics.get("gross_reward_risk"))
            after_cost_reward_risk = _number(metrics.get("actual_after_cost_reward_risk"))
            stop_distance_pct = _number(metrics.get("stop_distance_pct"))
            if (
                after_cost_reward_risk is None
                or after_cost_reward_risk + 1e-12 < DEFAULT_V5_POLICY.minimum_after_cost_reward_risk
            ):
                paper_entry_blockers.append("after_cost_reward_risk_below_policy")
            if (
                stop_distance_pct is None
                or stop_distance_pct > DEFAULT_V5_POLICY.maximum_stop_distance_pct
            ):
                paper_entry_blockers.append("stop_distance_exceeds_policy")
            paper_entry_allowed = not paper_entry_blockers
        policy = evaluate_policy(
            strategy_id,
            results,
            reward_risk_ratio=rr,
            base_score=final_score,
            score_threshold=self.score_threshold,
            paper_entry=paper_entry_allowed,
            score_available=base_score is not None,
        )
        input_payload = _json_safe(
            {
                key: value
                for key, value in candidate.items()
                if key not in {"receipt_id", "receipt_hash_sha256"}
            }
        )
        input_payload_json = canonical_json(input_payload)
        input_hash = hashlib.sha256(input_payload_json.encode("utf-8")).hexdigest()
        return StrategyDecisionReceipt(
            schema_version="dawnstrike.strategy_decision_receipt.v2",
            receipt_id="",
            strategy_id=strategy_id,
            strategy_version=str(candidate.get("strategy_version") or "unversioned").strip(),
            symbol=symbol,
            market_date=market_date,
            decision_at=now,
            code_sha=self.code_sha,
            policy_version=self.policy_version,
            condition_results=results,
            first_blocking_failure=policy["first_blocking_failure"],
            all_blocking_failures=policy["all_blocking_failures"],
            disclosed_gaps=policy["disclosed_gaps"],
            research_pick_eligible=bool(policy["research_pick_eligible"]),
            paper_entry_eligible=bool(policy["paper_entry_eligible"]),
            pick_tier=policy["pick_tier"],
            base_strategy_score=base_score if base_score is not None else 0.0,
            score_adjustment=score_adjustment,
            final_score=final_score,
            entry_reference=entry,
            stop=stop,
            target=target,
            reward_risk_ratio=rr,
            source_identity=self.source_identity,
            input_hash_sha256=input_hash,
            research_only=research_only,
            broker_execution_enabled=False,
            input_payload_json=input_payload_json,
            plan_hash_sha256=plan_hash,
            gross_reward_risk_ratio=gross_reward_risk,
            after_cost_reward_risk_ratio=after_cost_reward_risk,
            stop_distance_pct=stop_distance_pct,
            paper_entry_blockers=tuple(paper_entry_blockers),
        )

    def evaluate_candidates(
        self,
        candidates: list[Mapping[str, Any]],
        *,
        condition_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        decision_at: str | None = None,
    ) -> list[StrategyDecisionReceipt]:
        overrides = condition_overrides or {}
        receipts = [
            self.build_receipt(
                row,
                condition_overrides=overrides.get(
                    str(row.get("symbol") or row.get("ticker") or "").upper()
                ),
                decision_at=decision_at,
            )
            for row in candidates
        ]
        unknown_sector_selected = False
        adjusted: list[StrategyDecisionReceipt] = []
        for receipt in receipts:
            if (
                receipt.strategy_id == "cross_sectional_relative_strength"
                and receipt.research_pick_eligible
                and _unresolved_sector(receipt)
            ):
                if unknown_sector_selected:
                    limit_reason = "sector_industry:UNKNOWN_COHORT_LIMIT"
                    adjusted.append(
                        replace(
                            receipt,
                            first_blocking_failure=limit_reason,
                            all_blocking_failures=tuple(
                                dict.fromkeys(receipt.all_blocking_failures + (limit_reason,))
                            ),
                            research_pick_eligible=False,
                            paper_entry_eligible=False,
                            pick_tier="WATCH_ONLY",
                            receipt_id="",
                            receipt_hash_sha256="",
                        )
                    )
                    continue
                unknown_sector_selected = True
            adjusted.append(receipt)
        return adjusted

    @staticmethod
    def _result_for(
        candidate: Mapping[str, Any],
        strategy_id: str,
        condition_id: str,
        category: str,
        overrides: Mapping[str, Any],
    ) -> ConditionResult:
        raw = overrides.get(condition_id, candidate.get(condition_id))
        if isinstance(raw, ConditionResult):
            return _reject_ai_override(raw, category)
        if isinstance(raw, Mapping):
            payload = dict(raw)
            payload.setdefault("condition_id", condition_id)
            return _reject_ai_override(ConditionResult(**payload), category)
        if raw is True:
            return ConditionResult(
                condition_id,
                ConditionStatus.PASS,
                observed_value=True,
                reason="deterministic evidence passed",
            )
        if raw is False:
            return ConditionResult(
                condition_id,
                ConditionStatus.FAIL,
                observed_value=False,
                reason="deterministic evidence failed",
            )
        # Explicit legacy gate names can be carried forward without treating
        # absent evidence as a pass.
        gates = candidate.get("gates") or candidate.get("condition_values")
        if isinstance(gates, Mapping) and condition_id in gates:
            return StrategyDecisionService._result_for(
                {condition_id: gates[condition_id]}, strategy_id, condition_id, category, {}
            )
        if strategy_id == "cross_sectional_relative_strength" and condition_id == "sector_industry":
            return ConditionResult(
                condition_id,
                ConditionStatus.MISSING_DISCLOSED,
                observed_value="UNKNOWN",
                reason="sector evidence unresolved; deterministic UNKNOWN bucket",
                unresolved_unknowns=("sector_industry",),
            )
        if (
            condition_id == "valid_target_when_required"
            and _first_present(candidate, "target_required", "requires_target") is False
        ):
            return ConditionResult(
                condition_id,
                ConditionStatus.NOT_APPLICABLE,
                observed_value=False,
                reason="candidate does not require a target",
            )
        return ConditionResult(
            condition_id,
            ConditionStatus.MISSING_DISCLOSED,
            reason=f"{category} evidence not supplied",
        )


def _number(value: Any, default: float | None = None) -> float | None:
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


def _first_present(candidate: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in candidate and candidate[key] is not None and candidate[key] != "":
            return candidate[key]
    return None


def _json_safe(value: Any) -> Any:
    if isinstance(value, ConditionResult):
        return value.to_dict()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    return value


def _unresolved_sector(receipt: StrategyDecisionReceipt) -> bool:
    for result in receipt.condition_results:
        if result.condition_id != "sector_industry":
            continue
        return result.status in {
            ConditionStatus.FAIL,
            ConditionStatus.MISSING_DISCLOSED,
            ConditionStatus.STALE,
            ConditionStatus.CONFLICT,
        }
    return False


def _reject_ai_override(result: ConditionResult, category: str) -> ConditionResult:
    if result.resolver_id == "strategy_gap_resolver" and category != "AI_RESOLVABLE":
        return ConditionResult(
            result.condition_id,
            ConditionStatus.FAIL,
            observed_value=None,
            reason="AI resolver cannot override deterministic condition",
            source_urls=result.source_urls,
            source_hashes=result.source_hashes,
            observed_at=result.observed_at,
            effective_at=result.effective_at,
            resolver_id="strategy_decision_policy",
            resolution_method="ai_override_rejected",
            requested_model=result.requested_model,
            actual_model=result.actual_model,
            contradictions=result.contradictions,
            unresolved_unknowns=result.unresolved_unknowns,
        )
    return result


__all__ = ["StrategyDecisionService"]
