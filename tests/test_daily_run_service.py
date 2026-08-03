from __future__ import annotations

from pathlib import Path

from intraday_scanner.services.daily_run_service import (
    DAILY_STAGE_ORDER,
    record_daily_stage,
    release_manifest_payload,
    shared_daily_run_id,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_shared_run_ledger_propagates_failure_and_accepts_sourced_retry(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "state" / "dawnstrike.sqlite"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir(exist_ok=True)
    release_sha = "a" * 40
    day = "2026-07-31"

    for stage in (
        "morning_collection",
        "ranking_delivery",
        "intraday_monitor",
    ):
        record_daily_stage(
            db_path=db_path,
            market_date=day,
            stage_name=stage,
            status="COMPLETE",
            runtime_root=runtime,
            state_root=state,
            release_sha=release_sha,
            exit_code=0,
        )
    failed = record_daily_stage(
        db_path=db_path,
        market_date=day,
        stage_name="eod_outcome_capture",
        status="TERMINAL_MISSING",
        runtime_root=runtime,
        state_root=state,
        release_sha=release_sha,
        exit_code=2,
        error_code="ineligible_provider_error",
        error_detail="Yahoo and Alpaca exhausted.",
    )

    assert failed["run"]["status"] == "DEGRADED"
    assert failed["run"]["failed_stage"] == "eod_outcome_capture"
    assert failed["upstream"]["ready"] is False
    assert failed["upstream"]["failed_stages"][0]["reason"] == (
        "Yahoo and Alpaca exhausted."
    )

    record_daily_stage(
        db_path=db_path,
        market_date=day,
        stage_name="eod_outcome_capture",
        status="COMPLETE",
        runtime_root=runtime,
        state_root=state,
        release_sha=release_sha,
        exit_code=0,
        output_hash_sha256="b" * 64,
    )
    repaired = record_daily_stage(
        db_path=db_path,
        market_date=day,
        stage_name="paper_reconciliation",
        status="COMPLETE",
        runtime_root=runtime,
        state_root=state,
        release_sha=release_sha,
        exit_code=0,
    )

    assert repaired["run"]["status"] == "IN_PROGRESS"
    assert repaired["run"]["failed_stage"] is None
    assert repaired["upstream"]["ready"] is True
    eod_attempts = [
        row
        for row in repaired["stages"]
        if row["stage_name"] == "eod_outcome_capture"
    ]
    assert [row["attempt_no"] for row in eod_attempts] == [1, 2]
    assert [row["status"] for row in eod_attempts] == [
        "TERMINAL_MISSING",
        "COMPLETE",
    ]


def test_full_release_bound_chain_completes_only_after_readiness(tmp_path: Path) -> None:
    db_path = tmp_path / "state.sqlite"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    release_sha = "c" * 40
    day = "2026-07-31"
    snapshot = {}

    for stage in DAILY_STAGE_ORDER:
        snapshot = record_daily_stage(
            db_path=db_path,
            market_date=day,
            stage_name=stage,
            status="COMPLETE",
            runtime_root=runtime,
            state_root=state,
            release_sha=release_sha,
            exit_code=0,
        )

    assert snapshot["run"]["run_id"] == shared_daily_run_id(day, release_sha)
    assert snapshot["run"]["status"] == "COMPLETE"
    assert snapshot["run"]["completed_at"]
    assert snapshot["last_fully_successful_run"]["run_id"] == (
        snapshot["run"]["run_id"]
    )
    store = SQLiteScanStore(db_path)
    assert len(store.load_daily_run_stages(run_id=snapshot["run"]["run_id"])) == len(
        DAILY_STAGE_ORDER
    )


def test_release_manifest_binds_runtime_state_schema_and_artifacts(
    tmp_path: Path,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()

    manifest = release_manifest_payload(
        source_sha="d" * 40,
        build_sha="e" * 40,
        runtime_root=runtime,
        state_root=state,
        schema_version=13,
        data_watermark="2026-07-31T20:00:00Z",
        artifact_hashes={"data/calendar.json": "f" * 64},
    )

    assert manifest["deployment_boundary"] == "configured_runtime_and_durable_state"
    assert "runtime_root" not in manifest
    assert "state_root" not in manifest
    assert manifest["database_schema_version"] == 13
    assert manifest["scheduler_version"] == "dawnstrike-scheduler-v6"
    assert manifest["strategy_versions"]["alphaops_v6_shadow"] == (
        "dawnstrike-alphaops-v6-shadow"
    )
    assert len(manifest["release_manifest_sha256"]) == 64
    assert manifest["broker_execution_enabled"] is False
