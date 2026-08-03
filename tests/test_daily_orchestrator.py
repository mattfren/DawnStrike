from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from intraday_scanner.services.daily_orchestrator_service import (
    daily_orchestration_status,
    write_heartbeat,
)
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
