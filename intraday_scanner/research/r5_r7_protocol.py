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
import random
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, getcontext
from pathlib import Path
from typing import Any, Mapping, Sequence
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.execution_cost import DEFAULT_EXECUTION_COST_MODEL, estimate_round_trip_cost
from intraday_scanner.alpha.outcome_semantics import account_equity_drawdown
from intraday_scanner.alpha.v5_policy import DEFAULT_V5_POLICY, evaluate_v5_official_paper
from intraday_scanner.performance.account_contract import account_session_return_pct

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
_MANIFEST_IDENTITY_KEYS = ("session_id", "universe_manifest_sha256", "source_config_sha256", "raw_events_sha256")


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
    ticker: str = "",
    expected_opening_bars: int = 15,
) -> dict[str, Any]:
    """Evaluate one fixed ORB rule without same-bar or hindsight fills."""

    if scope not in SCOPE_IDS:
        return _rejected("unknown_scope")
    if not bars or prior_close is None or not math.isfinite(float(prior_close)) or prior_close <= 0:
        return _rejected("missing_prior_close")
    if scope == "panel_orb15_continuation_research_v1" and ticker.upper() not in FIXED_PANEL:
        return _rejected("panel_membership_invalid")
    ordered = sorted((dict(row) for row in bars), key=lambda row: str(row.get("event_at") or row.get("bar_start_at") or ""))
    if any(not _chronology_valid(row) for row in ordered):
        return _rejected("source_availability_or_ingestion_invalid")
    opening = [row for row in ordered if "09:30" <= _clock(row) < "09:45"]
    later = [row for row in ordered if "09:45" <= _clock(row) < "11:30"]
    if len(opening) < expected_opening_bars:
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
    # Break detection uses a later quote only.  A later bar high is not a
    # decision-time executable observation and therefore cannot trigger entry.
    break_row = next(
        (
            row for row in later
            if _number(row.get("ask") or row.get("executable_ask")) is not None
            and _number(row.get("ask") or row.get("executable_ask")) > opening_high
        ),
        None,
    )
    if break_row is None:
        return _rejected("upside_break_missing_before_11_30")
    if not _chronology_valid(break_row):
        return _rejected("break_availability_invalid")
    entry = _number(break_row.get("ask") or break_row.get("executable_ask"))
    if entry is None or entry <= opening_low:
        return _rejected("executable_quote_missing")
    stop = opening_low
    risk = entry - stop
    if risk <= 0:
        return _rejected("nonpositive_range_risk")
    break_at = _parse_time(break_row.get("event_at") or break_row.get("bar_start_at"))
    close_dt = _parse_time(close_at)
    decision_at = _parse_time(break_row.get("ingested_at") or break_row.get("available_at") or break_row.get("event_at"))
    # Every input used by the decision must have arrived by that decision.
    if any(_parse_time(row.get("ingested_at")) > decision_at for row in opening + [break_row]):
        return _rejected("decision_input_ingested_after_decision")
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
        "decision_at": decision_at.isoformat(),
        "ticker": ticker.upper(),
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
    missing: list[str] = []
    total_fees = 0.0
    total_turnover = 0.0
    exposures: list[float] = []
    losing_days = 0
    valid_no_trade = 0
    for row in sessions:
        sid = str(row.get("session_id") or "")
        if not sid or row.get("starting_equity_cents") is None or row.get("ending_equity_cents") is None or "external_flow_cents" not in row:
            missing.append(sid or "unknown")
            continue
        try:
            beginning_cents = int(row["starting_equity_cents"])
            ending_cents = int(row["ending_equity_cents"])
            flow_cents = int(row["external_flow_cents"])
            fees = float(row["fees"])
            turnover = float(row.get("turnover", 0.0))
            exposure = float(row.get("average_gross_exposure", 0.0))
        except (KeyError, TypeError, ValueError):
            missing.append(sid or "unknown")
            continue
        if not all(math.isfinite(value) for value in (fees, turnover, exposure)):
            missing.append(sid or "unknown")
            continue
        if beginning_cents <= 0 or ending_cents <= 0:
            missing.append(sid or "unknown")
            continue
        validation = account_session_return_pct(
            beginning_equity_cents=beginning_cents,
            ending_equity_cents=ending_cents,
            external_flow_cents=flow_cents,
        )
        if validation is None:
            missing.append(sid or "unknown")
            continue
        daily_return = float(validation / Decimal("100"))
        returns.append(daily_return)
        equity = ending_cents
        total_fees += fees
        total_turnover += turnover
        exposures.append(exposure)
        if daily_return < 0:
            losing_days += 1
        if int(row.get("trade_count", 0) or 0) == 0:
            valid_no_trade += 1
    if not returns:
        return {"status": "NO_VALID_SESSIONS", "returns": [], "missing_sessions": missing}
    accounting_rows = []
    for row in sessions:
        if row.get("starting_equity_cents") is None or row.get("ending_equity_cents") is None or "external_flow_cents" not in row:
            continue
        accounting_rows.append({
            "account_equity": row["ending_equity_cents"],
            "cash_flow": row["external_flow_cents"],
            "cash_flow_timing": row.get("cash_flow_timing", "start"),
            "valuation_currency": row.get("valuation_currency", "USD"),
        })
    unitized_drawdown = account_equity_drawdown(accounting_rows)
    if unitized_drawdown is None:
        return {"status": "INVALID_FLOW_EVIDENCE", "returns": returns, "missing_sessions": missing}
    linked = math.prod(1.0 + value for value in returns) - 1.0
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
        "maximum_drawdown": abs(float(unitized_drawdown)) / 100.0,
        "losing_day_count": losing_days,
        "valid_no_trade_sessions": valid_no_trade,
        "tail_mean_bottom_10pct": sum(tail) / len(tail),
        "missing_sessions": missing,
        "mfe_used": False,
        "price_optimism_used": False,
    }


def bootstrap_paired_session_returns(
    *,
    challenger_returns: Sequence[float],
    baseline_returns: Sequence[float],
    block_length: int = 5,
    resamples: int = 10_000,
    seed: int = 27_029,
) -> dict[str, Any]:
    """Compute the preregistered fixed-block paired log-return interval.

    The samples are derived from the two supplied account/session ledgers.  No
    caller-provided interval or significance flag is accepted.  Incomplete
    blocks are excluded rather than padded with zero performance.
    """

    if block_length < 1 or resamples < 1 or len(challenger_returns) != len(baseline_returns):
        return {"status": "INVALID_INPUT", "reason": "paired_series_shape_invalid"}
    if len(challenger_returns) < block_length:
        return {"status": "WAITING", "reason": "insufficient_complete_block_sessions"}
    diffs: list[float] = []
    for challenger, baseline in zip(challenger_returns, baseline_returns):
        c = _number(challenger)
        b = _number(baseline)
        if c is None or b is None or c <= -1.0 or b <= -1.0:
            return {"status": "INVALID_INPUT", "reason": "nonfinite_or_invalid_return"}
        diffs.append(math.log1p(c) - math.log1p(b))
    block_count = len(diffs) // block_length
    blocks = [diffs[index * block_length : (index + 1) * block_length] for index in range(block_count)]
    if not blocks:
        return {"status": "WAITING", "reason": "no_complete_blocks"}
    observed = sum(diffs) / len(diffs)
    rng = random.Random(seed)
    samples: list[float] = []
    for _ in range(resamples):
        picked = [blocks[rng.randrange(block_count)] for _ in range(block_count)]
        samples.append(sum(value for block in picked for value in block) / len(diffs))
    samples.sort()
    lower_index = min(len(samples) - 1, max(0, math.floor(0.05 * len(samples))))
    return {
        "status": "COMPLETE",
        "block_length_sessions": block_length,
        "complete_block_count": block_count,
        "session_count": len(diffs),
        "resamples": resamples,
        "seed": seed,
        "observed_mean_daily_log_return": observed,
        "one_sided_lower_bound_95": samples[lower_index],
        "caller_interval_ignored": True,
    }


def admit_d022_trade(*, proposal: Mapping[str, Any], portfolio: Mapping[str, Any], wrapper: Mapping[str, Any]) -> dict[str, Any]:
    """Execute the D022 aggregate admission boundary on current+proposed state."""

    required = ("symbol", "notional", "open_risk_pct", "sector_theme", "direction")
    if any(key not in proposal for key in required):
        return {"status": "REJECTED", "reason": "proposal_state_missing"}
    if any(key not in portfolio for key in ("gross_pct", "net_pct", "open_risk_pct", "sector_theme_pct", "daily_loss_pct", "drawdown_pct", "concurrent_positions", "entries")):
        return {"status": "REJECTED", "reason": "portfolio_state_missing"}
    wrapper_keys = ("max_symbol_notional_pct", "gross_exposure_pct", "net_exposure_pct", "sector_theme_exposure_pct", "aggregate_open_risk_pct", "daily_loss_stop_pct", "drawdown_stop_pct", "max_concurrent_positions", "max_entries_per_session")
    if any(key not in wrapper for key in wrapper_keys):
        return {"status": "REJECTED", "reason": "wrapper_state_missing"}
    wrapper_values = [_number(wrapper.get(key)) for key in wrapper_keys]
    if any(value is None or value < 0 for value in wrapper_values):
        return {"status": "REJECTED", "reason": "wrapper_state_nonfinite"}
    values = [proposal.get("notional"), proposal.get("open_risk_pct"), portfolio.get("gross_pct"), portfolio.get("net_pct"), portfolio.get("open_risk_pct"), portfolio.get("sector_theme_pct"), portfolio.get("daily_loss_pct"), portfolio.get("drawdown_pct")]
    if any(_number(value) is None for value in values):
        return {"status": "REJECTED", "reason": "portfolio_state_nonfinite"}
    if str(proposal.get("direction")).lower() != "long":
        return {"status": "REJECTED", "reason": "long_only"}
    symbol_pct = float(proposal["notional"])
    proposed_risk = float(proposal["open_risk_pct"])
    theme_pct = float(portfolio["sector_theme_pct"])
    if symbol_pct < 0 or proposed_risk < 0 or theme_pct < 0:
        return {"status": "REJECTED", "reason": "portfolio_state_negative"}
    try:
        concurrent = int(portfolio["concurrent_positions"])
        entries = int(portfolio["entries"])
    except (TypeError, ValueError):
        return {"status": "REJECTED", "reason": "portfolio_count_invalid"}
    if concurrent < 0 or entries < 0 or concurrent != float(portfolio["concurrent_positions"]) or entries != float(portfolio["entries"]):
        return {"status": "REJECTED", "reason": "portfolio_count_invalid"}
    checks = {
        "symbol_notional": symbol_pct <= float(wrapper["max_symbol_notional_pct"]),
        "gross": float(portfolio["gross_pct"]) + symbol_pct <= float(wrapper["gross_exposure_pct"]),
        "net": float(portfolio["net_pct"]) + symbol_pct <= float(wrapper["net_exposure_pct"]),
        "sector_theme": theme_pct + symbol_pct <= float(wrapper["sector_theme_exposure_pct"]),
        "open_risk": float(portfolio["open_risk_pct"]) + proposed_risk <= float(wrapper["aggregate_open_risk_pct"]),
        "daily_loss": float(portfolio["daily_loss_pct"]) < float(wrapper["daily_loss_stop_pct"]),
        "drawdown": float(portfolio["drawdown_pct"]) < float(wrapper["drawdown_stop_pct"]),
        "concurrency": concurrent < int(wrapper["max_concurrent_positions"]),
        "entries": entries < int(wrapper["max_entries_per_session"]),
    }
    return {"status": "ADMITTED" if all(checks.values()) else "REJECTED", "checks": checks, "policy": "D022_intersection"}


def run_v5_admission(*, signal: Mapping[str, Any], observation: Mapping[str, Any]) -> dict[str, Any]:
    """Run the existing frozen V5 admission function and preserve its trace."""

    decision = evaluate_v5_official_paper(dict(signal), dict(observation), policy=DEFAULT_V5_POLICY)
    return decision.to_dict()


def compare_v5_baseline_challenger(
    *, signal: Mapping[str, Any], observation: Mapping[str, Any],
    challenger_signal: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Invoke the frozen V5 baseline and challenger on one bound input.

    The challenger may alter only its strategy payload; observation, policy
    identity, risk fields and cost model remain explicit in the comparison.
    """
    baseline = run_v5_admission(signal=signal, observation=observation)
    challenger = run_v5_admission(signal=challenger_signal or signal, observation=observation)
    shared = {
        "observation_sha256": _hash(observation),
        "risk_policy_version": DEFAULT_V5_POLICY.policy_version,
        "cost_model_version": DEFAULT_V5_POLICY.cost_model_version,
        "risk_fields_equal": all(signal.get(key) == (challenger_signal or signal).get(key) for key in ("entry_watch_level", "invalidation_level", "target_1")),
    }
    return {
        "status": "COMPLETE",
        "baseline": baseline,
        "challenger": challenger,
        "shared_binding": shared,
        "equal_input_risk_cost": shared["risk_fields_equal"] and baseline.get("policy_version") == challenger.get("policy_version"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def run_r7_weekly_controller(
    *, dataset: Mapping[str, Any], protocol: Mapping[str, Any], code_sha: str,
    prior_state: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run one offline D029 weekly controller against identity-bound rows."""
    from intraday_scanner.alpha.v6.calibration import calibration_report
    from intraday_scanner.alpha.v6.contracts import canonical_hash
    from intraday_scanner.alpha.v6.drift import build_drift_report
    from intraday_scanner.alpha.v6.training import train_shadow_challengers, walk_forward_challenger_predictions

    rows = [dict(row) for row in list(dataset.get("rows") or [])]
    if str(dataset.get("dataset_hash_sha256") or "") == "" or not code_sha:
        return {"status": "QUARANTINED", "reason": "dataset_or_code_identity_missing", "research_only": True}
    ordered = sorted(rows, key=lambda row: (str(row.get("market_date") or ""), str(row.get("decision_id") or "")))
    dates = sorted({str(row.get("market_date") or "") for row in ordered})
    if any(not row.get("decision_id") or not row.get("market_date") or not row.get("source_artifact_hash_sha256") for row in ordered):
        return {"status": "QUARANTINED", "reason": "row_lineage_missing", "research_only": True}
    if any(not _decision_chronology_valid(row) for row in ordered):
        return {"status": "QUARANTINED", "reason": "future_or_invalid_chronology", "research_only": True}
    if len(dates) < 100:
        return {"status": "RETAIN_WAITING_MARKET_EVIDENCE", "reason": "insufficient_60_20_20_sessions", "session_count": len(dates), "research_only": True, "broker_execution_enabled": False}
    training_dates = dates[:60]
    validation_dates = dates[60:80]
    shadow_dates = dates[80:100]
    training_cutoff = training_dates[-1]
    receipt = train_shadow_challengers(dict(dataset), code_sha=code_sha)
    model_id = "v6m-" + _hash({"dataset": dataset.get("dataset_hash_sha256"), "cutoff": training_cutoff, "code": code_sha})[:28]
    predictions = walk_forward_challenger_predictions(dict(dataset), model_run_id=model_id)
    calibration = calibration_report([row for row in predictions if "activation_probability" in row])
    split = max(20, len(dates) // 2)
    baseline_dates = dates[:split]
    recent_dates = dates[split:]
    baseline_rows = [row for row in ordered if str(row.get("market_date")) in baseline_dates]
    recent_rows = [row for row in ordered if str(row.get("market_date")) in recent_dates]
    def _dimension_rows(rows_for_dim: list[dict[str, Any]], dimension: str) -> list[dict[str, Any]]:
        return [{**row, "source": row.get("source_artifact_hash_sha256"), "feature_schema_version": dataset.get("feature_schema_version"), "cost_model_version": row.get("cost_model_version", V5_COST_MODEL_VERSION), "regime": row.get("regime_key", "unknown"), "decision_id": row.get("decision_id")} for row in rows_for_dim]
    baseline_drift_rows = _dimension_rows(baseline_rows, "baseline")
    recent_drift_rows = _dimension_rows(recent_rows, "recent")
    reference_window = {"start": baseline_dates[0], "end": baseline_dates[-1], "market_dates": baseline_dates}
    recent_window = {"start": recent_dates[0], "end": recent_dates[-1], "market_dates": recent_dates}
    diagnostic = build_drift_report(
        baseline_rows=baseline_drift_rows, current_rows=recent_drift_rows,
        reference_window=reference_window, recent_window=recent_window,
        config={"protocol_hash": protocol.get("protocol_hash_sha256")}, source={"dataset_hash": dataset.get("dataset_hash_sha256")},
        config_hash_sha256=_hash({"protocol_hash": protocol.get("protocol_hash_sha256")}), source_hash_sha256=_hash({"dataset_hash": dataset.get("dataset_hash_sha256")}),
        window_hash_sha256=canonical_hash({"reference": reference_window, "recent": recent_window}),
        input_hash_sha256=canonical_hash({"reference": sorted(baseline_drift_rows, key=canonical_hash), "recent": sorted(recent_drift_rows, key=canonical_hash)}),
        code_sha=code_sha, minimum_observations=20, minimum_market_sessions=5,
    )
    diagnostic_dimensions = {}
    for name, required_fields in {
        "data_quality": ("source_artifact_hash_sha256",),
        "calibration": ("activation_probability", "activation_label"),
        "conditional_expectancy": ("realized_net_excess_return_pct",),
        "execution": ("cost_model_version", "cost_status"),
        "regime": ("regime_key",),
    }.items():
        observed = sum(1 for row in ordered if all(row.get(field) not in {None, ""} for field in required_fields))
        diagnostic_dimensions[name] = {"observations": observed, "status": "EVALUABLE" if observed >= 20 else "UNKNOWN_INSUFFICIENT_OBSERVATIONS", "minimum_observations": 20}
    breaches = [row for row in ordered if _number(row.get("diagnostic_value")) is not None and float(row["diagnostic_value"]) < 0.5]
    consecutive = len(breaches) >= 2 and str(breaches[-1].get("market_date")) > str(breaches[-2].get("market_date"))
    prior = str((prior_state or {}).get("status") or "RETAIN_FROZEN_CHAMPION")
    if diagnostic.get("status", "").startswith("QUARANTINE") or consecutive:
        status = "ROLLBACK_FROZEN_CHAMPION" if prior == "OBSERVER_ONLY" else "RETAIN_FROZEN_CHAMPION"
    elif receipt.get("status", "").startswith("TRAINED") and diagnostic.get("status") == "STABLE":
        status = "OBSERVER_ONLY"
    else:
        status = "RETAIN_WAITING_MARKET_EVIDENCE"
    return {
        "status": status, "schedule": {"training_dates": training_dates, "validation_dates": validation_dates, "shadow_dates": shadow_dates},
        "model_run_id": model_id, "training_cutoff": training_cutoff, "training_receipt": receipt,
        "predictions": predictions, "calibration": calibration, "diagnostics": diagnostic,
        "diagnostic_dimensions": diagnostic_dimensions,
        "phases": {"collect": len(ordered), "validate": len(ordered), "mature": sum(1 for row in ordered if row.get("label_available_at") and str(row.get("label_available_at"))[:10] <= training_cutoff), "diagnose": True, "propose": True, "train": receipt.get("status"), "validate_predictions": sum(1 for row in predictions if str(row.get("market_date") or "") in validation_dates), "shadow_predictions": sum(1 for row in predictions if str(row.get("market_date") or "") in shadow_dates), "paired_comparison": "DERIVED_FROM_BOUND_ROWS"},
        "breach_count": len(breaches), "consecutive_breaches": consecutive, "rollback_target": "frozen_v5",
        "activation_time": None if status != "OBSERVER_ONLY" else datetime.now().astimezone().isoformat(),
        "trial_history": [{"trial_id": "fixed-d029-1", "dataset_hash": dataset.get("dataset_hash_sha256"), "status": status}],
        "research_only": True, "broker_execution_enabled": False, "economic_pass": False,
    }


def simulate_causal_fill_lifecycle(
    *,
    manifest: Mapping[str, Any],
    decision: Mapping[str, Any],
    signal: Mapping[str, Any],
    quotes: Sequence[Mapping[str, Any]],
    path_events: Sequence[Mapping[str, Any]],
    portfolio: Mapping[str, Any],
    wrapper: Mapping[str, Any],
    desired_quantity: int,
    v5_signal: Mapping[str, Any] | None = None,
    v5_observation: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Journal a no-network hypothetical entry and causal exit lifecycle."""

    journal: list[dict[str, Any]] = []
    def finish(payload: dict[str, Any]) -> dict[str, Any]:
        payload["journal"] = journal
        payload["journal_sha256"] = _hash(journal)
        return payload

    if str(manifest.get("status") or "") != "COMPLETE":
        return finish({"status": "MISSING", "reason": "manifest_not_complete"})
    if not _manifest_identity_valid(manifest):
        return finish({"status": "MISSING", "reason": "manifest_identity_missing"})
    journal.append({"kind": "raw_manifest", "manifest_identity": {key: manifest[key] for key in _MANIFEST_IDENTITY_KEYS}})
    if not _decision_chronology_valid(decision):
        return finish({"status": "REJECTED", "reason": "decision_chronology_invalid"})
    if desired_quantity < 1:
        return finish({"status": "REJECTED", "reason": "quantity_invalid"})
    if v5_signal is not None:
        v5_trace = run_v5_admission(signal=v5_signal, observation=v5_observation or {})
        journal.append({"kind": "v5_admission", "payload": v5_trace})
        if v5_trace.get("eligible_for_official_paper") is not True:
            return finish({"status": "REJECTED", "reason": "v5_admission", "v5_trace": v5_trace})
    admission = admit_d022_trade(proposal=signal, portfolio=portfolio, wrapper=wrapper)
    journal.append({"kind": "admission", "payload": admission})
    if admission.get("status") != "ADMITTED":
        return finish({"status": "REJECTED", "reason": "d022_admission", "admission": admission})
    decision_at = _parse_time(decision["decision_at"])
    quote = next((dict(row) for row in sorted(quotes, key=lambda row: str(row.get("event_at") or "")) if _causal_quote(row, decision_at)), None)
    if quote is None:
        return finish({"status": "UNFILLED", "reason": "entry_quote_missing_or_noncausal", "admission": admission})
    quantity = min(desired_quantity, int(quote.get("fill_quantity") or desired_quantity))
    if quantity <= 0:
        return finish({"status": "UNFILLED", "reason": "partial_nonfill_zero", "admission": admission})
    entry_raw = _number(quote.get("ask"))
    if entry_raw is None:
        return finish({"status": "UNFILLED", "reason": "entry_ask_missing", "admission": admission})
    entry_cost = estimate_round_trip_cost(entry_raw, entry_raw, quantity, model=DEFAULT_EXECUTION_COST_MODEL)
    entry_fill = entry_cost.entry_fill_price
    journal.append({"kind": "entry_fill", "event_at": quote.get("event_at"), "quantity": quantity, "raw_price": entry_raw, "fill_price": entry_fill})
    deadline = _parse_time(str(signal["maximum_exit_at"]))
    exit_event = None
    exit_reason = "deadline"
    for row in sorted(path_events, key=lambda value: str(value.get("event_at") or "")):
        if not _causal_path(row, decision_at, entry_at=_parse_time(str(quote["event_at"]))):
            continue
        at = _parse_time(row["event_at"])
        if at > deadline:
            continue
        if row.get("halt") is True:
            return finish({"status": "CENSORED", "reason": "halt_before_exit", "quantity": quantity})
        high = _number(row.get("high")); low = _number(row.get("low"))
        if high is not None and low is not None and low <= float(signal["stop_price"]) and high >= float(signal["target_price"]):
            return finish({"status": "AMBIGUOUS", "reason": "same_bar_stop_target", "quantity": quantity})
        if low is not None and low <= float(signal["stop_price"]):
            exit_event, exit_reason = row, "stop"
            break
        if high is not None and high >= float(signal["target_price"]):
            exit_event, exit_reason = row, "target"
            break
        if at == deadline:
            exit_event, exit_reason = row, "close_deadline"
            break
    if exit_event is None:
        return finish({"status": "CENSORED", "reason": "exit_path_missing", "quantity": quantity})
    exit_raw = _number(exit_event.get("close") or exit_event.get("bid"))
    if exit_raw is None:
        return finish({"status": "CENSORED", "reason": "exit_price_missing", "quantity": quantity})
    round_trip = estimate_round_trip_cost(entry_raw, exit_raw, quantity, model=DEFAULT_EXECUTION_COST_MODEL)
    journal.append({"kind": "exit_fill", "event_at": exit_event.get("event_at"), "reason": exit_reason, "quantity": quantity, "raw_price": exit_raw, "fill_price": round_trip.exit_fill_price, "fees": round_trip.commission})
    gross_pnl = (exit_raw - entry_raw) * quantity
    return finish({"status": "COMPLETE", "exit_reason": exit_reason, "quantity": quantity, "entry_fill": entry_fill, "exit_fill": round_trip.exit_fill_price, "fees": round_trip.commission, "total_cost": round_trip.total_cost, "gross_pnl": gross_pnl, "net_pnl": gross_pnl - round_trip.total_cost, "cost_model_version": round_trip.model_version, "cost_status": round_trip.status, "economic_eligibility": "WAITING_COST_EVIDENCE", "financial_pass": False, "research_only": True, "broker_execution_enabled": False, "position_flat": True})


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
    period_returns: Sequence[float] | None = None,
    fixed_withdrawal: float | None = None,
) -> dict[str, Any]:
    """Run an explicit illustrative multi-period cash model.

    All inputs are hypothetical.  The return sequence is replayed in its
    supplied order and in reverse order to expose sequence risk; no owner
    capital, forecast, capacity or retirement date is inferred.
    """

    values = (illustrative_capital, annual_return_assumption, tax_rate, annual_withdrawal_rate, annual_cost_rate, decay_rate)
    if illustrative_capital <= 0 or not all(math.isfinite(float(value)) for value in values):
        raise ValueError("illustrative assumptions must be finite and capital must be positive")
    returns = list(period_returns) if period_returns is not None else [annual_return_assumption]
    if not returns or any(not math.isfinite(float(value)) or float(value) <= -1 for value in returns):
        raise ValueError("period_returns must be finite and greater than -100 percent")
    withdrawal = float(fixed_withdrawal) if fixed_withdrawal is not None else illustrative_capital * annual_withdrawal_rate
    if not math.isfinite(withdrawal) or withdrawal < 0:
        raise ValueError("fixed_withdrawal must be finite and non-negative")
    reserve_floor = illustrative_capital * max(0, int(reserve_months)) / 12 * annual_withdrawal_rate
    def replay(sequence: Sequence[float]) -> dict[str, Any]:
        balance = illustrative_capital
        periods: list[dict[str, Any]] = []
        losing_periods = 0
        for index, rate in enumerate(sequence, start=1):
            start = balance
            gross = start * float(rate)
            costs = start * annual_cost_rate
            taxable = gross - costs
            taxes = max(0.0, taxable * tax_rate)
            ending = max(0.0, start + gross - costs - taxes - withdrawal)
            if gross < 0:
                losing_periods += 1
            periods.append({"period": index, "starting_capital": start, "return_rate": float(rate), "gross_result": gross, "costs": costs, "taxes": taxes, "withdrawal": withdrawal, "ending_capital": ending})
            balance = ending
        return {"ending_capital": balance, "periods": periods, "losing_period_count": losing_periods}
    forward = replay(returns)
    reverse = replay(list(reversed(returns)))
    gross_income = illustrative_capital * annual_return_assumption
    costs = illustrative_capital * annual_cost_rate
    taxes = max(0.0, (gross_income - costs) * tax_rate)
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
        "period_returns": returns,
        "forward_replay": forward,
        "reverse_replay": reverse,
        "sequence_difference": forward["ending_capital"] - reverse["ending_capital"],
        "reserve_floor_assumption": reserve_floor,
        "capacity_status": "UNKNOWN_NO_CAPACITY_EVIDENCE",
        "personal_capital_commitment": None,
        "retirement_date": None,
        "risk_budget": None,
        "forecast": False,
    }


def _rejected(reason: str) -> dict[str, Any]:
    return {"status": "REJECTED", "reason": reason, "research_only": True, "broker_execution_enabled": False}


def _manifest_identity_valid(manifest: Mapping[str, Any]) -> bool:
    """Require a source-bound manifest identity before hypothetical fills."""

    if not str(manifest.get("session_id") or "").strip():
        return False
    for key in _MANIFEST_IDENTITY_KEYS[1:]:
        value = str(manifest.get(key) or "").lower()
        if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
            return False
    return True


def _number(value: Any) -> float | None:
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def _clock(row: Mapping[str, Any]) -> str:
    value = str(row.get("event_at") or row.get("bar_start_at") or "")
    try:
        return _parse_time(value).astimezone(ZoneInfo("America/New_York")).strftime("%H:%M")
    except ValueError:
        return ""


def _parse_time(value: Any) -> datetime:
    text = str(value or "").replace("Z", "+00:00")
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timezone-aware timestamp required")
    return parsed


def _chronology_valid(row: Mapping[str, Any]) -> bool:
    event = row.get("event_at") or row.get("bar_start_at")
    available = row.get("available_at") or row.get("provider_available_at")
    ingested = row.get("ingested_at")
    if not event or not available or not ingested:
        return False
    try:
        return _parse_time(event) <= _parse_time(available) <= _parse_time(ingested)
    except ValueError:
        return False


def _decision_chronology_valid(row: Mapping[str, Any]) -> bool:
    event = row.get("feature_event_at") or row.get("feature_timestamp")
    available = row.get("feature_available_at") or row.get("provider_available_at")
    ingested = row.get("feature_ingested_at") or row.get("ingested_at")
    decision = row.get("decision_at")
    if not event or not available or not ingested or not decision:
        return False
    try:
        return _parse_time(event) <= _parse_time(available) <= _parse_time(ingested) <= _parse_time(decision)
    except ValueError:
        return False


def _causal_quote(row: Mapping[str, Any], decision_at: datetime) -> bool:
    if _number(row.get("ask")) is None or _number(row.get("bid")) is None:
        return False
    event = row.get("event_at")
    available = row.get("provider_available_at") or row.get("available_at")
    ingested = row.get("ingested_at")
    if not event or not available or not ingested:
        return False
    try:
        return _parse_time(event) >= decision_at and _parse_time(event) <= _parse_time(available) <= _parse_time(ingested)
    except ValueError:
        return False


def _causal_path(row: Mapping[str, Any], decision_at: datetime, *, entry_at: datetime) -> bool:
    event = row.get("event_at")
    available = row.get("provider_available_at") or row.get("available_at")
    ingested = row.get("ingested_at")
    if not event or not available or not ingested:
        return False
    try:
        at = _parse_time(event)
        return at >= entry_at and _parse_time(available) >= at and _parse_time(ingested) >= _parse_time(available)
    except ValueError:
        return False


__all__ = [
    "FIXED_PANEL",
    "LEGACY_REJECTED_STRATEGY",
    "SCOPE_IDS",
    "V5_COST_MODEL_VERSION",
    "build_income_illustration",
    "admit_d022_trade",
    "bootstrap_paired_session_returns",
    "compare_v5_baseline_challenger",
    "build_observer_controller",
    "build_research_protocol",
    "evaluate_confirmation_summary",
    "evaluate_controller_update",
    "evaluate_gap_orb15_signal",
    "run_v5_admission",
    "run_r7_weekly_controller",
    "simulate_causal_fill_lifecycle",
    "replay_account_twr",
]
