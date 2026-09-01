"""Read-only proof of Dawnstrike's release-bound Windows task DAG."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import stat
import subprocess  # nosec B404
from datetime import date, datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.providers.web_source_base import validate_web_source_config

CANONICAL_TASK_NAME = "Dawnstrike 10of10 Daily Finalize"
AUXILIARY_TASK_NAME = "Dawnstrike Delayed SIP Capture"
AUXILIARY_SIDECAR_CONTRACT = "dawnstrike.account_capture_trial_sidecar.v1"
AUXILIARY_DECLARATION_FILE = Path("config") / "state_preparation_contract.json"
AUXILIARY_PYTHON_BOOTSTRAP = Path("scripts") / "dawnstrike_python_bootstrap.py"
AUXILIARY_CAPTURE_RUNNER = Path("scripts") / "run_daily_intraday_capture.py"
AUXILIARY_PYTHON_PREFIX = ("-I", "-B", "-S", "-X")
AUXILIARY_BOOTSTRAP_PRELOADER = (
    "import hashlib,sys; p=sys.argv[1]; e=sys.argv[2]; b=open(p,'rb').read(); "
    "a=hashlib.sha256(b).hexdigest(); a==e or (_ for _ in ()).throw("
    "RuntimeError('bootstrap hash mismatch')); r=sys.argv[3:]; sys.argv=[p,*r]; "
    "exec(compile(b,p,'exec'),{'__name__':'__main__','__file__':p})"
)
AUXILIARY_INTERPRETER = Path(
    r"C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe"
)
AUXILIARY_INTERPRETER_SHA256 = "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"
AUXILIARY_INTERPRETER_VERSION = "3.13.14"
AUXILIARY_INTERPRETER_SIGNER_SUBJECT = (
    "CN=Python Software Foundation, O=Python Software Foundation, L=Beaverton, S=Oregon, C=US"
)
AUXILIARY_INTERPRETER_SIGNER_THUMBPRINT = "9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48"
APPROVED_GIT_PATH = Path(r"C:\Program Files\Git\cmd\git.exe")
APPROVED_GIT_SHA256 = "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"
AUXILIARY_REQUIRED_OPTION_ORDER = (
    "--candidate-sha",
    "--repo-root",
    "--db-path",
    "--evidence-root",
    "--run-root",
    "--output-root",
    "--session-root",
    "--symbols-manifest",
    "--symbols-manifest-sha256",
    "--entitlement-receipt",
    "--entitlement-receipt-sha256",
    "--source-config",
    "--source-config-sha256",
    "--env-file",
    "--max-pages",
    "--retries",
)
AUXILIARY_REQUIRED_OPTIONS = frozenset(AUXILIARY_REQUIRED_OPTION_ORDER)
GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}$")
SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
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
EXPECTED_TASK_EXECUTABLE = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
EXPECTED_WINDOWS_TIMEZONE_ID = "Central Standard Time"
EXPECTED_VERCEL_PROJECT_ID = "prj_5pef3EZF1u5YadebEz3dFjnkWOXy"
EXPECTED_MULTIPLE_INSTANCES = "IgnoreNew"
EXPECTED_TRIGGER_TYPES = {
    **{name: "MSFT_TaskWeeklyTrigger" for name in EXPECTED_TASKS if name != CANONICAL_TASK_NAME},
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
    runtime_identity_before = _runtime_git_contract(runtime)
    expected_runtime_sha = (
        runtime_identity_before.get("candidate_sha", "")
        if runtime_identity_before is not None
        else ""
    )
    required = {
        "morning_runner": runtime / "scripts" / "run_alphaops_morning.ps1",
        "monitor_runner": runtime / "scripts" / "run_alphaops_monitor.ps1",
        "eod_runner": runtime / "scripts" / "run_alphaops_eod.ps1",
        "daily_runner": runtime / "scripts" / "run_daily_finalize.ps1",
        "alpha_registration": runtime / "scripts" / "register_alphaops_tasks.ps1",
        "finalize_registration": (runtime / "scripts" / "register_daily_finalize_task.ps1"),
        "rollback": runtime / "scripts" / "restore_dawnstrike_tasks.ps1",
        "durable_source_config": state / "config" / "web_sources.yaml",
    }
    present = {name: path.is_file() for name, path in required.items()}
    source_config = validate_web_source_config(required["durable_source_config"])
    queried = _query_scheduled_tasks()
    task_rows = _normalize_task_rows(queried)
    observation_date = datetime.now(SCHEDULE_TIMEZONE).date()
    activation_completed_at = _load_exact_activation_completion(runtime, state)
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
                expected_sha=expected_runtime_sha,
                expected_repetition=EXPECTED_TASK_REPETITIONS.get(name),
                expected_execution_limit=EXPECTED_EXECUTION_LIMITS[name],
                observation_date=observation_date,
                activation_completed_at=activation_completed_at,
            )
        )
    auxiliary_rows = [row for row in task_rows if str(row.get("name") or "") == AUXILIARY_TASK_NAME]
    auxiliary_check = _auxiliary_task_check(
        auxiliary_rows, runtime, state, observation_date=observation_date
    )
    if auxiliary_check is not None:
        checks.append(auxiliary_check)
    canonical_principal_ids = {
        str(check.get("principal_user_id") or "")
        for check in checks
        if check.get("name") in EXPECTED_TASKS
    }
    principal_identity_matches = (
        len(canonical_principal_ids) == 1 and "" not in canonical_principal_ids
    )
    if auxiliary_check is not None and auxiliary_check.get("enabled") is True:
        auxiliary_principal = str(auxiliary_check.get("principal_user_id") or "")
        principal_identity_matches = principal_identity_matches and (
            auxiliary_principal.casefold() == next(iter(canonical_principal_ids), "").casefold()
        )
    for check in checks:
        check["principal_identity_matches"] = principal_identity_matches
    auxiliary_unexpected = _auxiliary_unexpected_rows(auxiliary_rows, auxiliary_check)
    unexpected_enabled = [
        row
        for row in task_rows
        if (row.get("enabled") is True or str(row.get("state") or "") in {"Queued", "Running"})
        and (
            (
                str(row.get("name") or "") not in EXPECTED_TASKS
                and str(row.get("name") or "") != AUXILIARY_TASK_NAME
            )
            or str(row.get("task_path") or "\\") != "\\"
        )
    ]
    unexpected_enabled.extend(auxiliary_unexpected)
    # Preserve the first-seen order while preventing a duplicate auxiliary
    # row from being reported twice through the generic name filter.
    seen_rows: set[int] = set()
    deduped_unexpected: list[dict[str, Any]] = []
    for row in unexpected_enabled:
        if id(row) not in seen_rows:
            seen_rows.add(id(row))
            deduped_unexpected.append(row)
    unexpected_enabled = deduped_unexpected
    expected_task_names = list(EXPECTED_TASKS)
    failed_checks = [
        check for check in checks if str(check.get("status") or "") not in {"LOCAL_VERIFIED"}
    ]
    runtime_identity_after = _runtime_git_contract(runtime)
    runtime_identity_stable = (
        runtime_identity_before is not None and runtime_identity_after == runtime_identity_before
    )
    if not runtime_identity_stable:
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Restore a clean runtime whose stable HEAD and tree equal origin/main, "
            "then rerun scheduler-doctor."
        )
    elif not all(present.values()) or source_config.get("ready") is not True:
        status = "FAILED"
        next_action = (
            "Restore the missing V6 scheduler artifacts and a semantically valid "
            "durable source configuration."
        )
    elif any(check.get("state") in {"unavailable", "unknown"} for check in checks):
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Run scheduler-doctor on the approved Windows runtime with Task Scheduler query access."
        )
    elif failed_checks or unexpected_enabled or not principal_identity_matches:
        status = "BLOCKED_EXTERNAL"
        next_action = (
            "Register or repair every Dawnstrike V6 task against the exact runtime "
            "and state roots, remove duplicate enabled runners, then rerun scheduler-doctor."
        )
    else:
        status = "LOCAL_VERIFIED"
        next_action = "Run one dated full-chain rehearsal and preserve its shared run ledger."
    next_runs = sorted(
        str(check.get("next_run_time")) for check in checks if check.get("next_run_time")
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
        "runtime_identity_status": ("LOCAL_VERIFIED" if runtime_identity_stable else "FAILED"),
        "runtime_identity_stable": runtime_identity_stable,
        "runtime_identity_clean": True if runtime_identity_stable else None,
        "runtime_identity_failed": not runtime_identity_stable,
        "runtime_sha": (
            runtime_identity_before.get("candidate_sha")
            if runtime_identity_stable and runtime_identity_before is not None
            else None
        ),
        "runtime_tree": (
            runtime_identity_before.get("candidate_tree")
            if runtime_identity_stable and runtime_identity_before is not None
            else None
        ),
        "runtime_origin_sha256": (
            runtime_identity_before.get("runtime_origin_sha256")
            if runtime_identity_stable and runtime_identity_before is not None
            else None
        ),
        "required_files": present,
        "durable_source_config": source_config,
        "expected_task_names": expected_task_names,
        "governed_auxiliary_task_name": AUXILIARY_TASK_NAME,
        "governed_auxiliary_task": auxiliary_check,
        "principal_identity_matches": principal_identity_matches,
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
    expected_sha: str,
    expected_repetition: str | None,
    expected_execution_limit: str,
    observation_date: date,
    activation_completed_at: datetime | None,
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
    executable_ok = execute.casefold() == EXPECTED_TASK_EXECUTABLE.casefold()
    expected_arguments = _expected_action_arguments(
        task_name,
        expected_runner=expected_runner,
        runtime_root=runtime_root,
        state_root=state_root,
        expected_sha=expected_sha,
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
    history_superseded = _history_superseded_by_exact_runtime_activation(
        last_run_time=str(task.get("last_run_time") or ""),
        activation_completed_at=activation_completed_at,
    )
    last_run_result_acceptable = _acceptable_last_result(last_result) or history_superseded
    expected_hour, expected_minute = (int(value) for value in expected_start.split(":"))
    trigger_start_local = (
        trigger_start.astimezone(SCHEDULE_TIMEZONE) if trigger_start is not None else None
    )
    scheduled_time_matches = (
        trigger_start_local is not None
        and trigger_start_local.hour == expected_hour
        and trigger_start_local.minute == expected_minute
        and trigger_start_local.second == 0
        and trigger_start_local.microsecond == 0
    )
    trigger_active = (
        trigger_start_local is not None and trigger_start_local.date() <= observation_date
    )
    trigger_end_boundary_absent = not str(task.get("trigger_end_boundary") or "")
    trigger_random_delay_absent = not str(task.get("trigger_random_delay") or "")
    repetition_duration = str(task.get("repetition_duration") or "")
    expected_repetition_duration = expected_repetition or ""
    repetition_matches = repetition_duration == expected_repetition_duration
    repetition_interval = str(task.get("repetition_interval") or "")
    expected_repetition_interval = EXPECTED_REPETITION_INTERVALS.get(task_name, "")
    repetition_interval_matches = repetition_interval == expected_repetition_interval
    expected_repetition_stop = EXPECTED_REPETITION_STOP_AT_DURATION_END.get(task_name)
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
    host_timezone_matches = task.get("host_timezone_id") == EXPECTED_WINDOWS_TIMEZONE_ID
    multiple_instances_matches = task.get("multiple_instances") == EXPECTED_MULTIPLE_INSTANCES
    execution_time_limit = str(task.get("execution_time_limit") or "")
    execution_limit_matches = execution_time_limit == expected_execution_limit
    expected_restart_count = EXPECTED_RESTART_COUNTS.get(task_name)
    restart_count_matches = _optional_int_matches(task.get("restart_count"), expected_restart_count)
    expected_restart_interval = EXPECTED_RESTART_INTERVALS.get(task_name, "")
    restart_interval_matches = str(task.get("restart_interval") or "") == expected_restart_interval
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
            "SUPERSEDED_BY_EXACT_RUNTIME_ACTIVATION"
            if history_superseded and not _acceptable_last_result(last_result)
            else "ACCEPTABLE"
            if last_run_result_acceptable
            else "STALE_OR_FAILED"
        ),
        "history_superseded_by_exact_runtime_activation": history_superseded,
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


def _auxiliary_task_check(
    rows: list[dict[str, Any]],
    runtime: Path,
    state: Path,
    *,
    observation_date: date | None = None,
) -> dict[str, Any] | None:
    if not rows:
        contract = _load_auxiliary_contract(runtime, state)
        if not contract["declared"]:
            return None
        return {
            "name": AUXILIARY_TASK_NAME,
            "task_path": "\\",
            "state": "missing",
            "enabled": None,
            "status": "FAILED",
            "governed": False,
            "definition_status": "MISSING",
            "operational_ready": False,
            "operational_status": "DISABLED",
            "failure_reason": (
                "governed auxiliary task is missing" if contract["valid"] else contract["reason"]
            ),
            "last_task_result": None,
        }
    if len(rows) != 1:
        return {
            "name": AUXILIARY_TASK_NAME,
            "status": "FAILED",
            "governed": False,
            "failure_reason": "auxiliary task is duplicated",
            "duplicate_count": len(rows),
        }
    row = rows[0]
    enabled_flag = row.get("enabled") is True
    task_state = str(row.get("state") or "")
    if (enabled_flag and task_state not in {"Ready", "Running", "Queued"}) or (
        not enabled_flag and task_state != "Disabled"
    ):
        return {
            **row,
            "status": "FAILED",
            "governed": False,
            "definition_status": "INVALID_STATE",
            "operational_ready": False,
            "operational_status": "BLOCKED_NONOPERATIONAL",
            "failure_reason": "auxiliary enabled/state pair is contradictory",
            "last_task_result": row.get("last_task_result"),
        }
    enabled = enabled_flag
    if not enabled and task_state == "Disabled":
        enabled = False
    elif task_state in {"Ready", "Running", "Queued"}:
        enabled = True
    contract = _load_auxiliary_contract(runtime, state)
    if not enabled and not contract["declared"]:
        return {
            **row,
            "status": "LOCAL_VERIFIED",
            "governed": False,
            "definition_status": "DISABLED_UNDECLARED",
            "last_task_result": row.get("last_task_result"),
        }
    if not contract["valid"]:
        return {
            **row,
            "status": "FAILED",
            "governed": False,
            "definition_status": "INVALID",
            "failure_reason": contract["reason"],
            "last_task_result": row.get("last_task_result"),
        }
    action = _validate_auxiliary_action(row, runtime, state, contract)
    health = _validate_auxiliary_health(row, enabled, contract, observation_date=observation_date)
    health_valid = health["valid"]
    definition_integrity_valid = action["valid"] and health["definition_integrity_valid"]
    verified = action["valid"] and health_valid
    operational_ready = enabled and verified
    failure_reason = action["reason"] or health["reason"]
    return {
        **row,
        "status": "LOCAL_VERIFIED" if verified else "FAILED",
        "governed": definition_integrity_valid,
        "definition_status": (
            "READY"
            if enabled and definition_integrity_valid
            else "DISABLED"
            if not enabled and definition_integrity_valid
            else "INVALID"
        ),
        "operational_ready": operational_ready,
        "operational_status": (
            "READY" if operational_ready else "BLOCKED_NONOPERATIONAL" if enabled else "DISABLED"
        ),
        "sidecar_contract_matches": True,
        "action_contract_matches": action["action_contract_matches"],
        "runtime_root_matches": action["runtime_root_matches"],
        "state_root_matches": action["state_root_matches"],
        "candidate_sha_matches": action["candidate_sha_matches"],
        "bootstrap_matches": action["bootstrap_matches"],
        "runner_matches": action["runner_matches"],
        "task_path_matches": action["task_path_matches"],
        "action_count_matches": action["action_count_matches"],
        "state_matches": action["state_matches"],
        "failure_reason": failure_reason,
        **health,
        "last_task_result": row.get("last_task_result"),
    }


def _auxiliary_unexpected_rows(
    rows: list[dict[str, Any]], check: dict[str, Any] | None
) -> list[dict[str, Any]]:
    if not rows or check is None:
        return []
    if len(rows) != 1:
        return rows
    enabled = rows[0].get("enabled") is True or str(rows[0].get("state") or "") in {
        "Queued",
        "Running",
    }
    return rows if enabled and check.get("status") != "LOCAL_VERIFIED" else []


def _load_auxiliary_contract(runtime: Path, state: Path) -> dict[str, Any]:
    declaration_path = runtime / AUXILIARY_DECLARATION_FILE
    if not declaration_path.is_file():
        return {"declared": False, "valid": False, "reason": "sidecar declaration is missing"}
    try:
        declaration = json.loads(
            declaration_path.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_keys,
        )
    except (OSError, UnicodeDecodeError, ValueError):
        return {"declared": True, "valid": False, "reason": "sidecar declaration is invalid"}
    expected = {
        "schema_version": "dawnstrike.state_preparation_contract.v1",
        "sidecar_contract": AUXILIARY_SIDECAR_CONTRACT,
        "sidecar_version": 1,
        "legacy_schema_marker": 30,
        "required_before_activation": True,
        "capture_interpreter_path": str(AUXILIARY_INTERPRETER),
        "capture_interpreter_version": AUXILIARY_INTERPRETER_VERSION,
        "capture_interpreter_sha256": AUXILIARY_INTERPRETER_SHA256,
        "capture_interpreter_signer_subject": AUXILIARY_INTERPRETER_SIGNER_SUBJECT,
        "capture_interpreter_signer_thumbprint": AUXILIARY_INTERPRETER_SIGNER_THUMBPRINT,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if declaration != expected:
        return {"declared": True, "valid": False, "reason": "sidecar declaration is invalid"}
    runtime_contract = _runtime_git_contract(runtime)
    if runtime_contract is None:
        return {"declared": True, "valid": False, "reason": "runtime SHA is unavailable"}
    runtime_sha = runtime_contract["candidate_sha"]
    runtime_tree = runtime_contract["candidate_tree"]
    runtime_origin_sha = runtime_contract["runtime_origin_sha256"]
    receipt_root = state / "receipts" / "capture-task"
    receipt_paths = sorted(receipt_root.glob(f"capture-task-rebind-{runtime_sha}.json"))
    if len(receipt_paths) != 1:
        return {
            "declared": True,
            "valid": False,
            "reason": "capture sidecar receipt is missing or ambiguous",
        }
    try:
        from scripts.capture_task_contract import load_receipt

        receipt = load_receipt(
            receipt_paths[0], candidate_sha=runtime_sha, candidate_tree=runtime_tree
        )
        if str(receipt.get("activation_id") or "") != str(
            receipt.get("activation_receipt_name") or ""
        ).removeprefix("runtime-activation-").removesuffix(".json"):
            raise ValueError("capture activation id is not bound to its receipt name")
        if receipt.get("action_after_sha256") == receipt.get("action_before_sha256"):
            raise ValueError("capture action was not rebound")
        activation_name = str(receipt["activation_receipt_name"])
        activation_path = state / "receipts" / "runtime-activation" / activation_name
        if (
            activation_name != "runtime-activation-" + str(receipt["activation_id"]) + ".json"
            or not activation_path.is_file()
        ):
            raise ValueError("activation receipt is missing")
        from scripts.runtime_activation_contract import _assert_no_reparse_components
        from scripts.runtime_activation_contract import (
            load_receipt as load_activation_receipt,
        )

        _assert_no_reparse_components(activation_path)
        activation_raw = activation_path.read_bytes()
        if hashlib.sha256(activation_raw).hexdigest() != receipt["activation_receipt_sha256"]:
            raise ValueError("activation receipt hash mismatch")

        activation = load_activation_receipt(activation_path)
        if (
            activation.get("schema_version") != "dawnstrike.runtime_activation_receipt.v2"
            or activation.get("status") != "COMPLETE"
            or activation.get("activation_id") != receipt.get("activation_id")
            or activation.get("candidate_sha") != runtime_sha
            or activation.get("candidate_tree") != runtime_tree
            or activation.get("runtime_origin_sha256") != runtime_origin_sha
            or receipt.get("runtime_origin_sha256") != runtime_origin_sha
            or activation.get("state_preparation_contract") != AUXILIARY_SIDECAR_CONTRACT
            or activation.get("auxiliary_capture_present") is not True
            or activation.get("auxiliary_capture_state_after") != "Disabled"
            or activation.get("auxiliary_capture_action") != "DISABLED_UNTIL_EXACT_SHA_REBIND"
            or activation.get("auxiliary_capture_action_contract_sha256")
            != receipt.get("action_before_sha256")
            or activation.get("auxiliary_capture_definition_contract_sha256")
            != receipt.get("definition_before_sha256")
            or activation.get("auxiliary_capture_xml_sha256") != receipt.get("xml_before_sha256")
        ):
            raise ValueError("activation receipt is not bound to the runtime")
        hardening_relative = str(activation.get("capture_hardening_receipt_relative_path") or "")
        if not hardening_relative or hardening_relative == "NONE":
            raise ValueError("activation receipt has no hardening attestation path")
        hardening_path = state / Path(hardening_relative)
        if hardening_path.resolve().parent != (state / "receipts" / "capture-task").resolve():
            raise ValueError("hardening attestation path escaped the capture receipt root")
        hardening_path_hash = hashlib.sha256(hardening_path.read_bytes()).hexdigest()
        if hardening_path_hash != activation.get("capture_hardening_receipt_raw_sha256"):
            raise ValueError("hardening attestation raw hash mismatch")
        from scripts.capture_task_hardening_contract import load_receipt as load_hardening_receipt

        hardening = load_hardening_receipt(
            hardening_path, candidate_sha=runtime_sha, candidate_tree=runtime_tree
        )
        if (
            hardening.get("schema_version") != "dawnstrike.capture_task_hardening_receipt.v2"
            or hardening.get("receipt_relative_path") != hardening_relative
            or hardening.get("receipt_sha256") != activation.get("capture_hardening_receipt_sha256")
            or hardening.get("xml_after_sha256") != activation.get("capture_hardening_xml_sha256")
            or hardening.get("action_after_sha256")
            != activation.get("capture_hardening_action_sha256")
            or hardening.get("principal_after_sha256")
            != activation.get("capture_hardening_principal_sha256")
            or hardening.get("trigger_sha256") != activation.get("capture_hardening_trigger_sha256")
            or hardening.get("settings_after_sha256")
            != activation.get("capture_hardening_settings_sha256")
        ):
            raise ValueError("activation receipt is not bound to the hardening attestation")
        if (
            receipt.get("hardening_receipt_relative_path") != hardening_relative
            or receipt.get("hardening_receipt_raw_sha256")
            != activation.get("capture_hardening_receipt_raw_sha256")
            or receipt.get("hardening_receipt_sha256") != hardening.get("receipt_sha256")
        ):
            raise ValueError("capture rebind receipt is not bound to the hardening attestation")
        if _runtime_git_contract(runtime) != runtime_contract:
            raise ValueError("runtime identity changed during contract validation")
    except (ImportError, KeyError, OSError, TypeError, ValueError):
        return {
            "declared": True,
            "valid": False,
            "reason": "capture sidecar receipt is invalid",
        }
    return {
        "declared": True,
        "valid": True,
        "candidate_sha": runtime_sha,
        "candidate_tree": runtime_tree,
        "runtime_origin_sha256": runtime_origin_sha,
        "activation_id": str(receipt["activation_id"]),
        "action_contract_sha256": str(receipt["action_after_sha256"]),
        "definition_contract_sha256": str(receipt["definition_after_sha256"]),
        "principal_sha256": str(receipt["principal_sha256"]),
        "trigger_sha256": str(receipt["trigger_sha256"]),
        "settings_sha256": str(receipt["settings_sha256"]),
        "symbols_manifest_sha256": str(receipt["symbols_manifest_sha256"]),
        "entitlement_receipt_sha256": str(receipt["entitlement_receipt_sha256"]),
        "source_config_sha256": str(receipt["source_config_sha256"]),
        "capture_interpreter_path": declaration["capture_interpreter_path"],
        "capture_interpreter_version": declaration["capture_interpreter_version"],
        "capture_interpreter_sha256": declaration["capture_interpreter_sha256"],
        "capture_interpreter_signer_subject": declaration["capture_interpreter_signer_subject"],
        "capture_interpreter_signer_thumbprint": declaration[
            "capture_interpreter_signer_thumbprint"
        ],
    }


def _runtime_git_sha(runtime: Path) -> str | None:
    return _runtime_git_value(runtime, "HEAD")


def _runtime_git_tree(runtime: Path) -> str | None:
    return _runtime_git_value(runtime, "HEAD^{tree}")


def _runtime_git_origin_sha(runtime: Path) -> str | None:
    try:
        git_path, _ = _approved_git()
        completed = subprocess.run(  # nosec B603, B607
            [str(git_path), "-C", str(runtime), "remote", "get-url", "origin"],
            capture_output=True,
            check=True,
            text=True,
            env=_governed_git_environment(),
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    origin = completed.stdout.strip()
    if not origin:
        return None
    return hashlib.sha256(origin.encode("utf-8")).hexdigest()


def _runtime_git_origin_main(runtime: Path) -> str | None:
    return _runtime_git_value(runtime, "refs/remotes/origin/main")


def _runtime_git_snapshot(runtime: Path) -> dict[str, str] | None:
    candidate_sha = _runtime_git_sha(runtime)
    candidate_tree = _runtime_git_tree(runtime)
    runtime_origin_sha256 = _runtime_git_origin_sha(runtime)
    origin_main_sha = _runtime_git_origin_main(runtime)
    if (
        candidate_sha is None
        or candidate_tree is None
        or runtime_origin_sha256 is None
        or origin_main_sha != candidate_sha
    ):
        return None
    return {
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "runtime_origin_sha256": runtime_origin_sha256,
        "origin_main_sha": origin_main_sha,
        "git_executable_sha256": APPROVED_GIT_SHA256,
    }


def _runtime_git_contract(runtime: Path) -> dict[str, str] | None:
    """Return only a stable, clean exact-origin runtime identity."""

    before = _runtime_git_snapshot(runtime)
    if before is None or not _runtime_git_clean(runtime):
        return None
    after = _runtime_git_snapshot(runtime)
    if after != before or not _runtime_git_clean(runtime):
        return None
    final = _runtime_git_snapshot(runtime)
    return before if final == before else None


def _runtime_git_clean(runtime: Path) -> bool:
    """Match the release checkout cleanliness contract without exposing paths."""

    try:
        git_path, _ = _approved_git()
        status = subprocess.run(  # nosec B603, B607
            [
                str(git_path),
                "-C",
                str(runtime),
                "status",
                "--porcelain=v1",
                "--untracked-files=all",
                "--ignore-submodules=none",
            ],
            capture_output=True,
            check=True,
            text=True,
            env=_governed_git_environment(),
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
        )
        if status.stdout.strip():
            return False
        ignored = subprocess.run(  # nosec B603, B607
            [
                str(git_path),
                "-C",
                str(runtime),
                "ls-files",
                "--others",
                "--ignored",
                "--exclude-standard",
                "-z",
            ],
            capture_output=True,
            check=True,
            text=False,
            env=_governed_git_environment(),
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    for name in ignored.stdout.decode("utf-8", errors="replace").split("\0"):
        suffix = Path(name).suffix.lower()
        if suffix in {
            ".ps1",
            ".psm1",
            ".py",
            ".pyc",
            ".pyd",
            ".dll",
            ".exe",
            ".com",
            ".bat",
            ".cmd",
            ".sh",
            ".pth",
        } or Path(name).name.lower() in {"sitecustomize.py", "usercustomize.py"}:
            return False
    try:
        git_path, _ = _approved_git()
        flags = subprocess.run(
            [str(git_path), "-C", str(runtime), "ls-files", "-v", "-z"],
            capture_output=True,
            check=True,
            text=False,
            env=_governed_git_environment(),
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
        )
        if any(
            entry and entry[:1] in {b"h", b"H", b"s", b"S"}
            for entry in flags.stdout.split(b"\0")
        ):
            return False
        diff = subprocess.run(
            [str(git_path), "-C", str(runtime), "diff-index", "--quiet", "HEAD", "--"],
            capture_output=True,
            check=False,
            env=_governed_git_environment(),
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
        )
        if diff.returncode != 0:
            return False
        replacements = subprocess.run(
            [str(git_path), "-C", str(runtime), "replace", "-l"],
            capture_output=True,
            check=True,
            text=True,
            env=_governed_git_environment(),
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
        )
        if replacements.stdout.strip():
            return False
        for pattern in ("filter.*", "core.attributesfile", "core.hooksPath"):
            config = subprocess.run(
                [str(git_path), "-C", str(runtime), "config", "--local", "--get-regexp", pattern],
                capture_output=True,
                check=False,
                text=True,
                env=_governed_git_environment(),
                timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
            )
            if config.returncode == 0 and config.stdout.strip():
                return False
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return False
    return True


def _governed_git_environment() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")}
    env.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_NO_REPLACE_OBJECTS": "1",
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )
    return env


def _runtime_git_value(runtime: Path, revision: str) -> str | None:
    try:
        git_path, _ = _approved_git()
        completed = subprocess.run(  # nosec B603, B607
            [str(git_path), "-C", str(runtime), "rev-parse", revision],
            capture_output=True,
            check=True,
            text=True,
            env=_governed_git_environment(),
            timeout=SCHEDULER_QUERY_TIMEOUT_SECONDS,
        )
    except (OSError, RuntimeError, subprocess.SubprocessError):
        return None
    value = completed.stdout.strip().lower()
    return value if len(value) == 40 and all(char in "0123456789abcdef" for char in value) else None


def _approved_git() -> tuple[Path, str]:
    path = APPROVED_GIT_PATH
    try:
        cursor = path
        while True:
            details = cursor.lstat()
            if getattr(details, "st_file_attributes", 0) & getattr(
                stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
            ):
                raise OSError("reparse point")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        if not path.is_file():
            raise OSError("not a regular file")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError as exc:
        raise RuntimeError("approved Git executable is unavailable") from exc
    if digest != APPROVED_GIT_SHA256:
        raise RuntimeError("approved Git executable hash changed")
    return path, digest


def _action_option(tokens: list[str], option: str) -> str | None:
    try:
        index = tokens.index(option)
    except ValueError:
        return None
    return tokens[index + 1] if index + 1 < len(tokens) else None


def _safe_regular_path(path: Path) -> bool:
    try:
        from scripts.capture_task_contract import _assert_no_reparse_components

        supplied = _assert_no_reparse_components(path)
        return supplied.is_file() and not supplied.is_symlink()
    except (ImportError, OSError, TypeError, ValueError):
        return False


def _safe_file_sha256(path_text: str, expected: Any) -> bool:
    if not isinstance(expected, str) or SHA256_PATTERN.fullmatch(expected) is None:
        return False
    path = Path(path_text)
    if not _safe_regular_path(path):
        return False
    try:
        from scripts.capture_task_contract import _assert_no_reparse_components

        supplied = _assert_no_reparse_components(path)
        before = supplied.stat()
        digest = hashlib.sha256()
        with supplied.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        _assert_no_reparse_components(supplied)
        after = supplied.stat()
        identity = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if any(getattr(before, field) != getattr(after, field) for field in identity):
            return False
        return digest.hexdigest() == expected
    except (OSError, ValueError):
        return False


def _reject_duplicate_json_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON field: {key}")
        result[key] = value
    return result


def _validate_auxiliary_health(
    row: dict[str, Any],
    enabled: bool,
    contract: dict[str, Any],
    *,
    observation_date: date | None = None,
) -> dict[str, Any]:
    last_result = row.get("last_task_result")
    trigger_start = _parse_aware_datetime(str(row.get("trigger_start_boundary") or ""))
    next_run = _parse_aware_datetime(str(row.get("next_run_time") or ""))
    trigger_start_local = trigger_start.astimezone(SCHEDULE_TIMEZONE) if trigger_start else None
    next_run_local = next_run.astimezone(SCHEDULE_TIMEZONE) if next_run else None
    observed_day = observation_date or datetime.now(SCHEDULE_TIMEZONE).date()
    checks = {
        "last_task_result_acceptable": _acceptable_last_result(last_result),
        "trigger_count_matches": type(row.get("trigger_count")) is int
        and row.get("trigger_count") == 1,
        "trigger_type_matches": row.get("trigger_type") == "MSFT_TaskWeeklyTrigger",
        "trigger_enabled_matches": row.get("trigger_enabled") is True,
        "trigger_days_of_week_matches": row.get("trigger_days_of_week") == 62,
        "trigger_weeks_interval_matches": row.get("trigger_weeks_interval") == 1,
        "trigger_end_boundary_absent": not str(row.get("trigger_end_boundary") or ""),
        "trigger_random_delay_absent": not str(row.get("trigger_random_delay") or ""),
        "trigger_start_boundary_valid": trigger_start is not None,
        "next_run_time_valid": next_run is not None,
        "schedule_time_matches": (
            trigger_start_local is not None
            and trigger_start_local.hour == 15
            and trigger_start_local.minute == 20
            and trigger_start_local.second == 0
            and trigger_start_local.microsecond == 0
        ),
        "schedule_weekday_matches": (
            trigger_start_local is not None
            and trigger_start_local.weekday() < 5
            and next_run_local is not None
            and next_run_local.weekday() < 5
        ),
        "trigger_active_on_observation_date": (
            trigger_start_local is not None and trigger_start_local.date() <= observed_day
        ),
        "next_run_schedule_time_matches": (
            next_run_local is not None
            and next_run_local.hour == 15
            and next_run_local.minute == 20
            and next_run_local.second == 0
            and next_run_local.microsecond == 0
        ),
        "next_run_future": (
            next_run_local is not None
            and trigger_start_local is not None
            and next_run_local > trigger_start_local
            and next_run_local.date() >= observed_day
        ),
        "host_timezone_matches": row.get("host_timezone_id") == EXPECTED_WINDOWS_TIMEZONE_ID,
        "multiple_instances_matches": row.get("multiple_instances") == EXPECTED_MULTIPLE_INSTANCES,
        "execution_time_limit_matches": row.get("execution_time_limit") == "PT3H",
        "restart_count_matches": row.get("restart_count") == 3,
        "restart_interval_matches": row.get("restart_interval") == "PT15M",
        "noninteractive": str(row.get("logon_type") or "") in NONINTERACTIVE_LOGON_TYPES,
        "run_level_matches": row.get("run_level") == EXPECTED_RUN_LEVEL,
        "start_when_available": row.get("start_when_available") is True,
        "wake_to_run": row.get("wake_to_run") is True,
        "battery_safe": (
            row.get("stop_if_going_on_batteries") is False
            and row.get("disallow_start_if_on_batteries") is False
        ),
        "principal_contract_matches": row.get("principal_sha256")
        == contract.get("principal_sha256"),
        "trigger_contract_matches": row.get("trigger_sha256") == contract.get("trigger_sha256"),
        "settings_contract_matches": row.get("settings_sha256") == contract.get("settings_sha256"),
        "definition_contract_matches": row.get("definition_contract_sha256")
        == contract.get("definition_contract_sha256"),
    }
    structural_checks = {
        name: value for name, value in checks.items() if name.endswith("_contract_matches")
    }
    structural_valid = all(structural_checks.values())
    operational_health_valid = enabled and all(checks.values())
    checks["definition_integrity_valid"] = structural_valid
    checks["operational_health_valid"] = operational_health_valid
    if not enabled:
        return {
            "valid": structural_valid,
            "reason": "auxiliary task definition lineage is invalid"
            if not structural_valid
            else "",
            **checks,
        }
    return {
        "valid": checks["operational_health_valid"],
        "reason": "auxiliary task is not operationally unattended-safe"
        if not checks["operational_health_valid"]
        else "",
        **checks,
    }


def _acceptable_last_result(value: Any) -> bool:
    return value is None or (type(value) is int and value in ACCEPTABLE_LAST_RESULTS)


def _validate_auxiliary_action(
    row: dict[str, Any], runtime: Path, state: Path, contract: dict[str, Any]
) -> dict[str, Any]:
    execute = str(row.get("execute") or "")
    arguments = str(row.get("arguments") or "")
    working_directory = str(row.get("working_directory") or "")
    try:
        tokens = [token.strip('"') for token in shlex.split(arguments, posix=False)]
    except ValueError:
        tokens = []
    prefix_matches = (
        len(tokens) >= 18
        and tuple(tokens[:4]) == AUXILIARY_PYTHON_PREFIX
        and tokens[4].startswith("pycache_prefix=")
        and tokens[5] == "-u"
        and tokens[6] == "-c"
        and tokens[7] == AUXILIARY_BOOTSTRAP_PRELOADER
    )
    bootstrap = tokens[8] if prefix_matches else None
    bootstrap_sha = tokens[9] if prefix_matches else None
    release_root = tokens[11] if prefix_matches and tokens[10] == "--release-root" else None
    expected_sha = tokens[13] if prefix_matches and tokens[12] == "--expected-sha" else None
    runner = tokens[15] if prefix_matches and tokens[14] == "--script" else None
    separator_matches = prefix_matches and tokens[16] == "--"
    prefix_matches = separator_matches and not any(
        token in (*AUXILIARY_PYTHON_PREFIX, "-u") for token in tokens[12:]
    )
    option_values: dict[str, str] = {}
    option_order: list[str] = []
    duplicate_options: set[str] = set()
    unknown_options: set[str] = set()
    unexpected_arguments = False
    option_index = 17 if prefix_matches else len(tokens)
    while option_index < len(tokens):
        option = tokens[option_index]
        if not option.startswith("--"):
            unexpected_arguments = True
            option_index += 1
            continue
        option_order.append(option)
        if option not in AUXILIARY_REQUIRED_OPTIONS and option != "--execute":
            unknown_options.add(option)
        if option in option_values:
            duplicate_options.add(option)
        if option == "--execute":
            option_values[option] = ""
            option_index += 1
            continue
        if option_index + 1 >= len(tokens) or tokens[option_index + 1].startswith("--"):
            option_values[option] = ""
            option_index += 1
            continue
        option_values[option] = tokens[option_index + 1]
        option_index += 2
    candidate = option_values.get("--candidate-sha")
    repo_root = option_values.get("--repo-root")
    env_file = option_values.get("--env-file")
    hash_options_valid = all(
        SHA256_PATTERN.fullmatch(option_values.get(name, "")) is not None
        for name in (
            "--symbols-manifest-sha256",
            "--entitlement-receipt-sha256",
            "--source-config-sha256",
        )
    )
    candidate_format_valid = GIT_SHA_PATTERN.fullmatch(candidate or "") is not None
    required_options_present = (
        not duplicate_options
        and not unknown_options
        and not unexpected_arguments
        and tuple(option_order)
        == (*AUXILIARY_REQUIRED_OPTION_ORDER, "--execute")
        and set(option_values) == AUXILIARY_REQUIRED_OPTIONS | {"--execute"}
        and AUXILIARY_REQUIRED_OPTIONS.issubset(option_values)
        and option_values.get("--execute") == ""
        and all(
            option_values.get(name) for name in AUXILIARY_REQUIRED_OPTIONS if name != "--execute"
        )
        and hash_options_valid
        and candidate_format_valid
        and option_values.get("--max-pages") == "100"
        and option_values.get("--retries") == "3"
    )
    external_path_options = (
        "--db-path",
        "--evidence-root",
        "--run-root",
        "--output-root",
        "--session-root",
        "--symbols-manifest",
        "--entitlement-receipt",
        "--source-config",
    )
    task_path_matches = str(row.get("task_path") or "\\") == "\\"
    action_count_matches = row.get("action_count") == 1
    state_matches = str(row.get("state") or "") in {
        "Ready",
        "Running",
        "Queued",
        "Disabled",
    }
    try:
        runtime_root_matches = (
            Path(working_directory).resolve() == runtime
            and repo_root is not None
            and Path(repo_root).resolve() == runtime
            and release_root is not None
            and Path(release_root).resolve() == runtime
        )
        external_values = [option_values.get(name, "") for name in external_path_options]
        external_paths = [Path(value).resolve() for value in external_values]
        external_paths_are_distinct = all(external_values) and all(
            left != right and not left.is_relative_to(right) and not right.is_relative_to(left)
            for index, left in enumerate(external_paths)
            for right in external_paths[index + 1 :]
        )
        state_root_matches = (
            required_options_present
            and all(
                Path(option_values[name]).is_absolute()
                for name in AUXILIARY_REQUIRED_OPTIONS
                if name
                not in {
                    "--candidate-sha",
                    "--max-pages",
                    "--retries",
                    "--symbols-manifest-sha256",
                    "--entitlement-receipt-sha256",
                    "--source-config-sha256",
                }
            )
            and all(
                not (
                    Path(option_values[name]).resolve().is_relative_to(runtime)
                    or Path(option_values[name]).resolve().is_relative_to(state)
                )
                for name in external_path_options
            )
            and external_paths_are_distinct
        )
        bootstrap_matches = (
            bootstrap is not None
            and Path(bootstrap).resolve() == runtime / AUXILIARY_PYTHON_BOOTSTRAP
            and _safe_regular_path(Path(bootstrap))
        )
        bootstrap_sha_matches = (
            bootstrap is not None
            and _safe_file_sha256(bootstrap, bootstrap_sha)
        )
        runner_matches = (
            runner is not None
            and Path(runner).resolve() == runtime / AUXILIARY_CAPTURE_RUNNER
            and _safe_regular_path(Path(runner))
        )
        env_file_matches = (
            env_file is not None
            and Path(env_file).resolve() == state / "secrets" / "runtime.env"
            and _safe_regular_path(Path(env_file))
        )
    except (OSError, RuntimeError):
        runtime_root_matches = False
        state_root_matches = False
        bootstrap_matches = False
        bootstrap_sha_matches = False
        runner_matches = False
        env_file_matches = False
    candidate_sha_matches = candidate == contract["candidate_sha"]
    expected_sha_matches = expected_sha == contract["candidate_sha"]
    bytecode_prefix_matches = (
        candidate_format_valid
        and len(tokens) >= 5
        and tokens[4]
        == "pycache_prefix=" + str((state / "capture-bytecode" / str(candidate)).resolve())
    )
    prefix_matches = prefix_matches and bytecode_prefix_matches
    input_hashes_match = all(
        option_values.get(option) == contract.get(contract_key)
        for option, contract_key in (
            ("--symbols-manifest-sha256", "symbols_manifest_sha256"),
            ("--entitlement-receipt-sha256", "entitlement_receipt_sha256"),
            ("--source-config-sha256", "source_config_sha256"),
        )
    )
    input_files_match = all(
        _safe_file_sha256(option_values.get(option, ""), contract.get(contract_key))
        for option, contract_key in (
            ("--symbols-manifest", "symbols_manifest_sha256"),
            ("--entitlement-receipt", "entitlement_receipt_sha256"),
            ("--source-config", "source_config_sha256"),
        )
    )
    action_text = "|".join((execute, arguments, working_directory))
    action_hash = hashlib.sha256(action_text.encode("utf-8")).hexdigest()
    action_contract_matches = action_hash == contract["action_contract_sha256"]
    try:
        interpreter = Path(execute)
        interpreter_matches = (
            interpreter.is_absolute()
            and interpreter.resolve() == AUXILIARY_INTERPRETER.resolve()
            and _safe_file_sha256(str(interpreter), AUXILIARY_INTERPRETER_SHA256)
        )
    except (OSError, RuntimeError):
        interpreter_matches = False
    valid = all(
        (
            interpreter_matches,
            bootstrap_matches,
            bootstrap_sha_matches,
            runner_matches,
            prefix_matches,
            required_options_present,
            runtime_root_matches,
            state_root_matches,
            env_file_matches,
            task_path_matches,
            action_count_matches,
            state_matches,
            candidate_sha_matches,
            expected_sha_matches,
            input_hashes_match,
            input_files_match,
            action_contract_matches,
        )
    )
    return {
        "valid": valid,
        "reason": "" if valid else "auxiliary action/root/SHA contract is invalid",
        "action_contract_matches": action_contract_matches,
        "runtime_root_matches": runtime_root_matches,
        "state_root_matches": state_root_matches,
        "env_file_matches": env_file_matches,
        "candidate_sha_matches": candidate_sha_matches,
        "input_hashes_match": input_hashes_match,
        "input_files_match": input_files_match,
        "bootstrap_matches": bootstrap_matches,
        "bootstrap_sha_matches": bootstrap_sha_matches,
        "expected_sha_matches": expected_sha_matches,
        "runner_matches": runner_matches,
        "prefix_matches": prefix_matches,
        "task_path_matches": task_path_matches,
        "action_count_matches": action_count_matches,
        "state_matches": state_matches,
    }


def _expected_action_arguments(
    task_name: str,
    *,
    expected_runner: Path,
    runtime_root: Path,
    state_root: Path,
    expected_sha: str = "",
) -> str:
    arguments = (
        f'-NoProfile -ExecutionPolicy Bypass -File "{expected_runner}" '
        f'-RuntimeRoot "{runtime_root}" -StateRoot "{state_root}" -ExpectedSha "{expected_sha}"'
    )
    if task_name == CANONICAL_TASK_NAME:
        arguments += f' -PublicationMode Production -VercelProjectId "{EXPECTED_VERCEL_PROJECT_ID}"'
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
        "function Get-DawnstrikeQuerySha256([string]$Text) { "
        "$sha = [System.Security.Cryptography.SHA256]::Create(); "
        "try { $bytes = [System.Text.Encoding]::UTF8.GetBytes($Text); "
        "return ([System.BitConverter]::ToString($sha.ComputeHash($bytes))).Replace('-', '')."
        "ToLowerInvariant() } "
        "finally { $sha.Dispose() } }; "
        "function Get-DawnstrikeQuerySectionHash([string]$Xml, [string]$Name) { "
        "if ([string]::IsNullOrWhiteSpace($Xml)) { return $null }; "
        "try { $doc = [System.Xml.XmlDocument]::new(); $doc.PreserveWhitespace = $true; "
        "$doc.LoadXml($Xml); $nodes = @($doc.SelectNodes(\"//*[local-name()='$Name']\")); "
        "if ($nodes.Count -ne 1) { return $null }; "
        "return Get-DawnstrikeQuerySha256 ([string]$nodes[0].OuterXml) } catch { return $null } }; "
        "function Get-DawnstrikeQueryDefinitionHash([string]$Xml) { "
        "if ([string]::IsNullOrWhiteSpace($Xml)) { return $null }; "
        "try { $doc = [System.Xml.XmlDocument]::new(); "
        "$doc.PreserveWhitespace = $false; $doc.LoadXml($Xml); "
        "$namespace = [string]$doc.DocumentElement.NamespaceURI; "
        "if ([string]::IsNullOrWhiteSpace($namespace)) { "
        "$nodes = @($doc.SelectNodes('/Task/Settings/Enabled')) } "
        "else { $manager = [System.Xml.XmlNamespaceManager]::new($doc.NameTable); "
        "$manager.AddNamespace('task', $namespace); "
        "$nodes = @($doc.SelectNodes('/task:Task/task:Settings/task:Enabled', $manager)) }; "
        "if ($nodes.Count -gt 1) { return $null }; "
        "if ($nodes.Count -eq 1) { $null = $nodes[0].ParentNode.RemoveChild($nodes[0]) }; "
        "return Get-DawnstrikeQuerySha256 ([string]$doc.OuterXml) } catch { return $null } }; "
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
        "$taskXml = $null; if (Get-Command Export-ScheduledTask -ErrorAction SilentlyContinue) { "
        "try { $taskXml = [string](Export-ScheduledTask -TaskName $task.TaskName "
        "-TaskPath $task.TaskPath -ErrorAction Stop) } catch { $taskXml = $null } }; "
        "[pscustomobject]@{name=$task.TaskName; task_path=$task.TaskPath; "
        "state=$task.State.ToString(); enabled=[bool]$task.Settings.Enabled; "
        "action_count=$actions.Count; trigger_count=$triggers.Count; "
        "host_timezone_id=$hostTimeZoneId; "
        "logon_type=$task.Principal.LogonType.ToString(); "
        "principal_user_id=[string]$task.Principal.UserId; "
        "run_level=$task.Principal.RunLevel.ToString(); "
        "start_when_available=[bool]$task.Settings.StartWhenAvailable; "
        "wake_to_run=[bool]$task.Settings.WakeToRun; "
        "stop_if_going_on_batteries=[bool]$task.Settings.StopIfGoingOnBatteries; "
        "disallow_start_if_on_batteries=[bool]$task.Settings.DisallowStartIfOnBatteries; "
        "execution_time_limit=[string]$task.Settings.ExecutionTimeLimit; "
        "restart_count=[int]$task.Settings.RestartCount; "
        "restart_interval=[string]$task.Settings.RestartInterval; "
        "multiple_instances=[string]$task.Settings.MultipleInstances; "
        "principal_sha256=Get-DawnstrikeQuerySectionHash $taskXml 'Principal'; "
        "trigger_sha256=Get-DawnstrikeQuerySectionHash $taskXml 'Triggers'; "
        "settings_sha256=Get-DawnstrikeQuerySectionHash $taskXml 'Settings'; "
        "definition_contract_sha256=Get-DawnstrikeQueryDefinitionHash $taskXml; "
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
        "principal_user_id=$null; "
        "principal_sha256=$null; trigger_sha256=$null; settings_sha256=$null; "
        "definition_contract_sha256=$null; "
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
        completed = subprocess.run(  # nosec B603, B607
            [EXPECTED_TASK_EXECUTABLE, "-NoProfile", "-Command", script],
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
                "detail": (completed.stderr.strip() or "Task Scheduler query failed."),
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
        "principal_user_id": None,
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


def _history_superseded_by_exact_runtime_activation(
    *,
    last_run_time: str,
    activation_completed_at: datetime | None,
) -> bool:
    """Return true only for failure history superseded by exact activation."""

    last_run = _parse_aware_datetime(last_run_time)
    if last_run is None or activation_completed_at is None:
        return False
    return activation_completed_at > last_run


def _load_exact_activation_completion(runtime: Path, state: Path) -> datetime | None:
    """Load one strict COMPLETE receipt that supersedes prior task history.

    A missing, malformed, stale, or ambiguous receipt is deliberately treated
    as no supersession.  This keeps a preserved failed task result blocking
    until the runtime activation itself provides a fresh, exact proof.
    """

    runtime_contract = _runtime_git_contract(runtime)
    if runtime_contract is None:
        return None
    runtime_sha = runtime_contract["candidate_sha"]
    runtime_tree = runtime_contract["candidate_tree"]
    runtime_origin_sha = runtime_contract["runtime_origin_sha256"]
    try:
        from scripts.runtime_activation_contract import (
            _assert_no_reparse_components,
            load_receipt,
        )
    except ImportError:
        return None
    matches: list[datetime] = []
    receipt_root = state / "receipts" / "runtime-activation"
    try:
        paths = sorted(receipt_root.glob("runtime-activation-*.json"))
    except OSError:
        return None
    for path in paths:
        name_match = re.fullmatch(r"runtime-activation-([0-9a-f]{24})\.json", path.name)
        if name_match is None:
            # Prepared, failure, compensation, and other governed sidecars are
            # not activation-completion receipts and must not poison exact
            # COMPLETE-history supersession.
            continue
        try:
            _assert_no_reparse_components(path)
            payload = load_receipt(path)
            activation_id = name_match.group(1)
            if str(payload.get("activation_id") or "") != activation_id:
                return None
            if (
                payload.get("status") != "COMPLETE"
                or payload.get("candidate_sha") != runtime_sha
                or payload.get("candidate_tree") != runtime_tree
            ):
                continue
            if payload.get("runtime_origin_sha256") != runtime_origin_sha:
                continue
            completed = _parse_aware_datetime(str(payload.get("completed_at_utc") or ""))
            if completed is None:
                return None
            matches.append(completed)
        except (OSError, TypeError, ValueError):
            return None
    if _runtime_git_contract(runtime) != runtime_contract:
        return None
    return matches[0] if len(matches) == 1 else None


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
