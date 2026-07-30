"""Read-only proof of the canonical Windows daily-finalize scheduler."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

CANONICAL_TASK_NAME = "Dawnstrike 10of10 Daily Finalize"
SCHED_S_TASK_RUNNING = 0x00041301
SCHED_S_TASK_HAS_NOT_RUN = 0x00041303


def scheduler_doctor(root: str | Path) -> dict[str, Any]:
    """Check local scheduler artifacts and the real task registration state.

    Registration is intentionally not performed by this doctor.  A missing
    task is an external gate, not a local success, so the result remains
    fail-closed until the operator registers it on the approved checkout.
    """

    base = Path(root)
    required = {
        "daily_runner": base / "scripts" / "run_daily_finalize.ps1",
        "task_registration": base / "scripts" / "register_daily_finalize_task.ps1",
        "restore_previous": base / "scripts" / "restore_previous_publish_task.ps1",
    }
    present: dict[str, bool] = {name: path.is_file() for name, path in required.items()}
    task = _query_scheduled_task()
    if not all(present.values()):
        status = "FAILED"
        next_action = "Restore missing scheduler artifacts."
    elif task["state"] == "missing":
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Register exactly one replacement task on the approved checkout, then rerun "
            "scheduler-doctor."
        )
    elif task["state"] in {"unavailable", "unknown"}:
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Run scheduler-doctor on the approved Windows checkout with task-query access."
        )
    elif not task.get("enabled", False):
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Enable the canonical task on the approved checkout, then rerun scheduler-doctor."
        )
    elif task.get("state") not in {"Ready", "Running"}:
        status = "FAILED"
        next_action = "Repair the canonical task state before relying on daily publication."
    elif task.get("last_task_result") not in {
        None,
        0,
        SCHED_S_TASK_RUNNING,
        SCHED_S_TASK_HAS_NOT_RUN,
    }:
        status = "FAILED"
        next_action = "Inspect the last daily-finalize failure before relying on the next run."
    else:
        status = "LOCAL_VERIFIED"
        next_action = (
            "Run one approved dated finalize rehearsal, then rerun this doctor."
            if task.get("last_task_result") == SCHED_S_TASK_HAS_NOT_RUN
            else "Keep the task registered exactly once and rerun this doctor after each release."
        )
    return {
        "status": status,
        "required_files": present,
        "scheduled_task": task,
        "expected_task_name": CANONICAL_TASK_NAME,
        "next_action": next_action,
    }


def _query_scheduled_task() -> dict[str, Any]:
    if os.name != "nt":
        return {
            "name": CANONICAL_TASK_NAME,
            "state": "unavailable",
            "enabled": None,
            "last_task_result": None,
            "last_run_time": None,
            "next_run_time": None,
            "detail": "Windows Task Scheduler is unavailable on this host.",
        }
    script = (
        "$task = Get-ScheduledTask -TaskName 'Dawnstrike 10of10 Daily Finalize' "
        "-ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { exit 3 }; "
        "$info = Get-ScheduledTaskInfo -TaskName $task.TaskName; "
        "[pscustomobject]@{ name=$task.TaskName; state=$task.State.ToString(); "
        "enabled=[bool]$task.Settings.Enabled; last_task_result=$info.LastTaskResult; "
        "last_run_time=if ($info.LastRunTime) {$info.LastRunTime.ToString('o')} else {$null}; "
        "next_run_time=if ($info.NextRunTime) {$info.NextRunTime.ToString('o')} else {$null} } "
        "| ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {
            "name": CANONICAL_TASK_NAME,
            "state": "unavailable",
            "enabled": None,
            "last_task_result": None,
            "last_run_time": None,
            "next_run_time": None,
            "detail": str(exc),
        }
    if completed.returncode == 3:
        return {
            "name": CANONICAL_TASK_NAME,
            "state": "missing",
            "enabled": None,
            "last_task_result": None,
            "last_run_time": None,
            "next_run_time": None,
        }
    if completed.returncode != 0:
        return {
            "name": CANONICAL_TASK_NAME,
            "state": "unknown",
            "enabled": None,
            "last_task_result": None,
            "last_run_time": None,
            "next_run_time": None,
            "detail": completed.stderr.strip() or "Task Scheduler query failed.",
        }
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return {
            "name": CANONICAL_TASK_NAME,
            "state": "unknown",
            "enabled": None,
            "last_task_result": None,
            "last_run_time": None,
            "next_run_time": None,
            "detail": "Task Scheduler returned non-JSON output.",
        }
    if not isinstance(result, dict):
        return {
            "name": CANONICAL_TASK_NAME,
            "state": "unknown",
            "enabled": None,
            "last_task_result": None,
            "last_run_time": None,
            "next_run_time": None,
            "detail": "Task Scheduler returned an invalid task object.",
        }
    return result
