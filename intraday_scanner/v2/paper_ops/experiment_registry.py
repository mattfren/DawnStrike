"""Frozen one-change PaperOps experiment contracts and governance packets."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.v2.paper_ops.position_management import (
    challenger_position_policies,
)

REQUIRED_PROMOTION_METRICS = (
    "forward_market_days",
    "forward_closed_trades",
    "truth_coverage_pct",
    "after_cost_expectancy",
    "profit_factor",
    "return_vs_cash",
    "return_vs_benchmark",
    "maximum_drawdown_pct",
    "gain_concentration_pct",
    "loss_concentration_pct",
    "chronological_walk_forward",
    "untouched_holdout",
    "slippage_stress_1_5x",
    "no_lookahead_proof",
    "reconciliation_proof",
)
REQUIRED_PROMOTION_THRESHOLDS: dict[str, Any] = {
    "forward_market_days_min": 60,
    "forward_closed_trades_min": 100,
    "truth_coverage_pct_min": 98.0,
    "after_cost_expectancy_min_exclusive": 0.0,
    "profit_factor_min": 1.20,
    "return_vs_cash_min_exclusive": 0.0,
    "return_vs_benchmark_min_exclusive": 0.0,
    "maximum_drawdown_pct_floor": -8.0,
    "gain_concentration_pct_max": 25.0,
    "loss_concentration_pct_max": 25.0,
    "chronological_walk_forward_required": True,
    "untouched_holdout_required": True,
    "slippage_stress_multiplier": 1.50,
    "slippage_stress_expectancy_min_exclusive": 0.0,
    "no_lookahead_proof_required": True,
    "reconciliation_proof_required": True,
    "manual_operator_review_required": True,
}


@dataclass(frozen=True, slots=True)
class ExperimentContract:
    experiment_id: str
    champion_strategy_id: str
    champion_strategy_version: str
    challenger_strategy_id: str
    challenger_strategy_version: str
    primary_hypothesis: str
    controlled_change: str
    frozen_configuration: dict[str, Any]
    training_cutoff: str
    validation_start: str
    validation_end: str
    untouched_holdout_start: str
    untouched_holdout_end: str
    required_metrics: tuple[str, ...]
    promotion_thresholds: dict[str, Any]
    stop_condition: str
    promotion_decision: str
    asset_cohort: str
    status: str = "registered_forward_only"
    auto_promotion_enabled: bool = False
    research_only: bool = True
    broker_execution_enabled: bool = False

    @property
    def frozen_configuration_hash(self) -> str:
        return hashlib.sha256(
            json.dumps(
                self.frozen_configuration,
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["required_metrics"] = list(self.required_metrics)
        payload["frozen_configuration_hash"] = self.frozen_configuration_hash
        return payload


def build_experiment_registry() -> tuple[ExperimentContract, ...]:
    policy_experiments = tuple(
        ExperimentContract(
            experiment_id=f"lifecycle:{policy.strategy_id}:{policy.strategy_version}",
            champion_strategy_id=policy.strategy_id,
            champion_strategy_version="v1.0",
            challenger_strategy_id=policy.strategy_id,
            challenger_strategy_version=policy.strategy_version,
            primary_hypothesis=(
                "A thesis-specific causal exit and trading-session timeout improves "
                "after-cost expectancy without increasing drawdown."
            ),
            controlled_change=(
                "Replace only the generic stop-target-calendar-timeout lifecycle "
                f"with position policy {policy.policy_version}."
            ),
            frozen_configuration={
                "position_management_policy": policy.fingerprint_payload(),
                "position_management_policy_fingerprint": policy.fingerprint,
            },
            training_cutoff="2026-07-30",
            validation_start="2026-07-31",
            validation_end="2026-10-30",
            untouched_holdout_start="2026-11-02",
            untouched_holdout_end="2027-01-29",
            required_metrics=REQUIRED_PROMOTION_METRICS,
            promotion_thresholds=REQUIRED_PROMOTION_THRESHOLDS,
            stop_condition=(
                "Stop forward entry admission after a reconciliation breach, "
                "lookahead breach, drawdown above 8%, or truth coverage below 98%."
            ),
            promotion_decision="NOT_ELIGIBLE_AWAITING_OPERATOR_REVIEW",
            asset_cohort="stocks_and_etfs_separate",
        )
        for policy in challenger_position_policies()
    )
    allocator = ExperimentContract(
        experiment_id="fleet:overlap-correlation-allocator:v1",
        champion_strategy_id="individual_strategy_accounts",
        champion_strategy_version="v1",
        challenger_strategy_id="paperops_fleet_allocator",
        challenger_strategy_version="v1.0-challenger",
        primary_hypothesis=(
            "Fleet-level overlap and correlation limits reduce concentration and "
            "max-position saturation without degrading after-cost expectancy."
        ),
        controlled_change=(
            "Add only a shadow fleet allocation layer; preserve every individual "
            "strategy account and decision."
        ),
        frozen_configuration={
            "max_fleet_positions": 6,
            "max_symbol_overlap": 1,
            "max_correlation_group_positions": 2,
            "preserve_individual_strategy_accounts": True,
        },
        training_cutoff="2026-07-30",
        validation_start="2026-07-31",
        validation_end="2026-10-30",
        untouched_holdout_start="2026-11-02",
        untouched_holdout_end="2027-01-29",
        required_metrics=REQUIRED_PROMOTION_METRICS,
        promotion_thresholds=REQUIRED_PROMOTION_THRESHOLDS,
        stop_condition=(
            "Stop if any individual strategy ledger changes or the fleet exceeds "
            "an overlap/correlation limit."
        ),
        promotion_decision="NOT_ELIGIBLE_AWAITING_OPERATOR_REVIEW",
        asset_cohort="stocks_and_etfs_separate",
    )
    ranking = ExperimentContract(
        experiment_id="ranking:stock-etf-separation:v1",
        champion_strategy_id="cross_sectional_relative_strength",
        champion_strategy_version="v1.0",
        challenger_strategy_id="cross_sectional_relative_strength",
        challenger_strategy_version="v2.0-asset-cohorts",
        primary_hypothesis=(
            "Ranking stocks only against stocks and ETFs only against ETFs removes "
            "structural cross-asset rank distortion."
        ),
        controlled_change=(
            "Change only the cross-sectional ranking universe partition from mixed "
            "assets to separate stock and ETF cohorts."
        ),
        frozen_configuration={
            "asset_cohorts": ["stock", "etf"],
            "unknown_asset_policy": "block_from_ranked_selection",
            "ranking_metric_unchanged": True,
        },
        training_cutoff="2026-07-30",
        validation_start="2026-07-31",
        validation_end="2026-10-30",
        untouched_holdout_start="2026-11-02",
        untouched_holdout_end="2027-01-29",
        required_metrics=REQUIRED_PROMOTION_METRICS,
        promotion_thresholds=REQUIRED_PROMOTION_THRESHOLDS,
        stop_condition=(
            "Stop if asset classification is missing, stale, or changes after a "
            "decision timestamp."
        ),
        promotion_decision="NOT_ELIGIBLE_AWAITING_OPERATOR_REVIEW",
        asset_cohort="stocks_and_etfs_separate",
    )
    registry = (*policy_experiments, allocator, ranking)
    validate_experiment_registry(registry)
    return registry


def build_intraday_v5_experiment_contract() -> ExperimentContract:
    """Return the additive causal-replay contract for operator review."""

    contract = ExperimentContract(
        experiment_id="intraday:alphaops-v5-causal-replay:v1",
        champion_strategy_id="alphaops_v5_daily_policy",
        champion_strategy_version="dawnstrike-alphaops-v5.0.0",
        challenger_strategy_id="alphaops_v5_intraday_causal_replay",
        challenger_strategy_version="dawnstrike-alphaops-v5-intraday-replay.1",
        primary_hypothesis=(
            "Retained point-in-time intraday path evidence can measure the frozen "
            "AlphaOps V5 policy without lookahead or changing daily outputs."
        ),
        controlled_change=(
            "Add a research-only causal event clock, provisional 50 bps-per-side "
            "cost model, and path-aware attribution beside the daily backtest."
        ),
        frozen_configuration={
            "event_clock": "timestamp_then_sequence",
            "entry_rule": "strictly_after_decision_event",
            "trigger_bar_extrema": "excluded",
            "cost_model_status": "COST_MODEL_PROVISIONAL",
            "cost_model_version": "alphaops-v5-cost-model-50bps-0.005ps",
            "empirical_cost_required": True,
            "broker_execution_enabled": False,
        },
        training_cutoff="2026-07-30",
        validation_start="2026-07-31",
        validation_end="2026-10-30",
        untouched_holdout_start="2026-11-02",
        untouched_holdout_end="2027-01-29",
        required_metrics=REQUIRED_PROMOTION_METRICS,
        promotion_thresholds=REQUIRED_PROMOTION_THRESHOLDS,
        stop_condition=(
            "Stop on any future-data breach, missing retained evidence, source "
            "conflict, unresolved path truth, or protocol reconciliation breach."
        ),
        promotion_decision="NOT_EVALUABLE_PENDING_PROTOCOL_APPROVAL",
        asset_cohort="stocks_and_etfs_separate",
        status="registered_research_only",
    )
    validate_experiment_registry((contract,))
    return contract


def validate_experiment_registry(
    contracts: tuple[ExperimentContract, ...],
) -> None:
    identities: set[str] = set()
    for contract in contracts:
        if contract.experiment_id in identities:
            raise ValueError("experiment registry contains a duplicate identity")
        identities.add(contract.experiment_id)
        if not contract.primary_hypothesis.strip():
            raise ValueError("experiment primary hypothesis is required")
        if not contract.controlled_change.strip():
            raise ValueError("experiment controlled change is required")
        if contract.auto_promotion_enabled:
            raise ValueError("PaperOps experiments can never auto-promote")
        if contract.required_metrics != REQUIRED_PROMOTION_METRICS:
            raise ValueError("experiment promotion metrics must use the strict gate")
        if contract.promotion_thresholds != REQUIRED_PROMOTION_THRESHOLDS:
            raise ValueError(
                "experiment promotion thresholds must use the strict gate"
            )
        training = date.fromisoformat(contract.training_cutoff)
        validation_start = date.fromisoformat(contract.validation_start)
        validation_end = date.fromisoformat(contract.validation_end)
        holdout_start = date.fromisoformat(contract.untouched_holdout_start)
        holdout_end = date.fromisoformat(contract.untouched_holdout_end)
        if not (
            training < validation_start <= validation_end
            < holdout_start <= holdout_end
        ):
            raise ValueError("experiment intervals must be chronological and disjoint")
        if len(contract.frozen_configuration_hash) != 64:
            raise ValueError("experiment frozen configuration hash is invalid")


def build_governance_overlay(
    *,
    strategy_rows: list[dict[str, Any]],
    generated_at: str | None = None,
    minimum_sessions_for_inert_quarantine: int = 20,
) -> dict[str, Any]:
    """Create an operator-review block overlay; never enable or promote a strategy."""

    entries: list[dict[str, Any]] = []
    for row in strategy_rows:
        strategy_id = str(row.get("strategy_id") or "")
        if not strategy_id:
            continue
        eligible_sessions = int(row.get("eligible_sessions") or 0)
        accepted_signals = int(row.get("accepted_signals") or 0)
        reason = ""
        if strategy_id == "failed_breakout_reversal_short":
            reason = "short_borrow_not_verified"
        elif (
            eligible_sessions >= minimum_sessions_for_inert_quarantine
            and accepted_signals == 0
        ):
            reason = "inert_activation_logic_unproven"
        if not reason:
            continue
        entries.append({
            "strategy_id": strategy_id,
            "strategy_version": str(row.get("strategy_version") or ""),
            "execution_policy_version": str(
                row.get("execution_policy_version") or ""
            ),
            "strategy_semantics_fingerprint": str(
                row.get("strategy_semantics_fingerprint") or ""
            ),
            "allow_entries": False,
            "reason": reason,
            "operator_review_required": True,
        })
    return {
        "schema_version": "v2.strategy_governance_overlay.v1",
        "generated_at": generated_at
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "entries": entries,
        "auto_enable_supported": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def write_experiment_registry(path: str | Path) -> dict[str, Any]:
    contracts = build_experiment_registry()
    payload: dict[str, Any] = {
        "schema_version": "v2.paperops_experiment_registry.v1",
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "experiments": [contract.to_dict() for contract in contracts],
        "auto_promotion_enabled": False,
        "operator_review_required": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["registry_hash_sha256"] = hashlib.sha256(
        json.dumps(
            payload["experiments"],
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    destination = Path(path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    temporary.replace(destination)
    return payload


__all__ = [
    "ExperimentContract",
    "REQUIRED_PROMOTION_METRICS",
    "REQUIRED_PROMOTION_THRESHOLDS",
    "build_experiment_registry",
    "build_intraday_v5_experiment_contract",
    "build_governance_overlay",
    "validate_experiment_registry",
    "write_experiment_registry",
]
