import subprocess
from pathlib import Path

import pytest

import intraday_scanner.services.scheduler_doctor_service as scheduler
import scripts.dawnstrike_python_bootstrap as bootstrap

ROOT = Path(__file__).resolve().parents[1]
RUNNERS = (
    "run_alphaops_morning.ps1",
    "run_alphaops_monitor.ps1",
    "run_alphaops_eod.ps1",
    "run_alphaops_weekly_training.ps1",
    "run_daily_finalize.ps1",
)


def test_scheduled_runners_require_the_externally_activated_sha_and_bind_entry_bytes() -> None:
    for name in RUNNERS:
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "[Parameter(Mandatory = $true)]" in text
        assert "[string]$ExpectedSha" in text
        assert "-ExpectedSha $ExpectedSha" in text
        assert "-EntryScript $PSCommandPath" in text
        assert "LaunchManifestPath" in text
        assert "LaunchManifestSha256" in text


def test_legacy_registration_and_restore_entries_are_fail_closed_shims() -> None:
    expectations = {
        "register_alphaops_tasks.ps1": "direct AlphaOps task registration is disabled",
        "register_daily_finalize_task.ps1": (
            "direct daily-finalize task registration is disabled"
        ),
        "restore_dawnstrike_tasks.ps1": "direct XML task restore is disabled",
    }
    forbidden = (
        "Resolve-Path",
        "Test-Path",
        "Get-Content",
        "Register-ScheduledTask",
        "Set-ScheduledTask",
        "Disable-ScheduledTask",
        "Enable-ScheduledTask",
        "Assert-DawnstrikeProcessSourceBoundToHead",
    )

    for name, denial in expectations.items():
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "[string]$ExpectedSha" in text
        assert denial in text
        assert "administrator-installed protected release launcher" in text
        for marker in forbidden:
            assert marker not in text


@pytest.mark.skipif(
    not Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe").is_file(),
    reason="legacy scheduler-entry denial proof requires Windows PowerShell 5.1",
)
@pytest.mark.parametrize(
    ("script_name", "error", "requires_backup"),
    [
        (
            "register_alphaops_tasks.ps1",
            "direct AlphaOps task registration is disabled",
            False,
        ),
        (
            "register_daily_finalize_task.ps1",
            "direct daily-finalize task registration is disabled",
            False,
        ),
        (
            "restore_dawnstrike_tasks.ps1",
            "direct XML task restore is disabled",
            True,
        ),
    ],
)
def test_legacy_scheduler_entries_deny_before_attacker_path_access(
    tmp_path: Path,
    script_name: str,
    error: str,
    requires_backup: bool,
) -> None:
    powershell = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    runtime = tmp_path / "attacker-runtime"
    state = tmp_path / "state-must-not-be-created"
    backup = tmp_path / "attacker-backup"
    args = [
        powershell,
        "-NoProfile",
        "-ExecutionPolicy",
        "Bypass",
        "-File",
        str(ROOT / "scripts" / script_name),
        "-ExpectedSha",
        "a" * 40,
        "-RuntimeRoot",
        str(runtime),
        "-StateRoot",
        str(state),
    ]
    if requires_backup:
        args.extend(["-BackupRoot", str(backup)])

    result = subprocess.run(args, capture_output=True, text=True, check=False)

    assert result.returncode != 0
    assert error in (result.stdout + result.stderr)
    assert not runtime.exists()
    assert not state.exists()
    assert not backup.exists()


def test_process_runner_rejects_identity_substitution_and_git_execution_hooks() -> None:
    text = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(encoding="utf-8")
    for marker in (
        "does not match the externally activated SHA",
        "contains hidden Git index entries",
        "contains Git replace refs",
        "contains a Git execution/filter configuration",
        "differs from exact HEAD",
        "EntryScript",
    ):
        assert marker in text
    assert "$codeRoot = Get-DawnstrikeProtectedReleaseRoot -ExpectedSha $ExpectedSha" in text
    assert "-ReleaseRoot $codeRoot -ExpectedSha $ExpectedSha" in text
    assert "-ReleaseRoot $runtimePath" not in text
    assert "Assert-DawnstrikePythonDependencyAclBoundary" in text
    assert "Get-Acl -LiteralPath" in text


def test_scheduled_python_requires_an_administrator_owned_program_files_boundary() -> None:
    text = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(encoding="utf-8")
    for marker in (
        r"C:\Program Files\Dawnstrike\Python313",
        "python313.dll",
        r"DLLs\_hashlib.pyd",
        r"Lib\hashlib.py",
        r"Scripts\uv.exe",
        "is not owned by an administrator principal",
        "is writable by a non-admin principal",
    ):
        assert marker in text
    assert "AzureAD" not in text
    assert "Resolve-DawnstrikeProcessAclSid" in text
    assert "S-1-5-32-544" in text
    assert "S-1-5-18" in text
    assert "\\Administrators" not in text


def test_release_activation_uses_an_administrator_installed_preparse_launcher() -> None:
    launcher = (ROOT / "scripts" / "dawnstrike_release_launcher.ps1").read_text(encoding="utf-8")
    activation = (ROOT / "scripts" / "activate_dawnstrike_runtime.ps1").read_text(encoding="utf-8")
    installer = (ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1").read_text(
        encoding="utf-8"
    )

    for marker in (
        r"C:\Program Files\Dawnstrike\releases",
        r"scripts\dawnstrike_release_launcher.ps1",
        "Trusted release launcher requires exact local and origin/main SHA identity",
        "Trusted release entry bytes do not match the exact candidate commit",
        "[IO.FileShare]::Read",
        r"scripts\activate_dawnstrike_runtime.ps1",
        r"scripts\rollback_dawnstrike_runtime.ps1",
        r"scripts\prepare_dawnstrike_state.ps1",
        r"scripts\harden_intraday_capture_task.ps1",
        r"scripts\rebind_intraday_capture_task.ps1",
        "HardenCapture mode requires a locally prompted RunAsCredential",
        "RebindCapture mode requires a credential and exact input files",
        "Activate mode requires an elevated administrator process.",
        "-AllowLegacyCanonicalExecute",
    ):
        assert marker in launcher
    assert launcher.index("Open-DawnstrikeLauncherEntry") < launcher.index("& $entryLocks[0].path")
    for marker in (
        r"C:\Program Files\Dawnstrike\releases",
        r"scripts\dawnstrike_release_launcher.ps1",
        "Legacy canonical-task admission is restricted to the protected release launcher.",
        "Legacy canonical-task rebinding requires an elevated administrator process.",
        "Canonical tasks contain a mixed pinned/legacy executable set.",
    ):
        assert marker in activation
    assert "DAWNSTRIKE_TEST_LEGACY_CANONICAL_EXECUTE" not in activation
    for marker in (
        "requires an elevated administrator process",
        r"C:\Program Files\Dawnstrike",
        r"C:\ProgramData\Dawnstrike",
        "installers\\",
        "install_dawnstrike_host_boundary.ps1",
        "https://www.python.org/ftp/python/3.13.15/python-3.13.15-amd64.exe",
        "edec09c4853aeae9ac36efb8c9f95b6b8e2fee65eee56d9767a8b7c69c574403",  # pragma: allowlist secret  # noqa: E501
        "must run from the protected installed bootstrap path",
        "Copy-DawnstrikeExactGitFile",
        "SetAccessRuleProtection($true, $false)",
        "S-1-5-32-544",
        "S-1-5-18",
        "S-1-5-32-545",
        "research_only = $true",
        "broker_execution_enabled = $false",
        "Protected Python dependency verification failed",
        "dawnstrike.protected_python_verification.v1",
        "materialize_dawnstrike_dependencies.py",
        "dawnstrike.dependency_materialization.v1",
        "Include_pip=0",
        "Protected dependency materialization failed",
    ):
        assert marker in installer
    assert "robocopy.exe" not in installer


def test_host_installer_invokes_candidate_bootstrap_with_protected_reference_hash() -> None:
    installer = (ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1").read_text(
        encoding="utf-8"
    )

    assert (
        "$bootstrapReference = Join-Path $bootstrapRoot 'dawnstrike_python_bootstrap.py'"
    ) in installer
    assert (
        "$bootstrap = Join-Path $candidate 'scripts\\dawnstrike_python_bootstrap.py'"
    ) in installer
    assert (
        "$verificationTarget = Join-Path $candidate "
        "'scripts\\verify_dawnstrike_python_environment.py'"
    ) in installer
    assert "-Destination $bootstrapReference" in installer
    assert "$bootstrapHash = Get-DawnstrikeInstallSha256 $bootstrapReference" in installer
    assert "-I -B -S -c $preloader $bootstrap $bootstrapHash" in installer
    assert "$bootstrap = Join-Path $bootstrapRoot" not in installer
    assert "$verificationTarget = Join-Path $bootstrapRoot" not in installer


@pytest.mark.skipif(
    not Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe").is_file(),
    reason="scheduled-entry file-lock proof requires Windows PowerShell 5.1",
)
def test_task_guard_holds_entry_and_helper_bytes_against_concurrent_swap(tmp_path: Path) -> None:
    state = tmp_path / "state"
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    script = """
    $ErrorActionPreference = 'Stop'
    . '{runner}'
    function Get-DawnstrikeProtectedReleaseRoot {{ param([string]$ExpectedSha) '{root}' }}
    $gitPath = (Get-Command git.exe -ErrorAction Stop).Path
    $script:DawnstrikeProtectedGitBoundaryContract = [pscustomobject]@{{
        git_path=$gitPath; git_sha256=('a' * 64)
        git_boundary_manifest_path='{runner}'; git_boundary_manifest_sha256=('b' * 64)
    }}
    function Get-DawnstrikeProtectedPythonDependencyContract {{
        param([string]$ReleaseRoot,[string]$ExpectedSha,[switch]$RetainLocks)
        [pscustomobject]@{{
            python_path='{runner}'; python_sha256=('c' * 64)
            python_boundary_manifest_path='{runner}'; python_boundary_manifest_sha256=('d' * 64)
            requirements_lock_sha256=('e' * 64); requirements_lock_blob=('f' * 40)
            dependency_root='{root}'; dependency_manifest_path='{runner}'
            dependency_manifest_sha256=('1' * 64); locks=@()
        }}
    }}
    $m = New-DawnstrikeScheduledLaunchManifest `
        -RuntimeRoot '{root}' -StateRoot '{state}' -ExpectedSha '{sha}' `
        -TaskScript 'run_alphaops_morning.ps1'
    $bound = Assert-DawnstrikeScheduledLaunchManifest `
        -RuntimeRoot '{root}' -StateRoot '{state}' -ExpectedSha '{sha}' `
        -TaskScript 'run_alphaops_morning.ps1' -ManifestPath $m.path `
        -ManifestSha256 $m.sha256 -EntryScript '{entry}'
    try {{
        try {{
            [IO.File]::WriteAllText('{entry}', 'hostile replacement')
            'SWAP_SUCCEEDED'
        }} catch {{ 'SWAP_BLOCKED' }}
        try {{
            [IO.File]::WriteAllText('{refresh}', 'hostile replacement')
            'REFRESH_SWAP_SUCCEEDED'
        }} catch {{ 'REFRESH_SWAP_BLOCKED' }}
        try {{
            [IO.File]::WriteAllText('{service}', 'hostile replacement')
            'SERVICE_SWAP_SUCCEEDED'
        }} catch {{ 'SERVICE_SWAP_BLOCKED' }}
    }} finally {{ foreach ($lock in $bound.locks) {{ $lock.Dispose() }} }}
    """.format(
        runner=str(ROOT / "scripts" / "dawnstrike_process_runner.ps1").replace("'", "''"),
        sha=expected_sha,
        root=str(ROOT).replace("'", "''"),
        state=str(state).replace("'", "''"),
        entry=str(ROOT / "scripts" / "run_alphaops_morning.ps1").replace("'", "''"),
        refresh=str(ROOT / "scripts" / "refresh_luna_core_universe.py").replace("'", "''"),
        service=str(
            ROOT / "intraday_scanner" / "services" / "luna_core_universe_service.py"
        ).replace("'", "''"),
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SWAP_BLOCKED" in result.stdout
    assert "REFRESH_SWAP_BLOCKED" in result.stdout
    assert "SERVICE_SWAP_BLOCKED" in result.stdout


@pytest.mark.skipif(
    not Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe").is_file(),
    reason="exact Git source inventory proof requires Windows PowerShell 5.1",
)
def test_luna_source_inventory_rejects_a_tracked_service_hidden_from_disk() -> None:
    target = ROOT / "intraday_scanner" / "services" / "luna_core_universe_service.py"
    hidden = target.with_name("luna_core_universe_service.py.hostile-hidden")
    assert target.is_file()
    assert not hidden.exists()
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    target.replace(hidden)
    try:
        script = """
        $ErrorActionPreference = 'Stop'
        . '{runner}'
        $script:DawnstrikeProtectedGitBoundaryContract = [pscustomobject]@{{
            git_path=(Get-Command git.exe -ErrorAction Stop).Path
            git_sha256=('a' * 64); git_boundary_manifest_path='{runner}'
            git_boundary_manifest_sha256=('b' * 64)
        }}
        Get-DawnstrikeLunaCoreSourceFiles -ExpectedSha '{sha}' | Out-Null
        """.format(
            runner=str(ROOT / "scripts" / "dawnstrike_process_runner.ps1").replace("'", "''"),
            sha=expected_sha,
        )
        result = subprocess.run(
            ["powershell.exe", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "filesystem inventory differs from exact Git" in result.stderr
    finally:
        hidden.replace(target)

    script = """
    $ErrorActionPreference = 'Stop'
    . '{runner}'
    $script:DawnstrikeProtectedGitBoundaryContract = [pscustomobject]@{{
        git_path=(Get-Command git.exe -ErrorAction Stop).Path
        git_sha256=('a' * 64); git_boundary_manifest_path='{runner}'
        git_boundary_manifest_sha256=('b' * 64)
    }}
    $files = @(Get-DawnstrikeLunaCoreSourceFiles -ExpectedSha '{sha}')
    if ($files -cnotcontains 'intraday_scanner/services/luna_core_universe_service.py') {{
        throw 'tracked service absent from exact source inventory'
    }}
    """.format(
        runner=str(ROOT / "scripts" / "dawnstrike_process_runner.ps1").replace("'", "''"),
        sha=expected_sha,
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_inline_task_action_is_guarded_and_not_mutable_file_execution() -> None:
    text = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(encoding="utf-8")
    assert "Get-DawnstrikeScheduledLaunchCommand" in text
    assert "[IO.FileShare]::Read" in text
    assert "-Command" in text
    assert "-LaunchManifestSha256" in text


def test_protected_code_roots_are_distinct_from_mutable_runtime_work_roots() -> None:
    finalize = (ROOT / "scripts" / "run_daily_finalize.ps1").read_text(encoding="utf-8")
    publisher = (ROOT / "scripts" / "publish_vercel_public.ps1").read_text(encoding="utf-8")
    build = (ROOT / "scripts" / "build_vercel_public_stage.ps1").read_text(encoding="utf-8")
    verify = (ROOT / "scripts" / "verify_vercel_candidate.ps1").read_text(encoding="utf-8")
    assert 'Join-Path $codeRoot "scripts\\publish_vercel_public.ps1"' in finalize
    assert 'Join-Path $runtime "scripts\\publish_vercel_public.ps1"' not in finalize
    for text in (publisher, build, verify):
        assert "$codeRoot" in text
        assert "Get-DawnstrikeProtectedReleaseRoot" in text
        assert "-ReleaseRoot $codeRoot" in text
    assert 'Join-Path $codeRoot "scripts\\build_vercel_public_stage.ps1"' in publisher
    assert 'Join-Path $codeRoot "scripts\\verify_vercel_candidate.ps1"' in publisher
    assert 'Join-Path $codeRoot "scripts\\verify_public_artifact.py"' in verify

    for name in ("run_alphaops_morning.ps1", "run_alphaops_eod.ps1"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert 'Join-Path $codeRoot "scripts\\state_disaster_recovery.py"' in text
        assert 'Join-Path $runtime "scripts\\state_disaster_recovery.py"' not in text


def test_fixed_in_root_toolchain_seals_bind_launch_manifests_and_receipts() -> None:
    runner = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "dawnstrike_release_launcher.ps1").read_text(encoding="utf-8")
    for marker in (
        ".dawnstrike-git-boundary-v1.json",
        ".dawnstrike-python-boundary-v1.json",
        ".dawnstrike-dependency-boundary-v1.json",
        "git_boundary_manifest_sha256",
        "python_boundary_manifest_sha256",
        "dependency_manifest_sha256",
    ):
        assert marker in runner
        assert marker in launcher
    for marker in (
        ".dawnstrike-vercel-cli-boundary-v1.json",
        "Get-DawnstrikeProtectedVercelBoundaryContract",
        "vercel_cli_root",
        "vercel_cli_tree_sha256",
        "vercel_cli_boundary_manifest_sha256",
    ):
        assert marker in runner
    assert "Get-DawnstrikeProtectedGitBoundaryContract" in runner
    assert "Open-DawnstrikeLauncherGitBoundary" in launcher
    assert "source_release_sha" not in runner
    assert "source_release_sha" not in launcher
    assert "dependencyRoot '.dawnstrike-dependency-boundary-v1.json'" in runner
    assert "dependencyRoot '.dawnstrike-dependency-boundary-v1.json'" in launcher


def test_no_script_executes_from_the_retired_shared_bin_namespace() -> None:
    retired = r"C:\Program Files\Dawnstrike\bin"
    for script in (ROOT / "scripts").glob("*"):
        if script.suffix.casefold() not in {".ps1", ".py"}:
            continue
        assert retired not in script.read_text(encoding="utf-8"), script


def test_manual_production_operations_require_the_protected_launcher() -> None:
    bootstrap = (ROOT / "scripts" / "bootstrap_luna_core_universe.ps1").read_text(encoding="utf-8")
    recovery = (ROOT / "scripts" / "recover_vercel_publication.ps1").read_text(encoding="utf-8")
    launcher = (ROOT / "scripts" / "dawnstrike_release_launcher.ps1").read_text(encoding="utf-8")
    for text in (bootstrap, recovery):
        assert r"C:\Program Files\Dawnstrike\releases" in text
        assert r"scripts\dawnstrike_release_launcher.ps1" in text
        assert "$MyInvocation.ScriptName" in text
        assert "$ProtectedLauncherGrant" in text
        assert "requires an elevated administrator process" in text
    assert "-AdditionalSourceFiles" in bootstrap
    assert "Get-DawnstrikeLunaCoreSourceFiles" in bootstrap
    assert "Enter-DawnstrikeDailyRunLock" in bootstrap
    assert "Exit-DawnstrikeDailyRunLock" in bootstrap
    assert "--bootstrap-state-street-proxy" in bootstrap
    assert "Import-DawnstrikeEnvironment" in recovery
    assert "-RecoveryOnly" in recovery
    assert "-SuppressNativeConsoleReplay" in recovery
    assert "-Promote" not in recovery
    for marker in (
        "'BootstrapUniverse'",
        "'RecoverPublication'",
        "scripts\\protected_operation_contract.ps1",
        "scripts\\bootstrap_luna_core_universe.ps1",
        "scripts\\recover_vercel_publication.ps1",
        "scripts\\vercel_toolchain_contract.py",
        "ls-tree', '-r', '--name-only', $ExpectedSha",
        "-ProtectedLauncherGrant",
        "Protected exact-SHA release root",
        "requires Windows PowerShell 5.1 Desktop",
    ):
        assert marker in launcher


def test_primary_operator_docs_forbid_checkout_task_registration() -> None:
    documents = (
        ROOT / "README.md",
        ROOT / "docs" / "OPERATOR_MANUAL.md",
        ROOT / "docs" / "OPERATOR_RUNBOOK.md",
        ROOT / "docs" / "DAWNSTRIKE_EXPLAINED.md",
        ROOT / "docs" / "TROUBLESHOOTING.md",
        ROOT / "docs" / "operations" / "daily_finalize_runbook.md",
        ROOT / "docs" / "operations" / "alphaops_v6_release_runbook.md",
        ROOT / "docs" / "operations" / "dawnstrike_v5_release.md",
    )
    direct_registration = (
        "powershell -ExecutionPolicy Bypass -File scripts\\register_alphaops_tasks.ps1"
    )
    for document in documents:
        text = document.read_text(encoding="utf-8")
        assert direct_registration not in text
        assert "runtime_activation_and_rollback.md" in text


@pytest.mark.skipif(
    not Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe").is_file(),
    reason="protected-operation direct-call proof requires Windows PowerShell 5.1",
)
@pytest.mark.parametrize(
    ("script_name", "error"),
    [
        (
            "bootstrap_luna_core_universe.ps1",
            "Core-universe bootstrap is restricted to the protected release launcher.",
        ),
        (
            "recover_vercel_publication.ps1",
            "Vercel publication recovery is restricted to the protected release launcher.",
        ),
    ],
)
def test_manual_production_operations_reject_direct_invocation(
    script_name: str, error: str
) -> None:
    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(ROOT / "scripts" / script_name),
            "-ExpectedSha",
            "a" * 40,
            "-MarketDate",
            "2026-09-03",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert completed.returncode != 0
    assert error in completed.stderr


def test_bootstrap_rejects_replace_refs_filters_and_dependency_reparse_points() -> None:
    text = (ROOT / "scripts" / "dawnstrike_python_bootstrap.py").read_text(encoding="utf-8")
    assert '"GIT_NO_REPLACE_OBJECTS": "1"' in text
    assert '"replace", "-l"' in text
    assert "contains a Git execution or transport configuration" in text
    assert "FILE_ATTRIBUTE_REPARSE_POINT" in text

    dependency = ROOT / "tests" / "_identity_dependency_target"
    link = ROOT / "tests" / "_identity_dependency_link"
    dependency.mkdir(exist_ok=True)
    try:
        link.symlink_to(dependency, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink creation is unavailable on this host")
    try:
        original = bootstrap.sysconfig.get_paths
        bootstrap.sysconfig.get_paths = lambda: {
            "purelib": str(link),
            "platlib": str(link),
        }
        with pytest.raises(RuntimeError, match="reparse point"):
            bootstrap._append_governed_dependencies()
    finally:
        bootstrap.sysconfig.get_paths = original
        link.unlink(missing_ok=True)
        dependency.rmdir()


def test_scheduler_doctor_requires_protected_terminal_action_authority() -> None:
    text = (ROOT / "intraday_scanner/services/scheduler_doctor_service.py").read_text(
        encoding="utf-8"
    )
    assert "_load_protected_scheduler_authority(runtime, state)" in text
    assert "_canonical_task_contract_snapshot(task_rows)" in text
    assert "canonical_task_action_contract_matches" in text
    assert "canonical_task_definition_contract_matches" in text
    assert "Assert-DawnstrikeStateRootBoundary" in text
    assert "Open-DawnstrikeStateBoundaryRuntimeAuthorizationEvidence" in text
    assert "_scheduled_guard_action_matches" not in text
    assert "_expected_action_arguments" not in text
    assert r"C:\Program Files\Dawnstrike\releases" in text
    assert r"C:\r\dawnstrike-runtime\scripts\run_alphaops_morning.ps1" not in text
    assert scheduler.EXPECTED_TASK_EXECUTABLE.casefold() == (
        r"c:\windows\system32\windowspowershell\v1.0\powershell.exe"
    )
