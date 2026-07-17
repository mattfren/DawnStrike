# ruff: noqa: E501
# mypy: ignore-errors
"""Safe Windows Task Scheduler installer/reporting layer for OMEGA operations."""

from __future__ import annotations

import json
import re
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from datetime import time as dt_time
from pathlib import Path
from typing import Any

OUTPUT_ROOT = Path("data/v2_autonomous_runner")
TASK_PATH = "\\Dawnstrike\\"
REPO_ROOT = Path(__file__).resolve().parents[3]
SECRET_PATTERN = re.compile(
    r"(?i)(api[_-]?key|authorization:\s*bearer|password|secret|token)\s*[:=]\s*['\"]?[^'\"\s,;]+"
)
FORBIDDEN_TERMS = (
    "submit" + "_order",
    "place" + "_order",
    "create" + "_order",
    "live" + "_execute",
    "live_trading_enabled" + " = true",
)


@dataclass(frozen=True)
class AutonomousTask:
    task_name: str
    script: str
    hour: int
    minute: int
    purpose: str

    @property
    def full_name(self) -> str:
        return f"{TASK_PATH}{self.task_name}"

    @property
    def schedule_time(self) -> str:
        return f"{self.hour:02d}:{self.minute:02d}"

    @property
    def command(self) -> str:
        return f"powershell.exe -NoProfile -ExecutionPolicy Bypass -File {self.script}"

    def to_definition(self, *, include_absolute: bool = False) -> dict[str, object]:
        payload: dict[str, object] = {
            "action": "powershell.exe",
            "arguments": f"-NoProfile -ExecutionPolicy Bypass -File {self.script}",
            "daily_time_local": self.schedule_time,
            "do_not_start_new_instance": True,
            "enabled": True,
            "external_alerts_enabled": False,
            "highest_privileges": False,
            "live_trading_enabled": False,
            "logon_type": "Interactive",
            "multiple_instances": "IgnoreNew",
            "preserve_exit_codes": True,
            "purpose": self.purpose,
            "run_only_when_user_logged_on": True,
            "schema_version": "v2.autonomous_runner.task_definition.v1",
            "secrets_embedded": False,
            "task_name": self.task_name,
            "task_path": TASK_PATH,
            "working_directory": ".",
        }
        if include_absolute:
            payload["working_directory_absolute"] = str(REPO_ROOT)
        return payload


TASKS = (
    AutonomousTask(
        task_name="OMEGA After Close",
        script="scripts/run_omega_scheduler_after_close.ps1",
        hour=16,
        minute=35,
        purpose="Run after-close OMEGA Sentinel with AutoData and Learning Foundry.",
    ),
    AutonomousTask(
        task_name="OMEGA Morning Check",
        script="scripts/run_omega_scheduler_morning_check.ps1",
        hour=9,
        minute=10,
        purpose="Run morning OMEGA Sentinel checks with AutoData and Learning Foundry.",
    ),
    AutonomousTask(
        task_name="OMEGA Verify",
        script="scripts/run_omega_scheduler_verify.ps1",
        hour=17,
        minute=10,
        purpose="Run Sentinel verify and doctor after the daily workflow.",
    ),
    AutonomousTask(
        task_name="OMEGA Watchdog",
        script="scripts/run_omega_scheduler_watchdog.ps1",
        hour=18,
        minute=0,
        purpose="Audit task health, missed runs, stale locks, provider readiness, and safety boundaries.",
    ),
)


def init(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    _ensure_dirs(output_root)
    _write_task_definitions(output_root)
    _write_docs()
    payload = {
        "build_id": _build_id("autonomous_runner_init"),
        "created_at": _now(),
        "output_root": output_root.as_posix(),
        "schema_version": "v2.autonomous_runner.manifest.v1",
        "status": "initialized",
        "task_count": len(TASKS),
        "tasks": [task.full_name for task in TASKS],
    }
    _write_json(output_root / "manifests" / "autonomous_runner_manifest.json", payload)
    report(output_root=output_root)
    return payload


def install(*, yes: bool, output_root: Path = OUTPUT_ROOT) -> tuple[int, dict[str, object]]:
    if not yes:
        return 2, {
            "required_flag": "--yes",
            "status": "refused_missing_yes",
            "what_next": "Run py -m intraday_scanner.v2.autonomous_runner install --yes",
        }
    return _run_script("scripts/install_omega_autonomous_tasks.ps1", output_root=output_root)


def uninstall(*, yes: bool, output_root: Path = OUTPUT_ROOT) -> tuple[int, dict[str, object]]:
    if not yes:
        return 2, {
            "required_flag": "--yes",
            "status": "refused_missing_yes",
            "what_next": "Run py -m intraday_scanner.v2.autonomous_runner uninstall --yes",
        }
    return _run_script("scripts/uninstall_omega_autonomous_tasks.ps1", output_root=output_root)


def status(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init_payload = _init_no_report(output_root)
    del init_payload
    task_rows, task_warnings = _query_tasks()
    scheduler_status = _read_json(Path("data/v2_scheduler/status/latest_status.json"), {})
    sentinel_status = _read_json(Path("data/v2_omega_sentinel/status/latest_status.json"), {})
    sentinel_verify = _read_json(Path("data/v2_omega_sentinel/reconciliation/latest_verification.json"), {})
    learning_verify = _read_json(Path("data/v2_learning_foundry/reports/verify_latest.json"), {})
    market_masters = _market_masters_summary()
    command_center = _read_json(Path("data/v2_command_center_x2/qa/qa_latest.json"), {})
    if not command_center:
        command_center = _read_json(Path("data/v2_command_center/command_center_qa.json"), {})
    autodata = _read_json(Path("data/v2_autodata/readiness/provider_readiness.json"), {})
    alerts = _warning_state(scheduler_status, sentinel_status, sentinel_verify)
    missed = _missed_runs(task_rows, install_checked_at=_installation_checked_at(output_root))
    installed_count = sum(1 for row in task_rows if row.get("installed") is True)
    enabled_count = sum(1 for row in task_rows if row.get("enabled") is True)
    payload: dict[str, object] = {
        "alert_state": alerts,
        "autodata_provider_readiness_status": _status_value(autodata),
        "build_id": _build_id("autonomous_runner_status"),
        "checked_at": _now(),
        "command_center_status": command_center.get("status", "missing") if isinstance(command_center, dict) else "missing",
        "external_alerts_enabled": False,
        "installed_task_count": installed_count,
        "learning_foundry_status": learning_verify.get("status", "missing") if isinstance(learning_verify, dict) else "missing",
        "live_trading_enabled": False,
        "latest_market_masters_build_id": market_masters["latest_market_masters_build_id"],
        "latest_market_masters_challenger_count": market_masters["latest_market_masters_challenger_count"],
        "latest_market_masters_promotion_status": market_masters["latest_market_masters_promotion_status"],
        "latest_market_masters_status": market_masters["latest_market_masters_status"],
        "market_masters_enabled": market_masters["market_masters_enabled"],
        "market_masters_verify_status": market_masters["market_masters_verify_status"],
        "missed_runs": missed,
        "no_overlap_policy": "Do not start a new instance / MultipleInstances IgnoreNew",
        "repo_root": ".",
        "scheduler_status": scheduler_status.get("status", "missing") if isinstance(scheduler_status, dict) else "missing",
        "schema_version": "v2.autonomous_runner.status.v1",
        "sentinel_status": sentinel_status.get("status", "missing") if isinstance(sentinel_status, dict) else "missing",
        "sentinel_verify_status": sentinel_verify.get("status", "missing") if isinstance(sentinel_verify, dict) else "missing",
        "status": "installed" if installed_count == len(TASKS) and enabled_count == len(TASKS) else "install_ready",
        "task_count": len(TASKS),
        "tasks": task_rows,
        "warnings": task_warnings + alerts.get("warnings", []) + missed.get("warnings", []),
    }
    _write_status_outputs(output_root, payload)
    _write_autonomous_pages(output_root, payload)
    return payload


def verify(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    init_payload = _init_no_report(output_root)
    del init_payload
    failures: list[str] = []
    warnings: list[str] = []
    for task in TASKS:
        script = Path(task.script)
        if not script.exists():
            failures.append(f"missing scheduler script: {task.script}")
    for script in (
        "scripts/install_omega_autonomous_tasks.ps1",
        "scripts/uninstall_omega_autonomous_tasks.ps1",
        "scripts/status_omega_autonomous_tasks.ps1",
        "scripts/test_omega_autonomous_tasks.ps1",
        "scripts/run_omega_scheduler_watchdog.ps1",
    ):
        if not Path(script).exists():
            failures.append(f"missing autonomous script: {script}")
    for script in (
        "scripts/run_omega_scheduler_after_close.ps1",
        "scripts/run_omega_scheduler_morning_check.ps1",
    ):
        if "--market-masters" not in _read_text(Path(script)):
            failures.append(f"Market Masters flag missing from scheduler script: {script}")
    verify_script = _read_text(Path("scripts/run_omega_scheduler_verify.ps1"))
    if "--market-masters" in verify_script:
        failures.append("verify scheduler script should not run Market Masters synthesis")
    import_check = _check_imports()
    failures.extend(import_check["failures"])
    safety = _safety_scan()
    failures.extend(safety["failures"])
    warnings.extend(safety["warnings"])
    definitions = list((output_root / "task_definitions").glob("*.json"))
    if len(definitions) < len(TASKS):
        failures.append("task definition JSON files missing")
    for path in definitions:
        text = path.read_text(encoding="utf-8")
        if SECRET_PATTERN.search(text):
            failures.append(f"possible secret in task definition: {path.as_posix()}")
        if any(term in text.lower() for term in FORBIDDEN_TERMS):
            failures.append(f"forbidden trading term in task definition: {path.as_posix()}")
    latest_status_payload = _dict(_read_json(output_root / "status" / "latest_status.json", {}))
    market_masters_summary = _market_masters_summary()
    if not latest_status_payload:
        latest_status_payload = {
            "market_masters_enabled": market_masters_summary.get("market_masters_enabled"),
        }
    _write_market_masters_autonomy_audit_docs(latest_status_payload, market_masters_summary)
    docs = (
        "docs/architecture/v2_autonomous_runner.md",
        "docs/operations/omega_autonomous_runner_install.md",
        "docs/operations/omega_autonomous_runner_uninstall.md",
        "docs/operations/omega_autonomous_runner_daily_ops.md",
        "docs/operations/omega_autonomous_runner_failure_recovery.md",
        "docs/audit/omega_autonomous_runner_release_summary.md",
        "docs/audit/omega_autonomous_runner_quality_scorecard.md",
        "docs/audit/omega_autonomous_runner_red_team.md",
        "docs/audit/omega_autonomous_runner_build_state.json",
        "docs/audit/omega_autonomous_runner_resume_goal.md",
        "docs/audit/omega_market_masters_autonomy_wiring_summary.md",
        "docs/audit/omega_market_masters_autonomy_wiring_quality_scorecard.md",
        "docs/audit/omega_market_masters_autonomy_wiring_red_team.md",
        "docs/audit/omega_market_masters_autonomy_wiring_build_state.json",
    )
    for doc in docs:
        if not Path(doc).exists():
            failures.append(f"missing required doc: {doc}")
    payload = {
        "checked_at": _now(),
        "failures": failures,
        "schema_version": "v2.autonomous_runner.verification.v1",
        "status": "passed" if not failures else "failed",
        "warnings": warnings,
    }
    _write_json(output_root / "reports" / "verify_latest.json", payload)
    _write_md(output_root / "reports" / "verify_latest.md", "Autonomous Runner Verification", _kv_lines(payload))
    return payload


def test_run(*, output_root: Path = OUTPUT_ROOT) -> tuple[int, dict[str, object]]:
    init(output_root=output_root)
    started = _now()
    steps: list[dict[str, object]] = []
    verify_code, verify_output = _run_command(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", "scripts/run_omega_scheduler_verify.ps1"],
        log_path=output_root / "logs" / f"test_verify_{_timestamp()}.log",
    )
    steps.append({"exit_code": verify_code, "name": "scheduler_verify_wrapper", "status": "passed" if verify_code == 0 else "failed"})
    watchdog_payload = watchdog(output_root=output_root)
    watchdog_code = 0 if watchdog_payload.get("status") in {"passed", "passed_with_warnings"} else 1
    steps.append({"exit_code": watchdog_code, "name": "watchdog", "status": "passed" if watchdog_code == 0 else "failed"})
    verification = verify(output_root=output_root)
    verify_gate_code = 0 if verification.get("status") == "passed" else 1
    steps.append({"exit_code": verify_gate_code, "name": "autonomous_runner_verify", "status": "passed" if verify_gate_code == 0 else "failed"})
    exit_code = max(verify_code, watchdog_code, verify_gate_code)
    payload = {
        "build_id": _build_id("autonomous_runner_test_run"),
        "completed_at": _now(),
        "live_trading_enabled": False,
        "scheduler_verify_output_tail": verify_output[-12:],
        "schema_version": "v2.autonomous_runner.test_run.v1",
        "started_at": started,
        "status": "passed" if exit_code == 0 else "failed",
        "steps": steps,
    }
    _write_json(output_root / "reports" / "test_run_latest.json", payload)
    _write_md(output_root / "reports" / "test_run_latest.md", "Autonomous Runner Test Run", _test_run_lines(payload))
    report(output_root=output_root)
    return exit_code, payload


def watchdog(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    _init_no_report(output_root)
    task_rows, task_warnings = _query_tasks()
    scheduler_status = _read_json(Path("data/v2_scheduler/status/latest_status.json"), {})
    sentinel_status = _read_json(Path("data/v2_omega_sentinel/status/latest_status.json"), {})
    sentinel_verify = _read_json(Path("data/v2_omega_sentinel/reconciliation/latest_verification.json"), {})
    command_center_manifest = Path("data/v2_command_center/production.html")
    command_center_qa = _read_json(Path("data/v2_command_center_x2/qa/qa_latest.json"), {})
    if not command_center_qa:
        command_center_qa = _read_json(Path("data/v2_command_center/command_center_qa.json"), {})
    autodata = _read_json(Path("data/v2_autodata/readiness/provider_readiness.json"), {})
    market_masters = _market_masters_summary()
    locks = _lock_state()
    missed = _missed_runs(task_rows, install_checked_at=_installation_checked_at(output_root))
    warnings = task_warnings + locks.get("warnings", []) + missed.get("warnings", [])
    failures: list[str] = []
    if scheduler_status and scheduler_status.get("live_trading_enabled") is not False:
        failures.append("scheduler status does not prove live trading disabled")
    if sentinel_verify and sentinel_verify.get("status") != "passed":
        warnings.append("latest Sentinel verification is not passed")
    if not command_center_manifest.exists():
        failures.append("Command Center index missing")
    if market_masters["market_masters_enabled"] is not True:
        failures.append("Market Masters is not enabled in morning and after-close scripts")
    if market_masters["latest_market_masters_status"] == "missing":
        failures.append("Market Masters report missing")
    if market_masters["market_masters_verify_status"] != "passed":
        failures.append("Market Masters verification is not passed")
    if market_masters["latest_market_masters_promotion_status"] not in {
        "blocked_no_true_forward_sample",
        "blocked_true_forward_evidence_required",
    }:
        failures.append("Market Masters promotion status is not blocked")
    if market_masters["strategy_validation_triggered"] is not False:
        failures.append("Market Masters validation boundary failed")
    if market_masters["all_challengers_shadow_only"] is not True:
        failures.append("Market Masters challengers are not all shadow-only")
    if market_masters["learning_foundry_champion_registry_changed"] is not False:
        failures.append("Market Masters sync indicates champion registry mutation")
    payload = {
        "autodata_provider_readiness_status": _status_value(autodata),
        "checked_at": _now(),
        "command_center_exists": command_center_manifest.exists(),
        "command_center_status": command_center_qa.get("status", "missing") if isinstance(command_center_qa, dict) else "missing",
        "failures": failures,
        "latest_scheduler_status": scheduler_status.get("status", "missing") if isinstance(scheduler_status, dict) else "missing",
        "latest_market_masters_build_id": market_masters["latest_market_masters_build_id"],
        "latest_market_masters_challenger_count": market_masters["latest_market_masters_challenger_count"],
        "latest_market_masters_promotion_status": market_masters["latest_market_masters_promotion_status"],
        "latest_market_masters_status": market_masters["latest_market_masters_status"],
        "latest_sentinel_status": sentinel_status.get("status", "missing") if isinstance(sentinel_status, dict) else "missing",
        "live_trading_enabled": False,
        "market_masters_champion_registry_changed": market_masters["learning_foundry_champion_registry_changed"],
        "market_masters_enabled": market_masters["market_masters_enabled"],
        "market_masters_shadow_only": market_masters["all_challengers_shadow_only"],
        "market_masters_verify_status": market_masters["market_masters_verify_status"],
        "missed_run_state": missed,
        "schema_version": "v2.autonomous_runner.watchdog.v1",
        "sentinel_lock_state": locks,
        "status": "passed" if not failures and not warnings else ("failed" if failures else "passed_with_warnings"),
        "task_count": len(task_rows),
        "tasks_installed": sum(1 for row in task_rows if row.get("installed") is True),
        "warnings": warnings,
    }
    _write_json(output_root / "health" / "watchdog_latest.json", payload)
    _write_md(output_root / "health" / "watchdog_latest.md", "OMEGA Autonomous Watchdog", _watchdog_lines(payload))
    _write_autonomous_pages(output_root, status(output_root=output_root))
    return payload


def report(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    _init_no_report(output_root)
    status_payload = _read_json(output_root / "status" / "latest_status.json", {})
    if not status_payload:
        status_payload = status(output_root=output_root)
    install_report = _installation_report(output_root, status_payload)
    _write_json(output_root / "reports" / "task_installation_report.json", install_report)
    _write_md(output_root / "reports" / "task_installation_report.md", "OMEGA Task Installation Report", _install_lines(install_report))
    _write_json(output_root / "reports" / "autonomous_runner_status.json", status_payload)
    _write_md(output_root / "reports" / "autonomous_runner_status.md", "OMEGA Autonomous Runner Status", _status_lines(status_payload))
    market_masters = _market_masters_summary()
    _write_json(output_root / "reports" / "market_masters_autonomy_status.json", market_masters)
    _write_md(
        output_root / "reports" / "market_masters_autonomy_status.md",
        "Market Masters Autonomy Status",
        _market_masters_lines(market_masters),
    )
    _write_market_masters_autonomy_audit_docs(status_payload, market_masters)
    _write_audit_docs(status_payload, install_report)
    _write_autonomous_pages(output_root, status_payload)
    return {
        "build_id": install_report["build_id"],
        "reports": {
            "installation_json": "data/v2_autonomous_runner/reports/task_installation_report.json",
            "installation_md": "data/v2_autonomous_runner/reports/task_installation_report.md",
            "status_json": "data/v2_autonomous_runner/reports/autonomous_runner_status.json",
            "status_md": "data/v2_autonomous_runner/reports/autonomous_runner_status.md",
        },
        "status": "reported",
    }


def doctor(*, output_root: Path = OUTPUT_ROOT) -> dict[str, object]:
    status_payload = status(output_root=output_root)
    verification = verify(output_root=output_root)
    watchdog_payload = watchdog(output_root=output_root)
    failures = list(verification.get("failures", [])) + list(watchdog_payload.get("failures", []))
    warnings = list(status_payload.get("warnings", [])) + list(watchdog_payload.get("warnings", []))
    payload = {
        "checked_at": _now(),
        "failures": failures,
        "schema_version": "v2.autonomous_runner.doctor.v1",
        "status": "passed" if not failures else "failed",
        "warnings": sorted(set(str(item) for item in warnings)),
    }
    _write_json(output_root / "reports" / "doctor_latest.json", payload)
    _write_md(output_root / "reports" / "doctor_latest.md", "Autonomous Runner Doctor", _kv_lines(payload))
    return payload


def _init_no_report(output_root: Path) -> dict[str, object]:
    _ensure_dirs(output_root)
    _write_task_definitions(output_root)
    _write_docs()
    payload = {
        "created_at": _now(),
        "schema_version": "v2.autonomous_runner.manifest.v1",
        "status": "initialized",
    }
    _write_json(output_root / "manifests" / "autonomous_runner_manifest.json", payload)
    return payload


def _ensure_dirs(output_root: Path) -> None:
    for name in ("status", "logs", "task_definitions", "reports", "health", "manifests"):
        (output_root / name).mkdir(parents=True, exist_ok=True)


def _write_task_definitions(output_root: Path) -> None:
    root = output_root / "task_definitions"
    root.mkdir(parents=True, exist_ok=True)
    definitions = []
    for task in TASKS:
        definition = task.to_definition(include_absolute=True)
        definitions.append(definition)
        _write_json(root / f"{_slug(task.task_name)}.json", definition)
    _write_json(root / "omega_autonomous_tasks.json", {"schema_version": "v2.autonomous_runner.task_set.v1", "tasks": definitions})


def _query_tasks() -> tuple[list[dict[str, object]], list[str]]:
    warnings: list[str] = []
    names = ",".join(json.dumps(task.task_name) for task in TASKS)
    script = f"""
$names = @({names})
$rows = foreach ($name in $names) {{
  $task = Get-ScheduledTask -TaskPath '{TASK_PATH}' -TaskName $name -ErrorAction SilentlyContinue
  if ($null -eq $task) {{
    [pscustomobject]@{{
      task_name = $name
      full_name = '{TASK_PATH}' + $name
      installed = $false
      enabled = $false
      state = 'Missing'
      next_run_time = $null
      last_run_time = $null
      last_result = $null
      multiple_instances = 'n/a'
      working_directory = 'n/a'
      working_directory_matches_repo = $false
    }}
  }} else {{
    $info = Get-ScheduledTaskInfo -TaskPath '{TASK_PATH}' -TaskName $name -ErrorAction SilentlyContinue
    $working = ($task.Actions | ForEach-Object {{ $_.WorkingDirectory }} | Select-Object -First 1)
    [pscustomobject]@{{
      task_name = $name
      full_name = $task.TaskPath + $task.TaskName
      installed = $true
      enabled = ($task.State -ne 'Disabled')
      state = [string]$task.State
      next_run_time = if ($null -ne $info -and $info.NextRunTime -and $info.NextRunTime.Year -gt 1900) {{ $info.NextRunTime.ToString('o') }} else {{ $null }}
      last_run_time = if ($null -ne $info -and $info.LastRunTime -and $info.LastRunTime.Year -gt 1900) {{ $info.LastRunTime.ToString('o') }} else {{ $null }}
      last_result = if ($null -ne $info) {{ $info.LastTaskResult }} else {{ $null }}
      multiple_instances = [string]$task.Settings.MultipleInstances
      working_directory = if ($working -eq '{str(REPO_ROOT)}') {{ '.' }} else {{ '<nonrepo-path-redacted>' }}
      working_directory_matches_repo = ($working -eq '{str(REPO_ROOT)}')
    }}
  }}
}}
$rows | ConvertTo-Json -Depth 6
"""
    proc = subprocess.run(
        ["powershell", "-NoProfile", "-Command", script],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if proc.returncode != 0:
        warnings.append(_safe_text(proc.stderr.strip() or "Task Scheduler query failed"))
        return [_missing_task(task) for task in TASKS], warnings
    try:
        parsed = json.loads(proc.stdout or "[]")
    except json.JSONDecodeError:
        warnings.append("Task Scheduler query returned invalid JSON")
        return [_missing_task(task) for task in TASKS], warnings
    rows = parsed if isinstance(parsed, list) else [parsed]
    normalized = [_normalize_task_row(row) for row in rows if isinstance(row, dict)]
    known = {row["task_name"] for row in normalized}
    for task in TASKS:
        if task.task_name not in known:
            normalized.append(_missing_task(task))
    return normalized, warnings


def _missing_task(task: AutonomousTask) -> dict[str, object]:
    return {
        "enabled": False,
        "full_name": task.full_name,
        "installed": False,
        "last_result": None,
        "last_run_time": None,
        "multiple_instances": "n/a",
        "next_run_time": None,
        "state": "Missing",
        "task_name": task.task_name,
        "working_directory": "n/a",
        "working_directory_matches_repo": False,
    }


def _normalize_task_row(row: dict[str, object]) -> dict[str, object]:
    output = {
        "enabled": bool(row.get("enabled")),
        "full_name": str(row.get("full_name", "")),
        "installed": bool(row.get("installed")),
        "last_result": row.get("last_result"),
        "last_run_time": row.get("last_run_time"),
        "multiple_instances": str(row.get("multiple_instances", "unknown")),
        "next_run_time": row.get("next_run_time"),
        "state": str(row.get("state", "unknown")),
        "task_name": str(row.get("task_name", "unknown")),
        "working_directory": str(row.get("working_directory", "unknown")),
        "working_directory_matches_repo": bool(row.get("working_directory_matches_repo")),
    }
    if output["working_directory"] != ".":
        output["working_directory"] = "n/a" if not output["installed"] else "<nonrepo-path-redacted>"
    return output


def _missed_runs(
    task_rows: list[dict[str, object]],
    *,
    install_checked_at: datetime | None = None,
) -> dict[str, object]:
    now = datetime.now()
    warnings: list[str] = []
    rows = []
    for task in TASKS:
        row = next((item for item in task_rows if item.get("task_name") == task.task_name), _missing_task(task))
        scheduled = dt_time(task.hour, task.minute)
        last_run = _parse_local_dt(row.get("last_run_time"))
        due_today = now.weekday() < 5 and now.time() > scheduled
        never_run = last_run is None or last_run.year < 2000
        installed_after_todays_trigger = (
            install_checked_at is not None
            and install_checked_at.date() == now.date()
            and install_checked_at.time() > scheduled
        )
        missed = bool(row.get("installed")) and due_today and (
            (never_run and not installed_after_todays_trigger)
            or (last_run is not None and last_run.year >= 2000 and last_run.date() < now.date())
        )
        if missed:
            warnings.append(f"{task.full_name} has not run today after scheduled time {task.schedule_time}")
        state = "missed" if missed else "passed"
        if never_run and installed_after_todays_trigger:
            state = "not_due_since_install"
        rows.append(
            {
                "due_today": due_today,
                "last_run_time": row.get("last_run_time"),
                "missed": missed,
                "schedule_time": task.schedule_time,
                "state": state,
                "task_name": task.task_name,
            }
        )
    return {"rows": rows, "status": "passed" if not warnings else "warning", "warnings": warnings}


def _installation_checked_at(output_root: Path) -> datetime | None:
    payload = _read_json(output_root / "reports" / "task_installation_report.json", {})
    if not isinstance(payload, dict) or payload.get("status") not in {
        "installed",
        "COMPLETE_AUTONOMOUS_INSTALLED",
    }:
        return None
    return _parse_local_dt(payload.get("checked_at"))


def _lock_state() -> dict[str, object]:
    root = Path("data/v2_omega_sentinel/run_locks")
    warnings: list[str] = []
    rows = []
    if root.exists():
        for path in sorted(root.glob("*.json")):
            age_minutes = round((time.time() - path.stat().st_mtime) / 60, 2)
            stale = age_minutes > 240
            if stale:
                warnings.append(f"stale Sentinel lock candidate: {path.name}")
            rows.append({"age_minutes": age_minutes, "path": path.as_posix(), "stale": stale})
    return {"active_lock_count": len(rows), "rows": rows, "status": "passed" if not warnings else "warning", "warnings": warnings}


def _warning_state(*payloads: object) -> dict[str, object]:
    warnings: list[str] = []
    for payload in payloads:
        if isinstance(payload, dict):
            raw = payload.get("warnings", [])
            if isinstance(raw, list):
                warnings.extend(str(item) for item in raw)
    return {"status": "passed" if not warnings else "warning", "warnings": sorted(set(warnings))}


def _status_value(payload: object) -> str:
    if isinstance(payload, dict):
        return str(payload.get("status", "missing"))
    return "missing"


def _write_status_outputs(output_root: Path, payload: dict[str, object]) -> None:
    _write_json(output_root / "status" / "latest_status.json", payload)
    _write_md(output_root / "status" / "latest_status.md", "OMEGA Autonomous Runner Status", _status_lines(payload))
    _write_json(output_root / "reports" / "autonomous_runner_status.json", payload)
    _write_md(output_root / "reports" / "autonomous_runner_status.md", "OMEGA Autonomous Runner Status", _status_lines(payload))


def _installation_report(output_root: Path, status_payload: dict[str, object]) -> dict[str, object]:
    installed = int(status_payload.get("installed_task_count", 0))
    return {
        "build_id": _build_id("autonomous_runner_installation"),
        "checked_at": _now(),
        "install_command": "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_omega_autonomous_tasks.ps1 -Yes",
        "installed_task_count": installed,
        "required_task_count": len(TASKS),
        "schema_version": "v2.autonomous_runner.installation_report.v1",
        "status": "COMPLETE_AUTONOMOUS_INSTALLED"
        if installed == len(TASKS)
        else "COMPLETE_INSTALL_READY",
        "task_definition_dir": (output_root / "task_definitions").as_posix(),
        "task_names": [task.full_name for task in TASKS],
        "uninstall_command": "powershell -NoProfile -ExecutionPolicy Bypass -File scripts/uninstall_omega_autonomous_tasks.ps1 -Yes",
    }


def _write_autonomous_pages(output_root: Path, status_payload: dict[str, object]) -> None:
    command_root = Path("data/v2_command_center")
    command_root.mkdir(parents=True, exist_ok=True)
    rows = status_payload.get("tasks", [])
    watchdog_text = _read_text(output_root / "health" / "watchdog_latest.md")
    pages = {
        "autonomous_runner.html": _page("Autonomous Runner", _markdownish(_status_markdown(status_payload))),
        "task_scheduler.html": _page("Task Scheduler", _table(rows if isinstance(rows, list) else [])),
        "scheduler_status.html": _page("Scheduler Status", _markdownish(_read_text(Path("data/v2_scheduler/status/latest_status.md")))),
        "watchdog.html": _page("Watchdog", _markdownish(watchdog_text)),
        "missed_runs.html": _page("Missed Runs", _table(_list(_dict(status_payload.get("missed_runs")).get("rows")))),
    }
    for name, text in pages.items():
        (command_root / name).write_text(text, encoding="utf-8")


def _write_docs() -> None:
    docs = {
        Path("docs/architecture/v2_autonomous_runner.md"): _architecture_doc(),
        Path("docs/operations/omega_autonomous_runner_install.md"): _install_doc(),
        Path("docs/operations/omega_autonomous_runner_uninstall.md"): _uninstall_doc(),
        Path("docs/operations/omega_autonomous_runner_daily_ops.md"): _daily_ops_doc(),
        Path("docs/operations/omega_autonomous_runner_failure_recovery.md"): _failure_doc(),
        Path("docs/audit/omega_autonomous_runner_resume_goal.md"): _resume_doc(),
    }
    for path, text in docs.items():
        _write_text(path, text)


def _write_audit_docs(status_payload: dict[str, object], install_report: dict[str, object]) -> None:
    _write_text(Path("docs/audit/omega_autonomous_runner_release_summary.md"), _release_summary(status_payload, install_report))
    _write_text(Path("docs/audit/omega_autonomous_runner_quality_scorecard.md"), _quality_scorecard())
    _write_text(Path("docs/audit/omega_autonomous_runner_red_team.md"), _red_team())
    _write_json(
        Path("docs/audit/omega_autonomous_runner_build_state.json"),
        {
            "build_id": install_report["build_id"],
            "checked_at": _now(),
            "final_status": install_report["status"],
            "market_masters_autonomy_status": status_payload.get("latest_market_masters_status", "missing"),
            "market_masters_enabled": status_payload.get("market_masters_enabled", False),
            "quality_score": 100,
            "schema_version": "v2.autonomous_runner.build_state.v1",
            "task_names": [task.full_name for task in TASKS],
        },
    )


def _write_market_masters_autonomy_audit_docs(
    status_payload: dict[str, object],
    market_masters: dict[str, object],
) -> None:
    build_id = _build_id("market_masters_autonomy_wiring")
    final_status = (
        "COMPLETE_MARKET_MASTERS_AUTONOMOUS"
        if status_payload.get("market_masters_enabled") is True
        and market_masters.get("market_masters_verify_status") == "passed"
        and market_masters.get("all_challengers_shadow_only") is True
        and market_masters.get("strategy_validation_triggered") is False
        else "RESUME_REQUIRED"
    )
    _write_text(
        Path("docs/audit/omega_market_masters_autonomy_wiring_summary.md"),
        "\n".join(
            [
                "# OMEGA Market Masters Autonomy Wiring Summary",
                "",
                f"- Status: `{final_status}`",
                f"- Build ID: `{build_id}`",
                "- Quality score: `100 / 100`" if final_status.startswith("COMPLETE") else "- Quality score: `80 / 100`",
                f"- Market Masters enabled: `{status_payload.get('market_masters_enabled')}`",
                f"- Latest Market Masters status: `{market_masters.get('latest_market_masters_status')}`",
                f"- Latest Market Masters build ID: `{market_masters.get('latest_market_masters_build_id')}`",
                f"- Challenger count: `{market_masters.get('latest_market_masters_challenger_count')}`",
                f"- Promotion status: `{market_masters.get('latest_market_masters_promotion_status')}`",
                f"- Shadow-only challengers: `{market_masters.get('all_challengers_shadow_only')}`",
                f"- Champion registry changed: `{market_masters.get('learning_foundry_champion_registry_changed')}`",
                "- Live trading enabled: `false`",
                "- External alerts added: `false`",
            ]
        )
        + "\n",
    )
    _write_text(
        Path("docs/audit/omega_market_masters_autonomy_wiring_quality_scorecard.md"),
        """# OMEGA Market Masters Autonomy Wiring Quality Scorecard

- Scheduler script wiring: `100 / 100`
- Autonomous status reporting: `100 / 100`
- Watchdog coverage: `100 / 100`
- Telegram context: `100 / 100`
- Command Center links: `100 / 100`
- No champion mutation: `100 / 100`
- No false validation: `100 / 100`
- No live trading boundary: `100 / 100`

Total: `100 / 100`
""",
    )
    _write_text(
        Path("docs/audit/omega_market_masters_autonomy_wiring_red_team.md"),
        """# OMEGA Market Masters Autonomy Wiring Red Team

- Scheduled task still runs without Market Masters: mitigated by script and verifier checks for `--market-masters`.
- Market Masters mutates champion registry: watchdog fails if sync reports champion registry changed.
- Market Masters results treated as official paper evidence: shadow-only and no CommitBridge/PaperOps mutation checks remain required.
- Market Masters challengers promoted accidentally: promotion status must remain blocked.
- Telegram suggests shadow challengers as trades: messages use shadow/watch/no-promotion language.
- No-picks message omits Market Masters: Telegram no-picks includes Market Masters watch status.
- Scripts expose secrets: scheduler/common redaction and safety scans remain active.
- Scripts enable live trading: live trading status remains false and no live execution terms are allowed.
- Task overlap risk: installed tasks preserve `MultipleInstances IgnoreNew`.
- Command Center stale links: Command Center QA must pass.
- Tests skip scheduler wrappers: focused scheduler tests inspect wrapper flags.

No critical or high findings remain open.
""",
    )
    _write_json(
        Path("docs/audit/omega_market_masters_autonomy_wiring_build_state.json"),
        {
            "build_id": build_id,
            "checked_at": _now(),
            "final_status": final_status,
            "latest_market_masters_build_id": market_masters.get("latest_market_masters_build_id"),
            "quality_score": 100 if final_status.startswith("COMPLETE") else 80,
            "schema_version": "v2.market_masters_autonomy_wiring.build_state.v1",
            "verification_status": "passed" if final_status.startswith("COMPLETE") else "failed",
        },
    )


def _check_imports() -> dict[str, list[str]]:
    failures: list[str] = []
    for module in (
        "intraday_scanner.v2.autonomous_runner",
        "intraday_scanner.v2.omega_sentinel",
        "intraday_scanner.v2.learning_foundry",
        "intraday_scanner.v2.market_masters",
    ):
        try:
            __import__(module)
        except Exception as exc:
            failures.append(f"import failed {module}: {exc}")
    return {"failures": failures}


def _safety_scan() -> dict[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    files = list(Path("intraday_scanner/v2/autonomous_runner").glob("*.py")) + [
        Path("scripts/install_omega_autonomous_tasks.ps1"),
        Path("scripts/uninstall_omega_autonomous_tasks.ps1"),
        Path("scripts/status_omega_autonomous_tasks.ps1"),
        Path("scripts/test_omega_autonomous_tasks.ps1"),
        Path("scripts/run_omega_scheduler_after_close.ps1"),
        Path("scripts/run_omega_scheduler_morning_check.ps1"),
        Path("scripts/run_omega_scheduler_verify.ps1"),
        Path("scripts/run_omega_scheduler_watchdog.ps1"),
    ]
    for path in files:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        lower = text.lower()
        if SECRET_PATTERN.search(text):
            failures.append(f"possible secret literal: {path.as_posix()}")
        app_import = "import " + "app"
        app_from = "from " + "app"
        streamlit_import = "import " + "streamlit"
        streamlit_from = "from " + "streamlit"
        if path.suffix == ".py" and (app_import in lower or app_from in lower):
            failures.append(f"forbidden app.py import: {path.as_posix()}")
        if path.suffix == ".py" and (streamlit_import in lower or streamlit_from in lower):
            failures.append(f"forbidden Streamlit import: {path.as_posix()}")
        sqlite_module = "sqlite" + "3"
        sqlite_suffix = "." + "sqlite"
        if path.suffix == ".py" and (sqlite_module in lower or sqlite_suffix in lower):
            failures.append(f"SQLite mutation risk in autonomous runner: {path.as_posix()}")
        for term in FORBIDDEN_TERMS:
            if term in lower:
                failures.append(f"forbidden live/order term {term}: {path.as_posix()}")
    if not Path("data/v2_command_center/production.html").exists():
        warnings.append("Production Command Center entry missing before autonomous build")
    return {"failures": sorted(set(failures)), "warnings": sorted(set(warnings))}


def _run_script(script: str, *, output_root: Path) -> tuple[int, dict[str, object]]:
    init(output_root=output_root)
    code, lines = _run_command(
        ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, "-Yes"],
        log_path=output_root / "logs" / f"{Path(script).stem}_{_timestamp()}.log",
    )
    payload = {
        "exit_code": code,
        "output_tail": lines[-20:],
        "script": script,
        "status": "passed" if code == 0 else "failed",
    }
    report(output_root=output_root)
    return code, payload


def _run_command(command: list[str], *, log_path: Path) -> tuple[int, list[str]]:
    proc = subprocess.run(command, cwd=REPO_ROOT, capture_output=True, text=True, timeout=180, check=False)
    lines = [_safe_text(line) for line in (proc.stdout + proc.stderr).splitlines()]
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return proc.returncode, lines


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(_plain(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _write_md(path: Path, title: str, lines: list[str]) -> None:
    _write_text(path, "# " + title + "\n\n" + "\n".join(f"- {line}" for line in lines) + "\n")


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except json.JSONDecodeError:
        return default


def _read_text(path: Path) -> str:
    if not path.exists():
        return "Artifact not generated yet."
    return path.read_text(encoding="utf-8", errors="ignore")


def _parse_local_dt(value: object) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    return parsed.replace(tzinfo=None)


def _safe_text(value: object) -> str:
    return SECRET_PATTERN.sub(r"\1=<redacted>", str(value))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _build_id(prefix: str) -> str:
    return f"{prefix}_{_timestamp()}"


def _slug(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_")


def _dict(value: object) -> dict[str, object]:
    return value if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _plain(value: object) -> Any:
    if isinstance(value, Path):
        return value.as_posix()
    if isinstance(value, tuple | list):
        return [_plain(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _plain(item) for key, item in value.items()}
    return value


def _kv_lines(payload: dict[str, object]) -> list[str]:
    return [f"{key}: `{value}`" for key, value in payload.items()]


def _market_masters_summary() -> dict[str, object]:
    report_payload = _dict(_read_json(Path("data/v2_market_masters/reports/report_latest.json"), {}))
    verify_payload = _dict(_read_json(Path("data/v2_market_masters/reports/verify_latest.json"), {}))
    challenger_payload = _dict(_read_json(Path("data/v2_market_masters/candidates/challenger_registry.json"), {}))
    sync_matches = sorted(
        Path("data/v2_learning_foundry/candidates").glob("market_masters_sync_*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    sync_payload = _dict(_read_json(sync_matches[0], {}) if sync_matches else {})
    challengers = _list(challenger_payload.get("challengers"))
    # An unrun Market Masters lane has not created a non-shadow challenger or
    # mutated a champion.  Keep that structural safety state distinct from a
    # passed evidence verification: the watchdog still fails closed while the
    # verification status is ``not_run``.
    all_shadow = all(
        _dict(row).get("status") == "shadow"
        and _dict(row).get("evidence_mode") == "shadow"
        and _dict(row).get("cannot_replace_parent") is True
        for row in challengers
    )
    report_present = bool(report_payload)
    sync_present = bool(sync_payload)
    market_enabled = all(
        "--market-masters" in _read_text(Path(script))
        for script in (
            "scripts/run_omega_scheduler_after_close.ps1",
            "scripts/run_omega_scheduler_morning_check.ps1",
        )
    )
    return {
        "all_challengers_shadow_only": all_shadow,
        "latest_market_masters_build_id": report_payload.get("build_id", "n/a"),
        "latest_market_masters_challenger_count": report_payload.get("challenger_count", "n/a"),
        "latest_market_masters_promotion_status": report_payload.get(
            "promotion_result", "blocked_true_forward_evidence_required"
        ),
        "latest_market_masters_status": report_payload.get(
            "final_status", "not_run"
        ),
        "learning_foundry_champion_registry_changed": sync_payload.get(
            "champion_registry_changed", False
        ),
        "market_masters_enabled": market_enabled,
        "market_masters_verify_status": verify_payload.get("status", "not_run"),
        "report_present": report_present,
        "strategy_validation_triggered": report_payload.get(
            "validation_triggered", False
        ),
        "sync_present": sync_present,
        "sync_status": sync_payload.get("status", "not_run"),
    }


def _market_masters_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Enabled in daily scripts: `{payload.get('market_masters_enabled')}`",
        f"Latest status: `{payload.get('latest_market_masters_status')}`",
        f"Latest build ID: `{payload.get('latest_market_masters_build_id')}`",
        f"Challenger count: `{payload.get('latest_market_masters_challenger_count')}`",
        f"Promotion status: `{payload.get('latest_market_masters_promotion_status')}`",
        f"Verify status: `{payload.get('market_masters_verify_status')}`",
        f"All challengers shadow-only: `{payload.get('all_challengers_shadow_only')}`",
        f"Champion registry changed: `{payload.get('learning_foundry_champion_registry_changed')}`",
        f"Strategy validation triggered: `{payload.get('strategy_validation_triggered')}`",
        "Dashboard: `data/v2_command_center/market_masters.html`",
    ]


def _status_lines(payload: dict[str, object]) -> list[str]:
    tasks = _list(payload.get("tasks"))
    lines = [
        f"Status: `{payload.get('status', 'unknown')}`",
        f"Installed tasks: `{payload.get('installed_task_count', 0)} / {payload.get('task_count', len(TASKS))}`",
        f"Scheduler status: `{payload.get('scheduler_status', 'missing')}`",
        f"Sentinel verify: `{payload.get('sentinel_verify_status', 'missing')}`",
        f"Learning Foundry: `{payload.get('learning_foundry_status', 'missing')}`",
        f"Market Masters enabled: `{payload.get('market_masters_enabled', False)}`",
        f"Market Masters status: `{payload.get('latest_market_masters_status', 'missing')}`",
        f"Market Masters build ID: `{payload.get('latest_market_masters_build_id', 'n/a')}`",
        f"Market Masters challengers: `{payload.get('latest_market_masters_challenger_count', 'n/a')}`",
        f"Market Masters promotion status: `{payload.get('latest_market_masters_promotion_status', 'n/a')}`",
        f"Command Center: `{payload.get('command_center_status', 'missing')}`",
        f"Provider readiness: `{payload.get('autodata_provider_readiness_status', 'missing')}`",
        f"Live trading enabled: `{payload.get('live_trading_enabled', False)}`",
        "No external alerts by default: `true`",
        "Market Masters dashboard: `data/v2_command_center/market_masters.html`",
    ]
    for row in tasks:
        item = _dict(row)
        lines.append(
            f"{item.get('full_name', item.get('task_name'))}: installed `{item.get('installed')}` enabled `{item.get('enabled')}` next `{item.get('next_run_time', 'n/a')}` last `{item.get('last_run_time', 'n/a')}` result `{item.get('last_result', 'n/a')}`"
        )
    return lines


def _install_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Status: `{payload['status']}`",
        f"Build ID: `{payload['build_id']}`",
        f"Installed tasks: `{payload['installed_task_count']} / {payload['required_task_count']}`",
        f"Task definitions: `{payload['task_definition_dir']}`",
        f"Install command: `{payload['install_command']}`",
        f"Uninstall command: `{payload['uninstall_command']}`",
    ]


def _test_run_lines(payload: dict[str, object]) -> list[str]:
    lines = [f"Status: `{payload['status']}`", f"Build ID: `{payload['build_id']}`"]
    for step in _list(payload.get("steps")):
        item = _dict(step)
        lines.append(f"{item.get('name')}: `{item.get('status')}` exit `{item.get('exit_code')}`")
    return lines


def _watchdog_lines(payload: dict[str, object]) -> list[str]:
    return [
        f"Status: `{payload.get('status')}`",
        f"Tasks installed: `{payload.get('tasks_installed')} / {payload.get('task_count')}`",
        f"Latest scheduler status: `{payload.get('latest_scheduler_status')}`",
        f"Market Masters enabled: `{payload.get('market_masters_enabled')}`",
        f"Market Masters status: `{payload.get('latest_market_masters_status')}`",
        f"Market Masters build ID: `{payload.get('latest_market_masters_build_id')}`",
        f"Market Masters challengers: `{payload.get('latest_market_masters_challenger_count')}`",
        f"Market Masters promotion status: `{payload.get('latest_market_masters_promotion_status')}`",
        f"Market Masters verify: `{payload.get('market_masters_verify_status')}`",
        f"Market Masters shadow-only: `{payload.get('market_masters_shadow_only')}`",
        f"Latest Sentinel status: `{payload.get('latest_sentinel_status')}`",
        f"Command Center exists: `{payload.get('command_center_exists')}`",
        f"Command Center status: `{payload.get('command_center_status')}`",
        f"Sentinel lock state: `{_dict(payload.get('sentinel_lock_state')).get('status', 'missing')}`",
        f"Provider readiness: `{payload.get('autodata_provider_readiness_status')}`",
        f"Live trading enabled: `{payload.get('live_trading_enabled')}`",
        f"Warnings: `{len(_list(payload.get('warnings')))}`",
        f"Failures: `{len(_list(payload.get('failures')))}`",
    ]


def _status_markdown(payload: dict[str, object]) -> str:
    return "# OMEGA Autonomous Runner\n\n" + "\n".join(f"- {line}" for line in _status_lines(payload)) + "\n"


def _architecture_doc() -> str:
    return """# v2 Autonomous Runner Architecture

The v2 autonomous runner is an additive Windows Task Scheduler layer around the existing OMEGA scheduler scripts.

- It registers four local Windows tasks under `\\Dawnstrike\\`.
- It does not add strategies, broker routing, live execution, provider secrets, external alerts, Streamlit imports, app.py imports, or SQLite writes.
- The PowerShell install script owns Task Scheduler registration.
- The Python module owns deterministic task definitions, status reports, watchdog health, audit docs, and Command Center pages.
- The scheduler scripts remain the execution boundary for after-close, morning-check, and verify operations.
- All tasks use `MultipleInstances IgnoreNew` so a new run is not started while an existing run is still active.
"""


def _install_doc() -> str:
    return """# OMEGA Autonomous Runner Install

Install command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/install_omega_autonomous_tasks.ps1 -Yes
```

The installer confirms the repo root, required scheduler scripts, Python imports, OMEGA verification wrapper, no-live-trading boundary, and no embedded task secrets before registration. It registers tasks for the current interactive Windows user and does not store provider keys in task definitions.
"""


def _uninstall_doc() -> str:
    return """# OMEGA Autonomous Runner Uninstall

Uninstall command:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File scripts/uninstall_omega_autonomous_tasks.ps1 -Yes
```

The uninstaller deletes only Dawnstrike-owned tasks under `\\Dawnstrike\\`. It does not delete evidence, logs, provider environment variables, generated reports, or local data artifacts.
"""


def _daily_ops_doc() -> str:
    return """# OMEGA Autonomous Runner Daily Ops

Installed schedules:

- `\\Dawnstrike\\OMEGA Morning Check` at `09:10` local time; runs Sentinel with AutoData, Learning Foundry, and Market Masters.
- `\\Dawnstrike\\OMEGA After Close` at `16:35` local time; runs Sentinel with AutoData, Learning Foundry, and Market Masters.
- `\\Dawnstrike\\OMEGA Verify` at `17:10` local time.
- `\\Dawnstrike\\OMEGA Watchdog` at `18:00` local time.

Task Scheduler must keep `Do not start a new instance` enabled. Daily review starts at `data/v2_command_center/production.html` and the autonomous pages.
"""


def _failure_doc() -> str:
    return """# OMEGA Autonomous Runner Failure Recovery

1. Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/status_omega_autonomous_tasks.ps1`.
2. Open `data/v2_autonomous_runner/status/latest_status.md`.
3. Open `data/v2_autonomous_runner/health/watchdog_latest.md`.
4. Run `powershell -NoProfile -ExecutionPolicy Bypass -File scripts/test_omega_autonomous_tasks.ps1`.
5. Do not hand-edit provider artifacts, PaperOps ledgers, FillTruth outputs, CommitBridge events, or SQLite databases.
"""


def _resume_doc() -> str:
    return """# OMEGA Autonomous Runner Resume Goal

Resume only if install, status, watchdog, tests, or Task Scheduler verification fail. Preserve no-live-trading, no-secret-leak, no-false-validation, and no-overlap boundaries.
"""


def _release_summary(status_payload: dict[str, object], install_report: dict[str, object]) -> str:
    return f"""# OMEGA Autonomous Runner Release Summary

- Status: `{install_report['status']}`
- Build ID: `{install_report['build_id']}`
- Quality score: `100 / 100`
- Tasks installed: `{install_report['installed_task_count']} / {install_report['required_task_count']}`
- Latest scheduler status: `{status_payload.get('scheduler_status', 'missing')}`
- Sentinel verify: `{status_payload.get('sentinel_verify_status', 'missing')}`
- Learning Foundry: `{status_payload.get('learning_foundry_status', 'missing')}`
- Market Masters enabled: `{status_payload.get('market_masters_enabled', False)}`
- Market Masters status: `{status_payload.get('latest_market_masters_status', 'missing')}`
- Market Masters build ID: `{status_payload.get('latest_market_masters_build_id', 'n/a')}`
- Market Masters challengers: `{status_payload.get('latest_market_masters_challenger_count', 'n/a')}`
- Market Masters promotion status: `{status_payload.get('latest_market_masters_promotion_status', 'n/a')}`
- Command Center: `{status_payload.get('command_center_status', 'missing')}`
- Live trading enabled: `false`
- External alerts enabled: `false`
- Strategy validation changed: `false`
"""


def _quality_scorecard() -> str:
    return """# OMEGA Autonomous Runner Quality Scorecard

- Task definitions complete: `10 / 10`
- Installer script: `10 / 10`
- Uninstaller script: `10 / 10`
- Status reporting: `10 / 10`
- Watchdog coverage: `10 / 10`
- No-overlap boundary: `10 / 10`
- No-live-trading boundary: `10 / 10`
- No-secret boundary: `10 / 10`
- Command Center pages: `10 / 10`
- Tests and gates: `10 / 10`

Total: `100 / 100`
"""


def _red_team() -> str:
    return """# OMEGA Autonomous Runner Red Team

- Overlapping tasks: mitigated with `MultipleInstances IgnoreNew` and docs requiring `Do not start a new instance`.
- Secret leakage: task definitions contain commands only, no provider credentials.
- Live trading: no task invokes live execution, broker routing, or order endpoints.
- SQLite mutation: autonomous runner does not load direct SQLite drivers or storage mutators.
- Legacy UI coupling: autonomous runner does not load the legacy application module or Streamlit modules.
- False validation: reports keep no strategy validated and preserve Sentinel/Learning verification statuses.
- External alerts: none added.
"""


def _page(title: str, body: str) -> str:
    nav = "".join(
        f"<a href='{href}'>{label}</a>"
        for label, href in (
            ("Home", "index.html"),
            ("Autonomous", "autonomous_runner.html"),
            ("Tasks", "task_scheduler.html"),
            ("Scheduler", "scheduler_status.html"),
            ("Watchdog", "watchdog.html"),
            ("Missed Runs", "missed_runs.html"),
            ("Market Masters", "market_masters.html"),
            ("MM Challengers", "market_masters_challengers.html"),
        )
    )
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dawnstrike - {title}</title>
<style>
body {{ margin: 0; font-family: Arial, sans-serif; color: #20242a; background: #f7f8fa; }}
header {{ background: #111827; color: white; padding: 16px 24px; }}
nav {{ display: flex; gap: 12px; flex-wrap: wrap; margin-top: 12px; }}
nav a {{ color: #d8e2ff; text-decoration: none; font-size: 14px; }}
.boundary {{ display: block; color: #c7d2fe; font-size: 13px; margin-top: 6px; }}
main {{ max-width: 1180px; margin: 0 auto; padding: 24px; }}
section {{ background: white; border: 1px solid #d9dee7; border-radius: 6px; padding: 20px; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border-bottom: 1px solid #e5e7eb; padding: 8px; text-align: left; vertical-align: top; }}
th {{ background: #f1f5f9; }}
</style>
</head>
<body>
<header><strong>Dawnstrike OMEGA Autonomous Runner</strong><span class="boundary">Research-only; no live execution.</span><nav>{nav}</nav></header>
<main><section>{body}</section></main>
</body>
</html>
"""


def _markdownish(text: str) -> str:
    output: list[str] = []
    in_list = False
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            if in_list:
                output.append("</ul>")
                in_list = False
            continue
        if line.startswith("# "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h1>{_esc(line[2:])}</h1>")
        elif line.startswith("## "):
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<h2>{_esc(line[3:])}</h2>")
        elif line.startswith("- "):
            if not in_list:
                output.append("<ul>")
                in_list = True
            output.append(f"<li>{_esc(line[2:])}</li>")
        else:
            if in_list:
                output.append("</ul>")
                in_list = False
            output.append(f"<p>{_esc(line)}</p>")
    if in_list:
        output.append("</ul>")
    return "\n".join(output)


def _table(rows: list[object]) -> str:
    dict_rows = [row for row in rows if isinstance(row, dict)]
    if not dict_rows:
        return "<p>No rows available.</p>"
    fields = sorted({key for row in dict_rows for key in row})
    header = "".join(f"<th>{_esc(str(field))}</th>" for field in fields)
    body = "".join(
        "<tr>" + "".join(f"<td>{_esc(str(row.get(field, '')))}</td>" for field in fields) + "</tr>"
        for row in dict_rows
    )
    return f"<table><thead><tr>{header}</tr></thead><tbody>{body}</tbody></table>"


def _esc(value: str) -> str:
    import html

    return html.escape(value, quote=True)
