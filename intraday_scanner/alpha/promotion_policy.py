"""Deterministic strategy-promotion gates; no LLM or UI decision path."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

PROMOTION_POLICY_VERSION = "dawnstrike-promotion-policy-v1"


@dataclass(frozen=True, slots=True)
class PromotionEvidence:
    strategy_id: str
    strategy_version: str
    cohort: str
    real_market_days: int | None
    closed_forward_trades: int | None
    eligible_outcome_coverage_pct: float | None
    net_expectancy_pct: float | None
    profit_factor: float | None
    excess_return_vs_cash_pct: float | None
    excess_return_vs_benchmark_pct: float | None
    max_forward_drawdown_pct: float | None
    max_gain_concentration_pct: float | None
    max_loss_concentration_pct: float | None
    walk_forward_positive: bool | None
    holdout_positive: bool | None
    slippage_stress_positive: bool | None
    no_lookahead_passed: bool | None
    source_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PromotionDecision:
    status: str
    action: str
    model_version: str
    strategy_id: str
    strategy_version: str
    cohort: str
    component_scores: dict[str, float | int | None]
    vetoes: tuple[str, ...]
    source_refs: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "vetoes": list(self.vetoes), "source_refs": list(self.source_refs)}


def evaluate_promotion(evidence: PromotionEvidence) -> PromotionDecision:
    values = asdict(evidence)
    vetoes: list[str] = []
    gates: tuple[tuple[str, bool], ...] = (
        ("real_market_days_lt_60", _at_least(evidence.real_market_days, 60)),
        ("closed_forward_trades_lt_100", _at_least(evidence.closed_forward_trades, 100)),
        ("outcome_coverage_lt_98_pct", _at_least(evidence.eligible_outcome_coverage_pct, 98.0)),
        ("net_expectancy_not_positive", _positive(evidence.net_expectancy_pct)),
        ("profit_factor_lt_1_20", _at_least(evidence.profit_factor, 1.20)),
        ("cash_excess_not_positive", _positive(evidence.excess_return_vs_cash_pct)),
        ("benchmark_excess_not_positive", _positive(evidence.excess_return_vs_benchmark_pct)),
        ("drawdown_worse_than_8_pct", _drawdown_passes(evidence.max_forward_drawdown_pct)),
        ("gain_concentration_over_25_pct", _at_most(evidence.max_gain_concentration_pct, 25.0)),
        ("loss_concentration_over_25_pct", _at_most(evidence.max_loss_concentration_pct, 25.0)),
        ("walk_forward_not_positive", evidence.walk_forward_positive is True),
        ("holdout_not_positive", evidence.holdout_positive is True),
        ("slippage_stress_not_positive", evidence.slippage_stress_positive is True),
        ("lookahead_audit_failed", evidence.no_lookahead_passed is True),
    )
    for reason, passed in gates:
        if not passed:
            vetoes.append(reason)
    missing = [
        name for name, value in values.items() if value is None and name not in {"source_refs"}
    ]
    vetoes.extend(f"missing_{name}" for name in missing)
    vetoes = list(dict.fromkeys(vetoes))
    has_observed_failure = any(
        reason in vetoes
        for reason in {
            "net_expectancy_not_positive",
            "profit_factor_lt_1_20",
            "cash_excess_not_positive",
            "benchmark_excess_not_positive",
            "drawdown_worse_than_8_pct",
            "gain_concentration_over_25_pct",
            "loss_concentration_over_25_pct",
            "walk_forward_not_positive",
            "holdout_not_positive",
            "slippage_stress_not_positive",
            "lookahead_audit_failed",
        }
    )
    status = "FAILED" if has_observed_failure and not missing else "WAITING_FOR_FORWARD_EVIDENCE"
    if not vetoes:
        status = "PRODUCTION_VERIFIED"
    return PromotionDecision(
        status=status,
        action="PROMOTE" if not vetoes else "HOLD",
        model_version=PROMOTION_POLICY_VERSION,
        strategy_id=evidence.strategy_id,
        strategy_version=evidence.strategy_version,
        cohort=evidence.cohort,
        component_scores={
            "real_market_days": evidence.real_market_days,
            "closed_forward_trades": evidence.closed_forward_trades,
            "coverage_pct": evidence.eligible_outcome_coverage_pct,
            "profit_factor": evidence.profit_factor,
        },
        vetoes=tuple(vetoes),
        source_refs=evidence.source_refs,
    )


def _at_least(value: float | int | None, threshold: float) -> bool:
    return value is not None and value >= threshold


def _at_most(value: float | None, threshold: float) -> bool:
    return value is not None and value <= threshold


def _positive(value: float | None) -> bool:
    return value is not None and value > 0


def _drawdown_passes(value: float | None) -> bool:
    return value is not None and value >= -8.0
