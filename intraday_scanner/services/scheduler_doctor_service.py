"""Read-only proof of Dawnstrike's release-bound Windows task DAG."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_scanner.providers.web_source_base import validate_web_source_config

CANONICAL_TASK_NAME = "Dawnstrike 10of10 Daily Finalize"
EXPECTED_TASKS = {
    "Dawnstrike AlphaOps Morning": "run_alphaops_morning.ps1",
    "Dawnstrike AlphaOps Monitor 5m": "run_alphaops_monitor.ps1",
    "Dawnstrike AlphaOps EOD Full Report": "run_alphaops_eod.ps1",
    "Dawnstrike AlphaOps V6 Weekly Training": "run_alphaops_weekly_training.ps1",
    CANONICAL_TASK_NAME: "run_daily_finalize.ps1",
}
EXPECTED_TASK_STARTS = {
    "Dawnstrike AlphaOps Morning": "08:00",
    "Dawnstrike AlphaOps Monitor 5m": "08:35",
    "Dawnstrike AlphaOps EOD Full Report": "15:15",
    "Dawnstrike AlphaOps V6 Weekly Training": "21:00",
    CANONICAL_TASK_NAME: "17:30",
}
EXPECTED_TASK_REPETITIONS = {
    "Dawnstrike AlphaOps Monitor 5m": "PT6H35M",
}
EXPECTED_EXECUTION_LIMITS = {
    "Dawnstrike AlphaOps Morning": "PT1H",
    "Dawnstrike AlphaOps Monitor 5m": "PT4M",
    "Dawnstrike AlphaOps EOD Full Report": "PT2H",
    "Dawnstrike AlphaOps V6 Weekly Training": "PT3H",
    CANONICAL_TASK_NAME: "PT3H",
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
        "durable_source_config": state / "config" / "web_sources.yaml",
    }
    present = {name: path.is_file() for name, path in required.items()}
    source_config = validate_web_source_config(required["durable_source_config"])
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
                expected_start=EXPECTED_TASK_STARTS[name],
                expected_repetition=EXPECTED_TASK_REPETITIONS.get(name),
                expected_execution_limit=EXPECTED_EXECUTION_LIMITS[name],
            )
        )
    unexpected_enabled = [
        row
        for row in task_rows
        if str(row.get("name") or "") not in EXPECTED_TASKS
        and row.get("enabled") is True
        and str(runtime / "scripts").lower() in str(row.get("arguments") or "").lower()
    ]
    failed_checks = [
        check
        for check in checks
        if str(check.get("status") or "") not in {"LOCAL_VERIFIED"}
    ]
    if not all(present.values()) or source_config.get("ready") is not True:
        status = "FAILED"
        next_action = (
            "Restore the missing V6 scheduler artifacts and a semantically valid "
            "durable source configuration."
        )
    elif any(check.get("state") in {"unavailable", "unknown"} for check in checks):
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Run scheduler-doctor on the approved Windows runtime with "
            "Task Scheduler query access."
        )
    elif failed_checks or unexpected_enabled:
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Register or repair every Dawnstrike V6 task against the exact runtime "
            "and state roots, remove duplicate enabled runners, then rerun scheduler-doctor."
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
        "schema_version": "dawnstrike.scheduler_doctor.v4",
        "status": status,
        "runtime_root": str(runtime),
        "state_root": str(state),
        "required_files": present,
        "durable_source_config": source_config,
        "expected_task_names": list(EXPECTED_TASKS),
        "scheduled_tasks": checks,
        "scheduled_task": finalize,
        "expected_task_name": CANONICAL_TASK_NAME,
        "failed_task_count": len(failed_checks),
        "unexpected_enabled_tasks": unexpected_enabled,
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
    expected_start: str,
    expected_repetition: str | None,
    expected_execution_limit: str,
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
    trigger_start_boundary = str(task.get("trigger_start_boundary") or "")
    trigger_start = _parse_aware_datetime(trigger_start_boundary)
    history_superseded = _history_predates_trigger_definition(
        last_run_time=str(task.get("last_run_time") or ""),
        trigger_start=trigger_start,
    )
    last_run_result_acceptable = (
        last_result in ACCEPTABLE_LAST_RESULTS or history_superseded
    )
    expected_hour, expected_minute = (int(value) for value in expected_start.split(":"))
    scheduled_time_matches = (
        trigger_start is not None
        and trigger_start.hour == expected_hour
        and trigger_start.minute == expected_minute
        and trigger_start.second == 0
        and trigger_start.microsecond == 0
    )
    repetition_duration = str(task.get("repetition_duration") or "")
    repetition_matches = (
        expected_repetition is None or repetition_duration == expected_repetition
    )
    execution_time_limit = str(task.get("execution_time_limit") or "")
    execution_limit_matches = execution_time_limit == expected_execution_limit
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
            runner_ok,
            runtime_ok,
            state_ok,
            legacy_free,
            noninteractive,
            start_when_available,
            battery_safe,
            scheduled_time_matches,
            repetition_matches,
            execution_limit_matches,
            last_run_result_acceptable,
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
        "last_run_result_acceptable": last_run_result_acceptable,
        "last_run_status": (
            "SUPERSEDED_BY_CURRENT_DEFINITION"
            if history_superseded and last_result not in ACCEPTABLE_LAST_RESULTS
            else "ACCEPTABLE"
            if last_run_result_acceptable
            else "STALE_OR_FAILED"
        ),
        "history_superseded_by_current_definition": history_superseded,
        "expected_start_local": expected_start,
        "scheduled_time_matches": scheduled_time_matches,
        "expected_repetition_duration": expected_repetition,
        "repetition_duration_matches": repetition_matches,
        "expected_execution_time_limit": expected_execution_limit,
        "execution_time_limit_matches": execution_limit_matches,
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
    script = (
        "$names = @(Get-ScheduledTask | Where-Object { "
        "$_.TaskName -like 'Dawnstrike AlphaOps*' -or "
        "$_.TaskName -eq 'Dawnstrike 10of10 Daily Finalize' "
        "} | Select-Object -ExpandProperty TaskName); "
        # Explicitly preserve missing canonical rows in the output; querying all
        # similarly named tasks also exposes enabled duplicate runners.
        f"$expected = ConvertFrom-Json '{json.dumps(list(EXPECTED_TASKS))}'; "
        "$names = @($names + ($expected | Where-Object { $_ -notin $names })); "
        "$rows = foreach ($name in $names) { "
        "$task = Get-ScheduledTask -TaskName $name -ErrorAction SilentlyContinue; "
        "if ($null -eq $task) { "
        "[pscustomobject]@{name=$name; state='missing'; enabled=$null; "
        "last_task_result=$null; last_run_time=$null; next_run_time=$null; "
        "execute=$null; arguments=$null; working_directory=$null; "
        "execution_time_limit=$null; repetition_duration=$null} "
        "} else { "
        "$info = Get-ScheduledTaskInfo -TaskName $task.TaskName; "
        "$action = @($task.Actions)[0]; "
        "[pscustomobject]@{name=$task.TaskName; state=$task.State.ToString(); "
        "enabled=[bool]$task.Settings.Enabled; "
        "logon_type=$task.Principal.LogonType.ToString(); "
        "start_when_available=[bool]$task.Settings.StartWhenAvailable; "
        "stop_if_going_on_batteries=[bool]$task.Settings.StopIfGoingOnBatteries; "
        "disallow_start_if_on_batteries=[bool]$task.Settings.DisallowStartIfOnBatteries; "
        "execution_time_limit=[string]$task.Settings.ExecutionTimeLimit; "
        "trigger_start_boundary=if (@($task.Triggers)[0].StartBoundary) "
        "{[string]@($task.Triggers)[0].StartBoundary} else {$null}; "
        "repetition_duration=if (@($task.Triggers)[0].Repetition) "
        "{[string]@($task.Triggers)[0].Repetition.Duration} else {$null}; "
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
        "repetition_duration": None,
        "execution_time_limit": None,
        "trigger_start_boundary": None,
    }


def _history_predates_trigger_definition(
    *,
    last_run_time: str,
    trigger_start: datetime | None,
) -> bool:
    """Return true only when failure history belongs to the replaced definition."""

    last_run = _parse_aware_datetime(last_run_time)
    if last_run is None or trigger_start is None:
        return False
    return last_run.astimezone(trigger_start.tzinfo).date() < trigger_start.date()


def _parse_aware_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo is not None else None


__all__ = [
    "CANONICAL_TASK_NAME",
    "EXPECTED_TASKS",
    "FORBIDDEN_LEGACY_ROOT",
    "NONINTERACTIVE_LOGON_TYPES",
    "SCHED_S_TASK_HAS_NOT_RUN",
    "SCHED_S_TASK_RUNNING",
    "scheduler_doctor",
]
