import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

import intraday_scanner.services.scheduler_doctor_service as scheduler_service

_REAL_RUNTIME_GIT_CONTRACT = scheduler_service._runtime_git_contract


def _stable_runtime_contract() -> dict[str, str]:
    return {
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "runtime_origin_sha256": "e" * 64,
        "origin_main_sha": "a" * 40,
    }


@pytest.fixture(autouse=True)
def _stub_stable_runtime_contract(monkeypatch) -> None:
    monkeypatch.setattr(
        scheduler_service,
        "_runtime_git_contract",
        lambda _runtime: _stable_runtime_contract(),
    )


@pytest.mark.parametrize("failure", ["dirty-after", "identity-change"])
def test_runtime_git_contract_requires_stable_clean_identity(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    stable = _stable_runtime_contract()
    changed = {**stable, "candidate_tree": "c" * 40}
    snapshots = iter(
        [stable, stable, stable]
        if failure == "dirty-after"
        else [stable, changed]
    )
    clean = iter([True, False]) if failure == "dirty-after" else iter([True])
    monkeypatch.setattr(
        scheduler_service,
        "_runtime_git_snapshot",
        lambda _runtime: next(snapshots),
    )
    monkeypatch.setattr(
        scheduler_service,
        "_runtime_git_clean",
        lambda _runtime: next(clean),
    )

    assert _REAL_RUNTIME_GIT_CONTRACT(tmp_path) is None


def test_scheduler_query_uses_bounded_provider_side_dawnstrike_filter(
    monkeypatch,
) -> None:
    captured: dict[str, object] = {}
    duplicate = {
        "name": "Dawnstrike Finalize Recovery Copy",
        "state": "Ready",
        "enabled": True,
    }

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["kwargs"] = kwargs
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps([duplicate]),
            stderr="",
        )

    monkeypatch.setattr(scheduler_service, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(scheduler_service.subprocess, "run", fake_run)

    rows = scheduler_service._query_scheduled_tasks()

    command = captured["command"]
    assert isinstance(command, list)
    powershell = command[-1]
    assert (
        "Get-ScheduledTask -TaskPath '\\*' "
        "-TaskName 'Dawnstrike*' -ErrorAction Stop"
    ) in powershell
    assert "Get-ScheduledTask | Where-Object" not in powershell
    assert "Where-Object { $_ -notin $canonicalNames }" in powershell
    assert "-TaskPath $task.TaskPath -ErrorAction Stop" in powershell
    kwargs = captured["kwargs"]
    assert isinstance(kwargs, dict)
    assert kwargs["timeout"] == scheduler_service.SCHEDULER_QUERY_TIMEOUT_SECONDS == 30
    assert rows == [duplicate]


@pytest.mark.skipif(
    scheduler_service.os.name != "nt",
    reason="Generated ScheduledTasks query requires Windows PowerShell 5.",
)
def test_generated_scheduler_query_emits_nested_path_duplicate(
    tmp_path: Path,
    monkeypatch,
) -> None:
    shim = r"""
function Get-ScheduledTask {
    [CmdletBinding()]
    param([string[]]$TaskName, [string[]]$TaskPath)
    if ([string]$TaskPath -ne '\*') {
        return
    }
    $action = [pscustomobject]@{
        Execute = 'powershell.exe'
        Arguments = ''
        WorkingDirectory = ''
    }
    $repetition = [pscustomobject]@{
        Duration = ''
        Interval = ''
        StopAtDurationEnd = $false
    }
    $trigger = [pscustomobject]@{
        CimClass = [pscustomobject]@{CimClassName = 'MSFT_TaskWeeklyTrigger'}
        Enabled = $true
        DaysOfWeek = 62
        WeeksInterval = 1
        DaysInterval = $null
        StartBoundary = '2026-07-01T08:00:00-05:00'
        EndBoundary = $null
        RandomDelay = $null
        Repetition = $repetition
    }
    [pscustomobject]@{
        TaskName = 'Dawnstrike AlphaOps Morning Shadow'
        TaskPath = '\DawnstrikeShadow\'
        State = 'Ready'
        Actions = @($action)
        Triggers = @($trigger)
        Settings = [pscustomobject]@{
            Enabled = $true
            StartWhenAvailable = $true
            WakeToRun = $true
            StopIfGoingOnBatteries = $false
            DisallowStartIfOnBatteries = $false
            ExecutionTimeLimit = 'PT1H'
            RestartCount = 3
            RestartInterval = 'PT5M'
            MultipleInstances = 'IgnoreNew'
        }
        Principal = [pscustomobject]@{
            LogonType = 'Password'
            RunLevel = 'Limited'
        }
    }
}
function Get-ScheduledTaskInfo {
    [CmdletBinding()]
    param([string]$TaskName, [string]$TaskPath)
    if ($TaskPath -ne '\DawnstrikeShadow\') {
        throw "Generated query lost the nested task identity."
    }
    [pscustomobject]@{
        LastTaskResult = 0
        LastRunTime = [datetimeoffset]'2026-07-30T08:00:00-05:00'
        NextRunTime = [datetimeoffset]'2026-07-31T08:00:00-05:00'
    }
}
"""
    completed = scheduler_service.subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-Command",
            shim + scheduler_service._scheduler_query_script(),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=scheduler_service.SCHEDULER_QUERY_TIMEOUT_SECONDS,
    )

    assert completed.returncode == 0, completed.stderr
    rows = scheduler_service._normalize_task_rows(json.loads(completed.stdout))
    nested = next(
        row
        for row in rows
        if row.get("name") == "Dawnstrike AlphaOps Morning Shadow"
    )
    assert nested["task_path"] == "\\DawnstrikeShadow\\"

    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    _write_required_state(state)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [nested]


def test_scheduler_task_info_error_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    _write_required_state(state)

    def failed_run(_command, **_kwargs):
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="Get-ScheduledTaskInfo failed",
        )

    monkeypatch.setattr(scheduler_service, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(scheduler_service.subprocess, "run", failed_run)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == len(scheduler_service.EXPECTED_TASKS)
    assert all(row["state"] == "unknown" for row in result["scheduled_tasks"])


def test_scheduler_task_query_timeout_fails_closed(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    _write_required_state(state)

    def timed_out_run(command, **kwargs):
        raise scheduler_service.subprocess.TimeoutExpired(
            command,
            kwargs["timeout"],
        )

    monkeypatch.setattr(scheduler_service, "os", SimpleNamespace(name="nt"))
    monkeypatch.setattr(scheduler_service.subprocess, "run", timed_out_run)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == len(scheduler_service.EXPECTED_TASKS)
    assert all(row["state"] == "unavailable" for row in result["scheduled_tasks"])


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
        "dawnstrike_python_bootstrap.py",
        "run_daily_intraday_capture.py",
    ):
        (scripts / name).write_text("placeholder", encoding="utf-8")


def _write_required_state(state: Path) -> None:
    config = state / "config"
    config.mkdir(parents=True, exist_ok=True)
    (config / "web_sources.yaml").write_text(
        "enabled: true\n"
        "user_agent: DawnstrikeTest Contact: test@dawnstrike.test\n"
        "sources:\n"
        "  - name: candidates\n"
        "    type: local_inbox\n"
        "    enabled: true\n"
        "    path: data\\inbox\\screener\n",
        encoding="utf-8",
    )


def _healthy_tasks(runtime: Path, state: Path, *, last_result: int = 0):
    _write_required_state(state)
    return [
        {
            "name": name,
            "state": "Ready",
            "enabled": True,
            "action_count": 1,
            "trigger_count": 1,
            "host_timezone_id": scheduler_service.EXPECTED_WINDOWS_TIMEZONE_ID,
            "multiple_instances": scheduler_service.EXPECTED_MULTIPLE_INSTANCES,
            "run_level": scheduler_service.EXPECTED_RUN_LEVEL,
            "wake_to_run": True,
            "restart_count": scheduler_service.EXPECTED_RESTART_COUNTS[name],
            "restart_interval": scheduler_service.EXPECTED_RESTART_INTERVALS[name],
            "trigger_type": scheduler_service.EXPECTED_TRIGGER_TYPES[name],
            "trigger_enabled": True,
            "trigger_days_of_week": (
                scheduler_service.EXPECTED_TRIGGER_DAYS_OF_WEEK[name]
            ),
            "trigger_weeks_interval": (
                scheduler_service.EXPECTED_TRIGGER_WEEKS_INTERVAL.get(name)
            ),
            "trigger_days_interval": (
                scheduler_service.EXPECTED_TRIGGER_DAYS_INTERVAL.get(name)
            ),
            "trigger_end_boundary": None,
            "trigger_random_delay": None,
            "logon_type": "Password",
            "principal_user_id": r"DAWNSTRIKE\capture-service",
            "start_when_available": True,
            "stop_if_going_on_batteries": False,
            "disallow_start_if_on_batteries": False,
            "last_task_result": last_result,
            "last_run_time": "2026-07-30T17:30:00-05:00",
            "trigger_start_boundary": (
                "2026-07-01T"
                f"{scheduler_service.EXPECTED_TASK_STARTS[name]}:00-05:00"
            ),
            "next_run_time": (
                "2026-07-31T"
                f"{scheduler_service.EXPECTED_TASK_STARTS[name]}:00-05:00"
            ),
            "execute": "powershell.exe",
            "arguments": (
                f'-NoProfile -ExecutionPolicy Bypass -File '
                f'"{runtime / "scripts" / script}" '
                f'-RuntimeRoot "{runtime}" -StateRoot "{state}"'
                + (
                    " -PublicationMode Production "
                    f'-VercelProjectId "{scheduler_service.EXPECTED_VERCEL_PROJECT_ID}"'
                    if name == scheduler_service.CANONICAL_TASK_NAME
                    else ""
                )
            ),
            "working_directory": str(runtime),
            "repetition_duration": (
                scheduler_service.EXPECTED_TASK_REPETITIONS.get(name)
            ),
            "repetition_interval": (
                scheduler_service.EXPECTED_REPETITION_INTERVALS.get(name)
            ),
            "repetition_stop_at_duration_end": (
                scheduler_service.EXPECTED_REPETITION_STOP_AT_DURATION_END[name]
            ),
            "execution_time_limit": scheduler_service.EXPECTED_EXECUTION_LIMITS[name],
        }
        for name, script in scheduler_service.EXPECTED_TASKS.items()
    ]


def _auxiliary_task(runtime: Path, state: Path, *, candidate_sha: str = "a" * 40):
    external = runtime.parent / "aux-inputs"
    (external / "db").mkdir(parents=True, exist_ok=True)
    (external / "evidence").mkdir(parents=True, exist_ok=True)
    (external / "runs").mkdir(parents=True, exist_ok=True)
    (external / "output").mkdir(parents=True, exist_ok=True)
    (external / "sessions").mkdir(parents=True, exist_ok=True)
    (external / "config").mkdir(parents=True, exist_ok=True)
    bound_files = {
        external / "db" / "staging.sqlite": b"test-db",
        external / "config" / "symbols.json": b"test-symbols",
        external / "config" / "entitlement.json": b"test-entitlement",
        external / "config" / "web_sources.yaml": b"test-source-config",
    }
    for path, content in bound_files.items():
        path.write_bytes(content)
    (state / "secrets").mkdir(parents=True, exist_ok=True)
    (state / "secrets" / "runtime.env").write_text("TEST_ONLY=1\n", encoding="utf-8")
    input_hashes = {
        path.name: hashlib.sha256(content).hexdigest()
        for path, content in bound_files.items()
    }
    arguments = " ".join(
        f'"{token}"'
        for token in (
            "-I",
            "-B",
            "-S",
            "-X",
            f"pycache_prefix={state / 'capture-bytecode' / candidate_sha}",
            "-u",
            "-c",
            scheduler_service.AUXILIARY_BOOTSTRAP_PRELOADER,
            str(runtime / "scripts" / "dawnstrike_python_bootstrap.py"),
            hashlib.sha256(
                (runtime / "scripts" / "dawnstrike_python_bootstrap.py").read_bytes()
            ).hexdigest(),
            "--release-root",
            str(runtime),
            "--expected-sha",
            candidate_sha,
            "--script",
            str(runtime / "scripts" / "run_daily_intraday_capture.py"),
            "--",
            "--candidate-sha",
            candidate_sha,
            "--repo-root",
            str(runtime),
            "--db-path",
            str(external / "db" / "staging.sqlite"),
            "--evidence-root",
            str(external / "evidence"),
            "--run-root",
            str(external / "runs"),
            "--output-root",
            str(external / "output"),
            "--session-root",
            str(external / "sessions"),
            "--symbols-manifest",
            str(external / "config" / "symbols.json"),
            "--symbols-manifest-sha256",
            input_hashes["symbols.json"],
            "--entitlement-receipt",
            str(external / "config" / "entitlement.json"),
            "--entitlement-receipt-sha256",
            input_hashes["entitlement.json"],
            "--source-config",
            str(external / "config" / "web_sources.yaml"),
            "--source-config-sha256",
            input_hashes["web_sources.yaml"],
            "--env-file",
            str(state / "secrets" / "runtime.env"),
            "--max-pages",
            "100",
            "--retries",
            "3",
            "--execute",
        )
    )
    return {
        "name": scheduler_service.AUXILIARY_TASK_NAME,
        "task_path": "\\",
        "state": "Ready",
        "enabled": True,
        "action_count": 1,
        "trigger_count": 1,
        "host_timezone_id": scheduler_service.EXPECTED_WINDOWS_TIMEZONE_ID,
        "multiple_instances": scheduler_service.EXPECTED_MULTIPLE_INSTANCES,
        "run_level": scheduler_service.EXPECTED_RUN_LEVEL,
        "wake_to_run": True,
        "start_when_available": True,
        "stop_if_going_on_batteries": False,
        "disallow_start_if_on_batteries": False,
        "execution_time_limit": "PT3H",
        "restart_count": 3,
        "restart_interval": "PT15M",
        "logon_type": "Password",
        "principal_user_id": r"DAWNSTRIKE\capture-service",
        "trigger_type": "MSFT_TaskWeeklyTrigger",
        "trigger_enabled": True,
        "trigger_days_of_week": 62,
        "trigger_weeks_interval": 1,
        "trigger_end_boundary": None,
        "trigger_random_delay": None,
        "trigger_start_boundary": "2026-08-31T15:20:00-05:00",
        "next_run_time": "2026-09-01T15:20:00-05:00",
        "principal_sha256": "4" * 64,
        "trigger_sha256": "5" * 64,
        "settings_sha256": "6" * 64,
        "definition_contract_sha256": "3" * 64,
        "symbols_manifest_sha256": input_hashes["symbols.json"],
        "entitlement_receipt_sha256": input_hashes["entitlement.json"],
        "source_config_sha256": input_hashes["web_sources.yaml"],
        "execute": sys.executable,
        "arguments": arguments,
        "working_directory": str(runtime),
        "last_task_result": 0,
    }


def _auxiliary_contract(row: dict[str, object]) -> dict[str, object]:
    action_text = "|".join(
        str(row.get(field) or "")
        for field in ("execute", "arguments", "working_directory")
    )
    return {
        "declared": True,
        "valid": True,
        "candidate_sha": "a" * 40,
        "action_contract_sha256": hashlib.sha256(action_text.encode("utf-8")).hexdigest(),
        "definition_contract_sha256": "3" * 64,
        "principal_sha256": "4" * 64,
        "trigger_sha256": "5" * 64,
        "settings_sha256": "6" * 64,
        "symbols_manifest_sha256": str(row.get("symbols_manifest_sha256") or ""),
        "entitlement_receipt_sha256": str(row.get("entitlement_receipt_sha256") or ""),
        "source_config_sha256": str(row.get("source_config_sha256") or ""),
    }


def _auxiliary_declaration() -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.state_preparation_contract.v1",
        "sidecar_contract": "dawnstrike.account_capture_trial_sidecar.v1",
        "sidecar_version": 1,
        "legacy_schema_marker": 30,
        "required_before_activation": True,
        "capture_interpreter_path": str(scheduler_service.AUXILIARY_INTERPRETER),
        "capture_interpreter_version": scheduler_service.AUXILIARY_INTERPRETER_VERSION,
        "capture_interpreter_sha256": scheduler_service.AUXILIARY_INTERPRETER_SHA256,
        "capture_interpreter_signer_subject": (
            scheduler_service.AUXILIARY_INTERPRETER_SIGNER_SUBJECT
        ),
        "capture_interpreter_signer_thumbprint": (
            scheduler_service.AUXILIARY_INTERPRETER_SIGNER_THUMBPRINT
        ),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _prepare_auxiliary_loader_inputs(
    runtime: Path, state: Path, *, activation_raw: bytes = b"activation"
) -> tuple[dict[str, object], dict[str, object]]:
    (runtime / "config").mkdir(parents=True)
    (runtime / "config" / "state_preparation_contract.json").write_text(
        json.dumps(_auxiliary_declaration()), encoding="utf-8"
    )
    receipt_root = state / "receipts" / "capture-task"
    receipt_root.mkdir(parents=True)
    activation_id = "1" * 24
    capture = {
        "activation_id": activation_id,
        "activation_receipt_name": f"runtime-activation-{activation_id}.json",
        "activation_receipt_sha256": hashlib.sha256(activation_raw).hexdigest(),
        "runtime_origin_sha256": "e" * 64,
        "action_before_sha256": "f" * 64,
        "definition_before_sha256": "1" * 64,
        "xml_before_sha256": "2" * 64,
        "action_after_sha256": "c" * 64,
        "definition_after_sha256": "3" * 64,
        "principal_sha256": "4" * 64,
        "trigger_sha256": "5" * 64,
        "settings_sha256": "6" * 64,
        "symbols_manifest_sha256": "7" * 64,
        "entitlement_receipt_sha256": "8" * 64,
        "source_config_sha256": "9" * 64,
    }
    capture_path = receipt_root / f"capture-task-rebind-{'a' * 40}.json"
    capture_path.write_text("{}", encoding="utf-8")
    activation_root = state / "receipts" / "runtime-activation"
    activation_root.mkdir(parents=True)
    (activation_root / capture["activation_receipt_name"]).write_bytes(activation_raw)
    return capture, {
        "schema_version": "dawnstrike.runtime_activation_receipt.v1",
        "status": "COMPLETE",
        "activation_id": activation_id,
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "runtime_origin_sha256": "e" * 64,
        "state_preparation_contract": scheduler_service.AUXILIARY_SIDECAR_CONTRACT,
        "auxiliary_capture_present": True,
        "auxiliary_capture_state_after": "Disabled",
        "auxiliary_capture_action": "DISABLED_UNTIL_EXACT_SHA_REBIND",
        "auxiliary_capture_action_contract_sha256": "f" * 64,
        "auxiliary_capture_definition_contract_sha256": "1" * 64,
        "auxiliary_capture_xml_sha256": "2" * 64,
    }


def _activation_history_payload(activation_id: str) -> dict[str, object]:
    return {
        "status": "COMPLETE",
        "activation_id": activation_id,
        "candidate_sha": "a" * 40,
        "candidate_tree": "b" * 40,
        "runtime_origin_sha256": "e" * 64,
        "completed_at_utc": "2026-08-31T03:00:00+00:00",
    }


def test_scheduler_doctor_rejects_runtime_identity_change_during_query(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    stable = _stable_runtime_contract()
    changed = {**stable, "candidate_tree": "c" * 40}
    contracts = iter([stable, changed])
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_exact_activation_completion",
        lambda _runtime, _state: None,
    )
    monkeypatch.setattr(
        scheduler_service,
        "_runtime_git_contract",
        lambda _runtime: next(contracts),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["runtime_identity_status"] == "FAILED"
    assert result["runtime_identity_stable"] is False
    assert result["runtime_identity_failed"] is True


def test_scheduler_doctor_accepts_valid_ready_governed_auxiliary(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "LOCAL_VERIFIED"
    assert result["unexpected_enabled_tasks"] == []
    assert result["expected_task_names"] == list(scheduler_service.EXPECTED_TASKS)
    assert result["governed_auxiliary_task"]["status"] == "LOCAL_VERIFIED"
    assert result["governed_auxiliary_task"]["last_task_result"] == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("last_task_result", 1),
        ("trigger_count", 2),
        ("logon_type", "InteractiveToken"),
        ("stop_if_going_on_batteries", True),
        ("wake_to_run", False),
    ],
)
def test_scheduler_doctor_blocks_nonoperational_ready_auxiliary(
    tmp_path: Path, monkeypatch, field: str, value: object
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    auxiliary[field] = value
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)
    check = result["governed_auxiliary_task"]

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [auxiliary]
    assert check["definition_status"] == "READY"
    assert check["operational_ready"] is False
    assert check["operational_status"] == "BLOCKED_NONOPERATIONAL"
    assert check["failure_reason"] == (
        "auxiliary task is not operationally unattended-safe"
    )


@pytest.mark.parametrize(
    "arguments_mutator",
    [
        lambda args: args.replace('"-I" "-B"', '"-B" "-I"', 1),
        lambda args: args.replace('"-I" "-B"', '"-I"', 1),
        lambda args: args.replace('"-S" ', "", 1),
        lambda args: args.replace('"-X" "pycache_prefix=', '"-X" "-X" "pycache_prefix=', 1),
        lambda args: args.replace('"-u"', '"-u" "python.exe"', 1),
        lambda args: args.replace('"pycache_prefix=', '"pycache_prefix=C:\\hostile\\', 1),
        lambda args: args.replace(
            "dawnstrike_python_bootstrap.py", "wrong_bootstrap.py", 1
        ),
        lambda args: args.replace(
            scheduler_service.AUXILIARY_BOOTSTRAP_PRELOADER,
            "import sys",
            1,
        ),
        lambda args: args.replace(
            '"-c" "' + scheduler_service.AUXILIARY_BOOTSTRAP_PRELOADER + '" ',
            "",
            1,
        ),
        lambda args: re.sub(
            r'"[0-9a-f]{64}" "--release-root"',
            '"' + "b" * 64 + '" "--release-root"',
            args,
            count=1,
        ),
        lambda args: re.sub(
            r'"[0-9a-f]{64}"\s+"--release-root"',
            '"--release-root"',
            args,
            count=1,
        ),
        lambda args: args.replace(
            '"--expected-sha" "' + "a" * 40 + '"',
            '"--expected-sha" "' + "b" * 40 + '"',
            1,
        ),
        lambda args: re.sub(
            r'"--expected-sha" "[0-9a-f]{40}"\s+',
            "",
            args,
            count=1,
        ),
        lambda args: re.sub(
            r'"[^"\r\n]*dawnstrike_python_bootstrap\.py"\s*', "", args, count=1
        ),
        lambda args: re.sub(
            r'("--release-root"\s+)"[^"\r\n]+"',
            r'\1"C:\\hostile"',
            args,
            count=1,
        ),
        lambda args: re.sub(
            r'"--release-root"\s+"[^"\r\n]+"\s*', "", args, count=1
        ),
        lambda args: args.replace("run_daily_intraday_capture.py", "wrong_runner.py", 1),
        lambda args: re.sub(
            r'"--script"\s+"[^"\r\n]*run_daily_intraday_capture\.py"\s*',
            "",
            args,
            count=1,
        ),
        lambda args: args.replace('"--" ', "", 1),
        lambda args: re.sub(
            r'"[^"\r\n]*dawnstrike_python_bootstrap\.py"\s+'
            r'"--release-root"\s+"[^"\r\n]+"\s+"--script"\s+',
            "",
            args.replace('"-S" ', "", 1),
            count=1,
        ),
    ],
    ids=[
        "reordered",
        "missing-b",
        "missing-s",
        "duplicate",
        "interpreter-shadow",
        "wrong-pycache-root",
        "wrong-bootstrap",
        "wrong-preloader",
        "missing-preloader",
        "wrong-bootstrap-hash",
        "missing-bootstrap-hash",
        "wrong-expected-sha",
        "missing-expected-sha",
        "missing-bootstrap",
        "wrong-release-root",
        "missing-release-root",
        "wrong-runner",
        "missing-runner",
        "missing-separator",
        "old-direct-runner-action",
    ],
)
def test_scheduler_doctor_rejects_auxiliary_prefix_variants(
    tmp_path: Path, monkeypatch, arguments_mutator
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    auxiliary["arguments"] = arguments_mutator(auxiliary["arguments"])
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [auxiliary]
    assert result["governed_auxiliary_task"]["status"] == "FAILED"


@pytest.mark.parametrize(
    ("relative_path", "match_field"),
    [
        (scheduler_service.AUXILIARY_PYTHON_BOOTSTRAP, "bootstrap_matches"),
        (scheduler_service.AUXILIARY_CAPTURE_RUNNER, "runner_matches"),
    ],
)
@pytest.mark.parametrize("unsafe_shape", ["missing", "directory"])
def test_scheduler_doctor_rejects_unsafe_auxiliary_action_files(
    tmp_path: Path,
    monkeypatch,
    relative_path: Path,
    match_field: str,
    unsafe_shape: str,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    unsafe_path = runtime / relative_path
    unsafe_path.unlink()
    if unsafe_shape == "directory":
        unsafe_path.mkdir()
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    check = result["governed_auxiliary_task"]
    assert check["status"] == "FAILED"
    assert check[match_field] is False


def test_scheduler_doctor_rejects_tampered_auxiliary_bootstrap(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    (runtime / scheduler_service.AUXILIARY_PYTHON_BOOTSTRAP).write_text(
        "tampered", encoding="utf-8"
    )
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    check = result["governed_auxiliary_task"]
    assert check["status"] == "FAILED"
    assert check["bootstrap_matches"] is True
    action = scheduler_service._validate_auxiliary_action(
        auxiliary, runtime, state, _auxiliary_contract(auxiliary)
    )
    assert action["bootstrap_sha_matches"] is False


@pytest.mark.parametrize("duplicate_option", ["--candidate-sha", "--db-path"])
def test_scheduler_doctor_rejects_duplicate_auxiliary_options(
    tmp_path: Path, monkeypatch, duplicate_option: str
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    auxiliary["arguments"] = auxiliary["arguments"].replace(
        '"--execute"', f'"{duplicate_option}" "duplicate" "--execute"', 1
    )
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [auxiliary]
    assert result["governed_auxiliary_task"]["status"] == "FAILED"


def test_scheduler_doctor_rejects_reordered_auxiliary_options(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    auxiliary["arguments"] = auxiliary["arguments"].replace(
        '"--candidate-sha" "' + "a" * 40 + '" "--repo-root"',
        '"--repo-root"',
        1,
    ).replace(
        '"--db-path"',
        '"--candidate-sha" "' + "a" * 40 + '" "--db-path"',
        1,
    )
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [auxiliary]
    assert result["governed_auxiliary_task"]["status"] == "FAILED"


@pytest.mark.parametrize("duplicate_order", ["expected-first", "hostile-first"])
def test_auxiliary_loader_rejects_duplicate_sidecar_keys(
    tmp_path: Path, monkeypatch, duplicate_order: str
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    declaration = json.dumps(_auxiliary_declaration(), separators=(",", ":"))
    if duplicate_order == "expected-first":
        declaration = declaration[:-1] + ',"schema_version":"hostile"}'
    else:
        declaration = '{"schema_version":"hostile",' + declaration[1:]
    (runtime / "config").mkdir()
    (runtime / "config" / "state_preparation_contract.json").write_text(
        declaration, encoding="utf-8"
    )

    result = scheduler_service._load_auxiliary_contract(runtime, state)

    assert result == {
        "declared": True,
        "valid": False,
        "reason": "sidecar declaration is invalid",
    }


def test_auxiliary_loader_rejects_missing_activation_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    capture, _activation = _prepare_auxiliary_loader_inputs(runtime, state)
    import scripts.capture_task_contract as capture_contract

    monkeypatch.setattr(scheduler_service, "_runtime_git_sha", lambda _root: "a" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_tree", lambda _root: "b" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_origin_sha", lambda _root: "e" * 64)
    monkeypatch.setattr(scheduler_service, "_runtime_git_origin_main", lambda _root: "a" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_clean", lambda _root: True)
    monkeypatch.setattr(capture_contract, "load_receipt", lambda *_args, **_kwargs: capture)
    (state / "receipts" / "runtime-activation" / capture["activation_receipt_name"]).unlink()

    result = scheduler_service._load_auxiliary_contract(runtime, state)

    assert result["valid"] is False
    assert result["reason"] == "capture sidecar receipt is invalid"


def test_auxiliary_loader_rejects_tampered_activation_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    capture, _activation = _prepare_auxiliary_loader_inputs(runtime, state)
    import scripts.capture_task_contract as capture_contract

    monkeypatch.setattr(scheduler_service, "_runtime_git_sha", lambda _root: "a" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_tree", lambda _root: "b" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_origin_sha", lambda _root: "e" * 64)
    monkeypatch.setattr(scheduler_service, "_runtime_git_origin_main", lambda _root: "a" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_clean", lambda _root: True)
    monkeypatch.setattr(capture_contract, "load_receipt", lambda *_args, **_kwargs: capture)
    activation_path = (
        state
        / "receipts"
        / "runtime-activation"
        / capture["activation_receipt_name"]
    )
    activation_path.write_bytes(b"tampered")

    result = scheduler_service._load_auxiliary_contract(runtime, state)

    assert result["valid"] is False
    assert result["reason"] == "capture sidecar receipt is invalid"


@pytest.mark.parametrize("field", ["status", "candidate_sha", "candidate_tree"])
def test_auxiliary_loader_rejects_unbound_activation_receipt(
    tmp_path: Path, monkeypatch, field: str
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    capture, activation = _prepare_auxiliary_loader_inputs(runtime, state)
    import scripts.capture_task_contract as capture_contract
    import scripts.runtime_activation_contract as activation_contract

    monkeypatch.setattr(scheduler_service, "_runtime_git_sha", lambda _root: "a" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_tree", lambda _root: "b" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_origin_sha", lambda _root: "e" * 64)
    monkeypatch.setattr(scheduler_service, "_runtime_git_origin_main", lambda _root: "a" * 40)
    monkeypatch.setattr(scheduler_service, "_runtime_git_clean", lambda _root: True)
    monkeypatch.setattr(capture_contract, "load_receipt", lambda *_args, **_kwargs: capture)
    monkeypatch.setattr(activation_contract, "load_receipt", lambda *_args, **_kwargs: activation)
    invalid = dict(activation)
    invalid[field] = "PREPARED" if field == "status" else "d" * 40
    monkeypatch.setattr(activation_contract, "load_receipt", lambda *_args, **_kwargs: invalid)

    result = scheduler_service._load_auxiliary_contract(runtime, state)

    assert result["valid"] is False
    assert result["reason"] == "capture sidecar receipt is invalid"


def test_auxiliary_loader_rechecks_clean_runtime_after_receipt_validation(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    capture, activation = _prepare_auxiliary_loader_inputs(runtime, state)
    import scripts.capture_task_contract as capture_contract
    import scripts.runtime_activation_contract as activation_contract

    contracts = iter([_stable_runtime_contract(), None])
    monkeypatch.setattr(
        scheduler_service,
        "_runtime_git_contract",
        lambda _root: next(contracts),
    )
    monkeypatch.setattr(capture_contract, "load_receipt", lambda *_args, **_kwargs: capture)
    monkeypatch.setattr(
        activation_contract,
        "load_receipt",
        lambda *_args, **_kwargs: activation,
    )

    result = scheduler_service._load_auxiliary_contract(runtime, state)

    assert result["valid"] is False
    assert result["reason"] == "capture sidecar receipt is invalid"


def test_scheduler_doctor_rejects_undeclared_enabled_auxiliary(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: {
            "declared": False,
            "valid": False,
            "reason": "sidecar declaration is missing",
        },
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [auxiliary]
    assert result["governed_auxiliary_task"]["failure_reason"] == (
        "sidecar declaration is missing"
    )


def test_scheduler_doctor_rejects_missing_auxiliary_sidecar(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: {
            "declared": True,
            "valid": False,
            "reason": "capture sidecar receipt is missing or ambiguous",
        },
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [auxiliary]


@pytest.mark.parametrize("drift", ["root", "sha", "hash"])
def test_scheduler_doctor_rejects_auxiliary_contract_drift(
    tmp_path: Path, monkeypatch, drift: str
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    contract = _auxiliary_contract(auxiliary)
    if drift == "root":
        auxiliary["working_directory"] = str(tmp_path / "other-runtime")
    elif drift == "sha":
        auxiliary["arguments"] = str(auxiliary["arguments"]).replace("a" * 40, "b" * 40)
    else:
        contract["action_contract_sha256"] = "b" * 64
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: contract,
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [auxiliary]
    assert result["governed_auxiliary_task"]["status"] == "FAILED"


def test_scheduler_doctor_rejects_duplicate_auxiliary_definitions(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    rows.extend([auxiliary, {**auxiliary, "arguments": auxiliary["arguments"] + " "+'"duplicate"'}])
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["governed_auxiliary_task"]["failure_reason"] == (
        "auxiliary task is duplicated"
    )
    assert result["unexpected_enabled_tasks"] == rows[-2:]


def test_scheduler_doctor_allows_disabled_undeclared_auxiliary(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    auxiliary.update(state="Disabled", enabled=False)
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: {
            "declared": False,
            "valid": False,
            "reason": "sidecar declaration is missing",
        },
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "LOCAL_VERIFIED"
    assert result["unexpected_enabled_tasks"] == []
    assert result["governed_auxiliary_task"]["definition_status"] == (
        "DISABLED_UNDECLARED"
    )


def test_scheduler_doctor_accepts_disabled_governed_auxiliary(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    auxiliary.update(state="Disabled", enabled=False)
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: _auxiliary_contract(auxiliary),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "LOCAL_VERIFIED"
    assert result["unexpected_enabled_tasks"] == []
    assert result["governed_auxiliary_task"]["definition_status"] == "DISABLED"


def test_scheduler_doctor_rejects_missing_receipt_governed_auxiliary(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: {
            **_auxiliary_contract(_auxiliary_task(runtime, state)),
            "declared": True,
            "valid": True,
        },
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    checked = result["governed_auxiliary_task"]
    assert checked["status"] == "FAILED"
    assert checked["definition_status"] == "MISSING"
    assert checked["failure_reason"] == "governed auxiliary task is missing"


@pytest.mark.parametrize(
    ("field", "match_field"),
    [
        ("principal_sha256", "principal_contract_matches"),
        ("trigger_sha256", "trigger_contract_matches"),
        ("settings_sha256", "settings_contract_matches"),
        ("definition_contract_sha256", "definition_contract_matches"),
    ],
)
def test_scheduler_doctor_rejects_disabled_governed_auxiliary_definition_drift(
    tmp_path: Path, monkeypatch, field: str, match_field: str
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    auxiliary = _auxiliary_task(runtime, state)
    contract = _auxiliary_contract(auxiliary)
    auxiliary.update(state="Disabled", enabled=False)
    auxiliary[field] = "0" * 64
    rows.append(auxiliary)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_auxiliary_contract",
        lambda _runtime, _state: contract,
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    checked = result["governed_auxiliary_task"]
    assert checked["status"] == "FAILED"
    assert checked["governed"] is False
    assert checked["definition_status"] == "INVALID"
    assert checked["operational_status"] == "DISABLED"
    assert checked[match_field] is False


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
    rows = _healthy_tasks(runtime, state)
    monkeypatch.setattr(
        scheduler_service,
        "_query_scheduled_tasks",
        lambda: rows,
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "LOCAL_VERIFIED"
    assert result["failed_task_count"] == 0
    assert result["durable_source_config"]["ready"] is True
    assert all(row["legacy_root_absent"] for row in result["scheduled_tasks"])


def test_scheduler_doctor_rejects_semantically_invalid_durable_config(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    (state / "config" / "web_sources.yaml").write_text(
        "enabled: true\nuser_agent: REQUIRED_ACCOUNTABLE_EMAIL\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "FAILED"
    assert result["durable_source_config"]["ready"] is False


def test_scheduler_doctor_rejects_wrong_execution_deadline(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    rows[1]["execution_time_limit"] = "PT2H"
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == 1


def test_scheduler_doctor_blocks_failed_or_stale_task_history(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state, last_result=1)
    monkeypatch.setattr(
        scheduler_service,
        "_query_scheduled_tasks",
        lambda: rows,
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == len(scheduler_service.EXPECTED_TASKS)
    assert all(
        row["last_run_status"] == "STALE_OR_FAILED"
        for row in result["scheduled_tasks"]
    )


def test_activation_history_accepts_one_exact_clean_runtime_receipt(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    activation_id = "1" * 24
    receipt_root = state / "receipts" / "runtime-activation"
    receipt_root.mkdir(parents=True)
    (receipt_root / f"runtime-activation-{activation_id}.json").write_text(
        "{}", encoding="utf-8"
    )
    (receipt_root / f"runtime-activation-{activation_id}.prepared.json").write_text(
        "{}", encoding="utf-8"
    )
    import scripts.runtime_activation_contract as activation_contract

    monkeypatch.setattr(
        activation_contract,
        "load_receipt",
        lambda _path: _activation_history_payload(activation_id),
    )

    completed = scheduler_service._load_exact_activation_completion(runtime, state)

    assert completed == datetime.fromisoformat("2026-08-31T03:00:00+00:00")


def test_activation_history_requires_stable_clean_exact_origin_runtime(
    tmp_path: Path, monkeypatch
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    activation_id = "1" * 24
    receipt_root = state / "receipts" / "runtime-activation"
    receipt_root.mkdir(parents=True)
    (receipt_root / f"runtime-activation-{activation_id}.json").write_text(
        "{}", encoding="utf-8"
    )
    import scripts.runtime_activation_contract as activation_contract

    monkeypatch.setattr(
        activation_contract,
        "load_receipt",
        lambda _path: _activation_history_payload(activation_id),
    )
    contracts = iter([_stable_runtime_contract(), None])
    monkeypatch.setattr(
        scheduler_service,
        "_runtime_git_contract",
        lambda _runtime: next(contracts),
    )

    assert scheduler_service._load_exact_activation_completion(runtime, state) is None


@pytest.mark.parametrize("failure", ["origin", "tampered", "ambiguous"])
def test_activation_history_rejects_unbound_or_ambiguous_receipts(
    tmp_path: Path, monkeypatch, failure: str
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    receipt_root = state / "receipts" / "runtime-activation"
    receipt_root.mkdir(parents=True)
    first_id = "1" * 24
    second_id = "2" * 24
    first_path = receipt_root / f"runtime-activation-{first_id}.json"
    first_path.write_text("{}", encoding="utf-8")
    if failure in {"tampered", "ambiguous"}:
        (receipt_root / f"runtime-activation-{second_id}.json").write_text(
            "{}", encoding="utf-8"
        )
    import scripts.runtime_activation_contract as activation_contract

    def load_receipt(path: Path) -> dict[str, object]:
        if failure == "tampered" and path.name.endswith(f"{second_id}.json"):
            raise ValueError("tampered")
        activation_id = second_id if path.name.endswith(f"{second_id}.json") else first_id
        payload = _activation_history_payload(activation_id)
        if failure == "origin":
            payload["runtime_origin_sha256"] = "f" * 64
        return payload

    monkeypatch.setattr(activation_contract, "load_receipt", load_receipt)

    assert scheduler_service._load_exact_activation_completion(runtime, state) is None


@pytest.mark.parametrize(
    "completed_at",
    ["2026-08-31T07:59:59-05:00", "2026-08-31T08:00:00-05:00"],
)
def test_activation_history_does_not_supersede_newer_or_equal_failure(
    completed_at: str,
) -> None:
    assert (
        scheduler_service._history_superseded_by_exact_runtime_activation(
            last_run_time="2026-08-31T08:00:00-05:00",
            activation_completed_at=datetime.fromisoformat(completed_at),
        )
        is False
    )


def test_scheduler_doctor_accepts_failure_from_replaced_task_definition(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    morning = next(
        row
        for row in rows
        if row["name"] == "Dawnstrike AlphaOps Morning"
    )
    morning["last_task_result"] = 127
    morning["last_run_time"] = "2026-07-30T08:10:00-05:00"
    morning["trigger_start_boundary"] = "2026-07-31T08:00:00-05:00"
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)
    monkeypatch.setattr(
        scheduler_service,
        "_load_exact_activation_completion",
        lambda _runtime, _state: datetime.fromisoformat("2026-08-01T00:00:00+00:00"),
    )

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "LOCAL_VERIFIED"
    checked = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Morning"
    )
    assert checked["last_run_status"] == "SUPERSEDED_BY_EXACT_RUNTIME_ACTIVATION"
    assert checked["history_superseded_by_exact_runtime_activation"] is True


@pytest.mark.parametrize(
    "last_run_time",
    [
        "2026-07-31T07:45:00-05:00",
        "2026-07-31T08:00:00-05:00",
        "2026-07-31T08:15:00-05:00",
    ],
)
def test_scheduler_doctor_keeps_same_day_failure_blocking(
    tmp_path: Path,
    monkeypatch,
    last_run_time: str,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    morning = next(
        row
        for row in rows
        if row["name"] == "Dawnstrike AlphaOps Morning"
    )
    morning["last_task_result"] = 1
    morning["last_run_time"] = last_run_time
    morning["trigger_start_boundary"] = "2026-07-31T08:00:00-05:00"
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    checked = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Morning"
    )
    assert checked["last_run_status"] == "STALE_OR_FAILED"
    assert checked["history_superseded_by_exact_runtime_activation"] is False


def test_scheduler_doctor_uses_trigger_start_for_repeating_task(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    monitor = next(
        row
        for row in rows
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    monitor["trigger_start_boundary"] = "2026-07-31T08:35:00-05:00"
    monitor["last_run_time"] = "2026-07-31T14:00:00-05:00"
    monitor["next_run_time"] = "2026-07-31T14:05:00-05:00"
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "LOCAL_VERIFIED"
    checked = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    assert checked["scheduled_time_matches"] is True


def test_scheduler_doctor_rejects_wrong_trigger_start_even_if_next_run_matches(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    monitor = next(
        row
        for row in rows
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    monitor["trigger_start_boundary"] = "2026-07-01T08:30:00-05:00"
    monitor["next_run_time"] = "2026-07-31T08:35:00-05:00"
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    checked = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    assert checked["scheduled_time_matches"] is False


@pytest.mark.parametrize(
    "trigger_start_boundary",
    [
        "not-isoT08:35:00",
        "2026-07-31T08:35:00",
        "2026-07-31T08:35:00.123-05:00",
    ],
)
def test_scheduler_doctor_rejects_invalid_or_naive_trigger_boundary(
    tmp_path: Path,
    monkeypatch,
    trigger_start_boundary: str,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    monitor = next(
        row
        for row in rows
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    monitor["trigger_start_boundary"] = trigger_start_boundary
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    checked = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    assert checked["scheduled_time_matches"] is False


@pytest.mark.parametrize(
    ("duplicate_name", "duplicate_arguments"),
    [
        ("Dawnstrike AlphaOps Morning Early", None),
        (
            "Dawnstrike Finalize Recovery Copy",
            '-File "D:\\legacy-dawnstrike\\wrapper.ps1"',
        ),
    ],
)
def test_scheduler_doctor_blocks_enabled_duplicate_runner(
    tmp_path: Path,
    monkeypatch,
    duplicate_name: str,
    duplicate_arguments: str | None,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    duplicate = {
        **rows[0],
        "name": duplicate_name,
        "next_run_time": "2026-07-31T07:15:00-05:00",
    }
    if duplicate_arguments is not None:
        duplicate["arguments"] = duplicate_arguments
    rows.append(duplicate)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert [row["name"] for row in result["unexpected_enabled_tasks"]] == [duplicate_name]


def test_scheduler_doctor_blocks_same_name_in_alternate_task_path(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    alternate = {
        **rows[0],
        "task_path": "\\DawnstrikeShadow\\",
    }
    rows.append(alternate)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [alternate]


@pytest.mark.parametrize("state_value", ["Queued", "Running"])
def test_scheduler_doctor_blocks_disabled_active_duplicate(
    tmp_path: Path,
    monkeypatch,
    state_value: str,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    duplicate = {
        **rows[0],
        "name": "Dawnstrike AlphaOps Morning Previous",
        "state": state_value,
        "enabled": False,
    }
    rows.append(duplicate)
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["unexpected_enabled_tasks"] == [duplicate]


@pytest.mark.parametrize(
    ("field", "value", "match_field"),
    [
        ("action_count", 2, "action_count_matches"),
        ("trigger_count", 2, "trigger_count_matches"),
        ("execute", "cmd.exe", "executable_matches"),
    ],
)
def test_scheduler_doctor_requires_exact_action_shape(
    tmp_path: Path,
    monkeypatch,
    field: str,
    value: object,
    match_field: str,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    rows[0][field] = value
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == 1
    assert result["scheduled_tasks"][0][match_field] is False


def test_scheduler_doctor_rejects_inert_command_containing_expected_paths(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    runner = runtime / "scripts" / scheduler_service.EXPECTED_TASKS[rows[0]["name"]]
    rows[0]["arguments"] = (
        f'-Command "Write-Output \'{runner} {runtime} {state}\'"'
    )
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == 1
    assert result["scheduled_tasks"][0]["action_arguments_match"] is False


def test_scheduler_doctor_preserves_case_sensitive_publication_identity(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    finalize = next(
        row
        for row in rows
        if row["name"] == scheduler_service.CANONICAL_TASK_NAME
    )
    finalize["arguments"] = finalize["arguments"].replace(
        scheduler_service.EXPECTED_VERCEL_PROJECT_ID,
        scheduler_service.EXPECTED_VERCEL_PROJECT_ID.swapcase(),
    )
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == 1
    checked = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == scheduler_service.CANONICAL_TASK_NAME
    )
    assert checked["action_arguments_match"] is False


@pytest.mark.parametrize(
    ("task_name", "field", "value", "match_field"),
    [
        (
            "Dawnstrike AlphaOps Morning",
            "trigger_enabled",
            False,
            "trigger_enabled_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "trigger_type",
            "MSFT_TaskDailyTrigger",
            "trigger_type_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "trigger_days_of_week",
            2,
            "trigger_days_of_week_matches",
        ),
        (
            "Dawnstrike AlphaOps Monitor 5m",
            "repetition_interval",
            "PT10M",
            "repetition_interval_matches",
        ),
        (
            scheduler_service.CANONICAL_TASK_NAME,
            "trigger_days_interval",
            2,
            "trigger_days_interval_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "multiple_instances",
            "Parallel",
            "multiple_instances_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "host_timezone_id",
            "Eastern Standard Time",
            "host_timezone_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "trigger_start_boundary",
            "2099-07-31T08:00:00-05:00",
            "trigger_active_on_observation_date",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "trigger_end_boundary",
            "2026-07-30T08:00:00-05:00",
            "trigger_end_boundary_absent",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "trigger_random_delay",
            "PT30M",
            "trigger_random_delay_absent",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "wake_to_run",
            False,
            "wake_to_run_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "run_level",
            "Highest",
            "run_level_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "restart_count",
            0,
            "restart_count_matches",
        ),
        (
            "Dawnstrike AlphaOps Morning",
            "restart_interval",
            "PT10M",
            "restart_interval_matches",
        ),
        (
            "Dawnstrike AlphaOps Monitor 5m",
            "repetition_stop_at_duration_end",
            False,
            "repetition_stop_at_duration_end_matches",
        ),
    ],
)
def test_scheduler_doctor_requires_canonical_trigger_contract(
    tmp_path: Path,
    monkeypatch,
    task_name: str,
    field: str,
    value: object,
    match_field: str,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    task = next(row for row in rows if row["name"] == task_name)
    task[field] = value
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    assert result["failed_task_count"] == 1
    checked = next(row for row in result["scheduled_tasks"] if row["name"] == task_name)
    assert checked[match_field] is False


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


def test_scheduler_doctor_rejects_monitor_overlap_duration(
    tmp_path: Path,
    monkeypatch,
) -> None:
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    _write_required_scripts(runtime)
    rows = _healthy_tasks(runtime, state)
    monitor = next(
        row
        for row in rows
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    monitor["repetition_duration"] = "PT7H"
    monkeypatch.setattr(scheduler_service, "_query_scheduled_tasks", lambda: rows)

    result = scheduler_service.scheduler_doctor(runtime, state)

    assert result["status"] == "BLOCKED_EXTERNAL"
    checked = next(
        row
        for row in result["scheduled_tasks"]
        if row["name"] == "Dawnstrike AlphaOps Monitor 5m"
    )
    assert checked["repetition_duration_matches"] is False
