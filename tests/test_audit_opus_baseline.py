from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.audit_opus_baseline import collect_inventory, write_inventory


def _create_minimal_snapshot(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL);
            INSERT INTO schema_version VALUES (21, '2026-08-09T00:00:00Z');
            CREATE TABLE historical_signals (
                signal_id TEXT NOT NULL,
                market_date TEXT NOT NULL,
                generated_at TEXT NOT NULL,
                ticker TEXT NOT NULL,
                entry_watch_level REAL,
                invalidation_level REAL,
                target_1 REAL,
                raw_payload_json TEXT NOT NULL
            );
            INSERT INTO historical_signals VALUES
                ('s1', '2026-08-01', '2026-08-01T13:00:00Z', 'AAA', 10, 9, 11, '{}');
            CREATE TABLE signal_selections (
                strategy_id TEXT, strategy_version TEXT, cohort TEXT,
                decision TEXT, selected_at TEXT
            );
            INSERT INTO signal_selections VALUES
                ('alphaops_v5', 'v5', 'official_telegram', 'no_trade', '2026-08-01T13:00:00Z');
            """
        )


def test_inventory_is_deterministic_and_does_not_mutate_snapshot(tmp_path: Path) -> None:
    snapshot = tmp_path / "snapshot.sqlite"
    _create_minimal_snapshot(snapshot)
    before = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    first = collect_inventory(snapshot)
    second = collect_inventory(snapshot)
    assert first == second
    assert first["integrity_check"] == "ok"
    assert first["query_only"] == 1
    assert first["schema_version"] == 21
    assert first["table_counts"]["historical_signals"] == 1
    assert hashlib.sha256(snapshot.read_bytes()).hexdigest() == before

    output = tmp_path / "receipt.json"
    write_inventory(snapshot, output)
    assert json.loads(output.read_text(encoding="utf-8")) == first


def test_inventory_refuses_invalid_sqlite(tmp_path: Path) -> None:
    snapshot = tmp_path / "invalid.sqlite"
    snapshot.write_bytes(b"not a sqlite database")
    with pytest.raises(RuntimeError, match="read-only|integrity_check"):
        collect_inventory(snapshot)
