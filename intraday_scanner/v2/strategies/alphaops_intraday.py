"""Causal intraday adapter for the frozen AlphaOps V5 policy.

The adapter owns only event-time translation.  Eligibility remains the V5
policy, and every observation is explicitly bounded by its decision timestamp.
It creates simulated strategy signals for research; it never creates broker
orders.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
    DEFAULT_V5_POLICY,
    AlphaOpsV5Decision,
    AlphaOpsV5Policy,
    evaluate_v5_official_paper,
)
from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.strategies.models import (
    Direction,
    StrategySignal,
    StrategySpec,
)


@dataclass(frozen=True, slots=True)
class IntradayDecisionPoint:
    """One point-in-time candidate and the evidence available to it."""

    symbol: str
    decision_at: datetime
    signal: dict[str, Any]
    observation: dict[str, Any]
    artifact_identity: str
    artifact_hash_sha256: str
    exchange_session_id: str

    def __post_init__(self) -> None:
        if (
            self.decision_at.tzinfo is None
            or self.decision_at.utcoffset() != timezone.utc.utcoffset(self.decision_at)
        ):
            raise ValueError("decision_at must be timezone-aware UTC")
        if not self.artifact_identity or not self.artifact_hash_sha256:
            raise ValueError("intraday decision requires retained artifact lineage")


@dataclass(frozen=True, slots=True)
class IntradayPolicyEvaluation:
    decision: AlphaOpsV5Decision
    symbol: str
    decision_at: datetime
    artifact_identity: str
    artifact_hash_sha256: str
    exchange_session_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.decision.to_dict(),
            "symbol": self.symbol,
            "decision_at": self.decision_at.isoformat(),
            "artifact_identity": self.artifact_identity,
            "artifact_hash_sha256": self.artifact_hash_sha256,
            "exchange_session_id": self.exchange_session_id,
            "point_in_time": True,
        }


def evaluate_alphaops_intraday(
    point: IntradayDecisionPoint,
    *,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> IntradayPolicyEvaluation:
    """Evaluate V5 using only facts available at ``point.decision_at``."""

    observation = dict(point.observation)
    observation.setdefault("requested_at", point.decision_at.isoformat())
    observation.setdefault("observed_at", point.decision_at.isoformat())
    observation.setdefault("is_usable", True)
    decision = evaluate_v5_official_paper(
        point.signal,
        observation,
        decision_time=point.decision_at.isoformat(),
        policy=policy,
    )
    return IntradayPolicyEvaluation(
        decision=decision,
        symbol=point.symbol,
        decision_at=point.decision_at,
        artifact_identity=point.artifact_identity,
        artifact_hash_sha256=point.artifact_hash_sha256,
        exchange_session_id=point.exchange_session_id,
    )


def build_point_in_time_observation(
    bars: Sequence[Any],
    *,
    as_of: datetime,
    base: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an observation without allowing a future bar to leak backward."""

    accepted = [
        bar
        for bar in bars
        if (timestamp := _timestamp(bar)) is not None and timestamp <= as_of
    ]
    observation = dict(base or {})
    observation["requested_at"] = as_of.isoformat()
    observation["future_bars_excluded"] = len(bars) - len(accepted)
    if not accepted:
        observation.update({"is_usable": False, "observed_at": None})
        return observation
    latest = accepted[-1]
    latest_timestamp = _timestamp(latest)
    observation.update(
        {
            "is_usable": True,
            "observed_at": latest_timestamp.isoformat() if latest_timestamp else None,
            "price": _number(_value(latest, "close", "close_price")),
            "current_price": _number(_value(latest, "close", "close_price")),
        }
    )
    return observation


def build_alphaops_intraday_strategy(
    candidates: Mapping[str, Mapping[str, Any]],
    *,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> StrategySpec:
    """Create a v2 strategy spec whose eligibility is exactly the V5 policy."""

    def generate_signal(
        _spec: StrategySpec,
        _dataset: MarketDataset,
        symbol: str,
        bars: tuple[MarketBar, ...],
        index: int,
    ) -> StrategySignal | None:
        if index < 0 or index >= len(bars):
            return None
        candidate = dict(candidates.get(symbol, {}))
        if not candidate:
            return None
        decision_at = bars[index].timestamp
        signal = {
            **candidate,
            "strategy_id": candidate.get("strategy_id", policy.strategy_id),
            "strategy_version": candidate.get("strategy_version", policy.strategy_version),
        }
        observation = build_point_in_time_observation(
            bars[: index + 1],
            as_of=decision_at,
            base=candidate.get("observation")
            if isinstance(candidate.get("observation"), Mapping)
            else candidate,
        )
        artifact_identity = str(candidate.get("artifact_identity") or "unretained")
        artifact_hash = str(candidate.get("artifact_hash_sha256") or "")
        if not artifact_hash:
            return None
        point = IntradayDecisionPoint(
            symbol=symbol,
            decision_at=decision_at,
            signal=signal,
            observation=observation,
            artifact_identity=artifact_identity,
            artifact_hash_sha256=artifact_hash,
            exchange_session_id=str(
                candidate.get("exchange_session_id") or "unknown-session"
            ),
        )
        evaluation = evaluate_alphaops_intraday(point, policy=policy)
        decision = evaluation.decision
        if not decision.eligible_for_official_paper:
            return None
        entry = _number(observation.get("price"))
        stop = _number(signal.get("invalidation_level", signal.get("invalidation")))
        target = _number(signal.get("target_1", signal.get("target")))
        if entry is None or stop is None or target is None:
            return None
        direction = str(signal.get("direction") or Direction.LONG).lower()
        if direction not in {Direction.LONG, Direction.SHORT}:
            return None
        return StrategySignal(
            strategy_id=policy.strategy_id,
            strategy_version=policy.strategy_version,
            symbol=symbol,
            signal_index=index,
            direction=direction,
            entry_reference=entry,
            stop=stop,
            target=target,
            score=decision.feasibility_score,
            evidence=(
                "alphaops_v5_policy",
                evaluation.artifact_identity,
                evaluation.artifact_hash_sha256,
            ),
            invalidation="v5_policy_stop",
        )

    return StrategySpec(
        strategy_id=ALPHAOPS_V5_STRATEGY_ID,
        version=ALPHAOPS_V5_STRATEGY_VERSION,
        status="research_only_causal_intraday",
        description="AlphaOps V5 policy adapter for retained point-in-time intraday evidence.",
        compatible_timeframe="1min",
        required_data_fields=("timestamp", "open", "high", "low", "close", "volume"),
        parameters={
            "policy_version": policy.policy_version,
            "cost_model_version": policy.cost_model_version,
            "broker_execution_enabled": False,
        },
        indicators=(),
        entry_logic="delegate to frozen AlphaOps V5 official-paper policy",
        exit_logic="delegate to causal intraday replay path truth",
        stop_logic="use saved V5 invalidation level",
        target_logic="use saved V5 independent target",
        position_sizing_assumption="use frozen V5 simulated risk sizing",
        known_failure_modes=(
            "missing_retained_artifact",
            "future_bar_excluded",
            "provisional_cost_model",
        ),
        validation_status="research_only_pending_protocol_approval",
        generate_signal=generate_signal,
    )


def _value(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, Mapping) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _timestamp(value: Any) -> datetime | None:
    candidate = _value(value, "timestamp", "timestamp_at")
    if isinstance(candidate, datetime):
        return candidate
    if candidate:
        try:
            return datetime.fromisoformat(str(candidate).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


__all__ = [
    "IntradayDecisionPoint",
    "IntradayPolicyEvaluation",
    "build_alphaops_intraday_strategy",
    "build_point_in_time_observation",
    "evaluate_alphaops_intraday",
]
