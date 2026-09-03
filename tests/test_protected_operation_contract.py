from __future__ import annotations

import ctypes
import json
import os
import shutil
import struct
import subprocess
import time
from copy import deepcopy
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell.exe")
IS_ADMIN = bool(os.name == "nt" and ctypes.windll.shell32.IsUserAnAdmin())


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


def _make_directory_reparse(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=True)
        return
    except (OSError, NotImplementedError):
        result = subprocess.run(
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
                str(target),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            pytest.skip("directory reparse creation is unavailable on this host")


def _remove_directory_reparse(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        path.rmdir()


def _attempt_in_place_mount_point(path: Path, target: Path) -> tuple[bool, int]:
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = [
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    ]
    create_file.restype = wintypes.HANDLE
    device_io = kernel32.DeviceIoControl
    device_io.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
        wintypes.LPVOID,
    ]
    device_io.restype = wintypes.BOOL
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = [wintypes.HANDLE]
    close_handle.restype = wintypes.BOOL
    handle = create_file(
        str(path),
        0x40000000,
        0x1 | 0x2 | 0x4,
        None,
        3,
        0x02000000 | 0x00200000,
        None,
    )
    if handle == ctypes.c_void_p(-1).value:
        return False, ctypes.get_last_error()
    try:
        substitute = ("\\??\\" + str(target)).encode("utf-16-le")
        display = str(target).encode("utf-16-le")
        path_buffer = substitute + b"\x00\x00" + display + b"\x00\x00"
        reparse_data = (
            struct.pack(
                "<IHHHHHH",
                0xA0000003,
                8 + len(path_buffer),
                0,
                0,
                len(substitute),
                len(substitute) + 2,
                len(display),
            )
            + path_buffer
        )
        buffer = ctypes.create_string_buffer(reparse_data)
        returned = wintypes.DWORD()
        converted = bool(
            device_io(
                handle,
                0x000900A4,
                buffer,
                len(reparse_data),
                None,
                0,
                ctypes.byref(returned),
                None,
            )
        )
        error = 0 if converted else ctypes.get_last_error()
        if converted:
            delete_data = struct.pack("<IHH", 0xA0000003, 0, 0)
            delete_buffer = ctypes.create_string_buffer(delete_data)
            assert device_io(
                handle,
                0x000900AC,
                delete_buffer,
                len(delete_data),
                None,
                0,
                ctypes.byref(returned),
                None,
            )
        return converted, error
    finally:
        close_handle(handle)


def _task_snapshots(window: str = "after") -> tuple[str, list[dict[str, str]]]:
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
    elif window == "pending_finalizer_retry":
        now = "2026-09-03T18:00:00"
        snapshots[0]["next_run_time"] = "2026-09-04T08:00:00"
        snapshots[0]["last_run_time"] = "2026-09-03T08:00:00"
        snapshots[2]["last_run_time"] = "2026-09-03T15:15:00"
        snapshots[4]["last_run_time"] = "2026-09-03T17:30:00"
        snapshots[4]["next_run_time"] = "2026-09-03T17:45:00"
    elif window == "near_morning":
        now = "2026-09-03T07:54:01"
    elif window in {"monday_before_weekly", "monday_after_weekly"}:
        now = (
            "2026-09-07T20:59:00"
            if window == "monday_before_weekly"
            else "2026-09-07T22:00:00"
        )
        for snapshot in snapshots:
            snapshot["next_run_time"] = "2026-09-08T08:00:00"
            snapshot["last_run_time"] = "2026-09-06T08:00:00"
        snapshots[2]["last_run_time"] = "2026-09-07T15:15:00"
        snapshots[4]["last_run_time"] = "2026-09-07T17:30:00"
        snapshots[3]["next_run_time"] = (
            "2026-09-07T21:00:00"
            if window == "monday_before_weekly"
            else "2026-09-14T21:00:00"
        )
        snapshots[3]["last_run_time"] = (
            "2026-08-31T21:00:00"
            if window == "monday_before_weekly"
            else "2026-09-07T21:00:00"
        )
    return now, snapshots


def _run_boundary(
    *,
    window: str = "after",
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


def test_universe_bootstrap_boundary_accepts_only_after_monday_weekly() -> None:
    blocked = _run_boundary(window="monday_before_weekly", market_date="2026-09-07")
    accepted = _run_boundary(window="monday_after_weekly", market_date="2026-09-07")
    assert blocked.returncode != 0
    assert "pending same-day canonical trigger" in blocked.stderr
    assert accepted.returncode == 0, accepted.stderr


@pytest.mark.parametrize(
    ("kwargs", "error"),
    [
        ({"capture_state": "Queued"}, "absent or quiescent"),
        ({"task_state": "Running"}, "unique Ready canonical task"),
        ({"market_date": "2026-09-04"}, "host current date"),
        ({"market_date": "2026-02-30"}, "real canonical calendar date"),
        ({"window": "before"}, "pending same-day canonical trigger"),
        ({"window": "mid"}, "post-finalizer quiescent window"),
        ({"window": "out_of_order"}, "post-finalizer quiescent window"),
        ({"window": "near_morning"}, "pending same-day canonical trigger"),
        ({"window": "pending_finalizer_retry"}, "pending same-day canonical trigger"),
    ],
)
def test_universe_bootstrap_boundary_rejects_unsafe_snapshots(
    kwargs: dict[str, str], error: str
) -> None:
    result = _run_boundary(**kwargs)
    assert result.returncode != 0
    assert error in result.stderr


@pytest.mark.parametrize(
    "relative_link",
    [
        Path("logs"),
        Path("locks"),
        Path("config"),
        Path("config") / "luna_core_universe_generations",
    ],
)
def test_universe_state_boundary_rejects_descendant_reparse_points(
    tmp_path: Path,
    relative_link: Path,
) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    state.mkdir()
    outside.mkdir()
    link = state / relative_link
    link.parent.mkdir(parents=True, exist_ok=True)
    _make_directory_reparse(link, outside)

    try:
        result = _run_contract(
            f"$null = Assert-DawnstrikeUniverseStateBoundary -StateRoot {_ps_quote(state)}"
        )
        assert result.returncode != 0
        assert "contains a reparse point" in result.stderr
        assert list(outside.iterdir()) == []
    finally:
        _remove_directory_reparse(link)


def test_universe_log_boundary_rejects_junction_without_external_write(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    outside = tmp_path / "outside"
    logs = state / "logs"
    state.mkdir()
    outside.mkdir()
    sentinel = outside / "resolve_release_root.stdout.log"
    original = b"outside bytes must not change"
    sentinel.write_bytes(original)
    _make_directory_reparse(logs, outside)

    try:
        result = _run_contract(
            f"$null = Open-DawnstrikeUniverseBootstrapLogBoundary "
            f"-StateRoot {_ps_quote(state)} -MarketDate '2026-09-03'"
        )
        assert result.returncode != 0
        assert "log root contains a reparse point" in result.stderr
        assert sentinel.read_bytes() == original
        assert sorted(item.name for item in outside.iterdir()) == [sentinel.name]
    finally:
        _remove_directory_reparse(logs)


def test_daily_lock_write_boundary_blocks_in_place_reparse_and_preserves_lock_lifecycle(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    locks = state / "locks"
    outside = tmp_path / "outside"
    ready = tmp_path / "ready"
    release = tmp_path / "release"
    locks.mkdir(parents=True)
    outside.mkdir()
    sentinel = outside / "dawnstrike-daily-2026-09-03.lock"
    original = b"outside lock bytes must remain unchanged"
    sentinel.write_bytes(original)
    contract = ROOT / "scripts" / "protected_operation_contract.ps1"
    stage = ROOT / "scripts" / "invoke_dawnstrike_stage.ps1"
    script = f"""
    $ErrorActionPreference = 'Stop'
    Set-StrictMode -Version Latest
    . {_ps_quote(contract)}
    . {_ps_quote(stage)}
    $boundary = Open-DawnstrikeProtectedWriteDirectoryBoundary `
        -Path {_ps_quote(locks)} -Label 'Test daily lock root'
    try {{
        $lock = Enter-DawnstrikeDailyRunLock `
            -StateRoot {_ps_quote(state)} -MarketDate '2026-09-03' `
            -Owner 'luna_core_universe_bootstrap'
        if (-not $lock.acquired) {{ throw "Daily lock failed: $($lock.reason)" }}
        Exit-DawnstrikeDailyRunLock -Lock $lock
        if (Test-Path -LiteralPath ([string]$lock.lock_path)) {{
            throw 'Daily lock remained after governed release.'
        }}
        [IO.File]::WriteAllText({_ps_quote(ready)}, 'ready')
        $deadline = [DateTime]::UtcNow.AddSeconds(20)
        while (-not (Test-Path -LiteralPath {_ps_quote(release)})) {{
            if ([DateTime]::UtcNow -gt $deadline) {{ throw 'Test release signal timed out.' }}
            Start-Sleep -Milliseconds 25
        }}
    }} finally {{
        Close-DawnstrikeProtectedWriteDirectoryBoundary -Boundary $boundary
    }}
    [Console]::Out.WriteLine('PASS')
    """
    process = subprocess.Popen(
        [str(POWERSHELL), "-NoProfile", "-Command", script],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    converted = False
    error = 0
    ready_seen = False
    try:
        for _attempt in range(200):
            if ready.exists():
                ready_seen = True
                break
            if process.poll() is not None:
                break
            time.sleep(0.05)
        if ready_seen:
            converted, error = _attempt_in_place_mount_point(locks, outside)
    finally:
        release.write_text("release", encoding="utf-8")
        stdout, stderr = process.communicate(timeout=30)

    assert ready_seen, stderr
    assert process.returncode == 0, stderr
    assert "PASS" in stdout
    assert converted is False
    assert error == 145  # ERROR_DIR_NOT_EMPTY: the held marker cannot be removed.
    assert sentinel.read_bytes() == original
    assert list(locks.iterdir()) == []


def test_protected_directory_handle_denies_log_parent_replacement(
    tmp_path: Path,
) -> None:
    logs = tmp_path / "logs"
    moved = tmp_path / "logs.moved"
    logs.mkdir()
    result = _run_contract(
        f"""
        $handle = Open-DawnstrikeProtectedDirectoryHandle `
            -Path {_ps_quote(logs)} -Label 'Hostile log root'
        try {{
            $renamed = $false
            try {{
                [IO.Directory]::Move({_ps_quote(logs)}, {_ps_quote(moved)})
                $renamed = $true
            }} catch {{}}
            if ($renamed) {{ throw 'Protected log root was replaceable while held.' }}
            $writeHandle = [Dawnstrike.Security.ProtectedDirectoryNative]::CreateFileW(
                {_ps_quote(logs)},
                [uint32]1073741824,
                [uint32]0x00000007,
                [IntPtr]::Zero,
                [uint32]3,
                [uint32]0x02200000,
                [IntPtr]::Zero
            )
            try {{
                if ($null -ne $writeHandle -and -not $writeHandle.IsInvalid) {{
                    throw 'Protected log root admitted a competing write handle.'
                }}
            }} finally {{
                if ($null -ne $writeHandle) {{ $writeHandle.Dispose() }}
            }}
            [IO.File]::WriteAllText(
                (Join-Path {_ps_quote(logs)} 'authorized.log'),
                'authorized'
            )
        }} finally {{ $handle.Dispose() }}
        """
    )
    assert result.returncode == 0, result.stderr
    assert (logs / "authorized.log").read_text(encoding="utf-8") == "authorized"
    assert not moved.exists()


def test_universe_log_boundary_applies_privileged_acl_atomically() -> None:
    text = (ROOT / "scripts" / "protected_operation_contract.ps1").read_text(encoding="utf-8")
    function_start = text.index("function Open-DawnstrikeUniverseBootstrapLogBoundary")
    acl = text.index("$operationAcl = New-DawnstrikeUniverseLogDirectorySecurity", function_start)
    create = text.index(
        "[IO.Directory]::CreateDirectory($operationRoot, $operationAcl)",
        acl,
    )
    handle = text.index("$operationHandle = Open-DawnstrikeProtectedDirectoryHandle", create)
    assert function_start < acl < create < handle
    assert "Set-Acl -LiteralPath $operationRoot" not in text[function_start:handle]


def test_universe_log_boundary_holds_daily_lock_write_boundary_until_close() -> None:
    text = (ROOT / "scripts" / "protected_operation_contract.ps1").read_text(encoding="utf-8")
    function_start = text.index("function Open-DawnstrikeUniverseBootstrapLogBoundary")
    locks_open = text.index(
        "$locksBoundary = Open-DawnstrikeProtectedWriteDirectoryBoundary",
        function_start,
    )
    logs_open = text.index("$logsHandle = Open-DawnstrikeProtectedDirectoryHandle", locks_open)
    returned = text.index("locks_boundary = $locksBoundary", logs_open)
    close_start = text.index("function Close-DawnstrikeUniverseBootstrapLogBoundary", returned)
    locks_close = text.index(
        "Close-DawnstrikeProtectedWriteDirectoryBoundary -Boundary $Boundary.locks_boundary",
        close_start,
    )
    state_close = text.index("$Boundary.state_handle.Dispose()", locks_close)
    assert (
        function_start < locks_open < logs_open < returned < close_start < locks_close < state_close
    )


def test_universe_log_acl_builder_emits_one_directory_security_object() -> None:
    result = _run_contract(
        """
        $items = @(New-DawnstrikeUniverseLogDirectorySecurity)
        if ($items.Count -ne 1 -or
            $items[0] -isnot [Security.AccessControl.DirectorySecurity]) {
            throw 'Protected log ACL builder emitted an ambiguous result.'
        }
        """
    )
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not IS_ADMIN, reason="protected log ACL creation requires elevation")
def test_universe_log_boundary_creates_a_protected_per_invocation_root(
    tmp_path: Path,
) -> None:
    state = tmp_path / "state"
    state.mkdir()
    result = _run_contract(
        f"""
        $boundary = Open-DawnstrikeUniverseBootstrapLogBoundary `
            -StateRoot {_ps_quote(state)} -MarketDate '2026-09-03'
        try {{
            if (-not ([string]$boundary.path).StartsWith(
                (Join-Path {_ps_quote(state)} 'logs\\protected-luna-core-bootstrap-2026-09-03-'),
                [StringComparison]::OrdinalIgnoreCase
            )) {{ throw 'Protected log root path is not canonical.' }}
            [IO.File]::WriteAllText(
                (Join-Path ([string]$boundary.path) 'authorized.log'),
                'authorized'
            )
        }} finally {{
            Close-DawnstrikeUniverseBootstrapLogBoundary -Boundary $boundary
        }}
        """
    )
    assert result.returncode == 0, result.stderr


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
    task_calls = [
        index
        for index in range(len(text))
        if text.startswith(
            "Assert-DawnstrikeUniverseBootstrapBoundary -RequestedMarketDate",
            index,
        )
    ]
    state_calls = [
        index
        for index in range(len(text))
        if text.startswith("$state = Assert-DawnstrikeUniverseStateBoundary", index)
    ]
    log_open = text.index("$logBoundary = Open-DawnstrikeUniverseBootstrapLogBoundary")
    release_identity = text.index("$releaseSha = Resolve-DawnstrikeReleaseSha")
    enter = text.index("$dailyLock = Enter-DawnstrikeDailyRunLock")
    try_block = text.index("try {", enter)
    native_refresh = text.index("$refresh = Invoke-DawnstrikeNativeProcess", try_block)
    log_close = text.index("Close-DawnstrikeUniverseBootstrapLogBoundary", native_refresh)
    assert len(task_calls) == 2
    assert task_calls[0] < enter < try_block < task_calls[1]
    assert len(state_calls) == 2
    assert state_calls[0] < log_open < release_identity < enter
    assert try_block < state_calls[1] < native_refresh < log_close
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
