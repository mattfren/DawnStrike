"""Fail-closed protection for the operator's active SQLite state during tests."""

from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath

from intraday_scanner.errors import StorageError

ACTIVE_DATABASE = Path(r"C:\r\dawnstrike-state\shadow_real.sqlite")
_GUARD_ENV = "DAWNSTRIKE_TEST_ACTIVE_PATH_GUARD"
_OPERATOR_OPT_IN_ENV = "DAWNSTRIKE_OPERATOR_ACTIVE_TEST"


def assert_test_database_isolated(db_path: str | Path) -> None:
    """Reject the active database before SQLite can open it in a test process."""

    if os.environ.get(_GUARD_ENV) != "1" or os.environ.get(_OPERATOR_OPT_IN_ENV) == "1":
        return
    text = str(db_path)
    if text == ":memory:":
        return
    if is_active_database_path(text):
        raise StorageError(
            "Pytest active-state guard blocked C:\\r\\dawnstrike-state\\shadow_real.sqlite; "
            "only an explicitly marked operator test may inspect active state."
        )


def is_active_database_path(db_path: str | Path) -> bool:
    """Match the Windows operator path even when validation runs on Linux."""

    text = _sqlite_filename(db_path)
    windows_path = PureWindowsPath(text.replace("/", "\\"))
    active_windows_path = PureWindowsPath(str(ACTIVE_DATABASE))
    if windows_path.is_absolute() and (
        str(windows_path).casefold() == str(active_windows_path).casefold()
    ):
        return True
    try:
        resolved = Path(text).resolve(strict=False)
        active_resolved = ACTIVE_DATABASE.resolve(strict=False)
    except OSError:
        return False
    return os.path.normcase(str(resolved)) == os.path.normcase(str(active_resolved))


def is_explicit_absolute_database_path(db_path: str | Path) -> bool:
    """Recognize native and drive-qualified Windows absolute database paths."""

    text = _sqlite_filename(db_path)
    return Path(text).is_absolute() or PureWindowsPath(text.replace("/", "\\")).is_absolute()


def _sqlite_filename(db_path: str | Path) -> str:
    text = str(db_path)
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0]
        if text.startswith("/") and len(text) > 2 and text[2] == ":":
            text = text[1:]
    return text


__all__ = [
    "ACTIVE_DATABASE",
    "assert_test_database_isolated",
    "is_active_database_path",
    "is_explicit_absolute_database_path",
]
