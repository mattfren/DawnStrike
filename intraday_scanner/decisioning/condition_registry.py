"""Complete condition registry for the nine research strategies."""

# The registry is deliberately readable as a policy table.
# ruff: noqa: E501

from __future__ import annotations

from intraday_scanner.decisioning.contracts import ConditionCategory, ConditionSpec

_COMMON_MARKET = (
    ("valid_symbol", "Ticker identity is syntactically valid", True, True),
    ("point_in_time_ohlcv", "Decision uses point-in-time OHLCV", True, True),
    ("positive_current_price", "Current price is positive", True, True),
    ("positive_current_volume", "Current volume is positive", True, True),
    ("source_identity_present", "Market source identity is present", True, True),
    ("source_fresh", "Market source is within its freshness limit", True, True),
    ("no_market_source_conflict", "Market providers do not conflict", True, True),
)
_COMMON_RISK = (
    ("not_currently_halted", "Symbol is not currently halted", True, True),
    ("valid_entry_reference", "Entry reference is positive and usable", True, True),
    ("valid_stop_geometry", "Stop geometry is valid for direction", True, True),
    ("valid_target_when_required", "Required target is present and valid", True, True),
    ("reward_risk_at_least_1_50", "Reward/risk is at least 1.50R", True, True),
    ("within_risk_budget", "Candidate remains within the risk budget", True, True),
    ("spread_within_existing_policy", "Spread remains inside existing policy", True, True),
)
_COMMON_ADVISORY = (
    ("float_known", "Float is known from a trusted source", False, False),
    ("secondary_source_present", "A secondary source is available", False, False),
    ("historical_sample_sufficient", "Historical sample is sufficient", False, False),
    ("catalyst_identified", "A catalyst is identified", False, False),
)

_STRATEGY_CONDITIONS: dict[str, tuple[tuple[str, ConditionCategory, str, bool, bool], ...]] = {
    "ts_momentum_sma_atr": (
        ("trend_regime", ConditionCategory.STRATEGY_CORE, "Trend regime passes", True, True),
        ("extension_guard", ConditionCategory.STRATEGY_CORE, "Extension guard passes", True, True),
        (
            "volatility_regime",
            ConditionCategory.STRATEGY_CORE,
            "Volatility regime passes",
            True,
            True,
        ),
        (
            "offering_or_dilution",
            ConditionCategory.AI_RESOLVABLE,
            "Offering or dilution risk is resolved",
            True,
            True,
        ),
        (
            "corporate_action",
            ConditionCategory.AI_RESOLVABLE,
            "Corporate action risk is resolved",
            True,
            True,
        ),
        (
            "material_adverse_event",
            ConditionCategory.AI_RESOLVABLE,
            "Material adverse event is resolved",
            True,
            True,
        ),
    ),
    "donchian_breakout_20_10": (
        (
            "breakout_quality",
            ConditionCategory.STRATEGY_CORE,
            "Breakout quality passes",
            True,
            True,
        ),
        ("extension_guard", ConditionCategory.STRATEGY_CORE, "Extension guard passes", True, True),
        ("participation", ConditionCategory.STRATEGY_CORE, "Participation passes", True, True),
        (
            "volatility_regime",
            ConditionCategory.STRATEGY_CORE,
            "Volatility regime passes",
            True,
            True,
        ),
        (
            "breakout_catalyst",
            ConditionCategory.AI_RESOLVABLE,
            "Breakout catalyst is resolved",
            False,
            False,
        ),
        (
            "recent_filing_risk",
            ConditionCategory.AI_RESOLVABLE,
            "Recent filing risk is resolved",
            True,
            True,
        ),
        (
            "offering_or_dilution",
            ConditionCategory.AI_RESOLVABLE,
            "Offering or dilution risk is resolved",
            True,
            True,
        ),
    ),
    "cross_sectional_relative_strength": (
        ("rank_membership", ConditionCategory.STRATEGY_CORE, "Rank membership passes", True, True),
        ("rank_margin", ConditionCategory.STRATEGY_CORE, "Rank margin passes", True, True),
        (
            "sector_concentration",
            ConditionCategory.STRATEGY_CORE,
            "Sector concentration is controlled",
            True,
            True,
        ),
        (
            "company_identity",
            ConditionCategory.AI_RESOLVABLE,
            "Company identity is resolved",
            True,
            True,
        ),
        (
            "sector_industry",
            ConditionCategory.AI_RESOLVABLE,
            "Sector and industry are resolved",
            False,
            True,
        ),
    ),
    "pullback_reclaim_uptrend": (
        ("trend_slope", ConditionCategory.STRATEGY_CORE, "Trend slope passes", True, True),
        ("waterfall_guard", ConditionCategory.STRATEGY_CORE, "Waterfall guard passes", True, True),
        (
            "reclaim_confirmation",
            ConditionCategory.STRATEGY_CORE,
            "Reclaim confirmation passes",
            True,
            True,
        ),
        (
            "material_adverse_event",
            ConditionCategory.AI_RESOLVABLE,
            "Material adverse event is resolved",
            True,
            True,
        ),
        (
            "offering_or_dilution",
            ConditionCategory.AI_RESOLVABLE,
            "Offering or dilution risk is resolved",
            True,
            True,
        ),
        (
            "regulatory_event",
            ConditionCategory.AI_RESOLVABLE,
            "Regulatory event is resolved",
            True,
            True,
        ),
    ),
    "volatility_contraction_breakout": (
        ("participation", ConditionCategory.STRATEGY_CORE, "Participation passes", True, True),
        (
            "dead_liquidity_guard",
            ConditionCategory.STRATEGY_CORE,
            "Dead liquidity guard passes",
            True,
            True,
        ),
        ("regime_guard", ConditionCategory.STRATEGY_CORE, "Regime guard passes", True, True),
        (
            "contraction_breakout",
            ConditionCategory.STRATEGY_CORE,
            "Contraction breakout passes",
            True,
            True,
        ),
        (
            "earnings_window",
            ConditionCategory.AI_RESOLVABLE,
            "Earnings window is resolved",
            False,
            False,
        ),
        (
            "regulatory_event",
            ConditionCategory.AI_RESOLVABLE,
            "Regulatory event is resolved",
            False,
            False,
        ),
        (
            "catalyst_timing",
            ConditionCategory.AI_RESOLVABLE,
            "Catalyst timing is resolved",
            False,
            False,
        ),
    ),
    "failed_breakout_reversal_short": (
        (
            "rejection_quality",
            ConditionCategory.STRATEGY_CORE,
            "Rejection quality passes",
            True,
            True,
        ),
        ("squeeze_guard", ConditionCategory.STRATEGY_CORE, "Squeeze guard passes", True, True),
        (
            "failed_breakout_confirmation",
            ConditionCategory.STRATEGY_CORE,
            "Failed breakout is confirmed",
            True,
            True,
        ),
        (
            "borrow_or_locate_verified",
            ConditionCategory.EXECUTION_ONLY,
            "Borrow or locate is verified",
            False,
            True,
        ),
        (
            "offering_or_dilution",
            ConditionCategory.AI_RESOLVABLE,
            "Offering or dilution risk is resolved",
            True,
            True,
        ),
        ("squeeze_event", ConditionCategory.AI_RESOLVABLE, "Squeeze event is resolved", True, True),
        (
            "corporate_action",
            ConditionCategory.AI_RESOLVABLE,
            "Corporate action risk is resolved",
            True,
            True,
        ),
    ),
    "bullish_fvg_continuation": (
        (
            "daily_ohlc_proxy",
            ConditionCategory.ADVISORY,
            "Daily OHLC is explicitly a proxy for intraday structure",
            False,
            False,
        ),
        ("gap_quality", ConditionCategory.STRATEGY_CORE, "Gap quality passes", True, True),
        ("participation", ConditionCategory.STRATEGY_CORE, "Participation passes", True, True),
        ("trend_quality", ConditionCategory.STRATEGY_CORE, "Trend quality passes", True, True),
        (
            "intraday_microstructure_confirmed",
            ConditionCategory.EXECUTION_ONLY,
            "Intraday microstructure is confirmed",
            False,
            True,
        ),
        (
            "catalyst_event",
            ConditionCategory.AI_RESOLVABLE,
            "Catalyst event is resolved",
            False,
            False,
        ),
        (
            "offering_or_dilution",
            ConditionCategory.AI_RESOLVABLE,
            "Offering or dilution risk is resolved",
            True,
            True,
        ),
    ),
    "gap_up_continuation": (
        ("gap_threshold", ConditionCategory.STRATEGY_CORE, "Gap threshold passes", True, True),
        ("close_location", ConditionCategory.STRATEGY_CORE, "Close location passes", True, True),
        ("trend", ConditionCategory.STRATEGY_CORE, "Trend passes", True, True),
        ("participation", ConditionCategory.STRATEGY_CORE, "Participation passes", True, True),
        ("data_quality", ConditionCategory.STRATEGY_CORE, "Data quality passes", True, True),
        (
            "corporate_action_basis",
            ConditionCategory.AI_RESOLVABLE,
            "Corporate-action basis is resolved",
            False,
            True,
        ),
        (
            "catalyst_event",
            ConditionCategory.AI_RESOLVABLE,
            "Catalyst event is resolved",
            False,
            False,
        ),
        (
            "offering_or_dilution",
            ConditionCategory.AI_RESOLVABLE,
            "Offering or dilution risk is resolved",
            True,
            True,
        ),
    ),
    "gap_up_continuation_atr": (
        ("gap_threshold", ConditionCategory.STRATEGY_CORE, "Gap threshold passes", True, True),
        ("close_location", ConditionCategory.STRATEGY_CORE, "Close location passes", True, True),
        ("trend", ConditionCategory.STRATEGY_CORE, "Trend passes", True, True),
        ("participation", ConditionCategory.STRATEGY_CORE, "Participation passes", True, True),
        ("data_quality", ConditionCategory.STRATEGY_CORE, "Data quality passes", True, True),
        (
            "atr_normalization_valid",
            ConditionCategory.STRATEGY_CORE,
            "ATR normalization is valid",
            True,
            True,
        ),
        (
            "volatility_event_context",
            ConditionCategory.STRATEGY_CORE,
            "Volatility event context is valid",
            True,
            True,
        ),
        (
            "corporate_action_basis",
            ConditionCategory.AI_RESOLVABLE,
            "Corporate-action basis is resolved",
            False,
            True,
        ),
        (
            "catalyst_event",
            ConditionCategory.AI_RESOLVABLE,
            "Catalyst event is resolved",
            False,
            False,
        ),
        (
            "offering_or_dilution",
            ConditionCategory.AI_RESOLVABLE,
            "Offering or dilution risk is resolved",
            True,
            True,
        ),
    ),
}


def _spec(
    strategy_id: str,
    condition_id: str,
    category: ConditionCategory,
    description: str,
    research: bool,
    paper: bool,
) -> ConditionSpec:
    resolver = (
        "strategy_gap_resolver" if category == ConditionCategory.AI_RESOLVABLE else "deterministic"
    )
    return ConditionSpec(
        condition_id=condition_id,
        strategy_id=strategy_id,
        category=category,
        description=description,
        blocking_for_research_pick=research,
        blocking_for_paper_entry=paper,
        freshness_limit_seconds=86_400
        if category in {ConditionCategory.AI_RESOLVABLE, ConditionCategory.ADVISORY}
        else 300,
        required_source_types=("market_feed",)
        if category
        in {
            ConditionCategory.HARD_MARKET,
            ConditionCategory.HARD_RISK,
            ConditionCategory.STRATEGY_CORE,
        }
        else ("sec", "issuer_ir", "regulatory_notice"),
        resolver_id=resolver,
        threshold_contract={"policy": "existing"},
        missing_policy="BLOCKED_SAFETY"
        if category == ConditionCategory.HARD_RISK
        else "BLOCKED_DATA",
    )


def build_condition_registry() -> tuple[ConditionSpec, ...]:
    rows: list[ConditionSpec] = []
    for strategy_id, specifics in _STRATEGY_CONDITIONS.items():
        for condition_id, description, research, paper in _COMMON_MARKET:
            rows.append(
                _spec(
                    strategy_id,
                    condition_id,
                    ConditionCategory.HARD_MARKET,
                    description,
                    research,
                    paper,
                )
            )
        for condition_id, description, research, paper in _COMMON_RISK:
            rows.append(
                _spec(
                    strategy_id,
                    condition_id,
                    ConditionCategory.HARD_RISK,
                    description,
                    research,
                    paper,
                )
            )
        for condition_id, description, research, paper in _COMMON_ADVISORY:
            rows.append(
                _spec(
                    strategy_id,
                    condition_id,
                    ConditionCategory.ADVISORY,
                    description,
                    research,
                    paper,
                )
            )
        for condition_id, category, description, research, paper in specifics:
            rows.append(_spec(strategy_id, condition_id, category, description, research, paper))
    return tuple(rows)


def registry_for_strategy(strategy_id: str) -> tuple[ConditionSpec, ...]:
    rows = tuple(row for row in build_condition_registry() if row.strategy_id == strategy_id)
    if not rows:
        raise KeyError(f"unknown strategy: {strategy_id}")
    return rows


def strategy_ids() -> tuple[str, ...]:
    return tuple(_STRATEGY_CONDITIONS)


__all__ = ["build_condition_registry", "registry_for_strategy", "strategy_ids"]
