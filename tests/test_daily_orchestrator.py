from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService
from intraday_scanner.services.daily_orchestrator_service import (
    daily_orchestration_status,
    write_heartbeat,
)
from intraday_scanner.services.daily_run_service import DAILY_STAGE_ORDER, record_daily_stage
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_daily_orchestrator_reports_missing_stages_with_fresh_heartbeat(tmp_path: Path) -> None:
    now = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    store = SQLiteScanStore(tmp_path / "state.sqlite")
    heartbeat = write_heartbeat(
        state_root=tmp_path,
        market_date="2026-08-03",
        stage="morning_collection",
        run_id="daily-1",
        status="RUNNING",
        now=now,
    )
    status = daily_orchestration_status(
        store,
        market_date="2026-08-03",
        state_root=tmp_path,
        now=now,
    )

    assert heartbeat["run_id"] == "daily-1"
    assert status["heartbeat_stale"] is False
    assert status["status"] == "MISSED_OR_PENDING_STAGES"
    assert "morning_collection" in status["missing_stages"]


def test_daily_orchestrator_ignores_recovered_failed_attempt(tmp_path: Path) -> None:
    now = datetime(2026, 8, 28, 22, 35, tzinfo=timezone.utc)
    market_date = "2026-08-28"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    db_path = state / "state.sqlite"
    runtime.mkdir()
    state.mkdir()
    common = {
        "db_path": db_path,
        "market_date": market_date,
        "runtime_root": runtime,
        "state_root": state,
        "release_sha": "a" * 40,
    }
    record_daily_stage(
        **common,
        stage_name="scenario_intelligence",
        status="FAILED",
        exit_code=2,
        error_code="transient_provider_failure",
        required=False,
    )
    record_daily_stage(
        **common,
        stage_name="scenario_intelligence",
        status="COMPLETE",
        exit_code=0,
        required=False,
    )
    for stage in DAILY_STAGE_ORDER:
        record_daily_stage(
            **common,
            stage_name=stage,
            status="COMPLETE",
            exit_code=0,
        )
    write_heartbeat(
        state_root=state,
        market_date=market_date,
        stage="publication",
        run_id="daily-fixture",
        status="COMPLETE",
        now=now,
    )

    status = daily_orchestration_status(
        SQLiteScanStore(db_path),
        market_date=market_date,
        state_root=state,
        now=now,
    )

    assert status["latest_run"]["status"] == "COMPLETE"
    assert status["failed_stages"] == []
    assert status["missing_stages"] == []
    assert status["status"] == "HEALTHY"


def test_daily_orchestrator_does_not_require_live_heartbeat_after_complete_run(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 28, 23, 30, tzinfo=timezone.utc)
    market_date = "2026-08-28"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    db_path = state / "state.sqlite"
    runtime.mkdir()
    state.mkdir()
    common = {
        "db_path": db_path,
        "market_date": market_date,
        "runtime_root": runtime,
        "state_root": state,
        "release_sha": "a" * 40,
    }
    for stage in DAILY_STAGE_ORDER:
        record_daily_stage(
            **common,
            stage_name=stage,
            status="COMPLETE",
            exit_code=0,
        )
    write_heartbeat(
        state_root=state,
        market_date=market_date,
        stage="canonical_performance",
        run_id="daily-fixture",
        status="RUNNING",
        now=now - timedelta(minutes=31),
    )

    status = daily_orchestration_status(
        SQLiteScanStore(db_path),
        market_date=market_date,
        state_root=state,
        now=now,
    )

    assert status["latest_run"]["status"] == "COMPLETE"
    assert status["failed_stages"] == []
    assert status["missing_stages"] == []
    assert status["heartbeat_stale"] is False
    assert status["status"] == "HEALTHY"


def test_daily_orchestrator_interprets_closed_terminal_run_without_readiness(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc)
    state = tmp_path / "state"
    state.mkdir()
    result = DailyFinalizeService(
        state / "state.sqlite",
        state / "public",
        runtime_root=tmp_path,
        state_root=state,
        release_sha="b" * 40,
    ).run(market_date="2026-08-29", now="2026-08-29T21:00:00+00:00")

    status = daily_orchestration_status(
        SQLiteScanStore(state / "state.sqlite"),
        market_date="2026-08-29",
        state_root=state,
        now=now,
    )

    assert result["readiness"]["status"] == "not_applicable"
    assert status["status"] == "SKIPPED_NOT_APPLICABLE"
    assert status["terminal_state"] == "SKIPPED_NOT_APPLICABLE"
    assert status["heartbeat_stale"] is False
    assert status["latest_run"]["completed_at"] == now.isoformat()


def test_daily_orchestrator_keeps_closed_terminal_run_fresh_after_heartbeat_ttl(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc)
    state = tmp_path / "state"
    state.mkdir()
    DailyFinalizeService(
        state / "state.sqlite",
        state / "public",
        runtime_root=tmp_path,
        state_root=state,
        release_sha="c" * 40,
    ).run(market_date="2026-08-29", now=now.isoformat())

    status = daily_orchestration_status(
        SQLiteScanStore(state / "state.sqlite"),
        market_date="2026-08-29",
        state_root=state,
        now=now + timedelta(minutes=31),
    )

    assert status["status"] == "SKIPPED_NOT_APPLICABLE"
    assert status["terminal_state"] == "SKIPPED_NOT_APPLICABLE"
    assert status["heartbeat_stale"] is False
    assert status["latest_run"]["completed_at"] == now.isoformat()


def test_daily_orchestrator_keeps_expired_in_progress_run_stale(
    tmp_path: Path,
) -> None:
    now = datetime(2026, 8, 29, 21, 0, tzinfo=timezone.utc)
    market_date = "2026-08-29"
    state = tmp_path / "state"
    runtime = tmp_path / "runtime"
    state.mkdir()
    runtime.mkdir()
    observed_at = now - timedelta(minutes=31)
    record_daily_stage(
        db_path=state / "state.sqlite",
        market_date=market_date,
        stage_name="morning_collection",
        status="IN_PROGRESS",
        runtime_root=runtime,
        state_root=state,
        release_sha="d" * 40,
        observed_at=observed_at.isoformat(),
    )
    write_heartbeat(
        state_root=state,
        market_date=market_date,
        stage="morning_collection",
        run_id="daily-in-progress",
        status="RUNNING",
        now=observed_at,
    )

    status = daily_orchestration_status(
        SQLiteScanStore(state / "state.sqlite"),
        market_date=market_date,
        state_root=state,
        now=now,
    )

    assert status["latest_run"]["status"] == "IN_PROGRESS"
    assert status["heartbeat_stale"] is True
    assert status["status"] == "STALE_HEARTBEAT"
