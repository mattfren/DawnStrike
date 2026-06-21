"""Source reliability scoring for AlphaOps."""

from __future__ import annotations

from typing import Any

from intraday_scanner.models import utc_now_iso


def build_source_reliability(
    source_summary: dict[str, Any],
    *,
    outcomes: list[dict[str, Any]] | None = None,
    previous: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    outcomes = list(outcomes or [])
    previous = dict(previous or {})
    attempts = list(source_summary.get("attempts") or [])
    if not attempts:
        attempts = [{
            "source": source_summary.get("source") or "web_auto_collect",
            "status": source_summary.get("status") or "unknown",
            "rows_normalized": source_summary.get("rows_normalized") or 0,
            "rows_extracted": source_summary.get("rows_extracted") or 0,
            "rows_rejected": source_summary.get("rows_rejected") or 0,
        }]
    updated: list[dict[str, Any]] = []
    for attempt in attempts:
        source = str(attempt.get("source") or attempt.get("source_type") or "unknown")
        prior = dict(previous.get(source) or {})
        rows_normalized = int(float(attempt.get("rows_normalized") or 0))
        rows_extracted = int(float(attempt.get("rows_extracted") or rows_normalized or 0))
        rows_rejected = int(float(attempt.get("rows_rejected") or 0))
        stale_count = int(float(attempt.get("stale_count") or 0))
        missing_count = int(float(attempt.get("missing_critical_count") or 0))
        source_outcomes = [
            row for row in outcomes if str(row.get("source") or "").lower() == source.lower()
        ]
        winners = sum(1 for row in source_outcomes if bool(row.get("winner_close")))
        runs = int(prior.get("runs") or 0) + 1
        total_returned = int(prior.get("rows_returned") or 0) + rows_extracted
        total_normalized = int(prior.get("rows_normalized") or 0) + rows_normalized
        total_rejected = int(prior.get("rows_rejected") or 0) + rows_rejected
        total_stale = int(prior.get("stale_count") or 0) + stale_count
        total_missing = int(prior.get("missing_critical_count") or 0) + missing_count
        outcome_count = int(prior.get("outcome_count") or 0) + len(source_outcomes)
        winner_count = int(prior.get("winner_count") or 0) + winners
        updated.append({
            "source": source,
            "updated_at": utc_now_iso(),
            "runs": runs,
            "rows_returned": total_returned,
            "rows_normalized": total_normalized,
            "rows_rejected": total_rejected,
            "stale_count": total_stale,
            "missing_critical_count": total_missing,
            "outcome_count": outcome_count,
            "winner_count": winner_count,
            "reliability_score": reliability_score(
                rows_returned=total_returned,
                rows_normalized=total_normalized,
                rows_rejected=total_rejected,
                stale_count=total_stale,
                missing_critical_count=total_missing,
                outcome_count=outcome_count,
                winner_count=winner_count,
            ),
            "latest_status": str(attempt.get("status") or "unknown"),
        })
    return updated


def reliability_score(
    *,
    rows_returned: int,
    rows_normalized: int,
    rows_rejected: int,
    stale_count: int,
    missing_critical_count: int,
    outcome_count: int,
    winner_count: int,
) -> float:
    extract_score = (
        50.0
        if rows_returned <= 0
        else min(100.0, (rows_normalized / rows_returned) * 100.0)
    )
    reject_penalty = min(25.0, rows_rejected * 2.0)
    stale_penalty = min(30.0, stale_count * 10.0)
    missing_penalty = min(30.0, missing_critical_count * 5.0)
    outcome_bonus = 0.0
    if outcome_count:
        outcome_bonus = ((winner_count / outcome_count) - 0.5) * 20.0
    score = extract_score - reject_penalty - stale_penalty - missing_penalty + outcome_bonus
    return round(max(0.0, min(100.0, score)), 2)
