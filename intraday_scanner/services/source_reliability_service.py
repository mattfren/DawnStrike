"""Source reliability scoring for AlphaOps."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from intraday_scanner.models import utc_now_iso

# These are deliberate universe-membership policy decisions, not malformed
# source rows.  They must remain visible in the audit counters but must not be
# charged as extraction/data-quality failures.
UNIVERSE_FILTER_REJECTION_REASONS = frozenset(
    {
        "inactive",
        "not_us_equity",
        "unsupported_exchange",
        "not_tradable",
        "non_common_security_name",
    }
)


def _authenticated_outcome(row: dict[str, Any]) -> bool:
    """Return whether a row is safe to use as a source outcome prior.

    Production labels carry explicit eligibility.  Rows without an explicit
    marker are deliberately excluded; callers and fixtures must opt in rather
    than allowing a winner-shaped row to become a prior accidentally.
    """

    if row.get("authenticated_outcome") is not None:
        return bool(row.get("authenticated_outcome"))
    if row.get("production_learning_eligible") is not None:
        return bool(row.get("production_learning_eligible"))
    if row.get("learning_eligible") is False:
        return False
    return False


def _outcome_identity(row: dict[str, Any]) -> str:
    """Return a stable outcome identity, or empty when it is not provable."""

    if not _authenticated_outcome(row):
        return ""
    for key in ("outcome_id", "label_key", "signal_id"):
        value = str(row.get(key) or "").strip()
        if value:
            return f"{key}:{value}"
    return ""


def _authenticated_outcome_snapshot(
    outcomes: list[dict[str, Any]],
) -> tuple[dict[str, tuple[str, bool]], set[str], dict[str, set[str]], int]:
    """Return canonical labels, conflicting identities, and unidentified count.

    The input is an authoritative complete snapshot, not an append-only event
    history.  Exact duplicate rows are harmless; an identity whose source or
    winner truth disagrees is quarantined from every source so dictionary order
    cannot select a winner.
    """

    by_identity: dict[str, set[tuple[str, bool]]] = {}
    unidentified = 0
    for row in outcomes:
        if not _authenticated_outcome(row):
            continue
        identity = _outcome_identity(row)
        if not identity:
            unidentified += 1
            continue
        source = str(row.get("source") or "").strip().lower()
        by_identity.setdefault(identity, set()).add(
            (source, bool(row.get("winner_close")))
        )
    conflicts = {
        identity for identity, truths in by_identity.items() if len(truths) > 1
    }
    conflict_sources = {
        identity: {source for source, _winner in by_identity[identity]}
        for identity in conflicts
    }
    canonical: dict[str, tuple[str, bool]] = {}
    for identity, truths in by_identity.items():
        if identity not in conflicts:
            canonical[identity] = next(iter(truths))
    return canonical, conflicts, conflict_sources, unidentified


def _identity_set_hash(identities: list[str]) -> str:
    payload = json.dumps(
        sorted(identities), ensure_ascii=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _authenticated_snapshot_hash(outcomes: list[dict[str, Any]]) -> str:
    """Hash authenticated identity/source/winner truth, including conflicts."""

    truths: list[tuple[str, str, bool]] = []
    for row in outcomes:
        if not _authenticated_outcome(row):
            continue
        identity = _outcome_identity(row)
        truths.append(
            (
                identity,
                str(row.get("source") or "").strip().lower(),
                bool(row.get("winner_close")),
            )
        )
    payload = json.dumps(sorted(truths), ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(payload).hexdigest()


_LEGACY_IDENTITY_FIELDS = frozenset(
    {"outcome_identities", "outcome_winner_identities"}
)


def _rejection_counts(attempt: dict[str, Any]) -> tuple[int, int]:
    """Return (intentional universe rejects, actual quality rejects)."""

    counts = dict(attempt.get("rejection_reason_counts") or {})
    universe = sum(
        int(float(count or 0))
        for reason, count in counts.items()
        if str(reason).strip().lower() in UNIVERSE_FILTER_REJECTION_REASONS
    )
    total = int(float(attempt.get("rows_rejected") or 0))
    return universe, max(0, total - universe)


def build_source_reliability(
    source_summary: dict[str, Any],
    *,
    outcomes: list[dict[str, Any]] | None = None,
    previous: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    outcomes_provided = outcomes is not None
    outcomes = list(outcomes or [])
    previous = dict(previous or {})
    snapshot, conflict_ids, conflict_sources, unidentified_count = _authenticated_outcome_snapshot(
        outcomes
    )
    snapshot_hash = _authenticated_snapshot_hash(outcomes)
    global_snapshot_complete = (
        outcomes_provided and not conflict_ids and unidentified_count == 0
    )
    attempts = list(source_summary.get("attempts") or [])
    if not attempts:
        attempts = [
            {
                "source": source_summary.get("source") or "web_auto_collect",
                "status": source_summary.get("status") or "unknown",
                "rows_normalized": source_summary.get("rows_normalized") or 0,
                "rows_extracted": source_summary.get("rows_extracted") or 0,
                "rows_rejected": source_summary.get("rows_rejected") or 0,
            }
        ]
    updated: list[dict[str, Any]] = []
    for attempt in attempts:
        source = str(attempt.get("source") or attempt.get("source_type") or "unknown")
        prior = dict(previous.get(source) or {})
        rows_normalized = int(float(attempt.get("rows_normalized") or 0))
        rows_extracted = int(float(attempt.get("rows_extracted") or rows_normalized or 0))
        rows_rejected = int(float(attempt.get("rows_rejected") or 0))
        universe_rejected, quality_rejected = _rejection_counts(attempt)
        stale_count = int(float(attempt.get("stale_count") or 0))
        missing_count = int(float(attempt.get("missing_critical_count") or 0))
        source_outcomes = {
            identity: winner
            for identity, (row_source, winner) in snapshot.items()
            if row_source == source.lower() and identity not in conflict_ids
        }
        source_unidentified_count = sum(
            1
            for row in outcomes
            if str(row.get("source") or "").strip().lower() == source.lower()
            and _authenticated_outcome(row)
            and not _outcome_identity(row)
        )
        source_conflicting_identity_count = sum(
            1
            for identity in conflict_ids
            if source.lower() in conflict_sources.get(identity, set())
        )
        runs = int(prior.get("runs") or 0) + 1
        total_returned = int(prior.get("rows_returned") or 0) + rows_extracted
        total_normalized = int(prior.get("rows_normalized") or 0) + rows_normalized
        total_rejected = int(prior.get("rows_rejected") or 0) + rows_rejected
        total_universe_rejected = (
            int(prior.get("universe_filter_rejected_count") or 0) + universe_rejected
        )
        prior_quality_rejected = prior.get("data_quality_rejected_count")
        if prior_quality_rejected is None:
            # Older rows predate the rejection-class split.  Their rejected
            # count is the conservative quality-failure interpretation.
            prior_quality_rejected = prior.get("rows_rejected") or 0
        total_quality_rejected = int(prior_quality_rejected) + quality_rejected
        total_stale = int(prior.get("stale_count") or 0) + stale_count
        total_missing = int(prior.get("missing_critical_count") or 0) + missing_count
        # Outcome aggregates are recomputed from the complete current snapshot.
        # Collection-health counters above intentionally remain cumulative.
        outcome_identities = sorted(source_outcomes)
        winner_identities = sorted(
            identity for identity, winner in source_outcomes.items() if winner
        )
        outcome_count = len(outcome_identities)
        winner_count = len(winner_identities)
        clean_prior = {
            key: value for key, value in prior.items() if key not in _LEGACY_IDENTITY_FIELDS
        }
        updated.append(
            {
                **clean_prior,
                "source": source,
                "updated_at": utc_now_iso(),
                "runs": runs,
                "rows_returned": total_returned,
                "rows_normalized": total_normalized,
                "rows_rejected": total_rejected,
                "universe_filter_rejected_count": total_universe_rejected,
                "data_quality_rejected_count": total_quality_rejected,
                "stale_count": total_stale,
                "missing_critical_count": total_missing,
                "outcome_count": outcome_count,
                "winner_count": winner_count,
                "outcome_identity_set_hash_sha256": _identity_set_hash(outcome_identities),
                "outcome_snapshot_hash_sha256": snapshot_hash,
                "outcome_snapshot_status": (
                    "quarantined_conflicting_identity"
                    if conflict_ids
                    else "degraded_unidentified_authenticated_outcome"
                    if unidentified_count
                    else "complete_authenticated_snapshot"
                    if outcomes_provided
                    else "snapshot_absent"
                ),
                "outcome_conflicting_identity_count": source_conflicting_identity_count,
                "unidentified_authenticated_outcome_count": source_unidentified_count,
                "authenticated_snapshot_conflicting_identity_count": len(conflict_ids),
                "authenticated_snapshot_unidentified_count": unidentified_count,
                "outcome_evidence_status": (
                    "authenticated"
                    if global_snapshot_complete and outcome_count
                    else "collection_only"
                ),
                "alpha_adjustment_eligible": global_snapshot_complete and outcome_count > 0,
                "reliability_score": reliability_score(
                    rows_returned=total_returned,
                    rows_normalized=total_normalized,
                    rows_rejected=total_quality_rejected,
                    universe_filter_rejected=total_universe_rejected,
                    stale_count=total_stale,
                    missing_critical_count=total_missing,
                    outcome_count=outcome_count,
                    winner_count=winner_count,
                    outcome_evidence_complete=global_snapshot_complete,
                ),
                "latest_status": str(attempt.get("status") or "unknown"),
            }
        )
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
    universe_filter_rejected: int = 0,
    outcome_evidence_complete: bool = True,
) -> float:
    effective_returned = max(0, rows_returned - max(0, universe_filter_rejected))
    extract_score = (
        50.0
        if effective_returned <= 0
        else min(100.0, (rows_normalized / effective_returned) * 100.0)
    )
    reject_penalty = min(25.0, max(0, rows_rejected) * 2.0)
    stale_penalty = min(30.0, stale_count * 10.0)
    missing_penalty = min(30.0, missing_critical_count * 5.0)
    outcome_bonus = 0.0
    if outcome_count and outcome_evidence_complete:
        outcome_bonus = ((winner_count / outcome_count) - 0.5) * 20.0
    score = extract_score - reject_penalty - stale_penalty - missing_penalty + outcome_bonus
    return round(max(0.0, min(100.0, score)), 2)
