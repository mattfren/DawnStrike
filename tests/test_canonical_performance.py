from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.performance.snapshot import write_public_snapshot
from intraday_scanner.risk.policy import RiskInput, evaluate_risk
from intraday_scanner.services.daily_finalize_service import DailyFinalizeService
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _raw_db(path: Path) -> None:
    SQLiteScanStore(path).initialize()


def test_missing_outcome_is_missing_not_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "truth.sqlite"
    _raw_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO historical_signals
            (signal_id, scan_id, generated_at, market_date, ticker, signal_label,
             risk_flags_json, avoid_reasons_json, raw_payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "signal-1",
                "scan-1",
                "2026-07-29T13:00:00+00:00",
                "2026-07-29",
                "NOVA",
                "WATCH",
                "[]",
                "[]",
                json.dumps({"source_url": "https://example.test/signal-1"}),
            ),
        )

    result = CanonicalPerformanceService(db_path).reconcile(now="2026-07-29T21:00:00+00:00")
    row = next(item for item in result["rows"] if item["record_id"] == "research_signal:signal-1")

    assert result["status"] == "PARTIAL"
    assert row["record_status"] == "missing_outcome"
    assert row["return_pct"] is None
    assert row["net_pnl_cents"] is None
    assert result["daily"][0]["missing_outcome_count"] == 1


def test_closed_position_reconciles_costs_in_cents(tmp_path: Path) -> None:
    db_path = tmp_path / "paper.sqlite"
    _raw_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            CREATE TABLE paper_positions (
                position_id TEXT PRIMARY KEY,
                signal_id TEXT,
                market_date TEXT NOT NULL,
                ticker TEXT NOT NULL,
                status TEXT NOT NULL,
                quantity REAL NOT NULL,
                entry_price REAL,
                exit_price REAL,
                notional REAL,
                realized_pnl REAL,
                updated_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO paper_positions
            (position_id, signal_id, market_date, ticker, status, quantity,
             entry_price, exit_price, notional, realized_pnl, updated_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
            """,
            (
                "position-1",
                "signal-1",
                "2026-07-29",
                "NOVA",
                "CLOSED",
                100.0,
                10.0,
                11.0,
                1000.0,
                "2026-07-29T20:00:00+00:00",
                json.dumps(
                    {
                        "cohort": "official_forward_paper",
                        "fees": 1.0,
                        "slippage_cost": 2.0,
                        "source_url": "https://example.test/position-1",
                    }
                ),
            ),
        )
        run_migrations(connection)
        connection.execute(
            """
            INSERT INTO portfolio_equity_observations
            (observation_id, market_date, cohort, strategy_id, strategy_version,
             opening_equity_cents, ending_equity_cents, source_refs_json,
             source_hash_sha256, observed_at, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "equity-2026-07-29",
                "2026-07-29",
                "official_forward_paper",
                "alphaops_v4",
                "dawnstrike-alphaops-v4",
                100_000,
                109_700,
                '["https://example.test/equity"]',
                "equity-source-hash",
                "2026-07-29T21:00:00+00:00",
                "{}",
            ),
        )

    result = CanonicalPerformanceService(db_path).reconcile(now="2026-07-29T21:00:00+00:00")
    row = result["rows"][0]
    daily = result["daily"][0]

    assert row["record_status"] == "realized"
    assert row["gross_pnl_cents"] == 10000
    assert row["fees_cents"] == 100
    assert row["slippage_cents"] == 200
    assert row["net_pnl_cents"] == 9700
    assert row["return_pct"] == 9.7
    assert daily["return_basis"] == "net_after_costs"
    assert daily["net_pnl_cents"] == 9700
    assert daily["return_pct"] == 9.7
    assert daily["opening_equity_cents"] == 100_000
    assert daily["ending_equity_cents"] == 109_700


def test_reconciliation_and_snapshot_are_idempotent_and_bounded(tmp_path: Path) -> None:
    db_path = tmp_path / "repeat.sqlite"
    _raw_db(db_path)
    service = CanonicalPerformanceService(db_path)
    first = service.reconcile(now="2026-07-29T21:00:00+00:00")
    second = service.reconcile(now="2026-07-29T21:00:00+00:00")
    output = write_public_snapshot(db_path, tmp_path / "public" / "performance.json")

    assert first["rows"] == second["rows"]
    assert first["daily"] == second["daily"]
    assert output["manifest"]["status"] == "no_data"
    assert output["manifest"]["compressed_byte_count"] <= 250 * 1024
    assert output["manifest"]["compression"] == "gzip"
    assert (tmp_path / "public" / "performance.json.manifest.json").exists()


def test_reconciliation_reuses_timestamp_for_unchanged_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "stable.sqlite"
    _raw_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO historical_signals
            (signal_id, scan_id, generated_at, market_date, ticker, signal_label,
             risk_flags_json, avoid_reasons_json, raw_payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "stable-signal",
                "stable-scan",
                "2026-07-29T13:00:00+00:00",
                "2026-07-29",
                "NOVA",
                "WATCH",
                "[]",
                "[]",
                json.dumps({"source_url": "https://example.test/stable-signal"}),
            ),
        )
    service = CanonicalPerformanceService(db_path)

    first = service.reconcile(now="2026-07-29T21:00:00+00:00")
    second = service.reconcile()
    first_public = service.load_public_data()
    second_public = service.load_public_data()
    first_public["generated_at"] = None
    second_public["generated_at"] = None

    assert second["input_hash_sha256"] == first["input_hash_sha256"]
    assert second["calculated_at"] == first["calculated_at"]
    assert second["rows"] == first["rows"]
    assert second["daily"] == first["daily"]
    assert second_public == first_public


def test_full_reconcile_clears_stale_canonical_rows(tmp_path: Path) -> None:
    db_path = tmp_path / "stale.sqlite"
    _raw_db(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """
            INSERT INTO historical_signals
            (signal_id, scan_id, generated_at, market_date, ticker, signal_label,
             risk_flags_json, avoid_reasons_json, raw_payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "signal-stale",
                "scan-stale",
                "2026-07-29T13:00:00+00:00",
                "2026-07-29",
                "NOVA",
                "WATCH",
                "[]",
                "[]",
                "{}",
            ),
        )

    service = CanonicalPerformanceService(db_path)
    service.reconcile(now="2026-07-29T21:00:00+00:00")
    with sqlite3.connect(db_path) as connection:
        connection.execute("DELETE FROM historical_signals")
    result = service.reconcile(now="2026-07-29T21:05:00+00:00")

    assert result["row_count"] == 0
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM portfolio_performance_rows").fetchone()[0] == 0
        )


def test_daily_finalize_publishes_explicit_no_trade_and_is_idempotent(tmp_path: Path) -> None:
    result_root = tmp_path / "public"
    service = DailyFinalizeService(tmp_path / "empty.sqlite", result_root)
    first = service.run(market_date="2026-07-29", now="2026-07-29T21:00:00+00:00")
    second = service.run(market_date="2026-07-29", now="2026-07-29T21:00:00+00:00")

    assert first["status"] == "NO_DATA"
    assert first["readiness"]["status"] == "not_ready"
    assert first["readiness"]["http_status"] == 503
    assert second["run_id"] == first["run_id"]
    assert (result_root / "readiness.json").exists()
    assert (result_root / "stage-manifest.json").exists()


def test_daily_finalize_retains_prior_canonical_days(tmp_path: Path) -> None:
    db_path = tmp_path / "history.sqlite"
    _raw_db(db_path)
    with sqlite3.connect(db_path) as connection:
        for signal_id, market_date in (("signal-old", "2026-07-28"), ("signal-new", "2026-07-29")):
            connection.execute(
                """
                INSERT INTO historical_signals
                (signal_id, scan_id, generated_at, market_date, ticker, signal_label,
                 risk_flags_json, avoid_reasons_json, raw_payload_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    signal_id,
                    f"scan-{signal_id}",
                    f"{market_date}T13:00:00+00:00",
                    market_date,
                    "NOVA",
                    "WATCH",
                    "[]",
                    "[]",
                    json.dumps({"source_url": f"https://example.test/{signal_id}"}),
                ),
            )

    result = DailyFinalizeService(db_path, tmp_path / "public").run(
        market_date="2026-07-29",
        now="2026-07-29T21:00:00+00:00",
    )

    assert result["reconciliation"]["daily_count"] == 2
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM portfolio_daily_performance").fetchone()[0]
            == 2
        )


def test_daily_finalize_records_retries_and_never_greens_missing_data(
    tmp_path: Path, monkeypatch
) -> None:
    attempts = 0

    def flaky_reconcile(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            raise RuntimeError("upstream temporarily unavailable")
        return {
            "status": "NO_DATA",
            "input_hash_sha256": "input-hash",
            "row_count": 0,
            "daily_count": 0,
            "issue_count": 0,
        }

    monkeypatch.setattr(CanonicalPerformanceService, "reconcile", flaky_reconcile)
    db_path = tmp_path / "retry.sqlite"
    result = DailyFinalizeService(db_path, tmp_path / "public").run(
        market_date="2026-07-29",
        retry_limit=2,
    )

    assert attempts == 3
    assert result["retry_count"] == 2
    assert result["readiness"]["status"] == "not_ready"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT retry_count FROM daily_finalize_runs").fetchone()[0] == 2


def test_risk_policy_fails_closed_for_unknown_safety_inputs() -> None:
    decision = evaluate_risk(
        RiskInput(
            ticker="NOVA",
            decision_time="2026-07-29T14:00:00+00:00",
            equity_cents=100_000,
            entry_price=10.0,
            stop_price=9.9,
            proposed_notional_cents=10_000,
            daily_realized_loss_cents=0,
            ticker_notional_cents=0,
            correlated_position_count=0,
            halt_status=None,
            corporate_action_status="clear",
            sec_risk_status="clear",
            source_quality_status="verified",
            spread_bps=25.0,
        )
    )

    assert decision.allowed_for_paper is False
    assert "halt_status_unknown" in decision.reasons
    assert decision.action == "SHADOW_BLOCK"
