"""Risk gating for AlphaOps watchlist alerts."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class RiskDecision:
    ticker: str
    can_alert: bool
    risk_score: float
    risk_flags: list[str]
    avoid_reasons: list[str]
    hard_avoid_reasons: list[str]
    soft_penalties: list[str]
    strategy_receipt_tier: str = ""
    strategy_receipt_research_pick_eligible: bool | None = None
    strategy_receipt_paper_entry_eligible: bool | None = None
    strategy_receipt_disagreement: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "can_alert": self.can_alert,
            "risk_score": self.risk_score,
            "risk_flags": self.risk_flags,
            "avoid_reasons": self.avoid_reasons,
            "hard_avoid_reasons": self.hard_avoid_reasons,
            "soft_penalties": self.soft_penalties,
            "strategy_receipt_tier": self.strategy_receipt_tier,
            "strategy_receipt_research_pick_eligible": (
                self.strategy_receipt_research_pick_eligible
            ),
            "strategy_receipt_paper_entry_eligible": self.strategy_receipt_paper_entry_eligible,
            "strategy_receipt_disagreement": list(self.strategy_receipt_disagreement or []),
        }


HARD_AVOID_FLAGS = {
    "current_halt",
    "recent_offering",
    "active_dilution",
    "active_offering",
    "offering",
    "zero_volume",
    "price_below_min",
    "sub_min_price",
    "stale_source",
    "stale_data",
    "source_conflict",
    "no_source_confidence",
}


def evaluate_risk(
    candidate: dict[str, Any],
    features: dict[str, Any] | None = None,
    *,
    min_price: float = 0.50,
    max_spread_pct: float = 8.0,
    min_source_confidence: float = 20.0,
) -> RiskDecision:
    features = dict(features or {})
    ticker = str(candidate.get("ticker") or features.get("ticker") or "").strip().upper()
    raw_flags = _tokens(candidate.get("risk_flags")) + _tokens(features.get("risk_flags"))
    raw_avoids = _tokens(candidate.get("avoid_reasons")) + _tokens(features.get("avoid_reasons"))
    risk_flags = _dedupe(raw_flags)
    avoid_reasons = _dedupe(raw_avoids)
    hard: list[str] = []
    soft: list[str] = []
    receipt_tier = str(candidate.get("strategy_receipt_tier") or "").strip().upper()
    receipt_research_eligible = _bool_or_none(
        candidate.get("strategy_receipt_research_pick_eligible")
    )
    receipt_paper_eligible = _bool_or_none(
        candidate.get("strategy_receipt_paper_entry_eligible")
    )
    receipt_disagreements: list[str] = []
    receipt_enabled = _truthy(candidate.get("strategy_receipt_enabled"))
    receipt_shadow_only = _truthy(candidate.get("strategy_receipt_shadow_only"))

    price = _float(_candidate_or_feature(candidate, features, "premarket_price"))
    volume = _float(_candidate_or_feature(candidate, features, "premarket_volume"))
    spread = _float(_candidate_or_feature(candidate, features, "spread_pct"), 0.0) or 0.0
    source_confidence = _float(
        _candidate_or_feature(candidate, features, "source_confidence"), None
    )

    if not ticker:
        hard.append("invalid_ticker")
    if price is None or price <= 0:
        hard.append("missing_price")
    elif price < min_price:
        hard.append("sub_min_price")
    if volume is None or volume <= 0:
        hard.append("zero_volume")
    if spread > max_spread_pct:
        hard.append("extreme_spread")
    if source_confidence is None or source_confidence <= 0:
        hard.append("no_source_confidence")
    elif source_confidence < min_source_confidence:
        hard.append("low_source_confidence")

    if _truthy(candidate.get("current_halt")) or "current_halt" in risk_flags:
        hard.append("current_halt")
    if _truthy(candidate.get("recent_offering")) or "recent_offering" in risk_flags:
        hard.append("active_offering")
    if _truthy(candidate.get("stale_data_flag")) or _truthy(features.get("stale_data_flag")):
        hard.append("stale_source")
    if str(candidate.get("conflict_flags") or features.get("conflict_flags") or "").strip():
        hard.append("source_conflict")
    for reason in risk_flags + avoid_reasons:
        normalized = reason.lower()
        if normalized in HARD_AVOID_FLAGS:
            hard.append(normalized)

    if "unknown_float" in risk_flags:
        soft.append("unknown_float")
    if _float(_candidate_or_feature(candidate, features, "previous_close")) in {None, 0.0}:
        soft.append("missing_previous_close")
    if _float(_candidate_or_feature(candidate, features, "premarket_high")) in {None, 0.0}:
        soft.append("missing_high")
    if _float(_candidate_or_feature(candidate, features, "premarket_low")) in {None, 0.0}:
        soft.append("missing_low")
    if not str(candidate.get("catalyst_headline") or features.get("catalyst_headline") or ""):
        soft.append("no_catalyst")
    data_source_kind = str(
        _candidate_or_feature(candidate, features, "data_source_kind") or ""
    )
    if data_source_kind == "web_url":
        soft.append("public_url_unverified")
    source_count = _float(_candidate_or_feature(candidate, features, "source_count"), 0.0)
    if (source_count or 0.0) < 2:
        soft.append("low_source_count")
    gap_pct = _float(_candidate_or_feature(candidate, features, "gap_pct"), 0.0)
    if (gap_pct or 0.0) > 300:
        soft.append("mega_gap")
    if 0 < spread > 4.0:
        soft.append("wide_spread")

    if receipt_enabled:
        receipt_id = str(candidate.get("receipt_id") or "").strip()
        construction_status = str(
            candidate.get("strategy_receipt_construction_status") or ""
        ).upper()
        if not receipt_id or construction_status != "COMPLETE":
            receipt_disagreements.append("strategy_receipt_construction_failed")
            if not receipt_shadow_only:
                hard.append("strategy_receipt_unavailable")
        elif receipt_research_eligible is not True:
            receipt_disagreements.append("strategy_receipt_research_ineligible")
            if not receipt_shadow_only:
                hard.append("strategy_receipt_ineligible")
        elif receipt_tier not in {
            "QUALIFIED_PICK",
            "PICK_WITH_DISCLOSED_GAPS",
            "CONDITIONAL_PICK",
        }:
            receipt_disagreements.append("strategy_receipt_tier_not_alertable")
            if not receipt_shadow_only:
                hard.append("strategy_receipt_tier_not_alertable")
        legacy_can_alert = _bool_or_none(candidate.get("strategy_receipt_legacy_can_alert"))
        if (
            legacy_can_alert is not None
            and receipt_research_eligible is not None
            and legacy_can_alert != receipt_research_eligible
        ):
            receipt_disagreements.append("legacy_vs_receipt_alert_disposition")

    hard = _dedupe(hard)
    soft = _dedupe(soft)
    risk_score = max(0.0, 100.0 - (len(hard) * 35.0) - (len(soft) * 7.5))
    merged_flags = _dedupe(risk_flags + hard + soft)
    merged_avoids = _dedupe(avoid_reasons + hard)
    return RiskDecision(
        ticker=ticker,
        can_alert=not hard,
        risk_score=round(risk_score, 2),
        risk_flags=merged_flags,
        avoid_reasons=merged_avoids,
        hard_avoid_reasons=hard,
        soft_penalties=soft,
        strategy_receipt_tier=receipt_tier,
        strategy_receipt_research_pick_eligible=receipt_research_eligible,
        strategy_receipt_paper_entry_eligible=receipt_paper_eligible,
        strategy_receipt_disagreement=receipt_disagreements,
    )


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    return [
        part.strip()
        for part in str(value or "").replace(",", ";").split(";")
        if part.strip()
    ]


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        clean = value.strip()
        key = clean.lower()
        if clean and key not in seen:
            seen.add(key)
            out.append(key)
    return out


def _float(value: Any, default: float | None = None) -> float | None:
    if value in {None, ""}:
        return default
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return default
    return parsed if math.isfinite(parsed) else default


def _candidate_or_feature(
    candidate: dict[str, Any], features: dict[str, Any], name: str
) -> Any:
    """Use candidate data first while allowing null/blank legacy fallback."""

    for mapping in (candidate, features):
        if name in mapping and not _blank(mapping[name]):
            return mapping[name]
    return None


def _blank(value: Any) -> bool:
    return value is None or (isinstance(value, str) and not value.strip())


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}


def _bool_or_none(value: Any) -> bool | None:
    if isinstance(value, bool):
        return value
    if value in {None, ""}:
        return None
    if isinstance(value, (int, float)) and value in {0, 1}:
        return bool(value)
    normalized = str(value).strip().lower()
    if normalized in {"true", "yes", "y", "1"}:
        return True
    if normalized in {"false", "no", "n", "0"}:
        return False
    return None
