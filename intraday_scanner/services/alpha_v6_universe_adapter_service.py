"""Fail-closed materialization of point-in-time V6 universe source artifacts.

The adapter deliberately does not invent a public universe or fetch from an
unapproved provider. It validates a recorded, provider-shaped artifact into the
immutable V6 registration format and marks it ineligible for registration until
the operator supplies a licensed source contract and approval evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.errors import SnapshotValidationError

SOURCE_CONTRACT_SCHEMA = "dawnstrike.alphaops_v6.universe_source_contract.v1"
RAW_ARTIFACT_SCHEMA = "dawnstrike.alphaops_v6.universe_raw_artifact.v1"
CANDIDATE_SCHEMA = "dawnstrike.alphaops_v6.universe_candidate.v1"
_APPROVAL_STATUSES = frozenset({"APPROVED", "PENDING_EXTERNAL_APPROVAL"})
_TICKER = re.compile(r"^[A-Z][A-Z.-]{0,9}$")
_EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def build_alpha_v6_universe_candidate(
    *,
    source_contract_path: str | Path,
    raw_artifact_path: str | Path,
) -> dict[str, Any]:
    """Validate one recorded provider artifact without mutating durable state."""

    contract_path = Path(source_contract_path)
    artifact_path = Path(raw_artifact_path)
    contract = _read_object(contract_path, "V6 universe source contract")
    raw_bytes = artifact_path.read_bytes()
    raw_artifact = _decode_object(raw_bytes, "V6 universe raw artifact")
    contract_hash = _sha256(contract_path.read_bytes())
    raw_hash = _sha256(raw_bytes)
    _validate_contract(contract, raw_hash=raw_hash)
    _validate_raw_artifact(raw_artifact)
    as_of_date = _as_date(raw_artifact.get("as_of_date"), "raw artifact as_of_date")
    retrieved_at = _as_timestamp(raw_artifact.get("retrieved_at"), "raw artifact retrieved_at")
    records = raw_artifact["records"]
    assert isinstance(records, list)

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, row in enumerate(records):
        if not isinstance(row, dict):
            rejected.append(
                {"record_index": index, "ticker": None, "reasons": ["record_not_object"]}
            )
            continue
        normalized, reasons = _normalize_record(row, as_of_date=as_of_date)
        ticker = (
            str(normalized["ticker"])
            if normalized is not None
            else str(row.get("ticker") or "").upper()
        )
        if normalized and ticker in seen:
            reasons.append("duplicate_ticker")
        if reasons:
            rejected.append(
                {
                    "record_index": index,
                    "ticker": ticker or None,
                    "reasons": sorted(set(reasons)),
                    "source_ref": str(row.get("source_ref") or "") or None,
                }
            )
            continue
        assert normalized is not None
        seen.add(ticker)
        accepted.append(normalized)

    accepted.sort(key=lambda row: str(row["ticker"]))
    approval_status = str(contract["approval_status"]).upper()
    registration_allowed = approval_status == "APPROVED" and bool(accepted)
    source_lineage = {
        "source_id": f"{contract['provider_id']}:{contract['dataset_id']}",
        "provider_id": str(contract["provider_id"]),
        "dataset_id": str(contract["dataset_id"]),
        "dataset_version": str(contract["dataset_version"]),
        "terms_reference": str(contract["terms_reference"]),
        "entitlement_reference": str(contract["entitlement_reference"]),
        "accountable_contact": str(contract["accountable_contact"]),
        "approval_status": approval_status,
        "critical_truth_complete": bool(accepted),
        "retrieved_at": retrieved_at,
        "raw_artifact_sha256": raw_hash,
        "configuration_hash_sha256": contract_hash,
        "source_contract_hash_sha256": contract_hash,
        "raw_artifact_schema_version": RAW_ARTIFACT_SCHEMA,
        "registration_allowed": registration_allowed,
    }
    content = _candidate_content(
        as_of_date=as_of_date,
        source_lineage=source_lineage,
        members=accepted,
        rejected_members=rejected,
    )
    return {
        "schema_version": CANDIDATE_SCHEMA,
        "status": (
            "READY_FOR_PREVIEW"
            if registration_allowed
            else "BLOCKED_EXTERNAL_APPROVAL"
            if accepted
            else "BLOCKED_NO_ELIGIBLE_MEMBERS"
        ),
        "registration_allowed": registration_allowed,
        "required_external_inputs": _required_external_inputs(contract, accepted),
        **content,
        "candidate_hash_sha256": canonical_hash(content),
        "research_only": True,
        "broker_execution_enabled": False,
        "missing_truth_is_zero": False,
    }


def validate_alpha_v6_universe_candidate(candidate: dict[str, Any]) -> dict[str, Any]:
    """Reject altered or hand-authored universe candidates before review/use.

    A candidate is a deterministic review artifact.  Registration additionally
    re-materializes it from the named source contract and raw artifact, but
    preview must still reject accidental edits and legacy hand-written inputs.
    """

    if candidate.get("schema_version") != CANDIDATE_SCHEMA:
        raise SnapshotValidationError("V6 universe input must be an adapter candidate artifact.")
    as_of_date = _as_date(candidate.get("as_of_date"), "candidate as_of_date")
    source_lineage = candidate.get("source_lineage")
    members = candidate.get("members")
    rejected_members = candidate.get("rejected_members")
    if not isinstance(source_lineage, dict):
        raise SnapshotValidationError("V6 universe candidate source_lineage must be an object.")
    if not isinstance(members, list) or not all(isinstance(row, dict) for row in members):
        raise SnapshotValidationError("V6 universe candidate members must be an object list.")
    if not isinstance(rejected_members, list) or not all(
        isinstance(row, dict) for row in rejected_members
    ):
        raise SnapshotValidationError(
            "V6 universe candidate rejected_members must be an object list."
        )
    expected_hash = str(candidate.get("candidate_hash_sha256") or "").lower()
    if not _is_sha256(expected_hash):
        raise SnapshotValidationError("V6 universe candidate hash must be a SHA-256 hex digest.")
    content = _candidate_content(
        as_of_date=as_of_date,
        source_lineage=source_lineage,
        members=members,
        rejected_members=rejected_members,
    )
    if canonical_hash(content) != expected_hash:
        raise SnapshotValidationError(
            "V6 universe candidate hash does not match its reviewed content."
        )
    return {**candidate, **content}


def _candidate_content(
    *,
    as_of_date: str,
    source_lineage: dict[str, Any],
    members: list[dict[str, Any]],
    rejected_members: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return the exact immutable fields covered by a candidate digest."""

    return {
        "as_of_date": as_of_date,
        "source_lineage": source_lineage,
        "members": members,
        "rejected_members": rejected_members,
    }


def write_alpha_v6_universe_candidate(
    candidate: dict[str, Any], *, output_path: str | Path
) -> Path:
    """Write a deterministic candidate artifact for preview, never registration."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(candidate, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return path


def _validate_contract(contract: dict[str, Any], *, raw_hash: str) -> None:
    if contract.get("schema_version") != SOURCE_CONTRACT_SCHEMA:
        raise SnapshotValidationError("Unsupported V6 universe source contract schema.")
    required = (
        "provider_id",
        "dataset_id",
        "dataset_version",
        "terms_reference",
        "entitlement_reference",
        "accountable_contact",
        "approval_status",
        "expected_raw_artifact_sha256",
    )
    missing = [field for field in required if not str(contract.get(field) or "").strip()]
    if missing:
        raise SnapshotValidationError(
            "V6 universe source contract is missing: " + ", ".join(sorted(missing))
        )
    if not _EMAIL.fullmatch(str(contract["accountable_contact"]).strip()):
        raise SnapshotValidationError("V6 universe source contract accountable_contact is invalid.")
    status = str(contract["approval_status"]).upper().strip()
    if status not in _APPROVAL_STATUSES:
        raise SnapshotValidationError("V6 universe source contract approval_status is unsupported.")
    expected_hash = str(contract["expected_raw_artifact_sha256"]).lower()
    if not _is_sha256(expected_hash):
        raise SnapshotValidationError(
            "V6 universe source contract expected artifact hash is invalid."
        )
    if expected_hash != raw_hash:
        raise SnapshotValidationError(
            "V6 universe raw artifact hash does not match its source contract."
        )


def _validate_raw_artifact(raw_artifact: dict[str, Any]) -> None:
    if raw_artifact.get("schema_version") != RAW_ARTIFACT_SCHEMA:
        raise SnapshotValidationError("Unsupported V6 universe raw artifact schema.")
    _as_date(raw_artifact.get("as_of_date"), "raw artifact as_of_date")
    _as_timestamp(raw_artifact.get("retrieved_at"), "raw artifact retrieved_at")
    if not isinstance(raw_artifact.get("records"), list):
        raise SnapshotValidationError("V6 universe raw artifact records must be a list.")


def _normalize_record(
    row: dict[str, Any], *, as_of_date: str
) -> tuple[dict[str, Any] | None, list[str]]:
    ticker = str(row.get("ticker") or "").upper().strip()
    reasons: list[str] = []
    if not _TICKER.fullmatch(ticker):
        reasons.append("unresolved_or_malformed_ticker")
    if str(row.get("identity_status") or "").upper() != "RESOLVED":
        reasons.append("identity_not_resolved")
    if str(row.get("listing_status") or "").upper() not in {"ACTIVE", "HALTED"}:
        reasons.append("listing_not_active_or_halted")
    if str(row.get("instrument_type") or "").upper() != "COMMON_STOCK":
        reasons.append("not_common_stock")
    if row.get("is_otc") is not False:
        reasons.append("otc_or_unknown")
    if str(row.get("country") or "").upper() != "US":
        reasons.append("not_us_listing")
    if str(row.get("corporate_action_status") or "").upper() != "CLEAR":
        reasons.append("corporate_action_not_clear")
    for field in ("market_cap_usd", "avg_dollar_volume_20d"):
        value = row.get(field)
        try:
            if value is None or float(value) <= 0:
                reasons.append(f"missing_or_invalid_{field}")
        except (TypeError, ValueError):
            reasons.append(f"missing_or_invalid_{field}")
    source_ref = str(row.get("source_ref") or "").strip()
    if not source_ref:
        reasons.append("missing_source_ref")
    if reasons:
        return None, reasons
    return (
        {
            "ticker": ticker,
            "listing_status": str(row["listing_status"]).upper(),
            "valid_from": str(row.get("valid_from") or as_of_date)[:10],
            "valid_to": str(row.get("valid_to") or "")[:10] or None,
            "previous_ticker": str(row.get("previous_ticker") or "").upper() or None,
            "corporate_action_type": str(row.get("corporate_action_type") or "") or None,
            "source_ref": source_ref,
            "eligibility": {
                "identity_status": "RESOLVED",
                "instrument_type": "COMMON_STOCK",
                "is_otc": False,
                "country": "US",
                "corporate_action_status": "CLEAR",
                "market_cap_usd": float(row["market_cap_usd"]),
                "avg_dollar_volume_20d": float(row["avg_dollar_volume_20d"]),
            },
            "missing_truth_is_zero": False,
        },
        [],
    )


def _required_external_inputs(
    contract: dict[str, Any], accepted: list[dict[str, Any]]
) -> list[str]:
    if not accepted:
        return ["A source artifact with at least one complete, eligible member."]
    if str(contract["approval_status"]).upper() != "APPROVED":
        return [
            "A licensed provider approval record and entitlement reference "
            "for the source contract.",
            "An operator-approved source contract with approval_status=APPROVED.",
        ]
    return []


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise SnapshotValidationError(f"{label} does not exist: {path}")
    return _decode_object(path.read_bytes(), label)


def _decode_object(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SnapshotValidationError(f"{label} must be UTF-8 JSON object.") from exc
    if not isinstance(value, dict):
        raise SnapshotValidationError(f"{label} must be a JSON object.")
    return value


def _as_date(value: Any, field: str) -> str:
    try:
        return date.fromisoformat(str(value)[:10]).isoformat()
    except ValueError as exc:
        raise SnapshotValidationError(f"V6 universe {field} must be ISO date.") from exc


def _as_timestamp(value: Any, field: str) -> str:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise SnapshotValidationError(f"V6 universe {field} must be ISO-8601.") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise SnapshotValidationError(f"V6 universe {field} must include a timezone.")
    return parsed.isoformat()


def _is_sha256(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


__all__ = [
    "CANDIDATE_SCHEMA",
    "RAW_ARTIFACT_SCHEMA",
    "SOURCE_CONTRACT_SCHEMA",
    "build_alpha_v6_universe_candidate",
    "validate_alpha_v6_universe_candidate",
    "write_alpha_v6_universe_candidate",
]
