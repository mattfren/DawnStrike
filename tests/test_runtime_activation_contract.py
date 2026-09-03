from __future__ import annotations

import hashlib
import importlib.metadata
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, run_migrations
from scripts import runtime_activation_contract as activation_contract
from scripts import vercel_publication_journal as vercel_journal
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
PRODUCTION_RECORD_SET_SHA256 = (
    "447a0d12feffcfd6c353d9acb4cfd1e5cc1b35e3548cd7e9ad58666516b4b3af"  # pragma: allowlist secret
)
PRODUCTION_GIT_PATH = r"C:\Program Files\Git\cmd\git.exe"
PRODUCTION_GIT_SHA256 = (
    "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"  # pragma: allowlist secret
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


def _write_vercel_pre_mutation_journal(path: Path) -> None:
    alias = "https://dawnstrike-command-center-x3.vercel.app"
    prior_url = "https://dawnstrike-command-center-x3-priorabc-mattfrens-projects.vercel.app"
    payload: dict[str, object] = {
        "schema_version": vercel_journal.SCHEMA,
        "operation": "vercel_publication",
        "phase": "PRE_MUTATION",
        "sequence": 0,
        "project_id": "prj_test",
        "project_name": "dawnstrike-command-center-x3",
        "provider_scope": "mattfrens-projects",
        "production_aliases": [alias],
        "candidate_preview_url": (
            "https://dawnstrike-command-center-x3-previewabc-mattfrens-projects.vercel.app"
        ),
        "candidate_preview_deployment_id": "dpl_preview",
        "candidate_source_sha": "a" * 40,
        "candidate_source_tree": "b" * 40,
        "toolchain_identity_sha256": "9" * 64,
        "candidate_market_date": "2026-08-31",
        "candidate_build_id": "candidate-build",
        "candidate_build_sha": "c" * 64,
        "candidate_build_manifest_sha256": "8" * 64,
        "candidate_release_manifest_sha256": "a" * 64,
        "candidate_public_artifact_root_sha256": "1" * 64,
        "candidate_manifest_sha256": "d" * 64,
        "candidate_package_manifest_sha256": "e" * 64,
        "prior_aliases": [
            {
                "alias": alias,
                "deployment_id": "dpl_prior",
                "deployment_url": prior_url,
                "health_status": "alive",
                "readiness_status": "ready",
                "readiness_http_status": 200,
                "source_sha": "1" * 40,
                "source_tree": "2" * 40,
                "source_manifest_sha256": "3" * 64,
                "build_manifest_sha256": "4" * 64,
                "release_manifest_sha256": "5" * 64,
                "artifact_proof": {
                    "endpoint": prior_url,
                    "build_sha": "6" * 64,
                    "asset_count": 2,
                    "total_bytes": 100,
                    "file_hashes_sha256": "7" * 64,
                },
                "rollback_contract": {
                    "schema_version": "dawnstrike.vercel_rollback_target.v1",
                    "mode": "READY_SOURCE_MANIFEST",
                    "health_status": "alive",
                    "readiness_status": "ready",
                    "readiness_http_status": 200,
                    "readiness_reason": "complete",
                    "readiness_failed_checks": [],
                    "source_proof": {
                        "kind": "deployed_source_manifest",
                        "sha256": "3" * 64,
                    },
                },
            }
        ],
        "promoted_deployment_id": None,
        "promoted_deployment_url": None,
        "production_result_sha256": vercel_journal.EMPTY_SHA256,
        "result_relative_path": (
            "outputs/daily_finalize/vercel-publication/2026-08-31/daily-deployment-result.json"
        ),
        "result_payload": None,
        "prior_journal_file_sha256": vercel_journal.EMPTY_SHA256,
        "compensation_relative_path": "NONE",
        "compensation_sha256": vercel_journal.EMPTY_SHA256,
        "recorded_at_utc": "2026-08-31T12:00:00.000000Z",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["journal_self_sha256"] = hashlib.sha256(
        vercel_journal.canonical_json(payload)
    ).hexdigest()
    raw = vercel_journal.canonical_json(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    assert (
        vercel_journal.validate(
            raw, state_root=path.parents[4], journal_path=path, runtime_root=None
        )["phase"]
        == "PRE_MUTATION"
    )


def _git(root: Path, *arguments: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(root), *arguments],
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    return completed.stdout.strip()


def _host_dependency_record_contract(requirements_lock: Path) -> str:
    requirements: dict[str, str] = {}
    for line in requirements_lock.read_text(encoding="utf-8").splitlines():
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s\\]+)",
            line.strip(),
        )
        if match:
            requirements[re.sub(r"[-_.]+", "-", match.group(1)).lower()] = match.group(2)
    installed = {
        re.sub(r"[-_.]+", "-", dist.metadata["Name"]).lower(): dist
        for dist in importlib.metadata.distributions()
        if dist.metadata.get("Name")
    }
    rows = []
    for name, version in sorted(requirements.items()):
        dist = installed[name]
        assert dist.version == version
        record = next(item for item in dist.files or () if str(item).endswith(".dist-info/RECORD"))
        record_path = Path(dist.locate_file(record)).resolve(strict=True)
        rows.append(f"{name}\0{version}\0{hashlib.sha256(record_path.read_bytes()).hexdigest()}\n")
    return hashlib.sha256("".join(rows).encode()).hexdigest()


def _copy_bootstrap_for_host(destination: Path) -> None:
    discovered = shutil.which("git")
    assert discovered is not None
    git_path = Path(discovered).resolve()
    git_sha256 = hashlib.sha256(git_path.read_bytes()).hexdigest()
    record_contract = _host_dependency_record_contract(Path("requirements.lock"))
    source = Path("scripts/dawnstrike_python_bootstrap.py").read_text(encoding="utf-8")
    source = (
        source.replace(
            f'_APPROVED_GIT = Path(r"{PRODUCTION_GIT_PATH}")',
            f"_APPROVED_GIT = Path({str(git_path)!r})",
            1,
        )
        .replace(PRODUCTION_GIT_SHA256, git_sha256, 1)
        .replace(PRODUCTION_RECORD_SET_SHA256, record_contract, 1)
    )
    assert repr(str(git_path)) in source
    assert git_sha256 in source
    assert record_contract in source
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


def _install_local_interpreter_fixture_seam(candidate: Path) -> None:
    """Bind disposable activation fixtures to the signed interpreter running pytest."""

    production = r"C:\Program Files\Dawnstrike\Python313\python.exe"
    fixture = str(Path(sys.executable).resolve())
    for relative in (
        "scripts/activate_dawnstrike_runtime.ps1",
        "scripts/runtime_activation_lock.ps1",
        "scripts/dawnstrike_process_runner.ps1",
        "scripts/runtime_activation_contract.py",
        "scripts/vercel_toolchain_contract.py",
    ):
        path = candidate / relative
        text = path.read_text(encoding="utf-8")
        if production not in text:
            raise AssertionError(f"interpreter fixture seam is absent from {relative}")
        path.write_text(text.replace(production, fixture), encoding="utf-8")


def _install_local_bootstrap_origin_fixture_seam(
    bootstrap_script: Path, *, origin: Path
) -> None:
    """Authorize one exact disposable bare origin in a copied bootstrap only."""

    text = bootstrap_script.read_text(encoding="utf-8")
    anchor = (
        "    if origin_url is not None and origin_url not in _GOVERNED_ORIGIN_URLS:\n"
    )
    assert text.count(anchor) == 1
    replacement = (
        f"    fixture_governed_origins = _GOVERNED_ORIGIN_URLS | {{{str(origin)!r}}}\n"
        "    if origin_url is not None and origin_url not in fixture_governed_origins:\n"
    )
    bootstrap_script.write_text(text.replace(anchor, replacement, 1), encoding="utf-8")


def _install_local_github_ci_fixture_seam(contract_script: Path) -> None:
    """Use deterministic GitHub authority responses in a copied candidate only."""

    text = contract_script.read_text(encoding="utf-8")
    start = text.index("def _github_api_object(")
    end = text.index("\ndef validate_live_github_ci", start)
    original = text[start:end].replace("def _github_api_object(", "def _github_api_object_live(", 1)
    fixture = r"""

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
"""
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
        "capture_interpreter_path": (r"C:\Program Files\Dawnstrike\Python313\python.exe"),
        "capture_interpreter_version": "3.13.14",
        "capture_interpreter_sha256": (
            "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"
        ),
        "capture_interpreter_signer_subject": (
            "CN=Python Software Foundation, O=Python Software Foundation, "
            "L=Beaverton, S=Oregon, C=US"
        ),
        "capture_interpreter_signer_thumbprint": ("9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48"),
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
        Path(__file__).resolve().parents[1] / "scripts" / "activate_dawnstrike_runtime.ps1"
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
    declaration = script.split("function Get-DawnstrikeStatePreparationDeclaration", 1)[1].split(
        "function Get-DawnstrikeAuxiliaryCaptureTask", 1
    )[0]
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
        assert "Assert-DawnstrikeCandidateIdentityAndDeclaration" in Path(companion).read_text(
            encoding="utf-8"
        )


def test_powershell_receipt_paths_use_valid_backslash_regex() -> None:
    scripts = tuple(Path("scripts").glob("*.ps1"))
    source_by_script = {script: script.read_text(encoding="utf-8") for script in scripts}
    assert (
        source_by_script[Path("scripts/activate_dawnstrike_runtime.ps1")].count(
            r"-replace '\\','/'"
        )
        >= 2
    )
    assert (
        source_by_script[Path("scripts/harden_intraday_capture_task.ps1")].count(
            r"-replace '\\','/'"
        )
        >= 1
    )
    assert (
        source_by_script[Path("scripts/rebind_intraday_capture_task.ps1")].count(
            r"-replace '\\','/'"
        )
        >= 1
    )
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
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "valid": True,
        "declaration": True,
        "identity": True,
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize(
    ("now_local", "eod_last", "finalizer_last", "weekly_last", "weekly_next", "pending", "error"),
    [
        (
            "2026-09-03T18:00:00",
            "2026-09-03T15:15:00",
            "2026-09-03T17:30:00",
            "2026-08-31T21:00:00",
            "2026-09-07T21:00:00",
            False,
            None,
        ),
        (
            "2026-09-03T18:00:00",
            "2026-09-02T15:15:00",
            "2026-09-02T17:30:00",
            "2026-08-31T21:00:00",
            "2026-09-07T21:00:00",
            False,
            "post-Finalizer window",
        ),
        (
            "2026-09-03T18:00:00",
            "2026-09-03T15:15:00",
            "2026-09-03T17:30:00",
            "2026-08-31T21:00:00",
            "2026-09-07T21:00:00",
            True,
            "pending same-day canonical trigger",
        ),
        (
            "2026-09-07T22:00:00",
            "2026-09-07T15:15:00",
            "2026-09-07T17:30:00",
            "2026-08-31T21:00:00",
            "2026-09-14T21:00:00",
            False,
            "same-day Weekly task",
        ),
        (
            "2026-09-07T22:00:00",
            "2026-09-07T15:15:00",
            "2026-09-07T17:30:00",
            "2026-09-07T21:00:00",
            "2026-09-14T21:00:00",
            False,
            None,
        ),
    ],
)
def test_activation_post_finalizer_snapshot_is_exact_and_fail_closed(
    now_local: str,
    eod_last: str,
    finalizer_last: str,
    weekly_last: str,
    weekly_next: str,
    pending: bool,
    error: str | None,
) -> None:
    names = [
        "Dawnstrike AlphaOps Morning",
        "Dawnstrike AlphaOps Monitor 5m",
        "Dawnstrike AlphaOps EOD Full Report",
        "Dawnstrike AlphaOps V6 Weekly Training",
        "Dawnstrike 10of10 Daily Finalize",
    ]
    next_day = (datetime.fromisoformat(now_local) + timedelta(days=1)).strftime(
        "%Y-%m-%dT08:00:00"
    )
    snapshots = [
        {
            "name": name,
            "state": "Ready",
            "last_run_time": "2026-09-01T08:00:00",
            "next_run_time": next_day,
        }
        for name in names
    ]
    snapshots[2]["last_run_time"] = eod_last
    snapshots[3]["last_run_time"] = weekly_last
    snapshots[3]["next_run_time"] = weekly_next
    snapshots[4]["last_run_time"] = finalizer_last
    if pending:
        snapshots[4]["next_run_time"] = now_local[:10] + "T19:00:00"
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    payload = json.dumps(snapshots, separators=(",", ":"))
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{script}'
$snapshots = @((ConvertFrom-Json @'
{payload}
'@) | ForEach-Object {{ $_ }})
$local = [DateTime]::SpecifyKind(
    [DateTime]::ParseExact(
        '{now_local}', 'yyyy-MM-ddTHH:mm:ss',
        [Globalization.CultureInfo]::InvariantCulture
    ),
    [DateTimeKind]::Unspecified
)
$nowUtc = [DateTimeOffset]::new(
    $local,
    [TimeZoneInfo]::Local.GetUtcOffset($local)
).ToUniversalTime()
$null = Assert-DawnstrikePostFinalizerBoundarySnapshot `
    -NowUtc $nowUtc -TaskSnapshots $snapshots
'PASS'
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if error is None:
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert result.stdout.strip().splitlines()[-1] == "PASS"
    else:
        assert result.returncode != 0
        assert error in result.stderr


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_activation_source_admission_rejects_config_swap_between_validation_and_lock(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _git(candidate, "init")
    config = candidate / ".git" / "config"
    safe_config = config.read_text(encoding="utf-8")
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    candidate_ps = str(candidate.resolve()).replace("'", "''")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{script}'
$env:DAWNSTRIKE_TEST_ACTIVATION_METADATA_RACE = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_CLOCK = '1'
try {{
    $held = Assert-DawnstrikeActivationSourceAdmission `
        -CandidateRoot '{candidate_ps}' `
        -ExpectedSha '{CANDIDATE_SHA}'
    foreach ($stream in @($held)) {{ $stream.Dispose() }}
    [Console]::Error.WriteLine('hostile metadata swap unexpectedly passed')
    exit 9
}}
catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 23
}}
"""
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=candidate,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_METADATA_LOCK_DELAY_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(
            f"metadata race did not reach synchronization point: {marker!r} {stdout!r} {stderr!r}"
        )
    config.write_text(
        safe_config + '\n[url "https://attacker.invalid/"]\n\tinsteadOf = https://github.com/\n',
        encoding="utf-8",
    )
    stdout, stderr = process.communicate(timeout=15)
    assert process.returncode == 23, (marker, stdout, stderr)
    assert "changed before its exact-byte handle lock was acquired" in stderr


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_git_value_keeps_long_repo_authority_out_of_process_current_directory(
    tmp_path: Path,
) -> None:
    activation_path = Path("scripts/activate_dawnstrike_runtime.ps1").resolve()
    controller_directory = activation_path.parent
    long_root = tmp_path / "state" / "recovery-quarantine"
    while len(str(long_root)) <= 270:
        long_root /= "compensated-activation-segment"
    (long_root / ".git").mkdir(parents=True)
    long_root_text = str(long_root.resolve())
    assert len(long_root_text) > 260

    activation = str(activation_path).replace("'", "''")
    root = long_root_text.replace("'", "''")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{activation}'
$global:CapturedGitLaunch = $null
function Invoke-DawnstrikeJobProcess {{
    [CmdletBinding()]
    param(
        [string]$FilePath,
        [string[]]$ArgumentList,
        [string]$WorkingDirectory,
        [string]$Label,
        [int]$TimeoutSeconds,
        [int]$OutputDrainTimeoutSeconds,
        [hashtable]$EnvironmentOverrides
    )
    $global:CapturedGitLaunch = [pscustomobject]@{{
        file_path = $FilePath
        arguments = @($ArgumentList)
        working_directory = $WorkingDirectory
        label = $Label
        timeout_seconds = $TimeoutSeconds
        git_dir = [string]$EnvironmentOverrides.GIT_DIR
        git_common_dir = [string]$EnvironmentOverrides.GIT_COMMON_DIR
        git_work_tree = [string]$EnvironmentOverrides.GIT_WORK_TREE
    }}
    [pscustomobject]@{{ Stdout = '  MOCK_GIT_OUTPUT  '; ExitCode = 0 }}
}}
$value = Get-DawnstrikeGitValue `
    -GitPath '{PRODUCTION_GIT_PATH}' `
    -Root '{root}' `
    -Arguments @('rev-parse', 'HEAD') `
    -Label 'Long quarantine Git authority regression' `
    -TimeoutSeconds 30
$arguments = @($global:CapturedGitLaunch.arguments)
$cIndex = [array]::IndexOf([object[]]$arguments, '-C')
[pscustomobject]@{{
    root_length = '{root}'.Length
    c_index = $cIndex
    git_target = if ($cIndex -ge 0) {{ [string]$arguments[$cIndex + 1] }} else {{ '' }}
    working_directory = [string]$global:CapturedGitLaunch.working_directory
    git_dir = [string]$global:CapturedGitLaunch.git_dir
    git_common_dir = [string]$global:CapturedGitLaunch.git_common_dir
    git_work_tree = [string]$global:CapturedGitLaunch.git_work_tree
    value = $value
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["root_length"] > 260
    assert payload["c_index"] >= 0
    assert payload["git_target"] == long_root_text
    assert Path(payload["working_directory"]).resolve() == controller_directory
    assert payload["working_directory"] != long_root_text
    assert Path(payload["git_dir"]).resolve() == (long_root / ".git").resolve()
    assert Path(payload["git_common_dir"]).resolve() == (long_root / ".git").resolve()
    assert Path(payload["git_work_tree"]).resolve() == long_root.resolve()
    assert payload["value"] == "MOCK_GIT_OUTPUT"


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_git_value_launches_real_job_with_over_260_c_root(tmp_path: Path) -> None:
    git_path = shutil.which("git.exe")
    if git_path is None:
        pytest.skip("Git for Windows unavailable")

    repository = tmp_path / "native-repo"
    subprocess.run(
        [git_path, "init", "--quiet", str(repository)],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        timeout=30,
        check=True,
    )
    repository = repository.resolve()
    (repository / "authority.txt").write_text("fixture authority\n", encoding="utf-8")
    _git(repository, "add", "authority.txt")
    _git(
        repository,
        "-c",
        "user.name=Dawnstrike Test",
        "-c",
        "user.email=dawnstrike-test@example.invalid",
        "commit",
        "--quiet",
        "-m",
        "fixture authority",
    )
    fixture_sha = _git(repository, "rev-parse", "HEAD")
    assert fixture_sha != _git(Path.cwd(), "rev-parse", "HEAD")
    lexical_root = str(repository)
    while len(lexical_root) <= 270:
        lexical_root += r"\."
    assert len(lexical_root) > 260

    activation = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace(
        "'", "''"
    )
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    git = git_path.replace("'", "''")
    root = lexical_root.replace("'", "''")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{activation}'
. '{runner}'
$head = Get-DawnstrikeGitValue `
    -GitPath '{git}' `
    -Root '{root}' `
    -Arguments @('rev-parse', 'HEAD') `
    -Label 'Native long Git root regression' `
    -TimeoutSeconds 30
[pscustomobject]@{{ root_length = '{root}'.Length; head = $head }} |
    ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=repository,
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["root_length"] > 260
    assert payload["head"] == fixture_sha


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_activation_source_admission_rejects_linked_worktree_pointer(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    _git(candidate, "init")
    linked_metadata = tmp_path / "linked-metadata"
    (candidate / ".git").rename(linked_metadata)
    (candidate / ".git").write_text(f"gitdir: {linked_metadata}\n", encoding="utf-8")
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    candidate_ps = str(candidate.resolve()).replace("'", "''")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{script}'
try {{
    $null = Assert-DawnstrikeActivationSourceAdmission `
        -CandidateRoot '{candidate_ps}' `
        -ExpectedSha '{CANDIDATE_SHA}'
    exit 9
}}
catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 23
}}
"""

    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=candidate,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )

    assert result.returncode == 23, (result.stdout, result.stderr)
    assert "self-contained clone" in result.stderr


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_activation_source_admission_rejects_commondir_created_after_config_lock(
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    relative_files = (
        ".gitattributes",
        "scripts/activate_dawnstrike_runtime.ps1",
        "scripts/dawnstrike_process_runner.ps1",
        "scripts/dawnstrike_job_process.ps1",
        "scripts/runtime_activation_lock.ps1",
        "scripts/runtime_activation_lock_contract.py",
        "scripts/runtime_operation_journal.py",
        "scripts/runtime_activation_contract.py",
        "scripts/vercel_publication_journal.py",
        "scripts/dawnstrike_python_bootstrap.py",
        "scripts/state_disaster_recovery.py",
        "scripts/capture_task_safety.ps1",
        "scripts/invoke_dawnstrike_stage.ps1",
    )
    for relative in relative_files:
        path = candidate / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("* text=auto\n" if relative == ".gitattributes" else "fixture\n")
    _git(candidate, "init", "--initial-branch=main")
    _git(candidate, "config", "user.email", "activation-test@example.invalid")
    _git(candidate, "config", "user.name", "Activation Test")
    _git(candidate, "add", ".")
    _git(candidate, "commit", "-m", "fixture")
    expected_sha = _git(candidate, "rev-parse", "HEAD")
    hostile_common = tmp_path / "hostile-common"
    shutil.copytree(candidate / ".git", hostile_common)
    hostile_config = hostile_common / "config"
    hostile_config.write_text(
        hostile_config.read_text(encoding="utf-8")
        + '\n[url "https://attacker.invalid/"]\n\tinsteadOf = https://github.com/\n',
        encoding="utf-8",
    )
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    candidate_ps = str(candidate.resolve()).replace("'", "''")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{script}'
$env:DAWNSTRIKE_TEST_ACTIVATION_METADATA_ABSENCE_RACE = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_CLOCK = '1'
try {{
    $held = Assert-DawnstrikeActivationSourceAdmission `
        -CandidateRoot '{candidate_ps}' `
        -ExpectedSha '{expected_sha}'
    foreach ($stream in @($held)) {{ $stream.Dispose() }}
    exit 9
}}
catch {{
    [Console]::Error.WriteLine($_.Exception.Message)
    exit 23
}}
"""
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=candidate,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdout is not None
    marker = process.stdout.readline().strip()
    if marker != "DAWNSTRIKE_TEST_METADATA_ABSENCE_GUARD_READY":
        stdout, stderr = process.communicate(timeout=10)
        pytest.fail(f"metadata absence race did not synchronize: {marker!r} {stdout!r} {stderr!r}")
    (candidate / ".git" / "commondir").write_text(str(hostile_common), encoding="utf-8")
    stdout, stderr = process.communicate(timeout=15)

    assert process.returncode == 23, (marker, stdout, stderr)
    assert "metadata absence changed before source admission" in stderr


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_fresh_activation_boundary_blocks_crossed_clock_before_caller_mutation() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
. '{script}'
$env:DAWNSTRIKE_TEST_ACTIVATION_CLOCK = '1'
$global:ObservedActivationClocks = @()
function Invoke-DawnstrikeActivationBoundary {{
    [CmdletBinding()]
    param(
        [string]$PythonPath,
        [string]$CandidateRoot,
        [string]$MarketDate,
        [string]$RuntimeRoot,
        [string]$StateRoot,
        [DateTimeOffset]$NowUtc,
        [int]$TimeoutSeconds
    )
    $global:ObservedActivationClocks += $NowUtc.ToUniversalTime().ToString('o')
    if ($NowUtc -ge [DateTimeOffset]::Parse('2026-09-03T13:00:00Z')) {{
        throw 'crossed activation window'
    }}
    [pscustomobject]@{{ status='PASS'; ready=$true }}
}}
$early = Invoke-DawnstrikeFreshActivationBoundary `
    -PythonPath 'unused' -CandidateRoot 'unused' -MarketDate '2026-09-03' `
    -RuntimeRoot 'unused' -StateRoot 'unused' -TimeoutSeconds 30 `
    -TestNowUtc '2026-09-03T12:59:00Z'
$protectedIntervalBlocked = $false
try {{
    $null = Invoke-DawnstrikeFreshActivationBoundary `
        -PythonPath 'unused' -CandidateRoot 'unused' -MarketDate '2026-09-03' `
        -RuntimeRoot 'unused' -StateRoot 'unused' -TimeoutSeconds 30 `
        -MinimumMorningLeadSeconds 120 `
        -TestNowUtc '2026-09-03T12:58:30Z'
}}
catch {{
    $protectedIntervalBlocked = $_.Exception.Message -match 'required Morning safety margin'
}}
$mutationCount = 0
$durableJournalWrites = 0
$blocked = $false
try {{
    # Model the durable PRE_SWAP transition between the earlier admission and
    # the final boundary immediately before the first runtime rename.
    $durableJournalWrites++
    $null = Invoke-DawnstrikeFreshActivationBoundary `
        -PythonPath 'unused' -CandidateRoot 'unused' -MarketDate '2026-09-03' `
        -RuntimeRoot 'unused' -StateRoot 'unused' -TimeoutSeconds 30 `
        -TestNowUtc '2026-09-03T13:00:00Z'
    $mutationCount++
}}
catch {{ $blocked = $_.Exception.Message -eq 'crossed activation window' }}
$delayedWorkerBlocked = $false
try {{
    $null = Invoke-DawnstrikeFreshActivationBoundary `
        -PythonPath 'unused' -CandidateRoot 'unused' -MarketDate '2026-09-03' `
        -RuntimeRoot 'unused' -StateRoot 'unused' -TimeoutSeconds 30 `
        -TestNowUtc '2026-09-03T12:59:00Z' `
        -TestCompletionNowUtc '2026-09-03T13:00:00Z'
}}
catch {{
    $delayedWorkerBlocked = $_.Exception.Message -match 'expired during validation'
}}
[pscustomobject]@{{
    early_status=$early.status
    blocked=$blocked
    durable_journal_writes=$durableJournalWrites
    mutation_count=$mutationCount
    delayed_worker_blocked=$delayedWorkerBlocked
    protected_interval_blocked=$protectedIntervalBlocked
    observed=@($global:ObservedActivationClocks)
}} | ConvertTo-Json -Depth 4 -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload == {
        "early_status": "PASS",
        "blocked": True,
        "durable_journal_writes": 1,
        "mutation_count": 0,
        "delayed_worker_blocked": True,
        "protected_interval_blocked": True,
        "observed": [
            "2026-09-03T12:59:00.0000000+00:00",
            "2026-09-03T12:58:30.0000000+00:00",
            "2026-09-03T13:00:00.0000000+00:00",
            "2026-09-03T12:59:00.0000000+00:00",
        ],
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
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "action_blocked": True,
        "trigger_blocked": True,
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_canonical_task_semantics_accepts_legacy_execute_only_before_exact_sha_rebind() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
. '{script}'
function Get-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName)
    $policy = Get-DawnstrikeCanonicalTaskPolicy $TaskName 'C:\runtime' 'C:\state'
    $type = if ($policy.weekly) {{ 'MSFT_TaskWeeklyTrigger' }} else {{ 'MSFT_TaskDailyTrigger' }}
    [pscustomobject]@{{
        State='Ready'; TaskPath='\'
        Actions=@([pscustomobject]@{{
            Execute='powershell.exe'; Arguments=$policy.arguments; WorkingDirectory='C:\runtime'
        }})
        Triggers=@([pscustomobject]@{{
            CimClass=[pscustomobject]@{{ CimClassName=$type }}; Enabled=$true
            DaysOfWeek=if($policy.weekly){{[int]$policy.days}}else{{$null}}
            WeeksInterval=if($policy.weekly){{1}}else{{$null}}
            DaysInterval=if($policy.weekly){{$null}}else{{1}}
            StartBoundary=("2026-09-01T" + [string]$policy.start + ":00-05:00")
            EndBoundary=$null; RandomDelay=$null
            Repetition=if($policy.monitor){{
                [pscustomobject]@{{Interval='PT5M';Duration='PT6H35M';StopAtDurationEnd=$true}}
            }}else{{[pscustomobject]@{{Interval='';Duration='';StopAtDurationEnd=$false}}}}
        }})
        Principal=[pscustomobject]@{{
            LogonType='Password'; # pragma: allowlist secret
            UserId='activation-test';RunLevel='Limited'
        }}
        Settings=[pscustomobject]@{{
            Enabled=$true;StartWhenAvailable=$true;WakeToRun=$true
            StopIfGoingOnBatteries=$false;DisallowStartIfOnBatteries=$false
            MultipleInstances='IgnoreNew';ExecutionTimeLimit=$policy.execution_limit
            RestartCount=$policy.restart_count;RestartInterval=$policy.restart_interval
            Hidden=$false;RunOnlyIfIdle=$false;RunOnlyIfNetworkAvailable=$false
            UseUnifiedSchedulingEngine=$true
        }}
    }}
}}
$defaultBlocked = $false
try {{
    $null = Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state'
}} catch {{ $defaultBlocked = $_.Exception.Message -match 'executable' }}
$legacyAccepted = $false
try {{
    $null = Assert-DawnstrikeCanonicalTaskSemantics `
        -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' -AllowLegacyExecutable
    $legacyAccepted = $true
}} catch {{}}
$exactBlocked = $false
try {{
    $null = Assert-DawnstrikeCanonicalTaskSemantics `
        -RuntimeRoot 'C:\runtime' -StateRoot 'C:\state' `
        -ExpectedSha ('a' * 40) -AllowLegacyExecutable
}} catch {{ $exactBlocked = $_.Exception.Message -match 'executable' }}
[pscustomobject]@{{
    default_blocked=$defaultBlocked
    legacy_accepted=$legacyAccepted
    exact_sha_blocked=$exactBlocked
}} |
    ConvertTo-Json -Compress
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
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "default_blocked": True,
        "legacy_accepted": True,
        "exact_sha_blocked": True,
    }


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_legacy_execute_admission_requires_protected_launcher() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
. '{script}'
$defaultPass = $false
try {{ Assert-DawnstrikeLegacyCanonicalExecuteAdmission; $defaultPass = $true }} catch {{}}
$directBlocked = $false
try {{
    Assert-DawnstrikeLegacyCanonicalExecuteAdmission `
        -AllowLegacyCanonicalExecute -PreflightOnly
}}
catch {{ $directBlocked = $_.Exception.Message -match 'protected release launcher' }}
$env:DAWNSTRIKE_TEST_LEGACY_CANONICAL_EXECUTE = '1'
$environmentBypassBlocked = $false
try {{
    Assert-DawnstrikeLegacyCanonicalExecuteAdmission `
        -AllowLegacyCanonicalExecute -PreflightOnly
}}
catch {{ $environmentBypassBlocked = $_.Exception.Message -match 'protected release launcher' }}
$script:DawnstrikeActivationCallerPath = `
    'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1'
function Assert-DawnstrikeNoReparseComponents {{ param([string]$Path, [string]$Label) }}
$protectedPreflightPass = $false
try {{
    Assert-DawnstrikeLegacyCanonicalExecuteAdmission -AllowLegacyCanonicalExecute -PreflightOnly
    $protectedPreflightPass = $true
}} catch {{}}
[pscustomobject]@{{
    default_pass=$defaultPass
    direct_blocked=$directBlocked
    environment_bypass_blocked=$environmentBypassBlocked
    protected_preflight_pass=$protectedPreflightPass
}} | ConvertTo-Json -Compress
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
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "default_pass": True,
        "direct_blocked": True,
        "environment_bypass_blocked": True,
        "protected_preflight_pass": True,
    }


def test_release_launcher_grants_legacy_normalization_only_after_exact_admission_and_elevation() -> (  # noqa: E501
    None
):
    launcher = Path("scripts/dawnstrike_release_launcher.ps1").read_text(encoding="utf-8")
    protected_path = launcher.index("Assert-DawnstrikeLauncherProtectedPath")
    candidate_admission = launcher.index("Assert-DawnstrikeLauncherCandidate")
    activate_branch = launcher.index("elseif ($Mode -eq 'Activate')")
    elevation = launcher.index(
        "Activate mode requires an elevated administrator process", activate_branch
    )
    invocation = launcher.index("& $entryLocks[0].path", activate_branch)
    legacy_switch = launcher.index("-AllowLegacyCanonicalExecute", invocation)
    assert (
        protected_path
        < candidate_admission
        < activate_branch
        < elevation
        < invocation
        < legacy_switch
    )
    assert launcher.count("-AllowLegacyCanonicalExecute") == 1


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_legacy_execute_set_rejects_mixed_state_and_accepts_all_or_none() -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    command = rf"""
. '{script}'
$allLegacy = @($script:DawnstrikeCanonicalTaskNames | ForEach-Object {{
    [pscustomobject]@{{name=$_;legacy_execute=$true}}
}})
$allPinned = @($script:DawnstrikeCanonicalTaskNames | ForEach-Object {{
    [pscustomobject]@{{name=$_;legacy_execute=$false}}
}})
$mixed = @($allLegacy | ForEach-Object {{
    [pscustomobject]@{{name=$_.name;legacy_execute=$_.legacy_execute}}
}})
$mixed[2].legacy_execute = $false
$allLegacyCount = @(Get-DawnstrikeLegacyCanonicalExecuteSet $allLegacy).Count
$allPinnedCount = @(Get-DawnstrikeLegacyCanonicalExecuteSet $allPinned).Count
$mixedBlocked = $false
try {{ $null = Get-DawnstrikeLegacyCanonicalExecuteSet $mixed }}
catch {{ $mixedBlocked = $_.Exception.Message -match 'mixed pinned/legacy' }}
[pscustomobject]@{{
    all_legacy_count=$allLegacyCount
    all_pinned_count=$allPinnedCount
    mixed_blocked=$mixedBlocked
}} | ConvertTo-Json -Compress
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
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "all_legacy_count": 5,
        "all_pinned_count": 0,
        "mixed_blocked": True,
    }


def test_pre_quiesce_recovery_restores_exact_scheduler_xml_not_candidate_sha() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    recovery = activation.split('if ([string]$journal.payload.phase -eq "PRE_QUIESCE")', 1)[
        1
    ].split("$prepared = Invoke-DawnstrikeContractCli", 1)[0]
    compensation = activation.split(
        "function Invoke-DawnstrikeActivationCompensationStateMachine", 1
    )[1].split("function Get-DawnstrikeActivation", 1)[0]
    assert "$completeExpiredRecoveryCompensation" in recovery
    assert "Restore-DawnstrikeCanonicalTasksFromXmlBackup" in compensation
    assert "-EnableAfterRestore:$enableRestoredTasks" in compensation
    assert "Set-DawnstrikeCanonicalTaskExpectedSha" not in compensation


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_canonical_task_action_mutations_recover_strict_partial_sets_and_preserve_xml(
    tmp_path: Path,
) -> None:
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    state = str(tmp_path / "state").replace("'", "''")
    command = rf"""
. '{script}'
$global:MockStates = @{{}}
$global:MockActions = @{{}}
$global:SetCallCount = 0
$global:FailOnCall = 0
$global:MockPrincipal = 'activation-test'
$global:MockActionContext = 'Author'
$global:MockRegistrationAuthor = 'Dawnstrike'
$global:ManifestCreateCount = 0
$global:FailManifestOnCall = 0
$runtime = 'C:\runtime'
$state = '{state}'
New-Item -ItemType Directory -Path $state -Force | Out-Null
function New-DawnstrikeScheduledLaunchManifest {{
    [CmdletBinding()] param(
        [string]$RuntimeRoot,[string]$StateRoot,[string]$ExpectedSha,[string]$TaskScript
    )
    $global:ManifestCreateCount += 1
    if ($global:FailManifestOnCall -eq $global:ManifestCreateCount) {{
        throw 'simulated abrupt manifest preparation stop'
    }}
    $root = Join-Path $StateRoot 'receipts\scheduler-launch'
    New-Item -ItemType Directory -Path $root -Force | Out-Null
    $path = Join-Path $root ($ExpectedSha + '-' + $TaskScript + '.json')
    [IO.File]::WriteAllText(
        $path,
        ('{{"schema_version":"dawnstrike.scheduled_launch_manifest.v1","release_sha":"' +
            $ExpectedSha + '","task_script":"' + $TaskScript +
            '","research_only":true,"broker_execution_enabled":false}}'),
        [Text.UTF8Encoding]::new($false)
    )
    [pscustomobject]@{{path=$path;sha256=(Get-DawnstrikeSha256File $path)}}
}}
function Assert-DawnstrikeScheduledLaunchManifest {{
    [CmdletBinding()] param(
        [string]$RuntimeRoot,[string]$StateRoot,[string]$ExpectedSha,[string]$TaskScript,
        [string]$ManifestPath,[string]$ManifestSha256,[string]$EntryScript=''
    )
    if ((Get-DawnstrikeSha256File $ManifestPath) -cne $ManifestSha256) {{
        throw 'mock launch manifest hash mismatch'
    }}
    $payload = Get-Content -LiteralPath $ManifestPath -Raw | ConvertFrom-Json
    if (
        [string]$payload.release_sha -cne $ExpectedSha -or
        [string]$payload.task_script -cne $TaskScript
    ) {{
        throw 'mock launch manifest identity mismatch'
    }}
    [pscustomobject]@{{manifest=$payload;locks=@()}}
}}
function Get-DawnstrikeScheduledLaunchCommand {{
    [CmdletBinding()] param(
        [string]$Runner,[string]$RuntimeRoot,[string]$StateRoot,[string]$ExpectedSha,
        [string]$ManifestPath,[string]$ManifestSha256,[string]$PublicationMode='',
        [string]$VercelProjectId=''
    )
    (
        "& '$Runner' -RuntimeRoot '$RuntimeRoot' -StateRoot '$StateRoot' " +
        "-ExpectedSha '$ExpectedSha' -LaunchManifestPath '$ManifestPath' " +
        "-LaunchManifestSha256 '$ManifestSha256'"
    )
}}
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{
    $policy = Get-DawnstrikeCanonicalTaskPolicy $name $runtime $state
    $global:MockStates[$name] = 'Ready'
    $global:MockActions[$name] = [pscustomobject]@{{
        Execute='powershell.exe'; Arguments=$policy.arguments; WorkingDirectory=$runtime
    }}
}}
function Get-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName)
    if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ return @() }}
    [pscustomobject]@{{
        State=$global:MockStates[$TaskName]; TaskPath='\';
        Actions=@($global:MockActions[$TaskName])
    }}
}}
function Export-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $action = $global:MockActions[$TaskName]
    $enabled = if ($global:MockStates[$TaskName] -eq 'Disabled') {{ 'false' }} else {{ 'true' }}
    $safeCommand = [Security.SecurityElement]::Escape([string]$action.Execute)
    $safeArguments = [Security.SecurityElement]::Escape([string]$action.Arguments)
    $safeWorking = [Security.SecurityElement]::Escape([string]$action.WorkingDirectory)
    (
        "<Task version='1.4'><RegistrationInfo>" +
        "<Author>$global:MockRegistrationAuthor</Author></RegistrationInfo>" +
        "<Data>research-only</Data><Principals><Principal>" +
        "<UserId>$global:MockPrincipal</UserId><LogonType>Password</LogonType>" +
        "<RunLevel>LeastPrivilege</RunLevel></Principal></Principals>" +
        "<Triggers><CalendarTrigger>" +
        "<StartBoundary>2026-09-01T08:00:00-05:00</StartBoundary>" +
        "</CalendarTrigger></Triggers><Settings><Enabled>$enabled</Enabled>" +
        "<ExecutionTimeLimit>PT2H</ExecutionTimeLimit></Settings>" +
        "<Actions Context='$global:MockActionContext'><Exec>" +
        "<Command>$safeCommand</Command><Arguments>$safeArguments</Arguments>" +
        "<WorkingDirectory>$safeWorking</WorkingDirectory>" +
        "</Exec></Actions></Task>"
    )
}}
function New-ScheduledTaskAction {{
    [CmdletBinding()] param([string]$Execute,[string]$Argument,[string]$WorkingDirectory)
    [pscustomobject]@{{Execute=$Execute;Arguments=$Argument;WorkingDirectory=$WorkingDirectory}}
}}
function Set-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath,$Action)
    $global:SetCallCount += 1
    $global:MockActions[$TaskName] = @($Action)[0]
    if ($global:FailOnCall -eq $global:SetCallCount) {{
        throw 'simulated abrupt task mutation stop'
    }}
    [pscustomobject]@{{TaskName=$TaskName}}
}}
function Enable-ScheduledTask {{
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $global:MockStates[$TaskName] = 'Ready'
    [pscustomobject]@{{TaskName=$TaskName}}
}}
$contract = Get-DawnstrikeTaskContract $runtime $state
$backup = New-DawnstrikeTaskXmlBackup `
    -StateRoot $state -BackupName ('runtime-activation-' + ('a' * 24)) `
    -ActivationId ('a' * 24) -TaskContract $contract `
    -AuxiliaryCapture ([pscustomobject]@{{present=$false}})
$originalArguments = @{{}}
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{
    $originalArguments[$name] = [string]$global:MockActions[$name].Arguments
    $global:MockStates[$name] = 'Disabled'
}}
$global:FailOnCall = 3
$normalizationFailed = $false
try {{
    $null = Set-DawnstrikeCanonicalTaskPinnedExecutable `
        -RuntimeRoot $runtime -StateRoot $state -BackupName $backup.backup_name `
        -ExpectedManifestSha256 $backup.manifest_sha256
}} catch {{ $normalizationFailed = $_.Exception.Message -match 'simulated abrupt' }}
$partialNormalize = @(Get-DawnstrikeCanonicalTaskActionMutationSet `
    -Mode PIN_EXECUTABLE -RuntimeRoot $runtime -StateRoot $state `
    -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256)
$global:FailOnCall = 0
$global:SetCallCount = 0
$restored = Restore-DawnstrikeCanonicalTasksFromXmlBackup `
    -RuntimeRoot $runtime -StateRoot $state -BackupName $backup.backup_name `
    -ExpectedManifestSha256 $backup.manifest_sha256 `
    -ExpectedTaskContractSha256 $contract.task_contract_sha256 `
    -ExpectedTaskDefinitionContractSha256 $contract.task_definition_contract_sha256 `
    -ExpectedTaskActionContractSha256 $contract.task_action_contract_sha256 `
    -EnableAfterRestore
$restoredLegacyCount = @($script:DawnstrikeCanonicalTaskNames | Where-Object {{
    [string]$global:MockActions[$_].Execute -ceq 'powershell.exe'
}}).Count
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{ $global:MockStates[$name] = 'Disabled' }}
$normalized = Set-DawnstrikeCanonicalTaskPinnedExecutable `
    -RuntimeRoot $runtime -StateRoot $state -BackupName $backup.backup_name `
    -ExpectedManifestSha256 $backup.manifest_sha256
$argumentsPreserved = @($script:DawnstrikeCanonicalTaskNames | Where-Object {{
    [string]$global:MockActions[$_].Arguments -cne [string]$originalArguments[$_]
}}).Count -eq 0
$global:MockPrincipal = 'hostile-principal-drift'
$principalDriftBlocked = $false
try {{
    $null = Get-DawnstrikeCanonicalTaskActionMutationSet `
        -Mode PIN_EXECUTABLE -RuntimeRoot $runtime -StateRoot $state `
        -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256
}} catch {{ $principalDriftBlocked = $_.Exception.Message -match 'XML outside' }}
$global:MockPrincipal = 'activation-test'
$global:MockActionContext = 'HostileContext'
$actionShapeDriftBlocked = $false
try {{
    $null = Get-DawnstrikeCanonicalTaskActionMutationSet `
        -Mode PIN_EXECUTABLE -RuntimeRoot $runtime -StateRoot $state `
        -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256
}} catch {{ $actionShapeDriftBlocked = $_.Exception.Message -match 'XML outside' }}
$global:MockActionContext = 'Author'
$global:MockRegistrationAuthor = 'hostile-registration-drift'
$registrationDriftBlocked = $false
try {{
    $null = Get-DawnstrikeCanonicalTaskActionMutationSet `
        -Mode PIN_EXECUTABLE -RuntimeRoot $runtime -StateRoot $state `
        -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256
}} catch {{ $registrationDriftBlocked = $_.Exception.Message -match 'XML outside' }}
$global:MockRegistrationAuthor = 'Dawnstrike'
$targetSha = 'b' * 40
$global:SetCallCount = 0
$global:ManifestCreateCount = 0
$global:FailManifestOnCall = 3
$manifestPreparationFailed = $false
try {{
    $null = Set-DawnstrikeCanonicalTaskExpectedSha `
        -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $targetSha `
        -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256
}} catch {{ $manifestPreparationFailed = $_.Exception.Message -match 'simulated abrupt manifest' }}
$manifestFailureChangedNoActions = $global:SetCallCount -eq 0
$global:FailManifestOnCall = 0
$global:ManifestCreateCount = 0
$global:FailOnCall = 2
$rebindFailed = $false
try {{
    $null = Set-DawnstrikeCanonicalTaskExpectedSha `
        -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $targetSha `
        -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256
}} catch {{ $rebindFailed = $_.Exception.Message -match 'simulated abrupt' }}
$partialRebind = @(Get-DawnstrikeCanonicalTaskActionMutationSet `
    -Mode BIND_EXPECTED_SHA -RuntimeRoot $runtime -StateRoot $state `
    -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256 `
    -ExpectedSha $targetSha)
$global:FailOnCall = 0
$global:SetCallCount = 0
$null = Set-DawnstrikeCanonicalTaskExpectedSha `
    -RuntimeRoot $runtime -StateRoot $state -ExpectedSha $targetSha `
    -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256
$rebound = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled
$finalSet = @(Get-DawnstrikeCanonicalTaskActionMutationSet `
    -Mode BIND_EXPECTED_SHA -RuntimeRoot $runtime -StateRoot $state `
    -BackupName $backup.backup_name -ExpectedManifestSha256 $backup.manifest_sha256 `
    -ExpectedSha $targetSha)
$guardedTargets = @($script:DawnstrikeCanonicalTaskNames | Where-Object {{
    $runner = Get-DawnstrikeCanonicalTaskRunnerName $_
    $manifestPath = Join-Path $state (
        'receipts\scheduler-launch\' + $targetSha + '-' + $runner + '.json'
    )
    $manifestHash = Get-DawnstrikeSha256File $manifestPath
    $arguments = [string]$global:MockActions[$_].Arguments
    -not ($arguments -cmatch '^-NoProfile -ExecutionPolicy Bypass -Command ' -and
        $arguments.Contains($manifestPath) -and $arguments.Contains($manifestHash))
}}).Count -eq 0
[pscustomobject]@{{
    normalization_failed=$normalizationFailed
    partial_normalize_baseline=@($partialNormalize | Where-Object state -eq 'BASELINE').Count
    partial_normalize_target=@($partialNormalize | Where-Object state -eq 'TARGET').Count
    restored_ready=$restored.enabled_count
    restored_legacy=$restoredLegacyCount
    normalized_disabled=$normalized.disabled_count
    arguments_preserved=$argumentsPreserved
    principal_drift_blocked=$principalDriftBlocked
    action_shape_drift_blocked=$actionShapeDriftBlocked
    registration_drift_blocked=$registrationDriftBlocked
    manifest_preparation_failed=$manifestPreparationFailed
    manifest_failure_changed_no_actions=$manifestFailureChangedNoActions
    rebind_failed=$rebindFailed
    partial_rebind_baseline=@($partialRebind | Where-Object state -eq 'BASELINE').Count
    partial_rebind_target=@($partialRebind | Where-Object state -eq 'TARGET').Count
    rebound_disabled=$rebound.disabled_count
    final_target=@($finalSet | Where-Object state -eq 'TARGET').Count
    guarded_targets=$guardedTargets
}} | ConvertTo-Json -Compress
"""
    result = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=90,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "normalization_failed": True,
        "partial_normalize_baseline": 2,
        "partial_normalize_target": 3,
        "restored_ready": 5,
        "restored_legacy": 5,
        "normalized_disabled": 5,
        "arguments_preserved": True,
        "principal_drift_blocked": True,
        "action_shape_drift_blocked": True,
        "registration_drift_blocked": True,
        "manifest_preparation_failed": True,
        "manifest_failure_changed_no_actions": True,
        "rebind_failed": True,
        "partial_rebind_baseline": 3,
        "partial_rebind_target": 2,
        "rebound_disabled": 5,
        "final_target": 5,
        "guarded_targets": True,
    }


def test_activation_recovery_admission_precedes_strict_ready_task_admission() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    journal_probe = activation.index("$recoveryTaskAdmission = $false")
    strict_tasks = activation.index("$taskBefore = Get-DawnstrikeTaskContract $runtime $state")
    recovery_status = activation.index('status = "RECOVERY_REQUIRED"')
    recovery_branch = activation.index('if ([string]$journal.payload.phase -eq "PRE_QUIESCE")')
    assert journal_probe < strict_tasks < recovery_status < recovery_branch
    assert "$taskBefore = Get-DawnstrikeTaskContract $runtime $state -AllowDisabled" in activation
    assert '"PRE_SWAP", "POST_SWAP", "POST_SWAP_READY"' in activation
    assert "Get-DawnstrikeCanonicalTaskActionMutationSet" in activation


def test_post_swap_journal_is_durable_rebind_intent_before_any_task_update() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    post_swap = activation.index("-Operation runtime_activation -Phase POST_SWAP")
    rebind = activation.index(
        "Set-DawnstrikeCanonicalTaskExpectedSha",
        post_swap,
    )
    ready = activation.index(
        "-Operation runtime_activation -Phase POST_SWAP_READY",
        rebind,
    )
    assert post_swap < rebind < ready
    recovery = activation.split('if ([string]$journal.payload.phase -eq "POST_SWAP")', 1)[1].split(
        'if ([string]$journal.payload.phase -eq "PRE_SWAP")', 1
    )[0]
    assert "Get-DawnstrikeCanonicalTaskActionMutationSet" in recovery
    assert "BIND_EXPECTED_SHA" in recovery


def test_post_swap_rebind_requires_complete_guarded_manifest_set() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    post_swap = activation.index("-Operation runtime_activation -Phase POST_SWAP")
    create_set = activation.index(
        "$canonicalLaunchManifestSet = Get-DawnstrikeCanonicalLaunchManifestSet",
        post_swap,
    )
    rebind = activation.index("Set-DawnstrikeCanonicalTaskExpectedSha", create_set)
    ready = activation.index("-Operation runtime_activation -Phase POST_SWAP_READY", rebind)
    assert post_swap < create_set < rebind < ready
    assert "-LaunchManifestSet $canonicalLaunchManifestSet" in activation[rebind:ready]
    assert "-LaunchManifestPath ([string]$binding.path)" in activation
    assert "-LaunchManifestSha256 ([string]$binding.sha256)" in activation
    assert (
        'throw "Canonical task SHA binding requires a complete validated launch manifest set."'
        in activation
    )


def test_first_rename_bootstrap_recovery_precedes_runtime_git_admission() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    helper = activation.index("function Get-DawnstrikeActivationBootstrapRecovery")
    entry = activation.index("function Invoke-DawnstrikeRuntimeActivation", helper)
    bootstrap_call = activation.index(
        "$runtimeBootstrapRecovery = Get-DawnstrikeActivationBootstrapRecovery",
        entry,
    )
    runtime_read = activation.index(
        "$runtimeContract = Get-DawnstrikeGitContract $gitPath $runtime",
        entry,
    )
    assert helper < entry < runtime_read < bootstrap_call
    admission = activation[runtime_read : activation.index("$dbPath =", runtime_read)]
    assert "$runtimeContract = Get-DawnstrikeGitContract" in admission
    assert "if (-not $runtimePresentAtBootstrap -or" in admission
    assert (
        "previousRuntimeIdentityRoot = [string]$runtimeBootstrapRecovery.previous_root" in admission
    )
    bootstrap = activation[helper:entry]
    assert '$missingPhase -eq "PRE_SWAP"' in bootstrap
    assert '$missingPhase -in @("POST_SWAP", "POST_SWAP_READY")' in bootstrap
    assert "-not $runtimePresent" in bootstrap
    assert "Test-DawnstrikeRuntimeLockOwnerDead" in bootstrap
    assert "Runtime activation bootstrap journal does not bind the exact runtime lock." in bootstrap
    assert (
        "Missing runtime is admissible only at an exact activation compensation boundary."
        in bootstrap
    )


def test_vercel_publication_cutover_boundary_is_source_bound_and_precedes_both_swaps() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    helper = activation.index("function Assert-DawnstrikeVercelPublicationCutoverBoundary")
    entry = activation.index("function Invoke-DawnstrikeRuntimeActivation", helper)
    helper_source = activation[helper:entry]
    assert "$journalHelper, 'verify-history'" in helper_source
    assert "'--require-no-lock'" in helper_source
    assert "'--project-id', 'prj_5pef3EZF1u5YadebEz3dFjnkWOXy'" in helper_source
    assert "'--production-alias'" in helper_source
    assert "publish_vercel_public.ps1" not in helper_source
    assert activation.count("'scripts/vercel_publication_journal.py'") == 1
    assert activation.count('"scripts/vercel_publication_journal.py"') == 1

    recovery_swap = activation.index("[IO.Directory]::Move($runtime,$rollbackCheckout)", entry)
    recovery_barrier = activation.rindex(
        "Assert-DawnstrikeVercelPublicationCutoverBoundary", entry, recovery_swap
    )
    fresh_locked = activation.index("$lockedPublicationBoundary =", recovery_swap)
    task_backup = activation.index("$taskBackup = New-DawnstrikeTaskXmlBackup", fresh_locked)
    fresh_final = activation.index("$finalPublicationBoundary =", task_backup)
    pre_swap = activation.index("-Operation runtime_activation -Phase PRE_SWAP", fresh_final)
    fresh_swap = activation.index(
        "[System.IO.Directory]::Move($runtime, $rollbackCheckout)", pre_swap
    )
    assert helper < entry < recovery_barrier < recovery_swap < fresh_locked < task_backup
    assert task_backup < fresh_final < pre_swap < fresh_swap
    assert (
        "Vercel publication journal history changed after the locked cutover admission."
        in activation[fresh_final:pre_swap]
    )


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_vercel_publication_cutover_boundary_blocks_valid_nonterminal_malformed_and_active_lock(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    history = state / "outputs" / "daily_finalize" / "vercel-publication"
    journal_path = history / "2026-08-31" / "vercel-publication-operation.json"
    _write_vercel_pre_mutation_journal(journal_path)

    activation_copy = tmp_path / "activate_dawnstrike_runtime.ps1"
    production_python = r"C:\Program Files\Dawnstrike\Python313\python.exe"
    activation_copy.write_text(
        Path("scripts/activate_dawnstrike_runtime.ps1")
        .read_text(encoding="utf-8")
        .replace(production_python, str(Path(sys.executable).resolve())),
        encoding="utf-8",
    )
    activation = str(activation_copy).replace("'", "''")
    runner = str(Path("scripts/dawnstrike_job_process.ps1").resolve()).replace("'", "''")
    candidate = str(Path.cwd()).replace("'", "''")
    state_text = str(state).replace("'", "''")
    python = str(Path(sys.executable).resolve()).replace("'", "''")

    def invoke_boundary() -> dict[str, object]:
        command = rf"""
. '{runner}'
. '{activation}'
function Invoke-DawnstrikeActivationProcess {{
    param($FilePath,$ArgumentList,$WorkingDirectory,$Label,$TimeoutSeconds)
    $output = & $FilePath @ArgumentList 2>$null
    if ($LASTEXITCODE -ne 0) {{ throw "$Label failed with exit code $LASTEXITCODE." }}
    [pscustomobject]@{{Stdout=($output -join "`n");ExitCode=0}}
}}
try {{
    $proof = Assert-DawnstrikeVercelPublicationCutoverBoundary `
        -PythonPath '{python}' -CandidateRoot '{candidate}' `
        -StateRoot '{state_text}' -TimeoutSeconds 60
    [pscustomobject]@{{passed=$true;message='';journal_count=$proof.journal_count}} |
        ConvertTo-Json -Compress
}}
catch {{
    [pscustomobject]@{{passed=$false;message=$_.Exception.Message;journal_count=-1}} |
        ConvertTo-Json -Compress
}}
"""
        result = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
            cwd=Path.cwd(),
            text=True,
            capture_output=True,
            timeout=90,
            check=False,
        )
        assert result.returncode == 0, (result.stdout, result.stderr)
        return json.loads(result.stdout.strip().splitlines()[-1])

    valid_nonterminal = invoke_boundary()
    assert valid_nonterminal["passed"] is False
    assert str(valid_nonterminal["message"])

    journal_path.write_text("{}", encoding="utf-8")
    malformed = invoke_boundary()
    assert malformed["passed"] is False
    assert str(malformed["message"])

    journal_path.unlink()
    (history / "vercel-publication-operation.lock").write_text("active", encoding="utf-8")
    active_lock = invoke_boundary()
    assert active_lock["passed"] is False
    assert str(active_lock["message"])

    (history / "vercel-publication-operation.lock").unlink()
    clear = invoke_boundary()
    assert clear == {"passed": True, "message": "", "journal_count": 0}


def test_action_mutation_invariant_hashes_the_entire_task_xml() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    proof = activation.split("function Get-DawnstrikeCanonicalTaskXmlMutationProof", 1)[1].split(
        "function Get-DawnstrikeCanonicalTaskBackupMutationMap", 1
    )[0]
    assert "invariant_sha256 = Get-DawnstrikeSha256Text ([string]$document.OuterXml)" in proof
    assert "$null = $nodes[0].RemoveChild($enabled[0])" in proof
    assert '$command[0].InnerText = "__DAWNSTRIKE_COMMAND__"' in proof
    assert '$arguments[0].InnerText = "__DAWNSTRIKE_ARGUMENTS__"' in proof
    assert '$working[0].InnerText = "__DAWNSTRIKE_WORKING_DIRECTORY__"' in proof
    invariant = activation.split("function Assert-DawnstrikeCanonicalTaskNonActionInvariant", 1)[
        1
    ].split("function Test-DawnstrikeCanonicalTaskExactAction", 1)[0]
    assert "$Actual.invariant_sha256" in invariant


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_git_contract_rejects_combined_hidden_index_flags(tmp_path: Path) -> None:
    checkout = tmp_path / "checkout"
    checkout.mkdir()
    subprocess.run(
        ["git", "init", "--initial-branch=main", str(checkout)],
        check=True,
        capture_output=True,
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
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
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
    (root / ".gitattributes").write_bytes(
        (Path.cwd() / ".gitattributes").read_bytes().replace(b"\r\n", b"\n")
    )
    shutil.copy2(Path.cwd() / "requirements.lock", root / "requirements.lock")
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
            "-I",
            "-B",
            "-S",
            str(bootstrap),
            "--release-root",
            str(root),
            "--expected-sha",
            expected_sha,
            "--module",
            "intraday_scanner.probe",
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
        check=True,
        capture_output=True,
    )
    _git(checkout, "config", "user.email", "activation-test@example.invalid")
    _git(checkout, "config", "user.name", "Activation Test")
    _git(checkout, "add", ".")
    _git(checkout, "commit", "-m", "declaration fixture")
    return checkout, _git(checkout, "rev-parse", "HEAD"), _git(checkout, "rev-parse", "HEAD^{tree}")


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
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
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
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
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
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {"blocked": True, "mutated": True}


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
        cwd=Path.cwd(),
        text=True,
        capture_output=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, (result.stdout, result.stderr)
    assert json.loads(result.stdout.strip().splitlines()[-1]) == {
        "required": False,
        "present": False,
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
                "triggering_actor": {"id": activation_contract._GITHUB_RELEASE_ACTOR_ID},
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


def test_owner_authorization_accepts_current_codex_share_url() -> None:
    sol = _sol_payload()
    sol["codex_share_url"] = "https://chatgpt.com/s/cx_test-owner-report"

    body = activation_contract._owner_authorization_body(
        sol, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
    )

    assert json.loads(body)["codex_share_url"] == sol["codex_share_url"]


@pytest.mark.parametrize(
    "url",
    (
        "https://chatgpt.com/s/not-a-codex-share",
        "https://chatgpt.com/s/cx_report/extra",
        "https://example.com/s/cx_report",
    ),
)
def test_owner_authorization_rejects_unrecognized_share_url(url: str) -> None:
    sol = _sol_payload()
    sol["codex_share_url"] = url

    with pytest.raises(ActivationContractError, match="not immutable"):
        activation_contract._owner_authorization_body(
            sol, candidate_sha=CANDIDATE_SHA, candidate_tree=CANDIDATE_TREE
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
            return (
                [
                    {
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
                ],
                "4" * 64,
            )
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


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_activation_receipt_guard_blocks_transient_same_metadata_substitution(
    tmp_path: Path,
) -> None:
    """One held receipt handle denies mutation during verify and journal commit."""

    receipt = tmp_path / "held-receipt.json"
    original = b'{"proof":"trusted-exact-bytes"}'
    hostile = b'{"proof":"hostile-exact-bytes"}'
    assert len(hostile) == len(original)
    receipt.write_bytes(original)
    original_stat = receipt.stat()
    original_hash = hashlib.sha256(original).hexdigest()
    script = str(Path("scripts/activate_dawnstrike_runtime.ps1").resolve()).replace("'", "''")
    receipt_ps = str(receipt.resolve()).replace("'", "''")
    command = rf"""
$ErrorActionPreference = 'Stop'
. '{script}'
function Invoke-DawnstrikeContractCli {{
    [CmdletBinding()]
    param(
        [string]$PythonPath,
        [string]$CandidateRoot,
        [string[]]$Arguments,
        [string]$Label,
        [int]$TimeoutSeconds
    )
    $receiptIndex = [Array]::IndexOf($Arguments, '--receipt')
    $receiptPath = [string]$Arguments[$receiptIndex + 1]
    [Console]::Out.WriteLine('VERIFY_HANDLE_HELD')
    [Console]::Out.Flush()
    $null = [Console]::In.ReadLine()
    [pscustomobject]@{{ receipt_sha256 = Get-DawnstrikeSha256File $receiptPath }}
}}
$guard = Open-DawnstrikeActivationReceiptGuard `
    -Path '{receipt_ps}' -ExpectedStatus PREPARED `
    -PythonPath 'mock-python' -ToolRoot 'mock-root' -TimeoutSeconds 30
[Console]::Out.WriteLine('JOURNAL_TRANSITION_HANDLE_HELD')
[Console]::Out.Flush()
$null = [Console]::In.ReadLine()
$null = Confirm-DawnstrikeActivationReceiptGuard $guard
$result = [pscustomobject]@{{
    hash = [string]$guard.sha256
    length = [long]$guard.length
}}
Close-DawnstrikeActivationReceiptGuard $guard
$result | ConvertTo-Json -Compress
"""
    process = subprocess.Popen(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", command],
        cwd=Path.cwd(),
        text=True,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert process.stdin is not None
    assert process.stdout is not None

    def assert_locked_substitution_is_denied(label: str) -> None:
        replacement = tmp_path / f"{label}-replacement.json"
        replacement.write_bytes(hostile)
        os.utime(
            replacement,
            ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns),
        )
        with pytest.raises(OSError):
            receipt.write_bytes(hostile)
        with pytest.raises(OSError):
            os.replace(replacement, receipt)
        with pytest.raises(OSError):
            receipt.unlink()
        assert receipt.read_bytes() == original
        assert receipt.stat().st_size == original_stat.st_size
        assert receipt.stat().st_mtime_ns == original_stat.st_mtime_ns

    assert process.stdout.readline().strip() == "VERIFY_HANDLE_HELD"
    assert_locked_substitution_is_denied("verify")
    process.stdin.write("continue\n")
    process.stdin.flush()
    assert process.stdout.readline().strip() == "JOURNAL_TRANSITION_HANDLE_HELD"
    assert_locked_substitution_is_denied("journal")
    process.stdin.write("continue\n")
    process.stdin.flush()
    stdout, stderr = process.communicate(timeout=30)
    assert process.returncode == 0, (stdout, stderr)
    result = json.loads(stdout.strip().splitlines()[-1])
    assert result == {"hash": original_hash, "length": len(original)}


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
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")

    seam = rollback.index('DAWNSTRIKE_TEST_ROLLBACK_THROW_POINT -eq "during_compensation"')
    seam_guard = rollback.index('DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1"', seam)
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
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")

    assert "$attemptKey = [string]$journalBefore.raw_file_sha256" in activation
    assert "runtime-activation-$ActivationId.compensated-$attemptKey.json" in activation
    assert "runtime-activation-$ActivationId.failed-$attemptKey.json" in activation
    assert "recovery-quarantine\\compensated-$activationId-$compensationAttemptKey" in activation
    assert (
        "compensated-$activationId-$compensationAttemptKey-$preparedHash.prepared.json"
        in activation
    )
    assert "$preparedHash = if (" in activation
    assert "-PreparedReceiptSha256 $preparedHash" in activation
    assert "-PreparedReceiptSha256 (if (" not in activation
    assert "runtime-activation-$activationId.compensated.json" not in activation

    assert "$compensationAttemptKey = [string]$journalBefore.raw_file_sha256" in rollback
    assert "runtime-rollback-$activationId.compensated-$compensationAttemptKey.json" in rollback
    assert "runtime-rollback-$activationId.failed-$compensationAttemptKey.json" in rollback
    assert "runtime-rollback-$activationId.compensated.json" not in rollback
    assert "$failedAttemptKey = [string]$failedAttemptJournal.raw_file_sha256" in rollback
    assert '"failed-previous-runtime-$failedAttemptKey"' in rollback
    assert 'Join-Path $rollbackRoot "failed-previous-runtime"' not in rollback


def test_rebind_post_enable_compensation_uses_defined_receipt_path() -> None:
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8")
    recovery_start = rebind.index(
        'elseif ([string]$preExistingJournal.payload.phase -eq "POST_ENABLE"'
    )
    recovery_end = rebind.index("Clear-DawnstrikeCompensatedJournalTombstone", recovery_start)
    recovery = rebind[recovery_start:recovery_end]
    assert (
        "capture-task-rebind-$([string]$preExistingJournal.raw_file_sha256).compensated.json"
        in recovery
    )
    assert "-CompensationReceiptRelativePath $compensationReceiptRelativePath" in recovery
    assert "$compensationReceiptRelative " not in recovery
    assert "capture-task-rebind-$([string]$journalBefore.raw_file_sha256).failed.json" in rebind
    assert 'capture-task-rebind-" + $CandidateSha + ".failed.json' not in rebind


def test_rollback_compensated_recovery_binds_origin_before_lock_adoption() -> None:
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    start = rollback.index('if ([string]$compensatedJournal.payload.phase -eq "COMPENSATED")')
    end = rollback.index("return Invoke-DawnstrikeRuntimeRollback", start)
    recovery = rollback[start:end]
    origin_read = recovery.index("$compensationOrigin = Get-DawnstrikeGitValue")
    origin_hash = recovery.index("Get-DawnstrikeSha256Text $compensationOrigin", origin_read)
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
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8")

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
        "$compensationPayload.task_action_contract_sha256 -ne $restoredStart.action_contract_sha256"
    ) in rebind
    assert (
        "$compensationPayload.task_definition_contract_sha256 -ne "
        "$restoredStart.definition_contract_sha256"
    ) in rebind


def test_rollback_compensation_uses_powershell_51_relative_path_logic() -> None:
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    assert "[System.IO.Path]::GetRelativePath" not in rollback
    assert "$receiptFullPath.Substring($statePrefix.Length)" in rollback
    assert "Rollback activation receipt is outside StateRoot." in rollback


def test_hardening_journal_tracks_live_runtime_as_current_and_previous() -> None:
    hardening = Path("scripts/harden_intraday_capture_task.ps1").read_text(encoding="utf-8")
    assert "CurrentSha = $CandidateSha" not in hardening
    assert "CurrentTree = $CandidateTree" not in hardening
    assert "-CurrentSha $CandidateSha -CurrentTree $CandidateTree" not in hardening
    assert hardening.count("CurrentSha = $runtimeIdentity.head") == 2
    assert hardening.count("CurrentTree = $runtimeIdentity.tree") == 2
    assert (
        hardening.count("-CurrentSha $runtimeIdentity.head -CurrentTree $runtimeIdentity.tree") == 3
    )


def test_origin_advance_allows_only_exact_lock_bound_recovery() -> None:
    lock = Path("scripts/runtime_activation_lock.ps1").read_text(encoding="utf-8")
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    hardening = Path("scripts/harden_intraday_capture_task.ps1").read_text(encoding="utf-8")
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8")

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
    assert "-Operation runtime_activation" in activation[activation_gate:activation_evidence]
    assert "merge-base" in activation[activation_gate:activation_evidence]

    deferred = hardening.index("-RefreshOrigin -DeferOriginMainAdmission")
    hardening_gate = hardening.index(
        "if ($script:HardeningRemoteMain -cne $CandidateSha)", deferred
    )
    assert "-Operation capture_task_hardening" in hardening[hardening_gate : hardening_gate + 1200]
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
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    scripts = [
        activation,
        Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8"),
        Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8"),
        Path("scripts/harden_intraday_capture_task.ps1").read_text(encoding="utf-8"),
    ]
    state_preparation = Path("scripts/prepare_dawnstrike_state.ps1").read_text(encoding="utf-8")

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

    assert "Get-Command py.exe" not in state_preparation
    assert "Get-Command git.exe" not in state_preparation
    assert "Assert-DawnstrikeStatePreparationBootstrapSource" in state_preparation
    assert (
        '. (Join-Path $statePreparationToolRoot "scripts\\activate_dawnstrike_runtime.ps1")'
        in state_preparation
    )

    process_start = activation.index("function Invoke-DawnstrikeActivationProcess")
    process_end = activation.index("function Get-DawnstrikeActivationNowUtc", process_start)
    process = activation[process_start:process_end]
    assert "$effectiveArguments = @('-I', '-B') + $effectiveArguments" in process
    assert "-ArgumentList $effectiveArguments" in process


def test_state_preparation_strictly_classifies_hash_bound_lock_archives() -> None:
    script = Path("scripts/prepare_dawnstrike_state.ps1").read_text(encoding="utf-8")
    assert "^recovered-stale-([0-9a-f]{64})\\.lock$" in script
    assert "^dawnstrike-daily-(\\d{4}-\\d{2}-\\d{2})\\.lock\\.stale-dead-([0-9a-f]{64})$" in script
    assert "Get-DawnstrikeStrictRuntimeLock" in script
    assert "Get-DawnstrikeLockSnapshot" in script
    assert "Test-DawnstrikeLockOwnerActive" in script
    assert "$locks += $lockItem" in script


def test_activation_seals_init_before_first_stage_filesystem_mutation() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    boundary = activation.index("# INIT and its exact runtime lock must exist before clone")
    lock = activation.index(
        "$activationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal",
        boundary,
    )
    stage_directory_crash = activation.index(
        'if ($TestStageCrashPoint -eq "after_stage_directory")', lock
    )
    clone = activation.index('-Label "Candidate runtime staging"', lock)
    checkout_crash = activation.index('if ($TestStageCrashPoint -eq "after_stage_checkout")', clone)
    daily = activation.index("$dailyLock = Enter-DawnstrikeDailyRunLock", checkout_crash)
    assert lock < stage_directory_crash < clone < checkout_crash < daily
    assert "DAWNSTRIKE_TEST_ACTIVATION_STAGE_CRASH" in activation
    assert "INIT recovery could not quarantine the exact staged path" in activation
    assert "Staging failure journal identity is invalid" in activation


def test_activation_refreshes_clock_at_each_normal_mutation_boundary() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    entry = activation.index("function Invoke-DawnstrikeRuntimeActivation")
    body = activation[entry:]
    assert "$activationNowUtc" not in body
    assert body.count("Invoke-DawnstrikeFreshActivationBoundary") == 11
    assert "Activation fresh-clock override is test-only" in body

    init_lock = body.index("$activationLock = Enter-DawnstrikeGovernedRuntimeLockWithJournal")
    pre_init_recheck = body.rindex("$null = Invoke-DawnstrikeFreshActivationBoundary", 0, init_lock)
    daily_lock = body.index("$dailyLock = Enter-DawnstrikeDailyRunLock", init_lock)
    locked_recheck = body.index("$null = Invoke-DawnstrikeFreshActivationBoundary", daily_lock)
    task_backup = body.index("$taskBackup = New-DawnstrikeTaskXmlBackup", locked_recheck)
    pre_quiesce = body.index("-Operation runtime_activation -Phase PRE_QUIESCE", task_backup)
    task_recheck = body.index("$null = Invoke-DawnstrikeFreshActivationBoundary", pre_quiesce)
    task_disable = body.index("Disable-DawnstrikeCanonicalTasks", task_recheck)
    publication_recheck = body.index("$finalPublicationBoundary =", task_disable)
    swap_recheck = body.index(
        "$null = Invoke-DawnstrikeFreshActivationBoundary", publication_recheck
    )
    pre_swap = body.index("-Operation runtime_activation -Phase PRE_SWAP", swap_recheck)
    durable_pre_swap = body.index('$journalPhase = "PRE_SWAP"', pre_swap)
    final_swap_recheck = body.index(
        "$null = Invoke-DawnstrikeFreshActivationBoundary", durable_pre_swap
    )
    swap_started = body.index("$swapStarted = $true", final_swap_recheck)
    first_rename = body.index(
        "[System.IO.Directory]::Move($runtime, $rollbackCheckout)", swap_started
    )
    second_rename = body.index("[System.IO.Directory]::Move($stage, $runtime)", first_rename)

    assert pre_init_recheck < init_lock < daily_lock < locked_recheck < task_backup
    pre_init_region = body[pre_init_recheck:init_lock]
    assert "Assert-DawnstrikePostFinalizerMutationWindow" in pre_init_region
    assert pre_quiesce < task_recheck < task_disable
    assert (
        publication_recheck
        < swap_recheck
        < pre_swap
        < durable_pre_swap
        < final_swap_recheck
        < swap_started
        < first_rename
        < second_rename
    )
    between_renames = body[first_rename:second_rename]
    assert "Invoke-DawnstrikeFreshActivationBoundary" not in between_renames
    assert "installing C immediately" in between_renames

    normal_ready = body.rindex('$journalPhase = "POST_SWAP_READY"')
    normal_enable_recheck = body.index(
        "$null = Invoke-DawnstrikeFreshActivationBoundary", normal_ready
    )
    normal_enable = body.index("Enable-DawnstrikeCanonicalTasks", normal_enable_recheck)
    assert normal_ready < normal_enable_recheck < normal_enable


def test_every_runtime_lock_adoption_or_creation_rechecks_post_finalizer_window() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    entry = activation.index("function Invoke-DawnstrikeRuntimeActivation")
    body = activation[entry:]
    calls = list(
        re.finditer(
            r"(?m)^\s*\$[A-Za-z][A-Za-z0-9]*\s*=\s*"
            r"(?:Adopt|Enter)-DawnstrikeGovernedRuntimeLockWithJournal\b",
            body,
        )
    )
    assert len(calls) == 6
    for call in calls:
        prior_boundary = body.rfind("Assert-DawnstrikePostFinalizerMutationWindow", 0, call.start())
        prior_adopt = body.rfind("Adopt-DawnstrikeGovernedRuntimeLockWithJournal", 0, call.start())
        prior_enter = body.rfind("Enter-DawnstrikeGovernedRuntimeLockWithJournal", 0, call.start())
        assert prior_boundary > max(prior_adopt, prior_enter)


def test_activation_preflight_reports_terminal_compensation_cleanup_required() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    classification = activation.index("$recoveryTaskAdmission = $recoveryTaskAdmissionPhase")
    preflight = activation.index("if ($PreflightOnly -and $recoveryTaskAdmission)", classification)
    classified = activation[classification:preflight]
    recovery_result = activation[preflight : activation.index("if ($PreflightOnly)", preflight)]
    assert '"COMPENSATED"' in classified
    assert 'status = "RECOVERY_REQUIRED"' in recovery_result


def test_activation_recovery_never_uses_an_earlier_clock_to_cut_over_or_enable() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    recovery = activation.index("$completeExpiredRecoveryCompensation = {")
    recovery_end = activation.index(
        "# Any strict recovery failure must retain the adopted runtime lock", recovery
    )
    region = activation[recovery:recovery_end]

    pre_swap = region.index('if ([string]$journal.payload.phase -eq "PRE_SWAP")')
    publication = region.index("Assert-DawnstrikeVercelPublicationCutoverBoundary", pre_swap)
    fresh = region.index("$null = Invoke-DawnstrikeFreshActivationBoundary", publication)
    expired = region.index("$completeExpiredRecoveryCompensation", fresh)
    first_rename = region.index("[IO.Directory]::Move($runtime,$rollbackCheckout)", expired)
    second_rename = region.index("[IO.Directory]::Move($stage,$runtime)", first_rename)
    assert publication < fresh < expired < first_rename < second_rename

    post_swap_ready = region.index(
        'elseif ([string]$journal.payload.phase -eq "POST_SWAP_READY")', second_rename
    )
    disable = region.index("Set-DawnstrikeTasksFailClosedDisabled", post_swap_ready)
    ready_fresh = region.index("$null = Invoke-DawnstrikeFreshActivationBoundary", disable)
    ready_expired = region.index("$completeExpiredRecoveryCompensation", ready_fresh)
    ready_enable = region.index("Enable-DawnstrikeCanonicalTasks", ready_expired)
    assert post_swap_ready < disable < ready_fresh < ready_expired < ready_enable

    recovered_ready = region.index('$journalPhase = "POST_SWAP_READY"', ready_enable)
    recovered_fresh = region.index(
        "$null = Invoke-DawnstrikeFreshActivationBoundary", recovered_ready
    )
    recovered_expired = region.index(
        "$completeExpiredRecoveryCompensation", recovered_fresh
    )
    recovered_enable = region.index("Enable-DawnstrikeCanonicalTasks", recovered_expired)
    assert recovered_ready < recovered_fresh < recovered_expired < recovered_enable

    compensation_start = activation.index(
        "function Invoke-DawnstrikeActivationCompensationStateMachine"
    )
    compensation_end = activation.index(
        "function Invoke-DawnstrikeContractCli", compensation_start
    )
    compensation = activation[compensation_start:compensation_end]
    for filesystem_state in (
        "OLD_RUNTIME_INTACT",
        "AFTER_FIRST_RENAME",
        "AFTER_SECOND_RENAME",
    ):
        assert f'{{ "{filesystem_state}" }}' in compensation
    intent = compensation.index("Write-DawnstrikeActivationJsonExact $intentPayload $intentPath")
    assert "[IO.Directory]::Move($RuntimeRoot, $failedCandidate)" in compensation
    assert "[IO.Directory]::Move($RollbackCheckout, $RuntimeRoot)" in compensation
    stage_preserve = compensation.index("[IO.Directory]::Move($Stage, $failedCandidate)")
    shape_proof = compensation.index(
        "Activation compensation did not reach its exact filesystem boundary",
        stage_preserve,
    )
    candidate_proof = compensation.index("$preservedCandidate = Get-DawnstrikeGitContract")
    terminal = compensation.index(
        "-Operation runtime_activation -Phase COMPENSATED", candidate_proof
    )
    assert intent < stage_preserve < shape_proof < candidate_proof < terminal
    assert "Restore-DawnstrikeCanonicalTasksFromXmlBackup" in compensation
    assert "Restore-DawnstrikeAuxiliaryCaptureTask" in compensation
    assert 'status = "RECOVERED_EXPIRED_COMPENSATED"' in compensation
    assert "recovered_filesystem_state = [string]$intent.origin_filesystem_state" in compensation

    daily = compensation.index("$terminalDaily = Enter-DawnstrikeDailyRunLock", terminal)
    handshake = compensation.index("Confirm-DawnstrikeActivationDailyLockHandshake", daily)
    daily_release = compensation.index("Exit-DawnstrikeDailyRunLock $terminalDaily", handshake)
    daily_absent = compensation.index(
        "Activation compensation did not release its exact daily lock", daily_release
    )
    activation_release = compensation.index(
        "Exit-DawnstrikeGovernedRuntimeLock $ActivationLock", daily_absent
    )
    activation_absent = compensation.index(
        "Activation compensation did not release its exact runtime lock",
        activation_release,
    )
    assert (
        terminal
        < daily
        < handshake
        < daily_release
        < daily_absent
        < activation_release
        < activation_absent
    )


def test_activation_source_admission_revalidates_locked_metadata_bytes() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    start = activation.index("function Assert-DawnstrikeActivationSourceAdmission")
    end = activation.index("if ($InjectCrashBetweenRuntimeRenames", start)
    admission = activation[start:end]
    initial_validation = admission.index("$localConfig =")
    open_handle = admission.index("$metadataStream = [IO.File]::Open", initial_validation)
    exact_compare = admission.index("[Convert]::ToBase64String", open_handle)
    git_read = admission.index("rev-parse HEAD", exact_compare)
    assert initial_validation < open_handle < exact_compare < git_read
    assert "requires a self-contained clone" in admission
    assert "Git metadata absence changed before source admission" in admission
    assert "$safeEnvironment.GIT_COMMON_DIR = $gitMetadata" in admission
    assert "rejected a locked local Git execution/filter configuration" in admission
    assert "requires the Git worktree config extension disabled" in admission
    assert "extensions.worktreeConfig=false" in admission
    process = activation[
        activation.index("function Invoke-DawnstrikeActivationProcess") : activation.index(
            "function Get-DawnstrikeActivationNowUtc"
        )
    ]
    assert "extensions.worktreeConfig=false" in process
    assert "-ceq '-C'" in process
    assert "-eq '-C'" not in process
    assert "$environment.GIT_COMMON_DIR = $boundGitDirectory" in process


def test_activation_recovery_teardown_keeps_journal_until_locks_are_released() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")

    init = activation.index('if ([string]$journal.payload.phase -eq "INIT")')
    pre_quiesce = activation.index('if ([string]$journal.payload.phase -eq "PRE_QUIESCE")', init)
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

    pre_swap = activation.index('if ([string]$journal.payload.phase -eq "PRE_SWAP")', pre_quiesce)
    pre_quiesce_compensation = activation.index(
        "& $completeExpiredRecoveryCompensation", pre_quiesce, pre_swap
    )
    assert pre_quiesce < pre_quiesce_compensation < pre_swap

    compensation = activation.index("function Invoke-DawnstrikeActivationCompensationStateMachine")
    compensation_end = activation.index("function ", compensation + 10)
    compensated = activation.index("-Phase COMPENSATED", compensation, compensation_end)
    daily_release = activation.index(
        "Exit-DawnstrikeDailyRunLock $terminalDaily", compensated, compensation_end
    )
    activation_release = activation.index(
        "Exit-DawnstrikeGovernedRuntimeLock $ActivationLock",
        daily_release,
        compensation_end,
    )
    assert compensated < daily_release < activation_release
    assert "Recovery tombstone owner is still active" in activation
    assert "Recovery tombstone changed during validation" in activation


def test_activation_pre_swap_recovers_exact_post_second_rename_state() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")

    recovery = activation.index('if ([string]$journal.payload.phase -eq "PRE_SWAP")')
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
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
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
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    receipt = rollback.index('"Rollback ready receipt sealing"')
    ready = rollback.index("-Operation runtime_rollback -Phase POST_SWAP_READY", receipt)
    enable = rollback.index("        Enable-DawnstrikeCanonicalTasks", ready)
    terminal_receipt = rollback.index('"Rollback terminal receipt sealing"', enable)
    complete = rollback.index("-Operation runtime_rollback -Phase COMPLETE", terminal_receipt)
    assert receipt < ready < enable < terminal_receipt < complete
    assert 'schema_version = "dawnstrike.runtime_rollback_receipt.v2"' in rollback
    assert 'schema_version = "dawnstrike.runtime_rollback_receipt.v1"' in rollback
    assert "Restore-DawnstrikeCanonicalTasksFromXmlBackup" in rollback
    assert "-BackupName ([string]$activation.scheduler_backup_name)" in rollback
    assert "Get-DawnstrikeTaskXmlBackupManifest" in rollback
    assert "previous-SHA Ready boundary" in rollback
    assert 'journalPhase -in @("POST_SWAP", "POST_SWAP_READY")' in rollback
    assert 'DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_ready"' in rollback
    assert 'DAWNSTRIKE_TEST_ROLLBACK_CRASH_POINT -eq "after_enable"' in rollback


def test_legacy_activation_compensation_uses_sealed_backup_actions() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rollback = Path("scripts/rollback_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    activation_start = activation.index("$restoredRuntime = Get-DawnstrikeGitContract")
    activation_compensation = activation[
        activation_start : activation.index(
            "$journalBefore = Get-DawnstrikeStrictRuntimeOperationJournal",
            activation_start,
        )
    ]
    assert "Restore-DawnstrikeCanonicalTasksFromXmlBackup" in activation_compensation
    assert "-EnableAfterRestore" in activation_compensation
    assert "Set-DawnstrikeCanonicalTaskExpectedSha" not in activation_compensation
    assert "Assert-DawnstrikeCanonicalTaskSemantics" not in activation_compensation
    assert "exact sealed XML backup is the compatibility authority for P" in activation_compensation
    rollback_restore = rollback[
        rollback.index("# The restored runtime must be disabled and rebound") : rollback.index(
            'if ($journalPhase -eq "PRE_SWAP")',
            rollback.index("# The restored runtime must be disabled and rebound"),
        )
    ]
    assert "Restore-DawnstrikeCanonicalTasksFromXmlBackup" in rollback_restore
    assert "[string]$activation.scheduler_backup_name" in rollback_restore
    assert "-ExpectedSha $previousSha" not in rollback_restore
    assert "Principal" in activation and "Triggers" in activation and "Settings" in activation


def test_lock_adoption_recovers_a_replace_completed_before_process_death() -> None:
    lock = Path("scripts/runtime_activation_lock.ps1").read_text(encoding="utf-8")
    recovered_round = lock.index(
        "elseif($current.raw_file_sha256-eq[string]$payload.next_lock_file_sha256)"
    )
    resumable_round = lock.index("$needsNewRound=$true", recovered_round)
    new_temp = lock.index("$nextName='.next-runtime-lock-'", resumable_round)
    reseal = lock.index("'ADOPTION_PREPARED'", recovered_round)
    owner_guard = lock.index("process_started_at_utc-ne$ownerStart", recovered_round)
    assert recovered_round < resumable_round < new_temp < reseal < owner_guard
    assert "New-DawnstrikeRetainedRuntimeLockObject $path" in lock


def test_complete_activation_retry_reconciles_only_exact_owned_locks() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")

    complete = activation.index("$existingBackupManifest = Get-DawnstrikeTaskXmlBackupManifest")
    artifact_proof = activation.index("$null = Assert-DawnstrikeReceiptRecoveryArtifacts", complete)
    lock_branch = activation.index(
        "if (Test-Path -LiteralPath $completeRuntimeLockPath -PathType Leaf)",
        artifact_proof,
    )
    adopt = activation.index(
        "$completeLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal",
        lock_branch,
    )
    daily = activation.index("$completeDailyLock = Enter-DawnstrikeDailyRunLock", adopt)
    handshake = activation.index("Confirm-DawnstrikeActivationDailyLockHandshake", daily)
    daily_release = activation.index("Exit-DawnstrikeDailyRunLock $completeDailyLock", handshake)
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
        "Complete activation retry found a daily lock without its exact runtime lock" in activation
    )
    assert 'if ($TestStageCrashPoint -eq "after_complete_journal")' in activation
    assert "Stop-Process -Id $PID -Force" in activation


def test_installed_candidate_complete_retry_releases_stranded_lock_pair() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    installed = activation.index("if ($runtimeContract.head -eq $ExpectedSha)")
    next_preflight = activation.index("$dbPath = Join-Path $state", installed)
    installed_branch = activation[installed:next_preflight]

    journal = installed_branch.index("$earlyJournal = Get-DawnstrikeStrictRuntimeOperationJournal")
    complete = installed_branch.index('[string]$earlyJournal.payload.phase -ne "COMPLETE"', journal)
    foreign_guard = installed_branch.index(
        "Existing COMPLETE activation has a foreign or multiple daily lock set",
        complete,
    )
    adopt = installed_branch.index(
        "$earlyLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal",
        foreign_guard,
    )
    handshake = installed_branch.index("Confirm-DawnstrikeActivationDailyLockHandshake", adopt)
    daily_release = installed_branch.index("Exit-DawnstrikeDailyRunLock $earlyDaily", handshake)
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


def test_activation_nonterminal_failure_compensates_or_preserves_adoptable_lock_pair() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")

    pre_quiesce = activation.index("-Operation runtime_activation -Phase PRE_QUIESCE")
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
    compensation = activation.index(
        "Invoke-DawnstrikeActivationCompensationStateMachine", preserve
    )
    compensation_failure = activation.index(
        "Runtime activation durable compensation did not reach its terminal proof", compensation
    )
    preserve_assignment = activation.rindex(
        "$preserveLocks = $true", compensation, compensation_failure
    )
    finally_block = activation.index("finally {", preserve)
    finally_guard = activation.index("if (-not $preserveLocks)", finally_block)
    assert (
        failure_reconcile
        < preserve
        < compensation
        < preserve_assignment
        < compensation_failure
        < finally_block
        < finally_guard
    )
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

    assert "$activationLock = $null" in activation[compensation:compensation_failure]
    assert "$dailyLock = $null" in activation[compensation:compensation_failure]


def test_production_entrypoint_fault_injection_switches_are_environment_guarded() -> None:
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    rebind = Path("scripts/rebind_intraday_capture_task.ps1").read_text(encoding="utf-8")

    activation_guard = (
        '$InjectCrashBetweenRuntimeRenames -and $env:DAWNSTRIKE_TEST_LOCK_JOURNAL -ne "1"'
    )
    rebind_guard = (
        "($InjectFailureAfterMutation -or $InjectCrashAfterEnable) -and\n"
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
    activation = Path("scripts/activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")

    staging_cleanup = activation.index("if (-not $activationBodyStarted -and -not $preserveLocks")
    scheduler_backup = activation.index(
        "if (Test-Path -LiteralPath $schedulerBackupPath)", staging_cleanup
    )
    quarantine = activation.index(
        "Move-Item -LiteralPath $schedulerBackupPath -Destination",
        scheduler_backup,
    )
    release = activation.index("Exit-DawnstrikeGovernedRuntimeLock $activationLock", quarantine)
    remove_journal = activation.index("Remove-Item -LiteralPath $operationJournal -Force", release)
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


def _prepare_disposable_activation_recovery_fixture(tmp_path: Path) -> dict[str, object]:
    """Build a complete, local-only activation fixture for crash recovery tests."""

    source = Path.cwd()
    fixture_root = tmp_path
    if os.name == "nt":
        # The governed production roots keep the deepest compensation quarantine
        # comfortably below MAX_PATH (181 characters at the installed StateRoot).
        # Pytest's descriptive per-test directory can add more than 30 characters
        # and push the disposable Git checkout past Git for Windows' own path cap,
        # so use an isolated short sibling while retaining pytest's temp ownership.
        fixture_token = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:12]
        fixture_root = tmp_path.parent / f"ds-{fixture_token}"
        fixture_root.mkdir()
    candidate = fixture_root / "candidate"
    runtime = fixture_root / "dawnstrike-runtime"
    state = fixture_root / "state"
    backup = fixture_root / "backups"
    remote = fixture_root / "origin.git"
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
        "state_root_boundary.ps1",
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
        "refresh_luna_core_universe.py",
        "publish_vercel_public.ps1",
        "vercel_source_contract.ps1",
        "vercel_toolchain_contract.py",
        "vercel_publication_journal.py",
        "publication_boundary.py",
        "verify_daily_prepublication.py",
        "build_vercel_public_stage.ps1",
        "verify_vercel_candidate.ps1",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    candidate_process_runner = candidate / "scripts" / "dawnstrike_process_runner.ps1"
    process_runner_source = candidate_process_runner.read_text(encoding="utf-8")
    luna_fixture_anchor = (
        "    $releaseRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\\')\n"
    )
    assert process_runner_source.count(luna_fixture_anchor) == 1
    candidate_process_runner.write_text(
        process_runner_source.replace(
            luna_fixture_anchor,
            "    if ($env:DAWNSTRIKE_TEST_ACTIVATION_LUNA_MANIFEST_FIXTURE -eq '1') {\n"
            "        return @(\n"
            "            'scripts/refresh_luna_core_universe.py',\n"
            "            'intraday_scanner/__init__.py'\n"
            "        )\n"
            "    }\n"
            + luna_fixture_anchor,
            1,
        ),
        encoding="utf-8",
    )
    candidate_activation = candidate / "scripts" / "activate_dawnstrike_runtime.ps1"
    direct_python_anchor = """    if ([string]::Equals(
            [System.IO.Path]::GetFullPath($FilePath),
            $approvedPythonPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )) {"""
    direct_python_fixture = """    if (
        $env:DAWNSTRIKE_TEST_DIRECT_PYTHON_FIXTURE -ne "1" -and
        [string]::Equals(
            [System.IO.Path]::GetFullPath($FilePath),
            $approvedPythonPath,
            [System.StringComparison]::OrdinalIgnoreCase
        )
    ) {"""
    candidate_activation_source = candidate_activation.read_text(encoding="utf-8")
    assert candidate_activation_source.count(direct_python_anchor) == 1
    candidate_activation.write_text(
        candidate_activation_source.replace(
            direct_python_anchor,
            direct_python_fixture,
            1,
        ),
        encoding="utf-8",
    )
    _install_local_origin_fixture_seam(candidate / "scripts" / "runtime_activation_lock.ps1")
    _install_local_interpreter_fixture_seam(candidate)
    _install_local_bootstrap_origin_fixture_seam(
        candidate / "scripts" / "dawnstrike_python_bootstrap.py", origin=remote
    )
    _install_local_github_ci_fixture_seam(candidate / "scripts" / "runtime_activation_contract.py")
    shutil.copytree(
        source / "intraday_scanner",
        candidate / "intraday_scanner",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    for name in (".gitignore", ".gitattributes", "pyproject.toml", "requirements.lock"):
        shutil.copy2(source / name, candidate / name)
    candidate_bootstrap = candidate / "scripts" / "dawnstrike_python_bootstrap.py"
    candidate_bootstrap.write_text(
        candidate_bootstrap.read_text(encoding="utf-8").replace(
            PRODUCTION_RECORD_SET_SHA256,
            _host_dependency_record_contract(source / "requirements.lock"),
            1,
        ),
        encoding="utf-8",
    )

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
    previous_tree = _git(runtime, "rev-parse", "HEAD^{tree}")

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
    return {
        "source": source,
        "candidate": candidate,
        "runtime": runtime,
        "state": state,
        "backup": backup,
        "ci": ci,
        "sol": sol,
        "candidate_sha": candidate_sha,
        "candidate_tree": candidate_tree,
        "previous_sha": previous_sha,
        "previous_tree": previous_tree,
        "db": db,
        "db_hash_before": db_hash_before,
        "ci_completed_at": ci_payload["completed_at_utc"],
        "owner_comment_body": owner_comment_body,
    }


def _persistent_disposable_scheduler_mock(
    *,
    runtime: Path,
    state: Path,
    persistence_path: Path,
    phase: str,
    boundary_date: str = "2026-08-30",
) -> str:
    """Return process-persistent scheduled-task mocks for hard-crash tests."""

    template = r"""
$global:MockRuntime = '__RUNTIME__'
$global:MockState = '__STATE__'
$global:MockPersistencePath = '__PERSISTENCE__'
$global:MockPhase = '__PHASE__'
$global:MockTaskStates = @{}
$global:MockTaskExpectedSha = @{}
$global:MockTaskActions = @{}
$global:TaskEvents = @()

function Save-MockScheduler {
    $states = [ordered]@{}
    $expected = [ordered]@{}
    $actions = [ordered]@{}
    foreach ($name in $script:DawnstrikeCanonicalTaskNames) {
        $states[$name] = [string]$global:MockTaskStates[$name]
        if ($global:MockTaskExpectedSha.ContainsKey($name)) {
            $expected[$name] = [string]$global:MockTaskExpectedSha[$name]
        }
        if ($global:MockTaskActions.ContainsKey($name)) {
            $action = $global:MockTaskActions[$name]
            $actions[$name] = [ordered]@{
                Execute = [string]$action.Execute
                Arguments = [string]$action.Arguments
                WorkingDirectory = [string]$action.WorkingDirectory
            }
        }
    }
    $payload = [ordered]@{
        states = $states
        expected_sha = $expected
        actions = $actions
        events = @($global:TaskEvents)
    }
    [IO.File]::WriteAllText(
        $global:MockPersistencePath,
        ($payload | ConvertTo-Json -Depth 12 -Compress),
        [Text.UTF8Encoding]::new($false)
    )
}

if (Test-Path -LiteralPath $global:MockPersistencePath -PathType Leaf) {
    $saved = Get-Content -LiteralPath $global:MockPersistencePath -Raw -Encoding UTF8 |
        ConvertFrom-Json
    foreach ($property in $saved.states.PSObject.Properties) {
        $global:MockTaskStates[$property.Name] = [string]$property.Value
    }
    foreach ($property in $saved.expected_sha.PSObject.Properties) {
        $global:MockTaskExpectedSha[$property.Name] = [string]$property.Value
    }
    foreach ($property in $saved.actions.PSObject.Properties) {
        $global:MockTaskActions[$property.Name] = [pscustomobject]@{
            Execute = [string]$property.Value.Execute
            Arguments = [string]$property.Value.Arguments
            WorkingDirectory = [string]$property.Value.WorkingDirectory
        }
    }
    $global:TaskEvents = @($saved.events)
}
else {
    foreach ($name in $script:DawnstrikeCanonicalTaskNames) {
        $global:MockTaskStates[$name] = 'Ready'
    }
    Save-MockScheduler
}

function Get-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName)
    if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') { return @() }
    $boundSha = [string]$global:MockTaskExpectedSha[$TaskName]
    $hasPersistedAction = $global:MockTaskActions.ContainsKey($TaskName)
    # A restored task's sealed action is authoritative.  Derive only the
    # schedule shape from the legacy policy so this test double does not try
    # to reconstruct an older runtime's protected launcher command.
    $policy = if ($hasPersistedAction) {
        Get-DawnstrikeCanonicalTaskPolicy $TaskName $global:MockRuntime $global:MockState
    }
    elseif ($boundSha) {
        Get-DawnstrikeCanonicalTaskPolicy `
            $TaskName $global:MockRuntime $global:MockState $boundSha
    }
    else {
        Get-DawnstrikeCanonicalTaskPolicy $TaskName $global:MockRuntime $global:MockState
    }
    $currentAction = if ($hasPersistedAction) {
        $global:MockTaskActions[$TaskName]
    }
    else {
        [pscustomobject]@{
            Execute = $script:DawnstrikePowerShellExecutable
            Arguments = $policy.arguments
            WorkingDirectory = $global:MockRuntime
        }
    }
    $triggerType = if ($policy.weekly) { 'MSFT_TaskWeeklyTrigger' } else { 'MSFT_TaskDailyTrigger' }
    $dayOfWeek = if ($policy.weekly) { [int]$policy.days } else { $null }
    $weekInterval = if ($policy.weekly) { 1 } else { $null }
    $dayInterval = if ($policy.weekly) { $null } else { 1 }
    $repetition = if ($policy.monitor) {
        [pscustomobject]@{ Interval='PT5M'; Duration='PT6H35M'; StopAtDurationEnd=$true }
    }
    else {
        [pscustomobject]@{ Interval=''; Duration=''; StopAtDurationEnd=$false }
    }
    [pscustomobject]@{
        State = $global:MockTaskStates[$TaskName]
        TaskPath = '\'
        Actions = @($currentAction)
        Triggers = @([pscustomobject]@{
            CimClass = [pscustomobject]@{ CimClassName=$triggerType }
            Enabled = $true
            DaysOfWeek = $dayOfWeek
            WeeksInterval = $weekInterval
            DaysInterval = $dayInterval
            StartBoundary = ('2026-08-31T' + $policy.start + ':00-05:00')
            EndBoundary = $null
            RandomDelay = $null
            Repetition = $repetition
        })
        Principal = [pscustomobject]@{
            LogonType='Password'; UserId='activation-test'; RunLevel='Limited'
        }
        Settings = [pscustomobject]@{
            Enabled = ($global:MockTaskStates[$TaskName] -eq 'Ready')
            StartWhenAvailable = $true
            WakeToRun = $true
            StopIfGoingOnBatteries = $false
            DisallowStartIfOnBatteries = $false
            MultipleInstances = 'IgnoreNew'
            ExecutionTimeLimit = $policy.execution_limit
            RestartCount = $policy.restart_count
            RestartInterval = $policy.restart_interval
            Hidden = $false
            RunOnlyIfIdle = $false
            RunOnlyIfNetworkAvailable = $false
            UseUnifiedSchedulingEngine = $true
        }
    }
}

function Get-ScheduledTaskInfo {
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $lastRun = switch ($TaskName) {
        'Dawnstrike AlphaOps EOD Full Report' { [DateTime]'__BOUNDARY_DATE__T15:15:00' }
        'Dawnstrike 10of10 Daily Finalize' { [DateTime]'__BOUNDARY_DATE__T17:30:00' }
        'Dawnstrike AlphaOps V6 Weekly Training' { [DateTime]'__BOUNDARY_DATE__T21:00:00' }
        default { [DateTime]'__BOUNDARY_DATE__T08:00:00' }
    }
    [pscustomobject]@{
        LastRunTime = $lastRun
        NextRunTime = if ($TaskName -eq 'Dawnstrike AlphaOps V6 Weekly Training') {
            [DateTime]'__WEEKLY_NEXT_DATE__T21:00:00'
        }
        else { [DateTime]'__NEXT_DATE__T08:00:00' }
    }
}

function Export-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $task = Get-ScheduledTask -TaskName $TaskName
    $action = @($task.Actions)[0]
    $enabled = if ($global:MockTaskStates[$TaskName] -eq 'Disabled') { 'false' } else { 'true' }
    $safeName = [Security.SecurityElement]::Escape([string]$TaskName)
    $safeCommand = [Security.SecurityElement]::Escape([string]$action.Execute)
    $safeArguments = [Security.SecurityElement]::Escape([string]$action.Arguments)
    $safeWorking = [Security.SecurityElement]::Escape([string]$action.WorkingDirectory)
    "<Task><Name>$safeName</Name><Principal><UserId>activation-test</UserId><LogonType>Password</LogonType></Principal><Triggers><CalendarTrigger><StartBoundary>2026-08-31T08:00:00-05:00</StartBoundary></CalendarTrigger></Triggers><Settings><Enabled>$enabled</Enabled><ExecutionTimeLimit>PT2H</ExecutionTimeLimit></Settings><Actions><Exec><Command>$safeCommand</Command><Arguments>$safeArguments</Arguments><WorkingDirectory>$safeWorking</WorkingDirectory></Exec></Actions></Task>"
}

function Disable-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $global:MockTaskStates[$TaskName] = 'Disabled'
    $global:TaskEvents += [pscustomobject]@{
        phase=$global:MockPhase; operation='disable'; task=$TaskName; sha='NONE'
    }
    Save-MockScheduler
    [pscustomobject]@{ TaskName=$TaskName }
}

function Enable-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
    $global:MockTaskStates[$TaskName] = 'Ready'
    $boundSha = [string]$global:MockTaskExpectedSha[$TaskName]
    if ([string]::IsNullOrWhiteSpace($boundSha)) { $boundSha = 'UNBOUND_PRIOR' }
    $global:TaskEvents += [pscustomobject]@{
        phase=$global:MockPhase; operation='enable'; task=$TaskName; sha=$boundSha
    }
    Save-MockScheduler
    [pscustomobject]@{ TaskName=$TaskName }
}

function New-ScheduledTaskAction {
    [CmdletBinding()] param([string]$Execute,[string]$Argument,[string]$WorkingDirectory)
    [pscustomobject]@{ Execute=$Execute; Arguments=$Argument; WorkingDirectory=$WorkingDirectory }
}

function Set-ScheduledTask {
    [CmdletBinding()] param([string]$TaskName,[string]$TaskPath,$Action)
    $global:MockTaskActions[$TaskName] = @($Action)[0]
    $match = [regex]::Match(
        [string](@($Action)[0].Arguments),
        '-ExpectedSha\s+["'']?([0-9a-f]{40})'
    )
    if ($match.Success) {
        $global:MockTaskExpectedSha[$TaskName] = $match.Groups[1].Value
    }
    else {
        $null = $global:MockTaskExpectedSha.Remove($TaskName)
    }
    Save-MockScheduler
    [pscustomobject]@{ TaskName=$TaskName }
}
"""
    boundary_day = datetime.fromisoformat(boundary_date)
    replacements = {
        "__RUNTIME__": str(runtime).replace("'", "''"),
        "__STATE__": str(state).replace("'", "''"),
        "__PERSISTENCE__": str(persistence_path).replace("'", "''"),
        "__PHASE__": phase.replace("'", "''"),
        "__BOUNDARY_DATE__": boundary_day.date().isoformat(),
        "__NEXT_DATE__": (boundary_day + timedelta(days=1)).date().isoformat(),
        "__WEEKLY_NEXT_DATE__": (boundary_day + timedelta(days=7)).date().isoformat(),
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    return template


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
@pytest.mark.parametrize(
    (
        "crash_argument",
        "expected_phase",
        "expected_filesystem_state",
        "expected_ready_count",
        "candidate_bound_before",
        "cross_during_recovery_enable",
    ),
    (
        (
            "-TestStageCrashPoint 'after_prepared_receipt'",
            "PRE_QUIESCE",
            "OLD_RUNTIME_INTACT",
            0,
            False,
            False,
        ),
        (
            "-InjectCrashBetweenRuntimeRenames",
            "PRE_SWAP",
            "AFTER_FIRST_RENAME",
            0,
            False,
            False,
        ),
        (
            "-TestStageCrashPoint 'after_ready_receipt'",
            "POST_SWAP",
            "AFTER_SECOND_RENAME",
            0,
            True,
            False,
        ),
        (
            "-TestStageCrashPoint 'after_complete_receipt'",
            "POST_SWAP_READY",
            "AFTER_SECOND_RENAME",
            5,
            True,
            False,
        ),
        (
            "-TestStageCrashPoint 'after_ready_journal'",
            "POST_SWAP_READY",
            "AFTER_SECOND_RENAME",
            0,
            True,
            True,
        ),
        (
            "-TestStageCrashPoint 'after_candidate_runtime_rename'",
            "PRE_SWAP",
            "AFTER_SECOND_RENAME",
            0,
            False,
            True,
        ),
    ),
    ids=(
        "prepared-receipt-before-pre-swap-journal",
        "pre-swap-after-first-rename",
        "ready-receipt-before-post-swap-ready-journal",
        "complete-receipt-before-complete-journal",
        "post-swap-ready-crosses-during-recovery-enable",
        "post-swap-rebind-crosses-during-recovery-enable",
    ),
)
def test_expired_recovery_compensates_every_runtime_shape_without_late_candidate_enable(
    tmp_path: Path,
    crash_argument: str,
    expected_phase: str,
    expected_filesystem_state: str,
    expected_ready_count: int,
    candidate_bound_before: bool,
    cross_during_recovery_enable: bool,
) -> None:
    fixture = _prepare_disposable_activation_recovery_fixture(tmp_path)
    candidate = fixture["candidate"]
    runtime = fixture["runtime"]
    state = fixture["state"]
    backup = fixture["backup"]
    assert isinstance(candidate, Path)
    assert isinstance(runtime, Path)
    assert isinstance(state, Path)
    assert isinstance(backup, Path)
    candidate_sha = str(fixture["candidate_sha"])
    candidate_tree = str(fixture["candidate_tree"])
    previous_sha = str(fixture["previous_sha"])
    previous_tree = str(fixture["previous_tree"])
    persistence = tmp_path / "mock-scheduler.json"
    activation_script = str(
        (candidate / "scripts" / "activate_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    escaped = {
        "candidate": str(candidate).replace("'", "''"),
        "runtime": str(runtime).replace("'", "''"),
        "state": str(state).replace("'", "''"),
        "backup": str(backup).replace("'", "''"),
        "ci": str(fixture["ci"]).replace("'", "''"),
        "sol": str(fixture["sol"]).replace("'", "''"),
    }
    environment = rf"""
$env:DAWNSTRIKE_TEST_ACTIVATION_LUNA_MANIFEST_FIXTURE = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_CLOCK = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_STAGE_CRASH = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_COMPENSATION_CRASH = '1'
$env:DAWNSTRIKE_TEST_LOCK_JOURNAL = '1'
$env:DAWNSTRIKE_TEST_DIRECT_PYTHON_FIXTURE = '1'
$env:DAWNSTRIKE_TEST_GITHUB_CI_FIXTURE = '1'
$env:DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_SHA = '{candidate_sha}'
$env:DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_TREE = '{candidate_tree}'
$env:DAWNSTRIKE_TEST_GITHUB_CI_COMPLETED_AT = '{fixture["ci_completed_at"]}'
$env:DAWNSTRIKE_TEST_GITHUB_OWNER_COMMENT_BODY = '{fixture["owner_comment_body"]}'
"""
    invocation = rf"""
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{escaped["ci"]}' -SolEvidencePath '{escaped["sol"]}' `
  -CandidateRoot '{escaped["candidate"]}' -RuntimeRoot '{escaped["runtime"]}' `
  -StateRoot '{escaped["state"]}' -BackupRoot '{escaped["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120 `
  -TestNowUtc '2026-08-30T23:00:00Z'
""".strip()
    crash_mock = _persistent_disposable_scheduler_mock(
        runtime=runtime,
        state=state,
        persistence_path=persistence,
        phase="crash",
    )
    crash_command = rf"""
$ErrorActionPreference = 'Stop'
. '{activation_script}'
{environment}
{crash_mock}
$null = Invoke-DawnstrikeRuntimeActivation `
{invocation} `
  -TestFreshNowUtc '2026-08-30T23:00:00Z' {crash_argument}
throw 'activation crash seam returned unexpectedly'
"""
    crashed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", crash_command],
        cwd=fixture["source"],
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert crashed.returncode != 0, (crashed.stdout, crashed.stderr)

    journals = list((state / "receipts" / "runtime-operation").glob("runtime-activation-*.json"))
    assert len(journals) == 1, (crashed.stdout, crashed.stderr, journals)
    journal_path = journals[0]
    journal_before = json.loads(journal_path.read_text(encoding="utf-8"))
    assert journal_before["phase"] == expected_phase
    activation_id = journal_path.stem.removeprefix("runtime-activation-")
    assert re.fullmatch(r"[0-9a-f]{24}", activation_id)
    scheduler_before = json.loads(persistence.read_text(encoding="utf-8"))
    assert (
        list(scheduler_before["states"].values()).count("Ready")
        == expected_ready_count
    )
    if expected_ready_count == 0:
        assert set(scheduler_before["states"].values()) == {"Disabled"}
    if candidate_bound_before:
        assert set(scheduler_before["expected_sha"].values()) == {candidate_sha}
    else:
        assert not scheduler_before["expected_sha"]

    recovery_fresh_clock = (
        "2026-08-30T23:00:00Z"
        if cross_during_recovery_enable
        else "2026-09-01T03:00:00Z"
    )
    recovery_boundary_date = (
        datetime.fromisoformat(recovery_fresh_clock.replace("Z", "+00:00"))
        .astimezone()
        .date()
        .isoformat()
    )
    recovery_mock = _persistent_disposable_scheduler_mock(
        runtime=runtime,
        state=state,
        persistence_path=persistence,
        phase="recovery",
        boundary_date=recovery_boundary_date,
    )
    recovery_cross_argument = (
        "-TestEnableBoundaryCrossAfter 2" if cross_during_recovery_enable else ""
    )
    recovery_compensation_crash_argument = (
        "-TestCompensationCrashPoint 'after_stage_preserve'"
        if expected_phase == "PRE_QUIESCE"
        else ""
    )
    recovery_extra_arguments = " ".join(
        argument
        for argument in (
            recovery_cross_argument,
            recovery_compensation_crash_argument,
        )
        if argument
    )
    recovery_command = rf"""
$ErrorActionPreference = 'Stop'
. '{activation_script}'
{environment}
{recovery_mock}
    $recovered = Invoke-DawnstrikeRuntimeActivation `
    {invocation} `
      -TestFreshNowUtc '{recovery_fresh_clock}' {recovery_extra_arguments}
[pscustomobject]@{{
    recovered = $recovered
    task_states = $global:MockTaskStates
    task_expected_sha = $global:MockTaskExpectedSha
    task_events = @($global:TaskEvents)
}} | ConvertTo-Json -Depth 12 -Compress
"""
    if recovery_compensation_crash_argument:
        compensation_crashed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", recovery_command],
            cwd=fixture["source"],
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
        )
        assert compensation_crashed.returncode != 0, (
            compensation_crashed.stdout,
            compensation_crashed.stderr,
        )
        recovery_command = recovery_command.replace(
            recovery_compensation_crash_argument, ""
        )
    recovered = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", recovery_command],
        cwd=fixture["source"],
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert recovered.returncode == 0, (recovered.stdout, recovered.stderr)
    payload = json.loads(recovered.stdout.strip().splitlines()[-1])
    recovery = payload["recovered"]
    assert recovery["status"] == "RECOVERED_EXPIRED_COMPENSATED"
    recovered_phase = (
        "POST_SWAP_READY"
        if expected_phase == "POST_SWAP"
        or (cross_during_recovery_enable and expected_phase == "PRE_SWAP")
        else expected_phase
    )
    assert recovery["recovered_phase"] == recovered_phase
    assert recovery["recovered_filesystem_state"] == expected_filesystem_state
    assert recovery["candidate_sha"] == candidate_sha
    assert recovery["candidate_tree"] == candidate_tree
    assert recovery["restored_sha"] == previous_sha
    assert recovery["restored_tree"] == previous_tree
    assert set(payload["task_states"].values()) == {"Ready"}
    assert not payload["task_expected_sha"]
    recovery_enable_events = [
        event
        for event in payload["task_events"]
        if event["phase"] == "recovery" and event["operation"] == "enable"
    ]
    prior_enable_events = [
        event for event in recovery_enable_events if event["sha"] == "UNBOUND_PRIOR"
    ]
    candidate_enable_events = [
        event for event in recovery_enable_events if event["sha"] == candidate_sha
    ]
    assert len(prior_enable_events) == 5
    assert len(candidate_enable_events) == (2 if cross_during_recovery_enable else 0)

    assert _git(runtime, "rev-parse", "HEAD") == previous_sha
    assert _git(runtime, "rev-parse", "HEAD^{tree}") == previous_tree
    rollback_root = state / "runtime-rollbacks" / activation_id
    failed_candidate = rollback_root / "failed-candidate-runtime"
    assert _git(failed_candidate, "rev-parse", "HEAD") == candidate_sha
    assert _git(failed_candidate, "rev-parse", "HEAD^{tree}") == candidate_tree
    assert not Path(f"{runtime}.stage-{activation_id}").exists()
    assert not (rollback_root / "previous-runtime").exists()
    lock_root = state / "locks"
    assert not (lock_root / "dawnstrike-runtime-activation.lock").exists()
    assert not list(lock_root.glob("dawnstrike-daily-*.lock"))
    stale_lock_evidence = list(lock_root.glob("recovered-stale-*.lock"))
    expected_stale_runtime_locks = 2 if recovery_compensation_crash_argument else 1
    assert len(stale_lock_evidence) == expected_stale_runtime_locks
    assert all(
        re.fullmatch(r"recovered-stale-[0-9a-f]{64}\.lock", path.name)
        for path in stale_lock_evidence
    )
    assert len({path.name for path in stale_lock_evidence}) == len(stale_lock_evidence)
    assert hashlib.sha256(Path(fixture["db"]).read_bytes()).hexdigest() == fixture["db_hash_before"]

    verified_journal = subprocess.run(
        [
            sys.executable,
            str(candidate / "scripts" / "runtime_operation_journal.py"),
            "verify",
            str(journal_path),
            "--state-root",
            str(state),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert verified_journal.returncode == 0, verified_journal.stderr
    journal_after = json.loads(verified_journal.stdout)["payload"]
    assert journal_after["phase"] == "COMPENSATED"
    assert journal_after["current_sha"] == previous_sha
    assert journal_after["current_tree"] == previous_tree
    assert journal_after["task_contract_sha256"] == recovery["restored_task_contract_sha256"]

    compensation_path = state.joinpath(*recovery["compensation_receipt_relative_path"].split("/"))
    verified_compensation = subprocess.run(
        [
            sys.executable,
            str(candidate / "scripts" / "runtime_operation_journal.py"),
            "verify-compensation",
            "--receipt",
            str(compensation_path),
            "--state-root",
            str(state),
        ],
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert verified_compensation.returncode == 0, verified_compensation.stderr
    compensation = json.loads(verified_compensation.stdout)["payload"]
    assert compensation["status"] == "COMPENSATED"
    assert compensation["candidate_sha"] == candidate_sha
    assert compensation["task_state"] == "Ready"
    assert compensation["task_contract_sha256"] == recovery["restored_task_contract_sha256"]
    scheduler_backups = list((state / "scheduler-backups").glob("*/manifest.json"))
    assert len(scheduler_backups) == 1
    scheduler_manifest = json.loads(scheduler_backups[0].read_text(encoding="utf-8"))
    assert scheduler_manifest["task_contract_sha256"] == recovery["restored_task_contract_sha256"]

    if expected_filesystem_state == "AFTER_FIRST_RENAME" and not cross_during_recovery_enable:
        cleanup_mock = _persistent_disposable_scheduler_mock(
            runtime=runtime,
            state=state,
            persistence_path=persistence,
            phase="cleanup",
            boundary_date=(
                datetime.fromisoformat("2026-09-01T03:00:00+00:00")
                .astimezone()
                .date()
                .isoformat()
            ),
        )
        cleanup_command = rf"""
$ErrorActionPreference = 'Stop'
. '{activation_script}'
{environment}
{cleanup_mock}
$blocked = $false
$message = ''
try {{
    $null = Invoke-DawnstrikeRuntimeActivation `
{invocation} `
      -TestFreshNowUtc '2026-09-01T03:00:00Z'
}}
catch {{
    $blocked = $true
    $message = $_.Exception.Message
}}
[pscustomobject]@{{ blocked=$blocked; message=$message }} |
    ConvertTo-Json -Compress
"""
        cleaned = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cleanup_command],
            cwd=fixture["source"],
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
        )
        assert cleaned.returncode == 0, (cleaned.stdout, cleaned.stderr)
        cleanup_result = json.loads(cleaned.stdout.strip().splitlines()[-1])
        assert cleanup_result["blocked"] is True
        assert "boundary" in cleanup_result["message"].lower()
        assert not journal_path.exists()
        journal_archives = list(
            (state / "receipts" / "runtime-operation" / "archive").glob(
                "compensated-*.json"
            )
        )
        assert len(journal_archives) == 1
        receipt_archive = state / "receipts" / "runtime-activation" / "archive"
        assert len(list(receipt_archive.glob("*.prepared.json"))) == 1
        assert len(list(receipt_archive.glob("*.intent.json"))) == 1
        assert not (
            receipt_archive.parent
            / f"runtime-activation-{activation_id}.compensating.json"
        ).exists()
        quarantine = (
            state
            / "recovery-quarantine"
            / f"compensated-{activation_id}-{journal_after['prior_journal_file_sha256']}"
        )
        assert (quarantine / "rollback" / "failed-candidate-runtime").is_dir()
        assert (quarantine / "scheduler-backup" / "manifest.json").is_file()
        assert not rollback_root.exists()
        assert not scheduler_backups[0].parent.exists()
        verified_after_archive = subprocess.run(
            [
                sys.executable,
                str(candidate / "scripts" / "runtime_operation_journal.py"),
                "verify-compensation",
                "--receipt",
                str(compensation_path),
                "--state-root",
                str(state),
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert verified_after_archive.returncode == 0, verified_after_archive.stderr
        assert not (lock_root / "dawnstrike-runtime-activation.lock").exists()
        assert not list(lock_root.glob("dawnstrike-daily-*.lock"))


def _restore_activation_crash_snapshot(
    *,
    runtime: Path,
    state: Path,
    persistence: Path,
    snapshot: Path,
) -> None:
    def remove_readonly(function: object, path: str, _error: BaseException) -> None:
        Path(path).chmod(0o700)
        function(path)  # type: ignore[operator]

    if runtime.exists():
        shutil.rmtree(runtime, onexc=remove_readonly)
    shutil.copytree(snapshot / "runtime", runtime)
    # The activation worker can briefly outlive a forced PowerShell parent
    # while it closes its read-only SQLite handle.  Compensation never mutates
    # the database, so reset only the durable transaction namespaces rather
    # than racing that unrelated handle during the matrix.
    for name in (
        "locks",
        "receipts",
        "scheduler-backups",
        "runtime-rollbacks",
        "recovery-quarantine",
        "task-launch-manifests",
    ):
        live = state / name
        source = snapshot / "state" / name
        if live.exists():
            shutil.rmtree(live, onexc=remove_readonly)
        if source.exists():
            shutil.copytree(source, live)
    shutil.copy2(snapshot / "mock-scheduler.json", persistence)


def _exact_directory_bytes_digest(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        if path.is_dir():
            digest.update(b"D\0" + relative + b"\0")
        elif path.is_file():
            digest.update(b"F\0" + relative + b"\0")
            digest.update(hashlib.sha256(path.read_bytes()).digest())
        else:
            raise AssertionError(f"unexpected non-file activation artifact: {path}")
    return digest.hexdigest()


@pytest.mark.skipif(shutil.which("powershell") is None, reason="Windows PowerShell unavailable")
def test_activation_compensation_and_cleanup_hard_crash_boundaries_replay(
    tmp_path: Path,
) -> None:
    """Every durable compensation mutation survives a process kill and retry."""

    fixture = _prepare_disposable_activation_recovery_fixture(tmp_path)
    candidate = fixture["candidate"]
    runtime = fixture["runtime"]
    state = fixture["state"]
    backup = fixture["backup"]
    assert isinstance(candidate, Path)
    assert isinstance(runtime, Path)
    assert isinstance(state, Path)
    assert isinstance(backup, Path)
    candidate_sha = str(fixture["candidate_sha"])
    candidate_tree = str(fixture["candidate_tree"])
    previous_sha = str(fixture["previous_sha"])
    previous_tree = str(fixture["previous_tree"])
    persistence = tmp_path / "mock-scheduler.json"
    activation_script = str(
        (candidate / "scripts" / "activate_dawnstrike_runtime.ps1").resolve()
    ).replace("'", "''")
    escaped = {
        "candidate": str(candidate).replace("'", "''"),
        "runtime": str(runtime).replace("'", "''"),
        "state": str(state).replace("'", "''"),
        "backup": str(backup).replace("'", "''"),
        "ci": str(fixture["ci"]).replace("'", "''"),
        "sol": str(fixture["sol"]).replace("'", "''"),
    }
    environment = rf"""
$env:DAWNSTRIKE_TEST_ACTIVATION_LUNA_MANIFEST_FIXTURE = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_CLOCK = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_STAGE_CRASH = '1'
$env:DAWNSTRIKE_TEST_ACTIVATION_COMPENSATION_CRASH = '1'
$env:DAWNSTRIKE_TEST_LOCK_JOURNAL = '1'
$env:DAWNSTRIKE_TEST_DIRECT_PYTHON_FIXTURE = '1'
$env:DAWNSTRIKE_TEST_GITHUB_CI_FIXTURE = '1'
$env:DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_SHA = '{candidate_sha}'
$env:DAWNSTRIKE_TEST_GITHUB_CI_CANDIDATE_TREE = '{candidate_tree}'
$env:DAWNSTRIKE_TEST_GITHUB_CI_COMPLETED_AT = '{fixture["ci_completed_at"]}'
$env:DAWNSTRIKE_TEST_GITHUB_OWNER_COMMENT_BODY = '{fixture["owner_comment_body"]}'
"""
    invocation = rf"""
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{escaped["ci"]}' -SolEvidencePath '{escaped["sol"]}' `
  -CandidateRoot '{escaped["candidate"]}' -RuntimeRoot '{escaped["runtime"]}' `
  -StateRoot '{escaped["state"]}' -BackupRoot '{escaped["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120 `
  -TestNowUtc '2026-08-30T23:00:00Z'
""".strip()
    scheduler_mock = _persistent_disposable_scheduler_mock(
        runtime=runtime,
        state=state,
        persistence_path=persistence,
        phase="compensation-matrix",
    )
    initial_crash = rf"""
$ErrorActionPreference = 'Stop'
. '{activation_script}'
. (Join-Path '{escaped["candidate"]}' 'scripts\runtime_activation_lock.ps1')
. (Join-Path '{escaped["candidate"]}' 'scripts\dawnstrike_process_runner.ps1')
. (Join-Path '{escaped["candidate"]}' 'scripts\capture_task_safety.ps1')
. (Join-Path '{escaped["candidate"]}' 'scripts\invoke_dawnstrike_stage.ps1')
{environment}
{scheduler_mock}
$null = Invoke-DawnstrikeRuntimeActivation `
{invocation} `
  -TestFreshNowUtc '2026-08-30T23:00:00Z' `
  -TestStageCrashPoint 'after_complete_receipt'
throw 'initial compensation fixture did not crash'
"""
    seeded = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", initial_crash],
        cwd=fixture["source"],
        text=True,
        capture_output=True,
        timeout=600,
        check=False,
    )
    assert seeded.returncode != 0, (seeded.stdout, seeded.stderr)
    journal_paths = list(
        (state / "receipts" / "runtime-operation").glob("runtime-activation-*.json")
    )
    assert len(journal_paths) == 1
    activation_id = journal_paths[0].stem.removeprefix("runtime-activation-")
    rollback_root = state / "runtime-rollbacks" / activation_id
    assert (
        json.loads(journal_paths[0].read_text(encoding="utf-8"))["phase"]
        == "POST_SWAP_READY"
    )

    baseline = tmp_path / "compensation-baseline"
    shutil.copytree(runtime, baseline / "runtime")
    shutil.copytree(state, baseline / "state")
    shutil.copy2(persistence, baseline / "mock-scheduler.json")

    def direct_compensation_command(crash_point: str) -> str:
        return rf"""
$ErrorActionPreference = 'Stop'
. '{activation_script}'
. (Join-Path '{escaped["candidate"]}' 'scripts\runtime_activation_lock.ps1')
. (Join-Path '{escaped["candidate"]}' 'scripts\dawnstrike_process_runner.ps1')
. (Join-Path '{escaped["candidate"]}' 'scripts\capture_task_safety.ps1')
. (Join-Path '{escaped["candidate"]}' 'scripts\invoke_dawnstrike_stage.ps1')
{environment}
{scheduler_mock}
$script:DawnstrikeActivationEnableCrashAfter = 0
$gitPath = (Get-DawnstrikeApprovedGit).path
$interpreter = Get-DawnstrikeApprovedLockInterpreter
$journalRoot = Join-Path '{escaped["state"]}' 'receipts\runtime-operation'
$journalPath = (Get-ChildItem -LiteralPath $journalRoot `
    -Filter 'runtime-activation-*.json' -File).FullName
$journal = Get-DawnstrikeStrictRuntimeOperationJournal `
    $journalPath $interpreter.path $interpreter.sha256
$activationName = [IO.Path]::GetFileNameWithoutExtension($journalPath)
$activationId = $activationName.Substring('runtime-activation-'.Length)
$receiptRoot = Join-Path '{escaped["state"]}' 'receipts\runtime-activation'
$rollbackRoot = Join-Path '{escaped["state"]}' "runtime-rollbacks\$activationId"
$rollbackCheckout = Join-Path $rollbackRoot 'previous-runtime'
$previousRoot = if (Test-Path -LiteralPath $rollbackCheckout) {{
    $rollbackCheckout
}} else {{
    '{escaped["runtime"]}'
}}
$previousOrigin = Get-DawnstrikeGitValue `
    $gitPath $previousRoot @('remote','get-url','origin') `
    'Matrix previous origin' 120
$activationLock = Adopt-DawnstrikeGovernedRuntimeLockWithJournal `
    -StateRoot '{escaped["state"]}' -JournalPath $journalPath `
    -CandidateSha '{candidate_sha}' -CandidateTree '{candidate_tree}' `
    -OriginIdentity ([string]$journal.payload.origin_identity) `
    -PythonPath $interpreter.path -PythonSha256 $interpreter.sha256
try {{ throw [InvalidOperationException]::new('matrix compensation') }} catch {{ $failure = $_ }}
$result = Invoke-DawnstrikeActivationCompensationStateMachine `
    -Failure $failure -FailurePhase ([string]$journal.payload.phase) `
    -ActivationLock $activationLock -MarketDate '2026-08-31' `
    -CandidateRoot '{escaped["candidate"]}' -RuntimeRoot '{escaped["runtime"]}' `
    -StateRoot '{escaped["state"]}' -ActivationId $activationId `
    -OperationJournal $journalPath -ExpectedSha '{candidate_sha}' `
    -ExpectedTree '{candidate_tree}' -PreviousSha '{previous_sha}' `
    -PreviousTree '{previous_tree}' -OriginIdentity ([string]$journal.payload.origin_identity) `
    -PreviousOriginSha256 (Get-DawnstrikeSha256Text $previousOrigin) `
    -SchedulerBackupName ("runtime-activation-" + $activationId) `
    -PreparedReceipt (Join-Path $receiptRoot "runtime-activation-$activationId.prepared.json") `
    -ReadyReceipt (Join-Path $receiptRoot "runtime-activation-$activationId.ready.json") `
    -CompleteReceipt (Join-Path $receiptRoot "runtime-activation-$activationId.json") `
    -ReceiptRoot $receiptRoot `
    -Stage ('{escaped["runtime"]}' + ".stage-$activationId") `
    -RollbackRoot $rollbackRoot -RollbackCheckout $rollbackCheckout `
    -GitPath $gitPath -PythonPath $interpreter.path `
    -PythonSha256 $interpreter.sha256 -TimeoutSeconds 120 `
    -StateDeclaration ([pscustomobject]@{{ required = $false }}) `
    -AllowLegacyCanonicalExecute -TestCrashPoint '{crash_point}'
[pscustomobject]@{{
    recovered=$result
    states=$global:MockTaskStates
    expected=$global:MockTaskExpectedSha
}} |
    ConvertTo-Json -Depth 12 -Compress
"""

    def assert_compensated_boundary(
        *, point: str, result: subprocess.CompletedProcess[str]
    ) -> None:
        assert result.returncode == 0, (point, result.stdout, result.stderr)
        payload = json.loads(result.stdout.strip().splitlines()[-1])
        assert payload["recovered"]["status"] == "RECOVERED_EXPIRED_COMPENSATED", point
        assert set(payload["states"].values()) == {"Ready"}, point
        assert not payload["expected"], point
        assert _git(runtime, "rev-parse", "HEAD") == previous_sha
        assert _git(runtime, "rev-parse", "HEAD^{tree}") == previous_tree
        failed = rollback_root / "failed-candidate-runtime"
        assert _git(failed, "rev-parse", "HEAD") == candidate_sha
        assert _git(failed, "rev-parse", "HEAD^{tree}") == candidate_tree
        assert not (rollback_root / "previous-runtime").exists()
        assert not Path(f"{runtime}.stage-{activation_id}").exists()
        lock_root = state / "locks"
        assert not (lock_root / "dawnstrike-runtime-activation.lock").exists()
        assert not list(lock_root.glob("dawnstrike-daily-*.lock"))
        journal = json.loads(journal_paths[0].read_text(encoding="utf-8"))
        assert journal["phase"] == "COMPENSATED"

    replay_points = (
        "after_intent",
        "after_initial_disable",
        "after_candidate_preserve",
        "after_previous_restore",
        "after_final_disable",
        "after_task_action_1",
        "after_task_action_2",
        "after_task_action_3",
        "after_task_action_4",
        "after_task_action_5",
        "after_task_enable_1",
        "after_task_enable_2",
        "after_task_enable_3",
        "after_task_enable_4",
        "after_task_enable_5",
        "after_task_restore",
        "after_failure_receipt",
        "after_compensation_receipt",
    )
    for point in replay_points:
        print(f"compensation crash/retry: {point}", flush=True)
        _restore_activation_crash_snapshot(
            runtime=runtime,
            state=state,
            persistence=persistence,
            snapshot=baseline,
        )
        crashed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                direct_compensation_command(point),
            ],
            cwd=fixture["source"],
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        assert crashed.returncode != 0, (point, crashed.stdout, crashed.stderr)
        resumed = subprocess.run(
            [
                "powershell",
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                direct_compensation_command(""),
            ],
            cwd=fixture["source"],
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        assert_compensated_boundary(point=point, result=resumed)

    # Create one terminal COMPENSATED snapshot with both stale locks intact.
    # Every cleanup boundary below starts from these same exact bytes.
    _restore_activation_crash_snapshot(
        runtime=runtime,
        state=state,
        persistence=persistence,
        snapshot=baseline,
    )
    terminal_crash = subprocess.run(
        [
            "powershell",
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            direct_compensation_command("after_compensated_journal"),
        ],
        cwd=fixture["source"],
        text=True,
        capture_output=True,
        timeout=300,
        check=False,
    )
    assert terminal_crash.returncode != 0
    terminal_journal = json.loads(journal_paths[0].read_text(encoding="utf-8"))
    assert terminal_journal["phase"] == "COMPENSATED"
    terminal_attempt = terminal_journal["prior_journal_file_sha256"]
    terminal_compensation = state.joinpath(
        *terminal_journal["compensation_receipt_relative_path"].split("/")
    )
    terminal = tmp_path / "compensated-terminal-baseline"
    shutil.copytree(runtime, terminal / "runtime")
    shutil.copytree(state, terminal / "state")
    shutil.copy2(persistence, terminal / "mock-scheduler.json")
    terminal_candidate_digest = _exact_directory_bytes_digest(
        terminal
        / "state"
        / "runtime-rollbacks"
        / activation_id
        / "failed-candidate-runtime"
    )

    cleanup_points = (
        "cleanup_after_daily_release",
        "cleanup_after_runtime_release",
        "cleanup_after_prepared_archive",
        "cleanup_after_ready_archive",
        "cleanup_after_complete_archive",
        "cleanup_after_rollback_quarantine",
        "cleanup_after_scheduler_quarantine",
        "cleanup_after_intent_archive",
        "cleanup_after_journal_clear",
    )
    cleanup_mock = _persistent_disposable_scheduler_mock(
        runtime=runtime,
        state=state,
        persistence_path=persistence,
        phase="cleanup-matrix",
        boundary_date=(
            datetime.fromisoformat("2026-09-01T03:00:00+00:00")
            .astimezone()
            .date()
            .isoformat()
        ),
    )

    def cleanup_command(point: str) -> str:
        crash_argument = (
            f"-TestCompensationCrashPoint '{point}'" if point else ""
        )
        return rf"""
$ErrorActionPreference = 'Stop'
. '{activation_script}'
{environment}
{cleanup_mock}
$blocked = $false
$message = ''
try {{
    $null = Invoke-DawnstrikeRuntimeActivation `
{invocation} `
      -TestFreshNowUtc '2026-09-01T03:00:00Z' {crash_argument}
}}
catch {{
    $blocked = $true
    $message = $_.Exception.Message
}}
[pscustomobject]@{{ blocked=$blocked; message=$message }} | ConvertTo-Json -Compress
"""

    for point in cleanup_points:
        print(f"compensated cleanup crash/retry: {point}", flush=True)
        _restore_activation_crash_snapshot(
            runtime=runtime,
            state=state,
            persistence=persistence,
            snapshot=terminal,
        )
        crashed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cleanup_command(point)],
            cwd=fixture["source"],
            text=True,
            capture_output=True,
            timeout=300,
            check=False,
        )
        assert crashed.returncode != 0, (point, crashed.stdout, crashed.stderr)
        resumed = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", cleanup_command("")],
            cwd=fixture["source"],
            text=True,
            capture_output=True,
            timeout=600,
            check=False,
        )
        assert resumed.returncode == 0, (point, resumed.stdout, resumed.stderr)
        cleanup_result = json.loads(resumed.stdout.strip().splitlines()[-1])
        assert cleanup_result["blocked"] is True, point
        assert "boundary" in cleanup_result["message"].lower(), point
        assert _git(runtime, "rev-parse", "HEAD") == previous_sha
        assert set(json.loads(persistence.read_text(encoding="utf-8"))["states"].values()) == {
            "Ready"
        }
        assert not journal_paths[0].exists()
        lock_root = state / "locks"
        assert not (lock_root / "dawnstrike-runtime-activation.lock").exists()
        assert not list(lock_root.glob("dawnstrike-daily-*.lock"))
        archive = state / "receipts" / "runtime-activation" / "archive"
        assert len(list(archive.glob("*.prepared.json"))) == 1
        assert len(list(archive.glob("*.ready.json"))) == 1
        assert len(list(archive.glob("*.complete.json"))) == 1
        assert len(list(archive.glob("*.intent.json"))) == 1
        quarantine = (
            state
            / "recovery-quarantine"
            / f"compensated-{activation_id}-{terminal_attempt}"
        )
        assert _exact_directory_bytes_digest(
            quarantine / "rollback" / "failed-candidate-runtime"
        ) == terminal_candidate_digest
        assert (quarantine / "scheduler-backup" / "manifest.json").is_file()
        verified = subprocess.run(
            [
                sys.executable,
                str(candidate / "scripts" / "runtime_operation_journal.py"),
                "verify-compensation",
                "--receipt",
                str(terminal_compensation),
                "--state-root",
                str(state),
            ],
            text=True,
            capture_output=True,
            timeout=30,
            check=False,
        )
        assert verified.returncode == 0, (point, verified.stderr)


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
        "state_root_boundary.ps1",
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
        "refresh_luna_core_universe.py",
        "publish_vercel_public.ps1",
        "vercel_source_contract.ps1",
        "vercel_toolchain_contract.py",
        "vercel_publication_journal.py",
        "publication_boundary.py",
        "verify_daily_prepublication.py",
        "build_vercel_public_stage.ps1",
        "verify_vercel_candidate.ps1",
    ):
        shutil.copy2(source / "scripts" / name, candidate / "scripts" / name)
    candidate_process_runner = candidate / "scripts" / "dawnstrike_process_runner.ps1"
    process_runner_source = candidate_process_runner.read_text(encoding="utf-8")
    luna_fixture_anchor = (
        "    $releaseRoot = [IO.Path]::GetFullPath((Join-Path $PSScriptRoot '..')).TrimEnd('\\')\n"
    )
    assert process_runner_source.count(luna_fixture_anchor) == 1
    process_runner_source = process_runner_source.replace(
        luna_fixture_anchor,
        "    if ($env:DAWNSTRIKE_TEST_ACTIVATION_LUNA_MANIFEST_FIXTURE -eq '1') {\n"
        "        return @(\n"
        "            'scripts/refresh_luna_core_universe.py',\n"
        "            'intraday_scanner/__init__.py'\n"
        "        )\n"
        "    }\n" + luna_fixture_anchor,
        1,
    )
    candidate_process_runner.write_text(process_runner_source, encoding="utf-8")
    _install_local_origin_fixture_seam(candidate / "scripts" / "runtime_activation_lock.ps1")
    _install_local_interpreter_fixture_seam(candidate)
    _install_local_bootstrap_origin_fixture_seam(
        candidate / "scripts" / "dawnstrike_python_bootstrap.py", origin=remote
    )
    _install_local_github_ci_fixture_seam(candidate / "scripts" / "runtime_activation_contract.py")
    shutil.copytree(
        source / "intraday_scanner",
        candidate / "intraday_scanner",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
    )
    shutil.copy2(source / ".gitignore", candidate / ".gitignore")
    shutil.copy2(source / ".gitattributes", candidate / ".gitattributes")
    shutil.copy2(source / "pyproject.toml", candidate / "pyproject.toml")
    shutil.copy2(source / "requirements.lock", candidate / "requirements.lock")
    candidate_bootstrap = candidate / "scripts" / "dawnstrike_python_bootstrap.py"
    candidate_bootstrap.write_text(
        candidate_bootstrap.read_text(encoding="utf-8").replace(
            PRODUCTION_RECORD_SET_SHA256,
            _host_dependency_record_contract(source / "requirements.lock"),
            1,
        ),
        encoding="utf-8",
    )
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
    # The disposable transaction test uses a committed candidate-only fixture
    # seam for a representative two-file Luna launch manifest. Exhaustive
    # source inventory is covered separately.
    $env:DAWNSTRIKE_TEST_ACTIVATION_LUNA_MANIFEST_FIXTURE = '1'
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
$global:MockTaskActions = @{{}}
$global:TaskEvents = @()
foreach ($name in $script:DawnstrikeCanonicalTaskNames) {{
    $global:MockTaskStates[$name] = 'Ready'
}}
    function Get-ScheduledTask {{
        [CmdletBinding()] param([string]$TaskName)
        if ($TaskName -eq 'Dawnstrike Delayed SIP Capture') {{ return @() }}
        $boundSha = [string]$global:MockTaskExpectedSha[$TaskName]
        $hasPersistedAction = $global:MockTaskActions.ContainsKey($TaskName)
        # Persisted task actions represent the exact scheduler state.  Avoid
        # regenerating an older runtime's protected launcher command.
        $policy = if ($hasPersistedAction) {{
            Get-DawnstrikeCanonicalTaskPolicy $TaskName $global:MockRuntime $global:MockState
        }} elseif ($boundSha) {{
            Get-DawnstrikeCanonicalTaskPolicy `
                $TaskName $global:MockRuntime $global:MockState $boundSha
        }} else {{
            Get-DawnstrikeCanonicalTaskPolicy $TaskName $global:MockRuntime $global:MockState
        }}
        $currentAction = if ($hasPersistedAction) {{
            $global:MockTaskActions[$TaskName]
        }} else {{
            [pscustomobject]@{{
                Execute=$script:DawnstrikePowerShellExecutable;
                Arguments=$policy.arguments;
                WorkingDirectory=$global:MockRuntime
            }}
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
            Actions=@($currentAction);
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
    function Get-ScheduledTaskInfo {{
        [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
        $lastRun = switch ($TaskName) {{
            'Dawnstrike AlphaOps EOD Full Report' {{ [DateTime]'2026-08-30T15:15:00' }}
            'Dawnstrike 10of10 Daily Finalize' {{ [DateTime]'2026-08-30T17:30:00' }}
            'Dawnstrike AlphaOps V6 Weekly Training' {{ [DateTime]'2026-08-24T21:00:00' }}
            default {{ [DateTime]'2026-08-29T08:00:00' }}
        }}
        [pscustomobject]@{{
            LastRunTime=$lastRun;
            NextRunTime=[DateTime]'2026-08-31T08:00:00'
        }}
    }}
    function Export-ScheduledTask {{
        [CmdletBinding()] param([string]$TaskName,[string]$TaskPath)
        $task = Get-ScheduledTask -TaskName $TaskName
        $action = @($task.Actions)[0]
        $enabled = if ($global:MockTaskStates[$TaskName] -eq 'Disabled') {{
            'false'
        }} else {{ 'true' }}
        $safeName = [Security.SecurityElement]::Escape([string]$TaskName)
        $safeCommand = [Security.SecurityElement]::Escape([string]$action.Execute)
        $safeArguments = [Security.SecurityElement]::Escape([string]$action.Arguments)
        $safeWorking = [Security.SecurityElement]::Escape([string]$action.WorkingDirectory)
        "<Task><Name>$safeName</Name><Principal><UserId>activation-test</UserId><LogonType>Password</LogonType></Principal><Triggers><CalendarTrigger><StartBoundary>2026-08-31T08:00:00-05:00</StartBoundary></CalendarTrigger></Triggers><Settings><Enabled>$enabled</Enabled><ExecutionTimeLimit>PT2H</ExecutionTimeLimit></Settings><Actions><Exec><Command>$safeCommand</Command><Arguments>$safeArguments</Arguments><WorkingDirectory>$safeWorking</WorkingDirectory></Exec></Actions></Task>"
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
        $global:MockTaskActions[$TaskName] = @($Action)[0]
        $match = [regex]::Match(
            [string](@($Action)[0].Arguments),
            '-ExpectedSha\s+["'']?([0-9a-f]{{40}})'
        )
        if ($match.Success) {{
            $global:MockTaskExpectedSha[$TaskName] = $match.Groups[1].Value
        }} else {{
            $global:MockTaskExpectedSha.Remove($TaskName)
        }}
        [pscustomobject]@{{ TaskName=$TaskName }}
}}
$activated = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120 -TestNowUtc '2026-08-30T23:00:00Z'
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
      -BackupRetention 5 -ProcessTimeoutSeconds 120 -TestNowUtc '2026-08-30T23:00:00Z'
}}
catch {{ $activationMissingBundleBlocked = $true }}
finally {{ [System.IO.File]::Move($heldBundlePath, $bundlePath) }}
$activatedAgain = Invoke-DawnstrikeRuntimeActivation `
  -ExpectedSha '{candidate_sha}' -MarketDate '2026-08-31' `
  -CiEvidencePath '{values["ci"]}' -SolEvidencePath '{values["sol"]}' `
  -CandidateRoot '{values["candidate"]}' -RuntimeRoot '{values["runtime"]}' `
  -StateRoot '{values["state"]}' -BackupRoot '{values["backup"]}' `
  -BackupRetention 5 -ProcessTimeoutSeconds 120 -TestNowUtc '2026-08-30T23:00:00Z'
$receiptName = 'runtime-activation-' + $activated.activation_id + '.json'
            $receiptForRollback = Join-Path `
                '{values["state"]}' ('receipts\runtime-activation\' + $receiptName)
                . '{rollback_script}'
                $rollbackLegacyBlocked = $false
        $rollbackLegacyError = ''
        try {{
        $null = Invoke-DawnstrikeRuntimeRollback `
          -ActivationReceipt $receiptForRollback -ContractRoot '{values["candidate"]}' `
          -RuntimeRoot '{values["runtime"]}' -StateRoot '{values["state"]}' `
          -BackupRoot '{values["backup"]}' `
          -ProcessTimeoutSeconds 120
        }}
        catch {{
            $rollbackLegacyError = $_.Exception.Message
            $rollbackLegacyBlocked = $_.Exception.Message -match 'quarantined|authorized COMPLETE'
    }}
    $output = [pscustomobject]@{{
        activated=$activated
            activated_again=$activatedAgain
            rollback_legacy_blocked=$rollbackLegacyBlocked
            rollback_legacy_error=$rollbackLegacyError
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
        timeout=600,
        check=False,
    )
    if result.returncode != 0:
        print(result.stderr)
    assert result.returncode == 0, (result.stdout, result.stderr)
    payload = json.loads(result.stdout.strip().splitlines()[-1])
    assert payload["activated"]["status"] == "COMPLETE"
    assert payload["activated"]["candidate_sha"] == candidate_sha
    assert payload["activated"]["previous_runtime_rollback_authorized"] is False
    assert payload["activated"]["previous_runtime_disposition"] == "QUARANTINED_UNAUTHORIZED"
    assert payload["activated_again"]["receipt_sha256"] == payload["activated"]["receipt_sha256"]
    assert payload["activation_missing_bundle_blocked"] is True
    assert payload["rollback_legacy_blocked"] is True, payload["rollback_legacy_error"]
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
