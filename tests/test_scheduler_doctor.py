from pathlib import Path

import intraday_scanner.services.scheduler_doctor_service as scheduler_service


def _write_required_scripts(root: Path) -> None:
    scripts = root / "scripts"
    scripts.mkdir()
    for name in (
        "run_alphaops_morning.ps1",
        "run_alphaops_monitor.ps1",
        "run_alphaops_eod.ps1",
        "run_alphaops_weekly_training.ps1",
        "run_daily_finalize.ps1",
        "register_alphaops_tasks.ps1",
        "register_daily_finalize_task.ps1",
        "restore_dawnstrike_tasks.ps1",
    ):
        (scripts / name).write_text("placeholder", encoding="utf-8")


def _healthy_tasks(runtime: Path, state: Path, *, last_result: int = 0):
    return [
        {
            "name": name,
            "state": "Ready",
            "enabled": True,
            "logon_type": "Password",
            "start_when_available": True,
            "stop_if_going_on_batteries": False,
            "disallow_start_if_on_batteries": False,
            "last_task_result": last_result,
            "last_run_time": "2026-07-30T17:30:00-05:00",
            "next_run_time": (
                "2026-07-31T"
                f"{scheduler_service.EXPECTED_TASK_STARTS[name]}:00-05:00"
            ),
            "execute": "powershell.exe",
            "arguments": (
                f'-File "{runtime / "scripts" / script}" '
                f'-RuntimeRoot "{runtime}" -StateRoot "{state}"'
            ),
            "working_directory": str(runtime),
        }
        for name, script in scheduler_service.EXPECTED_TASKS.items()
    ]


def test_scheduler_doctor_blocks_when_any_v5_task_is_missing(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    rows[0] = {
        **rows[0],
        "state": "missing",
        "enabled": None,
        "arguments": None,
    }
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == 1
    assert result["expected_task_name"] == "Dawnstrike 10of10 Daily Finalize"


def test_scheduler_doctor_accepts_one_release_and_state_boundary(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    monkeypatch.setattr(
        scheduler_service,
        "_query_scheduled_tasks",
        lambda: _healthy_tasks(runtime, state),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "LOCAL_VERIFIED"
    assert result["failed_task_count"] == 0
    assert all(row["legacy_root_absent"] for row in result["scheduled_tasks"])


def test_scheduler_doctor_blocks_failed_or_stale_task_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    monkeypatch.setattr(
        scheduler_service,
        "_query_scheduled_tasks",
        lambda: _healthy_tasks(runtime, state, last_result=1),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == len(scheduler_service.EXPECTED_TASKS)
    assert all(
        row["last_run_status"] == "STALE_OR_FAILED"
        for row in result["scheduled_tasks"]
    )


def test_scheduler_doctor_blocks_enabled_duplicate_runner(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    rows.append(
        {
            **rows[0],
            "name": "Dawnstrike AlphaOps Morning Early",
            "next_run_time": "2026-07-31T07:15:00-05:00",
        }
    )
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert [row["name"] for row in result["unexpected_enabled_tasks"]] == [
        "Dawnstrike AlphaOps Morning Early"
    ]


def test_scheduler_doctor_rejects_legacy_source_root(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(
        runtime,
        state,
        last_result=scheduler_service.SCHED_S_TASK_HAS_NOT_RUN,
    )
    rows[1]["arguments"] += (
        f' -SourceRoot "{scheduler_service.FORBIDDEN_LEGACY_ROOT}"'
    )
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    monitor = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    assert monitor["legacy_root_absent"] is False


def test_scheduler_doctor_rejects_s4u_for_networked_alphaops(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    rows[0]["logon_type"] = "S4U"
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    morning = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Morning"
    )
    assert morning["noninteractive"] is False
