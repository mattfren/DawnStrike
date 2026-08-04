"""Require an exact-date, exact-release completed finalize receipt."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.daily_run_service import daily_run_snapshot, shared_daily_run_id
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

FINALIZE_STAGES = (
    "canonical_performance",
    "calendar_build",
    "publication",
    "readiness",
)


def verify(db_path: str | Path, market_date: str, release_sha: str) -> dict[str, object]:
    run_id = shared_daily_run_id(market_date, release_sha)
    store = SQLiteScanStore(db_path)
    store.initialize()
    snapshot = daily_run_snapshot(store, run_id)
    run = snapshot.get("run") or {}
    statuses = snapshot.get("latest_stage_statuses") or {}
    missing_or_failed = [
        stage
        for stage in FINALIZE_STAGES
        if str((statuses.get(stage) or {}).get("status") or "") != "COMPLETE"
    ]
    latest_rows: dict[str, dict[str, object]] = {}
    for row in snapshot.get("stages") or []:
        stage = str(row.get("stage_name") or "")
        if int(row.get("attempt_no") or 0) >= int(
            latest_rows.get(stage, {}).get("attempt_no") or 0
        ):
            latest_rows[stage] = row
    publication = latest_rows.get("publication") or {}
    publication_payload = publication.get("payload") or publication.get("payload_json") or {}
    if not isinstance(publication_payload, dict):
        publication_payload = {}
    publication_set_sha = str(publication_payload.get("publication_set_sha256") or "")
    expected_build_id = (
        hashlib.sha256(
            f"{release_sha}:{publication_set_sha}:{market_date[:10]}".encode()
        ).hexdigest()[:20]
        if publication_set_sha
        else ""
    )
    publication_identity_ready = bool(
        publication_payload.get("status") == "PRODUCTION_VERIFIED"
        and publication_payload.get("promoted") is True
        and publication_payload.get("source_sha") == release_sha
        and publication_payload.get("build_id") == expected_build_id
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
