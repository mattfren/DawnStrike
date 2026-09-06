"""The host-boundary ACL assertion must reject writes, not reads.

`Assert-DawnstrikeInstalledBoundaryAcl` rejects any non-admin Allow ACE whose
rights intersect `$writeLikeRights`. That mask previously OR-ed in `Modify` and
`FullControl`, which are composites carrying every read bit (FullControl is
0x1F01FF). The mask therefore matched *any* access right at all, so a plain
`ReadAndExecute` ACE (0x200A9) tripped it.

That made the assertion unsatisfiable - including against the read-only
`BUILTIN\\Users` ACE the installer itself grants in
`New-DawnstrikeProtectedDirectorySecurity` - so the host boundary could never be
installed and the runtime stayed pinned to an older release.
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
    reason="Windows ACL rights masks require Windows PowerShell",
)

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "scripts" / "install_dawnstrike_host_boundary.ps1"

# Every script that decides whether a principal may write to the protected
# prefix. The mask was fixed in the installer first, but the same bug survived in
# the two scripts that actually gate execution — which is why `origin/main` could
# not run on a host at all: `dawnstrike_process_runner.ps1` asserts this before
# EVERY py.exe invocation, and `dawnstrike_release_launcher.ps1` is the entry
# point the activation runbook requires.
BOUNDARY_SCRIPTS = (
    "install_dawnstrike_host_boundary.ps1",
    "dawnstrike_process_runner.ps1",
    "dawnstrike_release_launcher.ps1",
)

# Rights that must NOT be treated as write-like: a read-only grant is exactly
# what the installer hands BUILTIN\Users.
READ_ONLY_RIGHTS = (
    "ReadAndExecute",
    "Read",
    "ReadAndExecute, Synchronize",
    "ReadData",
    "ReadAttributes",
    "ReadPermissions",
    "Synchronize",
)

# Rights that MUST be treated as write-like.
MUTATING_RIGHTS = (
    "Write",
    "Modify",
    "FullControl",
    "Delete",
    "DeleteSubdirectoriesAndFiles",
    "ChangePermissions",
    "TakeOwnership",
    "WriteData",
    "AppendData",
)


def _mask_expression(script: str = "install_dawnstrike_host_boundary.ps1") -> str:
    """The literal `$writeLikeRights = ( ... )` expression from `script`."""

    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    match = re.search(r"\$writeLikeRights = \((.*?)\n\s*\)", text, re.S)
    assert match, f"writeLikeRights assignment not found in {script}"
    return "(" + match.group(1) + ")"


def _trips(rights: str, script: str = "install_dawnstrike_host_boundary.ps1") -> bool:
    """True when `rights` intersects `script`'s write-like mask."""

    command = (
        f"$writeLikeRights = {_mask_expression(script)}; "
        f"$candidate = [Security.AccessControl.FileSystemRights]'{rights}'; "
        "if (($candidate -band $writeLikeRights) -ne 0) { 'TRIPS' } else { 'CLEAR' }"
    )
    completed = subprocess.run(
        ["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-Command", command],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    output = completed.stdout.strip()
    assert output in {"TRIPS", "CLEAR"}, output
    return output == "TRIPS"


@pytest.mark.parametrize("rights", READ_ONLY_RIGHTS)
def test_read_only_rights_are_not_write_like(rights: str) -> None:
    assert not _trips(rights), (
        f"{rights} is read-only but matches the write-like mask, which makes the "
        "host-boundary assertion impossible to satisfy."
    )


@pytest.mark.parametrize("rights", MUTATING_RIGHTS)
def test_mutating_rights_are_write_like(rights: str) -> None:
    assert _trips(rights), f"{rights} grants mutation but escapes the write-like mask"


def test_mask_excludes_read_composites() -> None:
    """Guard the specific regression: Modify/FullControl must not be OR-ed in."""

    expression = _mask_expression()
    assert "FullControl" not in expression, (
        "FullControl is 0x1F01FF and carries every read bit; OR-ing it makes the "
        "mask match all rights. Modify and FullControl grants are still caught "
        "because both include the Write group."
    )
    assert "Modify" not in expression
    assert "Write" in expression


def test_installer_grants_users_read_only_access() -> None:
    """The mask must tolerate the ACE the installer itself writes."""

    text = INSTALLER.read_text(encoding="utf-8")
    assert "$users, 'ReadAndExecute'" in text.replace("\n", " ").replace("  ", " ") or (
        "ReadAndExecute" in text and "$users" in text
    ), "installer no longer grants Users a read-only ACE; revisit this contract"
    assert not _trips("ReadAndExecute, Synchronize")


@pytest.mark.parametrize("script", BOUNDARY_SCRIPTS)
@pytest.mark.parametrize("rights", READ_ONLY_RIGHTS)
def test_no_boundary_script_treats_read_as_write(script: str, rights: str) -> None:
    """The bug was fixed in one script and left in two others.

    `dawnstrike_process_runner.ps1` runs this assertion before every py.exe on
    the daily-stage path, so while its mask matched ReadAndExecute the governed
    release line could not execute a single stage on any machine.
    """

    assert not _trips(rights, script), (
        f"{script}: {rights} is read-only but matches the write-like mask, which "
        "makes the boundary assertion impossible to satisfy."
    )


@pytest.mark.parametrize("script", BOUNDARY_SCRIPTS)
@pytest.mark.parametrize("rights", MUTATING_RIGHTS)
def test_every_boundary_script_still_catches_mutation(script: str, rights: str) -> None:
    assert _trips(rights, script), (
        f"{script}: {rights} grants mutation but escapes the write-like mask"
    )


@pytest.mark.parametrize("script", BOUNDARY_SCRIPTS)
def test_no_boundary_script_ors_in_a_read_carrying_composite(script: str) -> None:
    """Guard the specific regression across every copy of the mask."""

    expression = _mask_expression(script)
    assert "FullControl" not in expression, (
        f"{script}: FullControl is 0x1F01FF and carries every read bit; OR-ing it "
        "makes the mask match all rights."
    )
    assert "Modify" not in expression, (
        f"{script}: Modify is 0x301BF and carries the read bits too."
    )
    assert "Write" in expression, f"{script}: the mask must still catch writes"


@pytest.mark.parametrize("script", BOUNDARY_SCRIPTS)
def test_mask_tolerates_the_acl_windows_actually_puts_on_program_files(script: str) -> None:
    r"""The decisive case, checked against the real machine rather than a literal.

    `C:\Program Files` is in the target list of both execution-path assertions,
    and Windows grants BUILTIN\Users ReadAndExecute on it. So did the installer,
    on the prefix it created. A mask that trips on that ACE cannot be satisfied by
    any correctly-installed host.
    """

    completed = subprocess.run(
        [
            "powershell.exe",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-Command",
            f"$writeLikeRights = {_mask_expression(script)}; "
            r"$acl = Get-Acl -LiteralPath 'C:\Program Files'; "
            "$bad = @($acl.Access | Where-Object { "
            "  $_.AccessControlType -eq 'Allow' -and "
            "  [string]$_.IdentityReference -notmatch "
            r"'(?i)(^|\\)(SYSTEM|Administrators|TrustedInstaller)$' -and "
            "  ($_.FileSystemRights -band $writeLikeRights) -ne 0 }); "
            "if ($bad.Count -eq 0) { 'SATISFIABLE' } else { 'UNSATISFIABLE:' + $bad.Count }",
        ],
        cwd=ROOT,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    verdict = completed.stdout.strip()
    assert verdict == "SATISFIABLE", (
        rf"{script}: the write-like mask rejects the stock ACL on C:\Program Files "
        f"({verdict}), so the assertion can never pass on this host."
    )
