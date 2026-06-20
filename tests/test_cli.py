from intraday_scanner.cli import main
from intraday_scanner.models import SnapshotRow
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_cli_init_db_creates_sqlite(tmp_path):
    db_path = tmp_path / "scanner.sqlite"
    assert main(["init-db", "--db-path", str(db_path)]) == 0
    assert db_path.exists()


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

    status = main(["live-scan", "--symbols", "NOVA", "--db-path", str(db_path)])

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
