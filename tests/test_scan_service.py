import sqlite3

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_scan_service_and_storage_persist_latest_scan(tmp_path):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite")
    store = SQLiteScanStore(config.database_path)
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    result = ScanService(provider, store=store).run(config, persist=True)
    latest = store.load_latest_scan()
    assert latest is not None
    assert latest["summary"]["run_id"] == result.run_id
    assert latest["top_explosive"]
    history = store.load_scan_history()
    assert history[0]["run_id"] == result.run_id
    with sqlite3.connect(config.database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
    assert {"snapshots", "ranked_candidates", "notifications_sent"} <= tables


def test_cli_style_scan_writes_output_files(tmp_path):
    config = ScannerConfig(output_dir=tmp_path / "scan")
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    result = ScanService(provider).run(config)
    paths = write_scan_outputs(result, config.output_dir)
    assert paths["ranked_candidates"].exists()
    assert paths["avoid_list"].exists()
    assert paths["top_explosive"].exists()
