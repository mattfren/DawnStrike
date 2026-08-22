"""Deterministic construction and persistence boundary for strategy receipts."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

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
        strategy_id = str(candidate.get("strategy_id") or "").strip()
        if not strategy_id:
            raise ValueError("candidate strategy_id is required")
        specs = registry_for_strategy(strategy_id)
        now = decision_at or datetime.now(UTC).replace(microsecond=0).isoformat()
        symbol = str(candidate.get("symbol") or candidate.get("ticker") or "").upper().strip()
        market_date = str(candidate.get("market_date") or now[:10])[:10]
        overrides = dict(candidate.get("condition_results") or {})
        overrides.update(dict(condition_overrides or {}))
        results = tuple(
            self._result_for(
                candidate,
                spec.condition_id,
                getattr(spec.category, "value", str(spec.category)),
                overrides,
            )
            for spec in specs
        )
        score = _number(candidate.get("score"), _number(candidate.get("alpha_score"), 0.0)) or 0.0
        entry = _number(
            candidate.get("entry_reference")
            or candidate.get("breakout_trigger")
            or candidate.get("premarket_price")
        )
        stop = _number(
            candidate.get("stop")
            or candidate.get("invalidation_level")
            or candidate.get("invalidation")
        )
        target = _number(
            candidate.get("target") or candidate.get("first_target") or candidate.get("target_1")
        )
        rr = _number(candidate.get("reward_risk_ratio"))
        if (
            rr is None
            and entry is not None
            and stop is not None
            and target is not None
            and entry != stop
        ):
            rr = abs(target - entry) / abs(entry - stop)
        policy = evaluate_policy(
            strategy_id,
            results,
            reward_risk_ratio=rr,
            base_score=score,
            score_threshold=self.score_threshold,
        )
        input_payload = {
            key: value
            for key, value in candidate.items()
            if key not in {"receipt_id", "receipt_hash_sha256"}
        }
        input_hash = hashlib.sha256(canonical_json(input_payload).encode("utf-8")).hexdigest()
        return StrategyDecisionReceipt(
            schema_version="dawnstrike.strategy_decision_receipt.v1",
            receipt_id="",
            strategy_id=strategy_id,
            strategy_version=str(candidate.get("strategy_version") or "unversioned"),
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
            base_strategy_score=score,
            score_adjustment=0.0,
            final_score=score,
            entry_reference=entry,
            stop=stop,
            target=target,
            reward_risk_ratio=rr,
            source_identity=self.source_identity,
            input_hash_sha256=input_hash,
            research_only=research_only,
            broker_execution_enabled=False,
        )

    def evaluate_candidates(
        self,
        candidates: list[Mapping[str, Any]],
        *,
        condition_overrides: Mapping[str, Mapping[str, Any]] | None = None,
        decision_at: str | None = None,
    ) -> list[StrategyDecisionReceipt]:
        overrides = condition_overrides or {}
        return [
            self.build_receipt(
                row,
                condition_overrides=overrides.get(
                    str(row.get("symbol") or row.get("ticker") or "").upper()
                ),
                decision_at=decision_at,
            )
            for row in candidates
        ]

    @staticmethod
    def _result_for(
        candidate: Mapping[str, Any], condition_id: str, category: str, overrides: Mapping[str, Any]
    ) -> ConditionResult:
        raw = overrides.get(condition_id, candidate.get(condition_id))
        if isinstance(raw, ConditionResult):
            return raw
        if isinstance(raw, Mapping):
            payload = dict(raw)
            payload.setdefault("condition_id", condition_id)
            return ConditionResult(**payload)
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
                {condition_id: gates[condition_id]}, condition_id, category, {}
            )
        return ConditionResult(
            condition_id,
            ConditionStatus.MISSING_DISCLOSED,
            reason=f"{category} evidence not supplied",
        )


def _number(value: Any, default: float | None = None) -> float | None:
    if value in {None, ""}:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number


__all__ = ["StrategyDecisionService"]
