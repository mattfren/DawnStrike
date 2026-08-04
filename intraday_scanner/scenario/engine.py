"""Deterministic policy that turns extracted facts into research-only scenarios."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from intraday_scanner.scenario.contracts import (
    SCENARIO_FEATURE_SCHEMA_VERSION,
    SCENARIO_FORWARD_COHORT,
    SCENARIO_POLICY_VERSION,
    ScenarioDecision,
    ScenarioExtraction,
    ScenarioNewsArticle,
    canonical_hash,
)

_EVENT_PRIORS = {
    "earnings_guidance": 3.0,
    "contract_customer": 3.0,
    "regulatory_fda": 3.0,
    "mergers_acquisitions": 2.5,
    "financing_dilution": -4.0,
    "exchange_halt": -4.0,
    "bankruptcy_distress": -5.0,
    "litigation": -2.0,
    "cybersecurity": -2.0,
    "recall": -2.0,
    "management_change": -1.0,
    "analyst_action": 1.0,
    "product_event": 1.5,
    "macro_sector": 0.5,
    "rumor": 0.0,
    "other": 0.0,
}
_TIER_BONUS = {"T1": 2.0, "T2": 1.0, "T3": 0.0, "UNKNOWN": -3.0}
_MATERIALITY_BONUS = {"high": 2.0, "medium": 1.0, "low": 0.0, "unknown": -0.5}


@dataclass(frozen=True)
class PriceContext:
    observed_at: str
    price: float | None
    atr: float | None
    spread_pct: float | None
    liquid: bool
    source_bar_hash_sha256: str
    source_kind: str = "minute_bars"


def evaluate_scenario(
    *,
    article: ScenarioNewsArticle,
    extraction: ScenarioExtraction,
    ticker: str,
    decision_at: str,
    price_context: PriceContext | None,
    cohort: str = SCENARIO_FORWARD_COHORT,
) -> ScenarioDecision:
    """Produce a reproducible research decision without predicted probabilities.

    The extractor supplies structured facts only. Every action, price level, and
    veto is set here using sourced article lineage plus completed market bars.
    """

    ticker = ticker.upper().strip()
    reason_codes: list[str] = []
    score = 0.0
    event_type = "other"
    directions: set[str] = set()
    feature_claims: list[dict[str, Any]] = []
    if extraction.status != "ok":
        reason_codes.append(extraction.abstain_reason or "extractor_abstained")
    else:
        for claim in extraction.claims:
            event_type = claim.event_type if event_type == "other" else event_type
            directions.add(claim.direction)
            direction_multiplier = (
                1.0
                if claim.direction == "bullish"
                else -1.0
                if claim.direction == "bearish"
                else 0.0
            )
            claim_score = _EVENT_PRIORS.get(claim.event_type, 0.0) * direction_multiplier
            claim_score += _MATERIALITY_BONUS.get(claim.materiality, -0.5) * direction_multiplier
            score += claim_score
            feature_claims.append(
                {
                    "event_type": claim.event_type,
                    "direction": claim.direction,
                    "materiality": claim.materiality,
                    "evidence_count": len(claim.evidence_spans),
                    "uncertainty_flags": list(claim.uncertainty_flags),
                }
            )
            if claim.uncertainty_flags:
                reason_codes.extend(f"uncertainty:{flag}" for flag in claim.uncertainty_flags)
    tier = article.tier
    score += _TIER_BONUS[tier]
    if tier == "UNKNOWN":
        reason_codes.append("unknown_source")
    if article.timing_kind != "forward_observed":
        reason_codes.append("historical_provider_timestamp_proxy")
    if "rumor" == event_type or any(claim.event_type == "rumor" for claim in extraction.claims):
        reason_codes.append("rumor_requires_independent_corroboration")
    if "bullish" in directions and "bearish" in directions:
        reason_codes.append("contradictory_claim_directions")
    direction = "bullish" if score >= 1.0 else "bearish" if score <= -1.0 else "mixed"
    action = "WATCH"
    entry_trigger = invalidation = target = None
    blocked = bool(reason_codes) and any(
        code
        in {
            "unknown_source",
            "rumor_requires_independent_corroboration",
            "contradictory_claim_directions",
        }
        or code.startswith("uncertainty:")
        for code in reason_codes
    )
    if extraction.status != "ok" or blocked:
        action = "ABSTAIN"
    elif direction == "bearish" and score <= -3.0:
        action = "AVOID"
    elif direction == "bullish" and score >= 4.0:
        if price_context is None:
            reason_codes.append("price_context_missing")
        elif price_context.price is None or price_context.atr is None or price_context.atr <= 0:
            reason_codes.append("completed_bar_atr_missing")
        elif not price_context.liquid:
            reason_codes.append("liquidity_veto")
        elif price_context.spread_pct is None or price_context.spread_pct > 2.0:
            reason_codes.append("spread_veto")
        else:
            price = price_context.price
            risk_unit = max(price_context.atr, price * 0.02)
            entry_trigger = round(max(price * 1.0025, price + 0.15 * price_context.atr), 4)
            invalidation = round(entry_trigger - risk_unit, 4)
            target = round(entry_trigger + 2 * risk_unit, 4)
            if invalidation > 0 and target > entry_trigger:
                action = "ENTER_LONG"
            else:
                reason_codes.append("invalid_levels")
    elif direction == "bearish":
        action = "AVOID"
    features = {
        "article_id": article.article_id,
        "timing_kind": article.timing_kind,
        "source_tier": tier,
        "claims": feature_claims,
        "score_components": {
            "directional_score": round(score, 4),
            "tier_bonus": _TIER_BONUS[tier],
        },
        "price_context": (
            {
                "observed_at": price_context.observed_at,
                "price": price_context.price,
                "atr": price_context.atr,
                "spread_pct": price_context.spread_pct,
                "liquid": price_context.liquid,
                "source_bar_hash_sha256": price_context.source_bar_hash_sha256,
                "source_kind": price_context.source_kind,
            }
            if price_context
            else None
        ),
        "policy_version": SCENARIO_POLICY_VERSION,
        "feature_schema_version": SCENARIO_FEATURE_SCHEMA_VERSION,
    }
    feature_hash = canonical_hash(features)
    decision_id = canonical_hash(
        {
            "article": article.article_id,
            "ticker": ticker,
            "cohort": cohort,
            "features": feature_hash,
        }
    )[:32]
    return ScenarioDecision(
        decision_id=decision_id,
        article_id=article.article_id,
        ticker=ticker,
        market_date=decision_at[:10],
        decision_at=decision_at,
        event_type=event_type,
        direction=direction,
        directional_evidence_score=round(score, 4),
        action=action,
        reason_codes=tuple(sorted(set(reason_codes))),
        source_tier=tier,
        source_lineage_hash_sha256=article.source_lineage_hash_sha256,
        feature_hash_sha256=feature_hash,
        features=features,
        entry_trigger=entry_trigger,
        invalidation_level=invalidation,
        target_1=target,
        cohort=cohort,
    )
