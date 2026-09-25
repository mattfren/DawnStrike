"""Operator switches must be reachable through the runtime environment loader.

``Import-DawnstrikeEnvironment`` silently skips any key missing from its
allowlist. That is the right default for an untrusted secrets file, but it means
a feature flag omitted from the list is *undeployable*: the operator sets it in
runtime.env, nothing errors, and the feature never activates.

These tests pin the flags the Python layer actually reads.
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
    reason="Windows PowerShell runtime environment loader",
)

ROOT = Path(__file__).resolve().parents[1]
LOADER = ROOT / "scripts" / "import_dawnstrike_environment.ps1"

# Every flag the Python layer reads from the process environment.
REQUIRED_FLAGS = (
    "DAWNSTRIKE_BOOTSTRAP_PAPER_MODE",
    "DAWNSTRIKE_ALPHAOPS_LIQUID_UNIVERSE",
    # Alpaca PAPER execution. Without these the adapter deploys but can never
    # be switched on, which is the failure mode this file exists to prevent.
    "DAWNSTRIKE_PAPER_EXECUTION_ENABLED",
    "DAWNSTRIKE_PAPER_ENTRIES_ENABLED",
    "DAWNSTRIKE_PAPER_KILL_SWITCH",
    "DAWNSTRIKE_PAPER_KILL_SWITCH_ENGAGED",
    "DAWNSTRIKE_PAPER_RISK_PCT",
    "DAWNSTRIKE_PAPER_MAX_POSITION_PCT",
    "DAWNSTRIKE_PAPER_MAX_CONCURRENT",
    "DAWNSTRIKE_PAPER_MAX_ENTRIES_PER_DAY",
    "DAWNSTRIKE_PAPER_DAILY_LOSS_LIMIT_PCT",
    "DAWNSTRIKE_PAPER_MAX_STALENESS_SECONDS",
)


def test_no_live_trading_key_is_allowlisted() -> None:
    """The loader must not be able to hand the process a live-trading switch."""

    forbidden = {"ALPACA_LIVE", "DAWNSTRIKE_LIVE_TRADING_ENABLED", "ALPACA_LIVE_API_KEY_ID"}
    assert not (forbidden & _allowlist())


def _allowlist() -> set[str]:
    text = LOADER.read_text(encoding="utf-8")
    block = re.search(r"foreach \(\$key in @\((.*?)\)\) \{", text, re.S)
    assert block, "allowlist block not found in the loader"
    return set(re.findall(r'"([A-Z0-9_]+)"', block.group(1)))


@pytest.mark.parametrize("flag", REQUIRED_FLAGS)
def test_operator_flag_is_allowlisted(flag: str) -> None:
    assert flag in _allowlist(), (
        f"{flag} is read by the Python layer but is not in the runtime "
        "environment allowlist, so runtime.env would silently ignore it."
    )


@pytest.mark.parametrize("flag", REQUIRED_FLAGS)
def test_operator_flag_actually_reaches_the_process(tmp_path: Path, flag: str) -> None:
    """End-to-end: the loader must export the flag, not merely list it."""

    state = tmp_path / "state"
    (state / "secrets").mkdir(parents=True)
    (state / "secrets" / "runtime.env").write_text(f"{flag}=true\n", encoding="utf-8")

    command = (
        f". '{LOADER}'; "
        f"Import-DawnstrikeEnvironment -StateRoot '{state}'; "
        f"[Environment]::GetEnvironmentVariable('{flag}', 'Process')"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == "true"


def test_unknown_key_is_still_ignored(tmp_path: Path) -> None:
    """The allowlist must stay restrictive; this is not a general env loader."""

    state = tmp_path / "state"
    (state / "secrets").mkdir(parents=True)
    (state / "secrets" / "runtime.env").write_text(
        "DAWNSTRIKE_NOT_A_REAL_FLAG=true\n", encoding="utf-8"
    )

    command = (
        f". '{LOADER}'; "
        f"Import-DawnstrikeEnvironment -StateRoot '{state}'; "
        "[Environment]::GetEnvironmentVariable('DAWNSTRIKE_NOT_A_REAL_FLAG', 'Process')"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip() == ""
