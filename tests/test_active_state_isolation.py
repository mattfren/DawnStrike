"""Regression proof that pytest fails before opening operator active state."""

from __future__ import annotations

import sqlite3

import pytest

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.storage.test_isolation import (
    ACTIVE_DATABASE,
    is_active_database_path,
    is_explicit_absolute_database_path,
)


@pytest.mark.parametrize(
    "path",
    (
        ACTIVE_DATABASE,
        str(ACTIVE_DATABASE),
        "file:C:/r/dawnstrike-state/shadow_real.sqlite?mode=ro",
    ),
)
def test_pytest_guard_rejects_active_path_before_sqlite_open(path: object) -> None:
    with pytest.raises(StorageError, match="active-state guard blocked"):
        sqlite3.connect(path)  # type: ignore[arg-type]


def test_store_observer_and_scenario_surfaces_reject_active_path() -> None:
    with pytest.raises(StorageError, match="active-state guard blocked"):
        SQLiteScanStore(ACTIVE_DATABASE, read_only=True)


def test_read_only_build_surface_rejects_active_path() -> None:
    with pytest.raises(StorageError, match="active-state guard blocked"):
        connect_read_only(ACTIVE_DATABASE)


def test_active_path_identity_is_portable_across_ci_operating_systems() -> None:
    assert is_active_database_path(r"C:\r\dawnstrike-state\shadow_real.sqlite") is True
    assert is_active_database_path("C:/r/dawnstrike-state/shadow_real.sqlite") is True
    assert is_active_database_path("file:C:/r/dawnstrike-state/shadow_real.sqlite?mode=ro") is True
    assert is_explicit_absolute_database_path(ACTIVE_DATABASE) is True
    assert is_active_database_path("data/shadow_real.sqlite") is False

