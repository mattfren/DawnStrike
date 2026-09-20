"""Safe, byte-preserving editor and inspector for KEY=value env-style files.

Design goals (see SEC-01 packet):
  * set-key changes exactly one KEY=value line without altering the bytes of
    any other line (line terminators, encoding, BOM are all preserved).
  * check-key reports only allowlisted metadata about a key's state -- it
    never echoes a value, a matched line, a raw diff, or a connection string.
  * No secret value is ever placed in an exception message, on stdout, on
    stderr, or in any artifact this module writes. Errors name the key and a
    failure *class* only.

This module intentionally does not use any diff/compare utility that would
print file contents (e.g. `diff`, `Compare-Object`, `fc`) -- comparisons are
done by hashing lines, never by displaying them.
"""

from __future__ import annotations

import hashlib
import os
import re
import stat
import tempfile
from dataclasses import dataclass
from enum import Enum
from typing import Optional


_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Matches "KEY=value" allowing leading whitespace and optional "export ".
_LINE_KV_RE = re.compile(
    r"^(?P<prefix>[ \t]*(?:export[ \t]+)?)(?P<key>[A-Za-z_][A-Za-z0-9_]*)=(?P<value>.*)$"
)

_UTF8_BOM = "﻿"

_BOOLEAN_TRUE = {"1", "true", "yes", "on", "enabled"}
_BOOLEAN_FALSE = {"0", "false", "no", "off", "disabled"}


class SafeEnvEditError(Exception):
    """Base error. Message must never contain line/value content."""


class KeyNotFoundError(SafeEnvEditError):
    def __init__(self, key: str):
        super().__init__(f"key not found: {key!r} (failure class: NOT_FOUND)")
        self.key = key


class DuplicateKeyError(SafeEnvEditError):
    def __init__(self, key: str, count: int):
        super().__init__(
            f"key is ambiguous: {key!r} occurs {count} times "
            f"(failure class: DUPLICATE_KEY); refusing to guess"
        )
        self.key = key
        self.count = count


class ConcurrentModificationError(SafeEnvEditError):
    def __init__(self, path: str):
        super().__init__(
            f"file changed on disk since it was read: {path!r} "
            f"(failure class: CONCURRENT_MODIFICATION); aborting without writing"
        )
        self.path = path


class WriteFailedError(SafeEnvEditError):
    def __init__(self, key: str, cause: BaseException):
        super().__init__(
            f"write failed for key {key!r} (failure class: {type(cause).__name__}); "
            f"original file left intact"
        )
        self.key = key


class Classification(str, Enum):
    ENABLED = "ENABLED"
    DISABLED = "DISABLED"
    NON_BOOLEAN = "NON_BOOLEAN"
    EMPTY = "EMPTY"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class FileMeta:
    size_bytes: int
    line_count: int
    mtime_ns: int


@dataclass(frozen=True)
class KeyStatus:
    key: str
    present: bool
    classification: Optional[Classification]
    occurrence_count: int
    file: FileMeta

    def to_public_dict(self) -> dict:
        """Allowlisted metadata only -- safe to print/log."""
        return {
            "key": self.key,
            "present": self.present,
            "classification": self.classification.value if self.classification else None,
            "occurrence_count": self.occurrence_count,
            "file_size_bytes": self.file.size_bytes,
            "file_line_count": self.file.line_count,
            "file_mtime_ns": self.file.mtime_ns,
        }


def _split_lines_preserving_terminators(raw: bytes) -> list[bytes]:
    """Split raw bytes into lines, keeping each line's own terminator.

    Handles '\\n', '\\r\\n', and a final line with no terminator at all.
    Never decodes to str, so this is agnostic to encoding beyond ASCII
    structural bytes ('\\n' == 0x0A, '\\r' == 0x0D), which is safe for
    UTF-8 (incl. BOM) and Latin-1 supersets.
    """
    lines: list[bytes] = []
    start = 0
    n = len(raw)
    i = 0
    while i < n:
        if raw[i] == 0x0A:  # \n
            lines.append(raw[start : i + 1])
            start = i + 1
        i += 1
    if start < n:
        lines.append(raw[start:n])
    return lines


def _decode_line_for_matching(line: bytes) -> str:
    """Best-effort decode of a single line for KEY= regex matching only.

    Uses utf-8 with surrogateescape so arbitrary bytes still round-trip if
    ever needed, but we never re-encode a *decoded value* -- only byte
    slices of the original line are used when constructing output.
    """
    return line.decode("utf-8", errors="surrogateescape")


def _file_hash(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _read_raw(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def _validate_key(key: str) -> None:
    if not _KEY_RE.match(key):
        raise SafeEnvEditError(
            "invalid key name (failure class: INVALID_KEY_FORMAT)"
        )


def _strip_bom(text: str) -> str:
    return text[1:] if text.startswith(_UTF8_BOM) else text


def _find_occurrences(lines: list[bytes], key: str) -> list[int]:
    indices = []
    for idx, line in enumerate(lines):
        text = _decode_line_for_matching(line)
        if idx == 0:
            text = _strip_bom(text)
        # Strip only the terminator for matching, keep everything else.
        stripped = text[:-2] if text.endswith("\r\n") else (
            text[:-1] if text.endswith("\n") else text
        )
        m = _LINE_KV_RE.match(stripped)
        if m and m.group("key") == key:
            indices.append(idx)
    return indices


def _classify_value(value: str) -> Classification:
    v = value.strip()
    if v == "":
        return Classification.EMPTY
    lv = v.lower()
    if lv in _BOOLEAN_TRUE:
        return Classification.ENABLED
    if lv in _BOOLEAN_FALSE:
        return Classification.DISABLED
    return Classification.NON_BOOLEAN


def _file_meta(path: str, raw: bytes, lines: list[bytes]) -> FileMeta:
    st = os.stat(path)
    return FileMeta(size_bytes=len(raw), line_count=len(lines), mtime_ns=st.st_mtime_ns)


def check_key(path: str, key: str) -> KeyStatus:
    """Report a key's state without revealing its value. Read-only."""
    _validate_key(key)
    raw = _read_raw(path)
    lines = _split_lines_preserving_terminators(raw)
    meta = _file_meta(path, raw, lines)
    occ = _find_occurrences(lines, key)

    if not occ:
        return KeyStatus(
            key=key, present=False, classification=None, occurrence_count=0, file=meta
        )

    if len(occ) > 1:
        return KeyStatus(
            key=key,
            present=True,
            classification=Classification.AMBIGUOUS,
            occurrence_count=len(occ),
            file=meta,
        )

    idx = occ[0]
    text = _decode_line_for_matching(lines[idx])
    if idx == 0:
        text = _strip_bom(text)
    stripped = text[:-2] if text.endswith("\r\n") else (
        text[:-1] if text.endswith("\n") else text
    )
    m = _LINE_KV_RE.match(stripped)
    assert m is not None
    classification = _classify_value(m.group("value"))
    return KeyStatus(
        key=key, present=True, classification=classification, occurrence_count=1, file=meta
    )


def _copy_permissions(src_path: str, dst_path: str) -> None:
    st = os.stat(src_path)
    os.chmod(dst_path, stat.S_IMODE(st.st_mode))
    if hasattr(os, "chown"):
        try:
            os.chown(dst_path, st.st_uid, st.st_gid)
        except (PermissionError, AttributeError, OSError):
            pass
    if os.name == "nt":
        _copy_windows_acl(src_path, dst_path)


def _copy_windows_acl(src_path: str, dst_path: str) -> None:
    """Best-effort ACL copy on Windows via pywin32 if available; no-op otherwise."""
    try:
        import win32security  # type: ignore

        sd = win32security.GetFileSecurity(
            src_path, win32security.DACL_SECURITY_INFORMATION
        )
        win32security.SetFileSecurity(
            dst_path, win32security.DACL_SECURITY_INFORMATION, sd
        )
    except ImportError:
        pass
    except Exception:
        # Never let ACL copy failure leak details; best-effort only.
        pass


def set_key(
    path: str,
    key: str,
    new_value: str,
    *,
    expected_hash: Optional[str] = None,
    _fail_before_write: bool = False,
    _writer=None,
) -> KeyStatus:
    """Change exactly one KEY=value line, byte-preserving everything else.

    Raises KeyNotFoundError / DuplicateKeyError / ConcurrentModificationError
    / WriteFailedError as appropriate. On any failure the original file is
    left untouched.

    `expected_hash`: if given, must match the sha256 of the file's current
    bytes or ConcurrentModificationError is raised (protects against a
    read-then-write race).
    `_writer`: injectable for testing write failures; defaults to a real
    atomic write. Must accept (tmp_path, data: bytes).
    """
    _validate_key(key)

    raw = _read_raw(path)
    if expected_hash is not None and _file_hash(raw) != expected_hash:
        raise ConcurrentModificationError(path)

    lines = _split_lines_preserving_terminators(raw)
    occ = _find_occurrences(lines, key)

    if not occ:
        raise KeyNotFoundError(key)
    if len(occ) > 1:
        raise DuplicateKeyError(key, len(occ))

    idx = occ[0]
    original_line = lines[idx]
    text = _decode_line_for_matching(original_line)
    bom = ""
    if idx == 0 and text.startswith(_UTF8_BOM):
        bom = _UTF8_BOM
        text = text[1:]
    if text.endswith("\r\n"):
        terminator = "\r\n"
        stripped = text[:-2]
    elif text.endswith("\n"):
        terminator = "\n"
        stripped = text[:-1]
    else:
        terminator = ""
        stripped = text

    m = _LINE_KV_RE.match(stripped)
    assert m is not None
    new_text = f"{bom}{m.group('prefix')}{key}={new_value}{terminator}"
    new_line = new_text.encode("utf-8", errors="surrogateescape")

    new_lines = list(lines)
    new_lines[idx] = new_line
    new_raw = b"".join(new_lines)

    dirpath = os.path.dirname(os.path.abspath(path)) or "."
    fd, tmp_path = tempfile.mkstemp(dir=dirpath, prefix=".safe_env_edit_", suffix=".tmp")
    fd_open = True
    try:
        if _fail_before_write:
            os.close(fd)
            fd_open = False
            raise RuntimeError("simulated write failure")
        if _writer is not None:
            os.close(fd)
            fd_open = False
            _writer(tmp_path, new_raw)
        else:
            with os.fdopen(fd, "wb") as f:
                fd_open = False  # fdopen now owns the fd; context manager closes it
                f.write(new_raw)
                f.flush()
                os.fsync(f.fileno())
        _copy_permissions(path, tmp_path)
        os.replace(tmp_path, path)
    except BaseException as exc:
        if fd_open:
            try:
                os.close(fd)
            except OSError:
                pass
        # Ensure temp file never lingers and never leak content in error.
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass
        raise WriteFailedError(key, exc) from None

    return check_key(path, key)


def _main(argv: Optional[list[str]] = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(description="Safe byte-preserving env file editor")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_set = sub.add_parser("set-key")
    p_set.add_argument("path")
    p_set.add_argument("key")
    p_set.add_argument("value")

    p_check = sub.add_parser("check-key")
    p_check.add_argument("path")
    p_check.add_argument("key")

    args = parser.parse_args(argv)

    if args.cmd == "check-key":
        status = check_key(args.path, args.key)
        for k, v in status.to_public_dict().items():
            print(f"{k}={v}")
        return 0

    if args.cmd == "set-key":
        status = set_key(args.path, args.key, args.value)
        print(f"key={status.key}")
        print(f"present={status.present}")
        print(f"classification={status.classification.value if status.classification else None}")
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(_main())
