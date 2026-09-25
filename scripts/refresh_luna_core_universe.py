"""Refresh the governed Luna core manifest from an exact current NDX export.

The refresh is staged and validated before the manifest pointer is replaced.
An unavailable, stale, malformed, or unexpected source leaves the previous
manifest untouched.  This script only writes the caller-provided state root;
it does not touch broker configuration or the runtime checkout.
"""

from __future__ import annotations

import argparse
import errno
import hashlib
import json
import os
import socket
import stat
import sys
import tempfile
import uuid
import zipfile
from contextlib import ExitStack, contextmanager
from datetime import date, datetime, timezone
from pathlib import Path
from typing import BinaryIO
from urllib.error import URLError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.luna_core_universe_service import (
    _TRUSTED_SOURCE_ROOTS,
    ACTIVE_POINTER_SCHEMA_VERSION,
    STATE_STREET_SPY_HOLDINGS_URL,
    _active_pointer_target,
    _canonical_member_hash,
    _nasdaq_sod_url_for_date,
    _parse_nasdaq_sod_weightings_xlsx_with_attestation,
    _parse_spy_holdings_xlsx_with_attestation,
    build_core_universe_contract,
    read_core_universe_manifest,
)

MAX_DOWNLOAD_BYTES = 2_000_000
NDX_SOURCE_ID = "nasdaq-ndx-point-in-time-2026-08-27"
SPY_SOURCE_ID = "state-street-spy-holdings-proxy-2026-08-24"
GENERATION_DIRECTORY = "luna_core_universe_generations"
# The release root anchors trust, but is not a recurring-session gate.  A
# requested later date is accepted only when the fresh official source still
# replays to this root's exact governed schema/content/member set.
RELEASE_ANCHOR_MARKET_DATE = "2026-08-27"
# Compatibility alias for callers that imported the old constant.  It is not
# used to reject a requested market date.
SUPPORTED_MARKET_DATE = RELEASE_ANCHOR_MARKET_DATE
REFRESH_LOCK_NAME = ".luna_core_universe.refresh.lock"
REFRESH_LOCK_SCHEMA_VERSION = "dawnstrike.luna.core_universe_refresh_lock.v1"
DIRECTORY_BOUNDARY_MARKER_NAME = ".luna_core_universe.directory_boundary.v1"
DIRECTORY_BOUNDARY_MARKER_BYTES = b"dawnstrike.luna.core_universe.directory_boundary.v1\n"
SPY_ARTIFACT_NAME = "spy-holdings.xlsx"


def _absolute_without_reparse_resolution(path: Path) -> Path:
    return Path(os.path.abspath(os.fspath(path)))


def _is_reparse_stat(value: os.stat_result) -> bool:
    return stat.S_ISLNK(value.st_mode) or bool(int(getattr(value, "st_file_attributes", 0)) & 0x400)


def _assert_no_reparse_components(path: Path, *, label: str) -> Path:
    """Return an absolute lexical path after rejecting every existing link component."""

    absolute = _absolute_without_reparse_resolution(path)
    missing_parent = False
    for component in [*reversed(absolute.parents), absolute]:
        try:
            metadata = os.lstat(component)
        except FileNotFoundError:
            missing_parent = True
            continue
        except OSError as exc:
            raise RuntimeError(f"{label} metadata is unavailable: {component}") from exc
        if missing_parent:
            raise RuntimeError(f"{label} changed below a missing parent: {component}")
        if _is_reparse_stat(metadata):
            raise RuntimeError(
                f"{label} contains a symlink, junction, or reparse point: {component}"
            )
        if component != absolute and not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError(f"{label} has a non-directory parent: {component}")
    return absolute


def _assert_path_type(
    path: Path,
    *,
    label: str,
    expected: str,
    allow_missing: bool = False,
) -> Path:
    absolute = _assert_no_reparse_components(path, label=label)
    try:
        metadata = os.lstat(absolute)
    except FileNotFoundError:
        if allow_missing:
            return absolute
        raise RuntimeError(f"{label} is missing: {absolute}") from None
    except OSError as exc:
        raise RuntimeError(f"{label} metadata is unavailable: {absolute}") from exc
    valid = (
        stat.S_ISDIR(metadata.st_mode)
        if expected == "directory"
        else stat.S_ISREG(metadata.st_mode)
    )
    if not valid:
        raise RuntimeError(f"{label} is not a regular {expected}: {absolute}")
    return absolute


def _ensure_regular_directory(path: Path, *, label: str) -> Path:
    absolute = _assert_no_reparse_components(path, label=label)
    try:
        absolute.mkdir()
    except FileExistsError:
        pass
    except OSError as exc:
        raise RuntimeError(f"{label} could not be created safely: {absolute}") from exc
    return _assert_path_type(absolute, label=label, expected="directory")


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (int(left.st_dev), int(left.st_ino)) == (int(right.st_dev), int(right.st_ino))


def _same_file_snapshot(left: os.stat_result, right: os.stat_result) -> bool:
    # Windows' CRT fstat reports creation time in st_ctime_ns while lstat can
    # expose a slightly different conversion for the same handle identity.
    # The opened handle denies writes; POSIX additionally compares change time.
    fields: tuple[str, ...] = (
        "st_dev",
        "st_ino",
        "st_mode",
        "st_nlink",
        "st_size",
        "st_mtime_ns",
    )
    if os.name != "nt":
        fields += ("st_ctime_ns",)
    return all(int(getattr(left, field)) == int(getattr(right, field)) for field in fields)


def _same_file_after_namespace_move(left: os.stat_result, right: os.stat_result) -> bool:
    """Compare one inode across a rename without treating POSIX ctime as content drift."""

    if os.name == "nt":
        return _same_file_snapshot(left, right)
    # A successful POSIX rename updates ctime on the moved inode. Keep the
    # pre-rename snapshot check strict, but after that namespace operation bind
    # the result to the exact inode and every stable file/content attribute.
    fields = ("st_dev", "st_ino", "st_mode", "st_nlink", "st_size", "st_mtime_ns")
    return all(int(getattr(left, field)) == int(getattr(right, field)) for field in fields)


def _lstat_from_directory(path: Path, directory_fd: int | None) -> os.stat_result:
    """lstat a child through an admitted POSIX directory descriptor when supplied."""

    if directory_fd is None:
        return os.lstat(path)
    return os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)


def _optional_lstat_from_directory(
    path: Path,
    directory_fd: int,
) -> os.stat_result | None:
    try:
        return _lstat_from_directory(path, directory_fd)
    except FileNotFoundError:
        return None


def _open_child_directory(
    parent_fd: int,
    path: Path,
    *,
    label: str,
    create: bool,
) -> int:
    """Open one direct child directory through an admitted POSIX parent."""

    name = path.name
    if not name or name in {".", ".."}:
        raise RuntimeError(f"{label} has an invalid child name: {path}")
    if create:
        try:
            os.mkdir(name, 0o700, dir_fd=parent_fd)
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeError(f"{label} could not be created safely: {path}") from exc
    try:
        before = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except FileNotFoundError:
        raise RuntimeError(f"{label} is missing: {path}") from None
    except OSError as exc:
        raise RuntimeError(f"{label} metadata is unavailable: {path}") from exc
    if _is_reparse_stat(before) or not stat.S_ISDIR(before.st_mode):
        raise RuntimeError(f"{label} is not a regular directory: {path}")
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(name, flags, dir_fd=parent_fd)
    except OSError as exc:
        raise RuntimeError(f"{label} could not be identity-bound: {path}") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
    except OSError as exc:
        os.close(descriptor)
        raise RuntimeError(f"{label} could not be identity-bound: {path}") from exc
    if (
        _is_reparse_stat(current)
        or not stat.S_ISDIR(opened.st_mode)
        or not _same_file_identity(before, opened)
        or not _same_file_identity(opened, current)
    ):
        os.close(descriptor)
        raise RuntimeError(f"{label} changed during admission: {path}")
    return descriptor


def _active_generation_relative_path(pointer: dict[str, object]) -> Path:
    """Validate the one governed active-generation path without resolving it."""

    generation_id = pointer.get("generation_id")
    if (
        not isinstance(generation_id, str)
        or not generation_id
        or generation_id in {".", ".."}
        or "/" in generation_id
        or "\\" in generation_id
    ):
        raise RuntimeError("active pointer generation id is missing or invalid")
    expected = Path(GENERATION_DIRECTORY) / generation_id / "luna_core_universe.json"
    target_text = pointer.get("manifest_path")
    if not isinstance(target_text, str) or target_text != expected.as_posix():
        raise RuntimeError("active pointer manifest path is not the governed generation target")
    return expected


@contextmanager
def _open_bound_active_generation(
    config_directory_fd: int,
    pointer: dict[str, object],
    *,
    config_root: Path,
):
    """Open the active generation solely through the admitted config descriptor."""

    relative = _active_generation_relative_path(pointer)
    generations_root = config_root / relative.parts[0]
    generation_dir = generations_root / relative.parts[1]
    with ExitStack() as held:
        generations_fd = _open_child_directory(
            config_directory_fd,
            generations_root,
            label="core-universe generation root",
            create=False,
        )
        held.callback(os.close, generations_fd)
        held.enter_context(
            _hold_posix_directory_mutation_watch(
                generations_fd,
                label="core-universe generation root",
                include_children=True,
            )
        )
        generation_fd = _open_child_directory(
            generations_fd,
            generation_dir,
            label="core-universe active generation directory",
            create=False,
        )
        held.callback(os.close, generation_fd)
        held.enter_context(
            _hold_posix_directory_mutation_watch(
                generation_fd,
                label="core-universe active generation directory",
                include_children=True,
            )
        )
        admitted = os.fstat(generation_fd)
        try:
            yield generation_fd, config_root / relative
        finally:
            reopened_fd = _open_child_directory(
                generations_fd,
                generation_dir,
                label="core-universe active generation directory",
                create=False,
            )
            try:
                if not _same_file_identity(admitted, os.fstat(reopened_fd)):
                    raise RuntimeError("core-universe active generation name changed while held")
            finally:
                os.close(reopened_fd)


def _read_bound_active_manifest(
    generation_directory_fd: int,
    manifest_path: Path,
    pointer: dict[str, object],
) -> tuple[bytes, dict[str, object]]:
    raw = _read_optional_file_from_directory(
        manifest_path,
        generation_directory_fd,
        label="core-universe active target",
    )
    if raw is None:
        raise RuntimeError(f"core-universe active target is missing: {manifest_path}")
    expected_hash = str(pointer.get("manifest_sha256") or "").strip().lower()
    if len(expected_hash) != 64 or any(
        character not in "0123456789abcdef" for character in expected_hash
    ):
        raise RuntimeError("active pointer manifest SHA-256 is missing or invalid")
    if hashlib.sha256(raw).hexdigest() != expected_hash:
        raise RuntimeError("active pointer manifest SHA-256 mismatch")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("active target manifest JSON is invalid") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("active target manifest must be a JSON object")
    if parsed.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION:
        raise RuntimeError("nested active generation pointers are not permitted")
    return raw, parsed


@contextmanager
def _open_refresh_lock_handle(
    path: Path,
    *,
    label: str,
    allow_delete_share: bool = False,
    exact_namespace_mutation: bool = False,
    directory_fd: int | None = None,
):
    """Open one exact inode while denying competing readers/writers.

    A stale lock must remain renameable by this process, so that admission uses
    DELETE sharing only.  A live lock instead uses READ sharing only and is
    therefore immutable and non-deletable for its full critical section.
    An exact Windows namespace-mutation source additionally requests DELETE
    access while continuing to deny competing writes and deletes.  That access
    is used for both handle-bound rename and handle-bound deletion.
    POSIX contenders are serialized with an advisory exclusive lock in
    addition to the no-follow descriptor identity checks.
    """

    if directory_fd is not None and os.name == "nt":
        raise RuntimeError(f"{label} requested a POSIX directory descriptor on Windows")
    if directory_fd is None:
        absolute = _assert_path_type(path, label=label, expected="file")
    else:
        absolute = _absolute_without_reparse_resolution(path)
    try:
        before = _lstat_from_directory(absolute, directory_fd)
    except FileNotFoundError:
        raise RuntimeError(f"{label} is missing: {absolute}") from None
    except OSError as exc:
        raise RuntimeError(f"{label} metadata is unavailable: {absolute}") from exc
    if _is_reparse_stat(before) or not stat.S_ISREG(before.st_mode):
        raise RuntimeError(f"{label} is not a regular file: {absolute}")
    if int(getattr(before, "st_nlink", 1)) != 1:
        raise RuntimeError(f"{label} must have exactly one filesystem link: {absolute}")

    stream: BinaryIO | None = None
    if os.name == "nt":
        import ctypes
        import msvcrt
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
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        if allow_delete_share and exact_namespace_mutation:
            raise RuntimeError(f"{label} requested incompatible handle rights")
        share_mode = 0x4 if allow_delete_share else 0x1
        desired_access = 0x80000000 | (0x00010000 if exact_namespace_mutation else 0)
        raw_handle = create_file(
            str(absolute),
            desired_access,
            share_mode,
            None,
            3,
            0x00200000,
            None,
        )
        if raw_handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            raise OSError(error, f"{label} could not be identity-locked")
        try:
            descriptor = msvcrt.open_osfhandle(
                int(raw_handle), os.O_RDONLY | int(getattr(os, "O_BINARY", 0))
            )
        except Exception:
            close_handle(raw_handle)
            raise
        stream = os.fdopen(descriptor, "rb", closefd=True)
    else:
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
        if directory_fd is None:
            descriptor = os.open(absolute, flags)
        else:
            descriptor = os.open(absolute.name, flags, dir_fd=directory_fd)
        try:
            import fcntl

            fcntl.flock(  # type: ignore[attr-defined]
                descriptor,
                fcntl.LOCK_EX | fcntl.LOCK_NB,  # type: ignore[attr-defined]
            )
        except Exception:
            os.close(descriptor)
            raise
        stream = os.fdopen(descriptor, "rb", closefd=True)

    try:
        opened = os.fstat(stream.fileno())
        current = _lstat_from_directory(absolute, directory_fd)
        if (
            _is_reparse_stat(current)
            or not stat.S_ISREG(opened.st_mode)
            or not _same_file_snapshot(before, opened)
            or not _same_file_snapshot(opened, current)
            or int(getattr(opened, "st_nlink", 1)) != 1
        ):
            raise RuntimeError(f"{label} changed while its exact handle was acquired")
        yield stream
    finally:
        stream.close()


def _read_optional_file_from_directory(
    path: Path,
    directory_fd: int,
    *,
    label: str,
) -> bytes | None:
    metadata = _optional_lstat_from_directory(path, directory_fd)
    if metadata is None:
        return None
    if _is_reparse_stat(metadata) or not stat.S_ISREG(metadata.st_mode):
        raise RuntimeError(f"{label} is not a regular file: {path}")
    with _open_refresh_lock_handle(path, label=label, directory_fd=directory_fd) as handle:
        admitted = os.fstat(handle.fileno())
        payload = handle.read()
        final = os.fstat(handle.fileno())
        current = _lstat_from_directory(path, directory_fd)
        if not _same_file_snapshot(admitted, final) or not _same_file_snapshot(final, current):
            raise RuntimeError(f"{label} changed while read")
        return payload


def _build_contract_from_bound_generation(
    manifest_bytes: bytes,
    generation_directory_fd: int,
    manifest_path: Path,
    *,
    observed_at: str,
    market_date: str,
    expected_artifacts: dict[str, bytes] | None = None,
) -> tuple[dict[str, object], dict[str, object], dict[str, bytes]]:
    """Validate a generation using only byte snapshots read through its descriptor."""

    try:
        wrapper = json.loads(manifest_bytes.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("core-universe generation manifest JSON is invalid") from exc
    if not isinstance(wrapper, dict):
        raise RuntimeError("core-universe generation manifest must be a JSON object")
    children = wrapper.get("manifests")
    if not isinstance(children, list):
        raise RuntimeError("core-universe generation manifest must contain a manifests list")

    bound_artifacts: dict[str, bytes] = {}
    validated_children: list[object] = []
    for child in children:
        if not isinstance(child, dict):
            raise RuntimeError("core-universe generation manifest contains a non-object child")
        index_name = (
            str(child.get("index_name") or child.get("index") or "")
            .strip()
            .lower()
            .replace(" ", "")
        )
        if index_name in {"nasdaq-100", "nasdaq100", "ndx"}:
            artifact_name = _ndx_artifact_name(market_date)
        elif index_name in {"s&p500", "sp500", "sandp500"}:
            artifact_name = SPY_ARTIFACT_NAME
        else:
            raise RuntimeError("core-universe generation manifest contains an unknown index")
        if artifact_name in bound_artifacts:
            raise RuntimeError("core-universe generation manifest contains a duplicate index")

        entries = child.get("source_artifacts")
        if not isinstance(entries, list) or len(entries) != 1 or not isinstance(entries[0], dict):
            raise RuntimeError(
                f"core-universe generation {artifact_name} must declare exactly one artifact"
            )
        entry = dict(entries[0])
        declared = entry.get("path") or entry.get("file") or entry.get("local_path")
        if not isinstance(declared, str) or not declared:
            raise RuntimeError(f"core-universe generation {artifact_name} path is missing")
        declared_path = Path(declared)
        expected_path = manifest_path.parent / artifact_name
        if declared_path.is_absolute():
            declaration_matches = os.path.normcase(
                os.fspath(_absolute_without_reparse_resolution(declared_path))
            ) == os.path.normcase(os.fspath(_absolute_without_reparse_resolution(expected_path)))
        else:
            declaration_matches = declared_path.parts == (artifact_name,)
        if not declaration_matches:
            raise RuntimeError(
                f"core-universe generation {artifact_name} path leaves its admitted directory"
            )

        payload = _read_optional_file_from_directory(
            expected_path,
            generation_directory_fd,
            label=f"core-universe generation {artifact_name}",
        )
        if payload is None:
            raise RuntimeError(f"core-universe generation {artifact_name} is missing")
        if expected_artifacts is not None and expected_artifacts.get(artifact_name) != payload:
            raise RuntimeError(f"core-universe generation {artifact_name} bytes changed")
        bound_artifacts[artifact_name] = payload
        for key in ("path", "file", "local_path"):
            entry.pop(key, None)
        entry["content"] = payload
        validated_children.append({**child, "source_artifacts": [entry]})

    expected_names = {_ndx_artifact_name(market_date), SPY_ARTIFACT_NAME}
    if set(bound_artifacts) != expected_names:
        raise RuntimeError("core-universe generation does not contain the exact governed indexes")
    validation_wrapper = {**wrapper, "manifests": validated_children}
    contract = build_core_universe_contract(
        validation_wrapper,
        observed_at=observed_at,
        market_date=market_date,
    )
    return contract, wrapper, bound_artifacts


def _generation_index_child(
    wrapper: dict[str, object],
    aliases: set[str],
    *,
    label: str,
) -> tuple[dict[str, object], list[object]]:
    children = wrapper.get("manifests")
    if not isinstance(children, list):
        raise RuntimeError("active core generation must contain a manifests list")
    child = next(
        (
            item
            for item in children
            if isinstance(item, dict)
            and str(item.get("index_name") or item.get("index") or "")
            .strip()
            .lower()
            .replace(" ", "")
            in aliases
        ),
        None,
    )
    if not isinstance(child, dict):
        raise RuntimeError(f"active core generation is missing its {label} manifest")
    members = child.get("members")
    if not isinstance(members, list):
        raise RuntimeError(f"active core generation {label} members are invalid")
    return child, members


def _windows_delete_from_handle(handle: BinaryIO) -> None:
    """Mark the exact already-open Windows file for deletion."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileDispositionInfo(ctypes.Structure):
        _fields_ = [("delete_file", wintypes.BOOLEAN)]

    information = _FileDispositionInfo(True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    if not set_information(
        msvcrt.get_osfhandle(handle.fileno()),
        4,  # FileDispositionInfo
        ctypes.byref(information),
        ctypes.sizeof(information),
    ):
        error = ctypes.get_last_error()
        raise OSError(error, os.strerror(error))


def _delete_refresh_lock_from_exact_handle(
    handle: BinaryIO,
    lock_path: Path,
    owner_bytes: bytes,
    *,
    directory_fd: int | None = None,
) -> None:
    """Delete only the admitted live lock while its exact handle remains held."""

    admitted = os.fstat(handle.fileno())
    handle.seek(0)
    if handle.read() != owner_bytes:
        raise RuntimeError("core universe refresh lock bytes changed before cleanup")
    current = _lstat_from_directory(lock_path, directory_fd)
    if not _same_file_snapshot(admitted, current):
        raise RuntimeError("core universe refresh lock was replaced before cleanup")
    if os.name == "nt":
        _windows_delete_from_handle(handle)
        return

    # Cooperative POSIX contenders must hold the advisory flock acquired by
    # _open_refresh_lock_handle. Keep that exact descriptor open across the
    # identity check and unlink, then prove the admitted inode lost its name.
    if directory_fd is None:
        os.unlink(lock_path)
    else:
        os.unlink(lock_path.name, dir_fd=directory_fd)
    if int(getattr(os.fstat(handle.fileno()), "st_nlink", 1)) != 0:
        raise RuntimeError("core universe refresh lock cleanup removed the wrong identity")


@contextmanager
def _hold_regular_directory(
    path: Path,
    *,
    label: str,
    allow_write_share: bool = False,
):
    """Hold a Windows directory against replacement and bind its POSIX identity."""

    absolute = _assert_path_type(path, label=label, expected="directory")
    before = os.lstat(absolute)
    windows_handle: int | None = None
    windows_close = None
    descriptor: int | None = None
    if os.name == "nt":
        import ctypes
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
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        windows_close = close_handle
        # FILE_SHARE_DELETE is never admitted, so the guarded directory cannot
        # be renamed or replaced. A write phase may admit FILE_SHARE_WRITE only
        # while an immutable child marker is held open below; that marker keeps
        # the directory nonempty and prevents an in-place reparse conversion
        # without breaking atomic child-file replacement.
        windows_handle = create_file(
            str(absolute),
            0x80000000,
            0x1 | (0x2 if allow_write_share else 0),
            None,
            3,
            0x02000000 | 0x00200000,
            None,
        )
        if windows_handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            raise RuntimeError(f"{label} could not be held safely (Windows error {error})")
    else:
        flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(absolute, flags)
        except OSError as exc:
            raise RuntimeError(f"{label} could not be held safely") from exc
    try:
        after_open = os.lstat(absolute)
        if _is_reparse_stat(after_open) or not _same_file_identity(before, after_open):
            raise RuntimeError(f"{label} changed while its replacement guard was acquired")
        if descriptor is not None and not _same_file_identity(os.fstat(descriptor), after_open):
            raise RuntimeError(f"{label} descriptor identity changed during admission")
        yield absolute
        final = os.lstat(absolute)
        if _is_reparse_stat(final) or not _same_file_identity(after_open, final):
            raise RuntimeError(f"{label} changed while its replacement guard was held")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if windows_handle is not None and windows_close is not None:
            windows_close(windows_handle)


@contextmanager
def _hold_regular_file(path: Path, *, label: str, expected_bytes: bytes):
    """Hold one exact regular file against writes, replacement, and deletion."""

    absolute = _assert_path_type(path, label=label, expected="file")
    before = os.lstat(absolute)
    if int(getattr(before, "st_nlink", 1)) != 1:
        raise RuntimeError(f"{label} must have exactly one filesystem link: {absolute}")
    windows_handle: int | None = None
    windows_close = None
    descriptor: int | None = None
    if os.name == "nt":
        import ctypes
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
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = [wintypes.HANDLE]
        close_handle.restype = wintypes.BOOL
        windows_close = close_handle
        windows_handle = create_file(
            str(absolute),
            0x80000000,
            0x1,
            None,
            3,
            0x00200000,
            None,
        )
        if windows_handle == ctypes.c_void_p(-1).value:
            error = ctypes.get_last_error()
            raise RuntimeError(f"{label} could not be held safely (Windows error {error})")
    else:
        flags = os.O_RDONLY | int(getattr(os, "O_NOFOLLOW", 0))
        try:
            descriptor = os.open(absolute, flags)
        except OSError as exc:
            raise RuntimeError(f"{label} could not be held safely") from exc
    try:
        after_open = os.lstat(absolute)
        if (
            _is_reparse_stat(after_open)
            or not _same_file_identity(before, after_open)
            or int(getattr(after_open, "st_nlink", 1)) != 1
        ):
            raise RuntimeError(f"{label} changed while its identity guard was acquired")
        if descriptor is not None and not _same_file_identity(os.fstat(descriptor), after_open):
            raise RuntimeError(f"{label} descriptor identity changed during admission")
        if absolute.read_bytes() != expected_bytes:
            raise RuntimeError(f"{label} has unexpected bytes: {absolute}")
        yield absolute
        final = os.lstat(absolute)
        if (
            _is_reparse_stat(final)
            or not _same_file_identity(after_open, final)
            or int(getattr(final, "st_nlink", 1)) != 1
            or absolute.read_bytes() != expected_bytes
        ):
            raise RuntimeError(f"{label} changed while its identity guard was held")
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if windows_handle is not None and windows_close is not None:
            windows_close(windows_handle)


def _ensure_directory_boundary_marker(directory: Path, *, label: str) -> Path:
    marker = _assert_path_type(
        directory / DIRECTORY_BOUNDARY_MARKER_NAME,
        label=label,
        expected="file",
        allow_missing=True,
    )
    if not os.path.lexists(marker):
        descriptor: int | None = None
        try:
            descriptor = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            with os.fdopen(descriptor, "wb") as handle:
                descriptor = None
                handle.write(DIRECTORY_BOUNDARY_MARKER_BYTES)
                handle.flush()
                os.fsync(handle.fileno())
        except FileExistsError:
            pass
        except OSError as exc:
            raise RuntimeError(f"{label} could not be created safely: {marker}") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)
    marker = _assert_path_type(marker, label=label, expected="file")
    if marker.read_bytes() != DIRECTORY_BOUNDARY_MARKER_BYTES:
        raise RuntimeError(f"{label} has unexpected bytes: {marker}")
    return marker


@contextmanager
def _hold_directory_write_boundary(path: Path, *, label: str):
    """Hold a directory through writes without admitting reparse conversion."""

    with ExitStack() as strict:
        absolute = strict.enter_context(_hold_regular_directory(path, label=label))
        marker = _ensure_directory_boundary_marker(
            absolute,
            label=f"{label} boundary marker",
        )
        with ExitStack() as held:
            held.enter_context(
                _hold_regular_file(
                    marker,
                    label=f"{label} boundary marker",
                    expected_bytes=DIRECTORY_BOUNDARY_MARKER_BYTES,
                )
            )
            held.enter_context(
                _hold_regular_directory(
                    absolute,
                    label=label,
                    allow_write_share=True,
                )
            )
            # The immutable child and relaxed no-delete directory handle now
            # overlap the strict handle, so there is no unguarded transition.
            strict.close()
            yield absolute


def _assert_universe_state_layout(state_root: Path, *, require_config: bool) -> Path:
    state = _assert_path_type(
        state_root,
        label="core-universe state root",
        expected="directory",
    )
    config = _assert_path_type(
        state / "config",
        label="core-universe config root",
        expected="directory",
        allow_missing=not require_config,
    )
    if not os.path.lexists(config):
        return state
    _assert_path_type(
        config / REFRESH_LOCK_NAME,
        label="core-universe refresh lock",
        expected="file",
        allow_missing=True,
    )
    _assert_path_type(
        config / "luna_core_universe.json",
        label="core-universe active pointer",
        expected="file",
        allow_missing=True,
    )
    _assert_path_type(
        config / GENERATION_DIRECTORY,
        label="core-universe generation root",
        expected="directory",
        allow_missing=True,
    )
    return state


def _normalise_market_date(value: str | None) -> str:
    requested = value or RELEASE_ANCHOR_MARKET_DATE
    try:
        return date.fromisoformat(str(requested)).isoformat()
    except (TypeError, ValueError) as exc:
        raise RuntimeError(f"market date must be an ISO date: {requested}") from exc


def _ndx_artifact_name(market_date: str) -> str:
    return f"ndx-sod-{market_date}.xlsx"


def _source_scope(root: dict[str, object], market_date: str) -> str:
    template = str(root.get("source_scope_template") or "").strip()
    if template:
        return template.format(market_date=market_date)
    return str(root.get("source_scope") or "").strip()


def _source_url(root: dict[str, object], market_date: str) -> str:
    if root.get("source_uri_template"):
        return _nasdaq_sod_url_for_date(market_date)
    return str(root.get("source_uri") or "").strip()


def _fetch(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "Dawnstrike/1 core-universe refresh"})
    try:
        with urlopen(request, timeout=30) as response:  # nosec B310 - fixed HTTPS roots below
            if response.status != 200:
                raise RuntimeError(f"source returned HTTP {response.status}")
            payload = response.read(MAX_DOWNLOAD_BYTES + 1)
    except (OSError, URLError) as exc:
        raise RuntimeError(f"source download failed: {exc}") from exc
    if len(payload) > MAX_DOWNLOAD_BYTES:
        raise RuntimeError("source download exceeded bounded size")
    return payload


def _read_json(path: Path) -> dict[str, object]:
    try:
        parsed = read_core_universe_manifest(path)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"proxy manifest unreadable: {exc}") from exc
    return parsed


def _resolve_proxy_paths(child: dict[str, object], base: Path) -> dict[str, object]:
    """Make retained proxy evidence paths independent of staging location."""

    resolved = dict(child)
    for key in ("raw_artifact", "raw_artifact_path"):
        path = resolved.get(key)
        if isinstance(path, str) and path and not Path(path).is_absolute():
            resolved[key] = str((base / path).resolve())
    entries = resolved.get("source_artifacts") or resolved.get("raw_artifacts")
    if isinstance(entries, list):
        copied: list[object] = []
        for entry in entries:
            if not isinstance(entry, dict):
                copied.append(entry)
                continue
            item = dict(entry)
            for key in ("path", "file", "local_path"):
                path = item.get(key)
                if isinstance(path, str) and path and not Path(path).is_absolute():
                    item[key] = str((base / path).resolve())
            copied.append(item)
        resolved["source_artifacts"] = copied
    if str(resolved.get("source_id") or "") == "state-street-spy-holdings-proxy-2026-08-24":
        resolved["source_uri"] = STATE_STREET_SPY_HOLDINGS_URL
        entries = resolved.get("source_artifacts")
        if isinstance(entries, list):
            resolved["source_artifacts"] = [
                {
                    **entry,
                    "uri": STATE_STREET_SPY_HOLDINGS_URL,
                }
                if isinstance(entry, dict)
                else entry
                for entry in entries
            ]
    return resolved


def _canonical_hash(records: list[dict[str, object]], *, effective_date: str) -> str:
    return _canonical_member_hash(
        [
            {
                "symbol": row["symbol"],
                "provider_symbol": row["provider_symbol"],
                "asset_class": row["asset_class"],
                "index": "Nasdaq-100",
                "valid_from": effective_date,
                "valid_to": None,
            }
            for row in records
        ]
    )


def _attest_ndx_payload(
    payload: bytes,
    *,
    market_date: str,
) -> tuple[list[str], dict[str, object]]:
    root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    try:
        symbols, attestation = _parse_nasdaq_sod_weightings_xlsx_with_attestation(
            payload, effective_date=market_date
        )
    except ValueError as exc:
        raise RuntimeError(
            f"NDX workbook is not the governed currentness root for {market_date}: {exc}"
        ) from exc
    expected_names = root.get("canonical_zip_member_names")
    expected_hashes = root.get("canonical_zip_member_hashes")
    expected_static = root.get("canonical_static_member_hashes")
    expected_content = str(root.get("canonical_content_digest_sha256") or "").lower()
    root_effective = str(root.get("effective_date") or "")
    if expected_names and list(attestation["member_names"]) != list(expected_names):
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: structure mismatch"
        )
    if expected_static and attestation["static_member_hashes"] != dict(expected_static):
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: static member mismatch"
        )
    if (
        market_date == root_effective
        and expected_hashes
        and attestation["member_hashes"] != dict(expected_hashes)
    ):
        raise RuntimeError(
            f"NDX workbook is not the governed currentness root for {market_date}: member mismatch"
        )
    if (
        market_date == root_effective
        and expected_content
        and attestation["content_digest_sha256"] != expected_content
    ):
        raise RuntimeError(
            f"NDX workbook is not the governed currentness root for {market_date}: content mismatch"
        )
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["Nasdaq-100"],
            "valid_from": market_date,
        }
        for symbol in symbols
    ]
    member_hash = _canonical_hash(
        [
            {
                "symbol": row["ticker"],
                "provider_symbol": row["provider_symbol"],
                "asset_class": row["asset_class"],
            }
            for row in records
        ],
        effective_date=market_date,
    )
    expected_member_set = str(root.get("canonical_member_set_hash_sha256") or "").lower()
    expected_symbol_set = str(root.get("canonical_symbol_set_hash_sha256") or "").lower()
    if expected_symbol_set and attestation.get("symbol_set_hash_sha256") != expected_symbol_set:
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: member set mismatch"
        )
    if (
        expected_member_set
        and market_date == str(root.get("effective_date") or "")
        and member_hash != expected_member_set
    ):
        raise RuntimeError(
            "NDX workbook is not the governed currentness root for "
            f"{market_date}: member set mismatch"
        )
    attestation["member_set_hash_sha256"] = member_hash
    return symbols, attestation


def _ndx_manifest(
    *,
    artifact_path: Path,
    payload: bytes,
    observed_at: str,
    market_date: str,
    parsed: tuple[list[str], dict[str, object]] | None = None,
) -> dict[str, object]:
    symbols, attestation = parsed or _attest_ndx_payload(payload, market_date=market_date)
    root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    source_uri = _source_url(root, market_date)
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["Nasdaq-100"],
            "valid_from": market_date,
        }
        for symbol in symbols
    ]
    member_hash = str(attestation["member_set_hash_sha256"])
    raw_hash = hashlib.sha256(payload).hexdigest()
    return {
        "source_id": NDX_SOURCE_ID,
        "source_uri": source_uri,
        "source_scope": _source_scope(root, market_date),
        "observed_at": observed_at,
        "effective_date": market_date,
        "reconstitution_id": root["reconstitution_id"],
        "index_name": "Nasdaq-100",
        "expected_count": 102,
        "completeness_verdict": "COMPLETE",
        "members": records,
        "canonical_zip_member_names": attestation["member_names"],
        "canonical_zip_member_hashes": attestation["member_hashes"],
        "canonical_static_member_hashes": attestation["static_member_hashes"],
        "canonical_content_digest_sha256": attestation["content_digest_sha256"],
        "canonical_member_set_hash_sha256": member_hash,
        "canonical_symbol_set_hash_sha256": attestation["symbol_set_hash_sha256"],
        "source_artifacts": [
            {
                "uri": source_uri,
                "path": str(artifact_path),
                "sha256": raw_hash,
                "byte_count": len(payload),
            }
        ],
        "reconstitution_lineage": {
            "schema_version": "dawnstrike.core_universe_lineage.v1",
            "builder_id": root["lineage_builder_id"],
            "transformation_id": root["lineage_transformation_id"],
            "reconstitution_id": root["reconstitution_id"],
            "effective_date": market_date,
            "input_artifact_hashes": [raw_hash],
            "canonical_member_set_hash_sha256": member_hash,
        },
    }


def _attest_spy_payload(
    payload: bytes,
    *,
    source_id: str,
    market_date: str,
) -> tuple[list[str], str, dict[str, object]]:
    """Capture a fresh State Street proxy only when its governed set matches."""

    root = _TRUSTED_SOURCE_ROOTS.get(source_id)
    if root is None:
        raise RuntimeError(f"SPY source trust root unknown: {source_id}")
    try:
        symbols, source_effective, attestation = _parse_spy_holdings_xlsx_with_attestation(
            [payload]
        )
    except (OSError, TypeError, ValueError, zipfile.BadZipFile) as exc:
        raise RuntimeError(f"SPY workbook is not a governed holdings capture: {exc}") from exc
    root_effective = str(root.get("effective_date") or "")
    maximum_age = int(root.get("maximum_source_age_days") or 0)
    try:
        source_age = (date.fromisoformat(market_date) - date.fromisoformat(source_effective)).days
    except ValueError:
        source_age = -1
    if (
        not root_effective
        or source_effective < root_effective
        or source_effective > market_date
        or maximum_age <= 0
        or source_age < 0
        or source_age > maximum_age
    ):
        raise RuntimeError(
            "SPY workbook is stale or future-dated for market date "
            f"{market_date}: source effective date {source_effective}"
        )
    expected_names = root.get("canonical_zip_member_names")
    if expected_names and list(attestation["member_names"]) != list(expected_names):
        raise RuntimeError("SPY workbook is not the governed holdings schema: structure mismatch")
    expected_static = root.get("canonical_static_member_hashes")
    if expected_static and attestation["static_member_hashes"] != dict(expected_static):
        raise RuntimeError("SPY workbook is not the governed holdings schema: member mismatch")
    expected_schema = str(root.get("canonical_schema_digest_sha256") or "").lower()
    if expected_schema and attestation["schema_digest_sha256"] != expected_schema:
        raise RuntimeError("SPY workbook is not the governed holdings schema: schema mismatch")
    expected_content = str(root.get("canonical_content_digest_sha256") or "").lower()
    if expected_content and attestation["content_digest_sha256"] != expected_content:
        raise RuntimeError("SPY workbook is not the governed holdings root: content mismatch")
    expected_symbols = str(root.get("canonical_symbol_set_hash_sha256") or "").lower()
    if expected_symbols and attestation["symbol_set_hash_sha256"] != expected_symbols:
        raise RuntimeError("SPY workbook is not the governed holdings root: member set mismatch")
    trusted_raw = root.get("raw_artifact_hashes")
    if trusted_raw and [hashlib.sha256(payload).hexdigest()] != list(trusted_raw):
        raise RuntimeError("SPY workbook is not the governed holdings root: raw digest mismatch")
    return symbols, source_effective, attestation


def _spy_manifest(
    *,
    artifact_path: Path,
    payload: bytes,
    observed_at: str,
    source_id: str,
    source_uri: str,
    parsed: tuple[list[str], str, dict[str, object]],
) -> dict[str, object]:
    symbols, source_effective, attestation = parsed
    root = _TRUSTED_SOURCE_ROOTS[source_id]
    records: list[dict[str, object]] = [
        {
            "ticker": symbol,
            "provider_symbol": symbol,
            "asset_class": "common_stock",
            "index_memberships": ["S&P 500"],
            "valid_from": source_effective,
        }
        for symbol in symbols
    ]
    canonical = [
        {
            "symbol": row["ticker"],
            "provider_symbol": row["provider_symbol"],
            "asset_class": row["asset_class"],
            "index": "S&P 500",
            "valid_from": source_effective,
            "valid_to": None,
        }
        for row in records
    ]
    member_hash = _canonical_member_hash(canonical)
    raw_hash = hashlib.sha256(payload).hexdigest()
    return {
        "source_id": source_id,
        "source_uri": source_uri,
        "source_scope": root["source_scope"],
        "observed_at": observed_at,
        "effective_date": source_effective,
        "reconstitution_id": root["reconstitution_id"],
        "index_name": "S&P 500",
        "expected_count": 503,
        "completeness_verdict": "COMPLETE",
        "members": records,
        "canonical_zip_member_names": attestation["member_names"],
        "canonical_static_member_hashes": attestation["static_member_hashes"],
        "canonical_schema_digest_sha256": attestation["schema_digest_sha256"],
        "canonical_content_digest_sha256": attestation["content_digest_sha256"],
        "canonical_symbol_set_hash_sha256": attestation["symbol_set_hash_sha256"],
        "canonical_member_set_hash_sha256": member_hash,
        "source_artifacts": [
            {
                "uri": source_uri,
                "path": str(artifact_path),
                "sha256": raw_hash,
                "byte_count": len(payload),
            }
        ],
        "reconstitution_lineage": {
            "schema_version": root["lineage_schema_version"],
            "builder_id": root["lineage_builder_id"],
            "transformation_id": root["lineage_transformation_id"],
            "reconstitution_id": root["reconstitution_id"],
            "effective_date": source_effective,
            "input_artifact_hashes": [raw_hash],
            "canonical_member_set_hash_sha256": member_hash,
        },
    }


def _canonical_json_sha256(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _open_posix_directory_mutation_watch(
    directory_fd: int,
    *,
    label: str,
    include_children: bool = False,
) -> int:
    """Watch an exact admitted directory for unowned namespace/content changes."""

    if not sys.platform.startswith("linux"):
        raise RuntimeError("POSIX core-universe refresh requires Linux directory mutation watches")
    descriptor_path = Path(f"/proc/self/fd/{directory_fd}")
    try:
        if not _same_file_identity(os.fstat(directory_fd), os.stat(descriptor_path)):
            raise RuntimeError(f"{label} descriptor alias has the wrong identity")
    except OSError as exc:
        raise RuntimeError(f"{label} descriptor alias is unavailable") from exc

    import ctypes

    libc = ctypes.CDLL(None, use_errno=True)
    try:
        inotify_init1 = libc.inotify_init1
        inotify_add_watch = libc.inotify_add_watch
    except AttributeError as exc:
        raise RuntimeError("Linux directory mutation watches are unavailable") from exc
    inotify_init1.argtypes = [ctypes.c_int]
    inotify_init1.restype = ctypes.c_int
    inotify_add_watch.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_uint32]
    inotify_add_watch.restype = ctypes.c_int
    watch_fd = inotify_init1(os.O_NONBLOCK | int(getattr(os, "O_CLOEXEC", 0)))
    if watch_fd < 0:
        error = ctypes.get_errno()
        raise RuntimeError("Linux directory mutation watch could not be opened") from OSError(
            error, os.strerror(error)
        )
    # IN_DELETE_SELF | IN_MOVE_SELF | IN_UNMOUNT.
    mutation_mask = 0x00000400 | 0x00000800 | 0x00002000
    if include_children:
        # IN_MODIFY | IN_ATTRIB | IN_CLOSE_WRITE | IN_MOVED_FROM |
        # IN_MOVED_TO | IN_CREATE | IN_DELETE. Watches for generation
        # namespaces are installed only after this process's writes complete.
        mutation_mask |= 0x00000002 | 0x00000004 | 0x00000008
        mutation_mask |= 0x00000040 | 0x00000080 | 0x00000100 | 0x00000200
    result = inotify_add_watch(watch_fd, os.fsencode(descriptor_path), mutation_mask)
    if result < 0:
        error = ctypes.get_errno()
        os.close(watch_fd)
        raise RuntimeError(f"Linux directory mutation watch could not bind {label}") from OSError(
            error, os.strerror(error)
        )
    return watch_fd


def _assert_no_posix_directory_mutation(
    watch_fd: int | None,
    *,
    label: str,
    include_children: bool = False,
) -> None:
    if watch_fd is None:
        return
    try:
        events = os.read(watch_fd, 4096)
    except BlockingIOError:
        return
    except OSError as exc:
        raise RuntimeError(f"{label} mutation watch became unavailable") from exc
    if events:
        if include_children:
            raise RuntimeError(f"{label} or its governed children changed while refresh was held")
        raise RuntimeError(f"{label} moved or was deleted while refresh was held")
    raise RuntimeError(f"{label} mutation watch closed unexpectedly")


@contextmanager
def _hold_posix_directory_mutation_watch(
    directory_fd: int,
    *,
    label: str,
    include_children: bool,
):
    watch_fd = _open_posix_directory_mutation_watch(
        directory_fd,
        label=label,
        include_children=include_children,
    )
    try:
        yield watch_fd
    finally:
        try:
            _assert_no_posix_directory_mutation(
                watch_fd,
                label=label,
                include_children=include_children,
            )
        finally:
            os.close(watch_fd)


@contextmanager
def _bind_refresh_config_directory(config_root: Path):
    """Yield an exact POSIX directory descriptor for lock namespace operations."""

    if os.name == "nt":
        yield None, None
        return
    before = os.lstat(config_root)
    flags = os.O_RDONLY | int(getattr(os, "O_DIRECTORY", 0)) | int(getattr(os, "O_NOFOLLOW", 0))
    try:
        descriptor = os.open(config_root, flags)
    except OSError as exc:
        raise RuntimeError("core-universe config root could not be identity-bound") from exc
    try:
        opened = os.fstat(descriptor)
        current = os.lstat(config_root)
    except OSError as exc:
        os.close(descriptor)
        raise RuntimeError("core-universe config root could not be identity-bound") from exc
    watch_fd: int | None = None
    try:
        if (
            _is_reparse_stat(current)
            or not stat.S_ISDIR(opened.st_mode)
            or not _same_file_identity(before, opened)
            or not _same_file_identity(opened, current)
        ):
            raise RuntimeError("core-universe config root changed during lock admission")
        watch_fd = _open_posix_directory_mutation_watch(
            descriptor,
            label="core-universe config root",
        )
        yield descriptor, watch_fd
    finally:
        try:
            _assert_no_posix_directory_mutation(
                watch_fd,
                label="core-universe config root",
            )
        finally:
            if watch_fd is not None:
                os.close(watch_fd)
            os.close(descriptor)


@contextmanager
def _refresh_lock(config_root: Path):
    """Serialize refreshes with identity-bound, recoverable owner metadata."""

    config_root = _assert_path_type(
        config_root,
        label="core-universe config root",
        expected="directory",
    )
    with _bind_refresh_config_directory(config_root) as config_binding:
        config_directory_fd, config_mutation_watch = config_binding
        lock_path = config_root / REFRESH_LOCK_NAME
        if config_directory_fd is None:
            _assert_path_type(
                lock_path,
                label="core-universe refresh lock",
                expected="file",
                allow_missing=True,
            )
        owner = _lock_owner_metadata()
        owner_bytes = (json.dumps(owner, sort_keys=True, separators=(",", ":")) + "\n").encode(
            "utf-8"
        )
        descriptor: int | None = None
        for _attempt in range(3):
            try:
                create_flags = (
                    os.O_CREAT | os.O_EXCL | os.O_WRONLY | int(getattr(os, "O_NOFOLLOW", 0))
                )
                if config_directory_fd is None:
                    descriptor = os.open(lock_path, create_flags, 0o600)
                else:
                    descriptor = os.open(
                        lock_path.name,
                        create_flags,
                        0o600,
                        dir_fd=config_directory_fd,
                    )
                break
            except FileExistsError as exc:
                if config_directory_fd is None:
                    _assert_path_type(
                        lock_path,
                        label="core-universe refresh lock",
                        expected="file",
                    )
                else:
                    existing = _lstat_from_directory(lock_path, config_directory_fd)
                    if _is_reparse_stat(existing) or not stat.S_ISREG(existing.st_mode):
                        raise RuntimeError(
                            f"core-universe refresh lock is not a regular file: {lock_path}"
                        ) from exc
                if not _archive_provably_dead_lock(lock_path):
                    raise RuntimeError("core universe refresh already in progress") from exc
            except OSError as exc:
                raise RuntimeError(f"core universe refresh lock unavailable: {exc}") from exc
        if descriptor is None:
            raise RuntimeError("core universe refresh lock could not be acquired")
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(owner_bytes)
            handle.flush()
            os.fsync(handle.fileno())
        if config_directory_fd is None:
            _assert_path_type(
                lock_path,
                label="core-universe refresh lock",
                expected="file",
            )
        with _open_refresh_lock_handle(
            lock_path,
            label="core-universe refresh lock",
            exact_namespace_mutation=True,
            directory_fd=config_directory_fd,
        ) as lock_handle:
            admitted = os.fstat(lock_handle.fileno())
            if lock_handle.read() != owner_bytes:
                raise RuntimeError("core universe refresh lock bytes changed during admission")
            lock_handle.seek(0)
            try:
                yield config_directory_fd, config_mutation_watch
            finally:
                final = os.fstat(lock_handle.fileno())
                lock_handle.seek(0)
                if (
                    not _same_file_snapshot(admitted, final)
                    or lock_handle.read() != owner_bytes
                    or not _same_file_snapshot(
                        final,
                        _lstat_from_directory(lock_path, config_directory_fd),
                    )
                ):
                    raise RuntimeError("core universe refresh lock changed while held")
                try:
                    _delete_refresh_lock_from_exact_handle(
                        lock_handle,
                        lock_path,
                        owner_bytes,
                        directory_fd=config_directory_fd,
                    )
                except OSError as exc:
                    # Leave the exact lock visible on cleanup failure so another
                    # refresh can never overlap this owner.
                    raise RuntimeError(f"could not remove refresh lock: {lock_path}") from exc


def _process_start_time(pid: int) -> str | None:
    """Return a PID-reuse-resistant process creation marker when available."""

    try:
        import psutil  # type: ignore[import-not-found]

        return f"{float(psutil.Process(pid).create_time()):.6f}"
    except (ImportError, OSError, ValueError):
        return None


def _lock_owner_metadata() -> dict[str, object]:
    pid = os.getpid()
    return {
        "schema_version": REFRESH_LOCK_SCHEMA_VERSION,
        "owner_token": uuid.uuid4().hex,
        "pid": pid,
        "process_start_time": _process_start_time(pid),
        "hostname": socket.gethostname(),
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }


def _lock_owner_is_dead(metadata: object) -> bool:
    if (
        not isinstance(metadata, dict)
        or metadata.get("schema_version") != REFRESH_LOCK_SCHEMA_VERSION
    ):
        return False
    raw_pid = metadata.get("pid")
    try:
        if isinstance(raw_pid, bool):
            return False
        pid = int(raw_pid)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    live = _process_is_live(pid)
    if live is False:
        return True
    if live is not True:
        # Unknown is not evidence of death.  Preserve the lock fail-closed.
        return False
    stored_start = metadata.get("process_start_time")
    current_start = _process_start_time(pid)
    # When a platform can provide creation time, a mismatch proves PID reuse.
    # If it cannot, an existing process is conservatively treated as live.
    return bool(stored_start and current_start and str(stored_start) != current_start)


def _process_is_live(pid: int) -> bool | None:
    """Probe liveness without using Windows ``os.kill(pid, 0)``.

    On Windows ``os.kill`` delegates to ``TerminateProcess`` for ordinary
    signals, including zero, so the POSIX liveness idiom can kill the refresh
    owner.  Return ``None`` whenever the platform cannot prove either state;
    callers must keep the lock in that case.
    """

    if pid == os.getpid():
        return True
    try:
        import psutil  # type: ignore[import-not-found]

        return bool(psutil.pid_exists(pid))
    except ImportError:
        pass
    if os.name == "nt":
        try:
            import ctypes

            process_query_limited_information = 0x1000
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            handle = kernel32.OpenProcess(process_query_limited_information, False, int(pid))
            if handle:
                kernel32.CloseHandle(handle)
                return True
            error = ctypes.get_last_error()
            if error == 87:  # ERROR_INVALID_PARAMETER: no such PID.
                return False
            if error == 5:  # ERROR_ACCESS_DENIED still proves a live process.
                return True
            return None
        except (AttributeError, OSError, ValueError):
            return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def _linux_renameat2(
    source: Path,
    destination: Path,
    flags: int,
    *,
    source_directory_fd: int | None = None,
    destination_directory_fd: int | None = None,
) -> bool:
    """Call Linux renameat2, returning False only when the primitive is absent."""

    if not sys.platform.startswith("linux"):
        return False
    import ctypes

    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    source_fd = -100 if source_directory_fd is None else source_directory_fd
    destination_fd = -100 if destination_directory_fd is None else destination_directory_fd
    source_name = source if source_directory_fd is None else Path(source.name)
    destination_name = destination if destination_directory_fd is None else Path(destination.name)
    result = renameat2(
        source_fd,
        os.fsencode(source_name),
        destination_fd,
        os.fsencode(destination_name),
        flags,
    )
    if result == 0:
        return True
    error = ctypes.get_errno()
    if error in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        return False
    raise OSError(error, os.strerror(error), os.fspath(destination))


def _rename_no_replace(source: Path, destination: Path) -> bool:
    """Atomically move ``source`` only while ``destination`` is absent."""

    if os.name == "nt":
        try:
            os.rename(source, destination)
        except FileExistsError:
            return False
        return True

    # Linux exposes the exact primitive needed here. Calling libc avoids a
    # check/rename sequence and therefore cannot overwrite a third contender.
    try:
        if _linux_renameat2(source, destination, 1):  # RENAME_NOREPLACE
            return True
    except FileExistsError:
        return False

    # Same-directory hard-link publication is also atomically no-clobber. The
    # brief two-link state is fail-closed to this module's nlink==1 admission.
    try:
        os.link(source, destination, follow_symlinks=False)
    except FileExistsError:
        return False
    source_identity = os.lstat(source)
    destination_identity = os.lstat(destination)
    if not _same_file_identity(source_identity, destination_identity):
        raise RuntimeError("no-replace lock restoration published the wrong identity")
    source.unlink()
    return True


def _windows_rename_from_handle(
    handle: BinaryIO,
    destination: Path,
    *,
    replace_if_exists: bool,
) -> None:
    """Atomically rename the exact already-open Windows file."""

    import ctypes
    import msvcrt
    from ctypes import wintypes

    class _FileRenameInfo(ctypes.Structure):
        _fields_ = [
            ("replace_if_exists", wintypes.BOOLEAN),
            ("root_directory", wintypes.HANDLE),
            ("file_name_length", wintypes.DWORD),
            ("file_name", wintypes.WCHAR * 1),
        ]

    encoded = os.fspath(destination).encode("utf-16-le")
    name_offset = _FileRenameInfo.file_name.offset
    buffer_size = name_offset + len(encoded) + 2
    buffer = ctypes.create_string_buffer(buffer_size)
    information = _FileRenameInfo.from_buffer(buffer)
    information.replace_if_exists = replace_if_exists
    information.root_directory = None
    information.file_name_length = len(encoded)
    ctypes.memmove(ctypes.addressof(buffer) + name_offset, encoded, len(encoded))
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    set_information = kernel32.SetFileInformationByHandle
    set_information.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    set_information.restype = wintypes.BOOL
    if not set_information(
        msvcrt.get_osfhandle(handle.fileno()),
        3,  # FileRenameInfo
        buffer,
        buffer_size,
    ):
        error = ctypes.get_last_error()
        if not replace_if_exists and error in {80, 183}:
            raise FileExistsError(error, os.strerror(error), os.fspath(destination))
        raise OSError(error, os.strerror(error), os.fspath(destination))


def _windows_replace_from_handle(handle: BinaryIO, destination: Path) -> None:
    """Atomically replace a Windows path with the exact already-open file."""

    _windows_rename_from_handle(handle, destination, replace_if_exists=True)


def _windows_rename_from_handle_no_replace(handle: BinaryIO, destination: Path) -> None:
    """Atomically publish an exact Windows handle without clobbering a path."""

    _windows_rename_from_handle(handle, destination, replace_if_exists=False)


def _renameat2_bound(
    source: Path,
    destination: Path,
    flags: int,
    source_directory_fd: int | None,
    destination_directory_fd: int | None,
) -> bool:
    if source_directory_fd is None and destination_directory_fd is None:
        return _linux_renameat2(source, destination, flags)
    if source_directory_fd is None or destination_directory_fd is None:
        raise RuntimeError("POSIX atomic replacement requires both directory descriptors")
    return _linux_renameat2(
        source,
        destination,
        flags,
        source_directory_fd=source_directory_fd,
        destination_directory_fd=destination_directory_fd,
    )


def _replace_from_exact_handle(
    handle: BinaryIO,
    source: Path,
    destination: Path,
    expected_bytes: bytes,
    *,
    source_directory_fd: int | None = None,
    destination_directory_fd: int | None = None,
) -> None:
    """Replace with the opened inode or restore prior POSIX truth on mismatch."""

    admitted = os.fstat(handle.fileno())
    current_source = _lstat_from_directory(source, source_directory_fd)
    if not _same_file_snapshot(admitted, current_source):
        raise RuntimeError("core-universe atomic temporary changed before replacement")
    if os.name == "nt":
        _windows_replace_from_handle(handle, destination)
    else:
        prior_destination = None
        try:
            prior_destination = _lstat_from_directory(destination, destination_directory_fd)
        except FileNotFoundError:
            pass
        destination_existed = prior_destination is not None
        if destination_existed:
            if not _renameat2_bound(
                source,
                destination,
                2,  # RENAME_EXCHANGE
                source_directory_fd,
                destination_directory_fd,
            ):
                raise RuntimeError("identity-bound POSIX exchange is unavailable")
        elif not _renameat2_bound(
            source,
            destination,
            1,  # RENAME_NOREPLACE
            source_directory_fd,
            destination_directory_fd,
        ):
            raise RuntimeError("identity-bound POSIX replacement is unavailable")
        installed = _lstat_from_directory(destination, destination_directory_fd)
        if not _same_file_after_namespace_move(admitted, installed):
            if destination_existed:
                if not _renameat2_bound(
                    source,
                    destination,
                    2,
                    source_directory_fd,
                    destination_directory_fd,
                ):
                    raise RuntimeError("atomic output rollback primitive is unavailable")
                restored = _lstat_from_directory(destination, destination_directory_fd)
                if prior_destination is None or not _same_file_after_namespace_move(
                    prior_destination, restored
                ):
                    raise RuntimeError("atomic output rollback did not restore prior truth")
            else:
                if source_directory_fd is None and destination_directory_fd is None:
                    if not _rename_no_replace(destination, source):
                        raise RuntimeError("atomic output rollback could not restore absence")
                    if os.path.lexists(destination):
                        raise RuntimeError("atomic output rollback did not restore absence")
                else:
                    if destination_directory_fd is None:
                        raise RuntimeError("atomic output rollback lost its directory descriptor")
                    if not _renameat2_bound(
                        destination,
                        source,
                        1,
                        destination_directory_fd,
                        source_directory_fd,
                    ):
                        raise RuntimeError("atomic output rollback could not restore absence")
                    if (
                        _optional_lstat_from_directory(destination, destination_directory_fd)
                        is not None
                    ):
                        raise RuntimeError("atomic output rollback did not restore absence")
            raise RuntimeError("core-universe atomic temporary was replaced during commit")
    installed = _lstat_from_directory(destination, destination_directory_fd)
    if not _same_file_after_namespace_move(admitted, installed):
        raise RuntimeError("core-universe atomic output has the wrong installed identity")
    handle.seek(0)
    if handle.read() != expected_bytes:
        raise RuntimeError("core-universe atomic output bytes changed during commit")


def _archive_provably_dead_lock(lock_path: Path) -> bool:
    # POSIX has no generally available compare-and-rename primitive that binds
    # the source pathname to this already-open inode. An advisory flock cannot
    # exclude a hostile non-cooperating rename, so automatic recovery is
    # deliberately unavailable there. Leaving the dead lock visible is the
    # fail-closed outcome; an operator can inspect and remove it explicitly.
    if os.name != "nt":
        return False

    stack = ExitStack()
    try:
        handle = stack.enter_context(
            _open_refresh_lock_handle(
                lock_path,
                label="core-universe refresh lock",
                exact_namespace_mutation=True,
            )
        )
    except OSError:
        stack.close()
        return False
    with stack:
        admitted = os.fstat(handle.fileno())
        raw = handle.read()
        try:
            metadata = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            return False
        if not _lock_owner_is_dead(metadata):
            return False
        current = os.lstat(lock_path)
        if not _same_file_snapshot(admitted, current):
            raise RuntimeError("core universe refresh lock changed before stale archival")
        archive = lock_path.with_name(
            f"{lock_path.name}.dead."
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}."
            f"{uuid.uuid4().hex[:8]}"
        )
        _assert_path_type(
            archive,
            label="core-universe dead-lock archive",
            expected="file",
            allow_missing=True,
        )
        try:
            _windows_rename_from_handle_no_replace(handle, archive)
        except (FileExistsError, FileNotFoundError):
            return False
        archived = os.lstat(archive)
        if not _same_file_snapshot(admitted, archived):
            raise RuntimeError("core universe refresh lock archive has the wrong identity")
        handle.seek(0)
        if handle.read() != raw:
            raise RuntimeError("core universe refresh lock bytes changed during stale archival")
        return True


def _replace_bytes_in_directory(path: Path, payload: bytes, directory_fd: int) -> None:
    """Atomically replace a direct POSIX child without re-resolving its parent path."""

    existing = _optional_lstat_from_directory(path, directory_fd)
    if existing is not None and (
        _is_reparse_stat(existing)
        or not stat.S_ISREG(existing.st_mode)
        or int(getattr(existing, "st_nlink", 1)) != 1
    ):
        raise RuntimeError(f"core-universe atomic output is not a regular file: {path}")
    temporary_path: Path | None = None
    descriptor: int | None = None
    create_flags = (
        os.O_CREAT
        | os.O_EXCL
        | os.O_RDWR
        | int(getattr(os, "O_NOFOLLOW", 0))
        | int(getattr(os, "O_CLOEXEC", 0))
    )
    for _attempt in range(16):
        candidate = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
        try:
            descriptor = os.open(candidate.name, create_flags, 0o600, dir_fd=directory_fd)
            temporary_path = candidate
            break
        except FileExistsError:
            continue
        except OSError as exc:
            raise RuntimeError(
                f"core-universe atomic temporary could not be created: {path}"
            ) from exc
    if descriptor is None or temporary_path is None:
        raise RuntimeError(f"core-universe atomic temporary name space is exhausted: {path}")
    descriptor_open = True
    try:
        exact_temporary = os.fdopen(descriptor, "w+b")
        descriptor_open = False
        with exact_temporary:
            exact_temporary.write(payload)
            exact_temporary.flush()
            os.fsync(exact_temporary.fileno())
            admitted = os.fstat(exact_temporary.fileno())
            current = _lstat_from_directory(temporary_path, directory_fd)
            if (
                _is_reparse_stat(current)
                or not stat.S_ISREG(admitted.st_mode)
                or not _same_file_snapshot(admitted, current)
                or int(getattr(admitted, "st_nlink", 1)) != 1
            ):
                raise RuntimeError("core-universe atomic temporary changed before replacement")
            exact_temporary.seek(0)
            if exact_temporary.read() != payload:
                raise RuntimeError(
                    "core-universe atomic temporary bytes changed before replacement"
                )
            exact_temporary.seek(0)
            _replace_from_exact_handle(
                exact_temporary,
                temporary_path,
                path,
                payload,
                source_directory_fd=directory_fd,
                destination_directory_fd=directory_fd,
            )
        installed = _lstat_from_directory(path, directory_fd)
        if _is_reparse_stat(installed) or not stat.S_ISREG(installed.st_mode):
            raise RuntimeError(f"core-universe atomic output has an invalid type: {path}")
    finally:
        if descriptor_open:
            os.close(descriptor)
        if temporary_path is not None:
            remaining = _optional_lstat_from_directory(temporary_path, directory_fd)
            if remaining is not None:
                if _is_reparse_stat(remaining) or not stat.S_ISREG(remaining.st_mode):
                    raise RuntimeError(
                        f"core-universe atomic temporary cleanup is unsafe: {temporary_path}"
                    )
                os.unlink(temporary_path.name, dir_fd=directory_fd)


def _replace_bytes(path: Path, payload: bytes, *, directory_fd: int | None = None) -> None:
    if directory_fd is not None:
        if os.name == "nt":
            raise RuntimeError("POSIX directory descriptor supplied for a Windows atomic output")
        _replace_bytes_in_directory(path, payload, directory_fd)
        return
    path = _assert_path_type(
        path,
        label="core-universe atomic output",
        expected="file",
        allow_missing=True,
    )
    parent = _assert_path_type(
        path.parent,
        label="core-universe atomic output parent",
        expected="directory",
    )
    with _hold_directory_write_boundary(
        parent,
        label="core-universe atomic output parent",
    ):
        path = _assert_path_type(
            path,
            label="core-universe atomic output",
            expected="file",
            allow_missing=True,
        )
        fd, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.",
            suffix=".tmp",
            dir=parent,
        )
        descriptor_open = True
        try:
            temporary_path = _assert_path_type(
                Path(temporary),
                label="core-universe atomic temporary",
                expected="file",
            )
            temporary_identity = os.fstat(fd)
            if not _same_file_identity(temporary_identity, os.lstat(temporary_path)):
                raise RuntimeError("core-universe atomic temporary identity changed")
            handle = os.fdopen(fd, "wb")
            descriptor_open = False
            with handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
                temporary_identity = os.fstat(handle.fileno())
            if not _same_file_identity(temporary_identity, os.lstat(temporary_path)):
                raise RuntimeError("core-universe atomic temporary identity changed")
            with _open_refresh_lock_handle(
                temporary_path,
                label="core-universe atomic temporary",
                exact_namespace_mutation=True,
            ) as exact_temporary:
                exact_temporary.seek(0)
                if exact_temporary.read() != payload:
                    raise RuntimeError(
                        "core-universe atomic temporary bytes changed before replacement"
                    )
                exact_temporary.seek(0)
                _assert_path_type(
                    path,
                    label="core-universe atomic output",
                    expected="file",
                    allow_missing=True,
                )
                _replace_from_exact_handle(
                    exact_temporary,
                    temporary_path,
                    path,
                    payload,
                )
            _assert_path_type(
                path,
                label="core-universe atomic output",
                expected="file",
            )
        finally:
            if descriptor_open:
                os.close(fd)
            temporary_path = Path(temporary)
            if os.path.lexists(temporary_path):
                _assert_path_type(
                    temporary_path,
                    label="core-universe atomic temporary cleanup",
                    expected="file",
                )
                temporary_path.unlink()


def _read_active_pointer(
    path: Path,
    *,
    directory_fd: int | None = None,
) -> dict[str, object] | None:
    raw: bytes | None
    if directory_fd is None:
        path = _assert_path_type(
            path,
            label="core-universe active pointer",
            expected="file",
            allow_missing=True,
        )
        if not os.path.lexists(path):
            return None
        raw = path.read_bytes()
    else:
        raw = _read_optional_file_from_directory(
            path,
            directory_fd,
            label="core-universe active pointer",
        )
        if raw is None:
            return None
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if isinstance(parsed, dict) and parsed.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION:
        return parsed
    return None


def _validated_active_pointer_target(
    path: Path,
    pointer: dict[str, object],
    *,
    config_root: Path,
) -> Path:
    target_text = pointer.get("manifest_path")
    if not isinstance(target_text, str) or not target_text.strip():
        raise RuntimeError("active pointer manifest path missing")
    relative = Path(target_text)
    if relative.is_absolute():
        raise RuntimeError("active pointer manifest path must be relative")
    lexical = _absolute_without_reparse_resolution(path.parent / relative)
    root = _absolute_without_reparse_resolution(config_root)
    try:
        common = Path(os.path.commonpath((os.fspath(root), os.fspath(lexical))))
    except ValueError as exc:
        raise RuntimeError("active pointer target escapes config root") from exc
    if os.path.normcase(os.fspath(common)) != os.path.normcase(os.fspath(root)):
        raise RuntimeError("active pointer target escapes config root")
    lexical = _assert_path_type(
        lexical,
        label="core-universe active target",
        expected="file",
    )
    try:
        service_target = _active_pointer_target(path, pointer)
    except ValueError as exc:
        raise RuntimeError(str(exc)) from exc
    if os.path.normcase(os.fspath(service_target)) != os.path.normcase(os.fspath(lexical)):
        raise RuntimeError("active pointer target changed through path resolution")
    return lexical


def _refresh_locked(
    *,
    state_root: Path,
    config_directory_fd: int | None,
    config_mutation_watch: int | None,
    proxy_manifest: Path | None,
    ndx_artifact: Path | None,
    spy_artifact: Path | None,
    market_date: str,
    bootstrap_state_street_proxy: bool,
) -> dict[str, object]:
    market_date = _normalise_market_date(market_date)
    if os.name != "nt" and config_directory_fd is None:
        raise RuntimeError("POSIX core-universe refresh requires an admitted config descriptor")
    state_root = _assert_universe_state_layout(state_root, require_config=True)
    config_root = _assert_path_type(
        state_root / "config",
        label="core-universe config root",
        expected="directory",
    )
    output_path = config_root / "luna_core_universe.json"
    if config_directory_fd is None:
        output_path = _assert_path_type(
            output_path,
            label="core-universe active pointer",
            expected="file",
            allow_missing=True,
        )
        output_exists = os.path.lexists(output_path)
        prior_output_bytes = None
    else:
        output_metadata = _optional_lstat_from_directory(output_path, config_directory_fd)
        output_exists = output_metadata is not None
        if output_metadata is not None and (
            _is_reparse_stat(output_metadata) or not stat.S_ISREG(output_metadata.st_mode)
        ):
            raise RuntimeError(f"core-universe active pointer is not a regular file: {output_path}")
        prior_output_bytes = None
    if bootstrap_state_street_proxy:
        if proxy_manifest is not None:
            raise RuntimeError("State Street bootstrap does not accept an explicit proxy manifest")
        if output_exists:
            raise RuntimeError("State Street bootstrap requires a completely absent active pointer")
    if config_directory_fd is None and output_exists:
        prior_output_bytes = output_path.read_bytes()
    elif config_directory_fd is not None and output_exists:
        prior_output_bytes = _read_optional_file_from_directory(
            output_path,
            config_directory_fd,
            label="core-universe active pointer",
        )
    if proxy_manifest is None and config_directory_fd is not None:
        source_path = _absolute_without_reparse_resolution(output_path)
    else:
        source_path = _assert_path_type(
            proxy_manifest or output_path,
            label="core-universe proxy manifest",
            expected="file",
            allow_missing=True,
        )
    proxy_bootstrapped = False
    if ndx_artifact is not None and market_date != RELEASE_ANCHOR_MARKET_DATE:
        raise RuntimeError(
            "later-date NDX refresh requires the authenticated dated source download; "
            "an explicit artifact has no authenticated date provenance"
        )
    source_bytes = (
        prior_output_bytes if proxy_manifest is None and config_directory_fd is not None else None
    )
    source_exists = (
        source_bytes is not None
        if proxy_manifest is None and config_directory_fd is not None
        else os.path.lexists(source_path)
    )
    if source_exists:
        if source_bytes is not None:
            try:
                parsed_source = json.loads(source_bytes.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise RuntimeError(f"proxy manifest is invalid: {source_path}") from exc
            if not isinstance(parsed_source, dict):
                raise RuntimeError(f"proxy manifest is invalid: {source_path}")
            if parsed_source.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION:
                if config_directory_fd is None:
                    raise RuntimeError("active pointer source is missing its config descriptor")
                with _open_bound_active_generation(
                    config_directory_fd,
                    parsed_source,
                    config_root=config_root,
                ) as (generation_fd, active_target):
                    _manifest_bytes, wrapper = _read_bound_active_manifest(
                        generation_fd,
                        active_target,
                        parsed_source,
                    )
                source_base = active_target.parent
            else:
                wrapper = parsed_source
                source_base = source_path.parent
        else:
            wrapper = _read_json(source_path)
            source_base = source_path.parent
            try:
                pointer = json.loads(source_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                pointer = None
            if (
                isinstance(pointer, dict)
                and pointer.get("schema_version") == ACTIVE_POINTER_SCHEMA_VERSION
            ):
                source_base = _validated_active_pointer_target(
                    source_path,
                    pointer,
                    config_root=config_root,
                ).parent
    elif bootstrap_state_street_proxy and proxy_manifest is None:
        spy_root = _TRUSTED_SOURCE_ROOTS.get(SPY_SOURCE_ID)
        spy_source_uri = str(spy_root.get("source_uri") or "") if spy_root else ""
        if not spy_root or spy_source_uri != STATE_STREET_SPY_HOLDINGS_URL:
            raise RuntimeError("State Street SPY bootstrap trust root is unavailable")
        wrapper = {
            "schema_version": "dawnstrike.luna.core_universe_manifest_wrapper.v1",
            "manifests": [
                {
                    "source_id": SPY_SOURCE_ID,
                    "source_uri": spy_source_uri,
                    "index_name": "S&P 500",
                }
            ],
        }
        source_base = config_root
        proxy_bootstrapped = True
    else:
        raise RuntimeError(f"proxy manifest missing: {source_path}")
    children = wrapper.get("manifests")
    if not isinstance(children, list):
        raise RuntimeError("proxy manifest must contain a manifests list")
    proxy_children = [
        _resolve_proxy_paths(child, source_base)
        for child in children
        if isinstance(child, dict)
        and str(child.get("index_name") or child.get("index") or "")
        .strip()
        .lower()
        .replace(" ", "")
        in {"s&p500", "sp500", "sandp500"}
    ]
    if len(proxy_children) != 1:
        raise RuntimeError("exactly one existing SPY tracker proxy manifest is required")

    ndx_root = _TRUSTED_SOURCE_ROOTS[NDX_SOURCE_ID]
    ndx_url = _source_url(ndx_root, market_date)
    if ndx_artifact is not None:
        ndx_artifact = _assert_path_type(
            ndx_artifact,
            label="core-universe NDX source artifact",
            expected="file",
        )
    payload = ndx_artifact.read_bytes() if ndx_artifact else _fetch(ndx_url)
    spy_source_id = str(proxy_children[0].get("source_id") or "").strip()
    if not spy_source_id:
        raise RuntimeError("SPY proxy source_id missing")
    spy_url = str(proxy_children[0].get("source_uri") or "").strip()
    if not spy_url:
        root = _TRUSTED_SOURCE_ROOTS.get(spy_source_id)
        spy_url = str(root.get("source_uri") or "") if root else ""
    if not spy_url:
        raise RuntimeError("SPY proxy source_uri missing")
    if spy_artifact is not None:
        spy_artifact = _assert_path_type(
            spy_artifact,
            label="core-universe SPY source artifact",
            expected="file",
        )
    spy_payload = (
        spy_artifact.read_bytes() if spy_artifact else _fetch(STATE_STREET_SPY_HOLDINGS_URL)
    )
    observed_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    ndx_symbols, ndx_attestation = _attest_ndx_payload(payload, market_date=market_date)
    spy_symbols, spy_effective, spy_attestation = _attest_spy_payload(
        spy_payload,
        source_id=spy_source_id,
        market_date=market_date,
    )
    stable_workbook_digest = str(ndx_attestation["content_digest_sha256"])
    proxy_manifest_hash = _canonical_json_sha256(
        {
            "source_id": spy_source_id,
            "source_uri": spy_url,
            "effective_date": spy_effective,
            "canonical_content_digest_sha256": spy_attestation["content_digest_sha256"],
            "canonical_symbol_set_hash_sha256": spy_attestation["symbol_set_hash_sha256"],
        }
    )
    generation_key = _canonical_json_sha256(
        {
            "market_date": market_date,
            "ndx_canonical_content_digest_sha256": stable_workbook_digest,
            "ndx_canonical_symbol_set_hash_sha256": ndx_attestation["symbol_set_hash_sha256"],
            "proxy_manifest_sha256": proxy_manifest_hash,
        }
    )
    generation_id = f"ndx-sod-{market_date}-{generation_key[:48]}"
    active_pointer = _read_active_pointer(
        output_path,
        directory_fd=config_directory_fd,
    )
    if active_pointer and (
        active_pointer.get("generation_id") == generation_id
        and active_pointer.get("generation_key") == generation_key
        and active_pointer.get("market_date") == market_date
        and active_pointer.get("ndx_canonical_content_digest_sha256") == stable_workbook_digest
        and active_pointer.get("proxy_manifest_sha256") == proxy_manifest_hash
    ):
        # Validate the byte-identical active pair before reusing it.  A
        # corrupted or forged pointer never becomes a READY retry merely
        # because its content-addressed metadata matches.
        if config_directory_fd is None:
            active_target = _validated_active_pointer_target(
                output_path,
                active_pointer,
                config_root=config_root,
            )
            installed_contract = build_core_universe_contract(
                output_path,
                observed_at=observed_at,
                market_date=market_date,
            )
            active_wrapper = _read_json(output_path)
        else:
            with ExitStack() as reuse_boundary:
                reuse_boundary.enter_context(
                    _hold_posix_directory_mutation_watch(
                        config_directory_fd,
                        label="core-universe active pointer directory",
                        include_children=True,
                    )
                )
                current_pointer_bytes = _read_optional_file_from_directory(
                    output_path,
                    config_directory_fd,
                    label="core-universe active pointer",
                )
                if prior_output_bytes is None or current_pointer_bytes != prior_output_bytes:
                    raise RuntimeError("active core generation pointer changed before validation")
                active_generation_fd, active_target = reuse_boundary.enter_context(
                    _open_bound_active_generation(
                        config_directory_fd,
                        active_pointer,
                        config_root=config_root,
                    )
                )
                manifest_bytes, _parsed_wrapper = _read_bound_active_manifest(
                    active_generation_fd,
                    active_target,
                    active_pointer,
                )
                installed_contract, active_wrapper, bound_artifacts = (
                    _build_contract_from_bound_generation(
                        manifest_bytes,
                        active_generation_fd,
                        active_target,
                        observed_at=observed_at,
                        market_date=market_date,
                    )
                )
                if installed_contract.get("status") != "READY":
                    raise RuntimeError(
                        "active core generation did not reach READY: "
                        + str(
                            installed_contract.get("reason") or installed_contract.get("blockers")
                        )
                    )
                active_ndx, active_ndx_members = _generation_index_child(
                    active_wrapper,
                    {"nasdaq-100", "nasdaq100", "ndx"},
                    label="NDX",
                )
                active_spy, active_spy_members = _generation_index_child(
                    active_wrapper,
                    {"s&p500", "sp500", "sandp500"},
                    label="SPY",
                )
                current_pointer_bytes = _read_optional_file_from_directory(
                    output_path,
                    config_directory_fd,
                    label="core-universe active pointer",
                )
                if prior_output_bytes is None or current_pointer_bytes != prior_output_bytes:
                    raise RuntimeError("active core generation pointer changed during validation")
                _assert_no_posix_directory_mutation(
                    config_mutation_watch,
                    label="core-universe config root",
                )
                return {
                    "status": "READY",
                    "manifest": str(output_path),
                    "ndx_artifact": str(active_target.parent / _ndx_artifact_name(market_date)),
                    "ndx_sha256": hashlib.sha256(
                        bound_artifacts[_ndx_artifact_name(market_date)]
                    ).hexdigest(),
                    "ndx_member_count": len(active_ndx_members),
                    "spy_artifact": str(active_target.parent / SPY_ARTIFACT_NAME),
                    "spy_sha256": hashlib.sha256(bound_artifacts[SPY_ARTIFACT_NAME]).hexdigest(),
                    "spy_member_count": len(active_spy_members),
                    "spy_effective_date": active_spy.get("effective_date"),
                    "observed_at": installed_contract.get("observed_at") or observed_at,
                    "generation_id": generation_id,
                    "generation_key": generation_key,
                    "market_date": market_date,
                    "reused": True,
                    "proxy_bootstrapped": False,
                }
        if installed_contract.get("status") != "READY":
            raise RuntimeError(
                "active core generation did not reach READY: "
                + str(installed_contract.get("reason") or installed_contract.get("blockers"))
            )
        active_ndx, active_ndx_members = _generation_index_child(
            active_wrapper,
            {"nasdaq-100", "nasdaq100", "ndx"},
            label="NDX",
        )
        active_spy, active_spy_members = _generation_index_child(
            active_wrapper,
            {"s&p500", "sp500", "sandp500"},
            label="SPY",
        )
        active_entries = active_ndx.get("source_artifacts")
        active_artifact = active_target.parent / _ndx_artifact_name(market_date)
        active_sha256 = ""
        if isinstance(active_entries, list) and active_entries:
            entry = active_entries[0]
            if isinstance(entry, dict):
                if isinstance(entry.get("path"), str):
                    active_artifact = Path(str(entry["path"]))
                    if not active_artifact.is_absolute():
                        active_artifact = _absolute_without_reparse_resolution(
                            active_target.parent / active_artifact
                        )
                active_sha256 = str(entry.get("sha256") or "").lower()
        active_artifact = _assert_path_type(
            active_artifact,
            label="active core-universe NDX artifact",
            expected="file",
        )
        if not active_sha256:
            active_sha256 = str(
                next(
                    (
                        item.get("raw_artifact_sha256")
                        for item in installed_contract.get("source_artifacts") or []
                        if isinstance(item, dict) and item.get("source_id") == NDX_SOURCE_ID
                    ),
                    "",
                )
            ).lower()
        active_spy_entries = active_spy.get("source_artifacts")
        active_spy_artifact = active_target.parent / SPY_ARTIFACT_NAME
        active_spy_sha256 = ""
        if isinstance(active_spy_entries, list) and active_spy_entries:
            spy_entry = active_spy_entries[0]
            if isinstance(spy_entry, dict):
                if isinstance(spy_entry.get("path"), str):
                    active_spy_artifact = Path(str(spy_entry["path"]))
                    if not active_spy_artifact.is_absolute():
                        active_spy_artifact = _absolute_without_reparse_resolution(
                            active_target.parent / active_spy_artifact
                        )
                active_spy_sha256 = str(spy_entry.get("sha256") or "").lower()
        active_spy_artifact = _assert_path_type(
            active_spy_artifact,
            label="active core-universe SPY artifact",
            expected="file",
        )
        if not active_spy_sha256:
            active_spy_sha256 = str(
                next(
                    (
                        item.get("raw_artifact_sha256")
                        for item in installed_contract.get("source_artifacts") or []
                        if isinstance(item, dict) and item.get("source_id") == spy_source_id
                    ),
                    "",
                )
            ).lower()
        _assert_no_posix_directory_mutation(
            config_mutation_watch,
            label="core-universe config root",
        )
        return {
            "status": "READY",
            "manifest": str(output_path),
            "ndx_artifact": str(active_artifact),
            "ndx_sha256": active_sha256,
            "ndx_member_count": len(active_ndx_members),
            "spy_artifact": str(active_spy_artifact),
            "spy_sha256": active_spy_sha256,
            "spy_member_count": len(active_spy_members),
            "spy_effective_date": active_spy.get("effective_date"),
            "observed_at": installed_contract.get("observed_at") or observed_at,
            "generation_id": generation_id,
            "generation_key": generation_key,
            "market_date": market_date,
            "reused": True,
            "proxy_bootstrapped": False,
        }
    generations_root = config_root / GENERATION_DIRECTORY
    generations_root_fd: int | None = None
    generation_directory_fd: int | None = None
    if config_directory_fd is None:
        generations_root = _ensure_regular_directory(
            generations_root,
            label="core-universe generation root",
        )
    else:
        generations_root_fd = _open_child_directory(
            config_directory_fd,
            generations_root,
            label="core-universe generation root",
            create=True,
        )
    generation_dir = generations_root / generation_id
    try:
        if generations_root_fd is None:
            generation_dir = _assert_path_type(
                generation_dir,
                label="core-universe generation directory",
                expected="directory",
                allow_missing=True,
            )
            generation_metadata = (
                os.lstat(generation_dir) if os.path.lexists(generation_dir) else None
            )
        else:
            generation_metadata = _optional_lstat_from_directory(
                generation_dir,
                generations_root_fd,
            )
            if generation_metadata is not None and (
                _is_reparse_stat(generation_metadata)
                or not stat.S_ISDIR(generation_metadata.st_mode)
            ):
                raise RuntimeError(
                    f"core-universe generation directory is not regular: {generation_dir}"
                )
        if generation_metadata is not None:
            if active_pointer is not None:
                if config_directory_fd is None:
                    active_generation_target = _validated_active_pointer_target(
                        output_path,
                        active_pointer,
                        config_root=config_root,
                    )
                    active_generation_matches = os.path.normcase(
                        os.fspath(active_generation_target.parent)
                    ) == os.path.normcase(os.fspath(generation_dir))
                else:
                    active_relative = _active_generation_relative_path(active_pointer)
                    active_generation_matches = active_relative.parent.name == generation_id
                if (
                    active_pointer.get("generation_id") == generation_id
                    or active_generation_matches
                ):
                    raise RuntimeError("refusing to replace an active core-universe generation")
            orphan = generations_root / (
                f"{generation_id}.orphan."
                f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}."
                f"{uuid.uuid4().hex[:8]}"
            )
            try:
                if generations_root_fd is None:
                    orphan = _assert_path_type(
                        orphan,
                        label="core-universe orphan generation",
                        expected="directory",
                        allow_missing=True,
                    )
                    with _hold_directory_write_boundary(
                        generations_root,
                        label="core-universe generation root",
                    ):
                        generation_dir = _assert_path_type(
                            generation_dir,
                            label="core-universe generation directory",
                            expected="directory",
                        )
                        os.replace(generation_dir, orphan)
                        _assert_path_type(
                            orphan,
                            label="core-universe orphan generation",
                            expected="directory",
                        )
                else:
                    if _optional_lstat_from_directory(orphan, generations_root_fd) is not None:
                        raise RuntimeError(
                            f"core-universe orphan generation already exists: {orphan}"
                        )
                    if not _linux_renameat2(
                        generation_dir,
                        orphan,
                        1,  # RENAME_NOREPLACE
                        source_directory_fd=generations_root_fd,
                        destination_directory_fd=generations_root_fd,
                    ):
                        raise RuntimeError(
                            "identity-bound POSIX generation archival is unavailable"
                        )
                    archived = _lstat_from_directory(orphan, generations_root_fd)
                    if not _same_file_after_namespace_move(generation_metadata, archived):
                        raise RuntimeError(
                            "archived core-universe generation has the wrong identity"
                        )
            except OSError as exc:
                raise RuntimeError(
                    f"could not preserve inactive core-universe generation: {generation_dir}"
                ) from exc
        if generations_root_fd is None:
            with _hold_regular_directory(
                generations_root,
                label="core-universe generation root",
            ):
                _assert_path_type(
                    generation_dir,
                    label="core-universe generation directory",
                    expected="directory",
                    allow_missing=True,
                )
                generation_dir.mkdir()
                generation_dir = _assert_path_type(
                    generation_dir,
                    label="core-universe generation directory",
                    expected="directory",
                )
        else:
            try:
                os.mkdir(generation_dir.name, 0o700, dir_fd=generations_root_fd)
            except OSError as exc:
                raise RuntimeError(
                    f"core-universe generation directory could not be created: {generation_dir}"
                ) from exc
            generation_directory_fd = _open_child_directory(
                generations_root_fd,
                generation_dir,
                label="core-universe generation directory",
                create=False,
            )
    finally:
        if generations_root_fd is not None and generation_directory_fd is None:
            os.close(generations_root_fd)
    final_artifact = generation_dir / _ndx_artifact_name(market_date)
    final_spy_artifact = generation_dir / SPY_ARTIFACT_NAME
    candidate_path = generation_dir / "luna_core_universe.json"
    pointer_swapped = False
    try:
        with ExitStack() as generation_boundary:
            if generation_directory_fd is None:
                generation_boundary.enter_context(
                    _hold_directory_write_boundary(
                        generation_dir,
                        label="core-universe generation directory",
                    )
                )
            else:
                if generations_root_fd is None:
                    raise RuntimeError("core generation root lost its directory descriptor")
                generation_boundary.callback(os.close, generations_root_fd)
                generation_boundary.callback(os.close, generation_directory_fd)
            # The generation is inactive until its pointer is swapped.  Both raw
            # bytes and manifest are written under that generation, then the exact
            # final paths are validated before activation.
            _replace_bytes(
                final_artifact,
                payload,
                directory_fd=generation_directory_fd,
            )
            _replace_bytes(
                final_spy_artifact,
                spy_payload,
                directory_fd=generation_directory_fd,
            )
            spy_child = _spy_manifest(
                artifact_path=final_spy_artifact,
                payload=spy_payload,
                observed_at=observed_at,
                source_id=spy_source_id,
                source_uri=spy_url,
                parsed=(spy_symbols, spy_effective, spy_attestation),
            )
            ndx_child = _ndx_manifest(
                artifact_path=final_artifact,
                payload=payload,
                observed_at=observed_at,
                market_date=market_date,
                parsed=(ndx_symbols, ndx_attestation),
            )
            candidate = {**wrapper, "manifests": [spy_child, ndx_child]}
            candidate_bytes = (json.dumps(candidate, indent=2, sort_keys=True) + "\n").encode(
                "utf-8"
            )
            _replace_bytes(
                candidate_path,
                candidate_bytes,
                directory_fd=generation_directory_fd,
            )
            if generation_directory_fd is None:
                contract = build_core_universe_contract(
                    candidate_path,
                    observed_at=observed_at,
                    market_date=market_date,
                )
            else:
                bound_candidate_bytes = _read_optional_file_from_directory(
                    candidate_path,
                    generation_directory_fd,
                    label="core-universe candidate manifest",
                )
                if bound_candidate_bytes != candidate_bytes:
                    raise RuntimeError("candidate core manifest bytes changed before validation")
                contract, _validated_wrapper, _validated_artifacts = (
                    _build_contract_from_bound_generation(
                        bound_candidate_bytes,
                        generation_directory_fd,
                        candidate_path,
                        observed_at=observed_at,
                        market_date=market_date,
                        expected_artifacts={
                            final_artifact.name: payload,
                            final_spy_artifact.name: spy_payload,
                        },
                    )
                )
            if contract.get("status") != "READY":
                raise RuntimeError(
                    "candidate core manifest did not reach READY: "
                    + str(contract.get("reason") or contract.get("blockers"))
                )
            if generation_directory_fd is not None:
                if generations_root_fd is None:
                    raise RuntimeError("core generation root lost its directory descriptor")
                generation_boundary.enter_context(
                    _hold_posix_directory_mutation_watch(
                        generations_root_fd,
                        label="core-universe generation root",
                        include_children=True,
                    )
                )
                generation_boundary.enter_context(
                    _hold_posix_directory_mutation_watch(
                        generation_directory_fd,
                        label="core-universe generation directory",
                        include_children=True,
                    )
                )
                named_generation_fd = _open_child_directory(
                    generations_root_fd,
                    generation_dir,
                    label="core-universe generation directory",
                    create=False,
                )
                try:
                    if not _same_file_identity(
                        os.fstat(generation_directory_fd),
                        os.fstat(named_generation_fd),
                    ):
                        raise RuntimeError(
                            "core-universe generation name changed before activation"
                        )
                finally:
                    os.close(named_generation_fd)
            pointer = {
                "schema_version": ACTIVE_POINTER_SCHEMA_VERSION,
                "generation_id": generation_id,
                "generation_key": generation_key,
                "market_date": market_date,
                "ndx_canonical_content_digest_sha256": stable_workbook_digest,
                "ndx_canonical_symbol_set_hash_sha256": ndx_attestation["symbol_set_hash_sha256"],
                "proxy_manifest_sha256": proxy_manifest_hash,
                "spy_canonical_content_digest_sha256": spy_attestation["content_digest_sha256"],
                "spy_canonical_symbol_set_hash_sha256": spy_attestation["symbol_set_hash_sha256"],
                "spy_effective_date": spy_effective,
                "observed_at": observed_at,
                "manifest_path": (
                    Path(GENERATION_DIRECTORY) / generation_id / "luna_core_universe.json"
                ).as_posix(),
                "manifest_sha256": hashlib.sha256(candidate_bytes).hexdigest(),
                "created_at": observed_at,
            }
            pointer_bytes = (json.dumps(pointer, indent=2, sort_keys=True) + "\n").encode("utf-8")
            # This is the single active-pair swap.  The previous pointer/manifest
            # is not touched until the complete generation is READY.
            _replace_bytes(
                output_path,
                pointer_bytes,
                directory_fd=config_directory_fd,
            )
            pointer_swapped = True
            if config_directory_fd is None:
                _validated_active_pointer_target(
                    output_path,
                    pointer,
                    config_root=config_root,
                )
                installed_contract = build_core_universe_contract(
                    output_path,
                    observed_at=observed_at,
                    market_date=market_date,
                )
            else:
                if generation_directory_fd is None or generations_root_fd is None:
                    raise RuntimeError("installed core generation lost its directory descriptor")
                generation_boundary.enter_context(
                    _hold_posix_directory_mutation_watch(
                        config_directory_fd,
                        label="core-universe active pointer directory",
                        include_children=True,
                    )
                )
                _active_generation_relative_path(pointer)
                installed_pointer_bytes = _read_optional_file_from_directory(
                    output_path,
                    config_directory_fd,
                    label="core-universe installed active pointer",
                )
                if installed_pointer_bytes != pointer_bytes:
                    raise RuntimeError("installed core pointer bytes changed during validation")
                named_generation_fd = _open_child_directory(
                    generations_root_fd,
                    generation_dir,
                    label="core-universe installed generation directory",
                    create=False,
                )
                try:
                    if not _same_file_identity(
                        os.fstat(generation_directory_fd),
                        os.fstat(named_generation_fd),
                    ):
                        raise RuntimeError(
                            "installed core generation name changed during validation"
                        )
                    installed_manifest_bytes = _read_optional_file_from_directory(
                        candidate_path,
                        named_generation_fd,
                        label="core-universe installed generation manifest",
                    )
                    if installed_manifest_bytes != candidate_bytes:
                        raise RuntimeError(
                            "installed core manifest bytes changed during validation"
                        )
                    installed_contract, _installed_wrapper, _installed_artifacts = (
                        _build_contract_from_bound_generation(
                            installed_manifest_bytes,
                            named_generation_fd,
                            candidate_path,
                            observed_at=observed_at,
                            market_date=market_date,
                            expected_artifacts={
                                final_artifact.name: payload,
                                final_spy_artifact.name: spy_payload,
                            },
                        )
                    )
                finally:
                    os.close(named_generation_fd)
            if installed_contract.get("status") != "READY":
                raise RuntimeError(
                    "installed core manifest did not reach READY: "
                    + str(installed_contract.get("reason") or installed_contract.get("blockers"))
                )
            _assert_no_posix_directory_mutation(
                config_mutation_watch,
                label="core-universe config root",
            )
            return {
                "status": "READY",
                "manifest": str(output_path),
                "ndx_artifact": str(final_artifact),
                "ndx_sha256": hashlib.sha256(payload).hexdigest(),
                "spy_artifact": str(final_spy_artifact),
                "spy_sha256": hashlib.sha256(spy_payload).hexdigest(),
                "spy_member_count": len(spy_symbols),
                "spy_effective_date": spy_effective,
                "ndx_member_count": len(ndx_symbols),
                "observed_at": observed_at,
                "generation_id": generation_id,
                "generation_key": generation_key,
                "market_date": market_date,
                "reused": False,
                "proxy_bootstrapped": proxy_bootstrapped,
            }
    except Exception:
        if pointer_swapped and prior_output_bytes is not None:
            _replace_bytes(
                output_path,
                prior_output_bytes,
                directory_fd=config_directory_fd,
            )
        elif pointer_swapped and prior_output_bytes is None:
            if config_directory_fd is None:
                if os.path.lexists(output_path):
                    _assert_path_type(
                        output_path,
                        label="core-universe failed active pointer",
                        expected="file",
                    )
                    output_path.unlink()
            else:
                failed_pointer = _optional_lstat_from_directory(
                    output_path,
                    config_directory_fd,
                )
                if failed_pointer is not None:
                    if _is_reparse_stat(failed_pointer) or not stat.S_ISREG(failed_pointer.st_mode):
                        raise RuntimeError("failed active pointer cleanup is unsafe") from None
                    os.unlink(output_path.name, dir_fd=config_directory_fd)
        # Preserve an inactive partial generation.  A later retry validates
        # and atomically archives it; recursive cleanup here would re-open a
        # descendant-reparse race while handling an already-failed refresh.
        raise


def refresh(
    *,
    state_root: Path,
    proxy_manifest: Path | None,
    ndx_artifact: Path | None,
    spy_artifact: Path | None = None,
    market_date: str | None = None,
    bootstrap_state_street_proxy: bool = False,
) -> dict[str, object]:
    market_date = _normalise_market_date(market_date)
    state_root = _assert_universe_state_layout(
        _absolute_without_reparse_resolution(state_root),
        require_config=False,
    )
    # Keep both ancestors open while the protected refresh runs.  On the
    # production Windows host, GENERIC_READ without FILE_SHARE_DELETE closes
    # the swap window between lexical admission and each elevated write.
    with _hold_regular_directory(state_root, label="core-universe state root"):
        config_root = _ensure_regular_directory(
            state_root / "config",
            label="core-universe config root",
        )
        with _hold_directory_write_boundary(config_root, label="core-universe config root"):
            _assert_universe_state_layout(state_root, require_config=True)
            with _refresh_lock(config_root) as config_binding:
                config_directory_fd, config_mutation_watch = config_binding
                _assert_universe_state_layout(state_root, require_config=True)
                return _refresh_locked(
                    state_root=state_root,
                    config_directory_fd=config_directory_fd,
                    config_mutation_watch=config_mutation_watch,
                    proxy_manifest=proxy_manifest,
                    ndx_artifact=ndx_artifact,
                    spy_artifact=spy_artifact,
                    market_date=market_date,
                    bootstrap_state_street_proxy=bootstrap_state_street_proxy,
                )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-root", default=r"C:\r\dawnstrike-state")
    parser.add_argument("--proxy-manifest", default=None)
    parser.add_argument("--ndx-artifact", default=None)
    parser.add_argument("--spy-artifact", default=None)
    parser.add_argument("--market-date", required=True)
    parser.add_argument(
        "--bootstrap-state-street-proxy",
        action="store_true",
        help=(
            "Bootstrap a missing core-universe pointer from the pinned official "
            "State Street SPY holdings source; never replaces an existing pointer"
        ),
    )
    args = parser.parse_args()
    try:
        result = refresh(
            state_root=Path(args.state_root),
            proxy_manifest=Path(args.proxy_manifest) if args.proxy_manifest else None,
            ndx_artifact=Path(args.ndx_artifact) if args.ndx_artifact else None,
            spy_artifact=Path(args.spy_artifact) if args.spy_artifact else None,
            market_date=args.market_date,
            bootstrap_state_street_proxy=args.bootstrap_state_street_proxy,
        )
    except (OSError, RuntimeError, ValueError) as exc:
        print(json.dumps({"status": "DATA_UNAVAILABLE", "error": str(exc)}, indent=2))
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
