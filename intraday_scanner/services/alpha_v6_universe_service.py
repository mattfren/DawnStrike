"""Immutable versioned-universe registration for AlphaOps V6."""

from __future__ import annotations

from datetime import date
from typing import Any

from intraday_scanner.alpha.v6.contracts import canonical_hash, utc_now
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

_ALLOWED_LISTING_STATUSES = frozenset({"ACTIVE", "HALTED", "DELISTED", "INACTIVE"})


def register_alpha_v6_universe(
    store: SQLiteScanStore,
    *,
    as_of_date: str,
    members: list[dict[str, Any]],
    source_lineage: dict[str, Any],
) -> dict[str, Any]:
    """Register a source-backed universe snapshot without silently rewriting it."""

    _parse_date(as_of_date, "as_of_date")
    if not members:
        raise SnapshotValidationError("V6 universe registration requires at least one member.")
    if not isinstance(source_lineage, dict) or not source_lineage:
        raise SnapshotValidationError("V6 universe registration requires source lineage.")
    normalized = [_normalize_member(member, as_of_date) for member in members]
    tickers = [str(member["ticker"]) for member in normalized]
    if len(tickers) != len(set(tickers)):
        raise SnapshotValidationError("V6 universe registration contains duplicate tickers.")
    source_hash = canonical_hash(
        {
            "as_of_date": as_of_date,
            "members": normalized,
            "source_lineage": source_lineage,
        }
    )
    universe_id = "v6u-" + source_hash[:28]
    version = {
        "universe_id": universe_id,
        "as_of_date": as_of_date,
        "created_at": utc_now(),
        "membership_count": len(normalized),
        "source_lineage": source_lineage,
        "source_lineage_hash_sha256": source_hash,
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
    }
    persisted = store.persist_alpha_v6_universe(version=version, members=normalized)
    return {**version, "persisted": persisted, "members": normalized}


def active_alpha_v6_membership_by_ticker(
    store: SQLiteScanStore, *, market_date: str, tickers: list[str]
) -> dict[str, dict[str, Any]]:
    """Resolve only source-backed membership valid at the decision date."""

    _parse_date(market_date, "market_date")
    rows = store.load_alpha_v6_universe_memberships(
        market_date=market_date,
        tickers=tickers,
    )
    return {
        ticker: {**row, "status": row.get("listing_status")}
        for ticker, row in rows.items()
    }


def _normalize_member(member: dict[str, Any], as_of_date: str) -> dict[str, Any]:
    ticker = str(member.get("ticker") or "").upper().strip()
    listing_status = str(member.get("listing_status") or "ACTIVE").upper().strip()
    if not ticker:
        raise SnapshotValidationError("V6 universe member is missing ticker.")
    if listing_status not in _ALLOWED_LISTING_STATUSES:
        allowed = ", ".join(sorted(_ALLOWED_LISTING_STATUSES))
        raise SnapshotValidationError(
            f"V6 universe member {ticker} has invalid listing_status; expected one of {allowed}."
        )
    valid_from = str(member.get("valid_from") or as_of_date)[:10]
    valid_to = str(member.get("valid_to") or "")[:10] or None
    _parse_date(valid_from, f"valid_from for {ticker}")
    if valid_to:
        _parse_date(valid_to, f"valid_to for {ticker}")
        if valid_to < valid_from:
            raise SnapshotValidationError(f"V6 universe member {ticker} ends before it starts.")
    return {
        "ticker": ticker,
        "listing_status": listing_status,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "previous_ticker": str(member.get("previous_ticker") or "").upper() or None,
        "corporate_action_type": str(member.get("corporate_action_type") or "") or None,
        "source_ref": str(member.get("source_ref") or "") or None,
        "missing_truth_is_zero": False,
    }


def _parse_date(value: str, field: str) -> None:
    try:
        date.fromisoformat(value[:10])
    except ValueError as exc:
        raise SnapshotValidationError(f"V6 universe {field} must be ISO date.") from exc


__all__ = [
    "active_alpha_v6_membership_by_ticker",
    "register_alpha_v6_universe",
]
