"""Prospective AlphaOps v5 official-paper eligibility and risk sizing.

The policy is deterministic and research-only.  It can create simulated paper
intents, but it cannot place or route broker orders.  AlphaOps v4 records remain
outside this contract and are never rewritten by it.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import asdict, dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.execution_cost import (
    CostModelStatus,
    ExecutionCostModel,
)
from intraday_scanner.alpha.path_replay import resolve_path
from intraday_scanner.alpha.plan_constructor import (
    COMPLETE as PLAN_COMPLETE,
)
from intraday_scanner.alpha.plan_constructor import (
    validate_alphaops_v5_plan,
)

EASTERN = ZoneInfo("America/New_York")

ALPHAOPS_V5_STRATEGY_ID = "alphaops_v5"
ALPHAOPS_V5_STRATEGY_VERSION = "dawnstrike-alphaops-v5.0.0"
ALPHAOPS_V5_ACCOUNT_ID = "alphaops_v5_simulated"
ALPHAOPS_V5_POLICY_VERSION = "alphaops-v5-official-paper-policy-2026-07-31"
ALPHAOPS_V5_COST_MODEL_VERSION = "alphaops-v5-cost-model-50bps-0.005ps"
ALPHAOPS_V5_COST_MODEL_STATUS = CostModelStatus.PROVISIONAL.value
ALPHAOPS_V5_ACTIVATION_TIMESTAMP = "2026-07-31T00:00:00-04:00"
V5_DECISION_TRACE_SCHEMA_VERSION = "dawnstrike.alphaops.v5_decision_trace.v1"

PASSING_ALERT_GATES = frozenset({"PASS", "ALERT_OK"})
PASSING_STATUSES = frozenset({"CLEAR", "VERIFIED", "OK", "PASS"})
INDEPENDENT_TARGET_BASES = frozenset(
    {
        "sourced_resistance",
        "prior_resistance",
        "prior_swing_high",
        "prior_day_resistance",
        "prior_week_resistance",
        "gap_boundary",
        "liquidity_level",
        "vwap_structure",
    }
)
RESEARCH_ONLY_TIERS = frozenset(
    {
        "probability_fallback",
        "watch_only",
        "needs_confirmation",
        "legacy_body_recovered",
        "legacy_recovered",
        "no_trade",
    }
)


@dataclass(frozen=True, slots=True)
class AlphaOpsV5Policy:
    """Frozen thresholds selected strictly before the v5 activation cutoff."""

    activation_timestamp: str = ALPHAOPS_V5_ACTIVATION_TIMESTAMP
    strategy_id: str = ALPHAOPS_V5_STRATEGY_ID
    strategy_version: str = ALPHAOPS_V5_STRATEGY_VERSION
    account_id: str = ALPHAOPS_V5_ACCOUNT_ID
    simulated_opening_equity: float = 100_000.0
    max_risk_per_position_pct: float = 0.25
    max_symbol_notional_pct: float = 10.0
    minimum_after_cost_reward_risk: float = 1.50
    maximum_chase_pct: float = 2.0
    maximum_stop_distance_pct: float = 15.0
    maximum_gap_pct: float = 50.0
    maximum_spread_bps: float = 200.0
    minimum_premarket_dollar_volume: float = 1_000_000.0
    minimum_source_confidence: float = 80.0
    minimum_source_count: int = 2
    maximum_quote_age_seconds: int = 360
    entry_slippage_bps: float = 50.0
    exit_slippage_bps: float = 50.0
    commission_per_share_per_side: float = 0.005
    entry_session_start: str = "09:30:00"
    entry_session_end_exclusive: str = "15:30:00"
    policy_version: str = ALPHAOPS_V5_POLICY_VERSION
    cost_model_version: str = ALPHAOPS_V5_COST_MODEL_VERSION

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


DEFAULT_V5_POLICY = AlphaOpsV5Policy()


def modeled_alphaops_v5_plan_metrics(
    plan: dict[str, Any],
    *,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> dict[str, float | str | None]:
    """Return the frozen-plan cost and stop metrics used by V5 admission.

    This intentionally excludes current-quote chase and freshness checks. It
    lets the immutable strategy receipt state the exact static paper boundary
    without claiming that a later watcher quote has already been observed.
    """

    direction = str(plan.get("direction") or "").strip().lower()
    entry = _number(plan.get("entry"))
    stop = _number(plan.get("stop"))
    target = _number(plan.get("target"))
    empty: dict[str, float | str | None] = {
        "direction": direction,
        "gross_reward_risk": None,
        "actual_after_cost_reward_risk": None,
        "stop_distance_pct": None,
        "expected_entry_price": None,
        "expected_stop_exit_price": None,
        "expected_target_exit_price": None,
    }
    if (
        direction not in {"long", "short"}
        or entry is None
        or stop is None
        or target is None
        or min(entry, stop, target) <= 0
    ):
        return empty
    if direction == "short":
        if not (stop > entry > target):
            return empty
        expected_entry = entry * (1 - policy.entry_slippage_bps / 10_000)
        expected_stop = stop * (1 + policy.exit_slippage_bps / 10_000)
        expected_target = target * (1 + policy.exit_slippage_bps / 10_000)
        gross_reward = entry - target
        gross_risk = stop - entry
        reward = expected_entry - expected_target
        risk = expected_stop - expected_entry
        stop_distance_pct = (stop - expected_entry) / expected_entry * 100
    else:
        if not (target > entry > stop):
            return empty
        expected_entry = entry * (1 + policy.entry_slippage_bps / 10_000)
        expected_stop = stop * (1 - policy.exit_slippage_bps / 10_000)
        expected_target = target * (1 - policy.exit_slippage_bps / 10_000)
        gross_reward = target - entry
        gross_risk = entry - stop
        reward = expected_target - expected_entry
        risk = expected_entry - expected_stop
        stop_distance_pct = (expected_entry - stop) / expected_entry * 100
    commission = policy.commission_per_share_per_side * 2
    reward -= commission
    risk += commission
    after_cost = reward / risk if reward > 0 and risk > 0 else None
    gross = gross_reward / gross_risk if gross_reward > 0 and gross_risk > 0 else None
    return {
        "direction": direction,
        "gross_reward_risk": _rounded(gross),
        "actual_after_cost_reward_risk": _rounded(after_cost),
        "stop_distance_pct": _rounded(stop_distance_pct),
        "expected_entry_price": _rounded(expected_entry),
        "expected_stop_exit_price": _rounded(expected_stop),
        "expected_target_exit_price": _rounded(expected_target),
    }


def v5_execution_cost_model(
    *, status: CostModelStatus = CostModelStatus.PROVISIONAL
) -> ExecutionCostModel:
    """Return the frozen V5 cost proxy with its empirical-evidence status."""

    return ExecutionCostModel(
        version=ALPHAOPS_V5_COST_MODEL_VERSION,
        entry_slippage_bps=DEFAULT_V5_POLICY.entry_slippage_bps,
        exit_slippage_bps=DEFAULT_V5_POLICY.exit_slippage_bps,
        commission_per_share_per_side=DEFAULT_V5_POLICY.commission_per_share_per_side,
        status=status,
    )


def evaluate_v5_causal_exit(
    bars: list[Any] | tuple[Any, ...],
    *,
    decision_at: datetime,
    trigger: float,
    target: float,
    stop: float,
    exit_policy: str = "target_stop_first",
) -> dict[str, Any]:
    """Evaluate a V5 exit challenger from the same causal bar sequence."""

    causal_bars = [
        {
            **bar,
            "observed_at": _bar_timestamp(bar),
        }
        if isinstance(bar, dict)
        else {
            "observed_at": _bar_timestamp(bar),
            "open": _bar_value(bar, "open", "open_price"),
            "high": _bar_value(bar, "high", "high_price"),
            "low": _bar_value(bar, "low", "low_price"),
            "close": _bar_value(bar, "close", "close_price"),
        }
        for bar in bars
    ]
    path = resolve_path(
        causal_bars,
        decision_at=decision_at,
        trigger=trigger,
        target=target,
        stop=stop,
    )
    payload = path.to_dict()
    payload.update(
        {
            "exit_policy": exit_policy,
            "cost_model_status": ALPHAOPS_V5_COST_MODEL_STATUS,
            "research_only": True,
            "promotion_eligible": False,
        }
    )
    if exit_policy == "session_close":
        post_entry = [
            bar
            for bar in causal_bars
            if (timestamp := _bar_timestamp(bar)) is not None and timestamp > decision_at
        ]
        if post_entry:
            last = post_entry[-1]
            payload["exit_time"] = _bar_timestamp(last).isoformat()  # type: ignore[union-attr]
            payload["exit_price"] = _bar_value(last, "close", "close_price")
            payload["conservative_policy_result"] = "session_close"
    elif exit_policy == "time_stop":
        post_entry = [
            bar
            for bar in causal_bars
            if (timestamp := _bar_timestamp(bar)) is not None and timestamp > decision_at
        ]
        if post_entry:
            selected = post_entry[min(2, len(post_entry) - 1)]
            payload["exit_time"] = _bar_timestamp(selected).isoformat()  # type: ignore[union-attr]
            payload["exit_price"] = _bar_value(selected, "close", "close_price")
            payload["conservative_policy_result"] = "time_stop"
    return payload


def build_v5_challenger_registry() -> tuple[dict[str, Any], ...]:
    """Register cost/risk/exit challengers without enabling any of them."""

    return (
        {
            "challenger_id": "v5_baseline",
            "controlled_change": "none; frozen V5 target-stop policy",
            "cost_model_status": ALPHAOPS_V5_COST_MODEL_STATUS,
            "promotion_eligible": False,
        },
        {
            "challenger_id": "v5_atr_stop_target",
            "controlled_change": "ATR stop and independent 2x ATR target geometry",
            "cost_model_status": ALPHAOPS_V5_COST_MODEL_STATUS,
            "promotion_eligible": False,
        },
        {
            "challenger_id": "v5_liquidity_aware_risk",
            "controlled_change": "reduce notional when liquidity is not high",
            "cost_model_status": ALPHAOPS_V5_COST_MODEL_STATUS,
            "promotion_eligible": False,
        },
        {
            "challenger_id": "v5_session_close_exit",
            "controlled_change": "session-close exit challenger",
            "cost_model_status": ALPHAOPS_V5_COST_MODEL_STATUS,
            "promotion_eligible": False,
        },
        {
            "challenger_id": "v5_cost_stress_1_5x",
            "controlled_change": "1.5x slippage stress only",
            "cost_model_status": ALPHAOPS_V5_COST_MODEL_STATUS,
            "promotion_eligible": False,
        },
    )


@dataclass(frozen=True, slots=True)
class AlphaOpsV5Decision:
    eligible_for_official_paper: bool
    action: str
    reasons: tuple[str, ...]
    checks: tuple[dict[str, Any], ...]
    computed: dict[str, Any]
    sizing: dict[str, Any]
    feasibility_score: float
    policy_version: str
    cost_model_version: str
    strategy_id: str
    strategy_version: str
    account_id: str
    activation_timestamp: str
    decision_fingerprint: str
    signal_id: str
    ticker: str
    plan_hash_sha256: str
    research_only: bool = True
    broker_execution_enabled: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema_version"] = V5_DECISION_TRACE_SCHEMA_VERSION
        payload["reasons"] = list(self.reasons)
        payload["checks"] = list(self.checks)
        return payload


def is_v5_active(
    value: str | datetime,
    *,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> bool:
    """Return whether a timestamp is at or beyond the prospective boundary."""

    observed = _parse_datetime(value)
    activation = _parse_datetime(policy.activation_timestamp)
    return observed is not None and activation is not None and observed >= activation


def alphaops_strategy_contract(
    value: str | datetime,
    *,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> tuple[str, str]:
    """Resolve the immutable strategy contract without rewriting v4 history."""

    if is_v5_active(value, policy=policy):
        return policy.strategy_id, policy.strategy_version
    return "alphaops_v4", "dawnstrike-alphaops-v4"


def evaluate_v5_official_paper(
    signal: dict[str, Any],
    observation: dict[str, Any],
    *,
    simulated_equity: float | None = None,
    existing_symbol_notional: float = 0.0,
    decision_time: str | None = None,
    policy: AlphaOpsV5Policy = DEFAULT_V5_POLICY,
) -> AlphaOpsV5Decision:
    """Evaluate one prospective entry and produce a reconstructable trace."""

    source = _SignalFacts(signal)
    raw_plan = signal.get("alphaops_market_structure_plan")
    strict_plan = dict(raw_plan) if isinstance(raw_plan, dict) else {}
    ticker = source.text("ticker").upper()
    try:
        strict_plan_valid = bool(strict_plan) and validate_alphaops_v5_plan(
            strict_plan, expected_ticker=ticker
        )
    except (TypeError, ValueError):
        strict_plan_valid = False
    strict_plan_hash = str(strict_plan.get("plan_hash_sha256") or "")
    strict_plan_direction = str(strict_plan.get("direction") or "").lower()
    at = (
        decision_time
        or str(observation.get("requested_at") or "")
        or str(observation.get("observed_at") or "")
    )
    equity = (
        policy.simulated_opening_equity if simulated_equity is None else _number(simulated_equity)
    )
    entry = _number(observation.get("price") or observation.get("current_price"))
    trigger = source.number(
        "entry_watch_level",
        "entry_trigger",
        "breakout_trigger",
        "premarket_price",
    )
    stop = source.number("invalidation_level", "invalidation", "exit_line")
    target = source.number("target_1", "first_target", "target")
    previous_close = source.number("previous_close")
    premarket_high = source.number("premarket_high")
    premarket_low = source.number("premarket_low")
    float_shares = source.number("float_shares")
    dollar_volume = source.number("dollar_volume", "premarket_dollar_volume")
    if dollar_volume is None:
        premarket_price = source.number("premarket_price")
        premarket_volume = source.number("premarket_volume", "volume")
        if premarket_price is not None and premarket_volume is not None:
            dollar_volume = premarket_price * premarket_volume
    gap_pct = source.number("gap_pct")
    if gap_pct is None and previous_close and previous_close > 0:
        reference_price = source.number("premarket_price")
        if reference_price is not None:
            gap_pct = (reference_price - previous_close) / previous_close * 100
    spread_pct = source.number("spread_pct")
    spread_bps = spread_pct * 100 if spread_pct is not None else None
    quote_age = _number(observation.get("quote_freshness_seconds"))
    if quote_age is None:
        # Legacy/non-Alpaca observations only have bar freshness.  They still
        # cannot pass the executable quote gate, but retaining the fallback
        # keeps the policy trace deterministic for those stand-down paths.
        quote_age = _number(observation.get("freshness_seconds"))

    direction = strict_plan_direction
    if direction == "short":
        # A short entry sells at the bid and an exit buys to cover at the ask;
        # both are adverse relative to the frozen structural levels.
        expected_entry = (
            entry * (1 - policy.entry_slippage_bps / 10_000) if entry is not None else None
        )
        expected_stop_exit = (
            stop * (1 + policy.exit_slippage_bps / 10_000) if stop is not None else None
        )
        expected_target_exit = (
            target * (1 + policy.exit_slippage_bps / 10_000) if target is not None else None
        )
    else:
        expected_entry = (
            entry * (1 + policy.entry_slippage_bps / 10_000) if entry is not None else None
        )
        expected_stop_exit = (
            stop * (1 - policy.exit_slippage_bps / 10_000) if stop is not None else None
        )
        expected_target_exit = (
            target * (1 - policy.exit_slippage_bps / 10_000) if target is not None else None
        )
    round_trip_commission = policy.commission_per_share_per_side * 2
    if direction == "short":
        gross_reward = entry - target if target is not None and entry is not None else None
        gross_risk = stop - entry if stop is not None and entry is not None else None
        reward_per_share = (
            expected_entry - expected_target_exit - round_trip_commission
            if expected_target_exit is not None and expected_entry is not None
            else None
        )
        risk_per_share = (
            expected_stop_exit - expected_entry + round_trip_commission
            if expected_entry is not None and expected_stop_exit is not None
            else None
        )
    else:
        gross_reward = target - entry if target is not None and entry is not None else None
        gross_risk = entry - stop if stop is not None and entry is not None else None
        reward_per_share = (
            expected_target_exit - expected_entry - round_trip_commission
            if expected_target_exit is not None and expected_entry is not None
            else None
        )
        risk_per_share = (
            expected_entry - expected_stop_exit + round_trip_commission
            if expected_entry is not None and expected_stop_exit is not None
            else None
        )
    after_cost_reward_risk = (
        reward_per_share / risk_per_share
        if reward_per_share is not None
        and risk_per_share is not None
        and reward_per_share > 0
        and risk_per_share > 0
        else None
    )
    gross_reward_risk = (
        gross_reward / gross_risk
        if gross_reward is not None
        and gross_risk is not None
        and gross_reward > 0
        and gross_risk > 0
        else None
    )
    stop_distance_pct = (
        (
            (stop - expected_entry) / expected_entry * 100
            if direction == "short"
            else (expected_entry - stop) / expected_entry * 100
        )
        if expected_entry is not None
        and stop is not None
        and expected_entry > 0
        and (
            (direction == "short" and stop > expected_entry)
            or (direction != "short" and expected_entry > stop)
        )
        else None
    )
    chase_pct = (
        max(
            0.0,
            (
                (trigger - expected_entry) / trigger * 100
                if direction == "short"
                else (expected_entry - trigger) / trigger * 100
            ),
        )
        if expected_entry is not None and trigger is not None and trigger > 0
        else None
    )

    checks: list[dict[str, Any]] = []

    def check(
        check_id: str,
        passed: bool,
        reason: str,
        *,
        observed: Any = None,
        threshold: Any = None,
        component: str,
        weight: float,
    ) -> None:
        checks.append(
            {
                "check_id": check_id,
                "passed": bool(passed),
                "reason": "" if passed else reason,
                "observed": observed,
                "threshold": threshold,
                "component": component,
                "weight": weight,
            }
        )

    check(
        "prospective_activation",
        is_v5_active(at, policy=policy),
        "pre_v5_activation",
        observed=at,
        threshold=policy.activation_timestamp,
        component="contract",
        weight=0,
    )
    strategy_id = source.text("strategy_id")
    strategy_version = source.text("strategy_version")
    check(
        "strategy_contract",
        strategy_id == policy.strategy_id and strategy_version == policy.strategy_version,
        "wrong_strategy_contract",
        observed=f"{strategy_id}:{strategy_version}",
        threshold=f"{policy.strategy_id}:{policy.strategy_version}",
        component="contract",
        weight=0,
    )
    plan_levels_bound = (
        strict_plan_valid
        and strict_plan.get("status") == PLAN_COMPLETE
        and strict_plan_direction in {"long", "short"}
        and str(signal.get("plan_hash_sha256") or "") == strict_plan_hash
        and _number(strict_plan.get("entry")) == trigger
        and _number(strict_plan.get("stop")) == stop
        and _number(strict_plan.get("target")) == target
        and source.text("target_basis_kind").lower()
        == str(strict_plan.get("target_basis_kind") or "").lower()
        and strict_plan.get("target_frozen_before_reward_risk") is True
    )
    check(
        "strict_frozen_plan",
        plan_levels_bound,
        "strict_frozen_plan_missing_or_invalid",
        observed={
            "status": strict_plan.get("status"),
            "direction": strict_plan_direction,
            "plan_hash_sha256": strict_plan_hash,
            "row_plan_hash_sha256": str(signal.get("plan_hash_sha256") or ""),
        },
        threshold="validated, source-bound AlphaOps v5 directional plan with frozen levels",
        component="contract",
        weight=0,
    )
    decision = source.text("decision", "decision_tier").lower()
    check(
        "selection_tier",
        decision == "clean_edge",
        "selection_not_clean_edge",
        observed=decision,
        threshold="clean_edge",
        component="evidence",
        weight=5,
    )
    gate = source.text("alert_gate_status").upper()
    check(
        "alert_gate",
        gate in PASSING_ALERT_GATES,
        "alert_gate_not_pass",
        observed=gate,
        threshold=sorted(PASSING_ALERT_GATES),
        component="evidence",
        weight=5,
    )
    manual = source.boolean("manual_confirmation_required")
    check(
        "manual_confirmation",
        manual is False,
        "manual_confirmation_required",
        observed=manual,
        threshold=False,
        component="evidence",
        weight=5,
    )
    tier_tokens = {
        decision,
        source.text("classification").lower().replace(" ", "_"),
        source.text("review_label").lower().replace(" ", "_"),
        source.text("selection_tier").lower(),
    }
    blocked_tiers = sorted(tier_tokens & RESEARCH_ONLY_TIERS)
    check(
        "research_only_tier",
        not blocked_tiers,
        "research_only_selection_tier",
        observed=blocked_tiers,
        threshold=[],
        component="evidence",
        weight=5,
    )

    source_confidence = source.number("source_confidence")
    source_count = source.integer("source_count")
    source_status = source.text("source_quality_status", "source_status").upper()
    stale = source.boolean("stale_data_flag")
    check(
        "source_quality",
        source_confidence is not None
        and source_confidence >= policy.minimum_source_confidence
        and source_count is not None
        and source_count >= policy.minimum_source_count
        and source_status in PASSING_STATUSES
        and stale is False,
        "source_evidence_incomplete_or_weak",
        observed={
            "confidence": source_confidence,
            "count": source_count,
            "status": source_status,
            "stale": stale,
        },
        threshold={
            "minimum_confidence": policy.minimum_source_confidence,
            "minimum_count": policy.minimum_source_count,
            "status": sorted(PASSING_STATUSES),
            "stale": False,
        },
        component="evidence",
        weight=10,
    )
    range_valid = (
        previous_close is not None
        and previous_close > 0
        and premarket_high is not None
        and premarket_low is not None
        and premarket_high > premarket_low > 0
    )
    check(
        "market_structure_evidence",
        range_valid,
        "market_structure_evidence_missing",
        observed={
            "previous_close": previous_close,
            "premarket_high": premarket_high,
            "premarket_low": premarket_low,
        },
        threshold="positive previous close and observed high > low > 0",
        component="evidence",
        weight=10,
    )
    float_status = source.text("float_status").upper()
    float_source = source.text("float_source")
    check(
        "float_evidence",
        float_shares is not None
        and float_shares > 0
        and (float_status in PASSING_STATUSES or bool(float_source)),
        "float_evidence_missing",
        observed={
            "float_shares": float_shares,
            "status": float_status,
            "source": float_source,
        },
        threshold="positive sourced or verified float",
        component="evidence",
        weight=5,
    )
    catalyst_text = source.text("catalyst_summary", "catalyst_headline")
    catalyst_url = source.text("catalyst_url")
    catalyst_status = source.text("catalyst_status").upper()
    catalyst_tier = source.text("catalyst_tier").upper()
    catalyst_valid = (
        bool(catalyst_text)
        and catalyst_text.lower() not in {"none", "no clear catalyst"}
        and bool(catalyst_url)
        and (catalyst_status in PASSING_STATUSES or catalyst_tier in {"A", "B"})
    )
    check(
        "catalyst_evidence",
        catalyst_valid,
        "catalyst_evidence_missing_or_weak",
        observed={
            "summary": catalyst_text,
            "url": catalyst_url,
            "status": catalyst_status,
            "tier": catalyst_tier,
        },
        threshold="sourced verified catalyst or tier A/B",
        component="catalyst",
        weight=10,
    )
    for check_id, names in (
        ("halt_status", ("halt_status",)),
        ("sec_risk_status", ("sec_risk_status",)),
        ("corporate_action_status", ("corporate_action_status",)),
    ):
        value = source.text(*names).upper()
        check(
            check_id,
            value in PASSING_STATUSES,
            f"{check_id}_unknown_or_blocked",
            observed=value,
            threshold=sorted(PASSING_STATUSES),
            component="evidence",
            weight=5,
        )

    local_time = _eastern_time(at)
    session_start = time.fromisoformat(policy.entry_session_start)
    session_end = time.fromisoformat(policy.entry_session_end_exclusive)
    check(
        "entry_session",
        local_time is not None and session_start <= local_time < session_end,
        "entry_outside_registered_session",
        observed=local_time.isoformat() if local_time else None,
        threshold=f"[{policy.entry_session_start},{policy.entry_session_end_exclusive}) ET",
        component="session",
        weight=10,
    )
    observed_at = _parse_datetime(str(observation.get("observed_at") or ""))
    requested_at = _parse_datetime(at)
    no_future_bar = (
        observed_at is not None and requested_at is not None and observed_at <= requested_at
    )
    quote_usable = _boolean(observation.get("is_usable"))
    check(
        "quote_freshness",
        quote_usable is True
        and quote_age is not None
        and 0 <= quote_age <= policy.maximum_quote_age_seconds
        and no_future_bar,
        "quote_missing_stale_or_lookahead",
        observed={
            "usable": quote_usable,
            "age_seconds": quote_age,
            "observed_at": str(observation.get("observed_at") or ""),
            "requested_at": at,
        },
        threshold={
            "maximum_age_seconds": policy.maximum_quote_age_seconds,
            "no_lookahead": True,
        },
        component="freshness",
        weight=10,
    )

    levels_valid = (
        expected_entry is not None
        and trigger is not None
        and stop is not None
        and target is not None
        and (
            (direction == "long" and target > expected_entry > stop > 0 and trigger > stop)
            or (direction == "short" and target < expected_entry < stop and trigger < stop)
        )
    )
    check(
        "level_geometry",
        levels_valid,
        "entry_stop_target_geometry_invalid",
        observed={
            "expected_entry": expected_entry,
            "trigger": trigger,
            "stop": stop,
            "target": target,
        },
        threshold="long: target > expected entry > stop; short: target < expected entry < stop",
        component="risk",
        weight=0,
    )
    target_basis = str(strict_plan.get("target_basis_kind") or "").lower()
    target_is_risk_derived = source.boolean("target_derived_from_risk")
    check(
        "independent_target",
        plan_levels_bound
        and target_basis in INDEPENDENT_TARGET_BASES
        and target_is_risk_derived is False,
        "target_not_independently_derived",
        observed={
            "basis": target_basis,
            "derived_from_risk": target_is_risk_derived,
        },
        threshold={
            "allowed_bases": sorted(INDEPENDENT_TARGET_BASES),
            "derived_from_risk": False,
        },
        component="risk",
        weight=0,
    )
    check(
        "stop_distance",
        stop_distance_pct is not None and stop_distance_pct <= policy.maximum_stop_distance_pct,
        "stop_distance_exceeds_policy",
        observed=_rounded(stop_distance_pct),
        threshold=policy.maximum_stop_distance_pct,
        component="stop_distance",
        weight=10,
    )
    check(
        "chase_distance",
        chase_pct is not None and chase_pct <= policy.maximum_chase_pct,
        "chase_distance_exceeds_policy",
        observed=_rounded(chase_pct),
        threshold=policy.maximum_chase_pct,
        component="chase",
        weight=10,
    )
    check(
        "gap_regime",
        gap_pct is not None and 0 <= gap_pct <= policy.maximum_gap_pct,
        "gap_regime_outside_policy",
        observed=_rounded(gap_pct),
        threshold=f"0..{policy.maximum_gap_pct}",
        component="gap",
        weight=5,
    )
    liquidity_tier = source.text("liquidity_tier").lower()
    check(
        "liquidity_and_spread",
        dollar_volume is not None
        and dollar_volume >= policy.minimum_premarket_dollar_volume
        and spread_bps is not None
        and spread_bps <= policy.maximum_spread_bps
        and liquidity_tier in {"watchable_liquidity", "high_liquidity", "institutional_liquidity"},
        "liquidity_or_spread_outside_policy",
        observed={
            "dollar_volume": _rounded(dollar_volume),
            "spread_bps": _rounded(spread_bps),
            "tier": liquidity_tier,
        },
        threshold={
            "minimum_dollar_volume": policy.minimum_premarket_dollar_volume,
            "maximum_spread_bps": policy.maximum_spread_bps,
        },
        component="liquidity",
        weight=15,
    )
    check(
        "after_cost_reward_risk",
        after_cost_reward_risk is not None
        and after_cost_reward_risk + 1e-12 >= policy.minimum_after_cost_reward_risk,
        "after_cost_reward_risk_below_policy",
        observed=_rounded(after_cost_reward_risk),
        threshold=policy.minimum_after_cost_reward_risk,
        component="cost_adjusted_r",
        weight=10,
    )

    risk_budget = (
        equity * policy.max_risk_per_position_pct / 100
        if equity is not None and equity > 0
        else None
    )
    symbol_notional_limit = (
        equity * policy.max_symbol_notional_pct / 100 if equity is not None and equity > 0 else None
    )
    remaining_notional = (
        max(0.0, symbol_notional_limit - max(0.0, existing_symbol_notional))
        if symbol_notional_limit is not None
        else None
    )
    risk_limited_shares = (
        math.floor(risk_budget / risk_per_share)
        if risk_budget is not None and risk_per_share is not None and risk_per_share > 0
        else 0
    )
    notional_limited_shares = (
        math.floor(remaining_notional / expected_entry)
        if remaining_notional is not None and expected_entry is not None and expected_entry > 0
        else 0
    )
    shares = min(risk_limited_shares, notional_limited_shares)
    proposed_notional = shares * expected_entry if expected_entry is not None else None
    proposed_risk = shares * risk_per_share if risk_per_share is not None else None
    check(
        "risk_sizing",
        equity is not None
        and equity > 0
        and risk_per_share is not None
        and risk_per_share > 0
        and shares >= 1
        and proposed_notional is not None
        and symbol_notional_limit is not None
        and remaining_notional is not None
        and proposed_notional <= remaining_notional + 1e-9
        and proposed_risk is not None
        and risk_budget is not None
        and proposed_risk <= risk_budget + 1e-9,
        "risk_sizing_unavailable_or_zero_shares",
        observed={
            "equity": _rounded(equity),
            "risk_per_share": _rounded(risk_per_share),
            "shares": shares,
            "existing_symbol_notional": _rounded(existing_symbol_notional),
        },
        threshold={
            "max_risk_pct": policy.max_risk_per_position_pct,
            "max_symbol_notional_pct": policy.max_symbol_notional_pct,
        },
        component="risk",
        weight=0,
    )

    reasons = tuple(
        dict.fromkeys(
            str(item["reason"]) for item in checks if not item["passed"] and item["reason"]
        )
    )
    eligible = not reasons
    weighted = [item for item in checks if float(item["weight"]) > 0]
    total_weight = sum(float(item["weight"]) for item in weighted)
    passed_weight = sum(float(item["weight"]) for item in weighted if item["passed"])
    feasibility_score = round(
        100 * passed_weight / total_weight if total_weight else 0.0,
        2,
    )
    sizing = {
        "simulated_equity": _rounded(equity),
        "risk_budget": _rounded(risk_budget),
        "symbol_notional_limit": _rounded(symbol_notional_limit),
        "existing_symbol_notional": _rounded(existing_symbol_notional),
        "remaining_symbol_notional": _rounded(remaining_notional),
        "risk_limited_shares": risk_limited_shares,
        "notional_limited_shares": notional_limited_shares,
        "shares": shares,
        "proposed_notional": _rounded(proposed_notional),
        "proposed_risk": _rounded(proposed_risk),
    }
    computed = {
        "decision_time": at,
        "direction": direction,
        "plan_hash_sha256": strict_plan_hash,
        "entry_price_observed": _rounded(entry),
        "expected_entry_price": _rounded(expected_entry),
        "stop_price": _rounded(stop),
        "expected_stop_exit_price": _rounded(expected_stop_exit),
        "target_price": _rounded(target),
        "expected_target_exit_price": _rounded(expected_target_exit),
        "trigger_price": _rounded(trigger),
        "risk_per_share_after_cost": _rounded(risk_per_share),
        "reward_per_share_after_cost": _rounded(reward_per_share),
        "gross_reward_risk": _rounded(gross_reward_risk),
        "actual_after_cost_reward_risk": _rounded(after_cost_reward_risk),
        "stop_distance_pct": _rounded(stop_distance_pct),
        "chase_pct": _rounded(chase_pct),
        "gap_pct": _rounded(gap_pct),
        "spread_bps": _rounded(spread_bps),
        "dollar_volume": _rounded(dollar_volume),
        "quote_age_seconds": _rounded(quote_age),
        "target_basis_kind": target_basis,
        "target_derived_from_risk": target_is_risk_derived,
    }
    fingerprint_payload = {
        "signal_id": source.text("signal_id"),
        "selection_id": source.text("selection_id"),
        "ticker": ticker,
        "plan_hash_sha256": strict_plan_hash,
        "direction": direction,
        "policy": policy.to_dict(),
        "checks": checks,
        "computed": computed,
        "sizing": sizing,
    }
    fingerprint = hashlib.sha256(
        json.dumps(
            fingerprint_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    return AlphaOpsV5Decision(
        eligible_for_official_paper=eligible,
        action="OFFICIAL_PAPER_ALLOW" if eligible else "RESEARCH_ONLY_BLOCK",
        reasons=reasons,
        checks=tuple(checks),
        computed=computed,
        sizing=sizing,
        feasibility_score=feasibility_score,
        policy_version=policy.policy_version,
        cost_model_version=policy.cost_model_version,
        strategy_id=policy.strategy_id,
        strategy_version=policy.strategy_version,
        account_id=policy.account_id,
        activation_timestamp=policy.activation_timestamp,
        decision_fingerprint=fingerprint,
        signal_id=source.text("signal_id"),
        ticker=ticker,
        plan_hash_sha256=strict_plan_hash,
    )


class _SignalFacts:
    """Read persisted top-level and nested immutable payload facts."""

    def __init__(self, signal: dict[str, Any]) -> None:
        self._sources: list[dict[str, Any]] = []
        self._append(signal)
        for name in (
            "raw_payload_json",
            "selection_payload_json",
            "payload_json",
        ):
            value = signal.get(name)
            if isinstance(value, dict):
                self._append(value)
                for nested in ("signal", "decision_payload"):
                    child = value.get(nested)
                    if isinstance(child, dict):
                        self._append(child)

    def _append(self, value: dict[str, Any]) -> None:
        if all(value is not item for item in self._sources):
            self._sources.append(value)

    def value(self, *names: str) -> Any:
        for source in self._sources:
            for name in names:
                if name in source and source[name] is not None and source[name] != "":
                    return source[name]
        return None

    def text(self, *names: str) -> str:
        value = self.value(*names)
        return str(value or "").strip()

    def number(self, *names: str) -> float | None:
        return _number(self.value(*names))

    def integer(self, *names: str) -> int | None:
        value = self.number(*names)
        return int(value) if value is not None else None

    def boolean(self, *names: str) -> bool | None:
        return _boolean(self.value(*names))


def _parse_datetime(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=EASTERN)
    return parsed.astimezone(EASTERN)


def _bar_value(value: Any, *names: str) -> Any:
    for name in names:
        if isinstance(value, dict) and name in value:
            return value[name]
        if hasattr(value, name):
            return getattr(value, name)
    return None


def _bar_timestamp(value: Any) -> datetime | None:
    timestamp = _bar_value(value, "timestamp")
    if isinstance(timestamp, datetime):
        return timestamp
    if timestamp:
        try:
            return datetime.fromisoformat(str(timestamp).replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _eastern_time(value: str | datetime) -> time | None:
    parsed = _parse_datetime(value)
    return parsed.timetz().replace(tzinfo=None) if parsed is not None else None


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        number = float(str(value).replace("$", "").replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return None if math.isnan(number) or math.isinf(number) else number


def _boolean(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        if value == 1:
            return True
        if value == 0:
            return False
        return None
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    return None


def _rounded(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


__all__ = [
    "ALPHAOPS_V5_ACCOUNT_ID",
    "ALPHAOPS_V5_ACTIVATION_TIMESTAMP",
    "ALPHAOPS_V5_COST_MODEL_VERSION",
    "ALPHAOPS_V5_POLICY_VERSION",
    "ALPHAOPS_V5_STRATEGY_ID",
    "ALPHAOPS_V5_STRATEGY_VERSION",
    "AlphaOpsV5Decision",
    "AlphaOpsV5Policy",
    "DEFAULT_V5_POLICY",
    "modeled_alphaops_v5_plan_metrics",
    "alphaops_strategy_contract",
    "evaluate_v5_official_paper",
    "is_v5_active",
]
