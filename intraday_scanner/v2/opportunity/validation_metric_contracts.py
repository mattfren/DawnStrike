"""Strict downstream contracts for bounded validation trading metrics.

This module is intentionally absent from the real-time opportunity import graph.
It defines research-only policy and status vocabulary; population and report
builders derive every numeric value from accepted validation/outcome evidence.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from intraday_scanner.v2.opportunity.models import (
    OpportunityContract,
    stable_identity,
)
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _quantize_metric_decimal,
)


class ExecutionStressScenario(str, Enum):
    BASE = "base"
    COST_2X = "cost_2x"
    COST_3X = "cost_3x"


CANONICAL_EXECUTION_STRESS_SCENARIOS = tuple(ExecutionStressScenario)
EXECUTION_STRESS_MULTIPLIERS = {
    ExecutionStressScenario.BASE: Decimal("1"),
    ExecutionStressScenario.COST_2X: Decimal("2"),
    ExecutionStressScenario.COST_3X: Decimal("3"),
}


class ValidationMetricReportStatus(str, Enum):
    AVAILABLE = "available"
    PROVISIONAL = "provisional"
    INSUFFICIENT_DATA = "insufficient_data"
    INCOMPLETE = "incomplete"
    EXTERNAL_DATA_BLOCKED = "external_data_blocked"


class ValidationMetricValueStatus(str, Enum):
    AVAILABLE = "available"
    PROVISIONAL = "provisional"
    INSUFFICIENT_DATA = "insufficient_data"
    INCOMPLETE = "incomplete"
    EXTERNAL_DATA_BLOCKED = "external_data_blocked"
    UNAVAILABLE = "unavailable"


class ValidationMetricScopeKind(str, Enum):
    FINAL_TRAIN_RESEARCH = "final_train_research"
    FINAL_VALIDATION = "final_validation"
    FOLD_TRAIN = "fold_train"
    FOLD_VALIDATION = "fold_validation"


class TradeMetricDisposition(str, Enum):
    RESOLVED_FILL_COST_COMPLETE = "resolved_fill_cost_complete"
    RESOLVED_FILL_COST_UNAVAILABLE = "resolved_fill_cost_unavailable"
    EXACT_NO_FILL = "exact_no_fill"
    NON_TAKE = "non_take"
    UNRESOLVED_TAKE = "unresolved_take"


class ExecutionCostEvidenceQuality(str, Enum):
    EMPIRICAL = "empirical"
    PROVISIONAL = "provisional"
    NONCONSOLIDATED = "nonconsolidated"
    UNAVAILABLE = "unavailable"
    NOT_APPLICABLE = "not_applicable"


class ValidationTradingMetric(str, Enum):
    TOTAL_TRADES = "total_trades"
    WINS = "wins"
    LOSSES = "losses"
    BREAKEVENS = "breakevens"
    WIN_RATE = "win_rate"
    EXPECTANCY_R = "expectancy_r"
    PROFIT_FACTOR = "profit_factor"
    AVERAGE_WIN_R = "average_win_r"
    AVERAGE_LOSS_R = "average_loss_r"
    MAXIMUM_FAVORABLE_EXCURSION_R = "maximum_favorable_excursion_r"
    MAXIMUM_ADVERSE_EXCURSION_R = "maximum_adverse_excursion_r"
    AVERAGE_HOLDING_LOWER_SECONDS = "average_holding_lower_seconds"
    AVERAGE_HOLDING_UPPER_SECONDS = "average_holding_upper_seconds"
    MEAN_SESSION_R = "mean_session_r"
    SESSION_R_SHARPE = "session_r_sharpe"
    SESSION_R_SORTINO = "session_r_sortino"
    SESSION_R_MAX_DRAWDOWN = "session_r_max_drawdown"
    SESSION_R_MAX_DRAWDOWN_DURATION_SESSIONS = (
        "session_r_max_drawdown_duration_sessions"
    )
    TOTAL_EXECUTION_COST_USD = "total_execution_cost_usd"
    CAPITAL_MAX_DRAWDOWN = "capital_max_drawdown"
    ANNUALIZED_RETURN = "annualized_return"
    BENCHMARK_EXCESS_RETURN = "benchmark_excess_return"


class ValidationTradingMetricUnit(str, Enum):
    COUNT = "count"
    FRACTION = "fraction"
    RATIO = "ratio"
    SECONDS = "seconds"
    SESSIONS = "sessions"
    USD = "usd"


CANONICAL_VALIDATION_TRADING_METRICS = tuple(ValidationTradingMetric)
VALIDATION_TRADING_METRIC_UNITS = {
    ValidationTradingMetric.TOTAL_TRADES: ValidationTradingMetricUnit.COUNT,
    ValidationTradingMetric.WINS: ValidationTradingMetricUnit.COUNT,
    ValidationTradingMetric.LOSSES: ValidationTradingMetricUnit.COUNT,
    ValidationTradingMetric.BREAKEVENS: ValidationTradingMetricUnit.COUNT,
    ValidationTradingMetric.WIN_RATE: ValidationTradingMetricUnit.FRACTION,
    ValidationTradingMetric.EXPECTANCY_R: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.PROFIT_FACTOR: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.AVERAGE_WIN_R: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.AVERAGE_LOSS_R: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.MAXIMUM_FAVORABLE_EXCURSION_R: (
        ValidationTradingMetricUnit.RATIO
    ),
    ValidationTradingMetric.MAXIMUM_ADVERSE_EXCURSION_R: (
        ValidationTradingMetricUnit.RATIO
    ),
    ValidationTradingMetric.AVERAGE_HOLDING_LOWER_SECONDS: (
        ValidationTradingMetricUnit.SECONDS
    ),
    ValidationTradingMetric.AVERAGE_HOLDING_UPPER_SECONDS: (
        ValidationTradingMetricUnit.SECONDS
    ),
    ValidationTradingMetric.MEAN_SESSION_R: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.SESSION_R_SHARPE: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.SESSION_R_SORTINO: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.SESSION_R_MAX_DRAWDOWN: ValidationTradingMetricUnit.RATIO,
    ValidationTradingMetric.SESSION_R_MAX_DRAWDOWN_DURATION_SESSIONS: (
        ValidationTradingMetricUnit.SESSIONS
    ),
    ValidationTradingMetric.TOTAL_EXECUTION_COST_USD: ValidationTradingMetricUnit.USD,
    ValidationTradingMetric.CAPITAL_MAX_DRAWDOWN: ValidationTradingMetricUnit.FRACTION,
    ValidationTradingMetric.ANNUALIZED_RETURN: ValidationTradingMetricUnit.FRACTION,
    ValidationTradingMetric.BENCHMARK_EXCESS_RETURN: ValidationTradingMetricUnit.FRACTION,
}


class ValidationSegmentDimension(str, Enum):
    DIRECTION = "direction"
    STRATEGY = "strategy"
    SECURITY_REGIME = "security_regime"
    MARKET_STATE = "market_state"
    REGIME_PAIR = "regime_pair"
    TIME_OF_DAY = "time_of_day"
    WEEKDAY = "weekday"
    MONTH = "month"
    YEAR = "year"
    LIQUIDITY_BUCKET = "liquidity_bucket"
    VOLATILITY_BUCKET = "volatility_bucket"
    CATALYST = "catalyst"


@dataclass(frozen=True)
class ValidationTradingMetricPolicy(OutcomeContract):
    policy_id: str
    policy_version: str
    stress_scenarios: tuple[ExecutionStressScenario, ...]
    decimal_precision: int
    decimal_scale: int
    rounding_mode: str
    session_downside_target_r: Decimal
    liquidity_low_percentile: Decimal
    liquidity_high_percentile: Decimal
    volatility_compression_ratio: Decimal
    volatility_expansion_ratio: Decimal
    segment_dimensions: tuple[ValidationSegmentDimension, ...]
    retain_all_coverage_rows: bool = True
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.validation_trading_metric_policy.v1"

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.validation_trading_metric_policy.v1",
        )
        _require_identity(self.policy_id, "policy_id")
        _require_sanitized_text(self.policy_version, "policy_version")
        if self.stress_scenarios != CANONICAL_EXECUTION_STRESS_SCENARIOS:
            raise ValueError("execution stress scenarios must use canonical order")
        if type(self.decimal_precision) is not int or self.decimal_precision != 64:
            raise ValueError("validation metric precision must be exactly 64")
        if type(self.decimal_scale) is not int or self.decimal_scale != 12:
            raise ValueError("validation metric scale must be exactly 12")
        if self.rounding_mode != "ROUND_HALF_EVEN":
            raise ValueError("validation metric rounding must be ROUND_HALF_EVEN")
        for value, name in (
            (self.session_downside_target_r, "session_downside_target_r"),
            (self.liquidity_low_percentile, "liquidity_low_percentile"),
            (self.liquidity_high_percentile, "liquidity_high_percentile"),
            (self.volatility_compression_ratio, "volatility_compression_ratio"),
            (self.volatility_expansion_ratio, "volatility_expansion_ratio"),
        ):
            _require_finite_decimal(value, name)
        if self.session_downside_target_r != Decimal("0"):
            raise ValueError("session downside target must be zero R")
        if not (
            Decimal("0")
            < self.liquidity_low_percentile
            < self.liquidity_high_percentile
            < Decimal("1")
        ):
            raise ValueError("liquidity percentile boundaries are invalid")
        if not (
            Decimal("0") < self.volatility_compression_ratio < Decimal("1")
            and self.volatility_expansion_ratio > Decimal("1")
        ):
            raise ValueError("volatility ratio boundaries are invalid")
        if (
            self.liquidity_low_percentile != Decimal("0.25")
            or self.liquidity_high_percentile != Decimal("0.75")
            or self.volatility_compression_ratio != Decimal("0.70")
            or self.volatility_expansion_ratio != Decimal("1.50")
        ):
            raise ValueError("validation metric v1 bucket thresholds are fixed")
        if self.segment_dimensions != tuple(ValidationSegmentDimension):
            raise ValueError("segment dimensions must use canonical exhaustive order")
        if not self.retain_all_coverage_rows:
            raise ValueError("validation metrics must retain every coverage row")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("validation metric policy must remain research-only")
        expected = stable_identity(
            "validation-trading-metric-policy",
            _identity_payload(self, "policy_id"),
        )
        if self.policy_id != expected:
            raise ValueError("validation metric policy identity does not match content")


def build_validation_trading_metric_policy(
    *,
    policy_version: str,
) -> ValidationTradingMetricPolicy:
    liquidity_low_percentile = Decimal("0.25")
    liquidity_high_percentile = Decimal("0.75")
    volatility_compression_ratio = Decimal("0.70")
    volatility_expansion_ratio = Decimal("1.50")
    values = {
        "policy_version": policy_version,
        "stress_scenarios": CANONICAL_EXECUTION_STRESS_SCENARIOS,
        "decimal_precision": 64,
        "decimal_scale": 12,
        "rounding_mode": "ROUND_HALF_EVEN",
        "session_downside_target_r": Decimal("0"),
        "liquidity_low_percentile": liquidity_low_percentile,
        "liquidity_high_percentile": liquidity_high_percentile,
        "volatility_compression_ratio": volatility_compression_ratio,
        "volatility_expansion_ratio": volatility_expansion_ratio,
        "segment_dimensions": tuple(ValidationSegmentDimension),
        "retain_all_coverage_rows": True,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.validation_trading_metric_policy.v1",
    }
    return ValidationTradingMetricPolicy(
        policy_id=stable_identity("validation-trading-metric-policy", values),
        policy_version=policy_version,
        stress_scenarios=CANONICAL_EXECUTION_STRESS_SCENARIOS,
        decimal_precision=64,
        decimal_scale=12,
        rounding_mode="ROUND_HALF_EVEN",
        session_downside_target_r=Decimal("0"),
        liquidity_low_percentile=liquidity_low_percentile,
        liquidity_high_percentile=liquidity_high_percentile,
        volatility_compression_ratio=volatility_compression_ratio,
        volatility_expansion_ratio=volatility_expansion_ratio,
        segment_dimensions=tuple(ValidationSegmentDimension),
    )


def quantize_validation_metric(
    value: Decimal,
    policy: ValidationTradingMetricPolicy,
) -> Decimal:
    _require_finite_decimal(value, "validation metric value")
    return _quantize_metric_decimal(value, policy)


def _require_finite_decimal(value: Decimal, name: str) -> None:
    if type(value) is not Decimal or not value.is_finite():
        raise ValueError(f"{name} must be a finite Decimal")


__all__ = [
    "CANONICAL_EXECUTION_STRESS_SCENARIOS",
    "CANONICAL_VALIDATION_TRADING_METRICS",
    "EXECUTION_STRESS_MULTIPLIERS",
    "ExecutionCostEvidenceQuality",
    "ExecutionStressScenario",
    "TradeMetricDisposition",
    "ValidationMetricReportStatus",
    "ValidationMetricScopeKind",
    "ValidationMetricValueStatus",
    "ValidationSegmentDimension",
    "ValidationTradingMetric",
    "ValidationTradingMetricPolicy",
    "ValidationTradingMetricUnit",
    "VALIDATION_TRADING_METRIC_UNITS",
    "build_validation_trading_metric_policy",
    "quantize_validation_metric",
]
