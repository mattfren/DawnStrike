from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, run_migrations
from scripts import runtime_activation_contract as activation_contract
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
PRODUCTION_GIT_PATH = r"C:\Program Files\Git\cmd\git.exe"
PRODUCTION_GIT_SHA256 = (
    "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"
)


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
        "report_sha256": "e" * 64,
        "codex_share_url": "https://chatgpt.com/share/test-owner-report",
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


def _copy_bootstrap_for_host(destination: Path) -> None:
    discovered = shutil.which("git")
    assert discovered is not None
    git_path = Path(discovered).resolve()
    git_sha256 = hashlib.sha256(git_path.read_bytes()).hexdigest()
    source = Path("scripts/dawnstrike_python_bootstrap.py").read_text(encoding="utf-8")
    source = source.replace(
        f'_APPROVED_GIT = Path(r"{PRODUCTION_GIT_PATH}")',
        f"_APPROVED_GIT = Path({str(git_path)!r})",
        1,
    ).replace(PRODUCTION_GIT_SHA256, git_sha256, 1)
    assert repr(str(git_path)) in source
    assert git_sha256 in source
    destination.write_text(source, encoding="utf-8")


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


def _install_local_github_ci_fixture_seam(contract_script: Path) -> None:
    """Use deterministic GitHub authority responses in a copied candidate only."""

    text = contract_script.read_text(encoding="utf-8")
    start = text.index("def _github_api_object(")
    end = text.index("\ndef validate_live_github_ci", start)
    original = text[start:end].replace(
        "def _github_api_object(", "def _github_api_object_live(", 1
    )
    fixture = r'''

def _github_api_object(path: str) -> tuple[Any, str]:
    if os.environ.get("DAWNSTRIKE_TEST_GITHUB_CI_FIXTURE") != "1":
        return _github_api_object_live(path)
    candidate_sha = os.environ["DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_SHA"]
    candidate_tree = os.environ["DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_TREE"]
    completed_at = os.environ["DAWNSTRIKE_TEST_GITHUB_CI_COMPLETED_AT"]
    owner_body = os.environ.get("DAWNSTRIKE_TEST_GITHUB_OWNER_COMMENT_BODY", "")
    if path == "/repos/mattfren/DawnStrike/actions/runs/12345":
        return ({
            "id": 12345,
            "workflow_id": _GITHUB_WORKFLOW_ID,
            "path": ".github/workflows/ci.yml",
            "event": "push",
            "run_attempt": 1,
            "head_branch": "main",
            "head_sha": candidate_sha,
            "status": "completed",
            "conclusion": "success",
            "updated_at": completed_at,
            "repository": {"id": _GITHUB_REPOSITORY_ID},
            "head_repository": {"id": _GITHUB_REPOSITORY_ID},
            "actor": {"id": _GITHUB_RELEASE_ACTOR_ID},
            "triggering_actor": {"id": _GITHUB_RELEASE_ACTOR_ID},
        }, "1" * 64)
    if path == "/repos/mattfren/DawnStrike/actions/runs/12345/jobs?per_page=100":
        return ({
            "total_count": 19,
            "jobs": [
                {"name": name, "status": "completed", "conclusion": "success", "run_attempt": 1}
                for name in sorted(_CI_JOB_NAMES)
            ],
        }, "2" * 64)
    if path == f"/repos/mattfren/DawnStrike/git/commits/{candidate_sha}":
        return ({"sha": candidate_sha, "tree": {"sha": candidate_tree}}, "3" * 64)
    if path == f"/repos/mattfren/DawnStrike/commits/{candidate_sha}/comments?per_page=100":
        comment_id = 987654321
        return ([{
            "id": comment_id,
            "url": f"https://api.github.com/repos/mattfren/DawnStrike/comments/{comment_id}",
            "html_url": f"https://github.com/mattfren/DawnStrike/commit/{candidate_sha}#commitcomment-{comment_id}",
            "commit_id": candidate_sha,
            "body": owner_body,
            "user": {"id": _GITHUB_RELEASE_ACTOR_ID},
            "author_association": "OWNER",
            "created_at": completed_at,
            "updated_at": completed_at,
        }], "4" * 64)
    return _github_api_object_live(path)
'''
    contract_script.write_text(text[:start] + original + fixture + text[end:], encoding="utf-8")


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


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_canonical_task_semantics_rejects_hostile_actions_and_missing_triggers() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
. '{script}'
$global:MockMode = 'bad_action'
function Get-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName)
    $policy = Get-DawnstrikeCanonicalTaskPolicy $TaskName 'C:\runtime' 'C:\state'
    $badAction = $global:MockMode -eq 'bad_action' -and
        $TaskName -in @('Dawnstrike AlphaOps Morning', 'Dawnstrike AlphaOps Monitor 5m')
    $actions = @([pscustomobject]@{{
        Execute = if ($badAction) {{ 'cmd.exe' }} else {{ $script:DawnstrikePowerShellExecutable }}
        Arguments = $policy.arguments
        WorkingDirectory = 'C:\runtime'
    }})
    $triggers = if ($global:MockMode -eq 'bad_trigger') {{ @() }} else {{
        $type = if ($policy.weekly) {{
            'MSFT_TaskWeeklyTrigger'
        }} else {{ 'MSFT_TaskDailyTrigger' }}
        @([pscustomobject]@{{
            CimClass = [pscustomobject]@{{ CimClassName = $type }}
            Enabled = $true
            DaysOfWeek = if ($policy.weekly) {{ [int]$policy.days }} else {{ $null }}
            WeeksInterval = if ($policy.weekly) {{ 1 }} else {{ $null }}
            DaysInterval = if ($policy.weekly) {{ $null }} else {{ 1 }}
            StartBoundary = '2026-09-01T08:00:00-05:00'
            EndBoundary = $null
            RandomDelay = $null
            Repetition = if ($policy.monitor) {{
                [pscustomobject]@{{ Interval='PT5M'; Duration='PT6H35M'; StopAtDurationEnd=$true }}
            }} else {{ [pscustomobject]@{{ Interval=''; Duration=''; StopAtDurationEnd=$false }} }}
        }})
    }}
    [pscustomobject]@{{
        State = 'Ready'; TaskPath = '\'; Actions = $actions; Triggers = $triggers
        Principal = [pscustomobject]@{{
            LogonType='Password'; UserId='activation-test'; RunLevel='Limited'
        }}
        Settings = [pscustomobject]@{{
            Enabled=$true; StartWhenAvailable=$true; WakeToRun=$true
            StopIfGoingOnBatteries=$false; DisallowStartIfOnBatteries=$false
            MultipleInstances='IgnoreNew'; ExecutionTimeLimit=$policy.execution_limit
            RestartCount=$policy.restart_count; RestartInterval=$policy.restart_interval
            Hidden=$false; RunOnlyIfIdle=$false; RunOnlyIfNetworkAvailable=$false
            UseUnifiedSchedulingEngine=$true
        }}
    }}
}}
$actionBlocked = $false
try {{
    $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state'
}}
catch {{ $actionBlocked = $_.Exception.Message -match 'executable|action' }}
$global:MockMode = 'bad_trigger'
$triggerBlocked = $false
try {{
    $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state'
}}
catch {{ $triggerBlocked = $_.Exception.Message -match 'trigger' }}
[pscustomobject]@{{ action_blocked=$actionBlocked; trigger_blocked=$triggerBlocked }} |
    ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(), text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "action_blocked": True,
        "trigger_blocked": True,
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_git_contract_rejects_combined_hidden_index_flags(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(checkout)],
        check=True, capture_output=True,
    )
    _git(checkout, "config", "user.email", "activation-test@example.invalid")
    _git(checkout, "config", "user.name", "Activation Test")
    tracked = checkout / "tracked.txt"
    tracked.write_text("tracked\n", encoding="utf-8")
    _git(checkout, "add", "tracked.txt")
    _git(checkout, "commit", "-m", "tracked")
    _git(checkout, "update-index", "--assume-unchanged", "tracked.txt")
    _git(checkout, "update-index", "--skip-worktree", "tracked.txt")

    activation = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    lock = str(Path("scripts/runtime_activation_lock.ps1").resolve()).replace("'", "''")
    root = str(checkout).replace("'", "''")
    command = rf"""
. '{activation}'
. '{runner}'
. '{lock}'
$gitPath = (Get-DawnstrikeApprovedGit).path
$blocked = $false
try {{ $null = Get-DawnstrikeGitContract -GitPath $gitPath -Root '{root}' -TimeoutSeconds 30 }}
catch {{ $blocked = $_.Exception.Message -match 'assume-unchanged|skip-worktree' }}
$blocked | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(), text=True, capture_output=True, timeout=60, check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) is True


def test_isolated_bootstrap_imports_intraday_from_exact_release_root(
    tmp_path: Path,
) -> None:
    root = tmp_path / "release"
    (root / "scripts").mkdir(parents=True)
    (root / "intraday_scanner").mkdir()
    bootstrap = root / "scripts" / "dawnstrike_python_bootstrap.py"
    _copy_bootstrap_for_host(bootstrap)
    (root / "intraday_scanner" / "__init__.py").write_text("\n", encoding="utf-8")
    (root / "intraday_scanner" / "probe.py").write_text(
        "print('EXACT_RELEASE_IMPORT')\n", encoding="utf-8"
    )
    (root / "pyproject.toml").write_text(
        "[project]\nname='activation-bootstrap-fixture'\n", encoding="utf-8"
    )
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(root)],
        check=True,
        capture_output=True,
    )
    _git(root, "config", "user.email", "activation-test@example.invalid")
    _git(root, "config", "user.name", "Activation Test")
    _git(root, "config", "core.autocrlf", "false")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "bootstrap fixture")
    expected_sha = _git(root, "rev-parse", "HEAD")
    assert _git(root, "status", "--porcelain=v1", "--untracked-files=all") == ""
    result = subprocess.run(
        [
            sys.executable,
            "-I", "-B", "-S",
            str(bootstrap),
            "--release-root", str(root),
            "--expected-sha", expected_sha,
            "--module", "intraday_scanner.probe",
            "--",
        ],
        cwd=root,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert result.stdout.strip() == "EXACT_RELEASE_IMPORT"
    assert "_assert_package_from(root)" in bootstrap.read_text(encoding="utf-8")


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


def test_activation_requires_live_pinned_github_ci_authority(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    completed = _now()
    ci_payload = _ci_payload(completed=completed)
    ci_payload["run_url"] = "https://github.com/mattfren/DawnStrike/actions/runs/12345"
    ci = tmp_path / "ci.json"
    sol = tmp_path / "sol.json"
    _write_json(ci, seal_evidence(ci_payload))
    _write_json(sol, seal_evidence(_sol_payload(completed=completed)))
    jobs = [
        {
            "name": name,
            "status": "completed",
            "conclusion": "success",
            "run_attempt": 1,
        }
        for name in sorted(activation_contract._CI_JOB_NAMES)
    ]

    def github(path: str):
        if path.endswith("/jobs?per_page=100"):
            return {"total_count": 19, "jobs": jobs}, "2" * 64
        if "/git/commits/" in path:
            return {"sha": CANDIDATE_SHA, "tree": {"sha": CANDIDATE_TREE}}, "3" * 64
        return (
            {
                "id": 12345,
                "workflow_id": activation_contract._GITHUB_WORKFLOW_ID,
                "path": ".github/workflows/ci.yml",
                "event": "push",
                "run_attempt": 1,
                "head_branch": "main",
                "head_sha": CANDIDATE_SHA,
                "status": "completed",
                "conclusion": "success",
                "updated_at": ci_payload["completed_at_utc"],
                "repository": {"id": activation_contract._GITHUB_REPOSITORY_ID},
                "head_repository": {"id": activation_contract._GITHUB_REPOSITORY_ID},
                "actor": {"id": activation_contract._GITHUB_RELEASE_ACTOR_ID},
                "triggering_actor": {
                    "id": activation_contract._GITHUB_RELEASE_ACTOR_ID
                },
            },
            "1" * 64,
        )

    monkeypatch.setattr(activation_contract, "_github_api_object", github)
    result = validate_evidence_pair(
        ci,
        sol,
        candidate_sha=CANDIDATE_SHA,
        candidate_tree=CANDIDATE_TREE,
        now=completed,
        require_live_github_ci=True,
    )
    assert result["ci_github_authority_sha256"]
    assert result["ci_evidence_sha256"] != result["ci_local_evidence_sha256"]

    jobs[0]["name"] = "Unexpected job"
    with pytest.raises(ActivationContractError, match="job names"):
        validate_evidence_pair(
            ci,
            sol,
            candidate_sha=CANDIDATE_SHA,
            candidate_tree=CANDIDATE_TREE,
            now=completed,
            require_live_github_ci=True,
        )


def test_live_owner_commit_comment_authorizes_exact_sol_report(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sol = _sol_payload()
    expected_body = activation_contract._owner_authorization_body(
        sol, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
    )
    comment_id = 123456789

    def github(path: str):
        if path.endswith("/comments?per_page=100"):
            return ([{
                "id": comment_id,
                "url": f"https://api.github.com/repos/mattfren/DawnStrike/comments/{comment_id}",
                "html_url": f"https://github.com/mattfren/DawnStrike/commit/{CANDIDATE_SHA}#commitcomment-{comment_id}",
                "commit_id": CANDIDATE_SHA,
                "body": expected_body,
                "user": {"id": activation_contract._GITHUB_RELEASE_ACTOR_ID},
                "author_association": "OWNER",
                "created_at": "2026-08-31T15:00:00Z",
                "updated_at": "2026-08-31T15:00:00Z",
            }], "4" * 64)
        return (
            {"sha": CANDIDATE_SHA, "tree": {"sha": CANDIDATE_TREE}},
            "3" * 64,
        )

    monkeypatch.setattr(activation_contract, "_github_api_object", github)
    result = activation_contract.validate_live_github_owner_authorization(
        sol, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
    )
    assert result["comment_id"] == comment_id
    assert result["github_owner_authorization_sha256"]

    for mutation, message in (
        (lambda c: c["user"].update(id=1), "actor"),
        (lambda c: c.update(commit_id="f" * 40), "comment commit"),
        (lambda c: c.update(body="{}"), "unique"),
        (lambda c: c.update(created_at="2026-08-31T15:00:01Z"), "timestamps"),
        (
            lambda c: c.update(
                html_url=(
                    "https://github.com/evil/DawnStrike/commit/"
                    + CANDIDATE_SHA
                    + "#commitcomment-123456789"
                )
            ),
            "URL",
        ),
    ):
        comment = {
            "id": comment_id,
            "url": f"https://api.github.com/repos/mattfren/DawnStrike/comments/{comment_id}",
            "html_url": f"https://github.com/mattfren/DawnStrike/commit/{CANDIDATE_SHA}#commitcomment-{comment_id}",
            "commit_id": CANDIDATE_SHA,
            "body": expected_body,
            "user": {"id": activation_contract._GITHUB_RELEASE_ACTOR_ID},
            "author_association": "OWNER",
            "created_at": "2026-08-31T15:00:00Z",
            "updated_at": "2026-08-31T15:00:00Z",
        }
        mutation(comment)

        def hostile(path: str, *, value: dict[str, object] = comment):
            if path.endswith("/comments?per_page=100"):
                return [value], "4" * 64
            return {"sha": CANDIDATE_SHA, "tree": {"sha": CANDIDATE_TREE}}, "3" * 64

        monkeypatch.setattr(activation_contract, "_github_api_object", hostile)
        with pytest.raises(ActivationContractError, match=message):
            activation_contract.validate_live_github_owner_authorization(
                sol, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
            )


def test_live_owner_authorization_rejects_fabricated_sol_and_redirect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sol = _sol_payload()
    expected_body = activation_contract._owner_authorization_body(
        sol, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
    )
    comment = {
        "id": 123456789,
        "url": "https://api.github.com/repos/mattfren/DawnStrike/comments/123456789",
        "html_url": f"https://github.com/mattfren/DawnStrike/commit/{CANDIDATE_SHA}#commitcomment-123456789",
        "commit_id": CANDIDATE_SHA,
        "body": expected_body,
        "user": {"id": activation_contract._GITHUB_RELEASE_ACTOR_ID},
        "author_association": "OWNER",
        "created_at": "2026-08-31T15:00:00Z",
        "updated_at": "2026-08-31T15:00:00Z",
    }

    def github(path: str):
        if path.endswith("/comments?per_page=100"):
            return [comment], "4" * 64
        return {"sha": CANDIDATE_SHA, "tree": {"sha": CANDIDATE_TREE}}, "3" * 64

    monkeypatch.setattr(activation_contract, "_github_api_object", github)
    fabricated = dict(sol, report_sha256="f" * 64)
    with pytest.raises(ActivationContractError, match="unique"):
        activation_contract.validate_live_github_owner_authorization(
            fabricated, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
        )

    monkeypatch.setattr(
        activation_contract,
        "_github_api_object",
        lambda path: (_ for _ in ()).throw(
            ActivationContractError("GitHub authority endpoint redirected unexpectedly")
        ),
    )
    with pytest.raises(ActivationContractError, match="redirected"):
        activation_contract.validate_live_github_owner_authorization(
            sol, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
        )


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


def test_rollback_ready_receipt_is_strict_and_not_terminal(tmp_path: Path) -> None:
    ready_payload = _receipt_payload(
        schema="dawnstrike.runtime_rollback_receipt.v2", status="PREPARED"
    )
    ready = seal_receipt(ready_payload, tmp_path / "rollback-ready.json")
    assert ready["status"] == "PREPARED"
    assert ready["task_enablement_restored"] is False
    assert load_receipt(tmp_path / "rollback-ready.json") == ready


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
            "previous_runtime_rollback_authorized": False,
            "previous_runtime_disposition": "QUARANTINED_UNAUTHORIZED",
            "previous_runtime_authorization_receipt_sha256": hashlib.sha256(b"").hexdigest(),
            "previous_runtime_authorization_journal_sha256": hashlib.sha256(b"").hexdigest(),
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


def test_rollback_compensation_failure_preserves_adoptable_locks() -> None:
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )

    seam = rollback.index(
        'DAWNSTRIKE_TEST_ROLLBACK_THROW_POINT -eq "during_compensation"'
    )
    seam_guard = rollback.index(
        'DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1"', seam
    )
    compensation_catch = rollback.index(
        "# Compensation failed, but the best-effort Disabled boundary was",
        seam,
    )
    fail_closed = rollback.rindex(
        "Set-DawnstrikeTasksFailClosedDisabled $runtime $state", seam, compensation_catch
    )
    phase_guard = rollback.index(
        'journalPhase -in @("PRE_SWAP", "POST_SWAP", "POST_SWAP_READY")', compensation_catch
    )
    preserve = rollback.index("$preserveLocks = $true", phase_guard)
    lock_pair_guard = rollback.index(
        "Nonterminal rollback compensation lacks its adoptable lock pair.", preserve
    )
    rethrow = rollback.index(
        "Runtime rollback failed and automatic candidate restore could not be completed;",
        lock_pair_guard,
    )
    assert (
        seam
        < seam_guard
        < fail_closed
        < compensation_catch
        < phase_guard
        < preserve
        < lock_pair_guard
        < rethrow
    )
    compensation_block = rollback[phase_guard:rethrow]
    assert "$null -eq $activationLock -or $null -eq $dailyLock" in compensation_block


def test_runtime_compensation_attempts_use_immutable_unique_evidence_paths() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )

    assert (
        '$compensationAttemptKey = [string]$journalBefore.raw_file_sha256'
        in activation
    )
    assert (
        'runtime-activation-$activationId.compensated-$compensationAttemptKey.json'
        in activation
    )
    assert (
        'runtime-activation-$activationId.failed-$compensationAttemptKey.json'
        in activation
    )
    assert (
        'recovery-quarantine\\compensated-$activationId-$compensationAttemptKey'
        in activation
    )
    assert (
        'compensated-$activationId-$compensationAttemptKey-$preparedHash.prepared.json'
        in activation
    )
    assert 'runtime-activation-$activationId.compensated.json' not in activation

    assert (
        '$compensationAttemptKey = [string]$journalBefore.raw_file_sha256'
        in rollback
    )
    assert (
        'runtime-rollback-$activationId.compensated-$compensationAttemptKey.json'
        in rollback
    )
    assert (
        'runtime-rollback-$activationId.failed-$compensationAttemptKey.json'
        in rollback
    )
    assert 'runtime-rollback-$activationId.compensated.json' not in rollback
    assert (
        '$failedAttemptKey = [string]$failedAttemptJournal.raw_file_sha256'
        in rollback
    )
    assert '"failed-previous-runtime-$failedAttemptKey"' in rollback
    assert 'Join-Path $rollbackRoot "failed-previous-runtime"' not in rollback


def test_rebind_post_enable_compensation_uses_defined_receipt_path() -> None:
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(
        encoding="utf-8"
    )
    recovery_start = rebind.index(
        'elseif ([string]$preExistingJournal.payload.phase -eq "POST_ENABLE"'
    )
    recovery_end = rebind.index(
        'Clear-DawnstrikeCompensatedJournalTombstone', recovery_start
    )
    recovery = rebind[recovery_start:recovery_end]
    assert (
        'capture-task-rebind-$([string]$preExistingJournal.raw_file_sha256).compensated.json'
        in recovery
    )
    assert (
        "-CompensationReceiptRelativePath $compensationReceiptRelativePath"
        in recovery
    )
    assert "$compensationReceiptRelative " not in recovery
    assert (
        'capture-task-rebind-$([string]$journalBefore.raw_file_sha256).failed.json'
        in rebind
    )
    assert 'capture-task-rebind-" + $CandidateSha + ".failed.json' not in rebind


def test_rollback_compensated_recovery_binds_origin_before_lock_adoption() -> None:
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    start = rollback.index(
        'if ([string]$compensatedJournal.payload.phase -eq "COMPENSATED")'
    )
    end = rollback.index("return Invoke-DawnstrikeRuntimeRollback", start)
    recovery = rollback[start:end]
    origin_read = recovery.index("$compensationOrigin = Get-DawnstrikeGitValue")
    origin_hash = recovery.index(
        "Get-DawnstrikeSha256Text $compensationOrigin", origin_read
    )
    identity = recovery.index(
        "$compensationOriginIdentity = Convert-DawnstrikeCanonicalOriginIdentity",
        origin_hash,
    )
    adoption = recovery.index("Adopt-DawnstrikeGovernedRuntimeLockWithJournal")
    clearing = recovery.index("Clear-DawnstrikeCompensatedJournalTombstone")
    assert origin_read < origin_hash < identity < adoption < clearing
    assert recovery.count("-OriginIdentity $compensationOriginIdentity") == 2
    assert "Convert-DawnstrikeCanonicalOriginIdentity $origin" not in recovery


def test_compensated_consumers_bind_exact_predecessor_and_restored_task() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(
        encoding="utf-8"
    )

    for script, journal_name in (
        (activation, "$compensatedJournal"),
        (rollback, "$compensatedJournal"),
    ):
        assert (
            "$compensationPayload.prior_journal_file_sha256 -ne "
            f"[string]{journal_name}.payload.prior_journal_file_sha256"
        ) in script
        assert (
            "$compensatedTasks.task_action_contract_sha256 -ne "
            "[string]$compensationPayload.task_action_contract_sha256"
        ) in script
        assert (
            "$compensatedTasks.task_definition_contract_sha256 -ne "
            "[string]$compensationPayload.task_definition_contract_sha256"
        ) in script

    assert (
        "$compensationPayload.prior_journal_file_sha256 -ne "
        "[string]$startJournal.payload.prior_journal_file_sha256"
    ) in rebind
    assert (
        "$compensationPayload.task_action_contract_sha256 -ne "
        "$restoredStart.action_contract_sha256"
    ) in rebind
    assert (
        "$compensationPayload.task_definition_contract_sha256 -ne "
        "$restoredStart.definition_contract_sha256"
    ) in rebind


def test_rollback_compensation_uses_powershell_51_relative_path_logic() -> None:
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    assert "[System.IO.Path]::GetRelativePath" not in rollback
    assert "$receiptFullPath.Substring($statePrefix.Length)" in rollback
    assert "Rollback activation receipt is outside StateRoot." in rollback


def test_hardening_journal_tracks_live_runtime_as_current_and_previous() -> None:
    hardening = Path("scripts/harden_intraday_capture_task.ps1").read_text(
        encoding="utf-8"
    )
    assert "CurrentSha = $CandidateSha" not in hardening
    assert "CurrentTree = $CandidateTree" not in hardening
    assert "-CurrentSha $CandidateSha -CurrentTree $CandidateTree" not in hardening
    assert hardening.count("CurrentSha = $runtimeIdentity.head") == 2
    assert hardening.count("CurrentTree = $runtimeIdentity.tree") == 2
    assert (
        hardening.count(
            "-CurrentSha $runtimeIdentity.head -CurrentTree $runtimeIdentity.tree"
        )
        == 3
    )


def test_origin_advance_allows_only_exact_lock_bound_recovery() -> None:
    lock = Path("scripts/runtime_activation_lock.ps1").read_text(encoding="utf-8")
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    hardening = Path("scripts/harden_intraday_capture_task.ps1").read_text(
        encoding="utf-8"
    )
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(
        encoding="utf-8"
    )

    helper = lock.index("function Get-DawnstrikeAdvancedOriginRecoveryAdmission")
    helper_end = lock.index("function Set-DawnstrikeRuntimeOperationJournalAdoption", helper)
    helper_body = lock[helper:helper_end]
    assert "Test-DawnstrikeRuntimeLockOwnerDead" in helper_body
    assert "payload.lock_token" in helper_body
    assert "payload.lock_file_sha256" in helper_body
    assert "old_lock_file_sha256" in helper_body
    assert "next_lock_file_sha256" in helper_body
    assert "requires exactly one lock-bound operation journal" in helper_body

    activation_gate = activation.index("if ($remoteMain -ne $ExpectedSha)")
    activation_evidence = activation.index("$evidence = Invoke-DawnstrikeContractCli")
    assert activation_gate < activation_evidence
    assert "-Operation runtime_activation" in activation[
        activation_gate:activation_evidence
    ]
    assert "merge-base" in activation[activation_gate:activation_evidence]

    deferred = hardening.index("-RefreshOrigin -DeferOriginMainAdmission")
    hardening_gate = hardening.index(
        "if ($script:HardeningRemoteMain -cne $CandidateSha)", deferred
    )
    assert "-Operation capture_task_hardening" in hardening[
        hardening_gate : hardening_gate + 1200
    ]
    assert "-AllowAdvancedOriginRecovery:" in hardening

    rebind_gate = rebind.index("if ($remoteMain.ToLowerInvariant() -ne $CandidateSha)")
    rebind_inputs = rebind.index("$inputs = Assert-DawnstrikeCaptureInput")
    assert rebind_gate < rebind_inputs
    assert "merge-base" in rebind[rebind_gate:rebind_inputs]
    assert "-Operation capture_task_rebind" in rebind[rebind_gate:rebind_inputs]

    for script in (activation, hardening, rebind):
        assert "RECOVERED_SUPERSEDED_TRANSACTION" in script
        assert "broker_execution_enabled = $false" in script


def test_recovery_tools_use_pinned_signed_python_and_git() -> None:
    lock = Path("scripts/runtime_activation_lock.ps1").read_text(encoding="utf-8")
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    scripts = [
        activation,
        Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8"),
        Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8"),
        Path("scripts/harden_intraday_capture_task.ps1").read_text(encoding="utf-8"),
        Path("scripts/prepare_dawnstrike_state.ps1").read_text(encoding="utf-8"),
    ]

    assert "$script:DawnstrikeApprovedGitPath=" in lock
    assert "$script:DawnstrikeApprovedGitSha256=" in lock
    assert "$script:DawnstrikeApprovedGitSubject=" in lock
    assert "$script:DawnstrikeApprovedGitThumbprint=" in lock
    assert "function Get-DawnstrikeApprovedGit" in lock
    assert "Get-AuthenticodeSignature" in lock
    assert "Approved Git executable hash changed" in lock
    assert "Approved Git executable signer is invalid" in lock

    for script in scripts:
        assert "Get-Command py.exe" not in script
        assert "Get-Command git.exe" not in script
        assert "Get-DawnstrikeApprovedGit" in script
        assert "Get-DawnstrikeApprovedLockInterpreter" in script

    process_start = activation.index("function Invoke-DawnstrikeActivationProcess")
    process_end = activation.index("function Get-DawnstrikeActivationNowUtc", process_start)
    process = activation[process_start:process_end]
    assert "$effectiveArguments = @('-I', '-B') + $effectiveArguments" in process
    assert "-ArgumentList $effectiveArguments" in process


def test_state_preparation_strictly_classifies_hash_bound_lock_archives() -> None:
    script = Path("scripts/prepare_dawnstrike_state.ps1").read_text(encoding="utf-8")
    assert "^recovered-stale-([0-9a-f]{64})\\.lock$" in script
    assert (
        "^dawnstrike-daily-(\\d{4}-\\d{2}-\\d{2})\\.lock\\.stale-dead-([0-9a-f]{64})$"
        in script
    )
    assert "Get-DawnstrikeStrictRuntimeLock" in script
    assert "Get-DawnstrikeLockSnapshot" in script
    assert "Test-DawnstrikeLockOwnerActive" in script
    assert "$locks += $lockItem" in script


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


def test_activation_recovery_teardown_keeps_journal_until_locks_are_released() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )

    init = activation.index('if ([string]$journal.payload.phase -eq "INIT")')
    pre_quiesce = activation.index(
        'if ([string]$journal.payload.phase -eq "PRE_QUIESCE")', init
    )
    init_release = activation.index(
        "Exit-DawnstrikeGovernedRuntimeLock $activationLock", init, pre_quiesce
    )
    init_kill = activation.index(
        'if ($TestStageCrashPoint -eq "after_init_recovery_lock_release")',
        init_release,
        pre_quiesce,
    )
    init_journal_remove = activation.index(
        "Remove-Item -LiteralPath $operationJournal -Force", init_kill, pre_quiesce
    )
    assert init_release < init_kill < init_journal_remove

    pre_swap = activation.index(
        'if ([string]$journal.payload.phase -eq "PRE_SWAP")', pre_quiesce
    )
    daily_release = activation.index(
        "Exit-DawnstrikeDailyRunLock $recoveryDaily", pre_quiesce, pre_swap
    )
    activation_release = activation.index(
        "Exit-DawnstrikeGovernedRuntimeLock $activationLock", daily_release, pre_swap
    )
    quiesce_kill = activation.index(
        'if ($TestStageCrashPoint -eq "after_pre_quiesce_recovery_lock_release")',
        activation_release,
        pre_swap,
    )
    quiesce_journal_remove = activation.index(
        "Remove-Item -LiteralPath $operationJournal -Force",
        quiesce_kill,
        pre_swap,
    )
    assert daily_release < activation_release < quiesce_kill < quiesce_journal_remove
    assert "Recovery tombstone owner is still active" in activation
    assert "Recovery tombstone changed during validation" in activation


def test_activation_pre_swap_recovers_exact_post_second_rename_state() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )

    recovery = activation.index(
        'if ([string]$journal.payload.phase -eq "PRE_SWAP")'
    )
    installed_case = activation.index(
        "elseif ($runtimePresent -and $rollbackPresent -and -not $stagePresent)",
        recovery,
    )
    candidate_proof = activation.index(
        "$candidateRecovery = Get-DawnstrikeGitContract", installed_case
    )
    previous_proof = activation.index(
        "$previousRecovery = Get-DawnstrikeGitContract", candidate_proof
    )
    candidate_origin = activation.index(
        "$candidateRecoveryOrigin = Convert-DawnstrikeCanonicalOriginIdentity",
        previous_proof,
    )
    transition = activation.index("-Operation runtime_activation -Phase POST_SWAP", installed_case)
    assert installed_case < candidate_proof < previous_proof < candidate_origin < transition
    assert 'if ($TestStageCrashPoint -eq "after_candidate_runtime_rename")' in activation
    assert "PRE_SWAP installed/previous runtime identity is invalid" in activation


def test_activation_seals_truthful_ready_then_terminal_evidence() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    ready = activation.index(
        "-Operation runtime_activation -Phase POST_SWAP_READY -CandidateSha $ExpectedSha"
    )
    ready_receipt = activation.index('"Ready-to-enable activation receipt sealing"')
    enable = activation.index("            Enable-DawnstrikeCanonicalTasks", ready)
    complete_receipt = activation.index('"Complete activation receipt sealing"', enable)
    complete = activation.index(
        "-Operation runtime_activation -Phase COMPLETE -CandidateSha $ExpectedSha",
        enable,
    )
    assert ready_receipt < ready < enable < complete_receipt < complete
    assert 'if ($TestStageCrashPoint -eq "after_ready_journal")' in activation
    assert 'if ($TestStageCrashPoint -eq "after_enable_before_complete")' in activation
    assert 'phase -eq "POST_SWAP_READY"' in activation
    assert "FileOptions]::WriteThrough" in activation


def test_rollback_seals_ready_then_terminal_evidence_after_task_enablement() -> None:
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    receipt = rollback.index('"Rollback ready receipt sealing"')
    ready = rollback.index(
        "-Operation runtime_rollback -Phase POST_SWAP_READY", receipt
    )
    enable = rollback.index("        Enable-DawnstrikeCanonicalTasks", ready)
    terminal_receipt = rollback.index('"Rollback terminal receipt sealing"', enable)
    complete = rollback.index(
        "-Operation runtime_rollback -Phase COMPLETE", terminal_receipt
    )
    assert receipt < ready < enable < terminal_receipt < complete
    assert 'schema_version = "dawnstrike.runtime_rollback_receipt.v2"' in rollback
    assert 'schema_version = "dawnstrike.runtime_rollback_receipt.v1"' in rollback
    assert "Set-DawnstrikeCanonicalTaskExpectedSha" in rollback
    assert "-ExpectedSha $previousSha" in rollback
    assert "Get-DawnstrikeTaskXmlBackupManifest" in rollback
    assert "previous-SHA Ready boundary" in rollback
    assert 'journalPhase -in @("POST_SWAP", "POST_SWAP_READY")' in rollback
    assert 'DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_ready"' in rollback
    assert 'DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_enable"' in rollback


def test_lock_adoption_recovers_a_replace_completed_before_process_death() -> None:
    lock = Path("scripts/runtime_activation_lock.ps1").read_text(encoding="utf-8")
    recovered_round = lock.index(
        'elseif($current.raw_file_sha256-eq$payload.next_lock_file_sha256)'
    )
    new_temp = lock.index('$nextName=\'.next-runtime-lock-\'', recovered_round)
    reseal = lock.index("'ADOPTION_PREPARED'", recovered_round)
    owner_guard = lock.index(
        'process_started_at_utc -ne $ownerStart', recovered_round
    )
    assert recovered_round < new_temp < reseal < owner_guard
    assert "return [pscustomobject]@{path=$path" in lock


def test_complete_activation_retry_reconciles_only_exact_owned_locks() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )

    complete = activation.index(
        "if (Test-Path -LiteralPath $completeReceipt -PathType Leaf)"
    )
    artifact_proof = activation.index(
        "$null = Assert-DawnstrikeReceiptRecoveryArtifacts", complete
    )
    lock_branch = activation.index(
        "if (Test-Path -LiteralPath $completeRuntimeLockPath -PathType Leaf)",
        artifact_proof,
    )
    adopt = activation.index(
        "$completeLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal",
        lock_branch,
    )
    daily = activation.index(
        "$completeDailyLock = Enter-DawnstrikeDailyRunLock", adopt
    )
    handshake = activation.index(
        "Confirm-DawnstrikeActivationDailyLockHandshake", daily
    )
    daily_release = activation.index(
        "Exit-DawnstrikeDailyRunLock $completeDailyLock", handshake
    )
    runtime_release = activation.index(
        "Exit-DawnstrikeGovernedRuntimeLock $completeLock", daily_release
    )
    returned = activation.index("return $existing", runtime_release)
    assert (
        artifact_proof
        < lock_branch
        < adopt
        < daily
        < handshake
        < daily_release
        < runtime_release
        < returned
    )
    assert (
        "Complete activation retry found a daily lock without its exact runtime lock"
        in activation
    )
    assert 'if ($TestStageCrashPoint -eq "after_complete_journal")' in activation
    assert "Stop-Process -Id $PID -Force" in activation


def test_installed_candidate_complete_retry_releases_stranded_lock_pair() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )
    installed = activation.index("if ($runtimeContract.head -eq $ExpectedSha)")
    next_preflight = activation.index("$dbPath = Join-Path $state", installed)
    installed_branch = activation[installed:next_preflight]

    journal = installed_branch.index(
        "$earlyJournal = Get-DawnstrikeStrictRuntimeOperationJournal"
    )
    complete = installed_branch.index(
        '[string]$earlyJournal.payload.phase -ne "COMPLETE"', journal
    )
    foreign_guard = installed_branch.index(
        "Existing COMPLETE activation has a foreign or multiple daily lock set",
        complete,
    )
    adopt = installed_branch.index(
        "$earlyLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal",
        foreign_guard,
    )
    handshake = installed_branch.index(
        "Confirm-DawnstrikeActivationDailyLockHandshake", adopt
    )
    daily_release = installed_branch.index(
        "Exit-DawnstrikeDailyRunLock $earlyDaily", handshake
    )
    runtime_release = installed_branch.index(
        "Exit-DawnstrikeGovernedRuntimeLock $earlyLock", daily_release
    )
    returned = installed_branch.index("return $receipt", runtime_release)
    assert (
        journal
        < complete
        < foreign_guard
        < adopt
        < handshake
        < daily_release
        < runtime_release
        < returned
    )
    assert (
        "Existing COMPLETE activation has a daily lock without its exact runtime lock"
        in installed_branch
    )


def test_activation_nonterminal_failure_preserves_adoptable_lock_pair() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )

    pre_quiesce = activation.index(
        "-Operation runtime_activation -Phase PRE_QUIESCE"
    )
    phase_mark = activation.index('$journalPhase = "PRE_QUIESCE"', pre_quiesce)
    body_mark = activation.index("$activationBodyStarted = $true", phase_mark)
    first_mutation = activation.index("Disable-DawnstrikeCanonicalTasks", body_mark)
    assert pre_quiesce < phase_mark < body_mark < first_mutation

    failure_reconcile = activation.index(
        "$failureJournal = Get-DawnstrikeStrictRuntimeOperationJournal"
    )
    preserve = activation.index(
            'if ($journalPhase -in @("PRE_QUIESCE", "PRE_SWAP", "POST_SWAP", "POST_SWAP_READY"))',
        failure_reconcile,
    )
    preserve_assignment = activation.index("$preserveLocks = $true", preserve)
    finally_block = activation.index("finally {", preserve)
    finally_guard = activation.index("if (-not $preserveLocks)", finally_block)
    assert failure_reconcile < preserve < preserve_assignment < finally_block < finally_guard
    assert "Nonterminal activation recovery lacks its adoptable lock pair" in activation
    assert "Nonterminal activation recovery lock pair was not preserved" in activation
    assert "if (-not $activationBodyStarted -and -not $preserveLocks" in activation

    recovery_start = activation.index(
        "$activationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal"
    )
    recovery_end = activation.index(
        "# Any strict recovery failure must retain the adopted runtime lock",
        recovery_start,
    )
    recovery_region = activation[recovery_start:recovery_end]
    assert "finally { Exit-DawnstrikeGovernedRuntimeLock $activationLock }" not in activation
    assert "Any strict recovery failure must retain the adopted runtime lock" in activation
    assert "Recovered COMPLETE activation could not reacquire its exact daily lock" in activation
    assert "Confirm-DawnstrikeActivationDailyLockHandshake" in recovery_region

    restore_failure = activation.rindex(
        "Runtime activation failed and automatic restore could not be completed;"
    )
    fail_closed = activation.rindex(
        "Set-DawnstrikeTasksFailClosedDisabled $runtime $state", 0, restore_failure
    )
    auxiliary_fail_closed = activation.index(
        "Disable-DawnstrikeAuxiliaryCaptureTask $runtime $state",
        fail_closed,
        restore_failure,
    )
    preserve_after_success = activation.index(
        "$preserveLocks = $true", auxiliary_fail_closed, restore_failure
    )
    assert fail_closed < auxiliary_fail_closed < preserve_after_success < restore_failure


def test_production_entrypoint_fault_injection_switches_are_environment_guarded() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8")

    activation_guard = (
        '$InjectCrashBetweenRuntimeRenames -and $env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1"'
    )
    rebind_guard = (
        '($InjectFailureAfterMutation -or $InjectCrashAfterEnable) -and\n'
        '    $env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1"'
    )
    assert activation.count(activation_guard) == 2
    assert activation.index(activation_guard) < activation.index(
        "Invoke-DawnstrikeRuntimeActivation"
    )
    assert "Activation runtime-rename crash injection is test-only." in activation
    assert rebind_guard in rebind
    assert rebind.index(rebind_guard) < rebind.index(
        '. (Join-Path $PSScriptRoot "activate_dawnstrike_runtime.ps1")'
    )
    assert "Capture-task rebind failure and crash injection are test-only." in rebind


def test_activation_init_cleanup_quarantines_completed_scheduler_backup() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(
        encoding="utf-8"
    )

    staging_cleanup = activation.index(
        'if (-not $activationBodyStarted -and -not $preserveLocks'
    )
    scheduler_backup = activation.index(
        'if (Test-Path -LiteralPath $schedulerBackupPath)', staging_cleanup
    )
    quarantine = activation.index(
        'Move-Item -LiteralPath $schedulerBackupPath -Destination',
        scheduler_backup,
    )
    release = activation.index(
        "Exit-DawnstrikeGovernedRuntimeLock $activationLock", quarantine
    )
    remove_journal = activation.index(
        "Remove-Item -LiteralPath $operationJournal -Force", release
    )
    assert staging_cleanup < scheduler_backup < quarantine < release < remove_journal
    assert "Failed scheduler backup quarantine did not complete" in activation


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
        "capture_task_safety.ps1",
        "rollback_dawnstrike_runtime.ps1",
        "runtime_activation_contract.py",
        "dawnstrike_job_process.ps1",
        "dawnstrike_process_runner.ps1",
        "dawnstrike_python_bootstrap.py",
        "invoke_dawnstrike_stage.ps1",
        "state_disaster_recovery.py",
        "run_alphaops_morning.ps1",
        "run_alphaops_monitor.ps1",
        "run_alphaops_eod.ps1",
        "run_alphaops_weekly_training.ps1",
        "run_daily_finalize.ps1",
        "import_dawnstrike_environment.ps1",
        "alpha_cycle_artifact.ps1",
        "monitor_schedule_helper.ps1",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    _install_local_origin_fixture_seam(candidate / "scripts" / "runtime_activation_lock.ps1")
    _install_local_github_ci_fixture_seam(candidate / "scripts" / "runtime_activation_contract.py")
    shutil.copytree(
        source / "intraday_scanner",
        candidate / "intraday_scanner",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(source / ".gitignore", candidate / ".gitignore")
    shutil.copy2(source / "pyproject.toml", candidate / "pyproject.toml")
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
    ci_payload["run_url"] = "https://github.com/mattfren/DawnStrike/actions/runs/12345"
    ci_payload["candidate_sha"] = candidate_sha
    ci_payload["candidate_tree"] = candidate_tree
    sol_payload = _sol_payload()
    sol_payload["candidate_sha"] = candidate_sha
    sol_payload["candidate_tree"] = candidate_tree
    owner_comment_body = activation_contract._owner_authorization_body(
        sol_payload, candidate_sha=candidate_sha, candidate_tree=candidate_tree
    )
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
    $env:DAWNSTRIKE_TEST_ACTIVATION_CLOCK = '1'
    $env:DAWNSTRIKE_TEST_GITHUB_CI_FIXTURE = '1'
    $env:DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_SHA = '{candidate_sha}'
    $env:DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_TREE = '{candidate_tree}'
        $env:DAWNSTRIKE_TEST_GITHUB_CI_COMPLETED_AT = '{ci_payload["completed_at_utc"]}'
        $env:DAWNSTRIKE_TEST_GITHUB_OWNER_COMMENT_BODY = '{owner_comment_body}'
$global:MockRuntime = '{values["runtime"]}'
$global:MockState = '{values["state"]}'
$global:MockTaskStates = @{{}}
$global:MockTaskExpectedSha = @{{}}
$global:TaskEvents = @()
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{
    $global:MockTaskStates[$name] = 'Ready'
}}
    function Get-ScheduledTask {{
        [CmdletBinding()] param([string]$TaskName)
        if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ return @() }}
        $boundSha = [string]$global:MockTaskExpectedSha[$TaskName]
        $policy = if ($boundSha) {{
            Get-DawnstrikeCanonicalTaskPolicy `
                $TaskName $global:MockRuntime $global:MockState $boundSha
        }} else {{
            Get-DawnstrikeCanonicalTaskPolicy $TaskName $global:MockRuntime $global:MockState
        }}
        $triggerType = if ($policy.weekly) {{
            'MSFT_TaskWeeklyTrigger'
        }} else {{ 'MSFT_TaskDailyTrigger' }}
        $dayOfWeek = if ($policy.weekly) {{ [int]$policy.days }} else {{ $null }}
        $weekInterval = if ($policy.weekly) {{ 1 }} else {{ $null }}
        $dayInterval = if ($policy.weekly) {{ $null }} else {{ 1 }}
        $repetition = if ($policy.monitor) {{
            [pscustomobject]@{{ Interval='PT5M'; Duration='PT6H35M'; StopAtDurationEnd=$true }}
        }} else {{
            [pscustomobject]@{{ Interval=''; Duration=''; StopAtDurationEnd=$false }}
        }}
        [pscustomobject]@{{
            State=$global:MockTaskStates[$TaskName]; TaskPath='\';
            Actions=@([pscustomobject]@{{
                    Execute=$script:DawnstrikePowerShellExecutable;
                    Arguments=$policy.arguments;
                WorkingDirectory=$global:MockRuntime
            }});
            Triggers=@([pscustomobject]@{{
                CimClass=[pscustomobject]@{{ CimClassName=$triggerType }};
                Enabled=$true;
                DaysOfWeek=$dayOfWeek;
                WeeksInterval=$weekInterval;
                DaysInterval=$dayInterval;
                StartBoundary=('2026-08-31T' + $policy.start + ':00-05:00');
                EndBoundary=$null;
                RandomDelay=$null;
                Repetition=$repetition
            }});
            Principal=[pscustomobject]@{{
                LogonType='Password'; UserId='activation-test'; RunLevel='Limited'
            }};
            Settings=[pscustomobject]@{{
                Enabled=($global:MockTaskStates[$TaskName] -eq 'Ready');
                StartWhenAvailable=$true;
                WakeToRun=$true;
                StopIfGoingOnBatteries=$false;
                DisallowStartIfOnBatteries=$false;
                MultipleInstances='IgnoreNew';
                ExecutionTimeLimit=$policy.execution_limit;
                RestartCount=$policy.restart_count;
                RestartInterval=$policy.restart_interval;
                Hidden=$false;
                RunOnlyIfIdle=$false;
                RunOnlyIfNetworkAvailable=$false;
                UseUnifiedSchedulingEngine=$true
            }}
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
function New-ScheduledTaskAction {{
    [CmdletBinding()] param([string]$Execute,[string]$Argument,[string]$WorkingDirectory)
    [pscustomobject]@{{ Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory }}
}}
function Set-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath,$Action)
        $match = [regex]::Match(
            [string](@($Action)[0].Arguments),
            '-ExpectedSha\s+["'']?([0-9a-f]{{40}})'
        )
    if (-not $match.Success) {{ throw 'mock task action did not carry an exact expected SHA' }}
    $global:MockTaskExpectedSha[$TaskName] = $match.Groups[1].Value
    [pscustomobject]@{{ TaskName=$TaskName }}
}}
$activated = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120 -TestNowUtc '2026-08-30T14:00:00Z'
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
      -BackupRetention 5 -ProcessTimeoutSeconds 120 -TestNowUtc '2026-08-30T14:00:00Z'
}}
catch {{ $activationMissingBundleBlocked = $true }}
finally {{ [System.IO.File]::Move($heldBundlePath, $bundlePath) }}
$activatedAgain = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120 -TestNowUtc '2026-08-30T14:00:00Z'
$receiptName = 'runtime-activation-' + $activated.activation_id + '.json'
    $receiptForRollback = Join-Path `
        '{values["state"]}' ('receipts\runtime-activation\' + $receiptName)
    . '{rollback_script}'
    $rollbackLegacyBlocked = $false
    try {{
        $null = Invoke-DawnstrikeRuntimeRollback `
          -ActivationReceipt $receiptForRollback -ContractRoot '{values["candidate"]}' `
          -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
          -BackupRoot '{values["backup"]}' `
          -ProcessTimeoutSeconds 120
    }}
    catch {{
        $rollbackLegacyBlocked = $_.Exception.Message -match 'quarantined|authorized COMPLETE'
    }}
    $output = [pscustomobject]@{{
        activated=$activated
        activated_again=$activatedAgain
        rollback_legacy_blocked=$rollbackLegacyBlocked
        activation_missing_bundle_blocked=$activationMissingBundleBlocked
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
    assert payload["activated"]["previous_runtime_rollback_authorized"] is False
    assert payload["activated"]["previous_runtime_disposition"] == "QUARANTINED_UNAUTHORIZED"
    assert payload["activated_again"]["receipt_sha256"] == payload["activated"]["receipt_sha256"]
    assert payload["activation_missing_bundle_blocked"] is True
    assert payload["rollback_legacy_blocked"] is True
    assert set(payload["task_states"].values()) == {"Ready"}
    assert len(payload["task_events"]) == 10
    assert all(event.startswith("disable:") for event in payload["task_events"][:5])
    assert all(event.startswith("enable:") for event in payload["task_events"][5:10])
    assert _git(runtime, "rev-parse", "HEAD") == candidate_sha
    assert _git(runtime, "rev-parse", "HEAD") != previous_sha
    assert not _git(runtime, "status", "--porcelain=v1", "--untracked-files=all")
    assert hashlib.sha256(db.read_bytes()).hexdigest() == db_hash_before
    assert not list((state / "locks").glob("*.lock"))
    assert (
        backup / f"runtime-activation-{payload['activated']['activation_id']}" / "receipt.json"
    ).is_file()
    activation_scheduler_backup = (
        state / "scheduler-backups" / payload["activated"]["scheduler_backup_name"]
    )
    assert (activation_scheduler_backup / "manifest.json").is_file()
    assert len(list(activation_scheduler_backup.glob("*.xml"))) == 5
