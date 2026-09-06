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


def _mask_expression() -> str:
    """The literal `$writeLikeRights = ( ... )` expression from the installer."""

    text = INSTALLER.read_text(encoding="utf-8")
    match = re.search(r"\$writeLikeRights = \((.*?)\n\s*\)", text, re.S)
    assert match, "writeLikeRights assignment not found in the installer"
    return "(" + match.group(1) + ")"


def _trips(rights: str) -> bool:
    """True when `rights` intersects the installer's write-like mask."""

    command = (
        f"$writeLikeRights = {_mask_expression()}; "
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
