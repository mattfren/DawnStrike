"""Read-only proof of Dawnstrike's release-bound Windows task DAG."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from typing import Any

CANONICAL_TASK_NAME = "Dawnstrike 10of10 Daily Finalize"
EXPECTED_TASKS = {
    "Dawnstrike AlphaOps Morning": "run_alphaops_morning.ps1",
    "Dawnstrike AlphaOps Monitor 5m": "run_alphaops_monitor.ps1",
    "Dawnstrike AlphaOps EOD Full Report": "run_alphaops_eod.ps1",
    "Dawnstrike AlphaOps V6 Weekly Training": "run_alphaops_weekly_training.ps1",
    CANONICAL_TASK_NAME: "run_daily_finalize.ps1",
}
SCHED_S_TASK_RUNNING = 0x00041301
SCHED_S_TASK_HAS_NOT_RUN = 0x00041303
ACCEPTABLE_LAST_RESULTS = {
    None,
    0,
    SCHED_S_TASK_RUNNING,
    SCHED_S_TASK_HAS_NOT_RUN,
}
FORBIDDEN_LEGACY_ROOT = r"C:\Users\MattFields\Dawnstrike"
# AlphaOps needs network and encrypted-file access for real sources, the
# durable state store, and Telegram.  Windows S4U expressly has neither, so
# it is not an acceptable unattended identity for this DAG.
NONINTERACTIVE_LOGON_TYPES = frozenset({"Password", "ServiceAccount"})


def scheduler_doctor(
    root: str | Path,
    state_root: str | Path = r"C:\r\dawnstrike-state",
) -> dict[str, Any]:
    """Verify every enabled Dawnstrike V6 task uses one runtime and state root."""

    runtime = Path(root).resolve()
    state = Path(state_root).resolve()
    required = {
        "morning_runner": runtime / "scripts" / "run_alphaops_morning.ps1",
        "monitor_runner": runtime / "scripts" / "run_alphaops_monitor.ps1",
        "eod_runner": runtime / "scripts" / "run_alphaops_eod.ps1",
        "daily_runner": runtime / "scripts" / "run_daily_finalize.ps1",
        "alpha_registration": runtime / "scripts" / "register_alphaops_tasks.ps1",
        "finalize_registration": (
            runtime / "scripts" / "register_daily_finalize_task.ps1"
        ),
        "rollback": runtime / "scripts" / "restore_dawnstrike_tasks.ps1",
    }
    present = {name: path.is_file() for name, path in required.items()}
    queried = _query_scheduled_tasks()
    task_rows = _normalize_task_rows(queried)
    by_name = {str(row.get("name") or ""): row for row in task_rows}
    checks: list[dict[str, Any]] = []
    for name, script_name in EXPECTED_TASKS.items():
        task = by_name.get(name) or _missing_task(name)
        checks.append(
            _task_check(
                task,
                expected_runner=runtime / "scripts" / script_name,
                runtime_root=runtime,
                state_root=state,
            )
        )
    failed_checks = [
        check
        for check in checks
        if str(check.get("status") or "") not in {"LOCAL_VERIFIED"}
    ]
    if not all(present.values()):
        status = "FAILED"
        next_action = "Restore the missing V6 scheduler artifacts."
    elif any(check.get("state") in {"unavailable", "unknown"} for check in checks):
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Run scheduler-doctor on the approved Windows runtime with "
            "Task Scheduler query access."
        )
    elif failed_checks:
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Register or repair every Dawnstrike V6 task against the exact runtime "
            "and state roots, then rerun scheduler-doctor."
        )
    else:
        status = "LOCAL_VERIFIED"
        next_action = (
            "Run one dated full-chain rehearsal and preserve its shared run ledger."
        )
    next_runs = sorted(
        str(check.get("next_run_time"))
        for check in checks
        if check.get("next_run_time")
    )
    finalize = next(
        (check for check in checks if check.get("name") == CANONICAL_TASK_NAME),
        _missing_task(CANONICAL_TASK_NAME),
    )
    return {
        "schema_version": "dawnstrike.scheduler_doctor.v3",
        "status": status,
        "runtime_root": str(runtime),
        "state_root": str(state),
        "required_files": present,
        "expected_task_names": list(EXPECTED_TASKS),
        "scheduled_tasks": checks,
        "scheduled_task": finalize,
        "expected_task_name": CANONICAL_TASK_NAME,
        "failed_task_count": len(failed_checks),
        "next_scheduled_run": next_runs[0] if next_runs else None,
        "forbidden_legacy_root": FORBIDDEN_LEGACY_ROOT,
        "next_action": next_action,
    }


def _task_check(
    task: dict[str, Any],
    *,
    expected_runner: Path,
    runtime_root: Path,
    state_root: Path,
) -> dict[str, Any]:
    state = str(task.get("state") or "unknown")
    arguments = str(task.get("arguments") or "")
    working_directory = str(task.get("working_directory") or "")
    action_text = " ".join(
        (
            str(task.get("execute") or ""),
            arguments,
            working_directory,
        )
    )
    runner_ok = str(expected_runner).lower() in arguments.lower()
    runtime_ok = (
        str(runtime_root).lower() in arguments.lower()
        and (
            not working_directory
            or str(runtime_root).lower() == working_directory.lower()
        )
    )
    state_ok = str(state_root).lower() in arguments.lower()
    legacy_free = FORBIDDEN_LEGACY_ROOT.lower() not in action_text.lower()
    enabled = task.get("enabled") is True
    last_result = task.get("last_task_result")
    healthy_state = state in {"Ready", "Running"}
    healthy_result = last_result in ACCEPTABLE_LAST_RESULTS
    logon_type = str(task.get("logon_type") or "")
    noninteractive = logon_type in NONINTERACTIVE_LOGON_TYPES
    start_when_available = task.get("start_when_available") is True
    battery_safe = (
        task.get("stop_if_going_on_batteries") is False
        and task.get("disallow_start_if_on_batteries") is False
    )
    verified = all(
        (
            enabled,
            healthy_state,
            healthy_result,
            runner_ok,
            runtime_ok,
            state_ok,
            legacy_free,
            noninteractive,
            start_when_available,
            battery_safe,
        )
    )
    return {
        **task,
        "expected_runner": str(expected_runner),
        "runner_matches": runner_ok,
        "runtime_root_matches": runtime_ok,
        "state_root_matches": state_ok,
        "legacy_root_absent": legacy_free,
        "noninteractive": noninteractive,
        "start_when_available": start_when_available,
        "battery_safe": battery_safe,
        "status": "LOCAL_VERIFIED" if verified else "FAILED",
    }


def _query_scheduled_tasks() -> list[dict[str, Any]] | dict[str, Any]:
    if os.name != "nt":
        return [
            {
                **_missing_task(name),
                "state": "unavailable",
                "detail": "Windows Task Scheduler is unavailable on this host.",
            }
            for name in EXPECTED_TASKS
        ]
    names_json = json.dumps(list(EXPECTED_TASKS))
    script = (
        f"$names = ConvertFrom-Json '{names_json}'; "
        "$rows = foreach ($name in $names) { "
        "$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { "
        "[pscustomobject]@{name=$name; state='missing'; enabled=$null; "
        "last_task_result=$null; last_run_time=$null; next_run_time=$null; "
        "execute=$null; arguments=$null; working_directory=$null} "
        "} else { "
        "$info = Get-ScheduledTaskInfo -TaskName $task.TaskName; "
        "$action = @($task.Actions)[0]; "
        "[pscustomobject]@{name=$task.TaskName; state=$task.State.ToString(); "
        "enabled=[bool]$task.Settings.Enabled; "
        "logon_type=$task.Principal.LogonType.ToString(); "
        "start_when_available=[bool]$task.Settings.StartWhenAvailable; "
        "stop_if_going_on_batteries=[bool]$task.Settings.StopIfGoingOnBatteries; "
        "disallow_start_if_on_batteries=[bool]$task.Settings.DisallowStartIfOnBatteries; "
        "last_task_result=$info.LastTaskResult; "
        "last_run_time=if ($info.LastRunTime) "
        "{$info.LastRunTime.ToString('o')} else {$null}; "
        "next_run_time=if ($info.NextRunTime) "
        "{$info.NextRunTime.ToString('o')} else {$null}; "
        "execute=$action.Execute; arguments=$action.Arguments; "
        "working_directory=$action.WorkingDirectory} } }; "
        "@($rows) | ConvertTo-Json -Compress"
    )
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=15,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return [
            {
                **_missing_task(name),
                "state": "unavailable",
                "detail": str(exc),
            }
            for name in EXPECTED_TASKS
        ]
    if completed.returncode != 0:
        return [
            {
                **_missing_task(name),
                "state": "unknown",
                "detail": (
                    completed.stderr.strip() or "Task Scheduler query failed."
                ),
            }
            for name in EXPECTED_TASKS
        ]
    try:
        result = json.loads(completed.stdout)
    except json.JSONDecodeError:
        return [
            {
                **_missing_task(name),
                "state": "unknown",
                "detail": "Task Scheduler returned non-JSON output.",
            }
            for name in EXPECTED_TASKS
        ]
    return result


def _normalize_task_rows(
    value: list[dict[str, Any]] | dict[str, Any],
) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [row for row in value if isinstance(row, dict)]
    if isinstance(value, dict):
        return [value]
    return []


def _missing_task(name: str) -> dict[str, Any]:
    return {
        "name": name,
        "state": "missing",
        "enabled": None,
        "logon_type": None,
        "start_when_available": None,
        "stop_if_going_on_batteries": None,
        "disallow_start_if_on_batteries": None,
        "last_task_result": None,
        "last_run_time": None,
        "next_run_time": None,
        "execute": None,
        "arguments": None,
        "working_directory": None,
    }


__all__ = [
    "CANONICAL_TASK_NAME",
    "EXPECTED_TASKS",
    "FORBIDDEN_LEGACY_ROOT",
    "NONINTERACTIVE_LOGON_TYPES",
    "SCHED_S_TASK_HAS_NOT_RUN",
    "SCHED_S_TASK_RUNNING",
    "scheduler_doctor",
]
