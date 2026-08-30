from __future__ import annotations

import json
import shutil
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner import cli as cli_module
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.services.setup_monitor import monitor_interval_gap_receipt
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

ROOT = Path(__file__).resolve().parents[1]
RELEASE_SHA = "a" * 40


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("node.exe") is None,
    reason="Windows Job Object contract requires Windows and Node.js",
)
def test_scheduled_process_timeout_receipt_is_distinct_and_tree_clean(tmp_path: Path):
    log_root = tmp_path / "logs"
    command = (
        f". '{ROOT / 'scripts' / 'dawnstrike_process_runner.ps1'}'; "
        "$r = Invoke-DawnstrikeNativeProcess "
        "-FilePath 'node.exe' "
        "-ArgumentList @('-e','setTimeout(()=>{},5000)') "
        f"-LogRoot '{log_root}' -LogName 'monitor-timeout' -TimeoutSeconds 1; "
        "$r | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout.strip().splitlines()[-1])
    assert receipt["exit_code"] == 124
    assert receipt["timed_out"] is True
    assert receipt["active_job_members_after_cleanup"] == 0
    assert receipt["timeout_cleanup_confirmed"] is True


@pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("node.exe") is None,
    reason="Windows Job Object contract requires Windows and Node.js",
)
def test_scheduled_process_nonzero_receipt_is_not_a_timeout(tmp_path: Path):
    log_root = tmp_path / "logs"
    command = (
        f". '{ROOT / 'scripts' / 'dawnstrike_process_runner.ps1'}'; "
        "$r = Invoke-DawnstrikeNativeProcess "
        "-FilePath 'node.exe' "
        "-ArgumentList @('-e',\"process.stderr.write('expected');process.exit(7)\") "
        f"-LogRoot '{log_root}' -LogName 'monitor-nonzero' -TimeoutSeconds 5; "
        "$r | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    receipt = json.loads(completed.stdout.strip().splitlines()[-1])
    assert receipt["exit_code"] == 7
    assert receipt["timed_out"] is False
    assert receipt["active_job_members_after_cleanup"] == 0


def test_scheduled_process_stage_budgets_fit_task_contract():
    runner = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(encoding="utf-8")
    job_runner = (ROOT / "scripts" / "dawnstrike_job_process.ps1").read_text(encoding="utf-8")
    assert '"(?i)monitor|trade_watch|scenario" { 180' in runner
    assert '"(?i)eod|paperops" { 7200' in runner
    assert '"(?i)finalize" { 10800' in runner
    assert '"(?i)weekly|training" { 10800' in runner
    assert "ValidateRange(1, 86400)" in job_runner


def test_monitor_cycle_start_is_an_exact_five_minute_boundary():
    helper = (ROOT / "scripts" / "monitor_schedule_helper.ps1").read_text(encoding="utf-8")
    assert (
        "function Get-DawnstrikeMonitorCycleStartUtc" in helper
        and "boundarySeconds" in helper
    )
    monitor = (ROOT / "scripts" / "run_alphaops_monitor.ps1").read_text(encoding="utf-8")
    assert "monitor_schedule_helper.ps1" in monitor


@pytest.mark.skipif(sys.platform != "win32", reason="Schedule helper requires PowerShell")
def test_powerShell_monitor_schedule_helper_executes_first_cycle_contract():
    helper = str(ROOT / "scripts" / "monitor_schedule_helper.ps1").replace("'", "''")
    command = f"""
    . '{helper}';
    $winter = Get-DawnstrikeMonitorScheduleStartUtc -MarketDate '2026-01-05';
    $summer = Get-DawnstrikeMonitorScheduleStartUtc -MarketDate '2026-07-06';
    $onTime = Get-DawnstrikeMonitorGapPlan -MarketDate '2026-01-05' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:35:32.1234567+00:00')) `
        -IsMarketDay $true;
    $late = Get-DawnstrikeMonitorGapPlan -MarketDate '2026-01-05' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:45:07.9876543+00:00')) `
        -IsMarketDay $true;
    $before = Get-DawnstrikeMonitorGapPlan -MarketDate '2026-01-05' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:30:00+00:00')) `
        -IsMarketDay $true;
    $closed = Get-DawnstrikeMonitorGapPlan -MarketDate '2026-01-05' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:45:00+00:00')) `
        -IsMarketDay $false;
    $unknown = Get-DawnstrikeMonitorGapPlan -MarketDate 'not-a-date' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:45:00+00:00')) `
        -IsMarketDay $true;
    $invalidBoundary = Get-DawnstrikeMonitorGapPlan -MarketDate '2026-01-05' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:45:00+00:00')) `
        -PreviousWatermarkUtc ([DateTimeOffset]::Parse('2026-01-05T14:40:01+00:00')) `
        -HasPreviousWatermark $true -IsMarketDay $true;
    $invalidBefore = Get-DawnstrikeMonitorGapPlan -MarketDate '2026-01-05' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:35:00+00:00')) `
        -PreviousWatermarkUtc ([DateTimeOffset]::Parse('2026-01-05T14:30:00+00:00')) `
        -HasPreviousWatermark $true -IsMarketDay $true;
    $invalidFuture = Get-DawnstrikeMonitorGapPlan -MarketDate '2026-01-05' `
        -CycleStartUtc ([DateTimeOffset]::Parse('2026-01-05T14:35:00+00:00')) `
        -PreviousWatermarkUtc ([DateTimeOffset]::Parse('2026-01-05T14:40:00+00:00')) `
        -HasPreviousWatermark $true -IsMarketDay $true;
    $jitter1 = Get-DawnstrikeMonitorCycleStartUtc `
        -NowUtc ([DateTimeOffset]::Parse('2026-01-05T14:05:32.1234567+00:00'));
    $jitter2 = Get-DawnstrikeMonitorCycleStartUtc `
        -NowUtc ([DateTimeOffset]::Parse('2026-01-05T14:10:07.9876543+00:00'));
    $watermark = [ordered]@{{
        schema_version='dawnstrike.monitor_interval_watermark.v2';
        schedule_id='alphaops-monitor-5m'; schedule_version='v1';
        market_date='2026-01-05'; release_sha=('a' * 40);
        last_cycle_start_utc='2026-01-05T14:05:00.0000000+00:00';
        interval_seconds=300; recorded_at_utc='2026-01-05T14:05:01.0000000+00:00';
        producer='run_alphaops_monitor.ps1'
    }};
    $watermark.watermark_sha256 = Get-DawnstrikeMonitorPayloadSha256 `
        -Payload $watermark -HashProperty 'watermark_sha256';
    $watermarkHashValid = $watermark.watermark_sha256 -eq (
        Get-DawnstrikeMonitorPayloadSha256 -Payload $watermark -HashProperty 'watermark_sha256'
    );
    $unknownReceipt = [ordered]@{{
        schema_version='dawnstrike.monitor_initial_coverage.v2';
        schedule_id='alphaops-monitor-5m'; schedule_version='v1';
        release_sha=('a' * 40); market_date='2026-01-05';
        status='UNKNOWN_INITIAL_COVERAGE'; reason='schedule_derivation_failed';
        market_data_available=$false; missing_is_not_zero=$true; research_only=$true;
        broker_execution_enabled=$false; recorded_at_utc='2026-01-05T14:05:01.0000000+00:00'
    }};
    $unknownReceipt.coverage_sha256 = Get-DawnstrikeMonitorPayloadSha256 `
        -Payload $unknownReceipt -HashProperty 'coverage_sha256';
    $unknownHashValid = $unknownReceipt.coverage_sha256 -eq (
        Get-DawnstrikeMonitorPayloadSha256 -Payload $unknownReceipt -HashProperty 'coverage_sha256'
    );
    [pscustomobject]@{{
        winter=$winter.ToString('o'); summer=$summer.ToString('o');
        on_time=$onTime.status; on_time_count=@($onTime.gap_slots).Count;
        late=$late.status; late_count=@($late.gap_slots).Count;
        late_slots=(@($late.gap_slots | ForEach-Object {{ $_.ToString('o') }}) -join ',');
        before=$before.status; closed=$closed.status; unknown=$unknown.status;
        unknown_reason=$unknown.reason; jitter1=$jitter1.ToString('o');
        jitter2=$jitter2.ToString('o'); watermark_hash_valid=$watermarkHashValid;
        unknown_hash_valid=$unknownHashValid;
        invalid_boundary=$invalidBoundary.status + ':' + $invalidBoundary.reason;
        invalid_before=$invalidBefore.status + ':' + $invalidBefore.reason;
        invalid_future=$invalidFuture.status + ':' + $invalidFuture.reason
    }} | ConvertTo-Json -Compress
    """
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result["winter"] == "2026-01-05T14:35:00.0000000+00:00"
    assert result["summer"] == "2026-07-06T13:35:00.0000000+00:00"
    assert result["on_time"] == "ON_TIME" and result["on_time_count"] == 0
    assert result["late"] == "GAPS_FOUND" and result["late_count"] == 2
    assert result["late_slots"] == (
        "2026-01-05T14:35:00.0000000+00:00,"
        "2026-01-05T14:40:00.0000000+00:00"
    )
    assert result["before"] == "BEFORE_SCHEDULE_START"
    assert result["closed"] == "NOT_APPLICABLE"
    assert result["unknown"] == "UNKNOWN_INITIAL_COVERAGE"
    assert result["unknown_reason"] == "schedule_derivation_failed"
    assert result["jitter1"] == "2026-01-05T14:05:00.0000000+00:00"
    assert result["jitter2"] == "2026-01-05T14:10:00.0000000+00:00"
    assert result["watermark_hash_valid"] is True
    assert result["unknown_hash_valid"] is True
    assert result["invalid_boundary"] == "INVALID_WATERMARK:watermark_not_on_interval_boundary"
    assert result["invalid_before"] == "INVALID_WATERMARK:watermark_before_schedule_start"
    assert result["invalid_future"] == "INVALID_WATERMARK:watermark_after_cycle_start"

    monitor = (ROOT / "scripts" / "run_alphaops_monitor.ps1").read_text(encoding="utf-8")
    assert "dawnstrike.monitor_interval_watermark.v2" in monitor
    assert "watermark_sha256" in monitor
    assert "dawnstrike.monitor_initial_coverage.v2" in monitor
    assert "coverage_sha256" in monitor


def test_first_monitor_cycle_binds_to_eastern_schedule_contract_and_dst():
    monitor = (ROOT / "scripts" / "run_alphaops_monitor.ps1").read_text(encoding="utf-8")
    helper = (ROOT / "scripts" / "monitor_schedule_helper.ps1").read_text(encoding="utf-8")
    assert "08:35 America/Chicago" in helper
    assert 'FindSystemTimeZoneById("Central Standard Time")' in helper
    assert monitor.index("$calendar = Invoke-DawnstrikeNativeProcess") < monitor.rindex(
        "Record-MissedMonitorIntervals"
    )
    assert '$monitorBeforeSchedule = $false' in monitor
    assert 'SKIPPED_NOT_APPLICABLE' in monitor
    assert 'before_schedule_start' in monitor

    central = ZoneInfo("America/Chicago")
    for market_date, expected_utc in (
        ("2026-01-05", "2026-01-05T14:35:00+00:00"),
        ("2026-07-06", "2026-07-06T13:35:00+00:00"),
    ):
        start = datetime.fromisoformat(f"{market_date}T08:35:00").replace(tzinfo=central)
        assert start.astimezone(timezone.utc).isoformat() == expected_utc

        # On-time first launch has no preceding required slot; a 10-minute
        # late launch proves exactly the two 5-minute slots before it.
        on_time = start.astimezone(timezone.utc)
        late = on_time + timedelta(minutes=10)
        assert [on_time + timedelta(minutes=5 * index) for index in range(2)] == [
            on_time,
            on_time + timedelta(minutes=5),
        ]
        assert late - on_time == timedelta(minutes=10)


@pytest.mark.skipif(sys.platform != "win32", reason="PowerShell process identity requires Windows")
def test_legacy_lock_uses_acquired_at_and_rejects_pid_reuse(tmp_path: Path):
    lock_path = tmp_path / "legacy.lock"
    quoted_path = str(lock_path).replace("'", "''")
    command = f"""
    . '{str(ROOT / 'scripts' / 'invoke_dawnstrike_stage.ps1').replace("'", "''")}';
    $ownerPid = $PID;
    $owner = Get-Process -Id $ownerPid;
    $active = @{{schema_version='dawnstrike.daily_run_lock.v2'; process_id=$ownerPid;
        acquired_at=$owner.StartTime.ToUniversalTime().AddSeconds(1).ToString('o')}} |
        ConvertTo-Json | Set-Content -LiteralPath '{quoted_path}';
    $activeResult = Test-DawnstrikeLockOwnerActive -LockPath '{quoted_path}';
    $reused = @{{schema_version='dawnstrike.daily_run_lock.v2'; process_id=$ownerPid;
        acquired_at=$owner.StartTime.ToUniversalTime().AddSeconds(-10).ToString('o')}} |
        ConvertTo-Json | Set-Content -LiteralPath '{quoted_path}';
    $reusedResult = Test-DawnstrikeLockOwnerActive -LockPath '{quoted_path}';
    [pscustomobject]@{{active=$activeResult; reused=$reusedResult}} | ConvertTo-Json -Compress
    """
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=20,
    )
    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    assert result == {"active": True, "reused": False}


def test_monitor_gap_receipt_is_idempotent_across_late_observation(tmp_path):
    store = SQLiteScanStore(tmp_path / "monitor.sqlite")
    first = monitor_interval_gap_receipt(
        expected_at="2026-08-29T14:00:00+00:00",
        observed_at="2026-08-29T14:06:00+00:00",
        interval_seconds=300,
        market_date="2026-08-29",
        release_sha=RELEASE_SHA,
    )
    later = monitor_interval_gap_receipt(
        expected_at=first["expected_at"],
        observed_at="2026-08-29T14:20:00+00:00",
        interval_seconds=300,
        market_date="2026-08-29",
        release_sha=RELEASE_SHA,
    )

    assert store.persist_monitor_interval_gap_receipts([first]) == {
        "inserted": 1,
        "reused": 0,
        "count": 1,
    }
    assert store.persist_monitor_interval_gap_receipts([later]) == {
        "inserted": 0,
        "reused": 1,
        "count": 1,
    }
    saved = store.load_monitor_interval_gap_receipts(market_date="2026-08-29")
    assert saved == [first]
    assert saved[0]["market_data_available"] is False
    assert saved[0]["missing_is_not_zero"] is True
    assert saved[0]["schema_version"] == "dawnstrike.monitor_interval_gap.v2"
    assert saved[0]["release_sha"] == RELEASE_SHA
    assert len(saved[0]["receipt_sha256"]) == 64
    assert store.load_monitor_interval_gap_projection(market_date="2026-08-29")
    assert store.count_monitor_interval_gaps(market_date="2026-08-29") == 1

    conflicting = dict(first)
    conflicting["schedule_id"] = "other-schedule"
    with pytest.raises(ValueError, match="identity hash"):
        store.persist_monitor_interval_gap_receipts([conflicting])


def test_monitor_gap_projection_has_fixed_order_and_bound_market_date(tmp_path):
    store = SQLiteScanStore(tmp_path / "monitor.sqlite")
    receipts = [
        monitor_interval_gap_receipt(
            expected_at=f"2026-08-{day:02d}T14:00:00+00:00",
            observed_at=f"2026-08-{day:02d}T14:06:00+00:00",
            interval_seconds=300,
            market_date=f"2026-08-{day:02d}",
            release_sha=RELEASE_SHA,
        )
        for day in (29, 30)
    ]
    assert store.persist_monitor_interval_gap_receipts(receipts)["inserted"] == 2

    expected_fields = (
        "gap_id",
        "run_id",
        "market_date",
        "schedule_id",
        "schedule_version",
        "release_sha",
        "expected_at",
        "observed_at",
        "interval_seconds",
        "status",
        "reason",
        "receipt_sha256",
    )
    filtered = store.load_monitor_interval_gap_projection(market_date="2026-08-29")
    assert len(filtered) == 1
    assert tuple(filtered[0]) == expected_fields
    assert filtered[0]["market_date"] == "2026-08-29"
    assert store.load_monitor_interval_gap_projection(market_date="2026-08-30")[0][
        "market_date"
    ] == "2026-08-30"
    # SQL structure is fixed and the caller value remains a bound predicate;
    # an injection-shaped suffix cannot widen the market-date result set.
    assert store.load_monitor_interval_gap_projection(
        market_date="2026-08-29' OR 1=1 --"
    ) == filtered


def test_monitor_gap_requires_exact_release_sha():
    with pytest.raises(ValueError, match="lowercase 40-character SHA"):
        monitor_interval_gap_receipt(
            expected_at="2026-08-29T14:00:00+00:00",
            observed_at="2026-08-29T14:06:00+00:00",
            interval_seconds=300,
            market_date="2026-08-29",
            release_sha="A" * 40,
        )


def test_continuous_monitor_catchup_receipts_only_complete_intervals(tmp_path):
    args = SimpleNamespace(
        db_path=str(tmp_path / "monitor.sqlite"),
        persist=True,
        persist_interval_gaps=True,
        release_sha=RELEASE_SHA,
        market_date="2026-08-29",
        schedule_id="alphaops-monitor-5m",
    )
    first_due = datetime.fromisoformat("2026-08-29T14:00:00+00:00")
    observed = datetime.fromisoformat("2026-08-29T14:06:00+00:00")
    assert cli_module._persist_monitor_interval_gaps(args, first_due, observed, 300) == 1
    store = SQLiteScanStore(args.db_path)
    saved = store.load_monitor_interval_gap_receipts(market_date="2026-08-29")
    assert [row["expected_at"] for row in saved] == ["2026-08-29T14:00:00+00:00"]
    # The 14:05 slot is still less than one full interval late and remains
    # the next immediate work item for the loop rather than being skipped.
    next_due = first_due
    while next_due + timedelta(seconds=300) <= observed:
        next_due += timedelta(seconds=300)
    assert next_due.isoformat() == "2026-08-29T14:05:00+00:00"


def test_persisted_monitor_loop_requires_release_sha_before_database_work(tmp_path):
    args = SimpleNamespace(
        db_path=str(tmp_path / "monitor.sqlite"), persist=True, persist_interval_gaps=True
    )
    with pytest.raises(SnapshotValidationError, match="requires --release-sha"):
        cli_module._persist_monitor_interval_gaps(
            args,
            datetime.fromisoformat("2026-08-29T14:00:00+00:00"),
            datetime.fromisoformat("2026-08-29T14:06:00+00:00"),
            300,
        )


def test_manual_monitor_persist_does_not_claim_unbound_interval_gaps(tmp_path):
    db_path = tmp_path / "monitor.sqlite"
    args = SimpleNamespace(db_path=str(db_path), persist=True)
    assert cli_module._persist_monitor_interval_gaps(
        args,
        datetime.fromisoformat("2026-08-29T14:00:00+00:00"),
        datetime.fromisoformat("2026-08-29T14:06:00+00:00"),
        300,
    ) == 0
    assert not db_path.exists()


def test_monitor_gap_schedule_identity_allows_competing_intervals(tmp_path):
    store = SQLiteScanStore(tmp_path / "monitor.sqlite")
    five_minute = monitor_interval_gap_receipt(
        expected_at="2026-08-29T14:00:00+00:00",
        observed_at="2026-08-29T14:06:00+00:00",
        interval_seconds=300,
        market_date="2026-08-29",
        schedule_id="alphaops-monitor-5m",
        release_sha=RELEASE_SHA,
    )
    one_minute = monitor_interval_gap_receipt(
        expected_at=five_minute["expected_at"],
        observed_at="2026-08-29T14:02:00+00:00",
        interval_seconds=60,
        market_date="2026-08-29",
        schedule_id="monitor-open-1m",
        release_sha=RELEASE_SHA,
    )
    stats = store.persist_monitor_interval_gap_receipts([five_minute, one_minute])
    assert stats["inserted"] == 2
    assert len(store.load_monitor_interval_gap_receipts(market_date="2026-08-29")) == 2


def test_monitor_gap_rejects_invalid_observation_and_market_slot():
    with pytest.raises(ValueError, match="after expected"):
        monitor_interval_gap_receipt(
            expected_at="2026-08-29T14:00:00+00:00",
            observed_at="2026-08-29T14:00:00+00:00",
            interval_seconds=300,
            market_date="2026-08-29",
            release_sha=RELEASE_SHA,
        )
    with pytest.raises(ValueError, match="at least one interval"):
        monitor_interval_gap_receipt(
            expected_at="2026-08-29T14:00:00+00:00",
            observed_at="2026-08-29T14:02:00+00:00",
            interval_seconds=300,
            market_date="2026-08-29",
            release_sha=RELEASE_SHA,
        )
    with pytest.raises(ValueError, match="market_date"):
        monitor_interval_gap_receipt(
            expected_at="2026-08-29T14:00:00+00:00",
            observed_at="2026-08-29T14:06:00+00:00",
            interval_seconds=300,
            market_date="2026-08-28",
            release_sha=RELEASE_SHA,
        )


def test_monitor_projection_and_count_match_payload_history(tmp_path):
    store = SQLiteScanStore(tmp_path / "monitor.sqlite")
    events = [
        {
            "ticker": "AAA",
            "event_type": "momentum_failure",
            "severity": "warning",
            "created_at": "2026-08-29T14:00:00+00:00",
            "extra": {"must_not_be_hydrated": True},
        },
        {
            "ticker": "BBB",
            "event_type": "manual_monitor_required",
            "severity": "info",
            "created_at": "2026-08-29T14:01:00+00:00",
        },
    ]
    store.persist_monitor_events(events, run_id="run-1")

    full = store.load_recent_monitor_events()
    projection = store.load_recent_monitor_event_projection()
    assert [(row["ticker"], row["event_type"]) for row in projection] == [
        (row["ticker"], row["event_type"]) for row in full
    ]
    assert store.count_monitor_events() == len(full)
    assert store.count_monitor_events(event_type="momentum_failure") == 1
    assert "extra" not in projection[0]

    with store._connect() as connection:
        plan = connection.execute(
            "EXPLAIN QUERY PLAN SELECT COUNT(*) FROM monitor_events "
            "WHERE event_type = ?",
            ("momentum_failure",),
        ).fetchall()
    assert any("idx_monitor_events_type_created" in str(row) for row in plan)


def test_monitor_history_limits_are_positive(tmp_path):
    store = SQLiteScanStore(tmp_path / "monitor.sqlite")
    for loader in (
        store.load_recent_monitor_events,
        store.load_recent_monitor_event_projection,
        store.load_latest_monitor_checks,
        store.load_latest_monitor_check_projection,
    ):
        try:
            loader(0)
        except ValueError:
            pass
        else:
            raise AssertionError("non-positive history limit was accepted")
    for loader in (
        store.load_monitor_interval_gap_receipts,
        store.load_monitor_interval_gap_projection,
    ):
        try:
            loader(limit=0)
        except ValueError:
            pass
        else:
            raise AssertionError("non-positive gap limit was accepted")
