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

DAILY_STAGE_NAMES = (
    "source_collection",
    "candidate_normalization",
    "selection",
    "delivery",
    "paper_fills",
    "outcome_capture",
    "paper_reconciliation",
    "canonical_performance",
    "public_snapshot",
    "preview_deployment",
    "production_promotion",
    "readiness",
)


class DailyFinalizeService:
    """Finalize one market date and fail readiness if any upstream step is incomplete."""

    def __init__(
        self,
        db_path: str | Path,
        output_root: str | Path = "build/public",
        paper_ops_root: str | Path | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.output_root = Path(output_root)
        self.paper_ops_root = Path(paper_ops_root) if paper_ops_root is not None else None
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
        run_id = hashlib.sha256(
            f"dawnstrike:daily-finalize:v1:{market_date}".encode()
        ).hexdigest()[:20]
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
                    result = CanonicalPerformanceService(
                        self.db_path,
                        paper_ops_root=self.paper_ops_root,
                    ).reconcile(
                        persist=True,
                        now=now,
                    )
                    publication = write_public_snapshot(
                        self.db_path,
                        self.output_root / "data" / "performance.json",
                        market_date=market_date,
                    )
                    upstream_status = self._read_upstream_stage(market_date)
                    readiness = self._write_readiness(
                        result, publication, market_date, upstream_status
                    )
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
                            "coverage": _coverage_summary(result.get("daily")),
                            "paper_ops": _paper_ops_summary(
                                result.get("paper_ops_reconciliation")
                            ),
                        },
                        "upstream_status": upstream_status,
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
        upstream_status: str = "not_recorded",
    ) -> dict[str, Any]:
        snapshot_status = str(publication["manifest"].get("status") or "degraded")
        status = (
            "ready"
            if snapshot_status in {"complete", "no_trade"} and upstream_status == "complete"
            else "not_ready"
        )
        payload = {
            "schema_version": "dawnstrike.readiness.v1",
            "market_date": market_date,
            "status": status,
            "http_status": 200 if status == "ready" else 503,
            "snapshot_status": snapshot_status,
            "upstream_status": upstream_status,
            "reconciliation_status": reconciliation.get("status"),
            "input_hash_sha256": reconciliation.get("input_hash_sha256"),
            "payload_sha256": publication["manifest"].get("payload_sha256"),
            "live_trading_enabled": False,
            "research_only": True,
            "reason": "complete_or_explicit_no_trade_with_upstream_success"
            if status == "ready"
            else "missing_or_degraded_upstream_truth",
        }
        path = self.output_root / "readiness.json"
        path.write_text(json.dumps(payload, sort_keys=True, indent=2), encoding="utf-8")
        return payload

    def _read_upstream_stage(self, market_date: str) -> str:
        """Read the latest local EOD automation result without mutating it."""

        try:
            with sqlite3.connect(self.db_path) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_runs'"
                ).fetchone()
                if not exists:
                    return "not_recorded"
                rows = connection.execute(
                    """
                    SELECT run_type, status, completed_at, payload_json
                    FROM automation_runs
                    WHERE lower(run_type) LIKE '%eod%'
                    ORDER BY completed_at DESC, rowid DESC
                    LIMIT 20
                    """
                ).fetchall()
        except sqlite3.Error:
            return "unreadable"
        for _run_type, status, completed_at, payload_json in rows:
            try:
                payload = json.loads(str(payload_json or "{}"))
            except json.JSONDecodeError:
                payload = {}
            observed_date = str(
                payload.get("market_date")
                or payload.get("run_date")
                or payload.get("date")
                or str(completed_at or "")[:10]
            )[:10]
            if observed_date != market_date:
                continue
            normalized = str(status or payload.get("status") or "").lower()
            if normalized in {"complete", "completed", "success", "ok", "passed", "ready"}:
                return "complete"
            return "failed"
        return "not_recorded"

    def _write_stage_manifest(
        self,
        reconciliation: dict[str, Any],
        publication: dict[str, Any],
        readiness: dict[str, Any],
        retry_count: int,
    ) -> dict[str, Any]:
        input_hash = reconciliation.get("input_hash_sha256")
        canonical_output_hash = reconciliation.get("output_hash_sha256")
        output_hash = publication["manifest"].get("payload_sha256")
        upstream_status = str(readiness.get("upstream_status") or "not_recorded")
        reconciliation_status = str(reconciliation.get("status") or "NO_DATA")
        snapshot_status = str(publication["manifest"].get("status") or "degraded")
        readiness_status = str(readiness.get("status") or "not_ready")
        generated_at = _utc_now()
        stages = [
            self._stage_record(
                "source_collection",
                upstream_status,
                "local-eod-stage-v1",
                input_hash=input_hash,
                output_hash=input_hash if upstream_status == "complete" else None,
                retry_count=retry_count,
                generated_at=generated_at,
                next_action=(
                    "Continue to normalization only after the upstream EOD stage records success."
                    if upstream_status != "complete"
                    else "Confirm source lineage and continue with the upstream manifest."
                ),
                warning=(
                    None
                    if upstream_status == "complete"
                    else "upstream stage not recorded as successful"
                ),
            ),
            *[
                self._stage_record(
                    name,
                    "not_recorded",
                    f"dawnstrike-{name}-v1",
                    input_hash=None,
                    output_hash=None,
                    retry_count=retry_count,
                    generated_at=generated_at,
                    next_action="Record and verify this upstream stage before publication.",
                )
                for name in (
                    "candidate_normalization",
                    "selection",
                    "delivery",
                    "paper_fills",
                    "outcome_capture",
                )
            ],
            self._stage_record(
                "paper_reconciliation",
                reconciliation_status,
                "dawnstrike-paper-reconciliation-v1",
                input_hash=input_hash,
                output_hash=canonical_output_hash,
                retry_count=retry_count,
                generated_at=generated_at,
                next_action=(
                    "Resolve reconciliation issues before declaring complete."
                    if reconciliation.get("issue_count", 0)
                    else "Keep the reconciled read model as the source for canonical performance."
                ),
                warning=(
                    f"{reconciliation.get('issue_count', 0)} reconciliation issue(s)"
                    if reconciliation.get("issue_count", 0)
                    else None
                ),
            ),
            self._stage_record(
                "canonical_performance",
                reconciliation_status,
                "dawnstrike-performance-v2",
                input_hash=input_hash,
                output_hash=canonical_output_hash,
                retry_count=retry_count,
                generated_at=generated_at,
                next_action=(
                    "Collect eligible source outcomes and equity observations before publication."
                    if reconciliation_status == "NO_DATA"
                    else "Review any pending or degraded rows before promotion."
                ),
            ),
            self._stage_record(
                "public_snapshot",
                snapshot_status,
                "dawnstrike-public-snapshot-v1",
                input_hash=input_hash,
                output_hash=output_hash,
                retry_count=retry_count,
                generated_at=generated_at,
                next_action=(
                    "Do not promote until the snapshot is complete or explicit no-trade."
                    if snapshot_status not in {"complete", "no_trade"}
                    else "Run artifact and browser verification."
                ),
            ),
            self._stage_record(
                "preview_deployment",
                "not_recorded",
                "vercel-preview-v1",
                input_hash=output_hash,
                output_hash=None,
                retry_count=0,
                generated_at=generated_at,
                next_action="Run Vercel native build and deploy a preview from a clean SHA.",
            ),
            self._stage_record(
                "production_promotion",
                "not_recorded",
                "vercel-promotion-v1",
                input_hash=None,
                output_hash=None,
                retry_count=0,
                generated_at=generated_at,
                next_action="Obtain explicit production approval after preview and rollback proof.",
            ),
            self._stage_record(
                "readiness",
                readiness_status,
                "dawnstrike-readiness-v1",
                input_hash=output_hash,
                output_hash=output_hash,
                retry_count=retry_count,
                generated_at=generated_at,
                next_action=(
                    "Resolve the exact failed checks before treating the publication as ready."
                    if readiness_status != "ready"
                    else "Proceed to browser and deployment verification."
                ),
                warning=readiness.get("reason") if readiness_status != "ready" else None,
            ),
        ]
        payload = {
            "schema_version": "dawnstrike.daily_stage_manifest.v1",
            "generated_at": generated_at,
            "input_hash_sha256": input_hash,
            "output_hash_sha256": output_hash,
            "retry_count": retry_count,
            "stages": stages,
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

    @staticmethod
    def _stage_record(
        name: str,
        domain_status: str,
        stage_version: str,
        *,
        input_hash: object,
        output_hash: object,
        retry_count: int,
        generated_at: str,
        next_action: str,
        warning: object = None,
    ) -> dict[str, Any]:
        status = _stage_status(domain_status)
        return {
            "stage": name,
            "stage_version": stage_version,
            "status": status,
            "domain_status": domain_status,
            "input_hash_sha256": input_hash,
            "output_hash_sha256": output_hash,
            "started_at": generated_at if status != "NOT_STARTED" else None,
            "completed_at": generated_at if status != "NOT_STARTED" else None,
            "attempt_count": retry_count + 1 if status != "NOT_STARTED" else 0,
            "warnings": warning,
            "error": warning if status == "FAILED" else None,
            "next_action": next_action,
        }

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


def _stage_status(domain_status: str) -> str:
    normalized = domain_status.strip().lower()
    if normalized in {"complete", "no_trade", "ready", "passed", "success"}:
        return "LOCAL_VERIFIED"
    if normalized in {"failed", "unreadable", "error"}:
        return "FAILED"
    if normalized in {"not_recorded", "missing", "not_started", "unknown"}:
        return "NOT_STARTED"
    return "IN_PROGRESS"


def _coverage_summary(value: object) -> dict[str, object]:
    daily = value if isinstance(value, list) else []
    eligible = observed = missing = excluded = 0
    for row in daily:
        if not isinstance(row, dict):
            continue
        coverage = row.get("coverage")
        if not isinstance(coverage, dict):
            continue
        eligible += _non_negative_int(coverage.get("eligible_count"))
        observed += _non_negative_int(coverage.get("observed_count"))
        missing += _non_negative_int(coverage.get("missing_count"))
        excluded += _non_negative_int(coverage.get("excluded_count"))
    return {
        "eligible_count": eligible,
        "observed_count": observed,
        "missing_count": missing,
        "excluded_count": excluded,
        "coverage_pct": round(observed / eligible * 100.0, 4) if eligible else None,
    }


def _paper_ops_summary(value: object) -> dict[str, object] | None:
    if not isinstance(value, dict):
        return None
    return {
        key: value.get(key)
        for key in (
            "state",
            "source_row_count",
            "accepted_count",
            "quarantined_count",
            "issue_count",
            "source_return_field_mismatch_count",
            "source_file_sha256",
        )
    }


def _non_negative_int(value: object) -> int:
    try:
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str):
            return max(0, int(value))
        return 0
    except (TypeError, ValueError):
        return 0
