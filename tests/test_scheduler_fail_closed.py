from __future__ import annotations

from pathlib import Path

import intraday_scanner.services.scheduler_doctor_service as scheduler


def test_scheduler_doctor_rejects_interactive_and_s4u_tasks(tmp_path: Path, monkeypatch) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    scripts = runtime / "scripts"
    scripts.mkdir(parents=True)
    state.mkdir()
    source_config = state / "config" / "web_sources.yaml"
    source_config.parent.mkdir()
    source_config.write_text(
        "enabled: true\n"
        "user_agent: DawnstrikeTest Contact: test@dawnstrike.test\n"
        "sources:\n"
        "  - name: candidates\n"
        "    type: local_inbox\n"
        "    enabled: true\n"
        "    path: data\\inbox\\screener\n",
        encoding="utf-8",
    )
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
                "execution_time_limit": scheduler.EXPECTED_EXECUTION_LIMITS[name],
                "repetition_duration": scheduler.EXPECTED_TASK_REPETITIONS.get(name),
            }
        )
    monkeypatch.setattr(scheduler, "_query_scheduled_tasks", lambda: rows)

    result = scheduler.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == len(scheduler.EXPECTED_TASKS)
