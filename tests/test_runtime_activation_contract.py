from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, run_migrations
from scripts.runtime_activation_contract import (
    ACTIVATION_SCHEMA,
    CI_SCHEMA,
    ROLLBACK_SCHEMA,
    SOL_SCHEMA,
    ActivationContractError,
    inspect_state,
    load_receipt,
    seal_evidence,
    seal_receipt,
    self_hash,
    validate_evidence,
    validate_evidence_pair,
)

CANDIDATE_SHA = "a" * 40
CANDIDATE_TREE = "b" * 40
PREVIOUS_SHA = "c" * 40
PREVIOUS_TREE = "d" * 40


def _now() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def _ci_payload(*, completed: datetime | None = None) -> dict[str, object]:
    return {
        "schema_version": CI_SCHEMA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "conclusion": "SUCCESS",
        "status": "COMPLETED",
        "head_branch": "main",
        "run_url": "https://github.com/example/dawnstrike/actions/runs/12345",
        "checks_total": 19,
        "checks_succeeded": 19,
        "completed_at_utc": (completed or _now()).isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _sol_payload(*, completed: datetime | None = None) -> dict[str, object]:
    return {
        "schema_version": SOL_SCHEMA,
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "auditor_model": "gpt-5.6-sol",
        "verdict": "ZERO_CRITICAL_HIGH",
        "critical_findings": 0,
        "high_findings": 0,
        "completed_at_utc": (completed or _now()).isoformat().replace("+00:00", "Z"),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _write_json(path: Path, value: object) -> None:
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _self_seal_unsafe(payload: dict[str, object]) -> dict[str, object]:
    value = dict(payload)
    value["evidence_sha256"] = self_hash(value, "evidence_sha256")
    return value


def _receipt_payload(
    *, schema: str = ACTIVATION_SCHEMA, status: str = "COMPLETE"
) -> dict[str, object]:
    activation_id = "e" * 24
    value: dict[str, object] = {
        "schema_version": schema,
        "status": status,
        "activation_id": activation_id,
        "market_date": "2026-08-31",
        "candidate_sha": CANDIDATE_SHA,
        "candidate_tree": CANDIDATE_TREE,
        "previous_sha": PREVIOUS_SHA,
        "previous_tree": PREVIOUS_TREE,
        "ci_evidence_sha256": "1" * 64,
        "sol_evidence_sha256": "2" * 64,
        "state_backup_id": f"runtime-activation-{activation_id}",
        "state_backup_db_sha256": "3" * 64,
        "state_schema_version": CURRENT_SCHEMA_VERSION,
        "state_quick_check": "ok",
        "rollback_bundle_sha256": "4" * 64,
        "task_count": 5,
        "task_contract_sha256": "5" * 64,
        "task_definition_contract_sha256": "9" * 64,
        "task_action_contract_sha256": "7" * 64,
        "task_paths_unchanged": True,
        "task_enablement_restored": status != "PREPARED",
        "scheduler_backup_name": (
            f"runtime-activation-{activation_id}"
            if schema == ACTIVATION_SCHEMA
            else f"runtime-rollback-{activation_id}"
        ),
        "scheduler_backup_manifest_sha256": "8" * 64,
        "runtime_origin_sha256": "6" * 64,
        "swap_contract": "same_volume_two_rename_with_immediate_restore",
        "prepared_at_utc": "2026-08-30T16:00:00Z",
        "completed_at_utc": "2026-08-30T16:01:00Z" if status != "PREPARED" else None,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if schema == ACTIVATION_SCHEMA:
        value.update(
            {
                "stage_name": f"dawnstrike-runtime.stage-{activation_id}",
                "rollback_checkout_name": "previous-runtime",
                "rollback_bundle_name": "previous-runtime.bundle",
            }
        )
    else:
        value["restored_sha"] = PREVIOUS_SHA
    return value


def test_exact_ci_and_sol_evidence_pair_passes(tmp_path: Path) -> None:
    ci = tmp_path / "ci.json"
    sol = tmp_path / "sol.json"
    _write_json(ci, seal_evidence(_ci_payload()))
    _write_json(sol, seal_evidence(_sol_payload()))

    result = validate_evidence_pair(
        ci,
        sol,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        now=_now(),
    )

    assert result["status"] == "PASS"
    assert result["candidate_sha"] == CANDIDATE_SHA
    assert result["research_only"] is True
    assert result["broker_execution_enabled"] is False


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        ({"candidate_sha": "f" * 40}, "candidate SHA mismatch"),
        ({"candidate_tree": "f" * 40}, "candidate tree mismatch"),
        ({"checks_succeeded": 18}, "check totals"),
        ({"checks_total": 18, "checks_succeeded": 18}, "check totals"),
        ({"conclusion": "FAILURE"}, "completed success"),
        ({"head_branch": "feature"}, "bound to main"),
        ({"broker_execution_enabled": True}, "enables broker execution"),
    ],
)
def test_ci_evidence_fails_closed_on_hostile_mutation(
    tmp_path: Path, mutation: dict[str, object], message: str
) -> None:
    ci_payload = _ci_payload()
    ci_payload.update(mutation)
    ci = tmp_path / "ci.json"
    sol = tmp_path / "sol.json"
    _write_json(ci, _self_seal_unsafe(ci_payload))
    _write_json(sol, seal_evidence(_sol_payload()))

    with pytest.raises(ActivationContractError, match=message):
        validate_evidence_pair(
            ci,
            sol,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            now=_now(),
        )


@pytest.mark.parametrize(
    "mutation",
    [
        {"auditor_model": "gpt-5.6-luna"},
        {"verdict": "PASS_WITH_HIGH_FINDING"},
        {"critical_findings": 1},
        {"high_findings": 1},
        {"research_only": False},
    ],
)
def test_sol_evidence_rejects_nonfinal_or_unsafe_audit(
    mutation: dict[str, object],
) -> None:
    payload = _sol_payload()
    payload.update(mutation)
    with pytest.raises(ActivationContractError):
        validate_evidence(_self_seal_unsafe(payload), now=_now())


def test_evidence_rejects_stale_tampered_and_sensitive_inputs() -> None:
    stale_time = _now() - timedelta(days=31)
    stale = seal_evidence(_ci_payload(completed=stale_time))
    with pytest.raises(ActivationContractError, match="older than 30 days"):
        validate_evidence(stale, now=_now())

    tampered = seal_evidence(_ci_payload())
    tampered["checks_total"] = 20
    with pytest.raises(ActivationContractError, match="self-hash mismatch"):
        validate_evidence(tampered, now=_now())

    sensitive = _ci_payload()
    sensitive["api_token"] = "must-not-appear"
    sensitive["evidence_sha256"] = "0" * 64
    with pytest.raises(ActivationContractError, match="sensitive field"):
        validate_evidence(sensitive, now=_now())


def test_state_inspection_is_read_only_and_requires_exact_schema(tmp_path: Path) -> None:
    db = tmp_path / "shadow_real.sqlite"
    with sqlite3.connect(db) as connection:
        assert run_migrations(connection) == CURRENT_SCHEMA_VERSION
    before = hashlib.sha256(db.read_bytes()).hexdigest()

    result = inspect_state(db)

    assert result["quick_check"] == "ok"
    assert result["schema_version"] == CURRENT_SCHEMA_VERSION
    assert hashlib.sha256(db.read_bytes()).hexdigest() == before
    assert not db.with_name(f"{db.name}-wal").exists()
    assert not db.with_name(f"{db.name}-shm").exists()


def test_state_inspection_rejects_missing_or_incompatible_schema(tmp_path: Path) -> None:
    missing = tmp_path / "shadow_real.sqlite"
    with sqlite3.connect(missing) as connection:
        connection.execute("CREATE TABLE unrelated (id INTEGER)")
    with pytest.raises(ActivationContractError, match="schema_version table is missing"):
        inspect_state(missing)

    incompatible = tmp_path / "other" / "shadow_real.sqlite"
    incompatible.parent.mkdir()
    with sqlite3.connect(incompatible) as connection:
        connection.execute("CREATE TABLE schema_version (version INTEGER NOT NULL)")
        connection.execute("INSERT INTO schema_version VALUES (?)", (CURRENT_SCHEMA_VERSION - 1,))
    with pytest.raises(ActivationContractError, match="does not exactly match"):
        inspect_state(incompatible)


def test_activation_and_rollback_receipts_are_strict_self_hashed_and_atomic(
    tmp_path: Path,
) -> None:
    activation_path = tmp_path / "receipts" / "activation.json"
    activation = seal_receipt(_receipt_payload(), activation_path)
    assert load_receipt(activation_path) == activation
    assert activation["status"] == "COMPLETE"
    with pytest.raises(ActivationContractError, match="already exists"):
        seal_receipt(_receipt_payload(), activation_path)

    rollback_path = tmp_path / "receipts" / "rollback.json"
    rollback = seal_receipt(
        _receipt_payload(schema=ROLLBACK_SCHEMA, status="ROLLED_BACK"),
        rollback_path,
    )
    assert load_receipt(rollback_path) == rollback
    assert rollback["restored_sha"] == PREVIOUS_SHA
    assert not list(activation_path.parent.glob("*.tmp"))


@pytest.mark.parametrize(
    "mutation",
    [
        {"task_paths_unchanged": False},
        {"broker_execution_enabled": True},
        {"state_quick_check": "corrupt"},
        {"state_schema_version": CURRENT_SCHEMA_VERSION - 1},
        {"rollback_bundle_sha256": "not-a-hash"},
        {"stage_name": "..\\escape"},
        {"scheduler_backup_name": "runtime-activation-" + "f" * 24},
        {"state_backup_id": "runtime-activation-" + "f" * 24},
        {"market_date": "2026-99-99"},
        {"completed_at_utc": "2026-08-30T15:59:59Z"},
    ],
)
def test_activation_receipt_rejects_unsafe_or_mismatched_contract(
    tmp_path: Path, mutation: dict[str, object]
) -> None:
    payload = _receipt_payload()
    payload.update(mutation)
    with pytest.raises(ActivationContractError):
        seal_receipt(payload, tmp_path / "receipt.json")
    assert not (tmp_path / "receipt.json").exists()


def test_activation_receipt_rejects_extra_fields_and_tampering(tmp_path: Path) -> None:
    payload = _receipt_payload()
    payload["operator_secret"] = "forbidden"
    with pytest.raises(ActivationContractError, match="sensitive field"):
        seal_receipt(payload, tmp_path / "extra.json")

    path = tmp_path / "receipt.json"
    seal_receipt(_receipt_payload(), path)
    stored = json.loads(path.read_text(encoding="utf-8"))
    stored["candidate_sha"] = "f" * 40
    _write_json(path, stored)
    with pytest.raises(ActivationContractError, match="self-hash mismatch"):
        load_receipt(path)


def test_windows_activation_scripts_preserve_nonpublishing_fail_closed_boundary() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    combined = activation + rollback

    assert "refs/remotes/origin/main" in activation
    assert "merge-base" in activation and "--is-ancestor" in activation
    assert "state_disaster_recovery.py" in activation
    assert '"bundle", "create"' in activation
    assert "same_volume_two_rename_with_immediate_restore" in combined
    assert "Get-DawnstrikeTaskContract" in combined
    assert "Enter-DawnstrikeDailyRunLock" in combined
    assert "broker_execution_enabled = $false" in combined
    assert "research_only = $true" in combined
    assert "publish_vercel" not in combined.lower()
    assert "telegram" not in combined.lower()
    assert "Remove-Item -LiteralPath $runtime" not in combined
    assert "Directory]::Move" in combined
    assert "scheduler-backups" in combined
    assert "Disable-ScheduledTask" in combined
    assert "Enable-ScheduledTask" in combined
    contract_validation = rollback.index("$contractGit = Get-DawnstrikeGitContract")
    contract_execution = rollback.index("$activation = Invoke-DawnstrikeContractCli")
    assert contract_validation < contract_execution
    disable_call = activation.index("            Disable-DawnstrikeCanonicalTasks")
    activation_swap = activation.index("[System.IO.Directory]::Move($runtime, $rollbackCheckout)")
    enable_call = activation.index("            Enable-DawnstrikeCanonicalTasks", activation_swap)
    assert disable_call < activation_swap < enable_call
    rollback_disable = rollback.index("            Disable-DawnstrikeCanonicalTasks")
    rollback_swap = rollback.index("[System.IO.Directory]::Move($runtime, $deactivatedCandidate)")
    rollback_enable = rollback.index("        Enable-DawnstrikeCanonicalTasks", rollback_swap)
    assert rollback_disable < rollback_swap < rollback_enable


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_task_contract_requires_exact_ready_or_explicit_disabled(tmp_path: Path) -> None:
    script = Path("scripts/activate_dawnstrike_runtime.ps1").resolve()
    safe = str(script).replace("'", "''")
    command = rf"""
. '{safe}'
$global:MockTaskState = 'Ready'
$global:MockDefinition = 'morning-v1'
function Get-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName)
    [pscustomobject]@{{
        State=$global:MockTaskState; TaskPath='\';
        Actions=@([pscustomobject]@{{
            Execute='powershell.exe';
            Arguments='-RuntimeRoot "C:\runtime" -StateRoot "C:\state"';
            WorkingDirectory='C:\runtime'
        }})
    }}
}}
function Export-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $enabled = if ($global:MockTaskState -eq 'Disabled') {{ 'false' }} else {{ 'true' }}
    "<Task><Name>$TaskName</Name><Description>$global:MockDefinition</Description><Settings><Enabled>$enabled</Enabled></Settings></Task>"
}}
$ready = Get-DawnstrikeTaskContract -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state'
$blocked = @{{}}
foreach ($state in @('Running', 'Queued', 'Unknown', 'Disabled')) {{
    $global:MockTaskState = $state
    $didBlock = $false
    try {{ $null = Get-DawnstrikeTaskContract -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' }}
        catch {{ $didBlock = $_.Exception.Message -match 'not in an approved exact state' }}
    $blocked[$state] = $didBlock
}}
$global:MockTaskState = 'Disabled'
$disabled = Get-DawnstrikeTaskContract `
    -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' -AllowDisabled
$global:MockDefinition = 'hostile-trigger-drift'
$mutated = Get-DawnstrikeTaskContract `
    -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' -AllowDisabled
$result = [pscustomobject]@{{
    ready=$ready
    blocked=$blocked
    disabled=$disabled
    mutated=$mutated
}}
$result | ConvertTo-Json -Depth 5 -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["ready"] is not None, (result.stdout, result.stderr)
    assert payload["ready"]["task_count"] == 5
    assert len(payload["ready"]["task_contract_sha256"]) == 64
    assert payload["blocked"] == {
        "Disabled": True,
        "Queued": True,
        "Running": True,
        "Unknown": True,
    }
    assert payload["disabled"]["disabled_count"] == 5
    assert payload["disabled"]["enabled_count"] == 0
    assert (
        payload["disabled"]["task_definition_contract_sha256"]
        == payload["ready"]["task_definition_contract_sha256"]
    )
    assert payload["disabled"]["task_contract_sha256"] != payload["ready"]["task_contract_sha256"]
    assert (
        payload["mutated"]["task_definition_contract_sha256"]
        != payload["ready"]["task_definition_contract_sha256"]
    )


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_git_contract_rejects_ignored_executable_artifact(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(checkout)],
        check=True,
        capture_output=True,
    )
    _git(checkout, "config", "user.email", "activation-test@example.invalid")
    _git(checkout, "config", "user.name", "Activation Test")
    (checkout / ".gitignore").write_text("*.pyc\n.pytest_cache/\n", encoding="utf-8")
    (checkout / "safe.txt").write_text("safe\n", encoding="utf-8")
    _git(checkout, "add", ".gitignore", "safe.txt")
    _git(checkout, "commit", "-m", "safe")
    (checkout / "hostile.pyc").write_bytes(b"hostile ignored executable")
    (checkout / ".pytest_cache").mkdir()
    (checkout / ".pytest_cache" / "README.md").write_text("inert\n", encoding="utf-8")

    activation = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    root = str(checkout).replace("'", "''")
    command = rf"""
. '{activation}'
. '{runner}'
$gitPath = (@(Get-Command git.exe -CommandType Application -ErrorAction Stop)[0]).Source
$blocked = $false
try {{ $null = Get-DawnstrikeGitContract -GitPath $gitPath -Root '{root}' -TimeoutSeconds 30 }}
catch {{ $blocked = $_.Exception.Message -match 'ignored executable' }}
$blocked | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) is True


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_backup_root_validation_is_absolute_isolated_and_drive_root_safe() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
. '{script}'
$result = [ordered]@{{}}
try {{
    Ensure-DawnstrikeActivationRoot 'relative-backups' 'BackupRoot' | Out-Null
    $result.relative = 'FAIL'
}}
catch {{ $result.relative = 'BLOCKED' }}
try {{
    Ensure-DawnstrikeActivationRoot 'C:\\' 'BackupRoot' | Out-Null
    $result.drive_root = 'PASS'
}}
catch {{ $result.drive_root = 'FAIL' }}
try {{
    Assert-DawnstrikeRootIsolation `
        'C:\\r\\dawnstrike-state\\nested' @('C:\\r\\dawnstrike-state') 'BackupRoot'
    $result.contained = 'FAIL'
}}
catch {{ $result.contained = 'BLOCKED' }}
$future = Join-Path $env:TEMP ('activation-future-' + $PID)
try {{
    $futurePath = Get-DawnstrikeFutureActivationRoot $future 'BackupRoot'
    $result.future = -not (Test-Path -LiteralPath $futurePath)
}}
catch {{ $result.future = $false }}
$result | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "relative": "BLOCKED",
        "drive_root": "PASS",
        "contained": "BLOCKED",
        "future": True,
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_disposable_activation_and_rollback_preserve_exact_runtime_and_state(
    tmp_path: Path,
) -> None:
    source = Path.cwd()
    candidate = tmp_path / "candidate"
    runtime = tmp_path / "dawnstrike-runtime"
    state = tmp_path / "state"
    backup = tmp_path / "backups"
    remote = tmp_path / "origin.git"
    candidate.mkdir()
    runtime.mkdir()
    state.mkdir()

    (candidate / "scripts").mkdir()
    for name in (
        "activate_dawnstrike_runtime.ps1",
        "rollback_dawnstrike_runtime.ps1",
        "runtime_activation_contract.py",
        "dawnstrike_job_process.ps1",
        "invoke_dawnstrike_stage.ps1",
        "state_disaster_recovery.py",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    shutil.copytree(
        source / "intraday_scanner",
        candidate / "intraday_scanner",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(source / ".gitignore", candidate / ".gitignore")
    subprocess.run(["git", "init", "--bare", str(remote)], check=True, capture_output=True)
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(candidate)],
        check=True,
        capture_output=True,
    )
    _git(candidate, "config", "user.email", "activation-test@example.invalid")
    _git(candidate, "config", "user.name", "Activation Test")
    _git(candidate, "add", ".")
    _git(candidate, "commit", "-m", "candidate")
    _git(candidate, "remote", "add", "origin", str(remote))
    _git(candidate, "push", "-u", "origin", "main")
    candidate_sha = _git(candidate, "rev-parse", "HEAD")
    candidate_tree = _git(candidate, "rev-parse", "HEAD^{tree}")

    subprocess.run(
        ["git", "init", "--initial-branch=main", str(runtime)],
        check=True,
        capture_output=True,
    )
    _git(runtime, "config", "user.email", "activation-test@example.invalid")
    _git(runtime, "config", "user.name", "Activation Test")
    (runtime / "previous.txt").write_text("previous-runtime\n", encoding="utf-8")
    _git(runtime, "add", "previous.txt")
    _git(runtime, "commit", "-m", "previous")
    _git(runtime, "remote", "add", "origin", str(remote))
    previous_sha = _git(runtime, "rev-parse", "HEAD")

    db = state / "shadow_real.sqlite"
    with sqlite3.connect(db) as connection:
        run_migrations(connection)
    db_hash_before = hashlib.sha256(db.read_bytes()).hexdigest()
    evidence_root = state / "evidence"
    evidence_root.mkdir()
    ci_payload = _ci_payload()
    ci_payload["candidate_sha"] = candidate_sha
    ci_payload["candidate_tree"] = candidate_tree
    sol_payload = _sol_payload()
    sol_payload["candidate_sha"] = candidate_sha
    sol_payload["candidate_tree"] = candidate_tree
    ci = evidence_root / "ci.json"
    sol = evidence_root / "sol.json"
    _write_json(ci, seal_evidence(ci_payload))
    _write_json(sol, seal_evidence(sol_payload))

    activation_script = str(
        (candidate / "scripts" / "activate_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    rollback_script = str(
        (candidate / "scripts" / "rollback_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    values = {
        "candidate": str(candidate).replace("'", "''"),
        "runtime": str(runtime).replace("'", "''"),
        "state": str(state).replace("'", "''"),
        "backup": str(backup).replace("'", "''"),
        "ci": str(ci).replace("'", "''"),
        "sol": str(sol).replace("'", "''"),
    }
    command = rf"""
. '{activation_script}'
$global:MockRuntime = '{values["runtime"]}'
$global:MockState = '{values["state"]}'
$global:MockTaskStates = @{{}}
$global:TaskEvents = @()
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{
    $global:MockTaskStates[$name] = 'Ready'
}}
function Get-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName)
    [pscustomobject]@{{
        State=$global:MockTaskStates[$TaskName]; TaskPath='\';
        Actions=@([pscustomobject]@{{
            Execute='powershell.exe';
            Arguments=(
                '-RuntimeRoot "' + $global:MockRuntime +
                '" -StateRoot "' + $global:MockState + '"'
            );
            WorkingDirectory=$global:MockRuntime
        }})
    }}
}}
function Export-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $enabled = if ($global:MockTaskStates[$TaskName] -eq 'Disabled') {{ 'false' }} else {{ 'true' }}
    "<Task><Name>$TaskName</Name><Runtime>$global:MockRuntime</Runtime><State>$global:MockState</State><Settings><Enabled>$enabled</Enabled></Settings></Task>"
}}
function Disable-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $global:MockTaskStates[$TaskName] = 'Disabled'
    $global:TaskEvents += ('disable:' + $TaskName)
    [pscustomobject]@{{ TaskName=$TaskName }}
}}
function Enable-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $global:MockTaskStates[$TaskName] = 'Ready'
    $global:TaskEvents += ('enable:' + $TaskName)
    [pscustomobject]@{{ TaskName=$TaskName }}
}}
$activated = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120
$bundlePath = Join-Path `
    '{values["state"]}' `
    ('runtime-rollbacks\' + $activated.activation_id + '\previous-runtime.bundle')
$heldBundlePath = $bundlePath + '.held'
[System.IO.File]::Move($bundlePath, $heldBundlePath)
$activationMissingBundleBlocked = $false
try {{
    $null = Invoke-DawnstrikeRuntimeActivation `
      -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
      -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
      -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
      -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
      -BackupRetention 5 -ProcessTimeoutSeconds 120
}}
catch {{ $activationMissingBundleBlocked = $true }}
finally {{ [System.IO.File]::Move($heldBundlePath, $bundlePath) }}
$activatedAgain = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120
$receiptName = 'runtime-activation-' + $activated.activation_id + '.json'
$receiptForRollback = Join-Path `
    '{values["state"]}' ('receipts\runtime-activation\' + $receiptName)
. '{rollback_script}'
$rolledBack = Invoke-DawnstrikeRuntimeRollback `
  -ActivationReceipt $receiptForRollback -ContractRoot '{values["candidate"]}' `
  -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
  -BackupRoot '{values["backup"]}' `
  -ProcessTimeoutSeconds 120
$stateBundlePath = Join-Path '{values["backup"]}' $activated.state_backup_id
$heldStateBundlePath = $stateBundlePath + '.held'
[System.IO.Directory]::Move($stateBundlePath, $heldStateBundlePath)
$rollbackMissingBackupBlocked = $false
try {{
    $null = Invoke-DawnstrikeRuntimeRollback `
      -ActivationReceipt $receiptForRollback `
      -ContractRoot '{values["candidate"]}' `
      -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
      -BackupRoot '{values["backup"]}' `
      -ProcessTimeoutSeconds 120
}}
catch {{ $rollbackMissingBackupBlocked = $true }}
finally {{ [System.IO.Directory]::Move($heldStateBundlePath, $stateBundlePath) }}
$rolledBackAgain = Invoke-DawnstrikeRuntimeRollback `
  -ActivationReceipt $receiptForRollback -ContractRoot '{values["candidate"]}' `
  -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
  -BackupRoot '{values["backup"]}' `
  -ProcessTimeoutSeconds 120
$output = [pscustomobject]@{{
    activated=$activated
    activated_again=$activatedAgain
    rolled_back=$rolledBack
    rolled_back_again=$rolledBackAgain
    activation_missing_bundle_blocked=$activationMissingBundleBlocked
    rollback_missing_backup_blocked=$rollbackMissingBackupBlocked
    task_states=$global:MockTaskStates
    task_events=$global:TaskEvents
}}
$output | ConvertTo-Json -Depth 12 -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=source,
        text=True,
        capture_output=True,
        timeout=180,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["activated"]["status"] == "COMPLETE"
    assert payload["activated"]["candidate_sha"] == candidate_sha
    assert payload["activated_again"]["receipt_sha256"] == payload["activated"]["receipt_sha256"]
    assert payload["activation_missing_bundle_blocked"] is True
    assert payload["rolled_back"]["status"] == "ROLLED_BACK"
    assert payload["rolled_back"]["restored_sha"] == previous_sha
    assert (
        payload["rolled_back_again"]["receipt_sha256"] == payload["rolled_back"]["receipt_sha256"]
    )
    assert payload["rollback_missing_backup_blocked"] is True
    assert set(payload["task_states"].values()) == {"Ready"}
    assert len(payload["task_events"]) == 20
    assert all(event.startswith("disable:") for event in payload["task_events"][:5])
    assert all(event.startswith("enable:") for event in payload["task_events"][5:10])
    assert all(event.startswith("disable:") for event in payload["task_events"][10:15])
    assert all(event.startswith("enable:") for event in payload["task_events"][15:20])
    assert _git(runtime, "rev-parse", "HEAD") == previous_sha
    assert not _git(runtime, "status", "--porcelain=v1", "--untracked-files=all")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_hash_before
    assert not list((state / "locks").glob("*.lock"))
    assert (
        backup / f"runtime-activation-{payload['activated']['activation_id']}" / "receipt.json"
    ).is_file()
    activation_scheduler_backup = (
        state / "scheduler-backups" / payload["activated"]["scheduler_backup_name"]
    )
    rollback_scheduler_backup = (
        state / "scheduler-backups" / payload["rolled_back"]["scheduler_backup_name"]
    )
    assert (activation_scheduler_backup / "manifest.json").is_file()
    assert len(list(activation_scheduler_backup.glob("*.xml"))) == 5
    assert (rollback_scheduler_backup / "manifest.json").is_file()
    assert len(list(rollback_scheduler_backup.glob("*.xml"))) == 5
