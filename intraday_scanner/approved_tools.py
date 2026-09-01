"""Fail-closed executable identities for Python-hosted production operations."""

from __future__ import annotations

import hashlib
import os
import shutil
import stat
import subprocess
from pathlib import Path

APPROVED_GIT_PATH = Path(r"C:\Program Files\Git\cmd\git.exe")
APPROVED_GIT_SHA256 = "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"
SAFE_GIT_CONFIGURATION = (
    "-c",
    "core.fsmonitor=false",
    "-c",
    "core.hooksPath=NUL" if os.name == "nt" else "core.hooksPath=/dev/null",
    "-c",
    "protocol.ext.allow=never",
    "-c",
    "submodule.recurse=false",
)
_FIXED_LOCAL_GIT_CONFIG = {
    "core.repositoryformatversion": "0",
    "core.filemode": "false",
    "core.bare": "false",
    "core.logallrefupdates": "true",
    "core.symlinks": "false",
    "core.ignorecase": "true",
    "remote.origin.url": "https://github.com/mattfren/DawnStrike.git",
    "remote.origin.fetch": "+refs/heads/*:refs/remotes/origin/*",
    "lfs.repositoryformatversion": "0",
}
_NON_EXECUTING_LOCAL_GIT_CONFIG = frozenset({"user.email", "user.name"})


class ApprovedToolError(RuntimeError):
    """A required production executable is missing, redirected, or changed."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_reparse(path: Path) -> None:
    cursor = path
    while True:
        details = cursor.lstat()
        if stat.S_ISLNK(details.st_mode) or getattr(
            details, "st_file_attributes", 0
        ) & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0):
            raise ApprovedToolError(f"approved tool path contains a reparse point: {path}")
        if cursor.parent == cursor:
            return
        cursor = cursor.parent


def approved_git_path() -> Path:
    if os.name != "nt":
        discovered = shutil.which("git")
        if not discovered:
            raise ApprovedToolError("Git is unavailable")
        return Path(discovered)
    configured = os.environ.get("DAWNSTRIKE_APPROVED_GIT_PATH", "").strip()
    path = Path(configured) if configured else APPROVED_GIT_PATH
    if str(path).casefold() != str(APPROVED_GIT_PATH).casefold():
        raise ApprovedToolError("approved Git path override is not exact")
    try:
        _assert_no_reparse(path)
        if not path.is_file() or _sha256(path) != APPROVED_GIT_SHA256:
            raise ApprovedToolError("approved Git executable identity changed")
    except OSError as exc:
        raise ApprovedToolError("approved Git executable is unavailable") from exc
    return path


def sanitized_git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.upper().startswith("GIT_")
    }
    environment["GIT_CONFIG_NOSYSTEM"] = "1"
    environment["GIT_CONFIG_GLOBAL"] = "NUL" if os.name == "nt" else "/dev/null"
    return environment


def _assert_local_git_config_safe(root: Path) -> None:
    try:
        completed = subprocess.run(  # noqa: S603 - exact executable is verified
            [
                str(approved_git_path()),
                *SAFE_GIT_CONFIGURATION,
                "-C",
                str(root),
                "config",
                "--local",
                "--no-includes",
                "--null",
                "--list",
            ],
            cwd=root,
            env=sanitized_git_environment(),
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ApprovedToolError("local Git configuration is unavailable") from exc
    seen: set[str] = set()
    for record in completed.stdout.split("\0"):
        if not record:
            continue
        try:
            key, value = record.split("\n", 1)
        except ValueError as exc:
            raise ApprovedToolError("local Git configuration is malformed") from exc
        key = key.casefold()
        if key in seen:
            raise ApprovedToolError("local Git configuration contains a duplicate key")
        seen.add(key)
        if key in _FIXED_LOCAL_GIT_CONFIG:
            if value != _FIXED_LOCAL_GIT_CONFIG[key]:
                raise ApprovedToolError(f"local Git configuration is not governed: {key}")
            continue
        if key in _NON_EXECUTING_LOCAL_GIT_CONFIG and 1 <= len(value) <= 512:
            continue
        if key.startswith("branch.") and key.endswith((".remote", ".merge")):
            branch, field = key[len("branch.") :].rsplit(".", 1)
            expected = "origin" if field == "remote" else f"refs/heads/{branch}"
            if branch and value == expected:
                continue
        raise ApprovedToolError(f"local Git configuration key is not governed: {key}")


def run_git(
    root: str | Path,
    *arguments: str,
    timeout: int = 30,
) -> subprocess.CompletedProcess[str]:
    resolved_root = Path(root).resolve()
    _assert_local_git_config_safe(resolved_root)
    completed = subprocess.run(  # noqa: S603 - executable is exact and hash-verified on Windows
        [
            str(approved_git_path()),
            *SAFE_GIT_CONFIGURATION,
            "-C",
            str(resolved_root),
            *arguments,
        ],
        cwd=resolved_root,
        env=sanitized_git_environment(),
        check=True,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    _assert_local_git_config_safe(resolved_root)
    return completed


def read_git_bytes(
    root: str | Path,
    *arguments: str,
    timeout: int = 30,
    max_bytes: int = 16 * 1024 * 1024,
) -> bytes:
    """Run exact Git and return bounded raw stdout without text transcoding."""

    resolved_root = Path(root).resolve()
    _assert_local_git_config_safe(resolved_root)
    completed = subprocess.run(  # noqa: S603 - exact executable is verified on Windows
        [
            str(approved_git_path()),
            *SAFE_GIT_CONFIGURATION,
            "-C",
            str(resolved_root),
            *arguments,
        ],
        cwd=resolved_root,
        env=sanitized_git_environment(),
        check=True,
        capture_output=True,
        text=False,
        timeout=timeout,
    )
    if len(completed.stdout) > max_bytes:
        raise ApprovedToolError("Git raw output exceeds the governed byte ceiling")
    _assert_local_git_config_safe(resolved_root)
    return completed.stdout
