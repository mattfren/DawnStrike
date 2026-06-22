"""No-trade decisioning for AlphaOps."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NoTradeDecision:
    no_trade: bool
    reason: str
    next_action: str
    clean_count: int
    blocked_count: int
    decision_tier: str = "no_trade"
    fallback_count: int = 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "no_trade": self.no_trade,
            "reason": self.reason,
            "next_action": self.next_action,
            "clean_count": self.clean_count,
            "blocked_count": self.blocked_count,
            "decision_tier": self.decision_tier,
            "fallback_count": self.fallback_count,
        }


def evaluate_no_trade(
    candidates: list[dict[str, Any]],
    *,
    source_summary: dict[str, Any] | None = None,
    min_source_confidence: float = 35.0,
    min_alpha_score: float = 45.0,
    min_fallback_alpha_score: float = 32.0,
    min_fallback_source_confidence: float = 20.0,
    min_fallback_risk_score: float = 55.0,
) -> NoTradeDecision:
    if not candidates:
        return NoTradeDecision(
            no_trade=True,
            reason="No usable candidates passed the scanner filters.",
            next_action="Wait for cleaner data or use the manual CSV fallback.",
            clean_count=0,
            blocked_count=0,
        )
    source_summary = dict(source_summary or {})
    status = str(source_summary.get("status") or source_summary.get("source_status") or "")
    if status in {"failed", "empty", "no_data"}:
        return NoTradeDecision(
            no_trade=True,
            reason=f"Source status is {status}; data is not strong enough to alert.",
            next_action="Wait for the next collection cycle or drop a manual CSV.",
            clean_count=0,
            blocked_count=len(candidates),
        )

    clean = [
        row
        for row in candidates
        if _bool(row.get("can_alert"))
        and not str(row.get("no_trade_reason") or "").strip()
        and _float(row.get("alpha_score"), 0.0) >= min_alpha_score
        and str(row.get("drawdown_risk_bucket") or "").upper() != "HIGH"
    ]
    blocked = len(candidates) - len(clean)
    fallback = _fallback_candidates(
        candidates,
        min_alpha_score=min_fallback_alpha_score,
        min_source_confidence=min_fallback_source_confidence,
        min_risk_score=min_fallback_risk_score,
    )
    if not clean:
        if fallback:
            return NoTradeDecision(
                no_trade=False,
                reason=(
                    "Probability fallback candidates are available, but the setup is "
                    "not clean enough to call a clean edge."
                ),
                next_action=(
                    "Review as watch-only probability setups. Confirm catalyst, ticker, "
                    "volume, and levels manually before doing anything."
                ),
                clean_count=0,
                blocked_count=blocked,
                decision_tier="probability_fallback",
                fallback_count=len(fallback),
            )
        reasons = _top_reasons(candidates)
        return NoTradeDecision(
            no_trade=True,
            reason=reasons or "All candidates were blocked by risk, weak edge, or stale data.",
            next_action="Do not force a pick. Re-scan in 5 minutes or wait for fresh data.",
            clean_count=0,
            blocked_count=blocked,
        )
    low_source = [
        row
        for row in clean
        if _float(row.get("source_confidence"), 100.0) < min_source_confidence
    ]
    if len(low_source) == len(clean):
        if fallback:
            return NoTradeDecision(
                no_trade=False,
                reason=(
                    "Only probability fallback candidates are available because source "
                    "confidence is low."
                ),
                next_action=(
                    "Treat this as watch-only. Use source confirmation and a fresh "
                    "5-minute check before trusting the setup."
                ),
                clean_count=0,
                blocked_count=blocked,
                decision_tier="probability_fallback",
                fallback_count=len(fallback),
            )
        return NoTradeDecision(
            no_trade=True,
            reason="Every clean candidate has low source confidence.",
            next_action="Wait for source confirmation before alerting.",
            clean_count=len(clean),
            blocked_count=blocked,
        )
    top = clean[0]
    if _float(top.get("risk_score"), 100.0) < 45.0:
        return NoTradeDecision(
            no_trade=True,
            reason="Top candidate risk score is too weak for an alert.",
            next_action="Wait for a cleaner setup instead of forcing the watchlist.",
            clean_count=len(clean),
            blocked_count=blocked,
        )
    return NoTradeDecision(
        no_trade=False,
        reason="Clean edge candidates are available.",
        next_action="Review top AlphaOps watchlist; no orders are placed by the app.",
        clean_count=len(clean),
        blocked_count=blocked,
        decision_tier="clean_edge",
    )


def _fallback_candidates(
    candidates: list[dict[str, Any]],
    *,
    min_alpha_score: float,
    min_source_confidence: float,
    min_risk_score: float,
) -> list[dict[str, Any]]:
    fallback = [
        row
        for row in candidates
        if _bool(row.get("can_alert"))
        and not str(row.get("no_trade_reason") or "").strip()
        and _float(row.get("alpha_score"), 0.0) >= min_alpha_score
        and _float(row.get("source_confidence"), 0.0) >= min_source_confidence
        and _float(row.get("risk_score"), 0.0) >= min_risk_score
        and str(row.get("drawdown_risk_bucket") or "").upper() != "HIGH"
        and (
            _float(row.get("score") or row.get("total_score"), 0.0) >= 40.0
            or _float(row.get("dollar_volume"), 0.0) >= 1_000_000.0
        )
    ]
    return sorted(
        fallback,
        key=lambda row: (
            _float(row.get("alpha_score"), 0.0),
            _float(row.get("dollar_volume"), 0.0),
        ),
        reverse=True,
    )


def _top_reasons(candidates: list[dict[str, Any]]) -> str:
    counts: dict[str, int] = {}
    for row in candidates:
        for key in ("no_trade_reason", "hard_avoid_reasons", "avoid_reasons", "risk_flags"):
            for token in _tokens(row.get(key)):
                counts[token] = counts.get(token, 0) + 1
    if not counts:
        return ""
    reason = sorted(counts.items(), key=lambda item: (-item[1], item[0]))[0][0]
    return reason.replace("_", " ")


def _tokens(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item)]
    return [
        part.strip()
        for part in str(value or "").replace(",", ";").split(";")
        if part.strip()
    ]


def _float(value: Any, default: float) -> float:
    if value in {None, ""}:
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    return str(value or "").strip().lower() in {"1", "true", "yes", "y"}
