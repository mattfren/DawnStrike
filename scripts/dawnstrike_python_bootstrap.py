"""Run a Dawnstrike module or script from one explicit release root.

Scheduled Python is always invoked with ``-I -B -S``.  The ``-S`` switch is
intentional: global ``.pth`` files and editable installs are not release
authority.  This tiny stdlib-only bootstrap then inserts only the materialized
release root and proves that ``intraday_scanner`` resolves from that root
before dispatching the requested module/script.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import importlib.abc
import importlib.machinery
import importlib.metadata
import importlib.util
import io
import os
import re
import runpy
import stat
import subprocess
import sys
import sysconfig
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import BinaryIO, NoReturn

_APPROVED_GIT = Path(r"C:\Program Files\Git\cmd\git.exe")
_APPROVED_GIT_SHA256 = (
    "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"  # pragma: allowlist secret
)
_APPROVED_DISTRIBUTION_RECORD_SET_SHA256 = (
    "447a0d12feffcfd6c353d9acb4cfd1e5cc1b35e3548cd7e9ad58666516b4b3af"  # pragma: allowlist secret
)
_FORBIDDEN_IGNORED_SUFFIXES = {
    ".bat",
    ".cmd",
    ".com",
    ".dll",
    ".exe",
    ".ps1",
    ".psm1",
    ".pth",
    ".py",
    ".pyc",
    ".pyd",
    ".sh",
}
_FORBIDDEN_IGNORED_NAMES = {"sitecustomize.py", "usercustomize.py"}
_EXACT_GIT_CONTRACT_ATTRIBUTE = "_dawnstrike_exact_git_contract_v1"
_EXACT_GIT_DESCENDANT_SENTINEL = "DAWNSTRIKE_EXACT_GIT_ADMISSION_REQUIRED"
_PUBLIC_WEB_EXACT_PATHS = frozenset({"web/index.html", "web/favicon.svg"})
_PUBLIC_WEB_EXACT_PREFIX = "web/assets/"
_EXACT_RELEASE_AUTHORITY_PATHS = frozenset(
    {
        "requirements.lock",
        "config/state_preparation_contract.json",
    }
)
_GOVERNED_ORIGIN_URLS = frozenset(
    {
        "https://github.com/mattfren/DawnStrike",
        "https://github.com/mattfren/DawnStrike.git",
    }
)
_RETRYABLE_EXACT_SOURCE_ADMISSION_FAILURE = (
    "release Git metadata changed during source verification"
)


def _is_reparse(path: Path) -> bool:
    """Reject Windows junctions/reparse points as well as POSIX symlinks."""

    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _fail(message: str) -> NoReturn:
    raise RuntimeError(message)


def _normalized_git_blob_sha1_bytes(body: bytes) -> str:
    body = body.replace(b"\r\n", b"\n")
    return hashlib.sha1(
        b"blob " + str(len(body)).encode("ascii") + b"\0" + body,
        usedforsecurity=False,
    ).hexdigest()


def _git_blob_sha1_bytes(body: bytes) -> str:
    """Hash exact committed bytes without worktree newline normalization."""

    return hashlib.sha1(
        b"blob " + str(len(body)).encode("ascii") + b"\0" + body,
        usedforsecurity=False,
    ).hexdigest()


def _normalized_git_blob_sha1(path: Path) -> str:
    return _normalized_git_blob_sha1_bytes(path.read_bytes())


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    fields: tuple[str, ...] = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if os.name != "nt":
        fields += ("st_ctime_ns",)
    return all(int(getattr(left, field)) == int(getattr(right, field)) for field in fields)


def _exact_git_environment(root: Path) -> dict[str, str]:
    """Return the only Git environment accepted for exact-release subprocesses."""

    git_dir = root / ".git"
    return {
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_DIR": str(git_dir),
        "GIT_COMMON_DIR": str(git_dir),
        "GIT_WORK_TREE": str(root),
    }


def _isolated_git_env(root: Path) -> dict[str, str]:
    blocked = {"PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"}
    env = {
        key: value
        for key, value in os.environ.items()
        if key.upper() not in blocked and not key.upper().startswith("GIT_")
    }
    # Do not allow machine/global Git configuration to add filters, hooks, or
    # other behavior to the release identity check.
    # Admission accepts only a self-contained clone. Bind Git to that exact
    # metadata directory so a concurrently created commondir pointer cannot
    # redirect config, refs, or objects mid-check.
    env.update(_exact_git_environment(root))
    return env


def _git_process(root: Path, *args: str) -> subprocess.CompletedProcess[bytes]:
    try:
        digest = hashlib.sha256(_APPROVED_GIT.read_bytes()).hexdigest()
    except OSError as exc:
        _fail(f"approved Git is unavailable: {exc}")
    if digest != _APPROVED_GIT_SHA256:
        _fail("approved Git hash changed")
    try:
        return subprocess.run(
            [
                str(_APPROVED_GIT),
                "-c",
                "extensions.worktreeConfig=false",
                "-c",
                "core.autocrlf=true",
                "-c",
                "core.hooksPath=NUL",
                "-c",
                "core.attributesFile=NUL",
                "-c",
                "core.fsmonitor=false",
                "-c",
                "core.untrackedCache=false",
                "-C",
                str(root),
                *args,
            ],
            cwd=str(root),
            env=_isolated_git_env(root),
            capture_output=True,
            text=False,
            check=False,
        )
    except OSError as exc:
        _fail(f"approved Git execution failed: {exc}")


def _git_bytes(root: Path, *args: str) -> bytes:
    result = _git_process(root, *args)
    if result.returncode != 0:
        _fail("exact release Git identity check failed")
    return result.stdout


def _git_optional(root: Path, *args: str) -> str | None:
    result = _git_process(root, *args)
    if result.returncode == 1:
        return None
    if result.returncode != 0:
        _fail("exact release optional Git identity check failed")
    return result.stdout.decode("utf-8", "strict")


def _git(root: Path, *args: str) -> str:
    return _git_bytes(root, *args).decode("utf-8", "strict")


def _open_locked_exact_file(path: Path) -> BinaryIO:
    """Open one no-follow file while denying writes/deletes on Windows."""

    if os.name != "nt":
        descriptor = os.open(
            path,
            os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0)),
        )
        handle = os.fdopen(descriptor, "rb", closefd=True)
        try:
            import fcntl

            lock_flags = fcntl.LOCK_SH | fcntl.LOCK_NB  # type: ignore[attr-defined]
            fcntl.flock(handle.fileno(), lock_flags)  # type: ignore[attr-defined]
        except (ImportError, OSError):
            handle.close()
            raise
        return handle

    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
        wintypes.DWORD,
        wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    raw_handle = create_file(
        str(path),
        0x80000000,  # GENERIC_READ
        0x00000001,  # FILE_SHARE_READ only: deny write and delete sharing
        None,
        3,  # OPEN_EXISTING
        0x00200000,  # FILE_FLAG_OPEN_REPARSE_POINT
        None,
    )
    invalid_handle = ctypes.c_void_p(-1).value
    if raw_handle is None or int(raw_handle) == invalid_handle:
        error = ctypes.get_last_error()
        raise OSError(error, f"cannot lock Git metadata: {path}")
    try:
        descriptor = msvcrt.open_osfhandle(
            int(raw_handle), os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except Exception:
        close_handle(raw_handle)
        raise
    return os.fdopen(descriptor, "rb", closefd=True)


def _read_locked_exact_file(path: Path, handles: list[BinaryIO]) -> bytes:
    before = path.lstat()
    handle = _open_locked_exact_file(path)
    try:
        opened = os.fstat(handle.fileno())
        after_open = path.lstat()
        if (
            not stat.S_ISREG(opened.st_mode)
            or _is_reparse(path)
            or not _same_file_snapshot(before, opened)
            or not _same_file_snapshot(opened, after_open)
        ):
            _fail(f"exact file identity changed while its handle was acquired: {path}")
        content = handle.read()
        after_read = os.fstat(handle.fileno())
        if not _same_file_snapshot(opened, after_read):
            _fail(f"exact file changed while its bytes were read: {path}")
        handle.seek(0)
    except Exception:
        handle.close()
        raise
    handles.append(handle)
    return content


class _GitMetadataChangeGuard:
    """Remember any Windows metadata-tree mutation, including transient ones."""

    def __init__(self, git_dir: Path) -> None:
        self._handle: int | None = None
        if os.name != "nt":
            return

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        find_first = kernel32.FindFirstChangeNotificationW
        find_first.argtypes = (wintypes.LPCWSTR, wintypes.BOOL, wintypes.DWORD)
        find_first.restype = wintypes.HANDLE
        handle = find_first(
            str(git_dir),
            True,
            0x00000001  # FILE_NOTIFY_CHANGE_FILE_NAME
            | 0x00000002  # FILE_NOTIFY_CHANGE_DIR_NAME
            | 0x00000004  # FILE_NOTIFY_CHANGE_ATTRIBUTES
            | 0x00000008  # FILE_NOTIFY_CHANGE_SIZE
            | 0x00000010  # FILE_NOTIFY_CHANGE_LAST_WRITE
            | 0x00000040  # FILE_NOTIFY_CHANGE_CREATION
            | 0x00000100,  # FILE_NOTIFY_CHANGE_SECURITY
        )
        invalid_handle = ctypes.c_void_p(-1).value
        if handle is None or int(handle) == invalid_handle:
            error = ctypes.get_last_error()
            raise OSError(error, f"cannot guard Git metadata directory: {git_dir}")
        self._handle = int(handle)

    def assert_unchanged(self) -> None:
        if self._handle is None:
            return

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        wait = kernel32.WaitForSingleObject
        wait.argtypes = (wintypes.HANDLE, wintypes.DWORD)
        wait.restype = wintypes.DWORD
        result = int(wait(self._handle, 0))
        if result == 0:  # WAIT_OBJECT_0
            _fail("release Git metadata changed during source verification")
        if result != 258:  # WAIT_TIMEOUT
            _fail("release Git metadata change guard failed")

    def close(self) -> None:
        if self._handle is None:
            return

        import ctypes
        from ctypes import wintypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        close = kernel32.FindCloseChangeNotification
        close.argtypes = (wintypes.HANDLE,)
        close.restype = wintypes.BOOL
        close(self._handle)
        self._handle = None


class _ExactSourceGuard:
    """Keep exact metadata and tracked-source authority alive through dispatch."""

    def __init__(
        self,
        handles: list[BinaryIO],
        metadata_guard: _GitMetadataChangeGuard,
        metadata_snapshots: dict[Path, bytes],
        forbidden_absent: tuple[Path, ...],
        tracked_handles: dict[Path, BinaryIO],
        source_bytes: dict[str, bytes],
        git_contract: object,
    ) -> None:
        self._handles = handles
        self._metadata_guard = metadata_guard
        self._metadata_snapshots = metadata_snapshots
        self._forbidden_absent = forbidden_absent
        self._tracked_handles = tracked_handles
        self.source_bytes = source_bytes
        self.git_contract = git_contract

    def assert_unchanged(self) -> None:
        """Fail before dispatch can consume Git metadata changed after admission."""

        try:
            if any(
                path.read_bytes() != expected for path, expected in self._metadata_snapshots.items()
            ):
                _fail("release Git metadata changed during dispatched target lifetime")
            if any(path.exists() for path in self._forbidden_absent):
                _fail("release Git metadata absence changed during dispatched target lifetime")
            for path, handle in self._tracked_handles.items():
                opened = os.fstat(handle.fileno())
                current = path.lstat()
                if not _same_file_snapshot(opened, current):
                    _fail("release tracked file changed during dispatched target lifetime")
            self._metadata_guard.assert_unchanged()
        except OSError as exc:
            _fail(f"release Git metadata changed during dispatched target lifetime: {exc}")

    def close(self) -> None:
        self._metadata_guard.close()
        for handle in self._handles:
            handle.close()
        self._handles.clear()


def _same_executable_path(value: object, expected: Path) -> bool:
    if not isinstance(value, (str, bytes, os.PathLike)):
        return False
    try:
        candidate = os.fsdecode(os.fspath(value))
    except TypeError:
        return False
    return os.path.normcase(os.path.abspath(candidate)) == os.path.normcase(
        os.path.abspath(expected)
    )


def _audit_invokes_approved_git(executable: object, command: object) -> bool:
    """Recognize any Git spawn in CPython's platform-specific audit payload."""

    if executable is not None and _same_executable_path(executable, _APPROVED_GIT):
        return True
    if isinstance(executable, (str, bytes, os.PathLike)):
        try:
            if Path(os.fsdecode(os.fspath(executable))).name.casefold() in {"git", "git.exe"}:
                return True
        except TypeError:
            pass
    if isinstance(command, (list, tuple)) and command:
        if _same_executable_path(command[0], _APPROVED_GIT):
            return True
        try:
            return Path(os.fsdecode(os.fspath(command[0]))).name.casefold() in {"git", "git.exe"}
        except TypeError:
            return False
    if os.name == "nt" and isinstance(command, str):
        prefix = subprocess.list2cmdline([str(_APPROVED_GIT)])
        folded = command.casefold()
        expected = prefix.casefold()
        if folded == expected or folded.startswith(expected + " "):
            return True
        unquoted = folded.lstrip().lstrip('"')
        return unquoted in {"git", "git.exe"} or unquoted.startswith(("git ", "git.exe "))
    return False


def _install_verified_git_dispatch_guard(root: Path, guard: _ExactSourceGuard) -> None:
    """Publish admitted Git values and prohibit post-admission Git reads."""

    for key in tuple(os.environ):
        if key.upper().startswith("GIT_"):
            del os.environ[key]
    os.environ[_EXACT_GIT_DESCENDANT_SENTINEL] = "1"
    if hasattr(sys, _EXACT_GIT_CONTRACT_ATTRIBUTE):
        _fail("exact Git contract process slot is already occupied")
    setattr(sys, _EXACT_GIT_CONTRACT_ATTRIBUTE, guard.git_contract)

    def audit_hook(event: str, args: tuple[object, ...]) -> None:
        if event != "subprocess.Popen" or len(args) != 4:
            return
        executable, command, _cwd, _environment = args
        if not _audit_invokes_approved_git(executable, command):
            return
        _fail("Git subprocess is forbidden after exact-source admission")

    sys.addaudithook(audit_hook)


def _validated_git_metadata(
    root: Path,
) -> tuple[dict[Path, bytes], list[BinaryIO], tuple[Path, ...], _GitMetadataChangeGuard]:
    """Lock a self-contained checkout's effective local Git control metadata."""

    dot_git = root / ".git"
    metadata_snapshots: dict[Path, bytes] = {}
    metadata_handles: list[BinaryIO] = []
    metadata_guard: _GitMetadataChangeGuard | None = None
    try:
        result = _validated_git_metadata_locked(root, dot_git, metadata_snapshots, metadata_handles)
        metadata_guard = result[3]
        return result
    except Exception:
        if metadata_guard is not None:
            metadata_guard.close()
        for handle in metadata_handles:
            handle.close()
        raise


def _validated_git_metadata_locked(
    root: Path,
    dot_git: Path,
    metadata_snapshots: dict[Path, bytes],
    metadata_handles: list[BinaryIO],
) -> tuple[dict[Path, bytes], list[BinaryIO], tuple[Path, ...], _GitMetadataChangeGuard]:
    if _is_reparse(dot_git):
        _fail("release Git metadata is a reparse point")
    if dot_git.is_file():
        _fail("release root must be a self-contained clone, not a linked Git worktree")
    if not dot_git.is_dir():
        _fail("release root has no valid Git metadata")
    git_dir = dot_git.resolve(strict=True)
    if _is_reparse(git_dir):
        _fail("release Git directory is invalid")

    forbidden_absent = (
        git_dir / "commondir",
        git_dir / "config.worktree",
        git_dir / "info" / "attributes",
        git_dir / "info" / "grafts",
        git_dir / "objects" / "info" / "alternates",
        git_dir / "objects" / "info" / "http-alternates",
    )
    if any(path.exists() for path in forbidden_absent):
        _fail("release checkout contains forbidden or linked Git metadata")

    config_path = git_dir / "config"
    if not config_path.is_file() or _is_reparse(config_path):
        _fail("release checkout Git config is unavailable")
    try:
        config_bytes = _read_locked_exact_file(config_path, metadata_handles)
        metadata_snapshots[config_path] = config_bytes
        local_config = config_bytes.decode("utf-8", "strict")
    except (OSError, UnicodeError) as exc:
        _fail(f"release checkout Git config is unavailable: {exc}")
    if re.search(
        r"(?im)^\s*\[\s*(?:filter|url|protocol|include|credential|http)(?:\s|\])"
        r"|^\s*(?:attributesfile|hookspath|path|sshcommand|proxy|helper|command)\s*=",
        local_config,
    ):
        _fail("release checkout contains a Git execution or transport configuration")
    if re.search(
        r"(?im)^\s*worktreeconfig\s*=\s*(?:true|yes|on|1)\s*(?:[#;].*)?$",
        local_config,
    ):
        _fail("release checkout requires the Git worktree config extension disabled")

    # Hold mutable control files without write/delete sharing. GIT_OPTIONAL_LOCKS
    # prevents the read-only proof from refreshing the held index.
    for control_path in (git_dir / "HEAD", git_dir / "index", git_dir / "info" / "exclude"):
        if not control_path.is_file() or _is_reparse(control_path):
            _fail("release checkout Git control metadata is unavailable")
        try:
            metadata_snapshots[control_path] = _read_locked_exact_file(
                control_path, metadata_handles
            )
        except OSError as exc:
            _fail(f"release checkout Git control metadata is unavailable: {exc}")
    for optional_control in (git_dir / "packed-refs", git_dir / "shallow"):
        if not optional_control.exists():
            continue
        if not optional_control.is_file() or _is_reparse(optional_control):
            _fail("release checkout Git control metadata is invalid")
        try:
            metadata_snapshots[optional_control] = _read_locked_exact_file(
                optional_control, metadata_handles
            )
        except OSError as exc:
            _fail(f"release checkout Git control metadata is unavailable: {exc}")

    metadata_guard = _GitMetadataChangeGuard(git_dir)
    if any(path.exists() for path in forbidden_absent):
        metadata_guard.close()
        _fail("release Git metadata changed before its absence guard was acquired")
    return metadata_snapshots, metadata_handles, forbidden_absent, metadata_guard


def _assert_exact_source(root: Path, expected_sha: str) -> _ExactSourceGuard:
    if len(expected_sha) != 40 or any(char not in "0123456789abcdef" for char in expected_sha):
        _fail("expected release SHA is invalid")
    metadata_snapshots, metadata_handles, forbidden_absent, metadata_guard = (
        _validated_git_metadata(root)
    )
    try:
        source_bytes, git_contract, tracked_handles = _assert_exact_source_locked(
            root,
            expected_sha,
            metadata_snapshots,
            forbidden_absent,
            metadata_guard,
            metadata_handles,
        )
    except Exception:
        metadata_guard.close()
        for handle in metadata_handles:
            handle.close()
        raise
    return _ExactSourceGuard(
        metadata_handles,
        metadata_guard,
        metadata_snapshots,
        forbidden_absent,
        tracked_handles,
        source_bytes,
        git_contract,
    )


class _ExactSourceRetryBudget:
    """Permit one exact transient across the entire pre-dispatch window."""

    def __init__(self) -> None:
        self._used = False

    def consume(self, error: RuntimeError) -> None:
        if self._used or str(error) != _RETRYABLE_EXACT_SOURCE_ADMISSION_FAILURE:
            raise error
        self._used = True

    def admit(self, root: Path, expected_sha: str) -> _ExactSourceGuard:
        try:
            return _assert_exact_source(root, expected_sha)
        except RuntimeError as exc:
            self.consume(exc)
        # _assert_exact_source closes its metadata watcher plus every metadata
        # and tracked-file handle on failure. This is a full admission from zero.
        return _assert_exact_source(root, expected_sha)


def _assert_exact_source_with_bounded_retry(root: Path, expected_sha: str) -> _ExactSourceGuard:
    """Compatibility entry point for one bounded initial-admission retry."""

    return _ExactSourceRetryBudget().admit(root, expected_sha)


def _assert_exact_source_locked(
    root: Path,
    expected_sha: str,
    metadata_snapshots: dict[Path, bytes],
    forbidden_absent: tuple[Path, ...],
    metadata_guard: _GitMetadataChangeGuard,
    source_handles: list[BinaryIO],
) -> tuple[dict[str, bytes], object, dict[Path, BinaryIO]]:
    attributes_path = root / ".gitattributes"
    if not attributes_path.is_file() or _is_reparse(attributes_path):
        _fail("release checkout has no regular governed .gitattributes")
    top = _git(root, "rev-parse", "--show-toplevel").strip()
    if Path(top).resolve(strict=True) != root:
        _fail("Git checkout root does not match the release root")
    head = _git(root, "rev-parse", "HEAD").strip().lower()
    if head != expected_sha:
        _fail("release HEAD does not match expected candidate SHA")
    attributes_blob = _git(root, "rev-parse", f"{expected_sha}:.gitattributes").strip().lower()
    if attributes_blob != _normalized_git_blob_sha1(attributes_path):
        _fail("release .gitattributes differs from exact HEAD")
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if status:
        markers = sorted({line[:2] for line in status.splitlines() if line})
        _fail(f"release checkout is not clean (porcelain markers: {','.join(markers)})")
    ignored = _git(root, "ls-files", "--others", "--ignored", "--exclude-standard", "-z")
    for relative in ignored.split("\0"):
        if not relative:
            continue
        path = Path(relative)
        if (
            path.suffix.lower() in _FORBIDDEN_IGNORED_SUFFIXES
            or path.name.lower() in _FORBIDDEN_IGNORED_NAMES
        ):
            _fail("release checkout contains an ignored executable or startup artifact")
    flags = _git(root, "ls-files", "-v", "-z")
    entries = flags.split("\0")
    if any(entry and entry[0] in "hSs" for entry in entries):
        _fail("release checkout contains hidden Git index entries")
    if _git(root, "replace", "-l").strip():
        _fail("release checkout contains Git replace refs")
    diff = subprocess.run(
        [
            str(_APPROVED_GIT),
            "-c",
            "extensions.worktreeConfig=false",
            "-c",
            "core.autocrlf=true",
            "-c",
            "core.hooksPath=NUL",
            "-c",
            "core.attributesFile=NUL",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(root),
            "diff-index",
            "--quiet",
            expected_sha,
            "--",
        ],
        cwd=str(root),
        env=_isolated_git_env(root),
        capture_output=True,
        check=False,
    )
    if diff.returncode != 0:
        _fail("release checkout differs from exact HEAD")
    try:
        if any(path.read_bytes() != expected for path, expected in metadata_snapshots.items()):
            _fail("release Git metadata changed during source verification")
        if any(path.exists() for path in forbidden_absent):
            _fail("release Git metadata absence changed during source verification")
        metadata_guard.assert_unchanged()
    except OSError as exc:
        _fail(f"release Git metadata changed during source verification: {exc}")

    exact_tree = _git(root, "rev-parse", f"{expected_sha}^{{tree}}").strip().lower()
    if len(exact_tree) != 40 or any(char not in "0123456789abcdef" for char in exact_tree):
        _fail("release Git tree identity is invalid")

    source_bytes: dict[str, bytes] = {}
    tracked_handles: dict[Path, BinaryIO] = {}
    tracked_inventory: list[tuple[str, str, str]] = []
    release_authority_blobs: dict[str, bytes] = {}
    public_web_inventory: list[tuple[str, str, str]] = []
    public_web_blobs: dict[str, bytes] = {}
    public_web_total_bytes = 0
    tree = _git(root, "ls-tree", "-r", "-z", "--full-tree", expected_sha)
    for entry in tree.split("\0"):
        if not entry:
            continue
        metadata, separator, relative = entry.partition("\t")
        fields = metadata.split()
        if separator != "\t" or len(fields) != 3:
            _fail("release Git tree inventory is invalid")
        mode, object_kind, object_id = fields
        relative_path = PurePosixPath(relative)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            _fail("release Git tracked path is unsafe")
        if object_kind != "blob" or mode not in {"100644", "100755"}:
            _fail("release Git tree contains a non-regular tracked entry")
        source_path = root.joinpath(*relative_path.parts)
        try:
            resolved = source_path.resolve(strict=True)
        except OSError as exc:
            _fail(f"release Git tracked file is unavailable: {exc}")
        if root not in resolved.parents or _is_reparse(resolved):
            _fail("release Git tracked file escapes the release root")
        parent = resolved.parent
        while parent != root:
            if _is_reparse(parent):
                _fail("release Git tracked path contains a reparse point")
            parent = parent.parent
        try:
            worktree_contents = _read_locked_exact_file(resolved, source_handles)
        except OSError as exc:
            _fail(f"release Git tracked file cannot be handle-locked: {exc}")
        if (
            _git_blob_sha1_bytes(worktree_contents) != object_id
            and _normalized_git_blob_sha1_bytes(worktree_contents) != object_id
        ):
            _fail("release Git tracked file differs from the exact candidate commit")
        if resolved in tracked_handles:
            _fail("release Git tracked file inventory is ambiguous")
        tracked_handles[resolved] = source_handles[-1]
        tracked_inventory.append((mode, object_id, relative))

        key = os.path.normcase(str(resolved))
        if relative_path.suffix.lower() in {".py", ".pyw"}:
            if b"\0" in worktree_contents or key in source_bytes:
                _fail("release Git Python source inventory is invalid")
            source_bytes[key] = worktree_contents

        if relative in _EXACT_RELEASE_AUTHORITY_PATHS:
            committed_contents = _git_bytes(root, "cat-file", "blob", object_id)
            if _git_blob_sha1_bytes(committed_contents) != object_id:
                _fail("release authority differs from the exact candidate commit")
            release_authority_blobs[relative] = committed_contents
            # requirements.lock is consumed before the immutable contract is
            # published, so expose its exact committed bytes to that admission
            # step through the already private captured-source mapping.
            if relative == "requirements.lock":
                source_bytes[key] = committed_contents

        is_public_web_source = relative in _PUBLIC_WEB_EXACT_PATHS or relative.startswith(
            _PUBLIC_WEB_EXACT_PREFIX
        )
        if is_public_web_source:
            if len(public_web_inventory) >= 128 or relative in public_web_blobs:
                _fail("release public web source inventory exceeds its ceiling")
            declared_size_raw = _git(root, "cat-file", "-s", object_id).strip()
            if not declared_size_raw.isascii() or not declared_size_raw.isdecimal():
                _fail("release public web source size is invalid")
            declared_size = int(declared_size_raw)
            if declared_size > 16 * 1024 * 1024:
                _fail("release public web source exceeds its file byte ceiling")
            public_web_total_bytes += declared_size
            if public_web_total_bytes > 32 * 1024 * 1024:
                _fail("release public web source exceeds its aggregate byte ceiling")
            contents = _git_bytes(root, "cat-file", "blob", object_id)
            if len(contents) != declared_size or _git_blob_sha1_bytes(contents) != object_id:
                _fail("release public web source differs from the exact candidate commit")
            public_web_inventory.append((mode, object_id, relative))
            public_web_blobs[relative] = contents
    if not source_bytes:
        _fail("release Git tree contains no governed Python source")
    metadata_guard.assert_unchanged()
    optional_origin = _git_optional(root, "config", "--get-all", "remote.origin.url")
    origin_values = optional_origin.splitlines() if optional_origin is not None else []
    if len(origin_values) > 1:
        _fail("release Git origin configuration is ambiguous")
    origin_url = origin_values[0] if origin_values else None
    if origin_url is not None and origin_url not in _GOVERNED_ORIGIN_URLS:
        _fail("release Git origin is not governed")
    optional_origin_main = _git(
        root, "for-each-ref", "--format=%(objectname)", "refs/remotes/origin/main"
    ).strip()
    origin_main_sha = optional_origin_main.lower() or None
    if origin_main_sha is not None and (
        len(origin_main_sha) != 40
        or any(char not in "0123456789abcdef" for char in origin_main_sha)
    ):
        _fail("release origin/main identity is invalid")
    final_status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    if final_status:
        _fail("release checkout changed while tracked handles were acquired")
    final_diff = subprocess.run(
        [
            str(_APPROVED_GIT),
            "-c",
            "extensions.worktreeConfig=false",
            "-c",
            "core.autocrlf=true",
            "-c",
            "core.hooksPath=NUL",
            "-c",
            "core.attributesFile=NUL",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "core.untrackedCache=false",
            "-C",
            str(root),
            "diff-index",
            "--quiet",
            expected_sha,
            "--",
        ],
        cwd=str(root),
        env=_isolated_git_env(root),
        capture_output=True,
        check=False,
    )
    if final_diff.returncode != 0:
        _fail("release checkout changed while tracked handles were acquired")
    try:
        if any(path.read_bytes() != expected for path, expected in metadata_snapshots.items()):
            _fail("release Git metadata changed during final source admission")
        if any(path.exists() for path in forbidden_absent):
            _fail("release Git metadata absence changed during final source admission")
        for path, handle in tracked_handles.items():
            if not _same_file_snapshot(os.fstat(handle.fileno()), path.lstat()):
                _fail("release tracked file changed during final source admission")
        metadata_guard.assert_unchanged()
    except OSError as exc:
        _fail(f"release source changed during final admission: {exc}")
    git_contract = MappingProxyType(
        {
            "schema_version": "dawnstrike.exact_git_contract.v1",
            "root": os.path.normcase(os.path.abspath(root)),
            "candidate_sha": expected_sha,
            "candidate_tree": exact_tree,
            "origin_url": origin_url,
            "origin_main_sha": origin_main_sha,
            "git_executable_sha256": _APPROVED_GIT_SHA256,
            "clean": True,
            "tracked_inventory": tuple(tracked_inventory),
            "release_authority_blobs": MappingProxyType(release_authority_blobs),
            "public_web_inventory": tuple(public_web_inventory),
            "public_web_blobs": MappingProxyType(public_web_blobs),
        }
    )
    return source_bytes, git_contract, tracked_handles


def _release_root(raw: str) -> Path:
    root = Path(raw).resolve(strict=True)
    expected = Path(__file__).resolve().parents[1]
    if root != expected or not root.is_dir():
        raise RuntimeError("release bootstrap root is not the materialized bootstrap parent")
    package = root / "intraday_scanner"
    if (
        _is_reparse(package)
        or _is_reparse(package / "__init__.py")
        or _is_reparse(root / "pyproject.toml")
        or not (package / "__init__.py").is_file()
        or not (root / "pyproject.toml").is_file()
    ):
        raise RuntimeError("release bootstrap root is incomplete")
    return root


def _assert_package_from(root: Path) -> None:
    spec = importlib.util.find_spec("intraday_scanner")
    expected = (root / "intraday_scanner" / "__init__.py").resolve(strict=True)
    if spec is None or spec.origin is None or Path(spec.origin).resolve(strict=True) != expected:
        raise RuntimeError("intraday_scanner did not resolve from the exact release root")


def _append_governed_dependencies() -> tuple[Path, ...]:
    """Expose only the pinned interpreter's dependency directories.

    The -S switch intentionally prevents Python from running site.py. That
    means the normal site-packages directories are absent from sys.path;
    append the interpreter's own purelib/platlib directories explicitly so
    installed dependencies remain usable without executing any global .pth
    file or editable-install finder. The release root is inserted first by
    main and therefore remains the package authority.
    """

    paths = sysconfig.get_paths()
    dependency_paths = set()
    for name in ("purelib", "platlib"):
        raw_dependency = Path(paths[name]) if paths.get(name) else None
        if raw_dependency is None:
            continue
        if _is_reparse(raw_dependency) or any(
            _is_reparse(parent) for parent in raw_dependency.parents
        ):
            raise RuntimeError("interpreter dependency path contains a reparse point")
        dependency_paths.add(raw_dependency.resolve(strict=True))
    prefix = Path(sysconfig.get_config_var("prefix") or sys.prefix).resolve(strict=True)
    for dependency in sorted(dependency_paths, key=str):
        if (
            not dependency.is_dir()
            or _is_reparse(dependency)
            or prefix not in dependency.parents
            or any(_is_reparse(parent) for parent in dependency.parents)
        ):
            raise RuntimeError("interpreter dependency path is outside the approved prefix")
        # -S suppresses .pth execution, but a reparse point or startup file in
        # the approved dependency directory would still let imports escape the
        # pinned interpreter boundary.
        for child in dependency.iterdir():
            if _is_reparse(child) or child.name.lower() in {"sitecustomize.py", "usercustomize.py"}:
                raise RuntimeError(
                    "interpreter dependency directory contains an unsafe startup link"
                )
        text = str(dependency)
        if text not in sys.path:
            sys.path.append(text)
    return tuple(sorted(dependency_paths, key=str))


def _locked_requirements(root: Path, release_bytes: dict[str, bytes]) -> dict[str, str]:
    """Read exact package pins from the repository's hash-locked manifest."""

    lockfile = root / "requirements.lock"
    lockfile_key = os.path.normcase(os.path.abspath(lockfile))
    captured = release_bytes.get(lockfile_key)
    if captured is None:
        _fail("release checkout has no captured exact-commit requirements manifest")
    try:
        lines = captured.decode("utf-8", "strict").splitlines()
    except UnicodeError as exc:
        _fail(f"requirements.lock is not strict UTF-8: {exc}")
    requirements: dict[str, str] = {}
    hashes: set[str] = set()
    current: str | None = None
    for line in lines:
        stripped = line.strip()
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s\\]+)",
            stripped,
        )
        if match is not None:
            name, version = match.groups()
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            if normalized in requirements and requirements[normalized] != version:
                _fail(f"requirements.lock contains conflicting pins for {name}")
            requirements[normalized] = version
            current = normalized
            continue
        if current is not None and re.search(r"--hash=sha256:[0-9a-f]{64}", stripped):
            hashes.add(current)
    if not requirements:
        _fail("requirements.lock contains no exact package pins")
    if hashes != set(requirements):
        _fail("requirements.lock is missing an exact sha256 hash for a package")
    return requirements


def _read_distribution_record(
    dist: importlib.metadata.Distribution,
    prefix: Path,
    distribution_name: str,
) -> tuple[set[str], dict[str, tuple[bytes, int | None]], set[str], str]:
    """Parse a locked distribution's anchored RECORD without loading its code."""

    # ``Distribution.files`` parses RECORD itself. Consulting it here would
    # validate one pathname read and then parse a second protected read. Bind
    # the concrete PathDistribution metadata directory instead and open RECORD
    # exactly once through the deny-write/delete handle below.
    metadata_location = getattr(dist, "_path", None)
    try:
        if not isinstance(metadata_location, (str, bytes, os.PathLike)):
            raise TypeError
        metadata_native_path = os.fspath(metadata_location)
        if not isinstance(metadata_native_path, str):
            raise TypeError
        metadata_path = Path(os.path.abspath(metadata_native_path))
    except TypeError:
        _fail(f"installed dependency {distribution_name} has no concrete metadata root")
    if not metadata_path.name.lower().endswith(".dist-info") or prefix not in metadata_path.parents:
        _fail("installed dependency metadata escapes the approved interpreter prefix")
    cursor = metadata_path
    while True:
        if _is_reparse(cursor):
            _fail("installed dependency metadata path contains a reparse point")
        if cursor == prefix:
            break
        cursor = cursor.parent
    record_path = metadata_path / "RECORD"
    if prefix not in record_path.parents:
        _fail("installed dependency RECORD escapes the approved interpreter prefix")
    cursor = record_path
    while True:
        if _is_reparse(cursor):
            _fail("installed dependency RECORD path contains a reparse point")
        if cursor == prefix:
            break
        cursor = cursor.parent
    record_path = record_path.resolve(strict=True)
    record_handles: list[BinaryIO] = []
    try:
        record_bytes = _read_locked_exact_file(record_path, record_handles)
        record_handle = record_handles[0]
        record_snapshot = os.fstat(record_handle.fileno())
        record_sha256 = hashlib.sha256(record_bytes).hexdigest()
        record_text = record_bytes.decode("utf-8", "strict")
        owned_paths: set[str] = set()
        owned_hashes: dict[str, tuple[bytes, int | None]] = {}
        top_level_names: set[str] = set()
        rows = csv.reader(io.StringIO(record_text, newline=""))
        for row in rows:
            if len(row) != 3:
                _fail(f"installed dependency {dist.metadata['Name']} has malformed RECORD data")
            relative, hash_spec, size_text = row
            located_file: object = dist.locate_file(PurePosixPath(relative))
            if not isinstance(located_file, (str, bytes, os.PathLike)):
                raise TypeError("installed dependency RECORD path is not filesystem-backed")
            located_native_path = os.fspath(located_file)
            if not isinstance(located_native_path, str):
                raise TypeError("installed dependency RECORD path is not text")
            target = Path(os.path.abspath(located_native_path))
            if os.path.commonpath((str(target), str(prefix))) != str(prefix):
                _fail("installed dependency RECORD contains a path outside the approved prefix")
            target_key = os.path.normcase(str(target))
            owned_paths.add(target_key)
            parts = PurePosixPath(relative).parts
            if parts and parts[0] not in {".", ".."}:
                top = parts[0]
                if top.endswith((".py", ".pyd")):
                    top = Path(top).stem
                if top.isidentifier():
                    top_level_names.add(top)
            unhashed_allowed = relative.endswith(".dist-info/RECORD") or relative.endswith(".pyc")
            if not hash_spec and not unhashed_allowed:
                _fail(f"installed dependency {distribution_name} contains an unhashed file")
            if hash_spec:
                algorithm, separator, encoded = hash_spec.partition("=")
                if separator != "=" or algorithm != "sha256" or not encoded:
                    _fail("installed dependency RECORD uses an unapproved digest")
                try:
                    expected = base64.urlsafe_b64decode(encoded + "===")
                except (ValueError, binascii.Error):
                    _fail("installed dependency RECORD digest is invalid")
                try:
                    expected_size = int(size_text) if size_text else None
                except ValueError:
                    _fail("installed dependency RECORD size is invalid")
                owned_hashes[target_key] = (expected, expected_size)
        if not _same_file_snapshot(
            record_snapshot, os.fstat(record_handle.fileno())
        ) or not _same_file_snapshot(record_snapshot, record_path.lstat()):
            _fail("installed dependency RECORD changed while its captured bytes were parsed")
        return owned_paths, owned_hashes, top_level_names, record_sha256
    except (OSError, UnicodeError) as exc:
        _fail(f"installed dependency RECORD is unreadable: {exc}")
    finally:
        for handle in record_handles:
            handle.close()


def _assert_locked_dependencies(
    root: Path,
    dependency_paths: tuple[Path, ...],
    release_bytes: dict[str, bytes],
) -> tuple[frozenset[str], frozenset[str], dict[str, tuple[bytes, int | None]]]:
    """Require the actual interpreter environment to match requirements.lock."""

    requirements = _locked_requirements(root, release_bytes)
    prefix = Path(sysconfig.get_config_var("prefix") or sys.prefix).resolve(strict=True)
    installed: dict[str, list[importlib.metadata.Distribution]] = {}
    owned_paths: set[str] = set()
    owned_hashes: dict[str, tuple[bytes, int | None]] = {}
    allowed_top_level: set[str] = set()
    record_contract_rows: list[str] = []
    for dependency in dependency_paths:
        for dist in importlib.metadata.distributions(path=[str(dependency)]):
            name = dist.metadata.get("Name")
            if not name:
                _fail("installed dependency metadata has no package name")
            normalized = re.sub(r"[-_.]+", "-", name).lower()
            installed.setdefault(normalized, []).append(dist)
    for name, version in requirements.items():
        matches = installed.get(name, [])
        if len(matches) != 1 or matches[0].version != version:
            _fail(f"installed dependency does not exactly match requirements.lock: {name}")
        distribution_paths, distribution_hashes, distribution_top_level, record_sha256 = (
            _read_distribution_record(matches[0], prefix, name)
        )
        owned_paths.update(distribution_paths)
        for path, contract in distribution_hashes.items():
            if path in owned_hashes and owned_hashes[path] != contract:
                _fail("installed dependency RECORD ownership is ambiguous")
            owned_hashes[path] = contract
        allowed_top_level.update(distribution_top_level)
        record_contract_rows.append(f"{name}\0{version}\0{record_sha256}\n")
    record_contract = hashlib.sha256("".join(record_contract_rows).encode()).hexdigest()
    if record_contract != _APPROVED_DISTRIBUTION_RECORD_SET_SHA256:
        _fail("installed dependency RECORD set is not the source-approved runtime contract")
    return frozenset(allowed_top_level), frozenset(owned_paths), owned_hashes


class _LockedDependencyGuard(importlib.abc.MetaPathFinder):
    def __init__(
        self,
        dependency_paths: tuple[Path, ...],
        allowed: frozenset[str],
        owned_paths: frozenset[str],
        owned_hashes: dict[str, tuple[bytes, int | None]],
    ) -> None:
        self._dependency_paths = [str(path) for path in dependency_paths]
        self._allowed = allowed
        self._owned_paths = owned_paths
        self._owned_hashes = owned_hashes

    def find_spec(self, fullname: str, path=None, target=None):  # type: ignore[no-untyped-def]
        if path is None:
            trusted_paths = [entry for entry in sys.path if entry not in self._dependency_paths]
            if importlib.machinery.PathFinder.find_spec(fullname, trusted_paths) is not None:
                return None
            search_paths = self._dependency_paths
        else:
            search_paths = [
                str(entry)
                for entry in path
                if any(
                    os.path.commonpath((str(entry), dependency)) == dependency
                    for dependency in self._dependency_paths
                )
            ]
            if not search_paths:
                return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, search_paths)
        if spec is None:
            return None
        if fullname.split(".", 1)[0] not in self._allowed:
            raise ModuleNotFoundError(f"dependency import is not locked: {fullname}")
        if spec.origin not in {None, "namespace"}:
            origin_path = Path(spec.origin).resolve(strict=True)
            origin = os.path.normcase(str(origin_path))
            if origin not in self._owned_paths:
                raise ModuleNotFoundError(f"dependency import path is not RECORD-owned: {fullname}")
            if origin_path.suffix.lower() in {".pyd", ".dll", ".so"}:
                _read_verified_dependency_bytes(origin_path, self._owned_hashes)
        return None


def _read_verified_dependency_bytes(
    path: Path, owned_hashes: dict[str, tuple[bytes, int | None]]
) -> bytes:
    resolved = path.resolve(strict=True)
    if _is_reparse(resolved) or any(_is_reparse(parent) for parent in resolved.parents):
        _fail("dependency import path contains a reparse point")
    key = os.path.normcase(str(resolved))
    contract = owned_hashes.get(key)
    if contract is None:
        _fail("dependency import has no anchored RECORD digest")
    contents = resolved.read_bytes()
    expected, expected_size = contract
    if expected_size is not None and len(contents) != expected_size:
        _fail("dependency import file size changed")
    if hashlib.sha256(contents).digest() != expected:
        _fail("dependency import file hash changed")
    return contents


class _VerifiedSourceLoader(importlib.machinery.SourceFileLoader):
    """Compile governed dependency source directly; never consume unsealed pyc."""

    owned_hashes: dict[str, tuple[bytes, int | None]] = {}

    def get_code(self, fullname: str):  # type: ignore[no-untyped-def]
        source_path = self.get_filename(fullname)
        source = _read_verified_dependency_bytes(Path(source_path), self.owned_hashes)
        return self.source_to_code(source, source_path)


class _VerifiedReleaseSourceLoader(importlib.machinery.SourceFileLoader):
    """Compile only captured exact-commit source from the physical release root."""

    source_bytes: dict[str, bytes] = {}

    def get_code(self, fullname: str):  # type: ignore[no-untyped-def]
        source_path = self.get_filename(fullname)
        key = os.path.normcase(os.path.abspath(source_path))
        source = self.source_bytes.get(key)
        if source is None:
            raise ModuleNotFoundError(
                f"release source import is not exact-commit owned: {fullname}"
            )
        return self.source_to_code(source, source_path)


def _install_verified_release_importer(root: Path, source_bytes: dict[str, bytes]) -> None:
    """Restrict every release-root path entry to the captured tracked inventory."""

    _VerifiedReleaseSourceLoader.source_bytes = source_bytes
    governed_file_finder = importlib.machinery.FileFinder.path_hook(
        (_VerifiedReleaseSourceLoader, importlib.machinery.SOURCE_SUFFIXES),
    )
    normalized_root = os.path.normcase(os.path.abspath(root))

    def governed_release_path_hook(path: str):  # type: ignore[no-untyped-def]
        normalized = os.path.normcase(os.path.abspath(path))
        try:
            if os.path.commonpath((normalized, normalized_root)) != normalized_root:
                raise ImportError
        except ValueError as exc:
            raise ImportError from exc
        return governed_file_finder(path)

    sys.path_hooks.insert(0, governed_release_path_hook)
    sys.path_importer_cache.pop(str(root), None)
    for source_path in source_bytes:
        parent = Path(source_path).parent
        while root == parent or root in parent.parents:
            sys.path_importer_cache.pop(str(parent), None)
            if parent == root:
                break
            parent = parent.parent


def _run_verified_release_script(
    script: Path, source_bytes: dict[str, bytes], argv: list[str]
) -> None:
    """Execute a tracked target from captured bytes, never a second path read."""

    key = os.path.normcase(os.path.abspath(script))
    source = source_bytes.get(key)
    if source is None:
        raise RuntimeError("release bootstrap target is not tracked exact-commit Python source")
    sys.argv = [str(script), *argv]
    code = compile(source, str(script), "exec")
    namespace = {
        "__name__": "__main__",
        "__file__": str(script),
        "__cached__": None,
        "__doc__": None,
        "__loader__": None,
        "__package__": None,
        "__spec__": None,
    }
    exec(code, namespace)  # nosec B102 - code is captured from the verified exact commit


def _install_verified_dependency_importers(
    dependency_paths: tuple[Path, ...],
    allowed: frozenset[str],
    owned_paths: frozenset[str],
    owned_hashes: dict[str, tuple[bytes, int | None]],
) -> None:
    _VerifiedSourceLoader.owned_hashes = owned_hashes
    governed_file_finder = importlib.machinery.FileFinder.path_hook(
        (_VerifiedSourceLoader, importlib.machinery.SOURCE_SUFFIXES),
        (importlib.machinery.ExtensionFileLoader, importlib.machinery.EXTENSION_SUFFIXES),
    )
    dependency_roots = [os.path.normcase(str(path)) for path in dependency_paths]

    def governed_dependency_path_hook(path: str):  # type: ignore[no-untyped-def]
        normalized = os.path.normcase(os.path.abspath(path))
        if not any(
            os.path.commonpath((normalized, dependency)) == dependency
            for dependency in dependency_roots
        ):
            raise ImportError
        return governed_file_finder(path)

    sys.path_hooks.insert(0, governed_dependency_path_hook)
    path_finder_index = next(
        (
            index
            for index, finder in enumerate(sys.meta_path)
            if finder is importlib.machinery.PathFinder
        ),
        len(sys.meta_path),
    )
    sys.meta_path.insert(
        path_finder_index,
        _LockedDependencyGuard(dependency_paths, allowed, owned_paths, owned_hashes),
    )
    for dependency in dependency_paths:
        sys.path_importer_cache.pop(str(dependency), None)


def _parse_bootstrap_args(argv: list[str] | None) -> tuple[argparse.Namespace, list[str]]:
    """Parse bootstrap flags without consuming the target's own options."""

    parser = argparse.ArgumentParser(add_help=False, allow_abbrev=False)
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--expected-sha", required=True)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--module")
    target.add_argument("--script")
    raw = list(sys.argv[1:] if argv is None else argv)
    if "--" in raw:
        separator = raw.index("--")
        bootstrap_args = raw[:separator]
        target_args = raw[separator + 1 :]
        return parser.parse_args(bootstrap_args), target_args
    # Backwards-compatible direct invocation. add_help=False ensures a target
    # module's --help is returned as remainder rather than causing the
    # bootstrap parser to exit before dispatch.
    return parser.parse_known_args(raw)


def main(argv: list[str] | None = None) -> int:
    args, remainder = _parse_bootstrap_args(argv)
    root = _release_root(args.release_root)
    retry_budget = _ExactSourceRetryBudget()
    source_guard = retry_budget.admit(root, args.expected_sha)
    source_guard_open = True
    try:
        _install_verified_release_importer(root, source_guard.source_bytes)
        sys.path.insert(0, str(root))
        dependency_paths = _append_governed_dependencies()
        allowed_dependencies, owned_dependency_paths, owned_dependency_hashes = (
            _assert_locked_dependencies(root, dependency_paths, source_guard.source_bytes)
        )
        _install_verified_dependency_importers(
            dependency_paths,
            allowed_dependencies,
            owned_dependency_paths,
            owned_dependency_hashes,
        )
        _assert_package_from(root)
        try:
            source_guard.assert_unchanged()
        except RuntimeError as exc:
            # Dependency admission and importer setup are deterministic products
            # of the exact captured release plus pinned interpreter. If the one
            # eligible notification arrives here, close every old guard first,
            # re-admit from zero, and prove both source/Git and dependency
            # contracts byte-for-byte identical before retaining those importers.
            retry_budget.consume(exc)
            original_source_bytes = dict(source_guard.source_bytes)
            original_git_contract = source_guard.git_contract
            source_guard.close()
            source_guard_open = False
            refreshed_guard = _assert_exact_source(root, args.expected_sha)
            try:
                if (
                    refreshed_guard.source_bytes != original_source_bytes
                    or refreshed_guard.git_contract != original_git_contract
                ):
                    _fail("exact source retry produced a different admitted release contract")
                (
                    refreshed_allowed_dependencies,
                    refreshed_owned_dependency_paths,
                    refreshed_owned_dependency_hashes,
                ) = _assert_locked_dependencies(
                    root, dependency_paths, refreshed_guard.source_bytes
                )
                if (
                    refreshed_allowed_dependencies != allowed_dependencies
                    or refreshed_owned_dependency_paths != owned_dependency_paths
                    or refreshed_owned_dependency_hashes != owned_dependency_hashes
                ):
                    _fail("exact source retry produced a different dependency contract")
                _VerifiedReleaseSourceLoader.source_bytes = refreshed_guard.source_bytes
                _VerifiedSourceLoader.owned_hashes = refreshed_owned_dependency_hashes
                _assert_package_from(root)
                refreshed_guard.assert_unchanged()
            except Exception:
                refreshed_guard.close()
                raise
            source_guard = refreshed_guard
            source_guard_open = True
        # The audit hook is process-permanent, so install it only after the last
        # retryable pre-dispatch checkpoint has passed.
        _install_verified_git_dispatch_guard(root, source_guard)
        if args.module:
            sys.argv = [args.module, *remainder]
            runpy.run_module(args.module, run_name="__main__")
        else:
            script = Path(args.script).resolve(strict=True)
            if root not in script.parents:
                raise RuntimeError("release bootstrap script is outside the exact release root")
            _run_verified_release_script(script, source_guard.source_bytes, remainder)
        source_guard.assert_unchanged()
        return 0
    finally:
        if source_guard_open:
            try:
                source_guard.assert_unchanged()
            finally:
                source_guard.close()


if __name__ == "__main__":
    raise SystemExit(main())
