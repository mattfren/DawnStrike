from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("node.exe") is None,
    reason="The bounded process contract requires Windows PowerShell and Node.js.",
)

ROOT = Path(__file__).resolve().parents[1]
HELPER = ROOT / "scripts" / "dawnstrike_job_process.ps1"
NODE = shutil.which("node.exe") or "node.exe"


def _ps_literal(value: str | Path) -> str:
    return "'" + str(value).replace("'", "''") + "'"


def _ps_array(values: list[str | Path]) -> str:
    return "@(" + ",".join(_ps_literal(value) for value in values) + ")"


def _run_powershell(command: str, *, timeout: int = 30) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            command,
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
        timeout=timeout,
    )


def _invoke_command(
    arguments: list[str | Path],
    *,
    label: str,
    timeout_seconds: int,
    output_drain_timeout_seconds: int = 1,
    job_memory_limit_bytes: int | None = None,
    rss_limit_bytes: int | None = None,
    rss_sample_milliseconds: int | None = None,
) -> str:
    guard_options = ""
    if job_memory_limit_bytes is not None:
        guard_options += f" -JobMemoryLimitBytes {job_memory_limit_bytes}"
    if rss_limit_bytes is not None:
        guard_options += f" -ProcessTreeRssLimitBytes {rss_limit_bytes}"
    if rss_sample_milliseconds is not None:
        guard_options += f" -RssSampleMilliseconds {rss_sample_milliseconds}"
    return (
        f". {_ps_literal(HELPER)}; "
        "$ErrorActionPreference = 'Stop'; "
        "$result = Invoke-DawnstrikeJobProcess "
        f"-FilePath {_ps_literal(NODE)} "
        f"-ArgumentList {_ps_array(arguments)} "
        f"-WorkingDirectory {_ps_literal(ROOT)} "
        f"-Label {_ps_literal(label)} "
        f"-TimeoutSeconds {timeout_seconds} "
        f"-OutputDrainTimeoutSeconds {output_drain_timeout_seconds}{guard_options}; "
        "$result | ConvertTo-Json -Compress"
    )


def _failure_command(
    arguments: list[str | Path],
    *,
    label: str,
    timeout_seconds: int,
    output_drain_timeout_seconds: int = 1,
    job_memory_limit_bytes: int | None = None,
    rss_limit_bytes: int | None = None,
    rss_sample_milliseconds: int | None = None,
) -> str:
    invocation = _invoke_command(
        arguments,
        label=label,
        timeout_seconds=timeout_seconds,
        output_drain_timeout_seconds=output_drain_timeout_seconds,
        job_memory_limit_bytes=job_memory_limit_bytes,
        rss_limit_bytes=rss_limit_bytes,
        rss_sample_milliseconds=rss_sample_milliseconds,
    ).rsplit("; $result | ConvertTo-Json -Compress", maxsplit=1)[0]
    return (
        invocation.replace("$result = Invoke-DawnstrikeJobProcess", "try { "
        "$result = Invoke-DawnstrikeJobProcess")
        + "; throw 'expected the bounded runner to fail' "
        + "} catch { [pscustomobject]@{ Message = $_.Exception.ToString() } "
        + "| ConvertTo-Json -Compress }"
    )


def _write_child_fixture(tmp_path: Path, *, delay_ms: int = 2500) -> Path:
    child = tmp_path / "detached child.js"
    child.write_text(
        "const fs = require('fs');\n"
        "const marker = process.argv[2];\n"
        "fs.writeFileSync(marker, 'started');\n"
        "setTimeout(() => {\n"
        "  fs.writeFileSync(marker, 'survived');\n"
        "  process.exit(0);\n"
        f"}}, {delay_ms});\n",
        encoding="utf-8",
    )
    return child


def _assert_child_did_not_survive(marker: Path) -> None:
    assert marker.read_text(encoding="utf-8") == "started"
    time.sleep(3)
    assert marker.read_text(encoding="utf-8") == "started"


def test_job_runner_succeeds_and_round_trips_windows_arguments(tmp_path: Path) -> None:
    fixture = tmp_path / "echo argv.js"
    fixture.write_text(
        "process.stdout.write(JSON.stringify(process.argv.slice(2)));\n",
        encoding="utf-8",
    )
    expected = [
        "plain",
        "has space",
        "space and trailing slash\\",
        "space and two trailing slashes\\\\",
        'embedded"quote',
        'backslash\\"quote',
        "",
    ]

    completed = _run_powershell(
        _invoke_command([fixture, *expected], label="argument probe", timeout_seconds=5)
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ExitCode"] == 0
    assert payload["ActiveJobMembersAfterCleanup"] == 0
    assert json.loads(payload["Stdout"]) == expected
    assert payload["Stderr"] == ""
    assert payload["JobMemoryLimitBytes"] == 0
    assert payload["JobMemoryLimitReadbackBytes"] == 0
    assert payload["JobLimitFlags"] == 0x2000
    assert payload["ProcessTreeRssLimitBytes"] == 0
    assert payload["ProcessTreeRssMeasurementAvailable"] is False
    assert payload["ProcessTreeRssSamples"] == 0


def test_job_runner_reports_native_memory_and_rss_guard_readback(tmp_path: Path) -> None:
    fixture = tmp_path / "guard telemetry.js"
    fixture.write_text(
        "const buffer = Buffer.alloc(8 * 1024 * 1024, 1);\n"
        "setTimeout(() => process.exit(buffer[0] === 1 ? 0 : 9), 150);\n",
        encoding="utf-8",
    )

    completed = _run_powershell(
        _invoke_command(
            [fixture],
            label="guard telemetry probe",
            timeout_seconds=5,
            job_memory_limit_bytes=268435456,
            rss_limit_bytes=268435456,
            rss_sample_milliseconds=100,
        )
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ExitCode"] == 0
    assert payload["ActiveJobMembersAfterCleanup"] == 0
    assert payload["JobMemoryLimitBytes"] == 268435456
    assert payload["JobMemoryLimitReadbackBytes"] == 268435456
    assert payload["JobLimitFlags"] & 0x2200 == 0x2200
    assert payload["ProcessTreeRssLimitBytes"] == 268435456
    assert payload["ProcessTreeRssMeasurementAvailable"] is True
    assert payload["ProcessTreeRssSamples"] > 0
    assert payload["GuardFailure"] is None


def test_job_runner_rss_supervisor_terminates_noncooperative_process(tmp_path: Path) -> None:
    fixture = tmp_path / "rss pressure.js"
    fixture.write_text(
        "const buffer = Buffer.alloc(64 * 1024 * 1024, 1);\n"
        "setTimeout(() => process.exit(buffer[0] === 1 ? 0 : 9), 5000);\n",
        encoding="utf-8",
    )

    completed = _run_powershell(
        _failure_command(
            [fixture],
            label="rss supervisor probe",
            timeout_seconds=10,
            job_memory_limit_bytes=268435456,
            rss_limit_bytes=16 * 1024 * 1024,
            rss_sample_milliseconds=50,
        )
    )

    assert completed.returncode == 0, completed.stderr
    message = json.loads(completed.stdout)["Message"]
    assert "process-tree RSS cap exceeded" in message
    assert "active_job_members_after_cleanup=0" in message


def test_job_runner_rejects_partial_observer_guard_without_fallback(tmp_path: Path) -> None:
    fixture = tmp_path / "partial guard.js"
    fixture.write_text("process.exit(0);\n", encoding="utf-8")

    completed = _run_powershell(
        _failure_command(
            [fixture],
            label="partial guard probe",
            timeout_seconds=5,
            job_memory_limit_bytes=65536,
        )
    )

    assert completed.returncode == 0, completed.stderr
    message = json.loads(completed.stdout)["Message"]
    assert "requires positive memory, RSS, and sampling values together" in message


def test_job_runner_preserves_real_nonzero_exit_and_stderr(tmp_path: Path) -> None:
    fixture = tmp_path / "exit seven.js"
    fixture.write_text(
        "process.stderr.write('expected-stderr'); process.exit(7);\n",
        encoding="utf-8",
    )

    completed = _run_powershell(
        _invoke_command([fixture], label="nonzero probe", timeout_seconds=5)
    )

    assert completed.returncode == 0, completed.stderr
    payload = json.loads(completed.stdout)
    assert payload["ExitCode"] == 7
    assert payload["Stderr"] == "expected-stderr"
    assert payload["ActiveJobMembersAfterCleanup"] == 0


def test_job_runner_timeout_kills_detached_child_tree(tmp_path: Path) -> None:
    marker = tmp_path / "timeout-child-marker.txt"
    child = _write_child_fixture(tmp_path)
    root = tmp_path / "timeout root.js"
    root.write_text(
        "const fs = require('fs');\n"
        "const { spawn } = require('child_process');\n"
        "const child = spawn(process.execPath, [process.argv[2], process.argv[3]], {\n"
        "  detached: true, windowsHide: true, stdio: ['ignore', 'inherit', 'inherit']\n"
        "});\n"
        "const deadline = Date.now() + 500;\n"
        "while (!fs.existsSync(process.argv[3]) && Date.now() < deadline) {\n"
        "  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);\n"
        "}\n"
        "setTimeout(() => process.exit(0), 5000);\n",
        encoding="utf-8",
    )

    completed = _run_powershell(
        _failure_command(
            [root, child, marker],
            label="tree timeout probe",
            timeout_seconds=1,
        )
    )

    assert completed.returncode == 0, completed.stderr
    message = json.loads(completed.stdout)["Message"]
    assert "timed out after 1000 milliseconds" in message
    assert "active_job_members_after_cleanup=0" in message
    _assert_child_did_not_survive(marker)


def test_job_runner_drain_failure_kills_inherited_output_child(tmp_path: Path) -> None:
    marker = tmp_path / "drain-child-marker.txt"
    child = _write_child_fixture(tmp_path)
    root = tmp_path / "root exits zero.js"
    root.write_text(
        "const fs = require('fs');\n"
        "const { spawn } = require('child_process');\n"
        "spawn(process.execPath, [process.argv[2], process.argv[3]], {\n"
        "  detached: true, windowsHide: true, stdio: ['ignore', 'inherit', 'inherit']\n"
        "});\n"
        "const deadline = Date.now() + 500;\n"
        "while (!fs.existsSync(process.argv[3]) && Date.now() < deadline) {\n"
        "  Atomics.wait(new Int32Array(new SharedArrayBuffer(4)), 0, 0, 10);\n"
        "}\n"
        "process.exit(0);\n",
        encoding="utf-8",
    )

    completed = _run_powershell(
        _failure_command(
            [root, child, marker],
            label="output drain probe",
            timeout_seconds=10,
            output_drain_timeout_seconds=1,
        )
    )

    assert completed.returncode == 0, completed.stderr
    message = json.loads(completed.stdout)["Message"]
    assert "output drain timed out after root exit" in message
    assert "active_job_members_after_cleanup=0" in message
    _assert_child_did_not_survive(marker)
