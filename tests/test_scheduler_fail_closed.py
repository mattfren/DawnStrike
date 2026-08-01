from __future__ import annotations

from pathlib import Path

import intraday_scanner.services.scheduler_doctor_service as scheduler


def test_scheduler_doctor_rejects_interactive_and_s4u_tasks(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    scripts = runtime / "scripts"
    scripts.mkdir(parents=True)
    state.mkdir()
    for name in (
        "run_alphaops_morning.ps1",
        "run_alphaops_monitor.ps1",
        "run_alphaops_eod.ps1",
        "run_daily_finalize.ps1",
        "register_alphaops_tasks.ps1",
        "register_daily_finalize_task.ps1",
        "restore_dawnstrike_tasks.ps1",
    ):
        (scripts / name).write_text("placeholder", encoding="utf-8")
    rows = []
    for name, script in scheduler.EXPECTED_TASKS.items():
        rows.append(
            {
                "name": name,
                "state": "Ready",
                "enabled": True,
                "logon_type": "S4U" if "Morning" in name else "Interactive",
                "start_when_available": True,
                "stop_if_going_on_batteries": False,
                "disallow_start_if_on_batteries": False,
                "last_task_result": 0,
                "execute": "powershell.exe",
                "arguments": (
                    f'-File "{scripts / script}" -RuntimeRoot "{runtime}" '
                    f'-StateRoot "{state}"'
                ),
                "working_directory": str(runtime),
            }
        )
    monkeypatch.setattr(scheduler, "_query_scheduled_tasks", lambda: rows)

    result = scheduler.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == 4
