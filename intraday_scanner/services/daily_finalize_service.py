"""One idempotent daily reconcile -> snapshot -> readiness chain."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.performance.snapshot import write_public_snapshot
from intraday_scanner.storage.migrations import run_migrations


class DailyFinalizeService:
    """Finalize one market date and fail readiness if any upstream step is incomplete."""

    def __init__(self, db_path: str | Path, output_root: str | Path = "build/public") -> None:
        self.db_path = Path(db_path)
        self.output_root = Path(output_root)
        self.lock_path = self.output_root / ".daily-finalize.lock"

    def run(
        self,
        *,
        market_date: str,
        retry_limit: int = 2,
        retry_delay_seconds: int = 0,
        now: str | None = None,
    ) -> dict[str, Any]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        acquired = False
        run_id = hashlib.sha256(f"{market_date}:{now or _utc_now()}".encode()).hexdigest()[:20]
        try:
            self.lock_path.touch(exist_ok=False)
            acquired = True
        except FileExistsError:
            return self._failure(market_date, run_id, "lock_held", http_status=503)
        try:
            self._record_run(run_id, market_date, "IN_PROGRESS", "lock_acquired", now=now)
            last_error: str | None = None
            for attempt in range(retry_limit + 1):
                try:
                    # Rebuild the canonical read model from all raw history.
                    # The run date scopes the publication/readiness record; it
                    # must not delete prior canonical days.
                    result = CanonicalPerformanceService(self.db_path).reconcile(
                        persist=True,
                        now=now,
                    )
                    publication = write_public_snapshot(
                        self.db_path,
                        self.output_root / "data" / "performance.json",
                        market_date=market_date,
                    )
                    readiness = self._write_readiness(result, publication, market_date)
                    stage = self._write_stage_manifest(result, publication, readiness, attempt)
                    status = str(result.get("status") or "DEGRADED")
                    self._record_run(
                        run_id,
                        market_date,
                        status,
                        "published",
                        now=now,
                        retry_count=attempt,
                        input_hash=result.get("input_hash_sha256"),
                        output_hash=publication["manifest"].get("payload_sha256"),
                    )
                    return {
                        "run_id": run_id,
                        "market_date": market_date,
                        "status": status,
                        "retry_count": attempt,
                        "readiness": readiness,
                        "stage_manifest": stage,
                        "reconciliation": {
                            "row_count": result.get("row_count"),
                            "daily_count": result.get("daily_count"),
                            "issue_count": result.get("issue_count"),
                            "input_hash_sha256": result.get("input_hash_sha256"),
                        },
                    }
                except (
                    Exception
                ) as exc:  # pragma: no cover - exercised by failure integration tests
                    last_error = f"{type(exc).__name__}: {exc}"
                    if attempt < retry_limit and retry_delay_seconds > 0:
                        time.sleep(retry_delay_seconds)
            self._record_run(
                run_id,
                market_date,
                "FAILED",
                "failed",
                now=now,
                retry_count=retry_limit,
                failure_reason=last_error,
            )
            return self._failure(
                market_date, run_id, last_error or "finalize_failed", http_status=503
            )
        finally:
            if acquired and self.lock_path.exists():
                self.lock_path.unlink()

    def _write_readiness(
        self,
        reconciliation: dict[str, Any],
        publication: dict[str, Any],
        market_date: str,
    ) -> dict[str, Any]:
        snapshot_status = str(publication["manifest"].get("status") or "degraded")
        status = "ready" if snapshot_status in {"complete", "no_trade"} else "not_ready"
        payload = {
            "schema_version": "dawnstrike.readiness.v1",
            "market_date": market_date,
            "status": status,
            "http_status": 200 if status == "ready" else 503,
            "snapshot_status": snapshot_status,
            "reconciliation_status": reconciliation.get("status"),
            "input_hash_sha256": reconciliation.get("input_hash_sha256"),
            "payload_sha256": publication["manifest"].get("payload_sha256"),
            "live_trading_enabled": False,
            "research_only": True,
            "reason": "complete_or_explicit_no_trade"
            if status == "ready"
            else "missing_or_degraded_upstream_truth",
        }
        path = self.output_root / "readiness.json"
        path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        return payload

    def _write_stage_manifest(
        self,
        reconciliation: dict[str, Any],
        publication: dict[str, Any],
        readiness: dict[str, Any],
        retry_count: int,
    ) -> dict[str, Any]:
        payload = {
            "schema_version": "dawnstrike.daily_stage_manifest.v1",
            "generated_at": _utc_now(),
            "input_hash_sha256": reconciliation.get("input_hash_sha256"),
            "output_hash_sha256": publication["manifest"].get("payload_sha256"),
            "retry_count": retry_count,
            "readiness": readiness,
            "artifacts": [
                "data/performance.json",
                "data/performance.json.manifest.json",
                "readiness.json",
            ],
            "research_only": True,
            "live_trading_enabled": False,
        }
        (self.output_root / "stage-manifest.json").write_text(
            json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8"
        )
        return payload

    def _record_run(
        self,
        run_id: str,
        market_date: str,
        status: str,
        stage: str,
        *,
        now: str | None,
        retry_count: int = 0,
        input_hash: object = None,
        output_hash: object = None,
        failure_reason: str | None = None,
    ) -> None:
        with sqlite3.connect(self.db_path) as connection:
            run_migrations(connection)
            connection.execute(
                """
                INSERT INTO daily_finalize_runs
                (run_id, market_date, status, stage, started_at, completed_at,
                 input_hash_sha256, output_hash_sha256, retry_count, failure_reason, payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(run_id) DO UPDATE SET
                    status = excluded.status, stage = excluded.stage,
                    completed_at = excluded.completed_at,
                    input_hash_sha256 = excluded.input_hash_sha256,
                    output_hash_sha256 = excluded.output_hash_sha256,
                    retry_count = excluded.retry_count,
                    failure_reason = excluded.failure_reason,
                    payload_json = excluded.payload_json
                """,
                (
                    run_id,
                    market_date,
                    status,
                    stage,
                    now or _utc_now(),
                    _utc_now() if status not in {"IN_PROGRESS"} else None,
                    str(input_hash) if input_hash else None,
                    str(output_hash) if output_hash else None,
                    retry_count,
                    failure_reason,
                    json.dumps(
                        {"run_id": run_id, "stage": stage, "status": status}, sort_keys=True
                    ),
                ),
            )

    def _failure(
        self, market_date: str, run_id: str, reason: str, *, http_status: int
    ) -> dict[str, Any]:
        payload = {
            "run_id": run_id,
            "market_date": market_date,
            "status": "FAILED",
            "reason": reason,
            "readiness": {"status": "not_ready", "http_status": http_status},
        }
        (self.output_root / "readiness.json").write_text(
            json.dumps(payload["readiness"], sort_keys=True, indent=2), encoding="utf-8"
        )
        return payload


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
