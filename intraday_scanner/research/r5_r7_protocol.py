"""Finite R5 research protocol and R7 observer-controller boundary.

This module freezes the D027-D029 contract before any outcome inspection.  It
is deliberately an offline research surface: it creates no orders, loads no
credentials, calls no provider, and never enables broker or live promotion.
Observational rows and synthetic controls remain separate from FillTruth and
official financial evidence.
"""

from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Mapping, Sequence

getcontext().prec = 28

PROTOCOL_SCHEMA = "dawnstrike.r5_r7.frozen_protocol.v1"
PROTOCOL_VERSION = "dawnstrike-r5-r7-d027-d029-v1"
LEGACY_REJECTED_STRATEGY = {
    "strategy_id": "gap_up_continuation",
    "version": "v1.0",
    "status": "REJECTED_FOR_SAME_SESSION_MANDATE",
    "reason": "daily_close_next_open_ten_calendar_day_timeout_incompatible",
}
SCOPE_IDS = (
    "gap_orb15_continuation_research_v1",
    "panel_orb15_continuation_research_v1",
)
FIXED_PANEL = ("DIA", "IWM", "QQQ", "SPY", "TLT")
V5_COST_MODEL_VERSION = "alphaops-v5-cost-model-50bps-0.005ps"
V5_POLICY_VERSION = "alphaops-v5-official-paper-policy-2026-07-31"


def _canonical(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _hash(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _file_hash(path: Path) -> str | None:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return None


def build_research_protocol(*, source_root: str | Path = ".") -> dict[str, Any]:
    """Return the immutable pre-outcome D027/D028/D029 protocol receipt."""

    root = Path(source_root).resolve()
    source_paths = {
        "v5_policy": root / "intraday_scanner" / "alpha" / "v5_policy.py",
        "execution_cost": root / "intraday_scanner" / "alpha" / "execution_cost.py",
        "learning_spec": root.parent / "dawnstrike-mission-audit-20260909" / "LEARNING_AND_DATA_SPEC.md",
    }
    scopes = {
        "gap_orb15_continuation_research_v1": {
            "universe": "complete_actual_original_mover_census",
            "fixed_panel": None,
            "opening_range": {"start_et": "09:30", "end_exclusive_et": "09:45"},
            "positive_opening_return_required": True,
            "overnight_gap_minimum_pct": 0.75,
            "corporate_action_valid_prior_close_required": True,
            "first_break_before_et": "11:30",
            "stop": "completed_opening_range_low",
            "target_multiple_r": 3.0,
            "maximum_hold_minutes": 60,
            "exit_deadline": "actual_exchange_close_minus_10_minutes",
            "direction": "long_only",
        },
        "panel_orb15_continuation_research_v1": {
            "universe": "fixed_reference_panel",
            "fixed_panel": list(FIXED_PANEL),
            "opening_range": {"start_et": "09:30", "end_exclusive_et": "09:45"},
            "positive_opening_return_required": True,
            "overnight_gap_minimum_pct": None,
            "corporate_action_valid_prior_close_required": True,
            "first_break_before_et": "11:30",
            "stop": "completed_opening_range_low",
            "target_multiple_r": 3.0,
            "maximum_hold_minutes": 60,
            "exit_deadline": "actual_exchange_close_minus_10_minutes",
            "direction": "long_only",
        },
    }
    wrapper = {
        "max_risk_per_position_pct": 0.25,
        "max_symbol_notional_pct": 10.0,
        "gross_exposure_pct": 30.0,
        "net_exposure_pct": 30.0,
        "sector_theme_exposure_pct": 20.0,
        "aggregate_open_risk_pct": 0.75,
        "daily_loss_stop_pct": 1.0,
        "drawdown_stop_pct": 8.0,
        "max_concurrent_positions": 3,
        "max_entries_per_session": 5,
        "entry_cutoff_et_exclusive": "15:30",
        "close_watchdog_offset_minutes": 10,
        "long_only": True,
        "no_leverage": True,
    }
    protocol_body: dict[str, Any] = {
        "schema_version": PROTOCOL_SCHEMA,
        "protocol_version": PROTOCOL_VERSION,
        "source_decision": "D027-D029",
        "outcome_inspection_started": False,
        "legacy_strategy_rejection": LEGACY_REJECTED_STRATEGY,
        "scopes": scopes,
        "baseline": {
            "strategy_id": "alphaops_v5",
            "strategy_version": "dawnstrike-alphaops-v5.0.0",
            "policy_version": V5_POLICY_VERSION,
            "cost_model_version": V5_COST_MODEL_VERSION,
            "unchanged_inside_wrapper": True,
        },
        "comparators": ["frozen_v5_same_scope", "cash_no_learning"],
        "wrapper": wrapper,
        "cost": {
            "status": "PROVISIONAL_UNKNOWN_EMPIRICAL_COMPONENTS",
            "adverse_slippage_bps_each_leg": 50.0,
            "commission_usd_per_share_each_side": 0.005,
            "measured_spread_bps": None,
            "latency_bps": None,
            "partial_nonfill": "explicit_unknown_until_observed",
            "market_impact": "explicit_unknown_until_observed",
            "halt_and_close_failure": "explicit_state_not_zero_cost",
            "no_double_counting": True,
        },
        "preregistration": {
            "primary_metric": "paired_mean_daily_log_account_return_after_costs",
            "minimum_useful_improvement_bps_per_expected_session": 2.0,
            "confirmation_min_common_complete_sessions": 60,
            "confirmation_min_hypothetical_trades_per_policy": 30,
            "development_validation_shadow_session_split": [60, 20, 20],
            "purge_overlapping_labels": True,
            "embargo_sessions": 1,
            "bootstrap": {"block_length_sessions": 5, "resamples": 10000, "seed": 27029},
            "familywise_one_sided_alpha": 0.05,
            "scope_family_count": 2,
            "max_drawdown_worsening_pp": 0.25,
            "pilot_sessions": 10,
            "pilot_purpose": "feasibility_only_no_auto_extension",
            "activation_cutoff": "frozen_before_first_outcome",
        },
        "negative_controls": [
            "delayed_or_missing_source_availability",
            "latency_and_partial_nonfill_stress",
            "unknown_cost_and_close_failure",
            "neighborhood_symbol_and_session_perturbation",
            "top_contributor_concentration",
        ],
        "r7": {
            "schedule": {"training_sessions": 60, "validation_sessions": 20, "shadow_sessions": 20},
            "learner": "existing_bounded_v6_learner_only_past_eligible_rows",
            "minimum_drift_sessions": 20,
            "consecutive_breaches_for_rollback": 2,
            "hysteresis": True,
            "single_losing_day_retrain": False,
            "automatic_broker_promotion": False,
            "default_action": "RETAIN_FROZEN_CHAMPION",
        },
        "source_hashes": {name: _file_hash(path) for name, path in source_paths.items()},
        "source_paths": {name: str(path) for name, path in source_paths.items()},
    }
    protocol_body["protocol_hash_sha256"] = _hash(protocol_body)
    return protocol_body


def evaluate_gap_orb15_signal(
    *,
    bars: Sequence[Mapping[str, Any]],
    prior_close: float | None,
    corporate_action_valid: bool,
    close_at: str,
    scope: str = "gap_orb15_continuation_research_v1",
) -> dict[str, Any]:
    """Evaluate one fixed ORB rule without same-bar or hindsight fills."""

    if scope not in SCOPE_IDS:
        return _rejected("unknown_scope")
    if not bars or prior_close is None or not math.isfinite(float(prior_close)) or prior_close <= 0:
        return _rejected("missing_prior_close")
    ordered = sorted((dict(row) for row in bars), key=lambda row: str(row.get("event_at") or row.get("bar_start_at") or ""))
    opening = [row for row in ordered if "09:30" <= _clock(row) < "09:45"]
    later = [row for row in ordered if "09:45" <= _clock(row) < "11:30"]
    if len(opening) < 1:
        return _rejected("opening_range_missing")
    first_open = _number(opening[0].get("open"))
    last_close = _number(opening[-1].get("close"))
    highs = [_number(row.get("high")) for row in opening]
    lows = [_number(row.get("low")) for row in opening]
    if first_open is None or last_close is None or any(value is None for value in highs + lows):
        return _rejected("opening_range_schema_invalid")
    opening_high = max(value for value in highs if value is not None)
    opening_low = min(value for value in lows if value is not None)
    opening_return = last_close / first_open - 1.0
    gap_pct = first_open / float(prior_close) - 1.0
    if opening_return <= 0:
        return _rejected("opening_return_not_positive")
    if scope == "gap_orb15_continuation_research_v1" and gap_pct < 0.0075:
        return _rejected("gap_below_0_75_pct")
    if scope == "gap_orb15_continuation_research_v1" and not corporate_action_valid:
        return _rejected("corporate_action_prior_close_unverified")
    break_row = next((row for row in later if (_number(row.get("high")) or -math.inf) > opening_high), None)
    if break_row is None:
        return _rejected("upside_break_missing_before_11_30")
    if not _chronology_valid(break_row):
        return _rejected("break_availability_invalid")
    entry = _number(break_row.get("executable_ask") or break_row.get("ask"))
    if entry is None or entry <= opening_low:
        return _rejected("executable_quote_missing")
    stop = opening_low
    risk = entry - stop
    if risk <= 0:
        return _rejected("nonpositive_range_risk")
    break_at = _parse_time(break_row.get("event_at") or break_row.get("bar_start_at"))
    close_dt = _parse_time(close_at)
    deadline = close_dt - timedelta(minutes=10)
    maximum_exit = break_at + timedelta(minutes=60)
    exit_deadline = min(maximum_exit, deadline)
    return {
        "status": "SIGNAL",
        "scope": scope,
        "direction": "long",
        "entry_price": entry,
        "stop_price": stop,
        "target_price": entry + 3.0 * risk,
        "risk_per_share": risk,
        "opening_range_high": opening_high,
        "opening_range_low": opening_low,
        "opening_return_pct": opening_return * 100.0,
        "overnight_gap_pct": gap_pct * 100.0,
        "break_event_at": break_at.isoformat(),
        "maximum_exit_at": exit_deadline.isoformat(),
        "close_deadline_at": deadline.isoformat(),
        "same_bar_fill": False,
        "hindsight_range_data": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def replay_account_twr(*, sessions: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Link full-account daily TWR with explicit cash-flow timing and costs."""

    equity = None
    returns: list[float] = []
    equity_curve: list[float] = []
    missing: list[str] = []
    total_fees = 0.0
    total_turnover = 0.0
    exposures: list[float] = []
    losing_days = 0
    valid_no_trade = 0
    for row in sessions:
        sid = str(row.get("session_id") or "")
        if not sid or row.get("ending_equity_after_fees") is None:
            missing.append(sid or "unknown")
            continue
        try:
            ending = float(row["ending_equity_after_fees"])
            flow_before = float(row.get("external_flow_before", 0.0))
            flow_after = float(row.get("external_flow_after", 0.0))
            fees = float(row["fees"])
            turnover = float(row.get("turnover", 0.0))
            exposure = float(row.get("average_gross_exposure", 0.0))
        except (KeyError, TypeError, ValueError):
            missing.append(sid or "unknown")
            continue
        if not all(math.isfinite(value) for value in (ending, flow_before, flow_after, fees, turnover, exposure)):
            missing.append(sid or "unknown")
            continue
        start = float(row.get("starting_equity", equity if equity is not None else ending))
        denominator = start + flow_before
        numerator = ending - flow_after
        if denominator <= 0 or numerator <= 0:
            missing.append(sid or "unknown")
            continue
        daily_return = numerator / denominator - 1.0
        returns.append(daily_return)
        equity = ending
        equity_curve.append(ending)
        total_fees += fees
        total_turnover += turnover
        exposures.append(exposure)
        if daily_return < 0:
            losing_days += 1
        if int(row.get("trade_count", 0) or 0) == 0:
            valid_no_trade += 1
    if not returns:
        return {"status": "NO_VALID_SESSIONS", "returns": [], "missing_sessions": missing}
    linked = math.prod(1.0 + value for value in returns) - 1.0
    peak = equity_curve[0]
    max_drawdown = 0.0
    for value in equity_curve:
        peak = max(peak, value)
        max_drawdown = max(max_drawdown, (peak - value) / peak if peak else 0.0)
    tail = sorted(returns)[: max(1, math.ceil(len(returns) * 0.10))]
    return {
        "status": "COMPLETE" if not missing else "PARTIAL_MISSING_SESSIONS",
        "session_count": len(returns),
        "returns": returns,
        "daily_log_returns": [math.log1p(value) for value in returns],
        "linked_twr_return": linked,
        "ending_equity": equity,
        "total_fees": total_fees,
        "total_turnover": total_turnover,
        "mean_gross_exposure": sum(exposures) / len(exposures),
        "maximum_drawdown": max_drawdown,
        "losing_day_count": losing_days,
        "valid_no_trade_sessions": valid_no_trade,
        "tail_mean_bottom_10pct": sum(tail) / len(tail),
        "missing_sessions": missing,
        "mfe_used": False,
        "price_optimism_used": False,
    }


def evaluate_confirmation_summary(
    *,
    protocol: Mapping[str, Any],
    scope: str,
    common_complete_sessions: int,
    challenger_trade_count: int,
    baseline_trade_count: int,
    paired_mean_daily_log_return: float | None,
    lower_bound_one_sided: float | None,
    drawdown_worsening_pp: float | None,
    critical_coverage_clear: bool,
    cost_valid: bool,
    capacity_valid: bool,
) -> dict[str, Any]:
    """Apply preregistered gates to summary statistics only.

    Raw outcomes must be inspected and frozen by a separately authorized
    evaluator.  This helper never promotes a policy and returns a waiting
    state whenever the required prospective evidence is absent.
    """

    if scope not in SCOPE_IDS:
        return {"status": "REJECTED", "reason": "unknown_scope", "promotion_eligible": False}
    prereg = protocol.get("preregistration") or {}
    reasons: list[str] = []
    if common_complete_sessions < int(prereg.get("confirmation_min_common_complete_sessions", 60)):
        reasons.append("insufficient_common_complete_sessions")
    if challenger_trade_count < int(prereg.get("confirmation_min_hypothetical_trades_per_policy", 30)):
        reasons.append("insufficient_challenger_trades")
    if baseline_trade_count < int(prereg.get("confirmation_min_hypothetical_trades_per_policy", 30)):
        reasons.append("insufficient_baseline_trades")
    if paired_mean_daily_log_return is None or lower_bound_one_sided is None:
        reasons.append("missing_paired_metric_or_interval")
    else:
        if paired_mean_daily_log_return < float(prereg.get("minimum_useful_improvement_bps_per_expected_session", 2.0)) / 10000:
            reasons.append("below_minimum_useful_improvement")
        if lower_bound_one_sided <= 0:
            reasons.append("familywise_lower_bound_not_positive")
    if drawdown_worsening_pp is None or drawdown_worsening_pp > float(prereg.get("max_drawdown_worsening_pp", .25)):
        reasons.append("drawdown_gate_failed_or_unknown")
    if not critical_coverage_clear:
        reasons.append("critical_coverage_or_close_failure")
    if not cost_valid:
        reasons.append("cost_validity_unknown_or_failed")
    if not capacity_valid:
        reasons.append("capacity_invalid_or_unknown")
    return {
        "status": "WAITING_MARKET_EVIDENCE" if reasons else "ELIGIBLE_FOR_SEPARATE_REVIEW",
        "scope": scope,
        "promotion_eligible": False,
        "reasons": reasons,
        "economic_pass": False,
        "outcome_inspection_authority": "separate_Astra_authorized_evaluator",
    }


def build_observer_controller(*, protocol: Mapping[str, Any], eligible_session_count: int) -> dict[str, Any]:
    """Build the D029 retain-by-default controller receipt."""

    enough = int(eligible_session_count) >= 60
    return {
        "schema_version": "dawnstrike.r7.observer_controller.v1",
        "protocol_hash_sha256": protocol.get("protocol_hash_sha256"),
        "champion": "frozen_v5",
        "no_learning_comparator": "cash_no_learning",
        "challenger_scopes": list(SCOPE_IDS),
        "learner": "intraday_scanner.alpha.v6.training",
        "schedule": {"training": 60, "validation": 20, "observer_shadow": 20},
        "eligible_session_count": int(eligible_session_count),
        "status": "READY_FOR_SYNTHETIC_ENGINEERING_ONLY" if enough else "RETAIN_WAITING_MARKET_EVIDENCE",
        "promotion": {
            "observer_only": True,
            "requires_d028_confirmation": True,
            "automatic_broker_promotion": False,
            "rollback_target": "frozen_v5",
        },
        "drift": {
            "categories": ["data_quality", "calibration", "conditional_expectancy", "execution", "regime"],
            "minimum_sessions": 20,
            "consecutive_breaches": 2,
            "hysteresis": True,
            "familywise_one_sided_alpha": 0.05,
            "single_losing_day_retrain": False,
        },
    }


def evaluate_controller_update(
    *,
    controller: Mapping[str, Any],
    candidate: Mapping[str, Any],
) -> dict[str, Any]:
    """Fail closed on provenance, cost, chronology, safety, or evidence gaps."""

    reasons: list[str] = []
    if candidate.get("protocol_hash_sha256") != controller.get("protocol_hash_sha256"):
        reasons.append("protocol_hash_mismatch")
    if candidate.get("cost_status") in {None, "UNKNOWN", "COST_UNKNOWN"}:
        reasons.append("unknown_cost")
    if int(candidate.get("eligible_session_count", 0) or 0) < 60:
        reasons.append("insufficient_sessions")
    if candidate.get("future_inputs") is True:
        reasons.append("future_input")
    if candidate.get("forged_lineage") is True:
        reasons.append("forged_lineage")
    if candidate.get("safety_limits_changed") is True:
        reasons.append("safety_limits_changed")
    if candidate.get("broker_execution_enabled") is True:
        reasons.append("broker_promotion_forbidden")
    return {
        "status": "RETAIN_FROZEN_CHAMPION" if reasons else "OBSERVER_ONLY_REVIEW_REQUIRED",
        "promotion_eligible": False,
        "reasons": reasons,
        "rollback_target": controller.get("champion", "frozen_v5"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def build_income_illustration(
    *,
    illustrative_capital: float,
    annual_return_assumption: float,
    tax_rate: float,
    annual_withdrawal_rate: float,
    annual_cost_rate: float,
    reserve_months: int,
    decay_rate: float,
) -> dict[str, Any]:
    """Return arithmetic only; never infer owner capital or retirement timing."""

    values = (illustrative_capital, annual_return_assumption, tax_rate, annual_withdrawal_rate, annual_cost_rate, decay_rate)
    if illustrative_capital <= 0 or not all(math.isfinite(float(value)) for value in values):
        raise ValueError("illustrative assumptions must be finite and capital must be positive")
    gross_income = illustrative_capital * annual_return_assumption
    costs = illustrative_capital * annual_cost_rate
    taxes = max(0.0, (gross_income - costs) * tax_rate)
    withdrawal = illustrative_capital * annual_withdrawal_rate
    return {
        "status": "ILLUSTRATIVE_ONLY",
        "capital_assumption": illustrative_capital,
        "annual_return_assumption": annual_return_assumption,
        "annual_cost_assumption": annual_cost_rate,
        "annual_tax_assumption": tax_rate,
        "annual_withdrawal_assumption": annual_withdrawal_rate,
        "annual_decay_assumption": decay_rate,
        "reserve_months_assumption": int(reserve_months),
        "gross_annual_result": gross_income,
        "annual_cost_amount": costs,
        "annual_tax_amount": taxes,
        "illustrative_net_income": gross_income - costs - taxes,
        "illustrative_withdrawal": withdrawal,
        "personal_capital_commitment": None,
        "retirement_date": None,
        "risk_budget": None,
        "forecast": False,
    }


def _rejected(reason: str) -> dict[str, Any]:
    return {"status": "REJECTED", "reason": reason, "research_only": True, "broker_execution_enabled": False}


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _clock(row: Mapping[str, Any]) -> str:
    value = str(row.get("event_at") or row.get("bar_start_at") or "")
    return value[11:16] if len(value) >= 16 else ""


def _parse_time(value: Any) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    return datetime.fromisoformat(text)


def _chronology_valid(row: Mapping[str, Any]) -> bool:
    event = row.get("event_at") or row.get("bar_start_at")
    available = row.get("available_at") or row.get("provider_available_at")
    if not event or not available:
        return False
    try:
        return _parse_time(available) >= _parse_time(event)
    except ValueError:
        return False


__all__ = [
    "FIXED_PANEL",
    "LEGACY_REJECTED_STRATEGY",
    "SCOPE_IDS",
    "V5_COST_MODEL_VERSION",
    "build_income_illustration",
    "build_observer_controller",
    "build_research_protocol",
    "evaluate_confirmation_summary",
    "evaluate_controller_update",
    "evaluate_gap_orb15_signal",
    "replay_account_twr",
]
