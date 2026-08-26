"""AlphaOps v4 research layer.

This package is intentionally additive to the Signal Engine v3 scanner.  It
does not place orders, hold broker credentials, or execute trades.
"""

from intraday_scanner.alpha.alpha_model import ALPHA_MODEL_VERSION, AlphaModel
from intraday_scanner.alpha.feature_factory import FEATURE_MODEL_VERSION, build_feature_vector
from intraday_scanner.alpha.no_trade_filter import NoTradeDecision, evaluate_no_trade
from intraday_scanner.alpha.plan_constructor import (
    ALLOWED_DERIVATION_POLICIES,
    AlphaOpsMarketStructurePlan,
    PlanObservation,
    apply_structural_level_enrichment,
    build_alphaops_v5_plan,
    build_market_structure_plan,
    construct_alphaops_v5_plan,
    is_valid_alphaops_v5_plan,
    validate_alphaops_v5_plan,
)
from intraday_scanner.alpha.risk_governor import RiskDecision, evaluate_risk
from intraday_scanner.alpha.episode_identity import (
    EpisodeIdentity,
    EpisodeIdentityError,
    build_episode_identity,
    canonical_episode_identity,
    deduplicate_episode_candidates,
)

__all__ = [
    "ALPHA_MODEL_VERSION",
    "ALLOWED_DERIVATION_POLICIES",
    "FEATURE_MODEL_VERSION",
    "AlphaModel",
    "AlphaOpsMarketStructurePlan",
    "apply_structural_level_enrichment",
    "NoTradeDecision",
    "PlanObservation",
    "RiskDecision",
    "build_feature_vector",
    "build_alphaops_v5_plan",
    "build_market_structure_plan",
    "construct_alphaops_v5_plan",
    "is_valid_alphaops_v5_plan",
    "validate_alphaops_v5_plan",
    "evaluate_no_trade",
    "evaluate_risk",
    "EpisodeIdentity",
    "EpisodeIdentityError",
    "build_episode_identity",
    "canonical_episode_identity",
    "deduplicate_episode_candidates",
]
