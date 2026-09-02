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


def test_registration_and_rollback_entries_are_pinned_and_sha_bound() -> None:
    absolute = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    for name in (
        "register_alphaops_tasks.ps1",
        "register_daily_finalize_task.ps1",
    ):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert absolute in text
        assert "New-ScheduledTaskAction" in text
        assert "-Execute $powershellExecutable" in text
        assert "-ExpectedSha" in text
        assert "$ExpectedSha" in text

    rollback = (ROOT / "scripts" / "restore_dawnstrike_tasks.ps1").read_text(encoding="utf-8")
    assert "[string]$ExpectedSha" in rollback
    assert "Assert-DawnstrikeProcessSourceBoundToHead" in rollback
    assert "Set-DawnstrikeCanonicalTaskExpectedSha" in rollback
    assert (
        "Assert-DawnstrikeCanonicalTaskSemantics -RuntimeRoot $runtime "
        "-StateRoot $state -ExpectedSha $ExpectedSha"
        in rollback
    )


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
    assert (
        "Assert-DawnstrikeProcessSourceBoundToHead -ReleaseRoot $runtimePath "
        "-ExpectedSha $ExpectedSha"
        in text
    )
    assert "Assert-DawnstrikePythonDependencyAclBoundary" in text
    assert "Get-Acl -LiteralPath" in text


def test_scheduled_python_requires_an_administrator_owned_program_files_boundary() -> None:
    text = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(
        encoding="utf-8"
    )
    for marker in (
        r"C:\Program Files\Dawnstrike\Python313\python.exe",
        "python313.dll",
        r"DLLs\_hashlib.pyd",
        r"Lib\hashlib.py",
        r"Scripts\uv.exe",
        "is not owned by an administrator principal",
        "is writable by a non-admin principal",
    ):
        assert marker in text
    assert "AzureAD" not in text
    assert "IdentityReference -notmatch" in text


def test_release_activation_uses_an_administrator_installed_preparse_launcher() -> None:
    launcher = (ROOT / "scripts" / "dawnstrike_release_launcher.ps1").read_text(
        encoding="utf-8"
    )
    installer = (
        ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1"
    ).read_text(encoding="utf-8")

    for marker in (
        r"C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1",
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
    ):
        assert marker in launcher
    assert launcher.index("Open-DawnstrikeLauncherEntry") < launcher.index(
        "& $entryLocks[0].path"
    )
    for marker in (
        "requires an elevated administrator process",
        r"C:\Program Files\Dawnstrike",
        r"C:\ProgramData\Dawnstrike",
        r"C:\Program Files\Dawnstrike\bin\install_dawnstrike_host_boundary.ps1",
        "https://www.python.org/ftp/python/3.13.14/python-3.13.14-amd64.exe",
        "c54d9b9bbb8a36e6489363ddd01139707fd781d72f1f9e90c7ec65d0061368e0",  # pragma: allowlist secret  # noqa: E501
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
    ):
        assert marker in installer


@pytest.mark.skipif(
    not Path(r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe").is_file(),
    reason="scheduled-entry file-lock proof requires Windows PowerShell 5.1",
)
def test_task_guard_holds_entry_and_helper_bytes_against_concurrent_swap(tmp_path: Path) -> None:
    state = tmp_path / "state"
    script = """
    $ErrorActionPreference = 'Stop'
    . '{runner}'
    $m = New-DawnstrikeScheduledLaunchManifest `
        -RuntimeRoot '{root}' -StateRoot '{state}' -ExpectedSha ('a' * 40) `
        -TaskScript 'run_alphaops_morning.ps1'
    $bound = Assert-DawnstrikeScheduledLaunchManifest `
        -RuntimeRoot '{root}' -StateRoot '{state}' -ExpectedSha ('a' * 40) `
        -TaskScript 'run_alphaops_morning.ps1' -ManifestPath $m.path `
        -ManifestSha256 $m.sha256 -EntryScript '{entry}'
    try {{
        try {{
            [IO.File]::WriteAllText('{entry}', 'hostile replacement')
            'SWAP_SUCCEEDED'
        }} catch {{ 'SWAP_BLOCKED' }}
    }} finally {{ foreach ($lock in $bound.locks) {{ $lock.Dispose() }} }}
    """.format(
        runner=str(ROOT / "scripts" / "dawnstrike_process_runner.ps1").replace("'", "''"),
        root=str(ROOT).replace("'", "''"),
        state=str(state).replace("'", "''"),
        entry=str(ROOT / "scripts" / "run_alphaops_morning.ps1").replace("'", "''"),
    )
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "SWAP_BLOCKED" in result.stdout


def test_inline_task_action_is_guarded_and_not_mutable_file_execution() -> None:
    text = (ROOT / "scripts" / "dawnstrike_process_runner.ps1").read_text(
        encoding="utf-8"
    )
    assert "Get-DawnstrikeScheduledLaunchCommand" in text
    assert "[IO.FileShare]::Read" in text
    assert "-Command" in text
    assert "-LaunchManifestSha256" in text


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


def test_scheduler_doctor_action_contract_contains_the_runtime_candidate_sha() -> None:
    expected_sha = "a" * 40
    args = scheduler._expected_action_arguments(
        "Dawnstrike AlphaOps Morning",
        expected_runner=Path(r"C:\r\dawnstrike-runtime\scripts\run_alphaops_morning.ps1"),
        runtime_root=Path(r"C:\r\dawnstrike-runtime"),
        state_root=Path(r"C:\r\dawnstrike-state"),
        expected_sha=expected_sha,
    )
    assert args.endswith(f'-ExpectedSha "{expected_sha}"')
    assert scheduler.EXPECTED_TASK_EXECUTABLE.casefold() == (
        r"c:\windows\system32\windowspowershell\v1.0\powershell.exe"
    )
