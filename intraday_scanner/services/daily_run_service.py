"""Shared release-bound run and stage ledger for Dawnstrike's daily DAG."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_STRATEGY_VERSION,
)
from intraday_scanner.alpha.v6_shadow import ALPHAOPS_V6_STRATEGY_VERSION
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _active_strategy_versions(*, paperops_version: str) -> dict[str, str]:
    """Return only strategies that can participate in the current release."""

    return {
        "alphaops_v5": ALPHAOPS_V5_STRATEGY_VERSION,
        "alphaops_v6_shadow": ALPHAOPS_V6_STRATEGY_VERSION,
        "paperops": paperops_version,
    }

DAILY_RUN_SCHEMA = "dawnstrike.daily_run.v1"
SCHEDULER_VERSION = "dawnstrike-scheduler-v6"
DAILY_STAGE_ORDER = (
    "morning_collection",
    "ranking_delivery",
    "indeterminate_research",
    "intraday_monitor",
    "scenario_intelligence",
    "scenario_finalization",
    "eod_outcome_capture",
    "paper_reconciliation",
    "alpha_learning",
    "paperops_forward",
    "canonical_performance",
    "calendar_build",
    "publication",
    "readiness",
)
REQUIRED_UPSTREAM_STAGES = (
    "morning_collection",
    "ranking_delivery",
    "intraday_monitor",
    "eod_outcome_capture",
    "paper_reconciliation",
)
REQUIRED_FULL_CHAIN_STAGES = (
    *REQUIRED_UPSTREAM_STAGES,
    "canonical_performance",
    "calendar_build",
    "publication",
    "readiness",
)
SUCCESS_STATUSES = frozenset({
    "COMPLETE",
    "NO_TRADE",
    "SKIPPED_NOT_APPLICABLE",
})
FAILURE_STATUSES = frozenset({"FAILED", "DEGRADED", "TERMINAL_MISSING"})


def shared_daily_run_id(market_date: str, release_sha: str) -> str:
    # A V6 release starts a new release-bound ledger.  V4/V5 rows retain their
    # original identities and remain immutable historical evidence.
    value = f"dawnstrike:daily:v6:{market_date[:10]}:{release_sha}"
    return "daily-" + hashlib.sha256(value.encode()).hexdigest()[:24]


def resolve_release_sha(runtime_root: str | Path) -> str:
    configured = os.environ.get("DAWNSTRIKE_RELEASE_SHA", "").strip()
    if configured:
        return configured
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=Path(runtime_root),
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError) as exc:
        raise ValueError(
            "A release SHA is required for the shared daily run ledger."
        ) from exc


def record_daily_stage(
    *,
    db_path: str | Path,
    market_date: str,
    stage_name: str,
    status: str,
    runtime_root: str | Path,
    state_root: str | Path,
    release_sha: str | None = None,
    required: bool = True,
    started_at: str | None = None,
    completed_at: str | None = None,
    exit_code: int | None = None,
    input_hash_sha256: str | None = None,
    output_hash_sha256: str | None = None,
    source_data_watermark: str | None = None,
    error_code: str | None = None,
    error_detail: str | None = None,
    payload: dict[str, Any] | None = None,
    observed_at: str | None = None,
) -> dict[str, Any]:
    if stage_name not in DAILY_STAGE_ORDER:
        raise ValueError(f"Unsupported daily stage: {stage_name}")
    normalized_status = status.strip().upper()
    if normalized_status not in SUCCESS_STATUSES | FAILURE_STATUSES | {"IN_PROGRESS"}:
        raise ValueError(f"Unsupported daily stage status: {status}")
    root = Path(runtime_root).resolve()
    durable_state = Path(state_root).resolve()
    sha = release_sha or resolve_release_sha(root)
    if not sha:
        raise ValueError("release_sha must not be blank")
    now = observed_at or _utc_now()
    start = started_at or now
    complete = completed_at or (None if normalized_status == "IN_PROGRESS" else now)
    run_id = shared_daily_run_id(market_date, sha)
    store = SQLiteScanStore(db_path)
    store.initialize()
    prior_runs = store.load_daily_runs(market_date=market_date, limit=100)
    prior = next(
        (row for row in prior_runs if str(row.get("run_id") or "") == run_id),
        {},
    )
    if not prior:
        initial_run: dict[str, Any] = {
            "run_id": run_id,
            "market_date": market_date[:10],
            "release_sha": sha,
            "runtime_root": str(root),
            "state_root": str(durable_state),
            "scheduler_version": SCHEDULER_VERSION,
            "strategy_versions": _active_strategy_versions(
                paperops_version="registered-strategy-manifest"
            ),
            "status": "IN_PROGRESS",
            "current_stage": stage_name,
            "started_at": start,
            "completed_at": None,
            "last_attempted_at": now,
            "failed_stage": None,
            "failure_reason": None,
            "source_data_watermark": source_data_watermark,
            "publication_timestamp": None,
            "deployed_source_sha": None,
            "deployed_build_sha": None,
            "schema_version": DAILY_RUN_SCHEMA,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        initial_run["payload_json"] = dict(initial_run)
        store.upsert_daily_run(initial_run)
        prior = initial_run
    existing_stages = store.load_daily_run_stages(
        run_id=run_id,
        stage_name=stage_name,
        limit=10_000,
    )
    attempt_no = max(
        (int(row.get("attempt_no") or 0) for row in existing_stages),
        default=0,
    ) + 1
    stage_event_id = _stage_event_id(run_id, stage_name, attempt_no)
    stage_row: dict[str, Any] = {
        "stage_event_id": stage_event_id,
        "run_id": run_id,
        "stage_name": stage_name,
        "attempt_no": attempt_no,
        "status": normalized_status,
        "required": required,
        "started_at": start,
        "completed_at": complete,
        "exit_code": exit_code,
        "input_hash_sha256": input_hash_sha256,
        "output_hash_sha256": output_hash_sha256,
        "source_data_watermark": source_data_watermark,
        "error_code": error_code,
        "error_detail": error_detail,
        "payload": dict(payload or {}),
        "schema_version": DAILY_RUN_SCHEMA,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    stage_row["payload_json"] = dict(stage_row)
    store.persist_daily_run_stage(stage_row)
    all_stages = store.load_daily_run_stages(run_id=run_id, limit=10_000)
    run_status = _run_status(all_stages)
    failed_stage, failure_reason = _latest_failure(all_stages)
    run_row: dict[str, Any] = {
        "run_id": run_id,
        "market_date": market_date[:10],
        "release_sha": sha,
        "runtime_root": str(root),
        "state_root": str(durable_state),
        "scheduler_version": SCHEDULER_VERSION,
        "strategy_versions": _active_strategy_versions(
            paperops_version="registered-strategy-manifest"
        ),
        "status": run_status,
        "current_stage": stage_name,
        "started_at": str(prior.get("started_at") or start),
        "completed_at": now if run_status == "COMPLETE" else None,
        "last_attempted_at": now,
        "failed_stage": failed_stage,
        "failure_reason": failure_reason,
        "source_data_watermark": (
            source_data_watermark
            or prior.get("source_data_watermark")
            or None
        ),
        "publication_timestamp": (
            complete
            if stage_name == "publication" and normalized_status in SUCCESS_STATUSES
            else prior.get("publication_timestamp")
        ),
        "deployed_source_sha": (
            (payload or {}).get("source_sha")
            if stage_name == "publication"
            else None
        )
        or prior.get("deployed_source_sha"),
        "deployed_build_sha": (
            (payload or {}).get("build_id")
            if stage_name == "publication"
            else None
        )
        or prior.get("deployed_build_sha"),
        "schema_version": DAILY_RUN_SCHEMA,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    run_row["payload_json"] = {
        **run_row,
        "latest_stage_statuses": _latest_stage_statuses(all_stages),
    }
    store.upsert_daily_run(run_row)
    return daily_run_snapshot(store, run_id)


def upstream_readiness(
    store: SQLiteScanStore,
    *,
    run_id: str,
    required_stages: tuple[str, ...] = REQUIRED_UPSTREAM_STAGES,
) -> dict[str, Any]:
    stages = store.load_daily_run_stages(run_id=run_id, limit=10_000)
    latest = _latest_by_stage(stages)
    missing = [stage for stage in required_stages if stage not in latest]
    failed = [
        {
            "stage": stage,
            "status": latest[stage].get("status"),
            "reason": latest[stage].get("error_detail")
            or latest[stage].get("error_code"),
        }
        for stage in required_stages
        if stage in latest
        and str(latest[stage].get("status") or "") not in SUCCESS_STATUSES
    ]
    return {
        "status": "READY" if not missing and not failed else "DEGRADED",
        "run_id": run_id,
        "required_stages": list(required_stages),
        "missing_stages": missing,
        "failed_stages": failed,
        "ready": not missing and not failed,
    }


def daily_run_snapshot(
    store: SQLiteScanStore,
    run_id: str,
) -> dict[str, Any]:
    run = next(
        (
            row
            for row in store.load_daily_runs(limit=1_000)
            if str(row.get("run_id") or "") == run_id
        ),
        None,
    )
    stages = store.load_daily_run_stages(run_id=run_id, limit=10_000)
    last_success = next(
        iter(store.load_daily_runs(status="COMPLETE", limit=1)),
        None,
    )
    return {
        "run": run,
        "stages": stages,
        "latest_stage_statuses": _latest_stage_statuses(stages),
        "upstream": upstream_readiness(store, run_id=run_id),
        "last_fully_successful_run": last_success,
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def latest_daily_run_snapshot(
    *,
    db_path: str | Path,
) -> dict[str, Any]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    latest = next(iter(store.load_daily_runs(limit=1)), None)
    if latest is None:
        return {
            "run": None,
            "stages": [],
            "latest_stage_statuses": {},
            "upstream": None,
            "last_fully_successful_run": None,
            "missing_truth_is_zero": False,
        }
    return daily_run_snapshot(store, str(latest.get("run_id") or ""))


def _run_status(stages: list[dict[str, Any]]) -> str:
    latest = _latest_by_stage(stages)
    if any(
        row.get("required") is True
        and str(row.get("status") or "") in FAILURE_STATUSES
        for row in latest.values()
    ):
        return "DEGRADED"
    if all(
        stage in latest
        and str(latest[stage].get("status") or "") in SUCCESS_STATUSES
        for stage in REQUIRED_FULL_CHAIN_STAGES
    ):
        return "COMPLETE"
    return "IN_PROGRESS"


def _latest_failure(
    stages: list[dict[str, Any]],
) -> tuple[str | None, str | None]:
    latest = _latest_by_stage(stages)
    for stage_name in DAILY_STAGE_ORDER:
        row = latest.get(stage_name)
        if (
            row is not None
            and row.get("required") is True
            and str(row.get("status") or "") in FAILURE_STATUSES
        ):
            return (
                stage_name,
                str(
                    row.get("error_detail")
                    or row.get("error_code")
                    or ""
                ),
            )
    return None, None


def _latest_stage_statuses(
    stages: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    return {
        stage: {
            "status": row.get("status"),
            "attempt_no": row.get("attempt_no"),
            "started_at": row.get("started_at"),
            "completed_at": row.get("completed_at"),
            "exit_code": row.get("exit_code"),
            "error_code": row.get("error_code"),
            "error_detail": row.get("error_detail"),
            "input_hash_sha256": row.get("input_hash_sha256"),
            "output_hash_sha256": row.get("output_hash_sha256"),
        }
        for stage, row in _latest_by_stage(stages).items()
    }


def _latest_by_stage(
    stages: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in stages:
        stage = str(row.get("stage_name") or "")
        previous = latest.get(stage)
        if previous is None or int(row.get("attempt_no") or 0) >= int(
            previous.get("attempt_no") or 0
        ):
            latest[stage] = row
    return latest


def _stage_event_id(run_id: str, stage_name: str, attempt_no: int) -> str:
    return "stage-" + hashlib.sha256(
        f"{run_id}:{stage_name}:{attempt_no}".encode()
    ).hexdigest()[:28]


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def release_manifest_payload(
    *,
    source_sha: str,
    build_sha: str,
    runtime_root: str | Path,
    state_root: str | Path,
    schema_version: int,
    data_watermark: str | None,
    artifact_hashes: dict[str, str],
) -> dict[str, Any]:
    # This manifest is published with the static site.  The runtime and state
    # paths remain in the private daily ledger, but publishing those absolute
    # paths creates needless host topology disclosure.
    runtime = Path(runtime_root).resolve()
    durable_state = Path(state_root).resolve()
    payload: dict[str, Any] = {
        "schema_version": "dawnstrike.release_manifest.v1",
        "source_sha": source_sha,
        "build_sha": build_sha,
        "deployment_boundary": "configured_runtime_and_durable_state",
        "deployment_boundary_sha256": hashlib.sha256(
            f"{runtime}\n{durable_state}".encode()
        ).hexdigest(),
        "database_schema_version": schema_version,
        "data_watermark": data_watermark,
        "strategy_versions": _active_strategy_versions(
            paperops_version="immutable-strategy-semantics-manifest"
        ),
        "scheduler_version": SCHEDULER_VERSION,
        "artifact_hashes": dict(sorted(artifact_hashes.items())),
        "created_at": _utc_now(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["release_manifest_sha256"] = hashlib.sha256(
        json.dumps(
            payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    return payload


__all__ = [
    "DAILY_STAGE_ORDER",
    "REQUIRED_FULL_CHAIN_STAGES",
    "REQUIRED_UPSTREAM_STAGES",
    "SCHEDULER_VERSION",
    "daily_run_snapshot",
    "latest_daily_run_snapshot",
    "record_daily_stage",
    "release_manifest_payload",
    "resolve_release_sha",
    "shared_daily_run_id",
    "upstream_readiness",
]
