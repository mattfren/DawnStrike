"""One idempotent daily reconcile -> snapshot -> readiness chain."""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.performance.calendar_snapshot import write_public_calendar
from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.performance.snapshot import write_public_snapshot
from intraday_scanner.services.daily_run_service import (
    FAILURE_STATUSES,
    SUCCESS_STATUSES,
    record_daily_stage,
    resolve_release_sha,
    shared_daily_run_id,
    upstream_readiness,
)
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

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
    "public_calendar",
    "preview_deployment",
    "production_promotion",
    "readiness",
)

_NON_BLOCKING_RECONCILIATION_WARNING_CODES = frozenset(
    {
        "missing_outcome",
        "paper_ops_equity_pnl_component_mismatch",
    }
)
_FULL_GIT_SHA = re.compile(r"^[0-9a-f]{40}$")
_LEGACY_RELEASE_SHA_FIELDS = ("release_sha", "source_sha")


class DailyFinalizeService:
    """Finalize one market date and fail readiness if any upstream step is incomplete."""

    def __init__(
        self,
        db_path: str | Path,
        output_root: str | Path = "build/public",
        paper_ops_root: str | Path | None = None,
        runtime_root: str | Path | None = None,
        state_root: str | Path | None = None,
        release_sha: str | None = None,
    ) -> None:
        self.db_path = Path(db_path)
        self.output_root = Path(output_root)
        self.paper_ops_root = Path(paper_ops_root) if paper_ops_root is not None else None
        self.runtime_root = Path(
            runtime_root or Path(__file__).resolve().parents[2]
        ).resolve()
        self.state_root = Path(state_root or self.db_path.parent).resolve()
        self.release_sha = release_sha or resolve_release_sha(self.runtime_root)
        self.lock_path = self.output_root / ".daily-finalize.lock"

    def run(
        self,
        *,
        market_date: str,
        retry_limit: int = 2,
        retry_delay_seconds: int = 0,
        now: str | None = None,
        scenario_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        self.output_root.mkdir(parents=True, exist_ok=True)
        acquired = False
        run_id = shared_daily_run_id(market_date, self.release_sha)
        if self._acquire_lock(run_id):
            acquired = True
        else:
            return self._failure(market_date, run_id, "lock_held", http_status=503)
        try:
            self._log_event("run_started", run_id=run_id, market_date=market_date)
            self._record_run(run_id, market_date, "IN_PROGRESS", "lock_acquired", now=now)
            last_error: str | None = None
            for attempt in range(retry_limit + 1):
                try:
                    # Rebuild the canonical read model from all raw history.
                    # The run date scopes the publication/readiness record; it
                    # must not delete prior canonical days.
                    upstream_status, upstream_stages = self._read_upstream_stages(
                        market_date
                    )
                    result = CanonicalPerformanceService(
                        self.db_path,
                        paper_ops_root=self.paper_ops_root,
                    ).reconcile(
                        as_of_market_date=market_date,
                        persist=True,
                        now=now,
                    )
                    reconciliation_gate = _reconciliation_gate(
                        result,
                        market_date=market_date,
                    )
                    self._record_shared_stage(
                        market_date=market_date,
                        stage_name="canonical_performance",
                        status=(
                            "COMPLETE"
                            if reconciliation_gate["ready"]
                            else _daily_stage_status(result.get("status"))
                        ),
                        exit_code=0 if reconciliation_gate["ready"] else 2,
                        output_hash=str(result.get("output_hash_sha256") or "") or None,
                        source_data_watermark=market_date,
                        payload={
                            "reconciliation_status": result.get("status"),
                            "issue_count": result.get("issue_count"),
                            "row_count": result.get("row_count"),
                            "reconciliation_gate": reconciliation_gate,
                        },
                        observed_at=now,
                    )
                    staging_root = self.output_root / f".publish-{run_id}-{attempt}"
                    staging_root.mkdir(parents=True, exist_ok=True)
                    publication = write_public_snapshot(
                        self.db_path,
                        staging_root / "performance.json",
                        market_date=market_date,
                        generated_at=now,
                    )
                    calendar_publication = write_public_calendar(
                        self.db_path,
                        staging_root / "calendar.json",
                        market_date=market_date,
                        canonical_input_hash_sha256=str(
                            publication["manifest"].get("input_hash_sha256") or ""
                        ),
                        performance_payload_sha256=str(
                            publication["manifest"].get("payload_sha256") or ""
                        ),
                        generated_at=now,
                    )
                    self._record_shared_stage(
                        market_date=market_date,
                        stage_name="calendar_build",
                        status=_daily_stage_status(
                            calendar_publication["manifest"].get("status")
                        ),
                        exit_code=0,
                        output_hash=str(
                            calendar_publication["manifest"].get(
                                "payload_sha256"
                            )
                            or ""
                        )
                        or None,
                        source_data_watermark=market_date,
                        payload=calendar_publication["manifest"],
                        observed_at=now,
                    )
                    scenario_publication = self._stage_scenario_snapshot(
                        staging_root,
                        scenario_payload=scenario_payload,
                    )
                    publication_set = self._promote_publication_pair(
                        staging_root,
                        publication=publication,
                        calendar_publication=calendar_publication,
                        scenario_publication=scenario_publication,
                        generated_at=now,
                    )
                    self._record_shared_stage(
                        market_date=market_date,
                        stage_name="publication",
                        status="COMPLETE",
                        exit_code=0,
                        output_hash=str(
                            publication_set.get("publication_set_sha256") or ""
                        )
                        or None,
                        source_data_watermark=market_date,
                        payload=publication_set,
                        observed_at=now,
                    )
                    readiness = self._write_readiness(
                        result,
                        publication,
                        calendar_publication,
                        publication_set,
                        scenario_publication,
                        market_date,
                        upstream_status,
                        reconciliation_gate=reconciliation_gate,
                        publication_timestamp=now,
                    )
                    readiness_stage_status = (
                        "COMPLETE"
                        if readiness.get("status") == "ready"
                        else "DEGRADED"
                    )
                    daily_snapshot = self._record_shared_stage(
                        market_date=market_date,
                        stage_name="readiness",
                        status=readiness_stage_status,
                        exit_code=(
                            0 if readiness_stage_status == "COMPLETE" else 2
                        ),
                        output_hash=str(
                            readiness.get("publication_set_sha256") or ""
                        )
                        or None,
                        source_data_watermark=market_date,
                        error_code=(
                            None
                            if readiness_stage_status == "COMPLETE"
                            else "daily_readiness_degraded"
                        ),
                        error_detail=(
                            None
                            if readiness_stage_status == "COMPLETE"
                            else str(readiness.get("reason") or "")
                        ),
                        payload=readiness,
                        observed_at=now,
                    )
                    readiness["daily_run"] = _public_daily_run(daily_snapshot)
                    readiness["last_attempted_run"] = (
                        readiness["daily_run"].get("run")
                    )
                    readiness["last_fully_successful_run"] = (
                        readiness["daily_run"].get(
                            "last_fully_successful_run"
                        )
                    )
                    readiness["source_data_watermark"] = market_date
                    _atomic_write_json(
                        self.output_root / "readiness.json",
                        readiness,
                    )
                    stage = self._write_stage_manifest(
                        result,
                        publication,
                        calendar_publication,
                        publication_set,
                        scenario_publication,
                        readiness,
                        attempt,
                        reconciliation_gate=reconciliation_gate,
                        upstream_stages=upstream_stages,
                        generated_at=now,
                    )
                    status = (
                        "COMPLETE"
                        if readiness.get("status") == "ready"
                        else "DEGRADED"
                    )
                    self._record_run(
                        run_id,
                        market_date,
                        status,
                        "published",
                        now=now,
                        retry_count=attempt,
                        input_hash=result.get("input_hash_sha256"),
                        output_hash=publication_set.get("publication_set_sha256"),
                    )
                    self._log_event(
                        "run_completed",
                        run_id=run_id,
                        market_date=market_date,
                        status=status,
                        retry_count=attempt,
                    )
                    return {
                        "run_id": run_id,
                        "market_date": market_date,
                        "status": status,
                        "retry_count": attempt,
                        "readiness": readiness,
                        "stage_manifest": stage,
                        "calendar_publication": calendar_publication["manifest"],
                        "scenario_publication": (
                            scenario_publication["manifest"]
                            if scenario_publication is not None
                            else None
                        ),
                        "publication_set": publication_set,
                        "daily_run": daily_snapshot,
                        "reconciliation": {
                            "row_count": result.get("row_count"),
                            "daily_count": result.get("daily_count"),
                            "issue_count": result.get("issue_count"),
                            "input_hash_sha256": result.get("input_hash_sha256"),
                            "coverage": _coverage_summary(result.get("daily"), market_date),
                            "paper_ops": _paper_ops_summary(
                                result.get("paper_ops_reconciliation")
                            ),
                            "gate": reconciliation_gate,
                        },
                        "upstream_status": upstream_status,
                    }
                except (
                    Exception
                ) as exc:  # pragma: no cover - exercised by failure integration tests
                    last_error = f"{type(exc).__name__}: {exc}"
                    self._cleanup_staging(
                        self.output_root / f".publish-{run_id}-{attempt}"
                    )
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
            self._log_event(
                "run_failed",
                run_id=run_id,
                market_date=market_date,
                reason=last_error or "finalize_failed",
            )
            return self._failure(
                market_date, run_id, last_error or "finalize_failed", http_status=503
            )
        finally:
            if acquired and self.lock_path.exists():
                self.lock_path.unlink()

    def _acquire_lock(self, run_id: str) -> bool:
        """Acquire a bounded lock and recover only a provably stale lock."""

        if self.lock_path.exists():
            age_seconds = max(0.0, time.time() - self.lock_path.stat().st_mtime)
            if age_seconds <= 4 * 60 * 60:
                return False
            self.lock_path.unlink()
        payload = json.dumps(
            {
                "run_id": run_id,
                "pid": os.getpid(),
                "started_at": _utc_now(),
            },
            sort_keys=True,
        )
        try:
            descriptor = os.open(
                self.lock_path,
                os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            )
            with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
                handle.write(payload)
        except FileExistsError:
            return False
        return True

    def _log_event(self, event: str, **payload: object) -> None:
        record = {"event": event, "at": _utc_now(), **payload}
        with (self.output_root / "daily-finalize.jsonl").open(
            "a", encoding="utf-8"
        ) as handle:
            handle.write(json.dumps(record, sort_keys=True, default=str) + "\n")

    @staticmethod
    def _stage_scenario_snapshot(
        staging_root: Path,
        *,
        scenario_payload: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Write the sanitized scenario projection into the same staged publication set."""

        if scenario_payload is None:
            return None
        snapshot_path = staging_root / "scenarios.json"
        _atomic_write_json(snapshot_path, scenario_payload)
        manifest = {
            "schema_version": "dawnstrike-scenarios-public-manifest-v1",
            "generated_at": scenario_payload.get("generated_at"),
            "payload_sha256": hashlib.sha256(snapshot_path.read_bytes()).hexdigest(),
            "record_count": len(scenario_payload.get("records") or []),
            "performance_day_count": len(scenario_payload.get("performance") or []),
            "calibration_status": scenario_payload.get("calibration_status") or "UNCALIBRATED",
            "research_only": True,
        }
        manifest_path = staging_root / "scenarios.json.manifest.json"
        _atomic_write_json(manifest_path, manifest)
        return {
            "snapshot_path": str(snapshot_path),
            "manifest_path": str(manifest_path),
            "manifest": manifest,
        }

    def _promote_publication_pair(
        self,
        staging_root: Path,
        *,
        publication: dict[str, Any],
        calendar_publication: dict[str, Any],
        scenario_publication: dict[str, Any] | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        """Promote one staged performance/calendar generation as a bound set."""

        performance_manifest = publication["manifest"]
        calendar_manifest = calendar_publication["manifest"]
        canonical_hash = str(performance_manifest.get("input_hash_sha256") or "")
        if (
            not canonical_hash
            or calendar_manifest.get("canonical_input_hash_sha256") != canonical_hash
            or calendar_manifest.get("performance_payload_sha256")
            != performance_manifest.get("payload_sha256")
        ):
            raise ValueError("performance/calendar publication hashes are not bound")
        staged = {
            "performance.json": Path(publication["snapshot_path"]),
            "performance.json.manifest.json": Path(publication["manifest_path"]),
            "calendar.json": Path(calendar_publication["calendar_path"]),
            "calendar.json.manifest.json": Path(calendar_publication["manifest_path"]),
        }
        if scenario_publication is not None:
            staged.update(
                {
                    "scenarios.json": Path(scenario_publication["snapshot_path"]),
                    "scenarios.json.manifest.json": Path(
                        scenario_publication["manifest_path"]
                    ),
                }
            )
        if any(not path.is_file() or path.parent != staging_root for path in staged.values()):
            raise ValueError("publication pair is not fully staged")
        destination = self.output_root / "data"
        destination.mkdir(parents=True, exist_ok=True)
        for name, source in staged.items():
            source.replace(destination / name)
        publication["snapshot_path"] = str(destination / "performance.json")
        publication["manifest_path"] = str(
            destination / "performance.json.manifest.json"
        )
        calendar_publication["calendar_path"] = str(destination / "calendar.json")
        calendar_publication["manifest_path"] = str(
            destination / "calendar.json.manifest.json"
        )
        if scenario_publication is not None:
            scenario_publication["snapshot_path"] = str(destination / "scenarios.json")
            scenario_publication["manifest_path"] = str(
                destination / "scenarios.json.manifest.json"
            )
        publication_set = {
            "schema_version": "dawnstrike.publication_set.v2",
            "market_date": performance_manifest.get("market_date"),
            "canonical_input_hash_sha256": canonical_hash,
            "performance_payload_sha256": performance_manifest.get("payload_sha256"),
            "calendar_payload_sha256": calendar_manifest.get("payload_sha256"),
            "performance_manifest_id": performance_manifest.get("manifest_id"),
            "calendar_manifest_id": calendar_manifest.get("manifest_id"),
            "generated_at": generated_at or _utc_now(),
            "research_only": True,
            "live_trading_enabled": False,
        }
        scenario_manifest: dict[str, Any] | None = None
        if scenario_publication is not None:
            scenario_manifest = scenario_publication["manifest"]
            publication_set["scenario_payload_sha256"] = scenario_manifest.get(
                "payload_sha256"
            )
            publication_set["scenario_manifest_sha256"] = hashlib.sha256(
                Path(scenario_publication["manifest_path"]).read_bytes()
            ).hexdigest()
        publication_set["publication_set_sha256"] = _publication_pair_hash(
            performance_manifest,
            calendar_manifest,
            scenario_manifest,
        )
        _atomic_write_json(destination / "publication-set.json", publication_set)
        staging_root.rmdir()
        return publication_set

    @staticmethod
    def _cleanup_staging(staging_root: Path) -> None:
        if not staging_root.is_dir():
            return
        for name in (
            "performance.json",
            "performance.json.manifest.json",
            "calendar.json",
            "calendar.json.manifest.json",
            "scenarios.json",
            "scenarios.json.manifest.json",
        ):
            path = staging_root / name
            if path.is_file():
                path.unlink()
        try:
            staging_root.rmdir()
        except OSError:
            pass

    def _write_readiness(
        self,
        reconciliation: dict[str, Any],
        publication: dict[str, Any],
        calendar_publication: dict[str, Any],
        publication_set: dict[str, Any],
        scenario_publication: dict[str, Any] | None,
        market_date: str,
        upstream_status: str = "not_recorded",
        reconciliation_gate: dict[str, Any] | None = None,
        publication_timestamp: str | None = None,
    ) -> dict[str, Any]:
        snapshot_status = str(publication["manifest"].get("status") or "degraded")
        calendar_status = str(
            calendar_publication["manifest"].get("status") or "degraded"
        )
        calendar_freshness = calendar_publication["manifest"].get("freshness")
        if not isinstance(calendar_freshness, dict):
            calendar_freshness = {
                "schema_version": "dawnstrike.calendar_freshness.v1",
                "status": "unknown",
                "fail_closed": True,
            }
        freshness_ready = calendar_freshness.get("status") == "current"
        safety_evidence = publication["manifest"].get("safety_evidence")
        safety_verified = _safety_is_verified(safety_evidence)
        gate = reconciliation_gate or {
            "ready": False,
            "status": "blocked",
            "warnings": [],
            "blocking": ["reconciliation gate was not evaluated"],
        }
        status = (
            "ready"
            if (
                snapshot_status in {"complete", "no_trade"}
                and calendar_status in {"complete", "no_trade"}
                and freshness_ready
                and upstream_status == "complete"
                and safety_verified
                and gate.get("ready") is True
            )
            else "not_ready"
        )
        payload = {
            "schema_version": "dawnstrike.readiness.v1",
            "market_date": market_date,
            "status": status,
            "http_status": 200 if status == "ready" else 503,
            "snapshot_status": snapshot_status,
            "calendar_status": calendar_status,
            "upstream_status": upstream_status,
            "safety_status": "verified" if safety_verified else "blocked_or_unknown",
            "reconciliation_status": reconciliation.get("status"),
            "reconciliation_gate": gate,
            "input_hash_sha256": reconciliation.get("input_hash_sha256"),
            "payload_sha256": publication["manifest"].get("payload_sha256"),
            "calendar_payload_sha256": calendar_publication["manifest"].get(
                "payload_sha256"
            ),
            "calendar_freshness": calendar_freshness,
            "authoritative_as_of_market_date": calendar_freshness.get(
                "authoritative_as_of_market_date"
            ),
            "next_publication_at": calendar_freshness.get("next_publication_at"),
            "stale_after": calendar_freshness.get("next_stale_after"),
            "publication_set_sha256": publication_set.get("publication_set_sha256"),
            "source_data_watermark": market_date,
            "outcome_coverage": _coverage_summary(
                reconciliation.get("daily"),
                market_date,
            ),
            "publication_timestamp": publication_timestamp or _utc_now(),
            "live_trading_enabled": False,
            "research_only": True,
            "reason": "complete_or_explicit_no_trade_with_upstream_success_and_safety"
            if status == "ready"
            else "missing_or_degraded_upstream_truth_or_safety",
        }
        if scenario_publication is not None:
            scenario_manifest = scenario_publication["manifest"]
            payload["scenario_intelligence"] = {
                "record_count": scenario_manifest.get("record_count"),
                "performance_day_count": scenario_manifest.get("performance_day_count"),
                "calibration_status": scenario_manifest.get("calibration_status"),
                "payload_sha256": scenario_manifest.get("payload_sha256"),
                "research_only": True,
            }
        path = self.output_root / "readiness.json"
        _atomic_write_json(path, payload)
        return payload

    def _record_shared_stage(
        self,
        *,
        market_date: str,
        stage_name: str,
        status: str,
        exit_code: int,
        output_hash: str | None = None,
        source_data_watermark: str | None = None,
        error_code: str | None = None,
        error_detail: str | None = None,
        payload: dict[str, Any] | None = None,
        observed_at: str | None = None,
    ) -> dict[str, Any]:
        return record_daily_stage(
            db_path=self.db_path,
            market_date=market_date,
            stage_name=stage_name,
            status=status,
            runtime_root=self.runtime_root,
            state_root=self.state_root,
            release_sha=self.release_sha,
            exit_code=exit_code,
            output_hash_sha256=output_hash,
            source_data_watermark=source_data_watermark,
            error_code=error_code,
            error_detail=error_detail,
            payload=payload,
            observed_at=observed_at,
        )

    def _read_upstream_stage(self, market_date: str) -> str:
        """Read the latest local EOD automation result without mutating it."""

        return self._read_upstream_stages(market_date)[0]

    def _read_upstream_stages(
        self, market_date: str
    ) -> tuple[str, dict[str, str]]:
        """Read one dated upstream receipt and its stage statuses.

        The receipt is written by the owned AlphaOps runner after the upstream
        chain exits.  Absence is intentionally not inferred as success.
        """

        store = SQLiteScanStore(self.db_path)
        store.initialize()
        shared_id = shared_daily_run_id(market_date, self.release_sha)
        shared_rows = store.load_daily_run_stages(
            run_id=shared_id,
            limit=10_000,
        )
        upstream_names = {
            "morning_collection",
            "ranking_delivery",
            "intraday_monitor",
            "eod_outcome_capture",
            "paper_reconciliation",
        }
        if any(
            str(row.get("stage_name") or "") in upstream_names
            for row in shared_rows
        ):
            shared = upstream_readiness(store, run_id=shared_id)
            latest = _latest_shared_stage_statuses(shared_rows)
            legacy_map = {
                "source_collection": "morning_collection",
                "candidate_normalization": "morning_collection",
                "selection": "ranking_delivery",
                "delivery": "ranking_delivery",
                "paper_fills": "intraday_monitor",
                "outcome_capture": "eod_outcome_capture",
            }
            stage_map = {
                legacy_name: _legacy_stage_status(
                    latest.get(shared_name)
                )
                for legacy_name, shared_name in legacy_map.items()
            }
            return (
                "complete" if shared.get("ready") is True else "failed",
                stage_map,
            )
        try:
            with sqlite3.connect(self.db_path) as connection:
                exists = connection.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name='automation_runs'"
                ).fetchone()
                if not exists:
                    return "not_recorded", {}
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
            return "unreadable", {}
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
            if _legacy_receipt_release_sha(payload) != self.release_sha:
                return "failed", {}
            normalized = str(status or payload.get("status") or "").lower()
            stages = payload.get("stages")
            stage_map = {
                str(item.get("stage")): str(item.get("status") or "")
                for item in stages
                if isinstance(item, dict) and item.get("stage")
            } if isinstance(stages, list) else {}
            if normalized in {"complete", "completed", "success", "ok", "passed", "ready"}:
                return "complete", stage_map
            return "failed", stage_map
        return "not_recorded", {}

    def _write_stage_manifest(
        self,
        reconciliation: dict[str, Any],
        publication: dict[str, Any],
        calendar_publication: dict[str, Any],
        publication_set: dict[str, Any],
        scenario_publication: dict[str, Any] | None,
        readiness: dict[str, Any],
        retry_count: int,
        reconciliation_gate: dict[str, Any] | None = None,
        upstream_stages: dict[str, str] | None = None,
        generated_at: str | None = None,
    ) -> dict[str, Any]:
        input_hash = reconciliation.get("input_hash_sha256")
        canonical_output_hash = reconciliation.get("output_hash_sha256")
        performance_output_hash = publication["manifest"].get("payload_sha256")
        calendar_output_hash = calendar_publication["manifest"].get(
            "payload_sha256"
        )
        output_hash = publication_set.get("publication_set_sha256")
        upstream_status = str(readiness.get("upstream_status") or "not_recorded")
        raw_reconciliation_status = str(reconciliation.get("status") or "NO_DATA")
        gate = reconciliation_gate or {}
        reconciliation_status = (
            str(gate.get("status") or "")
            if gate.get("ready") is True
            else raw_reconciliation_status
        )
        snapshot_status = str(publication["manifest"].get("status") or "degraded")
        calendar_status = str(
            calendar_publication["manifest"].get("status") or "degraded"
        )
        readiness_status = str(readiness.get("status") or "not_ready")
        generated_at = generated_at or _utc_now()
        upstream_stages = upstream_stages or {}
        upstream_stage_names = {
            "source_collection",
            "candidate_normalization",
            "selection",
            "delivery",
            "paper_fills",
            "outcome_capture",
        }
        stages = [
            self._stage_record(
                "source_collection",
                upstream_stages.get("source_collection", upstream_status),
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
                    upstream_stages.get(name, upstream_status)
                    if name in upstream_stage_names
                    else "not_recorded",
                    f"dawnstrike-{name}-v1",
                    input_hash=None,
                    output_hash=None,
                    retry_count=retry_count,
                    generated_at=generated_at,
                    next_action=(
                        "Record and verify this upstream stage before publication."
                        if name not in upstream_stages
                        else "Review the upstream receipt and preserve its source lineage."
                    ),
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
                    if gate.get("ready") is not True
                    else "Keep the reconciled read model as the source for canonical performance."
                ),
                warning=(
                    "; ".join(str(item) for item in gate.get("warnings") or [])
                    if gate.get("warnings")
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
                    if raw_reconciliation_status == "NO_DATA"
                    else "Review any pending or degraded rows before promotion."
                ),
            ),
            self._stage_record(
                "public_snapshot",
                snapshot_status,
                "dawnstrike-public-snapshot-v1",
                input_hash=input_hash,
                output_hash=performance_output_hash,
                retry_count=retry_count,
                generated_at=generated_at,
                next_action=(
                    "Do not promote until the snapshot is complete or explicit no-trade."
                    if snapshot_status not in {"complete", "no_trade"}
                    else "Run artifact and browser verification."
                ),
            ),
            self._stage_record(
                "public_calendar",
                calendar_status,
                "dawnstrike-public-calendar-v1",
                input_hash=publication["manifest"].get("input_hash_sha256"),
                output_hash=calendar_output_hash,
                retry_count=retry_count,
                generated_at=generated_at,
                next_action=(
                    "Do not promote until Calendar is complete or explicit no-trade."
                    if calendar_status not in {"complete", "no_trade"}
                    else "Verify Calendar values match the canonical snapshot."
                ),
            ),
            *(
                [
                    self._stage_record(
                        "scenario_snapshot",
                        "complete",
                        "dawnstrike-scenarios-public-v1",
                        input_hash=input_hash,
                        output_hash=scenario_publication["manifest"].get(
                            "payload_sha256"
                        ),
                        retry_count=retry_count,
                        generated_at=generated_at,
                        next_action=(
                            "Keep Scenario cohorts and calibration disclosure separate "
                            "from official paper performance."
                        ),
                    )
                ]
                if scenario_publication is not None
                else []
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
                "data/calendar.json",
                "data/calendar.json.manifest.json",
                "data/publication-set.json",
                "data/v6-learning.json",
                *(
                    ["data/scenarios.json", "data/scenarios.json.manifest.json"]
                    if scenario_publication is not None
                    else []
                ),
                "readiness.json",
            ],
            "research_only": True,
            "live_trading_enabled": False,
        }
        _atomic_write_json(self.output_root / "stage-manifest.json", payload)
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
                    (now or _utc_now()) if status not in {"IN_PROGRESS"} else None,
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
        _atomic_write_json(self.output_root / "readiness.json", payload["readiness"])
        return payload


def _atomic_write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    temporary.replace(path)


def _publication_pair_hash(
    performance_manifest: dict[str, Any],
    calendar_manifest: dict[str, Any],
    scenario_manifest: dict[str, Any] | None = None,
) -> str:
    payload = {
        "market_date": performance_manifest.get("market_date"),
        "canonical_input_hash_sha256": performance_manifest.get(
            "input_hash_sha256"
        ),
        "performance_payload_sha256": performance_manifest.get("payload_sha256"),
        "calendar_payload_sha256": calendar_manifest.get("payload_sha256"),
        "performance_manifest_id": performance_manifest.get("manifest_id"),
        "calendar_manifest_id": calendar_manifest.get("manifest_id"),
    }
    if scenario_manifest is not None:
        payload["scenario_payload_sha256"] = scenario_manifest.get("payload_sha256")
        payload["scenario_schema_version"] = scenario_manifest.get("schema_version")
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _stage_status(domain_status: str) -> str:
    normalized = domain_status.strip().lower()
    if normalized in {
        "complete",
        "complete_with_warnings",
        "no_trade",
        "ready",
        "ready_with_warnings",
        "passed",
        "success",
    }:
        return "LOCAL_VERIFIED"
    if normalized in {"failed", "unreadable", "error"}:
        return "FAILED"
    if normalized in {"partial", "degraded", "no_data"}:
        return "DEGRADED"
    if normalized in {"not_recorded", "missing", "not_started", "unknown"}:
        return "NOT_STARTED"
    return "IN_PROGRESS"


def _daily_stage_status(value: object) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {
        "complete",
        "completed",
        "no_trade",
        "ready",
        "success",
    }:
        return "COMPLETE" if normalized != "no_trade" else "NO_TRADE"
    return "DEGRADED"


def _latest_shared_stage_statuses(
    rows: list[dict[str, Any]],
) -> dict[str, str]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        stage = str(row.get("stage_name") or "")
        prior = latest.get(stage)
        if prior is None or int(row.get("attempt_no") or 0) >= int(
            prior.get("attempt_no") or 0
        ):
            latest[stage] = row
    return {
        stage: str(row.get("status") or "")
        for stage, row in latest.items()
    }


def _legacy_stage_status(value: str | None) -> str:
    if value in SUCCESS_STATUSES:
        return "complete"
    if value in FAILURE_STATUSES:
        return "failed"
    return "not_recorded"


def _legacy_receipt_release_sha(payload: dict[str, Any]) -> str | None:
    """Resolve the one explicit SHA identity accepted from legacy receipts."""

    declared = [
        payload[field]
        for field in _LEGACY_RELEASE_SHA_FIELDS
        if field in payload
    ]
    if not declared or any(
        not isinstance(value, str) or _FULL_GIT_SHA.fullmatch(value) is None
        for value in declared
    ):
        return None
    if len(set(declared)) != 1:
        return None
    return declared[0]


def _public_daily_run(snapshot: dict[str, Any]) -> dict[str, Any]:
    run = snapshot.get("run")
    last_success = snapshot.get("last_fully_successful_run")
    run_fields = (
        "run_id",
        "market_date",
        "release_sha",
        "scheduler_version",
        "status",
        "current_stage",
        "started_at",
        "completed_at",
        "last_attempted_at",
        "failed_stage",
        "failure_reason",
        "source_data_watermark",
        "publication_timestamp",
        "deployed_source_sha",
        "deployed_build_sha",
        "research_only",
        "broker_execution_enabled",
    )

    def select(value: object) -> dict[str, Any] | None:
        if not isinstance(value, dict):
            return None
        return {field: value.get(field) for field in run_fields}

    return {
        "run": select(run),
        "latest_stage_statuses": snapshot.get(
            "latest_stage_statuses",
            {},
        ),
        "upstream": snapshot.get("upstream"),
        "last_fully_successful_run": select(last_success),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _reconciliation_gate(
    reconciliation: dict[str, Any],
    *,
    market_date: str,
) -> dict[str, Any]:
    """Allow only declared, non-current warning classes to pass publication."""

    blocking: list[str] = []
    warning_counts: dict[str, int] = {}
    raw_issues = reconciliation.get("issues")
    issues = raw_issues if isinstance(raw_issues, list) else []
    if int(reconciliation.get("issue_count") or 0) != len(issues):
        blocking.append("reconciliation issue inventory count mismatch")
    for issue in issues:
        if not isinstance(issue, dict):
            blocking.append("reconciliation contains a malformed issue")
            continue
        code = str(issue.get("issue_code") or "").strip()
        severity = str(issue.get("severity") or "").strip().lower()
        issue_date = str(issue.get("market_date") or "")[:10]
        allowed = (
            severity == "warning"
            and code in _NON_BLOCKING_RECONCILIATION_WARNING_CODES
            and (
                code == "paper_ops_equity_pnl_component_mismatch"
                or (code == "missing_outcome" and issue_date < market_date)
            )
        )
        if allowed:
            warning_counts[code] = warning_counts.get(code, 0) + 1
        else:
            blocking.append(
                f"blocking reconciliation issue {code or 'unknown'} "
                f"for {issue_date or 'unknown-date'}"
            )

    paper_ops = reconciliation.get("paper_ops_reconciliation")
    if not isinstance(paper_ops, dict):
        blocking.append("PaperOps reconciliation inventory is missing")
    else:
        if str(paper_ops.get("state") or "") not in {"complete", "partial"}:
            blocking.append("PaperOps reconciliation state is not publishable")
        if int(paper_ops.get("quarantined_count") or 0) != 0:
            blocking.append("PaperOps contains quarantined rows")
        if int(paper_ops.get("source_return_field_mismatch_count") or 0) != 0:
            blocking.append("PaperOps source return fields do not reconcile")

    current_official = [
        row
        for row in reconciliation.get("daily") or []
        if isinstance(row, dict)
        and str(row.get("market_date") or "") == market_date
        and str(row.get("cohort") or "") == "official_forward_paper"
    ]
    if not current_official:
        blocking.append("current official forward-paper performance is missing")
    for row in current_official:
        status = str(row.get("status") or "").upper()
        raw_coverage = row.get("coverage")
        coverage = raw_coverage if isinstance(raw_coverage, dict) else {}
        if status not in {"COMPLETE", "NO_TRADE"}:
            blocking.append(
                f"current official strategy {row.get('strategy_id') or 'unknown'} "
                f"is {status or 'UNKNOWN'}"
            )
        if int(row.get("missing_outcome_count") or 0) != 0:
            blocking.append("current official performance has a missing outcome")
        if int(row.get("quarantined_count") or 0) != 0:
            blocking.append("current official performance has quarantined evidence")
        if int(coverage.get("missing_count") or 0) != 0:
            blocking.append("current official coverage is incomplete")
        if status == "NO_TRADE":
            try:
                return_pct = float(str(row.get("return_pct")))
            except (TypeError, ValueError):
                return_pct = None
            if return_pct != 0.0 or int(row.get("no_trade_count") or 0) < 1:
                blocking.append("current official NO_TRADE row is not an observed zero")

    warnings = [
        f"{count} {code} warning(s) retained with missing truth kept null"
        if code == "missing_outcome"
        else f"{count} {code} warning(s) retained after daily equity identity passed"
        for code, count in sorted(warning_counts.items())
    ]
    unique_blocking = sorted(set(blocking))
    ready = not unique_blocking
    return {
        "status": (
            "ready_with_warnings"
            if ready and warnings
            else "complete" if ready else "blocked"
        ),
        "ready": ready,
        "warning_codes": sorted(warning_counts),
        "warning_count": sum(warning_counts.values()),
        "warnings": warnings,
        "blocking": unique_blocking,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _coverage_summary(value: object, market_date: str | None = None) -> dict[str, object]:
    daily = value if isinstance(value, list) else []
    eligible = observed = missing = excluded = 0
    for row in daily:
        if not isinstance(row, dict):
            continue
        if market_date and str(row.get("market_date") or "") != market_date:
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


def _safety_is_verified(value: object) -> bool:
    if not isinstance(value, dict) or not value:
        return False
    required = ("source_quality", "halt_status", "corporate_action_status", "liquidity_evidence")
    return all(
        isinstance(value.get(key), dict)
        and value[key].get("state") == "verified"
        for key in required
    )


def _non_negative_int(value: object) -> int:
    try:
        if isinstance(value, int):
            return max(0, value)
        if isinstance(value, str):
            return max(0, int(value))
        return 0
    except (TypeError, ValueError):
        return 0
