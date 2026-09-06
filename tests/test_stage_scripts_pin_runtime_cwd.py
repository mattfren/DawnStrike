"""Stage scripts must pin the working directory to the runtime release.

``py.exe -m intraday_scanner.cli ...`` puts the *current directory* ahead of
``sys.path``, so a stage launched from any directory that also contains an
``intraday_scanner`` package executes that package instead of the runtime's.
The failure is silent about its cause: the wrong checkout simply rejects a
newer flag, e.g. ``unrecognized arguments: --release-sha``, and the monitor
aborts with "Could not persist monitor heartbeat."

Every stage script therefore pins the working directory immediately after it
resolves ``$runtime`` - not at the later ``Push-Location``, which several
``py.exe`` calls (the heartbeat and the stage recorders) already run before.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    sys.platform != "win32" or shutil.which("powershell") is None,
    reason="stage scripts are Windows PowerShell entry points",
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"

STAGE_SCRIPTS = (
    "run_alphaops_monitor.ps1",
    "run_alphaops_morning.ps1",
    "run_alphaops_eod.ps1",
    "run_alphaops_weekly_training.ps1",
)

PIN = "Set-Location -LiteralPath $runtime"
RESOLVE = "$runtime = (Resolve-Path $RuntimeRoot).Path"


def _lines(name: str) -> list[str]:
    return (SCRIPTS / name).read_text(encoding="utf-8").splitlines()


def _index_of(lines: list[str], needle: str) -> int:
    for number, line in enumerate(lines, start=1):
        if line.strip() == needle:
            return number
    raise AssertionError(f"{needle!r} not found")


def _first_interpreter_line(lines: list[str]) -> int:
    for number, line in enumerate(lines, start=1):
        if re.search(r'-FilePath\s+"py\.exe"', line):
            return number
    raise AssertionError("no py.exe invocation found")


@pytest.mark.parametrize("name", STAGE_SCRIPTS)
def test_pin_follows_runtime_resolution(name: str) -> None:
    lines = _lines(name)
    resolve = _index_of(lines, RESOLVE)
    pin = _index_of(lines, PIN)
    assert pin > resolve, f"{name}: the pin precedes the $runtime it uses"
    between = [line for line in lines[resolve:pin - 1] if line.strip()]
    assert all(line.lstrip().startswith("#") for line in between), (
        f"{name}: only comments may sit between the $runtime resolution and the "
        f"pin, so no code can run unpinned; found {between!r}"
    )


@pytest.mark.parametrize("name", STAGE_SCRIPTS)
def test_pin_precedes_every_interpreter_invocation(name: str) -> None:
    lines = _lines(name)
    assert _index_of(lines, PIN) < _first_interpreter_line(lines), (
        f"{name}: a py.exe call is reachable before the working directory is "
        "pinned, so it can resolve a different checkout of intraday_scanner."
    )


@pytest.mark.parametrize("name", STAGE_SCRIPTS)
def test_header_through_pin_actually_changes_directory(tmp_path: Path, name: str) -> None:
    """Execute the real header and prove the process directory moved."""

    lines = _lines(name)
    header = "\n".join(lines[: _index_of(lines, PIN)])
    header_script = tmp_path / "header.ps1"
    header_script.write_text(header + "\n$PWD.Path\n", encoding="utf-8")

    foreign = tmp_path / "foreign"
    runtime = tmp_path / "runtime"
    foreign.mkdir()
    runtime.mkdir()

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(header_script),
            "-RuntimeRoot",
            str(runtime),
            "-StateRoot",
            str(tmp_path / "state"),
        ],
        cwd=foreign,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().splitlines()[-1] == str(runtime), (
        f"{name}: header ran but left the working directory at "
        f"{completed.stdout.strip()!r}"
    )


def test_current_directory_really_shadows_the_installed_package(tmp_path: Path) -> None:
    """The hazard the pin exists to prevent is real, not hypothetical."""

    decoy = tmp_path / "intraday_scanner"
    decoy.mkdir()
    (decoy / "__init__.py").write_text("MARKER = 'decoy'\n", encoding="utf-8")

    completed = subprocess.run(
        [sys.executable, "-c", "import intraday_scanner; print(intraday_scanner.__file__)"],
        cwd=tmp_path,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    resolved = Path(completed.stdout.strip())
    assert resolved == decoy / "__init__.py", (
        "the working directory no longer shadows the installed package; "
        "re-examine whether the stage scripts still need to pin it"
    )
