"""Fail-closed gate for a local artifact immediately before Vercel upload.

The publication stage is intentionally absent from the required-stage set: it
is the side effect this gate authorizes and therefore cannot be its own input.
Post-publication identity remains the responsibility of
``verify_daily_finalize_receipt.py``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.approved_tools import run_git
from intraday_scanner.services.daily_run_service import (
    REQUIRED_UPSTREAM_STAGES,
    daily_run_snapshot,
    shared_daily_run_id,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from scripts.publication_boundary import prepublication_authorization_id
from scripts.verify_public_artifact import (
    public_artifact_identity,
)
from scripts.verify_public_artifact import (
    verify as verify_public_artifact,
)

PREPUBLICATION_STAGES = (
    *REQUIRED_UPSTREAM_STAGES,
    "canonical_performance",
    "calendar_build",
    "readiness",
)
PREPUBLICATION_SUCCESS_STATUSES = {
    "morning_collection": frozenset({"COMPLETE"}),
    "ranking_delivery": frozenset({"COMPLETE"}),
    "intraday_monitor": frozenset({"COMPLETE", "NO_TRADE"}),
    "eod_outcome_capture": frozenset({"COMPLETE", "NO_TRADE"}),
    "paper_reconciliation": frozenset({"COMPLETE", "NO_TRADE"}),
    "canonical_performance": frozenset({"COMPLETE"}),
    "calendar_build": frozenset({"COMPLETE", "NO_TRADE"}),
    "readiness": frozenset({"COMPLETE"}),
}


def verify(
    db_path: str | Path,
    artifact_root: str | Path,
    market_date: str,
    release_sha: str,
    *,
    runtime_root: str | Path | None = None,
    expected_market_date: str | None = None,
) -> dict[str, object]:
    """Return a machine-readable authorization decision without publishing."""

    errors: list[str] = []
    normalized_date = str(market_date).strip()
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", normalized_date) is None:
        errors.append("market_date_invalid")
    normalized_sha = str(release_sha).strip()
    if re.fullmatch(r"[0-9a-f]{40}", normalized_sha) is None:
        errors.append("release_sha_invalid")
    expected = str(expected_market_date or normalized_date).strip()
    if expected != normalized_date:
        errors.append("expected_market_date_mismatch")

    if runtime_root is not None:
        observed_sha = _git_head(Path(runtime_root))
        if observed_sha != normalized_sha:
            errors.append("runtime_release_sha_mismatch")

    snapshot: dict[str, Any] = {}
    try:
        store = SQLiteScanStore(db_path, read_only=True)
        store.initialize()
        run_id = shared_daily_run_id(normalized_date, normalized_sha)
        snapshot = daily_run_snapshot(store, run_id)
    except Exception as exc:
        run_id = shared_daily_run_id(normalized_date, normalized_sha)
        errors.append(f"daily_run_unreadable:{type(exc).__name__}")

    run = snapshot.get("run") if isinstance(snapshot, dict) else None
    run = run if isinstance(run, dict) else {}
    if run.get("market_date") != normalized_date:
        errors.append("daily_run_market_date_mismatch")
    if run.get("release_sha") != normalized_sha:
        errors.append("daily_run_release_sha_mismatch")
    statuses = snapshot.get("latest_stage_statuses") if isinstance(snapshot, dict) else {}
    statuses = statuses if isinstance(statuses, dict) else {}
    for stage in PREPUBLICATION_STAGES:
        status = str((statuses.get(stage) or {}).get("status") or "")
        if status not in PREPUBLICATION_SUCCESS_STATUSES[stage]:
            errors.append(f"stage_not_acceptable:{stage}:{status or 'missing'}")
    publication_stage = statuses.get("publication") or {}
    if str(publication_stage.get("status") or "") != "COMPLETE":
        errors.append("stage_not_acceptable:publication:missing_or_incomplete")

    root = Path(artifact_root).resolve()
    artifact = verify_public_artifact(root, expected_source_sha=normalized_sha)
    if artifact.get("status") != "PASS":
        errors.append("local_artifact_verification_failed")
    readiness = _read_object(root / "readiness.json")
    if readiness.get("status") != "ready" or readiness.get("http_status") != 200:
        errors.append("local_readiness_not_http_200")
    if readiness.get("market_date") != normalized_date:
        errors.append("artifact_market_date_mismatch")
    if readiness.get("research_only") is not True:
        errors.append("research_only_required")
    if readiness.get("broker_execution_enabled") is not False:
        errors.append("broker_execution_must_be_disabled")
    build_manifest = _read_object(root / "build-manifest.json")
    release_manifest = _read_object(root / "release-manifest.json")
    if build_manifest.get("market_date") != normalized_date:
        errors.append("build_market_date_mismatch")
    if build_manifest.get("source_sha") != normalized_sha:
        errors.append("build_release_sha_mismatch")
    if build_manifest.get("research_only") is not True:
        errors.append("build_research_only_required")
    if build_manifest.get("live_trading_enabled") is True:
        errors.append("build_live_trading_enabled")
    if build_manifest.get("broker_execution_enabled") is not False:
        errors.append("build_broker_execution_must_be_disabled")
    if runtime_root is not None:
        expected_boundary = hashlib.sha256(
            f"{Path(runtime_root).resolve()}\n{Path(db_path).resolve().parent}".encode()
        ).hexdigest()
        if release_manifest.get("deployment_boundary_sha256") != expected_boundary:
            errors.append("release_deployment_boundary_hash_mismatch")
    observed_schema = _read_schema_version(Path(db_path))
    if (
        observed_schema is None
        or release_manifest.get("database_schema_version") != observed_schema
    ):
        errors.append("release_database_schema_version_mismatch")

    performance_manifest = _read_object(root / "data" / "performance.json.manifest.json")
    calendar_manifest = _read_object(root / "data" / "calendar.json.manifest.json")
    publication_set = _read_object(root / "data" / "publication-set.json")
    canonical_stage = statuses.get("canonical_performance") or {}
    calendar_stage = statuses.get("calendar_build") or {}
    readiness_stage = statuses.get("readiness") or {}
    if canonical_stage.get("output_hash_sha256") != performance_manifest.get(
        "input_hash_sha256"
    ):
        errors.append("ledger_canonical_performance_hash_mismatch")
    if calendar_stage.get("output_hash_sha256") != calendar_manifest.get(
        "payload_sha256"
    ):
        errors.append("ledger_calendar_payload_hash_mismatch")
    publication_root = publication_set.get("publication_set_sha256")
    if publication_stage.get("output_hash_sha256") != publication_root:
        errors.append("ledger_publication_set_hash_mismatch")
    if readiness_stage.get("input_hash_sha256") != publication_root or readiness_stage.get(
        "output_hash_sha256"
    ) != publication_root:
        errors.append("ledger_readiness_publication_set_hash_mismatch")
    if readiness.get("publication_set_sha256") != publication_root:
        errors.append("readiness_publication_set_hash_mismatch")

    exact_artifact_identity: dict[str, Any]
    try:
        exact_artifact_identity = public_artifact_identity(root)
    except (OSError, RuntimeError) as exc:
        exact_artifact_identity = {
            "build_manifest_sha256": None,
            "release_manifest_raw_sha256": None,
            "public_artifact_root_sha256": None,
        }
        errors.append(f"prepublication_artifact_identity_unavailable:{type(exc).__name__}")

    artifact_identity = {
        "build_sha": build_manifest.get("build_sha"),
        "build_id": build_manifest.get("build_id"),
        "publication_set_sha256": build_manifest.get("publication_set_sha256"),
        "release_manifest_sha256": build_manifest.get("release_manifest_sha256"),
        **exact_artifact_identity,
    }
    authorization_id = prepublication_authorization_id(
        expected_market_date=expected,
        release_sha=normalized_sha,
        run_id=run_id,
        stage_statuses=statuses,
        artifact_identity=artifact_identity,
    )
    if any(value in (None, "") for value in artifact_identity.values()):
        errors.append("prepublication_artifact_identity_missing")

    return {
        "status": "PASS" if not errors else "BLOCKED",
        "ready": not errors,
        "run_id": run_id,
        "market_date": normalized_date,
        "release_sha": normalized_sha,
        "expected_market_date": expected,
        "authorization_schema_version": "dawnstrike.prepublication_authorization.v1",
        "authorization_id": authorization_id,
        "prepublication_authorization_id": authorization_id,
        "daily_ledger_authorization_id": authorization_id,
        "required_stages": list(PREPUBLICATION_STAGES),
        "publication_stage_excluded": True,
        "local_publication_stage_required": True,
        "artifact_identity": artifact_identity,
        "errors": list(dict.fromkeys(errors)),
        "artifact": artifact,
        "research_only": readiness.get("research_only"),
        "broker_execution_enabled": readiness.get("broker_execution_enabled"),
    }


def _read_object(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _git_head(root: Path) -> str:
    try:
        return run_git(root, "rev-parse", "HEAD").stdout.strip().lower()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _read_schema_version(path: Path) -> int | None:
    try:
        connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
        try:
            row = connection.execute(
                "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
            ).fetchone()
        finally:
            connection.close()
    except sqlite3.Error:
        return None
    return int(row[0]) if row else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", required=True)
    parser.add_argument("--artifact-root", required=True)
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--release-sha", required=True)
    parser.add_argument("--runtime-root", default=None)
    parser.add_argument("--expected-market-date", default=None)
    args = parser.parse_args(argv)
    result = verify(
        args.db_path,
        args.artifact_root,
        args.market_date,
        args.release_sha,
        runtime_root=args.runtime_root,
        expected_market_date=args.expected_market_date,
    )
    print(json.dumps(result, indent=2, sort_keys=True, default=str))
    return 0 if result["ready"] is True else 4


if __name__ == "__main__":
    raise SystemExit(main())
