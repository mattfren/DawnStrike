"""AlphaOps v4 research layer.

This package is intentionally additive to the Signal Engine v3 scanner.  It
does not place orders, hold broker credentials, or execute trades.
"""

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION, AlphaModel
from intraday_scanner.alpha.feature_factory import FEATURE_MODEL_VERSION, build_feature_vector
from intraday_scanner.alpha.no_trade_filter import NoTradeDecision, evaluate_no_trade
from intraday_scanner.alpha.risk_governor import RiskDecision, evaluate_risk

__all__ = [
    "ALPHA_MODEL_VERSION",
    "FEATURE_MODEL_VERSION",
    "AlphaModel",
    "NoTradeDecision",
    "RiskDecision",
    "build_feature_vector",
    "evaluate_no_trade",
    "evaluate_risk",
]
