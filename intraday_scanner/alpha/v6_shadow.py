"""Deterministic, research-only AlphaOps V6 shadow learning contracts.

V6 does not replace V5 and it does not send a recommendation or place an
order.  It records the exact decision-time evidence for a conservative shadow
cohort, then learns only from sourced, point-in-time, after-cost paper labels.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from statistics import mean, pstdev
from typing import Any

from intraday_scanner.alpha.canonical_return_truth import (
    CURRENT_RETURN_TRUTH,
    canonical_return_truth_projection,
    classify_canonical_return_truth,
)
from intraday_scanner.alpha.fill_truth import (
    MISSING_COMMITTED_FILL_TRUTH,
    has_authenticated_committed_fill_truth,
)
from intraday_scanner.alpha.v6.contracts import (
    ALPHAOPS_V6_MODEL_VERSION,
    ALPHAOPS_V6_STRATEGY_VERSION,
    FEATURE_SCHEMA_VERSION,
    V6_COST_MODEL_VERSION,
    is_valid_code_sha,
    is_valid_sha256,
)
from intraday_scanner.alpha.v6.training import predict_from_frozen_model_run
from intraday_scanner.alpha.v6.validation import (
    aggregate_daily_returns,
    daily_weighting_status,
)
from intraday_scanner.services.benchmark_service import (
    alphaops_v6_benchmark_policy,
    benchmark_coverage,
)
from intraday_scanner.services.outcome_capture_contract import classify_missing_capture

MIN_TRAINING_OUTCOMES = 30
MIN_GROUP_OUTCOMES = 12
MIN_FORWARD_SESSIONS = 60
MIN_FORWARD_CLOSED_TRADES = 100
_PASSING_GATES = {"PASS", "ALLOWED", "CLEAR"}


@dataclass(frozen=True)
class V6Prediction:
    status: str
    activation_probability: float | None
    conditional_net_excess_return_pct: float | None
    tail_loss_pct: float | None
    utility_lcb_pct: float | None
    sample_size: int
    group_key: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "activation_probability": _round(self.activation_probability),
            "conditional_net_excess_return_pct": _round(self.conditional_net_excess_return_pct),
            "tail_loss_pct": _round(self.tail_loss_pct),
            "utility_lcb_pct": _round(self.utility_lcb_pct),
            "sample_size": self.sample_size,
            "group_key": self.group_key,
            "research_only": True,
            "broker_execution_enabled": False,
        }


class V6EmpiricalShadowModel:
    """Small-sample-shrunk conditional utility estimator.

    The model is deliberately simple and inspectable.  It is calibrated only
    on historical labels whose outcome receipt is sourced and whose date is
    earlier than the candidate's decision date.
    """

    def __init__(self, rows: Iterable[dict[str, Any]] = ()) -> None:
        self.rows = [row for row in rows if row.get("learning_eligible") is True]
        self.by_group: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for row in self.rows:
            self.by_group[_group_key(row)].append(row)

    def predict(self, decision: dict[str, Any]) -> V6Prediction:
        group = _group_key(decision)
        rows = self.by_group.get(group, [])
        if len(self.rows) < MIN_TRAINING_OUTCOMES:
            return V6Prediction(
                status="UNCALIBRATED_INSUFFICIENT_OUTCOMES",
                activation_probability=None,
                conditional_net_excess_return_pct=None,
                tail_loss_pct=None,
                utility_lcb_pct=None,
                sample_size=len(self.rows),
                group_key=group,
            )
        sample = rows if len(rows) >= MIN_GROUP_OUTCOMES else self.rows
        activated = [row for row in sample if row.get("activation_status") == "ACTIVATED"]
        returns: list[float] = []
        for row in activated:
            value = _number(row.get("net_excess_return_pct"))
            if value is not None:
                returns.append(value)
        if not returns:
            return V6Prediction(
                status="UNCALIBRATED_NO_CONDITIONAL_RETURNS",
                activation_probability=None,
                conditional_net_excess_return_pct=None,
                tail_loss_pct=None,
                utility_lcb_pct=None,
                sample_size=len(sample),
                group_key=group,
            )
        activation_probability = (len(activated) + 1) / (len(sample) + 2)
        conditional = mean(returns)
        standard_error = pstdev(returns) / math.sqrt(len(returns)) if len(returns) > 1 else None
        tail = _tail_mean(returns)
        # Utility uses the conservative lower confidence bound of expected
        # activated return and a full tail-loss penalty.  It cannot override a
        # safety veto and is displayed only as shadow research.
        lower_conditional = (
            conditional - 1.96 * standard_error if standard_error is not None else None
        )
        utility = (
            activation_probability * lower_conditional
            + (1 - activation_probability) * 0.0
            - max(0.0, -(tail or 0.0))
            if lower_conditional is not None
            else None
        )
        return V6Prediction(
            status="CALIBRATED" if utility is not None else "UNCALIBRATED_LOW_VARIANCE_SAMPLE",
            activation_probability=activation_probability,
            conditional_net_excess_return_pct=conditional,
            tail_loss_pct=tail,
            utility_lcb_pct=utility,
            sample_size=len(sample),
            group_key=group,
        )


def build_v6_shadow_decisions(
    *,
    signals: list[dict[str, Any]],
    feature_vectors: list[dict[str, Any]],
    source_summary: dict[str, Any],
    regime: dict[str, Any],
    prior_outcomes: list[dict[str, Any]],
    frozen_model_run: dict[str, Any] | None = None,
    universe_membership_by_ticker: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Build immutable decision-time V6 records for every ranked candidate."""

    feature_by_ticker = {str(row.get("ticker") or "").upper(): row for row in feature_vectors}
    memberships = universe_membership_by_ticker or {}
    output: list[dict[str, Any]] = []
    for signal in signals:
        ticker = str(signal.get("ticker") or "").upper()
        feature = feature_by_ticker.get(ticker, {})
        decision_at = str(signal.get("timestamp") or signal.get("generated_at") or "")
        if not ticker or not decision_at:
            continue
        source_signal_id = str(signal.get("signal_id") or signal.get("signal_key") or "")
        if not source_signal_id:
            continue
        membership = _universe_membership(memberships.get(ticker))
        vetoes = _safety_vetoes(signal, feature, source_summary, membership)
        # Enforce the point-in-time boundary even if a caller accidentally
        # supplies a mixed historical/future outcome collection.
        model = V6EmpiricalShadowModel(
            row
            for row in prior_outcomes
            if str(row.get("market_date") or "")[:10] < decision_at[:10]
        )
        feature_hash = _hash(feature)
        draft = {
            "scan_id": str(signal.get("scan_id") or feature.get("scan_id") or ""),
            "source_signal_id": source_signal_id,
            "market_date": decision_at[:10],
            "decision_at": decision_at,
            "ticker": ticker,
            "strategy_version": ALPHAOPS_V6_STRATEGY_VERSION,
            "model_version": ALPHAOPS_V6_MODEL_VERSION,
            "feature_schema_version": FEATURE_SCHEMA_VERSION,
            "feature_hash_sha256": feature_hash,
            "setup_key": _setup_key(signal, feature),
            "regime_key": str(regime.get("regime") or "UNKNOWN"),
            "feature_vector": feature,
            "universe_membership": membership,
            "signal_facts": _safe_signal_facts(signal),
            "source_summary": source_summary,
            "evidence_cohort": "forward-current-v2",
            "safety_vetoes": vetoes,
            "estimated_round_trip_cost_bps": _estimated_cost_bps(feature),
            "cost_model_version": V6_COST_MODEL_VERSION,
            "execution_assumptions": {
                "entry": "saved_trigger_or_trigger_bar_open_whichever_is_higher",
                "exit": "saved_target_stop_first_touch_else_session_close",
                "bar_interval": "1m",
                "same_bar_ambiguity": "stop_first_conservative",
                "round_trip_cost_bps": _estimated_cost_bps(feature),
            },
            "benchmark_policy": alphaops_v6_benchmark_policy(),
            "point_in_time": {
                "all_inputs_observed_at_or_before_decision": True,
                "decision_timestamp": decision_at,
                "feature_timestamp": feature.get("timestamp"),
            },
            "research_only": True,
            "broker_execution_enabled": False,
        }
        draft["input_hash_sha256"] = _hash(draft)
        draft["source_lineage_hash_sha256"] = _hash(
            {
                "source_summary": source_summary,
                "feature_config_hash": feature.get("config_hash"),
                "feature_timestamp": feature.get("timestamp"),
            }
        )
        prediction = predict_from_frozen_model_run(frozen_model_run, draft)
        if prediction is None:
            prediction = model.predict(draft).to_dict()
        # Tracking is a controlled paper-research cohort, not a delivery or a
        # promotion.  Any V5/v6 safety veto excludes the candidate completely.
        draft["action"] = "SHADOW_TRACK" if not vetoes else "SHADOW_REJECT_VETO"
        draft["decision_state"] = "SELECTED" if not vetoes else "BLOCKED"
        draft["prediction"] = prediction
        draft["score_components"] = {
            "activation_probability": prediction.get("activation_probability"),
            "conditional_net_excess_return_pct": prediction.get(
                "conditional_net_excess_return_pct"
            ),
            "tail_loss_pct": prediction.get("tail_loss_pct"),
            "conservative_utility_lcb_pct": prediction.get("utility_lcb_pct"),
        }
        draft["uncertainty"] = {
            "status": prediction.get("status"),
            "sample_size": prediction.get("sample_size"),
            "interval_lower_pct": prediction.get("interval_lower_pct"),
            "interval_upper_pct": prediction.get("interval_upper_pct"),
        }
        draft["decision_id"] = (
            "v6d-"
            + _hash(
                {
                    "scan_id": draft["scan_id"],
                    "source_signal_id": source_signal_id,
                    "strategy_version": ALPHAOPS_V6_STRATEGY_VERSION,
                }
            )[:28]
        )
        draft["shadow_signal_id"] = "v6s-" + _hash({"decision_id": draft["decision_id"]})[:28]
        output.append(draft)
    return output


def build_v6_outcomes(
    *,
    decisions: list[dict[str, Any]],
    sourced_outcomes: list[dict[str, Any]],
    capture_attempts: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Turn one sourced V6 shadow receipt into one immutable learning label."""

    outcome_by_signal = {str(row.get("signal_id") or ""): row for row in sourced_outcomes}
    attempt_by_signal = {str(row.get("signal_id") or ""): row for row in capture_attempts}
    output: list[dict[str, Any]] = []
    for decision in decisions:
        shadow_signal_id = str(decision.get("shadow_signal_id") or "")
        if not shadow_signal_id:
            continue
        source = outcome_by_signal.get(shadow_signal_id)
        attempt = attempt_by_signal.get(shadow_signal_id)
        if source is None and attempt is None:
            continue
        outcome = _v6_outcome_from_source(decision, source, attempt)
        output.append(outcome)
    return output


def strict_walk_forward_evaluation(
    *,
    decisions: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> dict[str, Any]:
    """Evaluate only against future dates; no random or same-date leakage."""

    decision_by_id = {str(row.get("decision_id") or ""): row for row in decisions}
    rows: list[dict[str, Any]] = []
    for outcome in outcomes:
        decision = decision_by_id.get(str(outcome.get("decision_id") or ""))
        if decision is None:
            continue
        if (
            classify_canonical_return_truth(outcome, decision=decision) != CURRENT_RETURN_TRUTH
            or outcome.get("learning_eligible") is not True
            or not has_authenticated_committed_fill_truth(outcome)
        ):
            continue
        rows.append({**outcome, "decision": decision})
    dates = sorted({str(row.get("market_date") or "")[:10] for row in rows})
    predictions: list[dict[str, Any]] = []
    for date in dates:
        train = [row for row in rows if str(row.get("market_date") or "")[:10] < date]
        test = [row for row in rows if str(row.get("market_date") or "")[:10] == date]
        model = V6EmpiricalShadowModel(train)
        for row in test:
            prediction = model.predict(dict(row["decision"])).to_dict()
            predictions.append(
                {
                    "market_date": date,
                    "decision_id": row.get("decision_id"),
                    "prediction": prediction,
                    "realized_net_excess_return_pct": row.get("net_excess_return_pct"),
                    "training_max_market_date": max(
                        (str(item.get("market_date") or "")[:10] for item in train),
                        default=None,
                    ),
                }
            )
    eligible = [
        row
        for row in predictions
        if row["prediction"].get("status") == "CALIBRATED"
        and _number(row.get("realized_net_excess_return_pct")) is not None
    ]
    return {
        "schema_version": "dawnstrike.alphaops_v6.walk_forward.v1",
        "status": "EVALUABLE" if eligible else "NOT_EVALUABLE",
        "evaluation_method": "strict_expanding_window_by_market_date",
        "leakage_check": all(
            row["training_max_market_date"] is None
            or row["training_max_market_date"] < row["market_date"]
            for row in predictions
        ),
        "evaluated_prediction_count": len(eligible),
        "total_label_count": len(rows),
        "realized_net_excess_return_pct": _round(
            mean([float(row["realized_net_excess_return_pct"]) for row in eligible])
        )
        if eligible
        else None,
        "predictions": predictions,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def promotion_readiness(
    outcomes: list[dict[str, Any]],
    *,
    decisions: list[dict[str, Any]] | None = None,
    evaluation: dict[str, Any] | None = None,
    manual_operator_approval: bool = False,
) -> dict[str, Any]:
    """Evaluate every frozen promotion gate; absent proof always fails closed."""

    decision_by_id = {str(row.get("decision_id") or ""): row for row in (decisions or [])}
    valid = []
    for row in outcomes:
        decision = decision_by_id.get(str(row.get("decision_id") or ""))
        if (
            decision is not None
            and classify_canonical_return_truth(row, decision=decision) == CURRENT_RETURN_TRUTH
            and row.get("learning_eligible") is True
            and has_authenticated_committed_fill_truth(row)
            and row.get("prospective_promotion_eligible") is True
        ):
            valid.append(row)
    benchmark = benchmark_coverage(valid)
    sessions = {str(row.get("market_date") or "")[:10] for row in valid}
    returns: list[float] = []
    for row in valid:
        value = _number(row.get("net_excess_return_pct"))
        if value is not None:
            returns.append(value)
    tail = _tail_mean(returns) if returns else None
    tracked = [row for row in (decisions or []) if row.get("action") == "SHADOW_TRACK"]
    conclusive_ids = {
        str(row.get("decision_id") or "") for row in valid if str(row.get("decision_id") or "")
    }
    outcome_coverage_pct = (
        100.0
        * sum(1 for row in tracked if str(row.get("decision_id") or "") in conclusive_ids)
        / len(tracked)
        if tracked
        else None
    )
    daily_returns_by_date = aggregate_daily_returns(valid)
    daily_returns = [daily_returns_by_date[day][0] for day in sorted(daily_returns_by_date)]
    daily_weighting = daily_weighting_status(valid)
    # Allocation is already reflected in each account-day return; risk
    # observations are sessions and are not weighted again by invested cash.
    daily_observation_weights = [1.0] * len(daily_returns)
    profit_factor = _profit_factor(daily_returns, daily_observation_weights)
    maximum_drawdown = _maximum_drawdown(daily_returns)
    concentration = _return_concentration(daily_returns, daily_observation_weights)
    bootstrap_lower = _bootstrap_lower_bound(valid)
    stressed_expectancy = _stressed_expectancy(valid, multiplier=1.5)
    evaluation_data = evaluation or {}
    account_session_evidence = evaluation_data.get("account_session_evaluation")
    account_session_data = (
        account_session_evidence if isinstance(account_session_evidence, dict) else {}
    )
    return_metrics = evaluation_data.get("return_metrics")
    metrics = return_metrics if isinstance(return_metrics, dict) else {}
    holdout = evaluation_data.get("untouched_holdout")
    holdout_data = holdout if isinstance(holdout, dict) else {}
    comparison = evaluation_data.get("comparison_to_v5")
    comparison_data = comparison if isinstance(comparison, dict) else {}
    holdout_binding_valid = _holdout_binding_is_exact(
        holdout_data, model_run_id=evaluation_data.get("model_run_id")
    )
    comparison_binding_valid = _comparison_binding_is_exact(
        comparison_data, model_run_id=evaluation_data.get("model_run_id")
    )
    criteria = {
        "minimum_forward_sessions": len(sessions) >= MIN_FORWARD_SESSIONS,
        "minimum_closed_paper_trades": len(returns) >= MIN_FORWARD_CLOSED_TRADES,
        "eligible_outcome_coverage_at_least_98_pct": bool(
            outcome_coverage_pct is not None and outcome_coverage_pct >= 98.0
        ),
        "included_benchmark_coverage_100_pct": bool(
            benchmark["primary_complete"] and benchmark["secondary_complete"]
        ),
        "positive_mean_net_excess_return": bool(returns and mean(returns) > 0),
        # The one-percent objective is reported as a point target only.  It is
        # never a sizing instruction and cannot replace the positive lower CI.
        "one_percent_point_target_observed": bool(daily_returns and mean(daily_returns) >= 1.0),
        "bootstrap_95_lower_bound_above_zero": bool(
            bootstrap_lower is not None and bootstrap_lower > 0.0
        ),
        "positive_lower_confidence_bound_required": bool(
            bootstrap_lower is not None and bootstrap_lower > 0.0
        ),
        "profit_factor_at_least_1_20": bool(profit_factor is not None and profit_factor >= 1.2),
        "positive_excess_vs_primary_and_cash": bool(returns and mean(returns) > 0),
        "maximum_drawdown_no_worse_than_minus_8_pct": bool(
            maximum_drawdown is not None and maximum_drawdown >= -8.0
        ),
        "gain_loss_concentration_no_more_than_25_pct": bool(
            concentration is not None and concentration <= 25.0
        ),
        "authentic_account_weighted_risk_series": bool(
            daily_returns and daily_weighting["promotion_eligible"]
        ),
        "positive_purged_walk_forward": bool(
            metrics.get("status") == "EVALUABLE"
            and _number(metrics.get("after_cost_expectancy_pct")) is not None
            and float(metrics["after_cost_expectancy_pct"]) > 0
            and evaluation_data.get("no_lookahead") is True
        ),
        "positive_untouched_holdout": bool(
            holdout_binding_valid
            and holdout_data.get("evaluated_once") is True
            and _number(holdout_data.get("after_cost_expectancy_pct")) is not None
            and float(holdout_data["after_cost_expectancy_pct"]) > 0
        ),
        "positive_under_1_5x_slippage": bool(
            stressed_expectancy is not None and stressed_expectancy > 0.0
        ),
        "no_lookahead_and_reconciliation_pass": bool(
            evaluation_data.get("no_lookahead") is True
            and valid
            and all(row.get("no_lookahead") is True for row in valid)
        ),
        "challenger_beats_frozen_v5_objective": bool(
            comparison_binding_valid
            and _number(comparison_data.get("objective_delta_pct")) is not None
            and float(comparison_data["objective_delta_pct"]) > 0
        ),
        "untouched_holdout_receipt_exactly_bound": holdout_binding_valid,
        "comparison_to_v5_receipt_exactly_bound": comparison_binding_valid,
        "manual_operator_approval_recorded": manual_operator_approval,
        "primary_benchmark_coverage_complete": benchmark["primary_complete"],
        "secondary_benchmark_coverage_complete": benchmark["secondary_complete"],
        "all_sourced_and_point_in_time": all(
            bool(row.get("source_bar_hash_sha256")) and row.get("no_lookahead") is True
            for row in valid
        )
        and bool(valid),
        "authoritative_account_session_completeness": bool(
            account_session_data.get("status") == "COMPLETE"
        )
        if account_session_evidence is not None
        else False,
    }
    technical_criteria = {
        key: value for key, value in criteria.items() if key != "manual_operator_approval_recorded"
    }
    technically_ready = all(technical_criteria.values())
    approved = technically_ready and manual_operator_approval
    promotion_blockers = []
    if not holdout_binding_valid:
        promotion_blockers.append("untouched_holdout_receipt_missing_or_hash_mismatch")
    if not comparison_binding_valid:
        promotion_blockers.append("comparison_to_v5_receipt_missing_or_hash_mismatch")
    if not daily_weighting["promotion_eligible"]:
        promotion_blockers.append("allocation_or_account_weight_truth_missing_or_invalid")
    if account_session_evidence is None or account_session_data.get("status") != "COMPLETE":
        promotion_blockers.append("authoritative_account_session_completeness_missing_or_blocked")
    return {
        "status": (
            "MANUALLY_APPROVED_FOR_CONTROLLED_PROMOTION"
            if approved
            else "ELIGIBLE_FOR_MANUAL_REVIEW"
            if technically_ready
            else "NOT_ELIGIBLE_FOR_PROMOTION"
        ),
        "automatic_promotion": False,
        "criteria": criteria,
        "promotion_blockers": promotion_blockers,
        "forward_session_count": len(sessions),
        "closed_paper_trade_count": len(returns),
        "risk_observation_unit": "market_date",
        "risk_observation_count": len(daily_returns),
        "risk_series_status": daily_weighting["status"],
        "risk_series_promotion_eligible": daily_weighting["promotion_eligible"],
        "eligible_outcome_coverage_pct": _round(outcome_coverage_pct),
        "target": {
            "target_return_pct": 1.0,
            "target_is_evaluation_only": True,
            "point_target_observed": bool(daily_returns and mean(daily_returns) >= 1.0),
            "positive_lower_confidence_bound_required": True,
        },
        "mean_net_excess_return_pct": _round(mean(returns)) if returns else None,
        "tail_loss_pct": _round(tail),
        "profit_factor": _round(profit_factor),
        "maximum_drawdown_pct": _round(maximum_drawdown),
        "gain_loss_concentration_pct": _round(concentration),
        "bootstrap_95_lower_bound_pct": _round(bootstrap_lower),
        "one_point_five_x_slippage_expectancy_pct": _round(stressed_expectancy),
        "benchmark_coverage": benchmark,
        "performance_status": (
            "ELIGIBLE_FOR_MANUAL_REVIEW" if technically_ready else "WAITING_FOR_FORWARD_EVIDENCE"
        ),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _profit_factor(values: list[float], weights: list[float] | None = None) -> float | None:
    effective_weights = weights if weights is not None else [1.0] * len(values)
    gains = sum(
        value * weight for value, weight in zip(values, effective_weights, strict=True) if value > 0
    )
    losses = abs(
        sum(
            value * weight
            for value, weight in zip(values, effective_weights, strict=True)
            if value < 0
        )
    )
    return gains / losses if losses else None


def _maximum_drawdown(values: list[float]) -> float | None:
    if not values:
        return None
    equity = 1.0
    high = 1.0
    worst = 0.0
    for value in values:
        equity *= max(0.0, 1.0 + value / 100.0)
        high = max(high, equity)
        worst = min(worst, (equity / high - 1.0) * 100.0)
    return worst


def _return_concentration(values: list[float], weights: list[float] | None = None) -> float | None:
    if not values:
        return None
    effective_weights = weights if weights is not None else [1.0] * len(values)
    contributions = [
        abs(value * weight) for value, weight in zip(values, effective_weights, strict=True)
    ]
    denominator = sum(contributions)
    return 100.0 * max(contributions) / denominator if denominator else 100.0


def _holdout_binding_is_exact(holdout: dict[str, Any], *, model_run_id: object) -> bool:
    """Require an immutable holdout receipt to bind experiment/model/evidence."""

    evidence = holdout.get("evidence")
    evidence_data = evidence if isinstance(evidence, dict) else {}
    experiment_id = str(holdout.get("experiment_id") or "")
    bound_model = str(holdout.get("model_run_id") or evidence_data.get("model_run_id") or "")
    configured_hash = str(holdout.get("configuration_hash_sha256") or "")
    evidence_experiment_id = str(evidence_data.get("experiment_id") or "")
    evidence_configuration_hash = str(evidence_data.get("configuration_hash_sha256") or "")
    source_lineage_hash = str(
        evidence_data.get("source_lineage_hash_sha256")
        or evidence_data.get("source_hash_sha256")
        or ""
    )
    code_sha = str(evidence_data.get("code_sha") or "")
    evaluation_window = evidence_data.get("evaluation_window")
    window_data = evaluation_window if isinstance(evaluation_window, dict) else {}
    evidence_hash = str(holdout.get("evidence_hash_sha256") or "")
    declared_binding = str(
        holdout.get("model_binding_hash_sha256") or holdout.get("binding_hash_sha256") or ""
    )
    expected_binding = _hash(
        {
            "model_run_id": bound_model,
            "experiment_id": evidence_experiment_id,
            "configuration_hash_sha256": evidence_configuration_hash,
            "source_lineage_hash_sha256": source_lineage_hash,
            "code_sha": code_sha,
            "evaluation_window": window_data,
            "data_hash_sha256": evidence_data.get("data_hash_sha256"),
            "source_hash_sha256": evidence_data.get("source_hash_sha256"),
            "evidence_hash_sha256": evidence_hash,
        }
    )
    return bool(
        holdout.get("evaluated_once") is True
        and str(holdout.get("holdout_evaluation_id") or "")
        and experiment_id
        and bound_model
        and str(model_run_id or "") == bound_model
        and configured_hash
        and is_valid_sha256(configured_hash)
        and evidence_experiment_id == experiment_id
        and evidence_configuration_hash == configured_hash
        and source_lineage_hash
        and is_valid_sha256(evidence_configuration_hash)
        and is_valid_sha256(source_lineage_hash)
        and code_sha
        and is_valid_code_sha(code_sha)
        and is_valid_sha256(evidence_hash)
        and window_data
        and evidence_data
        and evidence_hash == _hash(evidence_data)
        and declared_binding == expected_binding
    )


def _comparison_binding_is_exact(comparison: dict[str, Any], *, model_run_id: object) -> bool:
    """Require a persisted V5 comparison receipt with complete lineage."""

    comparison_id = str(comparison.get("comparison_id") or "")
    input_hash = str(comparison.get("input_hash_sha256") or "")
    bound_model = str(comparison.get("model_run_id") or "")
    experiment_id = str(comparison.get("experiment_id") or "")
    configuration_hash = str(comparison.get("configuration_hash_sha256") or "")
    source_hash = str(
        comparison.get("source_lineage_hash_sha256") or comparison.get("source_hash_sha256") or ""
    )
    code_sha = str(comparison.get("code_sha") or "")
    evaluation_window = comparison.get("evaluation_window")
    window_data = evaluation_window if isinstance(evaluation_window, dict) else {}
    declared_binding = str(
        comparison.get("model_binding_hash_sha256") or comparison.get("binding_hash_sha256") or ""
    )
    metrics = comparison.get("series_metrics")
    metrics_hash = _hash(metrics) if isinstance(metrics, dict) else ""
    persisted_metrics_hash = str(comparison.get("comparison_metrics_hash_sha256") or "")
    expected_binding = _hash(
        {
            "model_run_id": bound_model,
            "experiment_id": experiment_id,
            "configuration_hash_sha256": configuration_hash,
            "source_lineage_hash_sha256": source_hash,
            "code_sha": code_sha,
            "evaluation_window": window_data,
            "comparison_id": comparison_id,
            "input_hash_sha256": input_hash,
            "comparison_metrics_hash_sha256": metrics_hash,
        }
    )
    return bool(
        comparison.get("status") == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
        and comparison_id
        and input_hash
        and bound_model
        and str(model_run_id or "") == bound_model
        and experiment_id
        and configuration_hash
        and is_valid_sha256(configuration_hash)
        and source_hash
        and is_valid_sha256(source_hash)
        and code_sha
        and is_valid_code_sha(code_sha)
        and window_data
        and persisted_metrics_hash == metrics_hash
        and is_valid_sha256(persisted_metrics_hash)
        and is_valid_sha256(input_hash)
        and isinstance(metrics, dict)
        and declared_binding == expected_binding
    )


def _bootstrap_lower_bound(rows: list[dict[str, Any]]) -> float | None:
    import random

    by_date: dict[str, list[float]] = defaultdict(list)
    for row in rows:
        value = _number(row.get("net_excess_return_pct"))
        if value is not None:
            by_date[str(row.get("market_date") or "")[:10]].append(value)
    dates = sorted(key for key, values in by_date.items() if key and values)
    if len(dates) < 2:
        return None
    generator = random.Random(6_001)
    estimates = []
    for _ in range(1_000):
        values = [value for _index in dates for value in by_date[generator.choice(dates)]]
        estimates.append(mean(values))
    estimates.sort()
    return estimates[max(0, int(len(estimates) * 0.025) - 1)]


def _stressed_expectancy(rows: list[dict[str, Any]], *, multiplier: float) -> float | None:
    values = []
    for row in rows:
        value = _number(row.get("net_excess_return_pct"))
        cost_bps = _number(row.get("estimated_round_trip_cost_bps"))
        if value is not None and cost_bps is not None:
            values.append(value - (multiplier - 1.0) * cost_bps / 100.0)
    return mean(values) if values else None


def _v6_outcome_from_source(
    decision: dict[str, Any],
    source: dict[str, Any] | None,
    attempt: dict[str, Any] | None,
) -> dict[str, Any]:
    source_or_attempt = source or attempt or {}
    missing_classification = _capture_missing_classification(source_or_attempt)
    fill_truth_present = has_authenticated_committed_fill_truth(source_or_attempt)
    observed_at = str(
        (source or attempt or {}).get("captured_at")
        or (source or attempt or {}).get("attempted_at")
        or _utc_now()
    )
    projection = (
        canonical_return_truth_projection(source, decision=decision) if source is not None else {}
    )
    current_return = bool(
        source is not None
        and classify_canonical_return_truth(source, decision=decision) == CURRENT_RETURN_TRUTH
        and projection
    )
    payload: dict[str, Any] = {
        **projection,
        "decision_id": decision["decision_id"],
        "shadow_signal_id": decision["shadow_signal_id"],
        "market_date": decision["market_date"],
        "observed_at": observed_at,
        "activation_status": projection.get("activation_status", "MISSING"),
        "outcome_status": projection.get("outcome_status", "TERMINAL_MISSING"),
        # V6's compatibility alias is a projection of authenticated after-cost
        # truth.  It is never recomputed from gross or expected prices.
        "net_return_pct": (
            _number(projection.get("after_cost_return_pct")) if current_return else None
        ),
        "first_touch": projection.get("path_event"),
        "counterfactual_rejected_candidate": bool(
            (source or {}).get("counterfactual_rejected_candidate")
        ),
        "counterfactual_policy": (source or {}).get("counterfactual_policy"),
        "learning_eligible": bool(
            current_return and projection.get("learning_eligible") is True and fill_truth_present
        ),
        "fill_truth_required": True,
        "fill_truth_status": (
            "committed" if fill_truth_present else "missing_committed_fill_truth"
        ),
        "return_learning_quarantine_reason": (
            None if fill_truth_present else MISSING_COMMITTED_FILL_TRUTH
        ),
        "no_lookahead": bool(projection.get("no_lookahead")),
        "cost_model_version": decision.get("cost_model_version"),
        "estimated_round_trip_cost_bps": _number(decision.get("estimated_round_trip_cost_bps")),
        "source_outcome_status": (
            source_or_attempt.get("outcome_status") or (attempt or {}).get("status")
        ),
        "missing_classification": missing_classification,
        "authoritative_terminal": missing_classification == "authoritative_terminal",
        "retryable_missing": missing_classification == "recoverable",
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if not current_return:
        payload["outcome_id"] = (
            "v6o-"
            + _hash(
                {
                    "decision_id": payload["decision_id"],
                    "source_status": payload["source_outcome_status"],
                    "outcome_status": payload["outcome_status"],
                }
            )[:28]
        )
    return payload


_capture_missing_classification = classify_missing_capture


def _safety_vetoes(
    signal: dict[str, Any],
    feature: dict[str, Any],
    source_summary: dict[str, Any],
    universe_membership: dict[str, Any],
) -> list[str]:
    vetoes: list[str] = []
    # V6 is the research-learning cohort, not the official-paper cohort.  A
    # legacy confidence/evidence gate may keep a row out of Telegram's official
    # section, but it must not create a cold-start deadlock that prevents safe
    # candidates from collecting forward outcomes.  Only material safety facts
    # veto research tracking here.
    hard_reasons = _tokens(signal.get("hard_avoid_reasons"))
    material_flags = _tokens(signal.get("risk_flags"))
    material_flags.extend(_tokens(signal.get("catalyst_risk_flags")))
    material_flags.extend(_tokens(signal.get("coverage_warning")))
    if signal.get("current_halt") is True or "current_halt" in material_flags:
        hard_reasons.append("current_halt")
    if signal.get("recent_offering") is True or any(
        item in material_flags
        for item in (
            "recent_offering",
            "active_offering",
            "active_dilution",
            "news_dilution_language",
            "dilution_risk",
        )
    ):
        hard_reasons.append("active_offering")
    if signal.get("reverse_split_90d") is True or "reverse_split_90d" in material_flags:
        hard_reasons.append("recent_reverse_split")
    if signal.get("stale_data_flag") is True or "stale_source" in material_flags:
        hard_reasons.append("stale_source")
    if str(signal.get("conflict_flags") or "").strip():
        hard_reasons.append("source_conflict")
    try:
        source_confidence = float(signal.get("source_confidence") or 0.0)
    except (TypeError, ValueError):
        source_confidence = 0.0
    if source_confidence < 18.0:
        hard_reasons.append("source_confidence_below_hard_floor")
    vetoes.extend(hard_reasons)
    raw = signal.get("raw_payload_json")
    raw_payload = raw if isinstance(raw, dict) else {}
    if raw_payload.get("avoid_reasons") or signal.get("avoid_reasons"):
        vetoes.append("candidate_risk_veto")
    if str(source_summary.get("status") or "").lower() not in {"success", "complete"}:
        vetoes.append("source_collection_not_complete")
    if universe_membership.get("status") != "ACTIVE":
        vetoes.append("versioned_universe_membership_not_active")
    if not feature.get("config_hash"):
        vetoes.append("feature_lineage_missing")
    return sorted(set(vetoes))


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip().lower() for item in value if str(item).strip()]
    return [
        part.strip().lower()
        for part in str(value or "").replace(",", ";").split(";")
        if part.strip()
    ]


def _universe_membership(membership: dict[str, Any] | None) -> dict[str, Any]:
    if membership is None:
        return {
            "status": "UNREGISTERED",
            "reason": "no_versioned_universe_registered_for_market_date",
            "missing_truth_is_zero": False,
        }
    return dict(membership)


def _setup_key(signal: dict[str, Any], feature: dict[str, Any]) -> str:
    raw = feature.get("feature_json") if isinstance(feature.get("feature_json"), dict) else {}
    setup = raw.get("playbook_setup") if isinstance(raw, dict) else {}
    return str(
        signal.get("setup_key")
        or signal.get("primary_setup")
        or (setup or {}).get("setup_key")
        or "unknown"
    )


def _safe_signal_facts(signal: dict[str, Any]) -> dict[str, Any]:
    return {
        key: signal.get(key)
        for key in (
            "ticker",
            "rank",
            "alpha_score",
            "entry_watch_level",
            "target_1",
            "invalidation_level",
            "can_alert",
            "alert_gate_status",
            "no_trade_reason",
            "source_confidence",
            "source",
            "source_url",
        )
    }


def _estimated_cost_bps(feature: dict[str, Any]) -> float | None:
    raw = feature.get("feature_json") if isinstance(feature.get("feature_json"), dict) else {}
    liquidity = raw.get("liquidity_execution") if isinstance(raw, dict) else {}
    spread_pct = _number((liquidity or {}).get("spread_pct"))
    if spread_pct is None:
        return None
    return round(max(15.0, spread_pct * 125.0 + 10.0), 4)


def _group_key(row: dict[str, Any]) -> str:
    return "|".join(
        (
            str(row.get("setup_key") or "unknown"),
            str(row.get("regime_key") or "UNKNOWN"),
        )
    )


def _tail_mean(values: list[float]) -> float | None:
    if not values:
        return None
    count = max(1, math.ceil(len(values) * 0.1))
    return mean(sorted(values)[:count])


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, default=str, separators=(",", ":")).encode()
    ).hexdigest()


def _number(value: Any) -> float | None:
    if value in {None, ""}:
        return None
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if math.isfinite(parsed) else None


def _text_or_none(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _round(value: float | None) -> float | None:
    return round(value, 6) if value is not None else None


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = [
    "ALPHAOPS_V6_MODEL_VERSION",
    "ALPHAOPS_V6_STRATEGY_VERSION",
    "MIN_FORWARD_CLOSED_TRADES",
    "MIN_FORWARD_SESSIONS",
    "V6EmpiricalShadowModel",
    "build_v6_outcomes",
    "build_v6_shadow_decisions",
    "promotion_readiness",
    "strict_walk_forward_evaluation",
]
