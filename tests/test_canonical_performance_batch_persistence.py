from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.performance.contracts import (
    Cohort,
    PerformanceRow,
    RecordStatus,
    safe_float,
)
from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _row(record_id: str, *, ticker: str = "NOVA") -> PerformanceRow:
    return PerformanceRow(
        record_id=record_id,
        market_date="2026-07-29",
        ticker=ticker,
        cohort=Cohort.ALPHAOPS_SIGNAL_RESEARCH,
        strategy_id="alphaops_v4",
        strategy_version="dawnstrike-alphaops-v4",
        signal_id=record_id,
        rank=1,
        record_status=RecordStatus.MISSING_OUTCOME,
        entry_price=None,
        exit_price=None,
        quantity=None,
        notional_cents=None,
        gross_pnl_cents=None,
        gross_return_pct=None,
        fees_cents=None,
        slippage_cents=None,
        net_pnl_cents=None,
        return_pct=None,
        benchmark_return_pct=None,
        excess_return_pct=None,
        source_refs=(f"source:{record_id}",),
        source_hash_sha256=f"hash:{record_id}",
        input_hash_sha256="input-hash",
        observed_at=None,
        reconciled_at="2026-07-29T21:00:00+00:00",
    )


def _canonical_table_snapshot(connection: sqlite3.Connection) -> tuple[tuple[object, ...], ...]:
    tables = (
        "portfolio_performance_rows",
        "portfolio_daily_performance",
        "performance_reconciliation_issues",
    )
    return tuple(
        tuple(
            tuple(row)
            for row in connection.execute(
                f"SELECT * FROM {table} ORDER BY rowid"
            ).fetchall()
        )
        for table in tables
    )


def test_canonical_batch_rolls_back_on_mid_batch_conflicting_identity(tmp_path: Path) -> None:
    db_path = tmp_path / "rollback.sqlite"
    SQLiteScanStore(db_path).initialize()
    service = CanonicalPerformanceService(db_path)
    with sqlite3.connect(db_path) as connection:
        run_migrations(connection)
        service._persist(connection, [_row("existing")], [], [], market_date=None)
        connection.commit()

        with pytest.raises(sqlite3.IntegrityError):
            service._persist(
                connection,
                [_row("new"), _row("new", ticker="CONFLICT")],
                [],
                [],
                market_date=None,
            )
        connection.commit()
        rows = connection.execute(
            "SELECT record_id, ticker FROM portfolio_performance_rows ORDER BY record_id"
        ).fetchall()

    assert rows == [("existing", "NOVA")]


def test_canonical_batch_retry_is_idempotent_and_preserves_nulls(tmp_path: Path) -> None:
    db_path = tmp_path / "retry.sqlite"
    SQLiteScanStore(db_path).initialize()
    service = CanonicalPerformanceService(db_path)
    row = _row("missing")
    with sqlite3.connect(db_path) as connection:
        run_migrations(connection)
        service._persist(connection, [row], [], [], market_date=None)
        first = _canonical_table_snapshot(connection)
        service._persist(connection, [row], [], [], market_date=None)
        second = _canonical_table_snapshot(connection)
        stored = connection.execute(
            "SELECT entry_price, exit_price, return_pct FROM portfolio_performance_rows"
        ).fetchone()

    assert first == second
    assert stored == (None, None, None)
    assert safe_float(float("nan")) is None
    assert safe_float(float("inf")) is None
    assert safe_float(float("-inf")) is None


def test_full_reconcile_hash_and_canonical_rows_are_stable_after_batching(tmp_path: Path) -> None:
    db_path = tmp_path / "parity.sqlite"
    SQLiteScanStore(db_path).initialize()
    service = CanonicalPerformanceService(db_path)
    first = service.reconcile(now="2026-07-29T21:00:00+00:00")
    with sqlite3.connect(db_path) as connection:
        first_snapshot = _canonical_table_snapshot(connection)
        first_comparison = connection.execute(
            "SELECT comparison_id, status, input_hash_sha256, payload_json "
            "FROM account_performance_comparisons ORDER BY rowid"
        ).fetchall()

    second = service.reconcile(now="2026-07-29T21:00:00+00:00")
    with sqlite3.connect(db_path) as connection:
        second_snapshot = _canonical_table_snapshot(connection)
        second_comparison = connection.execute(
            "SELECT comparison_id, status, input_hash_sha256, payload_json "
            "FROM account_performance_comparisons ORDER BY rowid"
        ).fetchall()

    assert first["input_hash_sha256"] == second["input_hash_sha256"]
    assert first["output_hash_sha256"] == second["output_hash_sha256"]
    assert first["rows"] == second["rows"]
    assert first["daily"] == second["daily"]
    assert first_snapshot == second_snapshot
    assert first_comparison == second_comparison
