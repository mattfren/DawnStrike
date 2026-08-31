"""Mechanical strategy catalog for the v2 Alpha Lab."""

from intraday_scanner.v2.strategies.alphaops_intraday import (
    IntradayDecisionPoint,
    IntradayPolicyEvaluation,
    build_alphaops_intraday_strategy,
    build_point_in_time_observation,
    evaluate_alphaops_intraday,
)
from intraday_scanner.v2.strategies.catalog import describe_strategy
from intraday_scanner.v2.strategies.challengers import (
    CHALLENGER_VERSION,
    GateEvaluation,
    GateResult,
    build_challenger_catalog,
    challenger_version_for,
    evaluate_challenger_gates,
)
from intraday_scanner.v2.strategies.combined_catalog import build_strategy_catalog
from intraday_scanner.v2.strategies.models import Direction, StrategySignal, StrategySpec
from intraday_scanner.v2.strategies.shadow_intraday import (
    EMPIRICAL_COST_REQUIRED,
    SHADOW_PROTOCOL_VERSION,
    ShadowEvaluation,
    ShadowSignal,
    ShadowStrategyConfig,
    ShadowStrategyRegistry,
    build_shadow_strategy_registry,
    evaluate_shadow_event,
    evaluate_shadow_strategy,
)

__all__ = [
    "Direction",
    "IntradayDecisionPoint",
    "IntradayPolicyEvaluation",
    "StrategySignal",
    "StrategySpec",
    "build_alphaops_intraday_strategy",
    "build_point_in_time_observation",
    "build_strategy_catalog",
    "build_challenger_catalog",
    "challenger_version_for",
    "evaluate_challenger_gates",
    "GateEvaluation",
    "GateResult",
    "CHALLENGER_VERSION",
    "describe_strategy",
    "evaluate_alphaops_intraday",
    "EMPIRICAL_COST_REQUIRED",
    "SHADOW_PROTOCOL_VERSION",
    "ShadowEvaluation",
    "ShadowSignal",
    "ShadowStrategyConfig",
    "ShadowStrategyRegistry",
    "build_shadow_strategy_registry",
    "evaluate_shadow_event",
    "evaluate_shadow_strategy",
]
