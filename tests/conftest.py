"""Repository-wide pytest isolation from Dawnstrike's active SQLite state."""

from __future__ import annotations

import os
import sqlite3
from collections.abc import Iterator
from typing import Any

import pytest

from intraday_scanner.storage.test_isolation import assert_test_database_isolated


def pytest_sessionstart(session: pytest.Session) -> None:
    """Enable the central guard before test-module collection/import begins."""

    os.environ["DAWNSTRIKE_TEST_ACTIVE_PATH_GUARD"] = "1"


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line(
        "markers",
        "operator_active_state: explicit read-only operator inspection of active state",
    )


@pytest.fixture(autouse=True)
def _block_active_sqlite(
    request: pytest.FixtureRequest,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[None]:
    """Block every in-process SQLite open before active state can be touched."""

    monkeypatch.setenv("DAWNSTRIKE_TEST_ACTIVE_PATH_GUARD", "1")
    if request.node.get_closest_marker("operator_active_state") is not None:
        monkeypatch.setenv("DAWNSTRIKE_OPERATOR_ACTIVE_TEST", "1")
        yield
        return
    monkeypatch.delenv("DAWNSTRIKE_OPERATOR_ACTIVE_TEST", raising=False)
    original_connect = sqlite3.connect

    def guarded_connect(database: Any, *args: Any, **kwargs: Any) -> sqlite3.Connection:
        assert_test_database_isolated(database)
        return original_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield
