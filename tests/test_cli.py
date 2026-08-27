import json
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.cli import main
from intraday_scanner.models import SnapshotRow
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


@pytest.fixture(autouse=True)
def _daily_learning_hmac_key(monkeypatch: pytest.MonkeyPatch) -> None:
    # Production imports this persistent key from runtime.env. Keep CLI tests
    # deterministic without ever placing a key in an output tree.
    monkeypatch.setenv("DAWNSTRIKE_DAILY_LEARNING_HMAC_KEY", "test-learning-key-" + "x" * 32)


def test_cli_init_db_creates_sqlite(tmp_path):
    db_path = tmp_path / "scanner.sqlite"
    assert main(["init-db", "--db-path", str(db_path)]) == 0
    assert db_path.exists()


def test_cli_strategy_learning_daily_writes_research_only_receipts(tmp_path, capsys):
    out_dir = tmp_path / "strategy-learning"
    status = main(
        [
            "strategy-learning-daily",
            "--market-date",
            "2026-08-20",
            "--cutoff",
            "2026-08-20T22:00:00+00:00",
            "--source-identity",
            "fixture-source:2026-08-20",
            "--code-sha",
            "fixture-code-sha",
            "--out-dir",
            str(out_dir),
        ]
    )
    assert status == 1
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "incomplete"
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    assert receipt["research_only"] is True
    assert receipt["automatic_promotion"] is False
    assert receipt["broker_execution_enabled"] is False


def test_cli_strategy_learning_daily_attributes_database_read_only(tmp_path, capsys):
    database_path = tmp_path / "performance.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, cohort TEXT, strategy_id TEXT, "
            "strategy_version TEXT, record_status TEXT, return_pct REAL, "
            "benchmark_return_pct REAL, open_position_count INTEGER, "
            "reconciled_at TEXT, payload_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "benchmark",
                    "2026-08-20",
                    "shadow_challenger",
                    "benchmark_buy_hold_equal_weight",
                    "v1.0",
                    "realized",
                    1.0,
                    None,
                    0,
                    "2026-08-20T21:00:00+00:00",
                    '{"record_type":"portfolio_observation","close_time":"2026-08-20T15:00:00+00:00"}',
                ),
                (
                    "loss",
                    "2026-08-20",
                    "shadow_challenger",
                    "ts_momentum_sma_atr",
                    "v1.0",
                    "realized",
                    -0.5,
                    None,
                    0,
                    "2026-08-20T21:00:00+00:00",
                    '{"record_type":"portfolio_observation","close_time":"2026-08-20T15:00:00+00:00"}',
                ),
            ],
        )
    out_dir = tmp_path / "strategy-learning-db"

    status = main(
        [
            "strategy-learning-daily",
            "--market-date",
            "2026-08-20",
            "--cutoff",
            "2026-08-20T22:00:00+00:00",
            "--source-identity",
            "fixture-db:2026-08-20",
            "--code-sha",
            "fixture-code-sha",
            "--out-dir",
            str(out_dir),
            "--db-path",
            str(database_path),
        ]
    )

    assert status == 1
    result = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    evidence = next(
        item["evidence"]
        for item in receipt["strategy_evidence"]
        if item["strategy_id"] == "ts_momentum_sma_atr"
    )
    assert evidence["counts"]["outcomes_retained"] == 0
    assert {row["record_id"] for row in evidence["misses"]} == {"loss"}
    assert receipt["automatic_policy_change"] is False


def test_cli_strategy_learning_honors_exact_timestamp_cutoff_for_same_day_close(
    tmp_path, capsys
):
    database_path = tmp_path / "timestamp-performance.sqlite"
    payload_before = {
        "trade_lifecycles": [
            {
                "trade_id": "before-cutoff",
                "status": "closed",
                "close_time": "2026-08-20T14:00:00+00:00",
                "return_pct": -1.0,
            },
        ],
        "record_type": "portfolio_observation",
    }
    payload_after = {
        "trade_lifecycles": [
            {
                "trade_id": "after-cutoff",
                "status": "closed",
                "close_time": "2026-08-20T15:00:00+00:00",
                "return_pct": 2.0,
            }
        ],
        "record_type": "portfolio_observation",
    }
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, cohort TEXT, strategy_id TEXT, "
            "strategy_version TEXT, record_status TEXT, return_pct REAL, "
                "benchmark_return_pct REAL, open_position_count INTEGER, "
            "reconciled_at TEXT, payload_json TEXT)"
        )
        connection.executemany(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    "same-day-before",
                    "2026-08-20",
                    "shadow_challenger",
                    "ts_momentum_sma_atr",
                    "v1.0",
                    "realized",
                    None,
                    None,
                    0,
                    "2026-08-20T14:20:00+00:00",
                    json.dumps(payload_before),
                ),
                (
                    "same-day-after",
                    "2026-08-20",
                    "shadow_challenger",
                    "ts_momentum_sma_atr",
                    "v1.0",
                    "realized",
                    None,
                    None,
                    0,
                    "2026-08-20T14:20:00+00:00",
                    json.dumps(payload_after),
                ),
            ],
        )
    out_dir = tmp_path / "strategy-learning-timestamp"

    status = main(
        [
            "strategy-learning-daily",
            "--market-date",
            "2026-08-20",
            "--cutoff",
            "2026-08-20T14:30:00+00:00",
            "--source-identity",
            "fixture-timestamp:2026-08-20",
            "--code-sha",
            "fixture-code-sha",
            "--out-dir",
            str(out_dir),
            "--db-path",
            str(database_path),
        ]
    )

    assert status == 1
    result = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    evidence = next(
        item["evidence"]
        for item in receipt["strategy_evidence"]
        if item["strategy_id"] == "ts_momentum_sma_atr"
    )
    # Lifecycle returns remain provisional without a committed FillTruth join;
    # the before-cutoff lifecycle is still retained as a miss, while the late
    # close is excluded from this point-in-time run.
    assert evidence["outcomes"] == []
    assert {row["record_id"] for row in evidence["misses"]} == {"before-cutoff"}
    assert all(row["record_id"] != "after-cutoff" for row in evidence["outcomes"])


def test_cli_strategy_learning_evidence_file_quarantines_unordered_terminal_rows(
    tmp_path, capsys
):
    evidence_path = tmp_path / "evidence.json"
    evidence_path.write_text(
        json.dumps(
            {
                "ts_momentum_sma_atr": {
                    "outcomes": [
                        {
                            "record_id": "before",
                            "status": "RESOLVED",
                            "market_date": "2026-08-20",
                            "terminal_event_at": "2026-08-20T14:00:00+00:00",
                            "return_pct": 1.0,
                        },
                        {
                            "record_id": "after",
                            "status": "RESOLVED",
                            "market_date": "2026-08-20",
                            "terminal_event_at": "2026-08-20T15:00:00+00:00",
                            "return_pct": 2.0,
                        },
                        {
                            "record_id": "missing-time",
                            "status": "RESOLVED",
                            "market_date": "2026-08-20",
                            "return_pct": 3.0,
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    status = main(
        [
            "strategy-learning-daily",
            "--market-date",
            "2026-08-20",
            "--cutoff",
            "2026-08-20T14:30:00+00:00",
            "--source-identity",
            "fixture-evidence-file:2026-08-20",
            "--code-sha",
            "fixture-code-sha",
            "--out-dir",
            str(tmp_path / "learning"),
            "--evidence-file",
            str(evidence_path),
        ]
    )
    assert status == 1
    result = json.loads(capsys.readouterr().out)
    receipt = json.loads(Path(result["receipt_path"]).read_text(encoding="utf-8"))
    evidence = next(
        item["evidence"]
        for item in receipt["strategy_evidence"]
        if item["strategy_id"] == "ts_momentum_sma_atr"
    )
    assert [row["record_id"] for row in evidence["outcomes"]] == ["before"]
    assert evidence["counts"]["future_evidence_excluded"] == 1
    assert evidence["counts"]["terminal_timestamp_quarantined"] == 1


def test_cli_strategy_learning_reuses_frozen_evidence_when_database_grows(tmp_path, capsys):
    database_path = tmp_path / "mutable-performance.sqlite"
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "CREATE TABLE portfolio_performance_rows ("
            "record_id TEXT, market_date TEXT, cohort TEXT, strategy_id TEXT, "
            "strategy_version TEXT, record_status TEXT, return_pct REAL, "
            "benchmark_return_pct REAL, open_position_count INTEGER, "
            "reconciled_at TEXT, payload_json TEXT)"
        )
        connection.execute(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "first",
                "2026-08-20",
                "shadow_challenger",
                "ts_momentum_sma_atr",
                "v1.0",
                "realized",
                -0.5,
                None,
                0,
                "2026-08-20T21:00:00+00:00",
                "{}",
            ),
        )
    out_dir = tmp_path / "strategy-learning-input-binding"
    arguments = [
        "strategy-learning-daily",
        "--market-date",
        "2026-08-20",
        "--cutoff",
        "2026-08-20T22:00:00+00:00",
        "--source-identity",
        "same-source-label",
        "--code-sha",
        "fixture-code-sha",
        "--out-dir",
        str(out_dir),
        "--db-path",
        str(database_path),
    ]
    assert main(arguments) == 1
    capsys.readouterr()
    with sqlite3.connect(database_path) as connection:
        connection.execute(
            "INSERT INTO portfolio_performance_rows VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "second",
                "2026-08-20",
                "shadow_challenger",
                "ts_momentum_sma_atr",
                "v1.0",
                "realized",
                0.75,
                None,
                0,
                "2026-08-20T21:00:00+00:00",
                "{}",
            ),
        )
    assert main(arguments) == 1
    reused = json.loads(capsys.readouterr().out)
    assert reused["idempotent_reused"] is True
    snapshot = json.loads(
        (out_dir / "2026-08-20" / "daily_learning_evidence_snapshot.json").read_text(
            encoding="utf-8"
        )
    )
    assert snapshot["input_hash_sha256"] == reused["input_hash_sha256"]
    assert snapshot["component_hashes"]["portfolio_performance_rows"]


def test_cli_live_scan_without_keys_fails_gracefully(monkeypatch, capsys):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)

    status = main(["live-scan", "--symbols", "NOVA"])

    captured = capsys.readouterr()
    assert status == 1
    assert "Missing Alpaca market-data credential" in captured.err
    assert "ALPACA_API_SECRET_KEY" in captured.err


def test_cli_live_scan_missing_keys_records_provider_health(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("ALPACA_API_KEY_ID", raising=False)
    monkeypatch.delenv("ALPACA_API_SECRET_KEY", raising=False)
    db_path = tmp_path / "scanner.sqlite"

    status = main(
        ["live-scan", "--symbols", "NOVA", "--db-path", str(db_path), "--persist"]
    )

    captured = capsys.readouterr()
    health = SQLiteScanStore(db_path).load_provider_health()
    assert status == 1
    assert "Missing Alpaca market-data credential" in captured.err
    assert health[0]["provider"] == "alpaca"
    assert health[0]["status"] == "error"


def test_cli_notify_dry_run_uses_persisted_scan(tmp_path, capsys):
    db_path = tmp_path / "scanner.sqlite"
    out_dir = tmp_path / "scan"
    assert (
        main(
            [
                "scan",
                "--snapshot",
                "sample_data/premarket_snapshot_sample.csv",
                "--out-dir",
                str(out_dir),
                "--db-path",
                str(db_path),
                "--persist",
            ]
        )
        == 0
    )

    status = main(["notify", "--db-path", str(db_path), "--dry-run"])

    captured = capsys.readouterr()
    assert status == 0
    assert "[dry-run:console]" in captured.out


def test_cli_monitor_setups_uses_persisted_scan(tmp_path, capsys):
    db_path = tmp_path / "scanner.sqlite"
    scan_out = tmp_path / "scan"
    monitor_out = tmp_path / "monitor"
    assert (
        main(
            [
                "scan",
                "--snapshot",
                "sample_data/premarket_snapshot_sample.csv",
                "--out-dir",
                str(scan_out),
                "--db-path",
                str(db_path),
                "--persist",
            ]
        )
        == 0
    )

    status = main(
        [
            "monitor-setups",
            "--snapshot",
            "sample_data/premarket_snapshot_sample.csv",
            "--db-path",
            str(db_path),
            "--out-dir",
            str(monitor_out),
            "--persist",
        ]
    )

    captured = capsys.readouterr()
    assert status == 0
    assert "monitor:" in captured.out
    assert (monitor_out / "setup_monitor_checks.csv").exists()


def test_cli_monitor_open_can_use_provider_backed_snapshots(monkeypatch, tmp_path, capsys):
    class FakeAlpacaProvider:
        def __init__(self, config):
            self.config = config

        def validate_credentials(self):
            return None

        def get_premarket_snapshot(self, symbols, config):
            return [
                SnapshotRow(
                    ticker=symbol,
                    company=symbol,
                    premarket_price=5.60,
                    previous_close=2.75,
                    premarket_high=5.80,
                    premarket_low=4.90,
                    premarket_volume=2_000_000,
                    float_shares=18_000_000,
                    market_cap=100_000_000,
                    spread_pct=1.0,
                    short_float_pct=12.0,
                    has_news=True,
                    current_halt=False,
                    recent_offering=False,
                    reverse_split_90d=False,
                    source="fake_alpaca",
                    as_of_timestamp="2026-06-18T09:35:00-04:00",
                    dollar_volume=11_200_000,
                    gap_pct=103.64,
                    catalyst_headline="fixture",
                )
                for symbol in symbols
            ]

    monkeypatch.setattr("intraday_scanner.cli.AlpacaProvider", FakeAlpacaProvider)
    db_path = tmp_path / "scanner.sqlite"
    scan_out = tmp_path / "scan"
    monitor_out = tmp_path / "monitor"

    assert (
        main(
            [
                "morning-run",
                "--snapshot",
                "sample_data/premarket_snapshot_sample.csv",
                "--out-dir",
                str(scan_out),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )

    status = main(
        [
            "monitor-open",
            "--provider",
            "alpaca",
            "--db-path",
            str(db_path),
            "--out-dir",
            str(monitor_out),
            "--persist",
            "--max-iterations",
            "1",
        ]
    )

    captured = capsys.readouterr()
    health = SQLiteScanStore(db_path).load_provider_health()
    assert status == 0
    assert "monitor:" in captured.out
    assert health[0]["provider"] == "alpaca"
    assert "loaded live monitor snapshot" in health[0]["detail"]


def test_cli_production_workflow_aliases_use_sample_mode(tmp_path, capsys):
    db_path = tmp_path / "scanner.sqlite"
    scan_out = tmp_path / "scan"
    monitor_out = tmp_path / "monitor"
    audit_out = tmp_path / "audit"

    assert (
        main(
            [
                "morning-run",
                "--snapshot",
                "sample_data/premarket_snapshot_sample.csv",
                "--out-dir",
                str(scan_out),
                "--db-path",
                str(db_path),
            ]
        )
        == 0
    )
    assert db_path.exists()
    assert (scan_out / "ranked_candidates.csv").exists()

    assert (
        main(
            [
                "monitor-open",
                "--snapshot",
                "sample_data/premarket_snapshot_sample.csv",
                "--db-path",
                str(db_path),
                "--out-dir",
                str(monitor_out),
                "--persist",
                "--max-iterations",
                "1",
            ]
        )
        == 0
    )
    assert (monitor_out / "setup_monitor_checks.csv").exists()

    assert (
        main(
            [
                "audit-latest",
                "--db-path",
                str(db_path),
                "--minute-bars",
                "sample_data/minute_bars/2026-06-18.csv",
                "--out-dir",
                str(audit_out),
                "--persist",
            ]
        )
        == 0
    )
    assert (audit_out / "paper_audit_summary.json").exists()

    assert main(["performance-report", "--db-path", str(db_path), "--persist"]) == 0
    assert main(["tune-strategy", "--out-dir", str(tmp_path / "tuning")]) == 0
    assert main(["notify-test"]) == 0
    assert main(["scheduler", "--json"]) == 0

    captured = capsys.readouterr()
    assert "morning-run saved recommendations" in captured.out
    assert "performance:" in captured.out
    assert "tune-strategy (fixture-only)" in captured.out
    assert "Dawnstrike notification test" in captured.out
    assert "monitor-open" in captured.out
