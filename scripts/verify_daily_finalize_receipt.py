"""Require an exact-date, exact-release completed finalize receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.daily_run_service import (
    daily_run_snapshot,
    shared_daily_run_id,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from scripts.public_lineage import is_lower_hex64

FINALIZE_STAGES = (
    "canonical_performance",
    "calendar_build",
    "publication",
    "readiness",
)
FINALIZE_SUCCESS_STATUSES = {
    "canonical_performance": frozenset({"COMPLETE"}),
    "calendar_build": frozenset({"COMPLETE", "NO_TRADE"}),
    "publication": frozenset({"COMPLETE"}),
    "readiness": frozenset({"COMPLETE"}),
}
_V6_IDENTITY_FIELDS = ("build_sha", "market_date", "schema_version", "v6_learning_sha256")
_STRICT_HASH_FIELDS = (
    "build_manifest_sha256",
    "authorized_build_manifest_sha256",
    "authorized_release_manifest_sha256",
    "public_artifact_root_sha256",
    "toolchain_identity_sha256",
    "vercel_source_manifest_sha256",
    "vercel_package_manifest_sha256",
    "release_manifest_sha256",
    "account_session_report_sha256",
)
_OFFICIAL_ACCOUNT_SESSION_IDENTITY = {
    "account_id": "alphaops_v5_simulated",
    "version_bucket": "v5",
    "cohort": "official_forward_paper",
    "strategy_id": "alphaops_v5",
    "strategy_version": "dawnstrike-alphaops-v5.0.0",
}


def verify(db_path: str | Path, market_date: str, release_sha: str) -> dict[str, object]:
    run_id = shared_daily_run_id(market_date, release_sha)
    store = SQLiteScanStore(db_path, read_only=True)
    store.initialize()
    snapshot = daily_run_snapshot(store, run_id)
    run = snapshot.get("run") or {}
    statuses = snapshot.get("latest_stage_statuses") or {}
    missing_or_failed = [
        stage
        for stage in FINALIZE_STAGES
        if str((statuses.get(stage) or {}).get("status") or "")
        not in FINALIZE_SUCCESS_STATUSES[stage]
    ]
    latest_rows: dict[str, dict[str, object]] = {}
    for row in snapshot.get("stages") or []:
        stage = str(row.get("stage_name") or "")
        row_attempt = row.get("attempt_no")
        latest_attempt = latest_rows.get(stage, {}).get("attempt_no")
        row_attempt_no = row_attempt if isinstance(row_attempt, int) else 0
        latest_attempt_no = latest_attempt if isinstance(latest_attempt, int) else 0
        if row_attempt_no >= latest_attempt_no:
            latest_rows[stage] = row
    publication = latest_rows.get("publication") or {}
    publication_payload = publication.get("payload") or publication.get("payload_json") or {}
    if not isinstance(publication_payload, dict):
        publication_payload = {}
    publication_set_sha = str(publication_payload.get("publication_set_sha256") or "")
    opportunity_projection_sha = str(
        publication_payload.get("opportunity_projection_sha256") or ""
    )
    v6_learning_sha = str(publication_payload.get("v6_learning_sha256") or "")
    expected_build_sha = ""
    expected_build_id = ""
    legacy_receipt = False
    strict_v6_lineage = all(
        is_lower_hex64(value)
        for value in (publication_set_sha, opportunity_projection_sha, v6_learning_sha)
    )
    if strict_v6_lineage:
        expected_build_sha = hashlib.sha256(
            (
                f"{release_sha}:{publication_set_sha}:{opportunity_projection_sha}:"
                f"{v6_learning_sha}:{market_date[:10]}"
            ).encode()
        ).hexdigest()
        expected_build_id = expected_build_sha[:20]
    elif _is_structurally_pre_v6_receipt(publication_payload):
        # Only structurally pre-V6 receipts remain readable as historical
        # evidence.  Any V6-era identity fields force the strict contract.
        expected_build_id = hashlib.sha256(
            f"{release_sha}:{publication_set_sha}:{opportunity_projection_sha}:{market_date[:10]}".encode()
        ).hexdigest()[:20]
        legacy_receipt = True
    strict_deployment_evidence = bool(
        publication_payload.get("schema_version") == "dawnstrike.daily_deployment.v1"
        and publication_payload.get("market_date") == market_date[:10]
        and publication_payload.get("expected_market_date") == market_date[:10]
        and publication_payload.get("allow_degraded") is False
        and publication_payload.get("research_only") is True
        and publication_payload.get("live_trading_enabled") is False
        and publication_payload.get("broker_execution_enabled") is False
        and _deployment_artifact_proofs_ready(publication_payload)
        and _is_git_object_id(publication_payload.get("source_tree"))
        and all(is_lower_hex64(publication_payload.get(field)) for field in _STRICT_HASH_FIELDS)
        and _account_session_report_ready(
            publication_payload.get("account_session_report"),
            expected_hash=publication_payload.get("account_session_report_sha256"),
            market_date=market_date[:10],
            release_sha=release_sha,
        )
    )
    publication_identity_ready = bool(
        strict_v6_lineage
        and strict_deployment_evidence
        and publication_payload.get("status") == "PRODUCTION_VERIFIED"
        and publication_payload.get("promoted") is True
        and publication_payload.get("source_sha") == release_sha
        and publication_payload.get("market_date") == market_date[:10]
        and publication_payload.get("build_id") == expected_build_id
        and publication_payload.get("build_sha") == expected_build_sha
        and publication_payload.get("v6_learning_sha256") == v6_learning_sha
        and publication_payload.get("promoted_deployment_id")
        and publication_payload.get("promoted_deployment_id")
        == publication_payload.get("production_deployment_id")
    )
    ready = bool(
        str(run.get("status") or "") == "COMPLETE"
        and not missing_or_failed
        and publication_identity_ready
    )
    return {
        "status": "READY" if ready else "BLOCKED",
        "ready": ready,
        "run_id": run_id,
        "market_date": market_date[:10],
        "release_sha": release_sha,
        "run_status": run.get("status"),
        "missing_or_failed_finalize_stages": missing_or_failed,
        "publication_identity_ready": publication_identity_ready,
        "strict_deployment_evidence": strict_deployment_evidence,
        "historical_legacy_receipt": legacy_receipt,
        "v6_learning_sha256": v6_learning_sha or None,
        "expected_build_sha": expected_build_sha or None,
        "expected_build_id": expected_build_id or None,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _is_structurally_pre_v6_receipt(payload: dict[str, object]) -> bool:
    """Recognize only receipts with no V6-era identity fields at all."""

    return not any(field in payload for field in _V6_IDENTITY_FIELDS)


def _is_git_object_id(value: object) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 40
        and value == value.lower()
        and all(character in "0123456789abcdef" for character in value)
    )


def _account_session_report_ready(
    value: object, *, expected_hash: object, market_date: str, release_sha: str
) -> bool:
    if not isinstance(value, dict) or not is_lower_hex64(expected_hash):
        return False
    canonical = json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    if hashlib.sha256(canonical).hexdigest() != expected_hash:
        return False
    expected = value.get("expected_session_count")
    if (
        value.get("schema_version") != "dawnstrike.account_session_report.v1"
        or value.get("status") != "COMPLETE"
        or value.get("market_date") != market_date
        or value.get("code_sha") != release_sha
        or value.get("research_only") is not True
        or value.get("broker_execution_enabled") is not False
        or value.get("unsafe_ledger_count") != 0
        or any(
            value.get(field) != expected_value
            for field, expected_value in _OFFICIAL_ACCOUNT_SESSION_IDENTITY.items()
        )
        or isinstance(expected, bool)
        or not isinstance(expected, int)
        or expected < 1
        or value.get("ledger_row_count") != expected
        or value.get("complete_count") != expected
        or value.get("missing_count") != 0
        or value.get("partial_count") != 0
        or value.get("quarantined_count") != 0
    ):
        return False
    if not all(
        is_lower_hex64(value.get(field))
        for field in (
            "input_hash_sha256",
            "expected_calendar_hash_sha256",
            "source_hashes_sha256",
        )
    ):
        return False
    series = value.get("series")
    if not isinstance(series, list) or len(series) != 1 or not isinstance(series[0], dict):
        return False
    item = series[0]
    return bool(
        item.get("status") == "COMPLETE"
        and item.get("market_date") == market_date
        and item.get("code_sha") == release_sha
        and item.get("expected_session_count") == expected
        and item.get("ledger_row_count") == expected
        and item.get("complete_count") == expected
        and item.get("research_only") is True
        and item.get("broker_execution_enabled") is False
        and all(
            item.get(field) == expected_value
            for field, expected_value in _OFFICIAL_ACCOUNT_SESSION_IDENTITY.items()
        )
    )


def _deployment_artifact_proofs_ready(payload: dict[str, object]) -> bool:
    authorization = payload.get("prepublication_authorization_id")
    if (
        not is_lower_hex64(authorization)
        or payload.get("daily_ledger_authorization_id") != authorization
    ):
        return False
    preview_url = payload.get("preview_url")
    if not isinstance(preview_url, str) or not preview_url.startswith("https://"):
        return False
    aliases = payload.get("production_aliases")
    if (
        not isinstance(aliases, list)
        or not aliases
        or aliases != sorted(set(aliases))
        or any(not isinstance(alias, str) or not alias.startswith("https://") for alias in aliases)
    ):
        return False
    proof_keys = {
        "endpoint",
        "build_sha",
        "asset_count",
        "total_bytes",
        "file_hashes_sha256",
    }

    def proof_tuple(value: object, *, endpoint: str) -> tuple[object, ...] | None:
        if not isinstance(value, dict) or set(value) != proof_keys:
            return None
        count = value.get("asset_count")
        total = value.get("total_bytes")
        if (
            value.get("endpoint") != endpoint.rstrip("/")
            or value.get("build_sha") != payload.get("build_sha")
            or isinstance(count, bool)
            or not isinstance(count, int)
            or not 1 <= count <= 256
            or isinstance(total, bool)
            or not isinstance(total, int)
            or not 0 <= total <= 134_217_728
            or not is_lower_hex64(value.get("file_hashes_sha256"))
        ):
            return None
        return (
            value.get("build_sha"),
            count,
            total,
            value.get("file_hashes_sha256"),
        )

    expected = proof_tuple(payload.get("preview_artifact_proof"), endpoint=preview_url)
    if expected is None or expected[3] != payload.get("public_artifact_root_sha256"):
        return False
    production = payload.get("production_artifact_proofs")
    if not isinstance(production, list) or len(production) != len(aliases):
        return False
    return all(
        proof_tuple(proof, endpoint=alias) == expected
        for proof, alias in zip(production, aliases, strict=True)
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--release-sha", required=True)
    args = parser.parse_args()
    result = verify(args.db_path, args.market_date, args.release_sha)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ready"] is True else 4


if __name__ == "__main__":
    raise SystemExit(main())
