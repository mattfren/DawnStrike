"""Central fail-closed point-in-time checks for Scenario price evidence."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def completed_minute_bar_at(value: str | datetime) -> datetime | None:
    """Return the exclusive completion timestamp for a one-minute bar."""

    observed = parse_aware_timestamp(value)
    return observed + timedelta(minutes=1) if observed is not None else None


def decision_price_evidence_violations(
    *,
    decision_at: str,
    observed_at: str,
    bar_completed_at: str,
    is_complete: Any,
    source_bar_hash_sha256: str,
    price: float | None,
    atr: float | None,
    spread_pct: float | None,
    liquid: bool | None,
) -> tuple[str, ...]:
    """Validate all evidence required to create a deterministic Scenario decision."""

    reasons = list(
        _lineage_violations(
            evidence_cutoff=decision_at,
            observed_at=observed_at,
            bar_completed_at=bar_completed_at,
            is_complete=is_complete,
            source_bar_hash_sha256=source_bar_hash_sha256,
        )
    )
    if not _positive(price):
        reasons.append("price_missing_or_invalid")
    if not _positive(atr):
        reasons.append("completed_bar_atr_missing")
    if spread_pct is None or not _nonnegative(spread_pct):
        reasons.append("spread_missing_or_invalid")
    elif float(spread_pct) > 2.0:
        reasons.append("spread_veto")
    if liquid is None:
        reasons.append("liquidity_evidence_missing")
    elif not liquid:
        reasons.append("liquidity_veto")
    return tuple(sorted(set(reasons)))


def subsequent_entry_evidence_violations(
    *,
    decision_at: str,
    requested_at: str,
    observed_at: str,
    bar_completed_at: str,
    is_complete: Any,
    source_bar_hash_sha256: str,
) -> tuple[str, ...]:
    """Require a completed, hashed bar strictly after the Scenario decision bar."""

    reasons = list(
        _lineage_violations(
            evidence_cutoff=requested_at,
            observed_at=observed_at,
            bar_completed_at=bar_completed_at,
            is_complete=is_complete,
            source_bar_hash_sha256=source_bar_hash_sha256,
        )
    )
    decision = parse_aware_timestamp(decision_at)
    completed = parse_aware_timestamp(bar_completed_at)
    if decision is None:
        reasons.append("scenario_decision_timestamp_missing_or_invalid")
    elif completed is not None and completed <= decision:
        reasons.append("entry_not_after_decision_bar")
    return tuple(sorted(set(reasons)))


def parse_aware_timestamp(value: str | datetime) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    else:
        text = str(value or "").strip()
        if not text:
            return None
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return None
    return parsed.astimezone(UTC)


def _lineage_violations(
    *,
    evidence_cutoff: str,
    observed_at: str,
    bar_completed_at: str,
    is_complete: Any,
    source_bar_hash_sha256: str,
) -> tuple[str, ...]:
    reasons: list[str] = []
    cutoff = parse_aware_timestamp(evidence_cutoff)
    observed = parse_aware_timestamp(observed_at)
    completed = parse_aware_timestamp(bar_completed_at)
    if cutoff is None:
        reasons.append("evidence_cutoff_timestamp_missing_or_invalid")
    if observed is None:
        reasons.append("price_timestamp_missing_or_invalid")
    if completed is None:
        reasons.append("price_bar_completion_missing_or_invalid")
    if not _true(is_complete):
        reasons.append("price_bar_incomplete")
    if not _SHA256.fullmatch(str(source_bar_hash_sha256 or "")):
        reasons.append("price_source_hash_missing_or_invalid")
    if observed is not None and completed is not None:
        if completed <= observed:
            reasons.append("price_bar_completion_not_after_observation")
        elif completed != observed + timedelta(minutes=1):
            reasons.append("price_bar_completion_mismatch")
    if cutoff is not None:
        if observed is not None and observed > cutoff:
            reasons.append("price_evidence_future")
        if completed is not None and completed > cutoff:
            reasons.append("price_evidence_future")
    return tuple(sorted(set(reasons)))


def _positive(value: float | None) -> bool:
    try:
        return value is not None and float(value) > 0
    except (TypeError, ValueError):
        return False


def _nonnegative(value: float | None) -> bool:
    try:
        return value is not None and float(value) >= 0
    except (TypeError, ValueError):
        return False


def _true(value: Any) -> bool:
    return value is True or value == 1
