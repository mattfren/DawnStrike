"""AlphaOps V6 deterministic research contracts.

This package deliberately contains no broker, execution, or LLM integration.
Every module works only with persisted, point-in-time research evidence.
"""

from intraday_scanner.alpha.v6.contracts import (
    ALPHAOPS_V6_MODEL_VERSION,
    ALPHAOPS_V6_STRATEGY_VERSION,
    FEATURE_SCHEMA_V2,
    V6_COST_MODEL_VERSION,
)

__all__ = [
    "ALPHAOPS_V6_MODEL_VERSION",
    "ALPHAOPS_V6_STRATEGY_VERSION",
    "V6_COST_MODEL_VERSION",
    "FEATURE_SCHEMA_V2",
]
