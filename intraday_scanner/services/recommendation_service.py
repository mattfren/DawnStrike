"""Recommendation payload construction."""

from __future__ import annotations

from typing import Any


def build_recommendations(scan: dict[str, Any], limit: int = 10) -> list[dict[str, Any]]:
    summary = dict(scan.get("summary") or {})
    scan_id = str(summary.get("run_id") or scan.get("run_id") or "")
    timestamp = str(summary.get("created_at") or "")
    recommendations = []
    for row in list(scan.get("ranked_candidates") or [])[:limit]:
        recommendations.append(
            {
                "scan_id": scan_id,
                "timestamp": timestamp,
                "rank": row.get("rank"),
                "ticker": row.get("ticker"),
                "score": row.get("score"),
                "component_scores": row.get("score_breakdown"),
                "thesis": _thesis(row),
                "catalyst_summary": row.get("catalyst_headline") or "No catalyst headline.",
                "risk_flags": row.get("risk_flags") or "",
                "breakout_trigger": row.get("breakout_trigger"),
                "pullback_zone_low": _pullback(row.get("pullback_zone"), 0),
                "pullback_zone_high": _pullback(row.get("pullback_zone"), 1),
                "invalidation_level": row.get("invalidation_level"),
                "first_target": row.get("first_target"),
                "stretch_target": row.get("stretch_target"),
                "exit_bias": row.get("best_exit_bias"),
                "confidence_level": row.get("setup_grade"),
                "data_quality_score": row.get("data_quality_score"),
            }
        )
    return recommendations


def _thesis(row: dict[str, Any]) -> str:
    return (
        f"{row.get('ticker')} has score {row.get('score')} with gap {row.get('gap_pct')}%, "
        f"watch price {row.get('breakout_trigger')}, invalidation "
        f"{row.get('invalidation_level')}, and first target {row.get('first_target')}."
    )


def _pullback(value: Any, index: int) -> str:
    parts = str(value or "").split("-", 1)
    if len(parts) != 2:
        return ""
    return parts[index].strip()
