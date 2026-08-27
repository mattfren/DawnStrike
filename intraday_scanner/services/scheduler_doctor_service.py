"""Read-only proof of Dawnstrike's release-bound Windows task DAG."""

from __future__ import annotations

import json
import os
import subprocess
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.providers.web_source_base import validate_web_source_config

CANONICAL_TASK_NAME = "Dawnstrike 10of10 Daily Finalize"
PUBLICATION_CONTRACT = {
    "schema_version": "dawnstrike.publication_schedule.v1",
    "timezone": "America/Chicago",
    "market_day_only": True,
    "scheduled_time_local": "17:30",
    "task_name": CANONICAL_TASK_NAME,
    "research_only": True,
    "live_trading_enabled": False,
}
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
SCHEDULER_QUERY_TIMEOUT_SECONDS = 30
EXPECTED_TASK_EXECUTABLE = "powershell.exe"
EXPECTED_WINDOWS_TIMEZONE_ID = "Central Standard Time"
EXPECTED_VERCEL_PROJECT_ID = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy"
EXPECTED_MULTIPLE_INSTANCES = "IgnoreNew"
EXPECTED_TRIGGER_TYPES = {
    **{
        name: "MSFT_TaskWeeklyTrigger"
        for name in EXPECTED_TASKS
        if name != CANONICAL_TASK_NAME
    },
    CANONICAL_TASK_NAME: "MSFT_TaskDailyTrigger",
}
EXPECTED_TRIGGER_DAYS_OF_WEEK = {
    "Dawnstrike AlphaOps Morning": 62,
    "Dawnstrike AlphaOps Monitor 5m": 62,
    "Dawnstrike AlphaOps EOD Full Report": 62,
    "Dawnstrike AlphaOps V6 Weekly Training": 2,
    CANONICAL_TASK_NAME: None,
}
EXPECTED_TRIGGER_WEEKS_INTERVAL = {
    name: 1 for name in EXPECTED_TASKS if name != CANONICAL_TASK_NAME
}
EXPECTED_TRIGGER_DAYS_INTERVAL = {CANONICAL_TASK_NAME: 1}
EXPECTED_REPETITION_INTERVALS = {"Dawnstrike AlphaOps Monitor 5m": "PT5M"}
EXPECTED_REPETITION_STOP_AT_DURATION_END = {
    name: name == "Dawnstrike AlphaOps Monitor 5m" for name in EXPECTED_TASKS
}
EXPECTED_RESTART_COUNTS = {
    "Dawnstrike AlphaOps Morning": 3,
    "Dawnstrike AlphaOps Monitor 5m": 3,
    "Dawnstrike AlphaOps EOD Full Report": 3,
    "Dawnstrike AlphaOps V6 Weekly Training": 4,
    CANONICAL_TASK_NAME: 2,
}
EXPECTED_RESTART_INTERVALS = {
    "Dawnstrike AlphaOps Morning": "PT5M",
    "Dawnstrike AlphaOps Monitor 5m": "PT5M",
    "Dawnstrike AlphaOps EOD Full Report": "PT5M",
    "Dawnstrike AlphaOps V6 Weekly Training": "PT15M",
    CANONICAL_TASK_NAME: "PT15M",
}
EXPECTED_RUN_LEVEL = "Limited"
SCHEDULE_TIMEZONE = ZoneInfo("America/Chicago")
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
    observation_date = datetime.now(SCHEDULE_TIMEZONE).date()
    by_name: dict[str, dict[str, Any]] = {}
    for row in task_rows:
        name = str(row.get("name") or "")
        task_path = str(row.get("task_path") or "\\")
        if name not in by_name or task_path == "\\":
            by_name[name] = row
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
                observation_date=observation_date,
            )
        )
    unexpected_enabled = [
        row
        for row in task_rows
        if (
            row.get("enabled") is True
            or str(row.get("state") or "") in {"Queued", "Running"}
        )
        and (
            str(row.get("name") or "") not in EXPECTED_TASKS
            or str(row.get("task_path") or "\\") != "\\"
        )
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
        "publication_contract": dict(PUBLICATION_CONTRACT),
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
    observation_date: date,
) -> dict[str, Any]:
    task_name = str(task.get("name") or "")
    state = str(task.get("state") or "unknown")
    execute = str(task.get("execute") or "")
    arguments = str(task.get("arguments") or "")
    working_directory = str(task.get("working_directory") or "")
    action_text = " ".join(
        (
            execute,
            arguments,
            working_directory,
        )
    )
    runner_ok = str(expected_runner).lower() in arguments.lower()
    executable_ok = execute.lower() == EXPECTED_TASK_EXECUTABLE
    expected_arguments = _expected_action_arguments(
        task_name,
        expected_runner=expected_runner,
        runtime_root=runtime_root,
        state_root=state_root,
    )
    action_arguments_match = arguments == expected_arguments
    action_count = task.get("action_count")
    action_count_matches = type(action_count) is int and action_count == 1
    trigger_count = task.get("trigger_count")
    trigger_count_matches = type(trigger_count) is int and trigger_count == 1
    runtime_ok = (
        str(runtime_root).lower() in arguments.lower()
        and str(runtime_root).lower() == working_directory.lower()
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
    trigger_start_local = (
        trigger_start.astimezone(SCHEDULE_TIMEZONE)
        if trigger_start is not None
        else None
    )
    scheduled_time_matches = (
        trigger_start_local is not None
        and trigger_start_local.hour == expected_hour
        and trigger_start_local.minute == expected_minute
        and trigger_start_local.second == 0
        and trigger_start_local.microsecond == 0
    )
    trigger_active = (
        trigger_start_local is not None
        and trigger_start_local.date() <= observation_date
    )
    trigger_end_boundary_absent = not str(task.get("trigger_end_boundary") or "")
    trigger_random_delay_absent = not str(task.get("trigger_random_delay") or "")
    repetition_duration = str(task.get("repetition_duration") or "")
    expected_repetition_duration = expected_repetition or ""
    repetition_matches = repetition_duration == expected_repetition_duration
    repetition_interval = str(task.get("repetition_interval") or "")
    expected_repetition_interval = EXPECTED_REPETITION_INTERVALS.get(task_name, "")
    repetition_interval_matches = (
        repetition_interval == expected_repetition_interval
    )
    expected_repetition_stop = EXPECTED_REPETITION_STOP_AT_DURATION_END.get(
        task_name
    )
    repetition_stop_matches = (
        type(task.get("repetition_stop_at_duration_end")) is bool
        and task.get("repetition_stop_at_duration_end") is expected_repetition_stop
    )
    expected_trigger_type = EXPECTED_TRIGGER_TYPES.get(task_name)
    trigger_type_matches = task.get("trigger_type") == expected_trigger_type
    trigger_enabled = task.get("trigger_enabled") is True
    expected_days_of_week = EXPECTED_TRIGGER_DAYS_OF_WEEK.get(task_name)
    trigger_days_of_week_matches = _optional_int_matches(
        task.get("trigger_days_of_week"), expected_days_of_week
    )
    expected_weeks_interval = EXPECTED_TRIGGER_WEEKS_INTERVAL.get(task_name)
    trigger_weeks_interval_matches = _optional_int_matches(
        task.get("trigger_weeks_interval"), expected_weeks_interval
    )
    expected_days_interval = EXPECTED_TRIGGER_DAYS_INTERVAL.get(task_name)
    trigger_days_interval_matches = _optional_int_matches(
        task.get("trigger_days_interval"), expected_days_interval
    )
    host_timezone_matches = (
        task.get("host_timezone_id") == EXPECTED_WINDOWS_TIMEZONE_ID
    )
    multiple_instances_matches = (
        task.get("multiple_instances") == EXPECTED_MULTIPLE_INSTANCES
    )
    execution_time_limit = str(task.get("execution_time_limit") or "")
    execution_limit_matches = execution_time_limit == expected_execution_limit
    expected_restart_count = EXPECTED_RESTART_COUNTS.get(task_name)
    restart_count_matches = _optional_int_matches(
        task.get("restart_count"), expected_restart_count
    )
    expected_restart_interval = EXPECTED_RESTART_INTERVALS.get(task_name, "")
    restart_interval_matches = (
        str(task.get("restart_interval") or "") == expected_restart_interval
    )
    logon_type = str(task.get("logon_type") or "")
    noninteractive = logon_type in NONINTERACTIVE_LOGON_TYPES
    run_level_matches = task.get("run_level") == EXPECTED_RUN_LEVEL
    start_when_available = task.get("start_when_available") is True
    wake_to_run = task.get("wake_to_run") is True
    battery_safe = (
        task.get("stop_if_going_on_batteries") is False
        and task.get("disallow_start_if_on_batteries") is False
    )
    verified = all(
        (
            enabled,
            healthy_state,
            executable_ok,
            action_arguments_match,
            action_count_matches,
            trigger_count_matches,
            runner_ok,
            runtime_ok,
            state_ok,
            legacy_free,
            noninteractive,
            run_level_matches,
            start_when_available,
            wake_to_run,
            battery_safe,
            scheduled_time_matches,
            trigger_active,
            trigger_end_boundary_absent,
            trigger_random_delay_absent,
            trigger_enabled,
            trigger_type_matches,
            trigger_days_of_week_matches,
            trigger_weeks_interval_matches,
            trigger_days_interval_matches,
            repetition_matches,
            repetition_interval_matches,
            repetition_stop_matches,
            host_timezone_matches,
            multiple_instances_matches,
            execution_limit_matches,
            restart_count_matches,
            restart_interval_matches,
            last_run_result_acceptable,
        )
    )
    return {
        **task,
        "expected_runner": str(expected_runner),
        "expected_executable": EXPECTED_TASK_EXECUTABLE,
        "executable_matches": executable_ok,
        "expected_arguments": expected_arguments,
        "action_arguments_match": action_arguments_match,
        "action_count_matches": action_count_matches,
        "trigger_count_matches": trigger_count_matches,
        "runner_matches": runner_ok,
        "runtime_root_matches": runtime_ok,
        "state_root_matches": state_ok,
        "legacy_root_absent": legacy_free,
        "noninteractive": noninteractive,
        "expected_run_level": EXPECTED_RUN_LEVEL,
        "run_level_matches": run_level_matches,
        "start_when_available": start_when_available,
        "wake_to_run_matches": wake_to_run,
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
        "trigger_active_on_observation_date": trigger_active,
        "trigger_end_boundary_absent": trigger_end_boundary_absent,
        "trigger_random_delay_absent": trigger_random_delay_absent,
        "expected_host_timezone_id": EXPECTED_WINDOWS_TIMEZONE_ID,
        "host_timezone_matches": host_timezone_matches,
        "expected_trigger_type": expected_trigger_type,
        "trigger_enabled_matches": trigger_enabled,
        "trigger_type_matches": trigger_type_matches,
        "expected_trigger_days_of_week": expected_days_of_week,
        "trigger_days_of_week_matches": trigger_days_of_week_matches,
        "expected_trigger_weeks_interval": expected_weeks_interval,
        "trigger_weeks_interval_matches": trigger_weeks_interval_matches,
        "expected_trigger_days_interval": expected_days_interval,
        "trigger_days_interval_matches": trigger_days_interval_matches,
        "expected_repetition_duration": expected_repetition_duration,
        "repetition_duration_matches": repetition_matches,
        "expected_repetition_interval": expected_repetition_interval,
        "repetition_interval_matches": repetition_interval_matches,
        "expected_repetition_stop_at_duration_end": expected_repetition_stop,
        "repetition_stop_at_duration_end_matches": repetition_stop_matches,
        "expected_multiple_instances": EXPECTED_MULTIPLE_INSTANCES,
        "multiple_instances_matches": multiple_instances_matches,
        "expected_execution_time_limit": expected_execution_limit,
        "execution_time_limit_matches": execution_limit_matches,
        "expected_restart_count": expected_restart_count,
        "restart_count_matches": restart_count_matches,
        "expected_restart_interval": expected_restart_interval,
        "restart_interval_matches": restart_interval_matches,
        "status": "LOCAL_VERIFIED" if verified else "FAILED",
    }


def _expected_action_arguments(
    task_name: str,
    *,
    expected_runner: Path,
    runtime_root: Path,
    state_root: Path,
) -> str:
    arguments = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{expected_runner}" '
        f'-RuntimeRoot "{runtime_root}" -StateRoot "{state_root}"'
    )
    if task_name == CANONICAL_TASK_NAME:
        arguments += (
            " -PublicationMode Production "
            f'-VercelProjectId "{EXPECTED_VERCEL_PROJECT_ID}"'
        )
    return arguments


def _optional_int_matches(value: Any, expected: int | None) -> bool:
    if expected is None:
        return value is None
    return type(value) is int and value == expected


def _scheduler_query_script() -> str:
    return (
        # Ask the Task Scheduler provider for the narrow Dawnstrike name
        # range. Enumerating every system task and filtering client-side can
        # exceed the bounded doctor timeout on otherwise healthy hosts.
        "$ErrorActionPreference = 'Stop'; "
        "$hostTimeZoneId = [TimeZoneInfo]::Local.Id; "
        "$tasks = @(Get-ScheduledTask -TaskPath '\\*' "
        "-TaskName 'Dawnstrike*' -ErrorAction Stop); "
        # Explicitly preserve missing canonical rows in the output; querying all
        # similarly named tasks also exposes enabled duplicate runners.
        f"$expected = ConvertFrom-Json '{json.dumps(list(EXPECTED_TASKS))}'; "
        "$canonicalNames = @($tasks | Where-Object { $_.TaskPath -eq '\\' } | "
        "Select-Object -ExpandProperty TaskName); "
        "$rows = foreach ($task in $tasks) { "
        "$info = Get-ScheduledTaskInfo -TaskName $task.TaskName "
        "-TaskPath $task.TaskPath -ErrorAction Stop; "
        "$actions = @($task.Actions); $action = $actions[0]; "
        "$triggers = @($task.Triggers); $trigger = $triggers[0]; "
        "[pscustomobject]@{name=$task.TaskName; task_path=$task.TaskPath; "
        "state=$task.State.ToString(); enabled=[bool]$task.Settings.Enabled; "
        "action_count=$actions.Count; trigger_count=$triggers.Count; "
        "host_timezone_id=$hostTimeZoneId; "
        "logon_type=$task.Principal.LogonType.ToString(); "
        "run_level=$task.Principal.RunLevel.ToString(); "
        "start_when_available=[bool]$task.Settings.StartWhenAvailable; "
        "wake_to_run=[bool]$task.Settings.WakeToRun; "
        "stop_if_going_on_batteries=[bool]$task.Settings.StopIfGoingOnBatteries; "
        "disallow_start_if_on_batteries=[bool]$task.Settings.DisallowStartIfOnBatteries; "
        "execution_time_limit=[string]$task.Settings.ExecutionTimeLimit; "
        "restart_count=[int]$task.Settings.RestartCount; "
        "restart_interval=[string]$task.Settings.RestartInterval; "
        "multiple_instances=[string]$task.Settings.MultipleInstances; "
        "trigger_type=if ($trigger) {$trigger.CimClass.CimClassName} else {$null}; "
        "trigger_enabled=if ($null -ne $trigger.Enabled) "
        "{[bool]$trigger.Enabled} else {$null}; "
        "trigger_days_of_week=if ($null -ne $trigger.DaysOfWeek) "
        "{[int]$trigger.DaysOfWeek} else {$null}; "
        "trigger_weeks_interval=if ($null -ne $trigger.WeeksInterval) "
        "{[int]$trigger.WeeksInterval} else {$null}; "
        "trigger_days_interval=if ($null -ne $trigger.DaysInterval) "
        "{[int]$trigger.DaysInterval} else {$null}; "
        "trigger_start_boundary=if ($trigger.StartBoundary) "
        "{[string]$trigger.StartBoundary} else {$null}; "
        "trigger_end_boundary=if ($trigger.EndBoundary) "
        "{[string]$trigger.EndBoundary} else {$null}; "
        "trigger_random_delay=if ($trigger.RandomDelay) "
        "{[string]$trigger.RandomDelay} else {$null}; "
        "repetition_duration=if ($trigger.Repetition) "
        "{[string]$trigger.Repetition.Duration} else {$null}; "
        "repetition_interval=if ($trigger.Repetition) "
        "{[string]$trigger.Repetition.Interval} else {$null}; "
        "repetition_stop_at_duration_end=if ($null -ne "
        "$trigger.Repetition.StopAtDurationEnd) "
        "{[bool]$trigger.Repetition.StopAtDurationEnd} else {$null}; "
        "last_task_result=$info.LastTaskResult; "
        "last_run_time=if ($info.LastRunTime) "
        "{$info.LastRunTime.ToString('o')} else {$null}; "
        "next_run_time=if ($info.NextRunTime) "
        "{$info.NextRunTime.ToString('o')} else {$null}; "
        "execute=$action.Execute; arguments=$action.Arguments; "
        "working_directory=$action.WorkingDirectory} }; "
        "$missing = foreach ($name in ($expected | "
        "Where-Object { $_ -notin $canonicalNames })) { "
        "[pscustomobject]@{name=$name; state='missing'; enabled=$null; "
        "task_path='\\'; "
        "action_count=$null; trigger_count=$null; "
        "host_timezone_id=$hostTimeZoneId; multiple_instances=$null; "
        "run_level=$null; wake_to_run=$null; restart_count=$null; "
        "restart_interval=$null; "
        "trigger_type=$null; trigger_enabled=$null; "
        "trigger_days_of_week=$null; trigger_weeks_interval=$null; "
        "trigger_days_interval=$null; trigger_end_boundary=$null; "
        "trigger_random_delay=$null; repetition_interval=$null; "
        "repetition_stop_at_duration_end=$null; "
        "last_task_result=$null; last_run_time=$null; next_run_time=$null; "
        "execute=$null; arguments=$null; working_directory=$null; "
        "execution_time_limit=$null; repetition_duration=$null} }; "
        "@(@($rows) + @($missing)) | ConvertTo-Json -Compress"
    )


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
    script = _scheduler_query_script()
    try:
        completed = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            check=False,
            text=True,
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
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
        "task_path": "\\",
        "state": "missing",
        "enabled": None,
        "action_count": None,
        "trigger_count": None,
        "host_timezone_id": None,
        "multiple_instances": None,
        "run_level": None,
        "wake_to_run": None,
        "restart_count": None,
        "restart_interval": None,
        "trigger_type": None,
        "trigger_enabled": None,
        "trigger_days_of_week": None,
        "trigger_weeks_interval": None,
        "trigger_days_interval": None,
        "trigger_end_boundary": None,
        "trigger_random_delay": None,
        "repetition_interval": None,
        "repetition_stop_at_duration_end": None,
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
    "EXPECTED_TASK_EXECUTABLE",
    "FORBIDDEN_LEGACY_ROOT",
    "NONINTERACTIVE_LOGON_TYPES",
    "SCHED_S_TASK_HAS_NOT_RUN",
    "SCHED_S_TASK_RUNNING",
    "SCHEDULER_QUERY_TIMEOUT_SECONDS",
    "scheduler_doctor",
]
