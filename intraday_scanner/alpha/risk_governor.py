"""Risk gating for AlphaOps watchlist alerts."""

from __future__ import annotations

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

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "can_alert": self.can_alert,
            "risk_score": self.risk_score,
            "risk_flags": self.risk_flags,
            "avoid_reasons": self.avoid_reasons,
            "hard_avoid_reasons": self.hard_avoid_reasons,
            "soft_penalties": self.soft_penalties,
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

    price = _float(candidate.get("premarket_price") or features.get("premarket_price"))
    volume = _float(candidate.get("premarket_volume") or features.get("premarket_volume"))
    spread = _float(candidate.get("spread_pct") or features.get("spread_pct"), 0.0) or 0.0
    source_confidence = (
        _float(candidate.get("source_confidence"), None)
        if candidate.get("source_confidence") not in {None, ""}
        else _float(features.get("source_confidence"), None)
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
    if _float(candidate.get("previous_close") or features.get("previous_close")) in {None, 0.0}:
        soft.append("missing_previous_close")
    if not str(candidate.get("catalyst_headline") or features.get("catalyst_headline") or ""):
        soft.append("no_catalyst")
    data_source_kind = str(
        candidate.get("data_source_kind") or features.get("data_source_kind") or ""
    )
    if data_source_kind == "web_url":
        soft.append("public_url_unverified")
    source_count = _float(candidate.get("source_count") or features.get("source_count"), 0.0)
    if (source_count or 0.0) < 2:
        soft.append("low_source_count")
    gap_pct = _float(candidate.get("gap_pct") or features.get("gap_pct"), 0.0)
    if (gap_pct or 0.0) > 300:
        soft.append("mega_gap")
    if 0 < spread > 4.0:
        soft.append("wide_spread")

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
        return float(value)
    except (TypeError, ValueError):
        return default


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}
