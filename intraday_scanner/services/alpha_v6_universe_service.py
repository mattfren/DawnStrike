"""Immutable versioned-universe registration for AlphaOps V6."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any

from intraday_scanner.alpha.v6.contracts import canonical_hash, utc_now
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

_ALLOWED_LISTING_STATUSES = frozenset({"ACTIVE", "HALTED", "DELISTED", "INACTIVE"})
_REQUIRED_LINEAGE_FIELDS = frozenset(
    {
        "source_id",
        "provider_id",
        "dataset_id",
        "dataset_version",
        "terms_reference",
        "entitlement_reference",
        "accountable_contact",
        "approval_status",
        "critical_truth_complete",
        "registration_allowed",
        "source_contract_hash_sha256",
        "retrieved_at",
        "raw_artifact_sha256",
        "configuration_hash_sha256",
    }
)


def register_alpha_v6_universe(
    store: SQLiteScanStore,
    *,
    as_of_date: str,
    members: list[dict[str, Any]],
    source_lineage: dict[str, Any],
) -> dict[str, Any]:
    """Register a source-backed universe snapshot without silently rewriting it."""

    version, normalized = prepare_alpha_v6_universe(
        as_of_date=as_of_date,
        members=members,
        source_lineage=source_lineage,
        require_registration_approval=True,
    )
    persisted = store.persist_alpha_v6_universe(version=version, members=normalized)
    return {**version, "persisted": persisted, "members": normalized}


def preview_alpha_v6_universe(
    store: SQLiteScanStore,
    *,
    as_of_date: str,
    members: list[dict[str, Any]],
    source_lineage: dict[str, Any],
) -> dict[str, Any]:
    """Diff a candidate against the current immutable snapshot without writing it."""

    version, normalized = prepare_alpha_v6_universe(
        as_of_date=as_of_date,
        members=members,
        source_lineage=source_lineage,
        require_registration_approval=False,
    )
    prior_versions = store.load_alpha_v6_universe_versions(limit=1)
    prior = prior_versions[0] if prior_versions else None
    prior_members = (
        store.load_alpha_v6_universe_members(universe_id=str(prior["universe_id"])) if prior else []
    )
    before = {str(row["ticker"]): row for row in prior_members}
    after = {str(row["ticker"]): row for row in normalized}
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = [
        {
            "ticker": ticker,
            "before": _member_truth(before[ticker]),
            "after": _member_truth(after[ticker]),
        }
        for ticker in sorted(set(before) & set(after))
        if _member_truth(before[ticker]) != _member_truth(after[ticker])
    ]
    diff = {
        "added_tickers": added,
        "removed_tickers": removed,
        "changed_members": changed,
        "prior_membership_count": len(before),
        "candidate_membership_count": len(after),
    }
    preview_content = {
        "candidate_universe_id": version["universe_id"],
        "as_of_date": as_of_date,
        "prior_universe_id": prior.get("universe_id") if prior else None,
        "candidate_source_lineage_hash_sha256": version["source_lineage_hash_sha256"],
        "diff": diff,
    }
    return {
        "schema_version": "dawnstrike.alphaops_v6.universe_preview.v1",
        "status": (
            "NO_CHANGE" if not (added or removed or changed) else "REQUIRES_EXPLICIT_CONFIRMATION"
        ),
        **preview_content,
        "preview_hash_sha256": canonical_hash(preview_content),
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
    }


def restore_alpha_v6_universe(
    store: SQLiteScanStore,
    *,
    universe_id: str,
    as_of_date: str,
    operator: str,
    reason: str,
) -> dict[str, Any]:
    """Create a new immutable future snapshot from a selected historical version.

    No historical row is edited and no earlier decision is reinterpreted. This
    is an auditable forward restore, not a destructive rollback.
    """

    _parse_date(as_of_date, "as_of_date")
    operator = operator.strip()
    reason = reason.strip()
    if not operator or not reason:
        raise SnapshotValidationError("V6 universe restore requires operator and reason.")
    versions = {
        str(row.get("universe_id") or ""): row
        for row in store.load_alpha_v6_universe_versions(limit=10_000)
    }
    prior = versions.get(universe_id)
    if prior is None:
        raise SnapshotValidationError(f"V6 universe {universe_id} does not exist.")
    if as_of_date < str(prior.get("as_of_date") or "")[:10]:
        raise SnapshotValidationError(
            "V6 universe restore cannot predate the source universe snapshot."
        )
    original_lineage = prior.get("source_lineage")
    if not isinstance(original_lineage, dict):
        raise SnapshotValidationError("V6 universe restore is missing original source lineage.")
    raw_artifact_hash = str(original_lineage.get("raw_artifact_sha256") or "")
    source_lineage = {
        # Keep the original source identity/retrieval time. A restore reuses an
        # immutable source snapshot; it does not pretend to have fetched it now.
        "source_id": str(original_lineage.get("source_id") or ""),
        "provider_id": str(original_lineage.get("provider_id") or ""),
        "dataset_id": str(original_lineage.get("dataset_id") or ""),
        "dataset_version": str(original_lineage.get("dataset_version") or ""),
        "terms_reference": str(original_lineage.get("terms_reference") or ""),
        "entitlement_reference": str(original_lineage.get("entitlement_reference") or ""),
        "accountable_contact": str(original_lineage.get("accountable_contact") or ""),
        "approval_status": str(original_lineage.get("approval_status") or ""),
        "critical_truth_complete": original_lineage.get("critical_truth_complete"),
        "registration_allowed": True,
        "source_contract_hash_sha256": str(
            original_lineage.get("source_contract_hash_sha256") or ""
        ),
        "retrieved_at": str(original_lineage.get("retrieved_at") or ""),
        "raw_artifact_sha256": raw_artifact_hash,
        "configuration_hash_sha256": canonical_hash(
            {
                "restore_from_universe_id": universe_id,
                "as_of_date": as_of_date,
                "operator": operator,
                "reason": reason,
            }
        ),
        "restore_from_universe_id": universe_id,
        "restore_reason": reason,
        "restore_operator": operator,
        "upstream_source_lineage": original_lineage,
    }
    result = register_alpha_v6_universe(
        store,
        as_of_date=as_of_date,
        members=store.load_alpha_v6_universe_members(universe_id=universe_id),
        source_lineage=source_lineage,
    )
    return {**result, "restored_from_universe_id": universe_id}


def prepare_alpha_v6_universe(
    *,
    as_of_date: str,
    members: list[dict[str, Any]],
    source_lineage: dict[str, Any],
    require_registration_approval: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Validate and normalize an immutable source snapshot before any write."""

    _parse_date(as_of_date, "as_of_date")
    if not members:
        raise SnapshotValidationError("V6 universe registration requires at least one member.")
    _validate_source_lineage(
        source_lineage,
        require_registration_approval=require_registration_approval,
    )
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
    return version, normalized


def active_alpha_v6_membership_by_ticker(
    store: SQLiteScanStore, *, market_date: str, tickers: list[str]
) -> dict[str, dict[str, Any]]:
    """Resolve only source-backed membership valid at the decision date."""

    _parse_date(market_date, "market_date")
    rows = store.load_alpha_v6_universe_memberships(
        market_date=market_date,
        tickers=tickers,
    )
    return {ticker: {**row, "status": row.get("listing_status")} for ticker, row in rows.items()}


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
    eligibility = member.get("eligibility")
    if eligibility is not None and not isinstance(eligibility, dict):
        raise SnapshotValidationError(f"V6 universe member {ticker} eligibility must be an object.")
    return {
        "ticker": ticker,
        "listing_status": listing_status,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "previous_ticker": str(member.get("previous_ticker") or "").upper() or None,
        "corporate_action_type": str(member.get("corporate_action_type") or "") or None,
        "source_ref": str(member.get("source_ref") or "") or None,
        "eligibility": dict(eligibility) if isinstance(eligibility, dict) else None,
        "missing_truth_is_zero": False,
    }


def _parse_date(value: str, field: str) -> None:
    try:
        date.fromisoformat(value[:10])
    except ValueError as exc:
        raise SnapshotValidationError(f"V6 universe {field} must be ISO date.") from exc


def _validate_source_lineage(
    source_lineage: dict[str, Any], *, require_registration_approval: bool
) -> None:
    if not isinstance(source_lineage, dict):
        raise SnapshotValidationError("V6 universe registration requires source lineage.")
    missing = sorted(
        field
        for field in _REQUIRED_LINEAGE_FIELDS
        if source_lineage.get(field) is None
        or (
            isinstance(source_lineage.get(field), str)
            and not str(source_lineage.get(field)).strip()
        )
    )
    if missing:
        raise SnapshotValidationError(
            "V6 universe lineage is missing required fields: " + ", ".join(missing)
        )
    for field in (
        "raw_artifact_sha256",
        "configuration_hash_sha256",
        "source_contract_hash_sha256",
    ):
        value = str(source_lineage.get(field) or "")
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value.lower()):
            raise SnapshotValidationError(f"V6 universe {field} must be a SHA-256 hex digest.")
    try:
        datetime.fromisoformat(str(source_lineage["retrieved_at"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError("V6 universe retrieved_at must be ISO-8601.") from exc
    if "@" not in str(source_lineage["accountable_contact"]):
        raise SnapshotValidationError("V6 universe accountable_contact is invalid.")
    if str(source_lineage["approval_status"]).upper() not in {
        "APPROVED",
        "PENDING_EXTERNAL_APPROVAL",
    }:
        raise SnapshotValidationError("V6 universe approval_status is unsupported.")
    if not isinstance(source_lineage["critical_truth_complete"], bool):
        raise SnapshotValidationError("V6 universe critical_truth_complete must be boolean.")
    if not isinstance(source_lineage["registration_allowed"], bool):
        raise SnapshotValidationError("V6 universe registration_allowed must be boolean.")
    if require_registration_approval and (
        source_lineage["approval_status"] != "APPROVED"
        or source_lineage["critical_truth_complete"] is not True
        or source_lineage["registration_allowed"] is not True
    ):
        raise SnapshotValidationError(
            "V6 universe registration is blocked until source approval and complete "
            "critical truth are recorded."
        )


def _member_truth(member: dict[str, Any]) -> dict[str, Any]:
    return {
        field: member.get(field)
        for field in (
            "listing_status",
            "valid_from",
            "valid_to",
            "previous_ticker",
            "corporate_action_type",
            "source_ref",
        )
    }


__all__ = [
    "active_alpha_v6_membership_by_ticker",
    "prepare_alpha_v6_universe",
    "preview_alpha_v6_universe",
    "register_alpha_v6_universe",
    "restore_alpha_v6_universe",
]
