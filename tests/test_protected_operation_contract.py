from __future__ import annotations

import json
import shutil
import subprocess
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")


pytestmark = pytest.mark.skipif(
    POWERSHELL is None,
    reason="protected-operation contracts require Windows PowerShell 5.1",
)


def _ps_quote(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _run_contract(body: str) -> subprocess.CompletedProcess[str]:
    helper = ROOT / "scripts" / "protected_operation_contract.ps1"
    script = f"""
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    . {_ps_quote(helper)}
    try {{
        {body}
        [Console]::Out.WriteLine('PASS')
    }} catch {{
        [Console]::Error.WriteLine($_.Exception.Message)
        exit 17
    }}
    """
    return subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )


def _task_snapshots(window: str = "before") -> tuple[str, list[dict[str, str]]]:
    names = [
        "Dawnstrike AlphaOps Morning",
        "Dawnstrike AlphaOps Monitor 5m",
        "Dawnstrike AlphaOps EOD Full Report",
        "Dawnstrike AlphaOps V6 Weekly Training",
        "Dawnstrike 10of10 Daily Finalize",
    ]
    now = "2026-09-03T07:00:00"
    snapshots = [
        {
            "name": name,
            "state": "Ready",
            "next_run_time": "2026-09-04T08:00:00",
            "last_run_time": "2026-09-02T08:00:00",
        }
        for name in names
    ]
    snapshots[0]["next_run_time"] = "2026-09-03T08:00:00"
    if window == "mid":
        now = "2026-09-03T12:00:00"
        snapshots[0]["next_run_time"] = "2026-09-04T08:00:00"
        snapshots[0]["last_run_time"] = "2026-09-03T08:00:00"
    elif window == "after":
        now = "2026-09-03T18:00:00"
        snapshots[0]["next_run_time"] = "2026-09-04T08:00:00"
        snapshots[0]["last_run_time"] = "2026-09-03T08:00:00"
        snapshots[2]["last_run_time"] = "2026-09-03T15:15:00"
        snapshots[4]["last_run_time"] = "2026-09-03T17:30:00"
    elif window == "out_of_order":
        now = "2026-09-03T18:00:00"
        snapshots[0]["next_run_time"] = "2026-09-04T08:00:00"
        snapshots[0]["last_run_time"] = "2026-09-03T08:00:00"
        snapshots[2]["last_run_time"] = "2026-09-03T15:15:00"
        snapshots[4]["last_run_time"] = "2026-09-03T10:00:00"
    return now, snapshots


def _run_boundary(
    *,
    window: str = "before",
    market_date: str = "2026-09-03",
    capture_state: str | None = None,
    task_state: str | None = None,
) -> subprocess.CompletedProcess[str]:
    now, snapshots = _task_snapshots(window)
    if task_state is not None:
        snapshots[1]["state"] = task_state
    task_json = json.dumps(snapshots, separators=(",", ":"))
    if capture_state is None:
        capture_expression = "@()"
    else:
        capture_json = json.dumps([{"state": capture_state}], separators=(",", ":"))
        capture_expression = (
            f"@((ConvertFrom-Json @'\n{capture_json}\n'@) | ForEach-Object {{ $_ }})"
        )
    return _run_contract(
        f"""
        $tasks = @((ConvertFrom-Json @'
{task_json}
'@) | ForEach-Object {{ $_ }})
        $captures = {capture_expression}
        $null = Assert-DawnstrikeUniverseBootstrapBoundarySnapshot `
            -RequestedMarketDate '{market_date}' `
            -Now ([DateTime]'{now}') `
            -CanonicalTasks $tasks `
            -CaptureTasks $captures
        """
    )


@pytest.mark.parametrize("capture_state", [None, "Ready", "Disabled"])
def test_universe_bootstrap_boundary_accepts_quiescent_capture_states(
    capture_state: str | None,
) -> None:
    result = _run_boundary(capture_state=capture_state)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "PASS"


def test_universe_bootstrap_boundary_accepts_post_finalizer_window() -> None:
    result = _run_boundary(window="after")
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"capture_state": "Queued"}, "absent or quiescent"),
        ({"task_state": "Running"}, "unique Ready canonical task"),
        ({"market_date": "2026-09-04"}, "host current date"),
        ({"market_date": "2026-02-30"}, "real canonical calendar date"),
        ({"window": "mid"}, "pre-Morning or post-finalizer"),
        ({"window": "out_of_order"}, "pre-Morning or post-finalizer"),
    ],
)
def test_universe_bootstrap_boundary_rejects_unsafe_snapshots(
    kwargs: dict[str, str], error: str
) -> None:
    result = _run_boundary(**kwargs)
    assert result.returncode != 0
    assert error in result.stderr


def _aliases() -> list[str]:
    return [
        "https://dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app",
        "https://dawnstrike-command-center-x3-mattfrens-projects.vercel.app",
        "https://dawnstrike-command-center-x3.vercel.app",
    ]


def _run_recovery_result(result: dict[str, object]) -> subprocess.CompletedProcess[str]:
    payload = json.dumps(result, separators=(",", ":"))
    expected_aliases = json.dumps(_aliases(), separators=(",", ":"))
    return _run_contract(
        f"""
        $result = ConvertFrom-Json @'
{payload}
'@
        $aliases = @((ConvertFrom-Json @'
{expected_aliases}
'@) | ForEach-Object {{ $_ }})
        $null = Assert-DawnstrikeVercelRecoveryResult `
            -Result $result `
            -ExpectedSha ('a' * 40) `
            -ExpectedMarketDate '2026-09-03' `
            -ExpectedProjectId 'prj_5pef3EZF1u5YadebEz3dFjnkWOXy' `
            -ExpectedProjectName 'dawnstrike-command-center-x3' `
            -ExpectedProviderScope 'mattfrens-projects' `
            -ExpectedAliases $aliases
        """
    )


def _daily_result() -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.daily_deployment.v1",
        "status": "PRODUCTION_VERIFIED",
        "market_date": "2026-09-03",
        "expected_market_date": "2026-09-03",
        "source_sha": "a" * 40,
        "project_id": "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
        "provider_scope": "mattfrens-projects",
        "production_aliases": _aliases(),
        "promoted": True,
        "readiness_http_status": 200,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_complete_publication_recovery_result_is_accepted() -> None:
    result = _run_recovery_result(_daily_result())
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("status", "DEGRADED"),
        ("market_date", "9999-99-99"),
        ("project_id", "prj_foreign"),
        ("provider_scope", "foreign-scope"),
        ("production_aliases", ["https://foreign.example"]),
        ("source_sha", "b" * 40),
        ("promoted", False),
        ("readiness_http_status", 503),
    ],
)
def test_complete_publication_recovery_result_rejects_wrong_identity(
    field: str, value: object
) -> None:
    payload = deepcopy(_daily_result())
    payload[field] = value
    result = _run_recovery_result(payload)
    assert result.returncode != 0


def test_recovery_only_result_binds_full_governed_target() -> None:
    payload = {
        "schema_version": "dawnstrike.vercel_publication_recovery.v1",
        "status": "NO_NONTERMINAL_CURRENT_OPERATION",
        "market_date": "2026-09-03",
        "project_id": "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
        "project_name": "dawnstrike-command-center-x3",
        "provider_scope": "mattfrens-projects",
        "production_aliases": _aliases(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result = _run_recovery_result(payload)
    assert result.returncode == 0, result.stderr
    payload["project_name"] = "foreign-project"
    result = _run_recovery_result(payload)
    assert result.returncode != 0


@pytest.mark.parametrize("status", ["ARCHIVED_COMPENSATED", "COMPENSATED"])
def test_compensated_recovery_result_requires_exact_archive_identity(status: str) -> None:
    payload = {
        "schema_version": "dawnstrike.vercel_publication_recovery.v1",
        "status": status,
        "market_date": "2026-09-03",
        "project_id": "prj_5pef3EZF1u5YadebEz3dFjnkWOXy",
        "project_name": "dawnstrike-command-center-x3",
        "provider_scope": "mattfrens-projects",
        "production_aliases": _aliases(),
        "archived_journal_sha256": "c" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    result = _run_recovery_result(payload)
    assert result.returncode == 0, result.stderr
    payload["archived_journal_sha256"] = "invalid"
    result = _run_recovery_result(payload)
    assert result.returncode != 0


def test_recovery_result_rejects_noncanonical_alias_order() -> None:
    payload = _daily_result()
    payload["production_aliases"] = list(reversed(_aliases()))
    result = _run_recovery_result(payload)
    assert result.returncode != 0


def test_bootstrap_rechecks_boundary_under_owned_daily_lock() -> None:
    text = (ROOT / "scripts" / "bootstrap_luna_core_universe.ps1").read_text(encoding="utf-8")
    calls = [
        index
        for index in range(len(text))
        if text.startswith(
            "Assert-DawnstrikeUniverseBootstrapBoundary -RequestedMarketDate",
            index,
        )
    ]
    enter = text.index("$dailyLock = Enter-DawnstrikeDailyRunLock")
    try_block = text.index("try {", enter)
    assert len(calls) == 2
    assert calls[0] < enter < try_block < calls[1]
    assert "-SuppressConsoleReplay" in text


def test_native_process_console_replay_can_be_suppressed(tmp_path: Path) -> None:
    runner = ROOT / "scripts" / "dawnstrike_process_runner.ps1"
    script = f"""
    $ErrorActionPreference = 'Stop'
    . {_ps_quote(runner)}
    $receipt = Invoke-DawnstrikeNativeProcess `
        -FilePath 'cmd.exe' `
        -ArgumentList @('/d', '/c', 'echo child-output') `
        -LogRoot {_ps_quote(tmp_path)} `
        -LogName 'single-json-proof' `
        -WorkingDirectory {_ps_quote(ROOT)} `
        -TimeoutSeconds 30 `
        -SuppressConsoleReplay
    [Console]::Out.WriteLine(('WRAPPER:' + ($receipt | ConvertTo-Json -Compress)))
    """
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        line for line in result.stdout.splitlines() if line.startswith("WRAPPER:")
    ]
    assert len(result.stdout.splitlines()) == 1
    assert "child-output" not in result.stdout
    assert "child-output" in (tmp_path / "single-json-proof.stdout.log").read_text()


def test_recovery_launcher_holds_toolchain_helper_bytes() -> None:
    launcher = ROOT / "scripts" / "dawnstrike_release_launcher.ps1"
    toolchain = ROOT / "scripts" / "vercel_toolchain_contract.py"
    expected_sha = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    script = f"""
    $ErrorActionPreference = 'Stop'
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        {_ps_quote(launcher)}, [ref]$tokens, [ref]$errors
    )
    if ($errors.Count -ne 0) {{ throw 'launcher parse failed' }}
    $required = @(
        'Get-DawnstrikeNormalizedBlobSha1',
        'Invoke-DawnstrikeLauncherGit',
        'Get-DawnstrikeLauncherGitDirectory',
        'Assert-DawnstrikeLauncherEntryPath',
        'Open-DawnstrikeLauncherEntry'
    )
    foreach ($name in $required) {{
        $matches = @($ast.FindAll({{
            param($node)
            $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
                [string]$node.Name -ceq $name
        }}, $true))
        if ($matches.Count -ne 1) {{ throw "missing launcher function: $name" }}
        Invoke-Expression ([string]$matches[0].Extent.Text)
    }}
    $script:DawnstrikeReleaseGitPath = 'C:\\Program Files\\Git\\cmd\\git.exe'
    $original = [IO.File]::ReadAllBytes({_ps_quote(toolchain)})
    $wrote = $false
    $lock = Open-DawnstrikeLauncherEntry `
        -Root {_ps_quote(ROOT)} `
        -Sha '{expected_sha}' `
        -RelativePath 'scripts/vercel_toolchain_contract.py'
    try {{
        try {{
            [IO.File]::WriteAllText({_ps_quote(toolchain)}, 'hostile replacement')
            $wrote = $true
        }} catch {{}}
    }} finally {{
        $lock.stream.Dispose()
        if ($wrote) {{ [IO.File]::WriteAllBytes({_ps_quote(toolchain)}, $original) }}
    }}
    if ($wrote) {{ throw 'launcher toolchain lock permitted replacement' }}
    """
    result = subprocess.run(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(shutil.which("pwsh") is None, reason="PowerShell 7 is unavailable")
def test_release_launcher_rejects_powershell_7_before_candidate_admission() -> None:
    launcher = ROOT / "scripts" / "dawnstrike_release_launcher.ps1"
    result = subprocess.run(
        [
            str(shutil.which("pwsh")),
            "-NoProfile",
            "-File",
            str(launcher),
            "-Mode",
            "RecoverPublication",
            "-ExpectedSha",
            "a" * 40,
            "-CandidateRoot",
            str(ROOT),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode != 0
    assert "requires Windows PowerShell 5.1 Desktop" in result.stderr


def test_release_launcher_rejects_reparse_parent_before_open(
    tmp_path: Path,
) -> None:
    root = tmp_path / "candidate"
    real = tmp_path / "outside"
    link = root / "scripts"
    root.mkdir()
    real.mkdir()
    (real / "entry.ps1").write_text("'safe'", encoding="utf-8")
    try:
        link.symlink_to(real, target_is_directory=True)
    except (OSError, NotImplementedError):
        junction = subprocess.run(
            [
                str(POWERSHELL),
                "-NoProfile",
                "-Command",
                (
                    "& { param($linkPath, $targetPath) "
                    "New-Item -ItemType Junction -Path $linkPath "
                    "-Target $targetPath -ErrorAction Stop | Out-Null }"
                ),
                str(link),
                str(real),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if junction.returncode != 0:
            pytest.skip("directory link creation is unavailable on this host")

    launcher = ROOT / "scripts" / "dawnstrike_release_launcher.ps1"
    script = f"""
    $ErrorActionPreference = 'Stop'
    $tokens = $null
    $errors = $null
    $ast = [System.Management.Automation.Language.Parser]::ParseFile(
        {_ps_quote(launcher)}, [ref]$tokens, [ref]$errors
    )
    $matches = @($ast.FindAll({{
        param($node)
        $node -is [System.Management.Automation.Language.FunctionDefinitionAst] -and
            [string]$node.Name -ceq 'Assert-DawnstrikeLauncherEntryPath'
    }}, $true))
    if ($matches.Count -ne 1) {{ throw 'missing launcher path contract' }}
    Invoke-Expression ([string]$matches[0].Extent.Text)
    Assert-DawnstrikeLauncherEntryPath `
        -Root {_ps_quote(root)} `
        -RelativePath 'scripts/entry.ps1' | Out-Null
    """
    try:
        result = subprocess.run(
            [str(POWERSHELL), "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode != 0
        assert "contains a reparse point" in result.stderr
    finally:
        try:
            link.unlink()
        except OSError:
            link.rmdir()
