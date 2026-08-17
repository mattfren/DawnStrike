"""Regression proof that pytest fails before opening operator active state."""

from __future__ import annotations

import sqlite3

import pytest

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.storage.test_isolation import ACTIVE_DATABASE


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

