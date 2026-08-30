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
from scripts.public_lineage import build_sha as _lineage_build_sha
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
        expected_build_sha = _lineage_build_sha(
            source_sha=release_sha,
            publication_set_sha256=publication_set_sha,
            opportunity_projection_sha256=opportunity_projection_sha,
            v6_learning_sha256=v6_learning_sha,
            market_date=market_date[:10],
        )
        expected_build_id = expected_build_sha[:20]
    elif (
        publication_set_sha
        and opportunity_projection_sha
        and not v6_learning_sha
        and not all(
            is_lower_hex64(value)
            for value in (publication_set_sha, opportunity_projection_sha)
        )
    ):
        # Receipts written before V6 remain readable as historical evidence.
        # Any receipt carrying a V6 value must satisfy the strict contract.
        expected_build_id = hashlib.sha256(
            f"{release_sha}:{publication_set_sha}:{opportunity_projection_sha}:{market_date[:10]}".encode()
        ).hexdigest()[:20]
        legacy_receipt = True
    publication_identity_ready = bool(
        publication_payload.get("status") == "PRODUCTION_VERIFIED"
        and publication_payload.get("promoted") is True
        and publication_payload.get("source_sha") == release_sha
        and (
            publication_payload.get("market_date") == market_date[:10]
            or legacy_receipt
        )
        and publication_payload.get("build_id") == expected_build_id
        and (
            (
                strict_v6_lineage
                and publication_payload.get("build_sha") == expected_build_sha
                and publication_payload.get("v6_learning_sha256") == v6_learning_sha
            )
            or (not v6_learning_sha and not strict_v6_lineage)
        )
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
        "v6_learning_sha256": v6_learning_sha or None,
        "expected_build_sha": expected_build_sha or None,
        "expected_build_id": expected_build_id or None,
        "research_only": True,
        "broker_execution_enabled": False,
    }


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
