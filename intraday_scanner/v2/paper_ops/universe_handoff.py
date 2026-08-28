"""Governed Morning-to-PaperOps point-in-time universe handoff.

The Morning cycle is the authority for the symbols that a scheduled PaperOps
run may observe.  This module turns the already persisted, governed Morning
artifacts into one immutable, content-addressed contract.  It intentionally
does not discover symbols, infer membership, or turn unavailable provider
truth into rows.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
from collections.abc import Iterable
from datetime import date, datetime
from pathlib import Path
from typing import Any

from intraday_scanner.services.luna_core_universe_service import (
    _TRUSTED_SOURCE_ROOTS,
    CORE_INDEXES,
    DEFAULT_MAX_AGE_DAYS,
    _canonical_member_hash,
    _canonical_symbol_set_hash,
    _trusted_source_uri,
    canonical_symbol,
)
from intraday_scanner.services.luna_core_universe_service import (
    _hash as _core_hash,
)
from intraday_scanner.v2.strategies import build_strategy_catalog

SCHEMA_VERSION = "dawnstrike.paperops.universe_handoff.v1"
_SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9.-]{0,14}$")
_SHA_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ALLOWED_SOURCE_STATUSES = {"success", "no_data", "empty", "partial", "failed"}


class UniverseHandoffError(ValueError):
    """Raised when a Morning universe handoff cannot be trusted."""


def build_universe_handoff(
    morning_root: str | Path,
    market_date: str | date,
    *,
    output_path: str | Path | None = None,
    allow_test_override: bool = False,
) -> dict[str, Any]:
    """Build and persist the exact deduplicated Morning PIT union.

    A PARTIAL contract is valid when one provider lane is unavailable; its
    shortfall is explicit and the scheduled consumer can still run the
    symbols actually backed by the surviving lane.  An empty union is never
    valid.
    """

    root = Path(morning_root).resolve()
    requested_date = _date_text(market_date)
    if requested_date is None:
        raise UniverseHandoffError("invalid market date")
    cycle = _read_required_json(root / "alpha_cycle.json", "alpha cycle")
    cycle_contract = _read_required_json(root / "alpha_run_contract.json", "alpha run contract")
    core = _read_required_json(root / "core_universe_contract.json", "core universe contract")
    source_summary = _read_required_json(
        root / "web_collect" / "source_summary.json", "Morning source summary"
    )

    _validate_cycle_identity(cycle, cycle_contract, requested_date)
    core_ready_indexes = _validate_core_contract(
        core, requested_date, allow_test_override=allow_test_override
    )
    _validate_source_summary(source_summary, requested_date, root=root)

    source_snapshot = _resolve_source_path(
        root,
        str(source_summary.get("snapshot_path") or ""),
    )
    mover_rows = _read_mover_snapshot(source_snapshot, requested_date)
    source_status = str(source_summary.get("status") or "").strip().lower()
    mover_lane_status = str(source_summary.get("mover_lane_status") or "").strip().upper()
    mover_available = source_status in {"success", "partial"} and bool(mover_rows)
    if mover_lane_status == "SOURCE_FAILED":
        mover_available = False

    core_ready = (
        str(core.get("status") or "").strip().upper() == "READY"
        and core_ready_indexes == set(CORE_INDEXES)
    )
    core_members = _core_members(core, allowed_indexes=core_ready_indexes)
    mover_members = (
        _mover_members(mover_rows, source_summary, requested_date) if mover_available else []
    )
    members = _merge_members(core_members, mover_members)
    symbols = [str(row["symbol"]) for row in members]
    if not symbols:
        raise UniverseHandoffError("universe union is empty; no current provider truth")

    shortfalls: list[str] = []
    if not core_ready:
        shortfalls.append("core_membership_unavailable")
    shortfalls.extend(
        f"core_index_unavailable:{index}"
        for index in CORE_INDEXES
        if index not in core_ready_indexes
    )
    if not mover_available:
        shortfalls.append("governed_mover_source_unavailable")
    if source_status in {"success", "partial"} and not mover_rows:
        shortfalls.append("governed_mover_snapshot_empty")
    declared_mover_count = int(source_summary.get("candidate_count") or 0)
    if source_status in {"success", "partial"} and declared_mover_count != len(mover_rows):
        shortfalls.append("governed_mover_snapshot_count_mismatch")
    if int(source_summary.get("source_failures") or 0) > 0:
        shortfalls.append("provider_failures_present")
    strategy_ids = _expected_strategy_ids()
    # The standalone web source summary is persisted before the Alpha cycle
    # attaches its authenticated prior-session adapter.  The cycle artifact is
    # the authoritative immutable container for that declaration; accepting a
    # declaration supplied only by the standalone summary would permit an
    # unbound fleet claim.
    cycle_source_summary = cycle.get("source_summary")
    adapter = (
        cycle_source_summary.get("morning_strategy_adapter")
        if isinstance(cycle_source_summary, dict)
        else None
    )
    declared_strategy_ids = (
        sorted(
            {
                str(value)
                for value in adapter.get("enabled_strategy_ids") or []
                if str(value).strip()
            }
        )
        if isinstance(adapter, dict)
        else []
    )
    coverage_status = "COMPLETE" if not shortfalls else "PARTIAL"
    generated_at = _first_text(
        cycle_contract.get("generated_at"),
        cycle.get("generated_at"),
        source_summary.get("created_at"),
    )
    if _iso_date(generated_at) != requested_date:
        raise UniverseHandoffError("Morning artifacts are stale or cross-date")

    source_artifacts = [
        _artifact(root / "alpha_cycle.json", root, "alpha_cycle"),
        _artifact(root / "alpha_run_contract.json", root, "alpha_run_contract"),
        _artifact(root / "core_universe_contract.json", root, "core_universe_contract"),
        _artifact(root / "web_collect" / "source_summary.json", root, "mover_source_summary"),
        _artifact(source_snapshot, root, "mover_snapshot"),
    ]
    _validate_source_artifact_manifest(root, source_artifacts)
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "market_date": requested_date,
        "generated_at": generated_at,
        "run_id": str(cycle.get("scan_id") or cycle_contract.get("producer_run_id") or ""),
        "morning_scan_id": str(cycle.get("scan_id") or ""),
        "universe_symbols": symbols,
        "symbols": symbols,
        "members": members,
        "core_universe": {
            "status": str(core.get("status") or "DATA_UNAVAILABLE"),
            "contract_id": str(core.get("contract_id") or ""),
            "content_hash_sha256": str(core.get("content_hash_sha256") or ""),
            "canonical_member_set_hash_sha256": str(
                core.get("canonical_member_set_hash_sha256") or ""
            ),
            "requested_market_date": core.get("requested_market_date"),
            "membership_count": int(core.get("membership_count") or 0),
            "included_count": len(core_members),
        },
        "mover_source": {
            "status": source_status,
            "lane_status": mover_lane_status or ("AVAILABLE" if mover_available else "UNAVAILABLE"),
            "source_identity": str(
                source_summary.get("source_identity") or source_summary.get("run_id") or ""
            ),
            "snapshot_path": source_snapshot.relative_to(root).as_posix(),
            "declared_count": int(source_summary.get("candidate_count") or 0),
            "available_count": len(mover_members),
            "source_failures": int(source_summary.get("source_failures") or 0),
        },
        "coverage": {
            "status": coverage_status,
            "core_membership_count": int(core.get("membership_count") or 0),
            "core_included_count": len(core_members),
            "mover_declared_count": int(source_summary.get("candidate_count") or 0),
            "mover_included_count": len(mover_members),
            "union_count": len(symbols),
            "overlap_count": sum(
                1 for row in members if set(row.get("lanes") or []) == {"core", "mover"}
            ),
            "shortfall_reasons": sorted(set(shortfalls)),
        },
        "strategy_fleet": {
            "expected_strategy_ids": list(strategy_ids),
            "declared_paperops_strategy_ids": list(strategy_ids),
            "declared_morning_strategy_ids": declared_strategy_ids,
            "missing_declared_strategy_ids": sorted(set(strategy_ids) - set(declared_strategy_ids)),
            "expected_count": len(strategy_ids),
        },
        "safety": {
            "research_only": True,
            "broker_execution": "disabled",
            "broker_execution_enabled": False,
            "missing_truth_is_zero": False,
        },
        "source_artifacts": source_artifacts,
    }
    digest = _handoff_hash(body)
    body["content_hash_sha256"] = digest
    body["content_hash"] = digest
    body["handoff_id"] = "paperops-universe-" + digest[:24]
    body["universe_id"] = "paperops-pit-universe-" + digest[:24]
    if output_path is not None:
        _write_immutable(Path(output_path), body)
    return body


def load_universe_handoff(
    path: str | Path,
    *,
    market_date: str | date | None = None,
    require_production: bool = False,
    verify_sources: bool = True,
) -> dict[str, Any]:
    """Load and verify a content-addressed universe handoff."""

    handoff_path = Path(path).resolve()
    payload = _read_required_json(handoff_path, "PaperOps universe handoff")
    if payload.get("schema_version") != SCHEMA_VERSION:
        raise UniverseHandoffError("universe handoff schema is invalid")
    expected_date = _date_text(market_date) if market_date is not None else None
    actual_date = _date_text(payload.get("market_date"))
    if actual_date is None or (expected_date is not None and actual_date != expected_date):
        raise UniverseHandoffError("universe handoff market date conflicts")
    if require_production and not verify_sources:
        raise UniverseHandoffError("production universe handoff requires source verification")
    claimed = str(payload.get("content_hash_sha256") or "").lower()
    if not _SHA_PATTERN.fullmatch(claimed) or claimed != _handoff_hash(payload):
        raise UniverseHandoffError("universe handoff content hash is invalid")
    if payload.get("content_hash") != claimed:
        raise UniverseHandoffError("universe handoff content hash alias conflicts")
    if payload.get("handoff_id") != "paperops-universe-" + claimed[:24]:
        raise UniverseHandoffError("universe handoff identity conflicts")
    if payload.get("universe_id") != "paperops-pit-universe-" + claimed[:24]:
        raise UniverseHandoffError("universe handoff universe identity conflicts")
    symbols = _validate_symbols(payload.get("universe_symbols"), "universe_symbols")
    if symbols != _validate_symbols(payload.get("symbols"), "symbols"):
        raise UniverseHandoffError("universe handoff symbol aliases conflict")
    members = payload.get("members")
    if (
        not isinstance(members, list)
        or [str(row.get("symbol")) for row in members if isinstance(row, dict)] != symbols
    ):
        raise UniverseHandoffError("universe handoff member union is invalid")
    _validate_core_binding(payload, actual_date)
    coverage = payload.get("coverage")
    if not isinstance(coverage, dict) or str(coverage.get("status") or "") not in {
        "COMPLETE",
        "PARTIAL",
    }:
        raise UniverseHandoffError("universe handoff coverage is invalid")
    safety = payload.get("safety")
    if require_production and (
        not isinstance(safety, dict)
        or safety.get("research_only") is not True
        or safety.get("broker_execution") != "disabled"
        or safety.get("broker_execution_enabled") is not False
        or safety.get("missing_truth_is_zero") is not False
    ):
        raise UniverseHandoffError("universe handoff production safety binding is invalid")
    if require_production and not symbols:
        raise UniverseHandoffError("universe handoff has no symbols")
    fleet = payload.get("strategy_fleet")
    expected_strategy_ids = list(_expected_strategy_ids())
    if (
        not isinstance(fleet, dict)
        or fleet.get("expected_strategy_ids") != expected_strategy_ids
        or fleet.get("declared_paperops_strategy_ids") != expected_strategy_ids
        or fleet.get("expected_count") != len(expected_strategy_ids)
    ):
        raise UniverseHandoffError("universe handoff strategy fleet is invalid")
    if verify_sources:
        artifacts = payload.get("source_artifacts")
        if not isinstance(artifacts, list) or not artifacts:
            raise UniverseHandoffError("universe handoff source artifacts are missing")
        _validate_source_artifact_manifest(handoff_path.parent, artifacts)
        for artifact in artifacts:
            if not isinstance(artifact, dict):
                raise UniverseHandoffError("universe handoff source artifact is malformed")
            artifact_path = _resolve_source_path(
                handoff_path.parent, str(artifact.get("path") or "")
            )
            digest = str(artifact.get("sha256") or "").lower()
            if not _SHA_PATTERN.fullmatch(digest) or _sha256_file(artifact_path) != digest:
                raise UniverseHandoffError("universe handoff source artifact hash mismatch")

        # The handoff's content hash authenticates the bytes of the handoff,
        # but it does not by itself authenticate the meaning of derived fields
        # such as the union, member lanes, and coverage.  Rebuild the contract
        # from the exact hashed Morning inputs and compare the complete
        # unhashed semantic body.  This closes the self-consistent-forgery
        # case where an attacker replaces those fields and recomputes every
        # handoff digest and identity alias.
        expected = build_universe_handoff(
            handoff_path.parent,
            actual_date,
            allow_test_override=not require_production,
        )
        expected_artifacts = expected.get("source_artifacts")
        if artifacts != expected_artifacts:
            raise UniverseHandoffError("universe handoff source artifact manifest is invalid")
        identity_fields = {
            "content_hash_sha256",
            "content_hash",
            "handoff_id",
            "universe_id",
        }
        expected_body = {
            key: value for key, value in expected.items() if key not in identity_fields
        }
        actual_body = {key: value for key, value in payload.items() if key not in identity_fields}
        if actual_body != expected_body:
            raise UniverseHandoffError("universe handoff semantic binding is invalid")
    return payload


def validate_universe_handoff(
    path: str | Path, market_date: str | date | None = None
) -> dict[str, Any]:
    return load_universe_handoff(path, market_date=market_date, require_production=True)


def _validate_cycle_identity(
    cycle: dict[str, Any], contract: dict[str, Any], market_date: str
) -> None:
    if str(contract.get("schema_version") or "") != "alphaops.run_contract.v1":
        raise UniverseHandoffError("Morning run contract schema is invalid")
    if str(contract.get("producer") or "") != "alphaops":
        raise UniverseHandoffError("Morning run contract producer is invalid")
    if str(contract.get("market_date") or "") != market_date:
        raise UniverseHandoffError("Morning run contract market date conflicts")
    scan_id = str(cycle.get("scan_id") or "")
    if not scan_id or scan_id != str(contract.get("producer_run_id") or ""):
        raise UniverseHandoffError("Morning scan identity is missing or inconsistent")
    cycle_date = _iso_date(cycle.get("generated_at"))
    if cycle_date is not None and cycle_date != market_date:
        raise UniverseHandoffError("Morning cycle artifact is stale or cross-date")
    if str(contract.get("source_status") or "") not in {
        "success",
        "ok",
        "no_data",
        "partial",
        "failed",
    }:
        raise UniverseHandoffError("Morning source status is not governed")


def _validate_core_contract(
    core: dict[str, Any], market_date: str, *, allow_test_override: bool = False
) -> set[str]:
    if str(core.get("schema_version") or "") != "dawnstrike.luna.core_universe.v1":
        raise UniverseHandoffError("core universe contract schema is invalid")
    if str(core.get("requested_market_date") or "") != market_date:
        raise UniverseHandoffError("core universe contract market date conflicts")
    observed_date = _iso_date(core.get("observed_at"))
    if observed_date is None:
        raise UniverseHandoffError("core universe contract observation is invalid")
    if str(core.get("status") or "").upper() not in {"READY", "DATA_UNAVAILABLE"}:
        raise UniverseHandoffError("core universe contract status is invalid")
    if str(core.get("completeness_verdict") or "").upper() not in {
        "COMPLETE",
        "INCOMPLETE",
    }:
        raise UniverseHandoffError("core universe contract completeness is invalid")
    if str(core.get("freshness_verdict") or "").upper() not in {
        "FRESH",
        "STALE",
        "UNKNOWN",
    }:
        raise UniverseHandoffError("core universe contract freshness is invalid")
    if str(core.get("status") or "").upper() == "READY" and str(
        core.get("freshness_verdict") or ""
    ).upper() == "FRESH":
        observed_day = date.fromisoformat(observed_date)
        requested_day = date.fromisoformat(market_date)
        if observed_day > requested_day or (
            requested_day - observed_day
        ).days > DEFAULT_MAX_AGE_DAYS:
            raise UniverseHandoffError("core universe observation is not fresh for market date")
    claimed = str(core.get("content_hash_sha256") or "").lower()
    if not _SHA_PATTERN.fullmatch(claimed):
        raise UniverseHandoffError("core universe contract hash is missing")
    unhashed = dict(core)
    for field in ("content_hash_sha256", "content_hash", "contract_id", "universe_id"):
        unhashed.pop(field, None)
    if _core_hash(unhashed) != claimed:
        raise UniverseHandoffError("core universe contract hash mismatch")
    if core.get("content_hash") not in {None, "", claimed}:
        raise UniverseHandoffError("core universe content hash alias conflicts")
    members = core.get("members")
    if not isinstance(members, list):
        raise UniverseHandoffError("core universe members are missing")
    core_symbols = [row.get("symbol") for row in members if isinstance(row, dict)]
    if core_symbols:
        _validate_symbols(core_symbols, "core members")
    elif str(core.get("status") or "").upper() == "READY":
        raise UniverseHandoffError("READY core universe has no members")
    declared_member_hash = str(core.get("canonical_member_set_hash_sha256") or "").lower()
    if not _SHA_PATTERN.fullmatch(declared_member_hash):
        raise UniverseHandoffError("core universe canonical member hash is missing")
    canonical_records = [
        {
            "symbol": canonical_symbol(row.get("symbol") or row.get("ticker")),
            "provider_symbol": canonical_symbol(
                row.get("provider_symbol") or row.get("mapped_symbol") or row.get("symbol")
            ),
            "asset_class": str(row.get("asset_class") or row.get("security_type") or "common_stock")
            .strip()
            .lower(),
            "index": str(index),
            "valid_from": row.get("valid_from"),
            "valid_to": row.get("valid_to"),
        }
        for row in members
        if isinstance(row, dict)
        for index in (row.get("index_memberships") or [])
    ]
    if str(core.get("status") or "").upper() == "READY":
        for row in members:
            if not isinstance(row, dict):
                continue
            valid_from = _date_text(row.get("valid_from"))
            valid_to = _date_text(row.get("valid_to"))
            if valid_from is None or valid_from > market_date or (
                valid_to is not None and market_date > valid_to
            ):
                raise UniverseHandoffError("core universe member is not valid for market date")
    if _canonical_member_hash(canonical_records) != declared_member_hash:
        raise UniverseHandoffError("core universe canonical member hash mismatch")
    source_ids = core.get("source_ids")
    source_uris = core.get("source_uris")
    if not isinstance(source_ids, list) or (
        str(core.get("status") or "").upper() == "READY"
        and not any(str(value).strip() for value in source_ids)
    ):
        raise UniverseHandoffError("core universe source identities are missing")
    if not isinstance(source_uris, list) or (
        str(core.get("status") or "").upper() == "READY"
        and not any(str(value).strip() for value in source_uris)
    ):
        raise UniverseHandoffError("core universe source URIs are missing")
    artifacts = core.get("source_artifacts")
    if not isinstance(artifacts, list):
        raise UniverseHandoffError("core universe source roots are missing")
    index_verdicts = core.get("index_verdicts")
    if not isinstance(index_verdicts, dict) or set(index_verdicts) != set(CORE_INDEXES):
        raise UniverseHandoffError("core universe index verdicts are incomplete")
    ready_indexes: set[str] = set()
    artifacts_by_index: dict[str, list[dict[str, Any]]] = {index: [] for index in CORE_INDEXES}
    for artifact in artifacts:
        if not isinstance(artifact, dict):
            raise UniverseHandoffError("core universe source root is malformed")
        binding = artifact.get("source_binding")
        if not isinstance(binding, dict):
            raise UniverseHandoffError("core universe source lineage is missing")
        index = str(binding.get("index") or "").strip()
        if index not in artifacts_by_index:
            raise UniverseHandoffError("core universe source root index is invalid")
        artifacts_by_index[index].append(artifact)
        if not str(artifact.get("source_id") or "").strip() or not str(
            artifact.get("source_uri") or ""
        ).strip():
            raise UniverseHandoffError("core universe source identity binding is invalid")
        raw_hashes = artifact.get("raw_artifact_hashes") or []
        if not isinstance(raw_hashes, list) or not raw_hashes or any(
            not _SHA_PATTERN.fullmatch(str(value).lower()) for value in raw_hashes
        ):
            raise UniverseHandoffError("core universe source artifact hashes are invalid")
        if not _SHA_PATTERN.fullmatch(
            str(artifact.get("canonical_member_set_hash_sha256") or "").lower()
        ):
            raise UniverseHandoffError("core universe source member hash is invalid")
        if not str(binding.get("authority") or "").strip() or not str(
            binding.get("transformation_id") or ""
        ).strip():
            raise UniverseHandoffError("core universe source lineage binding is invalid")
        if not str(binding.get("source_scope") or "").strip():
            raise UniverseHandoffError("core universe source scope is missing")
        trusted = _TRUSTED_SOURCE_ROOTS.get(str(artifact.get("source_id") or "").strip())
        if trusted is None:
            if not allow_test_override or (
                str(binding.get("authority") or "").strip().lower() != "fixture"
            ):
                raise UniverseHandoffError("core universe source root is not trusted")
        else:
            if str(binding.get("index") or "").strip() != str(trusted.get("index") or ""):
                raise UniverseHandoffError("core universe source root index is not trusted")
            if str(binding.get("transformation_id") or "").strip() != str(
                trusted.get("transformation_id") or ""
            ).strip():
                raise UniverseHandoffError("core universe source transformation is not trusted")
            trusted_membership_authority = str(
                trusted.get("membership_authority") or ""
            ).strip()
            if trusted_membership_authority and str(
                binding.get("membership_authority") or ""
            ).strip() != trusted_membership_authority:
                raise UniverseHandoffError("core universe membership authority is not trusted")
            if trusted.get("official_index_authority") is not None and binding.get(
                "official_index_authority"
            ) is not trusted.get("official_index_authority"):
                raise UniverseHandoffError("core universe official authority is not trusted")
            derived_effective = _date_text(binding.get("derived_effective_date"))
            if derived_effective is None or derived_effective > market_date:
                raise UniverseHandoffError("core universe source effective date is invalid")
            trusted_effective = _date_text(trusted.get("effective_date"))
            recurring_effective = bool(
                trusted.get("allow_future_same_semantic_set_dates")
                and market_date > trusted_effective
                and trusted_effective <= derived_effective <= market_date
            ) if trusted_effective else False
            if (
                trusted_effective
                and derived_effective != trusted_effective
                and not recurring_effective
            ):
                raise UniverseHandoffError("core universe source effective date is not trusted")
            if trusted_effective and market_date > trusted_effective:
                age_basis = derived_effective if recurring_effective else trusted_effective
                semantic_age = (
                    date.fromisoformat(market_date) - date.fromisoformat(age_basis)
                ).days
                maximum_age = int(
                    trusted.get("maximum_source_age_days") or DEFAULT_MAX_AGE_DAYS
                )
                if (
                    not trusted.get("allow_future_same_semantic_set_dates")
                    or semantic_age < 0
                    or semantic_age > maximum_age
                ):
                    raise UniverseHandoffError("core universe source semantic age is not trusted")
            trusted_scope_template = str(trusted.get("source_scope_template") or "").strip()
            trusted_scope = (
                trusted_scope_template.format(market_date=market_date)
                if trusted_scope_template
                else str(trusted.get("source_scope") or "").strip()
            )
            if trusted_scope and str(binding.get("source_scope") or "").strip() != trusted_scope:
                raise UniverseHandoffError("core universe source scope is not trusted")
            trusted_uri = _trusted_source_uri(trusted, market_date)
            if trusted_uri and str(artifact.get("source_uri") or "").strip() != trusted_uri:
                raise UniverseHandoffError("core universe source URI is not trusted")
            trusted_hashes = [
                str(value).lower() for value in trusted.get("raw_artifact_hashes") or []
            ]
            if trusted_hashes and [str(value).lower() for value in raw_hashes] != trusted_hashes:
                raise UniverseHandoffError("core universe source raw hash is not trusted")
            trusted_member_hash = str(
                trusted.get("canonical_member_set_hash_sha256") or ""
            ).lower()
            if trusted_member_hash and not recurring_effective and str(
                artifact.get("canonical_member_set_hash_sha256") or ""
            ).lower() != trusted_member_hash:
                raise UniverseHandoffError("core universe canonical member root is not trusted")
            trusted_symbol_hash = str(
                trusted.get("canonical_symbol_set_hash_sha256") or ""
            ).lower()
            if trusted_symbol_hash:
                trusted_symbols = [
                    row.get("symbol")
                    for row in members
                    if isinstance(row, dict)
                    and index in (row.get("index_memberships") or [])
                ]
                if _canonical_symbol_set_hash(trusted_symbols, index) != trusted_symbol_hash:
                    raise UniverseHandoffError("core universe canonical symbol root is not trusted")
            trusted_rows = [
                row
                for row in members
                if isinstance(row, dict) and index in (row.get("index_memberships") or [])
            ]
            if any(
                canonical_symbol(row.get("provider_symbol") or row.get("symbol"))
                != canonical_symbol(row.get("symbol"))
                or str(row.get("asset_class") or row.get("security_type") or "common_stock")
                .strip()
                .lower()
                != "common_stock"
                for row in trusted_rows
            ):
                raise UniverseHandoffError("core universe provider mapping is not trusted")
            for field in (
                "lineage_builder_id",
                "lineage_transformation_id",
                "lineage_schema_version",
                "reconstitution_id",
            ):
                expected_lineage = str(trusted.get(field) or "").strip()
                if expected_lineage and str(
                    artifact.get(field) or binding.get(field) or ""
                ).strip() != expected_lineage:
                    raise UniverseHandoffError(
                        f"core universe {field} is not trusted"
                    )
    if str(core.get("status") or "").upper() == "READY":
        if len(source_ids) != len({str(value).strip() for value in source_ids}):
            raise UniverseHandoffError("core universe source identities are duplicated")
        if len(source_uris) != len({str(value).strip() for value in source_uris}):
            raise UniverseHandoffError("core universe source URIs are duplicated")
        artifact_ids = {str(item.get("source_id") or "").strip() for item in artifacts}
        artifact_uris = {str(item.get("source_uri") or "").strip() for item in artifacts}
        if artifact_ids != {str(value).strip() for value in source_ids}:
            raise UniverseHandoffError("core universe source identities are not bound")
        if artifact_uris != {str(value).strip() for value in source_uris}:
            raise UniverseHandoffError("core universe source URIs are not bound")
    for index in CORE_INDEXES:
        verdict = index_verdicts[index]
        if not isinstance(verdict, dict):
            raise UniverseHandoffError("core universe index verdict is malformed")
        status = str(verdict.get("status") or "").upper()
        if status not in {"READY", "DATA_UNAVAILABLE"}:
            raise UniverseHandoffError("core universe index verdict status is invalid")
        raw_expected_count = verdict.get("expected_count")
        try:
            expected_count = int(raw_expected_count) if raw_expected_count is not None else None
            observed_count = int(verdict.get("observed_unique_count"))
        except (TypeError, ValueError) as exc:
            raise UniverseHandoffError("core universe index counts are invalid") from exc
        if (expected_count is not None and expected_count < 0) or observed_count < 0:
            raise UniverseHandoffError("core universe index counts are invalid")
        if status == "READY" and (expected_count is None or expected_count <= 0):
            raise UniverseHandoffError(f"core universe {index} expected count is invalid")
        if status == "READY" and len(artifacts_by_index[index]) != 1:
            raise UniverseHandoffError(f"core universe source root is missing for {index}")
        if status == "READY":
            required = {
                "count_verdict": "PASS",
                "effective_date_verdict": "PASS",
                "freshness_verdict": "FRESH",
                "completeness_verdict": "COMPLETE",
            }
            if any(
                str(verdict.get(field) or "").upper() != value
                for field, value in required.items()
            ):
                raise UniverseHandoffError(f"core universe {index} verdict is not governed")
            if expected_count != observed_count:
                raise UniverseHandoffError(f"core universe {index} count binding is invalid")
            if any(
                str(artifact["source_binding"].get("status") or "").upper()
                != "VERIFIED"
                or not _SHA_PATTERN.fullmatch(
                    str(
                        artifact["source_binding"].get(
                            "derived_member_set_hash_sha256"
                        )
                        or ""
                    ).lower()
                )
                or int(artifact["source_binding"].get("derived_membership_count") or 0)
                != observed_count
                for artifact in artifacts_by_index[index]
            ):
                raise UniverseHandoffError(f"core universe {index} source binding is invalid")
            index_records = [
                {
                    "symbol": canonical_symbol(row.get("symbol") or row.get("ticker")),
                    "provider_symbol": canonical_symbol(
                        row.get("provider_symbol") or row.get("mapped_symbol") or row.get("symbol")
                    ),
                    "asset_class": str(
                        row.get("asset_class") or row.get("security_type") or "common_stock"
                    ).strip().lower(),
                    "index": index,
                    "valid_from": row.get("valid_from"),
                    "valid_to": row.get("valid_to"),
                }
                for row in members
                if isinstance(row, dict) and index in (row.get("index_memberships") or [])
            ]
            if len(index_records) != observed_count:
                raise UniverseHandoffError(f"core universe {index} member count binding is invalid")
            expected_index_hash = _canonical_member_hash(index_records)
            if any(
                str(artifact.get("canonical_member_set_hash_sha256") or "").lower()
                != expected_index_hash
                or str(
                    artifact["source_binding"].get("derived_member_set_hash_sha256")
                    or ""
                ).lower()
                != expected_index_hash
                for artifact in artifacts_by_index[index]
            ):
                raise UniverseHandoffError(f"core universe {index} member hash binding is invalid")
            ready_indexes.add(index)
    if str(core.get("status") or "").upper() == "READY" and ready_indexes != set(CORE_INDEXES):
        raise UniverseHandoffError("READY core universe does not prove both index lanes")
    if str(core.get("status") or "").upper() == "READY" and (
        str(core.get("completeness_verdict") or "").upper() != "COMPLETE"
        or str(core.get("freshness_verdict") or "").upper() != "FRESH"
    ):
        raise UniverseHandoffError("READY core universe top-level verdict is not governed")
    return ready_indexes


def _validate_source_summary(
    source: dict[str, Any], market_date: str, *, root: Path | None = None
) -> None:
    status = str(source.get("status") or "").strip().lower()
    if status not in _ALLOWED_SOURCE_STATUSES:
        raise UniverseHandoffError("Morning source summary status is invalid")
    source_identity = _first_text(source.get("source_identity"), source.get("run_id"))
    if not source_identity:
        raise UniverseHandoffError("Morning source summary identity is missing")
    requested = str(source.get("requested_observed_at") or "").strip()
    if not requested or _iso_date(requested) != market_date:
        raise UniverseHandoffError("Morning source summary is cross-date")
    snapshot_value = str(source.get("snapshot_path") or "").strip()
    if not snapshot_value:
        raise UniverseHandoffError("Morning source summary snapshot path is missing")
    paths = source.get("paths")
    if isinstance(paths, dict) and paths.get("premarket_snapshot") and root is not None:
        if _resolve_source_path(root, snapshot_value) != _resolve_source_path(
            root, str(paths["premarket_snapshot"])
        ):
            raise UniverseHandoffError("Morning source summary snapshot binding is invalid")


def _validate_core_binding(payload: dict[str, Any], market_date: str) -> None:
    core = payload.get("core_universe")
    if not isinstance(core, dict):
        raise UniverseHandoffError("universe handoff core binding is missing")
    if str(core.get("requested_market_date") or "") != market_date:
        raise UniverseHandoffError("universe handoff core binding is cross-date")
    if str(core.get("status") or "") == "READY" and not _SHA_PATTERN.fullmatch(
        str(core.get("content_hash_sha256") or "")
    ):
        raise UniverseHandoffError("universe handoff core hash is invalid")


def _expected_strategy_ids() -> tuple[str, ...]:
    return tuple(
        sorted(
            {
                str(strategy.strategy_id)
                for strategy in build_strategy_catalog()
                if str(strategy.strategy_id)
                not in {
                    "benchmark_buy_hold_equal_weight",
                    "baseline_buy_hold",
                    "cash_no_trade_baseline",
                }
            }
        )
    )


def _core_members(
    core: dict[str, Any], *, allowed_indexes: set[str] | None = None
) -> list[dict[str, Any]]:
    output: list[dict[str, Any]] = []
    for raw in core.get("members") or []:
        if not isinstance(raw, dict):
            continue
        symbol = canonical_symbol(raw.get("symbol") or raw.get("ticker"))
        if not _SYMBOL_PATTERN.fullmatch(symbol):
            continue
        memberships = sorted(
            {str(item).strip() for item in raw.get("index_memberships") or [] if str(item).strip()}
        )
        if allowed_indexes is not None:
            memberships = [item for item in memberships if item in allowed_indexes]
            if not memberships:
                continue
        output.append(
            {
                "symbol": symbol,
                "provider_symbol": canonical_symbol(
                    raw.get("provider_symbol") or raw.get("mapped_symbol") or symbol
                ),
                "asset_class": str(
                    raw.get("asset_class") or raw.get("security_type") or "common_stock"
                ).strip().lower(),
                "valid_from": raw.get("valid_from"),
                "valid_to": raw.get("valid_to"),
                "lanes": ["core"],
                "lane": "core",
                "index_memberships": memberships,
                "sources": sorted({str(item) for item in raw.get("sources") or [] if str(item)}),
                "member_lineage": {
                    "contract_id": str(core.get("contract_id") or ""),
                    "contract_hash_sha256": str(core.get("content_hash_sha256") or ""),
                    "canonical_member_set_hash_sha256": str(
                        core.get("canonical_member_set_hash_sha256") or ""
                    ),
                    "provider_symbol": canonical_symbol(
                        raw.get("provider_symbol") or raw.get("mapped_symbol") or symbol
                    ),
                    "provider_mapping": {
                        "canonical_symbol": symbol,
                        "provider_symbol": canonical_symbol(
                            raw.get("provider_symbol") or raw.get("mapped_symbol") or symbol
                        ),
                    },
                },
            }
        )
    return sorted(output, key=lambda row: row["symbol"])


def _mover_members(
    rows: Iterable[dict[str, Any]], source: dict[str, Any], market_date: str
) -> list[dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        symbol = canonical_symbol(row.get("ticker") or row.get("symbol"))
        if _SYMBOL_PATTERN.fullmatch(symbol):
            grouped.setdefault(symbol, []).append(dict(row, ticker=symbol))
    output: list[dict[str, Any]] = []
    for symbol, candidates in sorted(grouped.items()):
        selected = sorted(candidates, key=lambda row: json.dumps(row, sort_keys=True, default=str))[
            0
        ]
        output.append(
            {
                "symbol": symbol,
                "lanes": ["mover"],
                "lane": "mover",
                "index_memberships": [],
                "sources": sorted({str(row.get("source") or "unknown") for row in candidates}),
                "member_lineage": {
                    "source_identity": str(
                        source.get("source_identity") or source.get("run_id") or ""
                    ),
                    "source_status": str(source.get("status") or ""),
                    "market_date": market_date,
                    "source_row": selected,
                },
            }
        )
    return output


def _merge_members(
    core: Iterable[dict[str, Any]], movers: Iterable[dict[str, Any]]
) -> list[dict[str, Any]]:
    merged: dict[str, dict[str, Any]] = {}
    for item in [*core, *movers]:
        symbol = str(item["symbol"])
        existing = merged.get(symbol)
        if existing is None:
            merged[symbol] = dict(item)
            continue
        existing["lanes"] = sorted(set(existing.get("lanes") or []) | set(item.get("lanes") or []))
        existing["lane"] = "+".join(existing["lanes"])
        existing["index_memberships"] = sorted(
            set(existing.get("index_memberships") or []) | set(item.get("index_memberships") or [])
        )
        existing["sources"] = sorted(
            set(existing.get("sources") or []) | set(item.get("sources") or [])
        )
        lineage = existing.setdefault("member_lineage", {})
        lineage.update(item.get("member_lineage") or {})
    return [merged[symbol] for symbol in sorted(merged)]


def _read_mover_snapshot(path: Path, market_date: str) -> list[dict[str, Any]]:
    if not path.is_file():
        raise UniverseHandoffError("governed mover snapshot is missing")
    try:
        with path.open("r", encoding="utf-8-sig", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, csv.Error) as exc:
        raise UniverseHandoffError("governed mover snapshot is unreadable") from exc
    for row in rows:
        ticker = canonical_symbol(row.get("ticker") or row.get("symbol"))
        if not _SYMBOL_PATTERN.fullmatch(ticker):
            raise UniverseHandoffError("governed mover snapshot ticker is invalid")
        declared = str(row.get("market_date") or row.get("as_of_date") or "").strip()
        timestamp = _first_text(row.get("as_of_timestamp"), row.get("extracted_at"))
        if declared and declared != market_date:
            raise UniverseHandoffError("governed mover snapshot is cross-date")
        if not declared and (not timestamp or _iso_date(timestamp) != market_date):
            raise UniverseHandoffError("governed mover snapshot row date is missing")
        if not str(row.get("source") or row.get("source_identity") or "").strip():
            raise UniverseHandoffError("governed mover snapshot row source is missing")
    return rows


def _validate_symbols(raw: Any, label: str) -> list[str]:
    if not isinstance(raw, list):
        raise UniverseHandoffError(f"{label} are missing")
    symbols = [canonical_symbol(item) for item in raw]
    if not symbols or any(not _SYMBOL_PATTERN.fullmatch(symbol) for symbol in symbols):
        raise UniverseHandoffError(f"{label} contain invalid symbols")
    if symbols != sorted(set(symbols)):
        raise UniverseHandoffError(f"{label} are not exact deduplicated order")
    return symbols


def _artifact(path: Path, root: Path, kind: str) -> dict[str, Any]:
    if not path.is_file():
        raise UniverseHandoffError(f"required {kind} artifact is missing")
    return {
        "kind": kind,
        "path": path.resolve().relative_to(root).as_posix(),
        "sha256": _sha256_file(path),
    }


def _validate_source_artifact_manifest(root: Path, artifacts: list[dict[str, Any]]) -> None:
    """Require the immutable handoff to name exactly its five source inputs."""

    expected_kinds = {
        "alpha_cycle",
        "alpha_run_contract",
        "core_universe_contract",
        "mover_source_summary",
        "mover_snapshot",
    }
    if len(artifacts) != len(expected_kinds):
        raise UniverseHandoffError("universe handoff source artifact manifest is invalid")
    kinds = [str(item.get("kind") or "") for item in artifacts]
    if set(kinds) != expected_kinds or len(set(kinds)) != len(kinds):
        raise UniverseHandoffError("universe handoff source artifact kinds are invalid")
    paths: list[Path] = []
    for item in artifacts:
        path = _resolve_source_path(root, str(item.get("path") or ""))
        paths.append(path)
        if not path.is_file():
            raise UniverseHandoffError("universe handoff source artifact is missing")
        kind = str(item.get("kind") or "")
        expected_path = {
            "alpha_cycle": root / "alpha_cycle.json",
            "alpha_run_contract": root / "alpha_run_contract.json",
            "core_universe_contract": root / "core_universe_contract.json",
            "mover_source_summary": root / "web_collect" / "source_summary.json",
        }.get(kind)
        if expected_path is not None and path != expected_path.resolve():
            raise UniverseHandoffError("universe handoff source artifact path is invalid")
        if kind == "mover_snapshot" and path.suffix.lower() != ".csv":
            raise UniverseHandoffError("universe handoff mover snapshot format is invalid")
    if len({path.resolve() for path in paths}) != len(paths):
        raise UniverseHandoffError("universe handoff source artifact paths are duplicated")


def _resolve_source_path(root: Path, value: str) -> Path:
    candidate = Path(value)
    resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError as exc:
        raise UniverseHandoffError("source artifact path escapes Morning artifact root") from exc
    return resolved


def _read_required_json(path: Path, label: str) -> dict[str, Any]:
    if not path.is_file():
        raise UniverseHandoffError(f"{label} is missing")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise UniverseHandoffError(f"{label} is malformed") from exc
    if not isinstance(payload, dict):
        raise UniverseHandoffError(f"{label} must be an object")
    return payload


def _write_immutable(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    encoded = (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if path.exists():
        if path.read_bytes() != encoded:
            raise UniverseHandoffError("universe handoff conflicts with immutable same-day bytes")
        return
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_bytes(encoded)
    temporary.replace(path)


def _handoff_hash(payload: dict[str, Any]) -> str:
    unhashed = {
        key: value
        for key, value in payload.items()
        if key not in {"content_hash_sha256", "content_hash", "handoff_id", "universe_id"}
    }
    return hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
            "utf-8"
        )
    ).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as exc:
        raise UniverseHandoffError(f"source artifact is unreadable: {path.name}") from exc
    return digest.hexdigest()


def _date_text(value: Any) -> str | None:
    text = str(value or "").strip()
    try:
        return date.fromisoformat(text).isoformat()
    except ValueError:
        return None


def _iso_date(value: Any) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return _date_text(text[:10])


def _first_text(*values: Any) -> str:
    return next((str(value).strip() for value in values if str(value or "").strip()), "")


def _cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build or validate a governed PaperOps universe handoff"
    )
    parser.add_argument("--morning-root")
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--out")
    parser.add_argument("--handoff")
    parser.add_argument("--validate", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.validate:
            payload = validate_universe_handoff(args.handoff or args.out or "", args.market_date)
        else:
            payload = build_universe_handoff(
                args.morning_root or "", args.market_date, output_path=args.out
            )
    except (OSError, TypeError, ValueError, UniverseHandoffError) as exc:
        print(f"status: invalid\ndetail: {exc}")
        return 2
    print(
        "status: valid\n"
        f"handoff_id: {payload['handoff_id']}\n"
        f"universe_count: {len(payload['universe_symbols'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())


__all__ = [
    "SCHEMA_VERSION",
    "UniverseHandoffError",
    "build_universe_handoff",
    "load_universe_handoff",
    "validate_universe_handoff",
]
