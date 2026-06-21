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

    def to_dict(self) -> dict[str, Any]:
        return {
            "no_trade": self.no_trade,
            "reason": self.reason,
            "next_action": self.next_action,
            "clean_count": self.clean_count,
            "blocked_count": self.blocked_count,
        }


def evaluate_no_trade(
    candidates: list[dict[str, Any]],
    *,
    source_summary: dict[str, Any] | None = None,
    min_source_confidence: float = 35.0,
    min_alpha_score: float = 45.0,
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
    ]
    blocked = len(candidates) - len(clean)
    if not clean:
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
        return NoTradeDecision(
            no_trade=True,
            reason="Every clean candidate has low source confidence.",
            next_action="Wait for source confirmation before alerting.",
            clean_count=len(clean),
            blocked_count=blocked,
        )
    return NoTradeDecision(
        no_trade=False,
        reason="Clean edge candidates are available.",
        next_action="Review top AlphaOps watchlist; no orders are placed by the app.",
        clean_count=len(clean),
        blocked_count=blocked,
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
