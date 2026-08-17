"""Fail-closed protection for the operator's active SQLite state during tests."""

from __future__ import annotations

import os
from pathlib import Path

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
    if text.startswith("file:"):
        text = text[5:].split("?", 1)[0].replace("/", "\\")
        if text.startswith("\\") and len(text) > 2 and text[2] == ":":
            text = text[1:]
    try:
        resolved = Path(text).resolve(strict=False)
    except OSError as exc:
        raise StorageError(f"Could not resolve guarded SQLite path: {db_path}") from exc
    if os.path.normcase(str(resolved)) == os.path.normcase(str(ACTIVE_DATABASE)):
        raise StorageError(
            "Pytest active-state guard blocked C:\\r\\dawnstrike-state\\shadow_real.sqlite; "
            "only an explicitly marked operator test may inspect active state."
        )


__all__ = ["ACTIVE_DATABASE", "assert_test_database_isolated"]
