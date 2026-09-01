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
    validate_state_preparation_declaration,
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


def _install_local_origin_fixture_seam(lock_script: Path) -> None:
    """Let a disposable candidate use its local bare remote in integration tests."""

    text = lock_script.read_text(encoding="utf-8")
    start_marker = "function Convert-DawnstrikeCanonicalOriginIdentity([string]$Origin) {"
    end_marker = "\nfunction New-DawnstrikeRuntimeLockPayload"
    start = text.index(start_marker)
    end = text.index(end_marker, start)
    fixture = (
        f"{start_marker}\n"
        "    if ([string]::IsNullOrWhiteSpace($Origin)) { throw 'Fixture origin is empty.' }\n"
        "    return 'github.com/mattfren/dawnstrike'\n"
        "}\n"
    )
    lock_script.write_text(text[:start] + fixture + text[end:], encoding="utf-8")


def _self_seal_unsafe(payload: dict[str, object]) -> dict[str, object]:
    value = dict(payload)
    value["evidence_sha256"] = self_hash(value, "evidence_sha256")
    return value


def _state_preparation_declaration() -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.state_preparation_contract.v1",
        "sidecar_contract": "dawnstrike.account_capture_trial_sidecar.v1",
        "sidecar_version": 1,
        "legacy_schema_marker": 30,
        "required_before_activation": True,
        "capture_interpreter_path": (
            r"C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe"
        ),
        "capture_interpreter_version": "3.13.14",
        "capture_interpreter_sha256": (
            "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"
        ),
        "capture_interpreter_signer_subject": (
            "CN=Python Software Foundation, O=Python Software Foundation, "
            "L=Beaverton, S=Oregon, C=US"
        ),
        "capture_interpreter_signer_thumbprint": (
            "9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48"
        ),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_state_preparation_declaration_requires_exact_schema_and_values() -> None:
    declaration = _state_preparation_declaration()
    assert validate_state_preparation_declaration(declaration) == declaration

    with pytest.raises(ActivationContractError, match="fields do not match"):
        validate_state_preparation_declaration({**declaration, "unexpected": True})
    with pytest.raises(ActivationContractError, match="violates"):
        validate_state_preparation_declaration({**declaration, "sidecar_version": True})
    with pytest.raises(ActivationContractError, match="violates"):
        validate_state_preparation_declaration({**declaration, "research_only": False})
    with pytest.raises(ActivationContractError, match="violates"):
        validate_state_preparation_declaration(
            {**declaration, "capture_interpreter_sha256": "0" * 64}
        )


def test_state_preparation_declaration_loader_rejects_duplicate_json_keys(
    tmp_path: Path,
) -> None:
    declaration = tmp_path / "state_preparation_contract.json"
    declaration.write_text(
        '{"schema_version":"dawnstrike.state_preparation_contract.v1",'
        '"schema_version":"hostile",'
        '"sidecar_contract":"dawnstrike.account_capture_trial_sidecar.v1",'
        '"sidecar_version":1,"legacy_schema_marker":30,'
        '"required_before_activation":true,"research_only":true,'
        '"broker_execution_enabled":false}\n',
        encoding="utf-8",
    )
    from scripts.runtime_activation_contract import _load_object

    with pytest.raises(ActivationContractError, match="duplicate JSON field"):
        _load_object(declaration)


def test_powershell_declaration_boundary_has_no_second_unvalidated_read() -> None:
    script_path = (
        Path(__file__).resolve().parents[1]
        / "scripts"
        / "activate_dawnstrike_runtime.ps1"
    )
    script = script_path.read_text(encoding="utf-8")
    body = script.split(
        "function Get-DawnstrikeStatePreparationDeclaration",
        1,
    )[1].split("function Get-DawnstrikeAuxiliaryCaptureTask", 1)[0]
    assert body.count("Invoke-DawnstrikeContractCli") == 1
    assert "Get-Content" not in body
    assert "$declaration = $validated" in body


def test_powershell_declaration_is_bound_to_exact_commit_and_rechecked() -> None:
    script = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    declaration = script.split(
        "function Get-DawnstrikeStatePreparationDeclaration", 1
    )[1].split("function Get-DawnstrikeAuxiliaryCaptureTask", 1)[0]
    assert '"ls-tree"' in declaration
    assert '"rev-parse"' in declaration
    assert '"hash-object"' in declaration
    assert "exact candidate commit" in declaration
    assert "post-validation binding" in declaration
    assert "Assert-DawnstrikeCandidateIdentityAndDeclaration" in script
    assert script.count("Assert-DawnstrikeCandidateIdentityAndDeclaration") >= 4
    for companion in (
        "scripts/prepare_dawnstrike_state.ps1",
        "scripts/rollback_dawnstrike_runtime.ps1",
    ):
        assert "Assert-DawnstrikeCandidateIdentityAndDeclaration" in Path(
            companion
        ).read_text(encoding="utf-8")


def test_powershell_receipt_paths_use_valid_backslash_regex() -> None:
    scripts = tuple(Path("scripts").glob("*.ps1"))
    source_by_script = {
        script: script.read_text(encoding="utf-8") for script in scripts
    }
    assert source_by_script[Path("scripts/activate_dawnstrike_runtime.ps1")].count(
        r"-replace '\\','/'"
    ) >= 2
    assert source_by_script[Path("scripts/harden_intraday_capture_task.ps1")].count(
        r"-replace '\\','/'"
    ) >= 1
    assert source_by_script[Path("scripts/rebind_intraday_capture_task.ps1")].count(
        r"-replace '\\','/'"
    ) >= 1
    assert all(r"-replace '\','/'" not in source for source in source_by_script.values())


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_powershell_backslash_regex_executes() -> None:
    result = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            r"if (('receipts\capture-task\x.json' -replace '\\','/') -ne "
            r"'receipts/capture-task/x.json') { exit 1 }",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr or result.stdout


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_powershell_activation_script_has_valid_ast() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
$tokens = $null
$errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(
    '{script}', [ref]$tokens, [ref]$errors
)
if ($errors.Count -gt 0) {{ throw (($errors | ForEach-Object {{ $_.Message }}) -join '; ') }}
$functions = @($ast.FindAll(
    {{ param($node) $node -is [System.Management.Automation.Language.FunctionDefinitionAst] }},
    $true
) | ForEach-Object Name)
[pscustomobject]@{{
    valid = ($null -ne $ast)
    declaration = ($functions -contains 'Get-DawnstrikeStatePreparationDeclaration')
    identity = ($functions -contains 'Assert-DawnstrikeCandidateIdentityAndDeclaration')
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(), text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "valid": True, "declaration": True, "identity": True
    }


def _declaration_checkout(
    tmp_path: Path, *, tracked: bool, declaration_text: str | None = None
) -> tuple[Path, str, str]:
    checkout = tmp_path / ("tracked" if tracked else "legacy")
    (checkout / "config").mkdir(parents=True)
    (checkout / "scripts").mkdir()
    shutil.copy2(
        Path("scripts/runtime_activation_contract.py"),
        checkout / "scripts/runtime_activation_contract.py",
    )
    (checkout / ".gitignore").write_text("__pycache__/\n", encoding="utf-8")
    if tracked:
        destination = checkout / "config/state_preparation_contract.json"
        if declaration_text is None:
            shutil.copy2(Path("config/state_preparation_contract.json"), destination)
        else:
            destination.write_text(declaration_text, encoding="utf-8")
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(checkout)],
        check=True, capture_output=True,
    )
    _git(checkout, "config", "user.email", "activation-test@example.invalid")
    _git(checkout, "config", "user.name", "Activation Test")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "declaration fixture")
    return checkout, _git(checkout, "rev-parse", "HEAD"), _git(
        checkout, "rev-parse", "HEAD^{tree}"
    )


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize("mutation", ["delete", "hostile_restore"])
def test_powershell_declaration_cannot_be_deleted_or_restored_after_clean_check(
    tmp_path: Path, mutation: str
) -> None:
    checkout, candidate_sha, candidate_tree = _declaration_checkout(tmp_path, tracked=True)
    declaration = checkout / "config/state_preparation_contract.json"
    if mutation == "delete":
        declaration.unlink()
    else:
        declaration.write_text(
            '{"schema_version":"dawnstrike.state_preparation_contract.v1",'
            '"schema_version":"hostile","sidecar_contract":"dawnstrike.account_capture_trial_sidecar.v1",'
            '"sidecar_version":1,"legacy_schema_marker":30,"required_before_activation":true,'
            '"research_only":true,"broker_execution_enabled":false}\n',
            encoding="utf-8",
        )
    source = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    root = str(checkout).replace("'", "''")
    command = rf"""
. '{source}'
. '{runner}'
$git = (Get-Command git.exe -CommandType Application)[0].Source
$py = (Get-Command py.exe -CommandType Application)[0].Source
$blocked = $false
try {{
    $null = Get-DawnstrikeStatePreparationDeclaration -CandidateRoot '{root}' `
        -GitPath $git -CandidateSha '{candidate_sha}' -CandidateTree '{candidate_tree}' `
        -PythonPath $py -TimeoutSeconds 30
}}
catch {{ $blocked = $true }}
$blocked | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(), text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) is True


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_powershell_strict_loader_rejects_duplicate_tracked_declaration(tmp_path: Path) -> None:
    duplicate = (
        '{"schema_version":"dawnstrike.state_preparation_contract.v1",'
        '"schema_version":"hostile","sidecar_contract":"dawnstrike.account_capture_trial_sidecar.v1",'
        '"sidecar_version":1,"legacy_schema_marker":30,"required_before_activation":true,'
        '"research_only":true,"broker_execution_enabled":false}\n'
    )
    checkout, candidate_sha, candidate_tree = _declaration_checkout(
        tmp_path, tracked=True, declaration_text=duplicate
    )
    source = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    root = str(checkout).replace("'", "''")
    command = rf"""
. '{source}'
. '{runner}'
$git = (Get-Command git.exe -CommandType Application)[0].Source
$py = (Get-Command py.exe -CommandType Application)[0].Source
$blocked = $false
try {{
    $null = Get-DawnstrikeStatePreparationDeclaration -CandidateRoot '{root}' `
        -GitPath $git -CandidateSha '{candidate_sha}' -CandidateTree '{candidate_tree}' `
        -PythonPath $py -TimeoutSeconds 30
}}
catch {{ $blocked = $_.Exception.Message -match 'validation' }}
$blocked | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(), text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) is True


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_powershell_declaration_restore_race_is_rejected_after_strict_read(
    tmp_path: Path,
) -> None:
    checkout, candidate_sha, candidate_tree = _declaration_checkout(tmp_path, tracked=True)
    declaration = checkout / "config/state_preparation_contract.json"
    hostile = (
        '{"schema_version":"dawnstrike.state_preparation_contract.v1",'
        '"schema_version":"hostile","sidecar_contract":"dawnstrike.account_capture_trial_sidecar.v1",'
        '"sidecar_version":1,"legacy_schema_marker":30,"required_before_activation":true,'
        '"research_only":true,"broker_execution_enabled":false}\n'
    ).replace("'", "''")
    source = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    root = str(checkout).replace("'", "''")
    declaration_path = str(declaration).replace("'", "''")
    command = rf"""
. '{source}'
. '{runner}'
    $originalGitValue = (Get-Command Get-DawnstrikeGitValue).ScriptBlock
    $racePath = '{declaration_path}'
    $racePayload = '{hostile}'
    $global:RaceMutated = $false
function Get-DawnstrikeGitValue {{
    param([string]$GitPath,[string]$Root,[string[]]$Arguments,[string]$Label,[int]$TimeoutSeconds)
    $value = & $originalGitValue @PSBoundParameters
    if ($Label -eq 'State-preparation declaration working-tree binding') {{
        [System.IO.File]::Delete($racePath)
        [System.IO.File]::WriteAllText(
            $racePath, $racePayload, [System.Text.UTF8Encoding]::new($false)
        )
        $global:RaceMutated = $true
    }}
    return $value
}}
$git = (Get-Command git.exe -CommandType Application)[0].Source
$py = (Get-Command py.exe -CommandType Application)[0].Source
$blocked = $false
try {{
    $null = Get-DawnstrikeStatePreparationDeclaration -CandidateRoot '{root}' `
        -GitPath $git -CandidateSha '{candidate_sha}' -CandidateTree '{candidate_tree}' `
        -PythonPath $py -TimeoutSeconds 30
}}
catch {{
    $blocked = $_.Exception.Message -match (
        'failed with exit code|changed during strict validation|bytes do not match'
    )
}}
    [pscustomobject]@{{blocked=$blocked; mutated=$global:RaceMutated}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(), text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "blocked": True, "mutated": True
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_powershell_legacy_compatibility_requires_exact_commit_absence(tmp_path: Path) -> None:
    checkout, candidate_sha, candidate_tree = _declaration_checkout(tmp_path, tracked=False)
    source = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    root = str(checkout).replace("'", "''")
    command = rf"""
. '{source}'
. '{runner}'
$git = (Get-Command git.exe -CommandType Application)[0].Source
$result = Get-DawnstrikeStatePreparationDeclaration -CandidateRoot '{root}' `
    -GitPath $git -CandidateSha '{candidate_sha}' -CandidateTree '{candidate_tree}'
[pscustomobject]@{{required=$result.required; present=$result.declaration_present}} |
    ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(), text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "required": False, "present": False
    }


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


def test_extended_rollback_requires_untampered_capture_hardening_chain(
    tmp_path: Path,
) -> None:
    payload = _receipt_payload(schema=ROLLBACK_SCHEMA, status="ROLLED_BACK")
    activation_id = str(payload["activation_id"])
    state_preparation_id = "state-preparation-" + "a" * 16 + "-" + "b" * 16
    payload.update(
        {
            "state_preparation_required": True,
            "state_preparation_contract": "dawnstrike.account_capture_trial_sidecar.v1",
            "state_preparation_receipt_sha256": "a" * 64,
            "state_preparation_after_db_sha256": "b" * 64,
            "state_preparation_after_wal_sha256": "c" * 64,
            "state_preparation_after_shm_sha256": "d" * 64,
            "state_preparation_after_logical_snapshot_sha256": "e" * 64,
            "state_preparation_inventory_sha256": "f" * 64,
            "state_preparation_backup_id": state_preparation_id,
            "state_preparation_backup_bundle_path": str(
                (tmp_path / state_preparation_id).resolve()
            ),
            "state_preparation_backup_db_sha256": "1" * 64,
            "state_preparation_backup_manifest_sha256": "2" * 64,
            "state_preparation_backup_manifest_file_sha256": "3" * 64,
            "state_backup_bundle_path": str(
                (tmp_path / f"runtime-activation-{activation_id}").resolve()
            ),
            "state_backup_logical_snapshot_sha256": "4" * 64,
            "state_backup_source_logical_snapshot_sha256": "5" * 64,
            "state_backup_manifest_sha256": "6" * 64,
            "auxiliary_capture_present": True,
            "auxiliary_capture_state_before": "Disabled",
            "auxiliary_capture_state_after": "Disabled",
            "auxiliary_capture_action": "RESTORED_EXACT",
            "auxiliary_capture_xml_sha256": "7" * 64,
            "auxiliary_capture_xml_file_sha256": "8" * 64,
            "auxiliary_capture_definition_contract_sha256": "9" * 64,
            "auxiliary_capture_action_contract_sha256": "a" * 64,
            "auxiliary_capture_backup_name": f"runtime-activation-{activation_id}",
            "auxiliary_capture_backup_manifest_sha256": "b" * 64,
            "capture_hardening_receipt_relative_path": (
                "receipts/capture-task/capture-task-hardening-" + CANDIDATE_SHA + ".json"
            ),
            "capture_hardening_receipt_raw_sha256": "c" * 64,
            "capture_hardening_receipt_sha256": "d" * 64,
            "capture_hardening_xml_sha256": "e" * 64,
            "capture_hardening_action_sha256": "f" * 64,
            "capture_hardening_principal_sha256": "1" * 64,
            "capture_hardening_trigger_sha256": "2" * 64,
            "capture_hardening_settings_sha256": "3" * 64,
            "capture_hardening_runner_before_sha256": "4" * 64,
            "capture_hardening_runner_target_sha256": "5" * 64,
        }
    )
    sealed = seal_receipt(payload, tmp_path / "extended-rollback.json")
    assert sealed["status"] == "ROLLED_BACK"

    omitted = dict(payload)
    omitted.pop("capture_hardening_receipt_sha256")
    with pytest.raises(ActivationContractError, match="fields do not match"):
        seal_receipt(omitted, tmp_path / "omitted.json")

    tampered = dict(payload)
    tampered["capture_hardening_runner_target_sha256"] = "not-a-hash"
    with pytest.raises(ActivationContractError, match="runner_target_sha256 is invalid"):
        seal_receipt(tampered, tmp_path / "tampered.json")


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
    payload["operator_secret"] = "forbidden"  # pragma: allowlist secret
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


def test_activation_seals_init_before_first_stage_filesystem_mutation() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    boundary = activation.index(
        "# INIT and its exact runtime lock must exist before clone"
    )
    lock = activation.index(
        "$activationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal",
        boundary,
    )
    stage_directory_crash = activation.index(
        'if ($TestStageCrashPoint -eq "after_stage_directory")', lock
    )
    clone = activation.index('-Label "Candidate runtime staging"', lock)
    checkout_crash = activation.index(
        'if ($TestStageCrashPoint -eq "after_stage_checkout")', clone
    )
    daily = activation.index(
        "$dailyLock = Enter-DawnstrikeDailyRunLock", checkout_crash
    )
    assert lock < stage_directory_crash < clone < checkout_crash < daily
    assert "DAWNSTRIKE_TEST_ACTIVATION_STAGE_CRASH" in activation
    assert "INIT recovery could not quarantine the exact staged path" in activation
    assert "Staging failure journal identity is invalid" in activation


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
    if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ return @() }}
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
    $enabled = if ($global:MockTaskState -eq 'Disabled') {{
        '<Enabled>false</Enabled>'
    }} else {{
        ''
    }}
    "<Task><Name>$TaskName</Name><Description>$global:MockDefinition</Description><Settings>$enabled</Settings></Task>"
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
        "runtime_activation_lock.ps1",
            "runtime_activation_lock_contract.py",
            "runtime_operation_journal.py",
        "runtime_operation_journal.py",
        "capture_task_safety.ps1",
        "rollback_dawnstrike_runtime.ps1",
        "runtime_activation_contract.py",
        "dawnstrike_job_process.ps1",
        "invoke_dawnstrike_stage.ps1",
        "state_disaster_recovery.py",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    _install_local_origin_fixture_seam(candidate / "scripts" / "runtime_activation_lock.ps1")
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
    if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ return @() }}
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
