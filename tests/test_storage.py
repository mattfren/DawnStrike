import sqlite3

from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.scoring import score_universe
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_sqlite_persists_raw_snapshots_and_recommendations(tmp_path):
    db_path = tmp_path / "scanner.sqlite"
    result = score_universe(
        read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"), ScannerConfig()
    )

    SQLiteScanStore(db_path).persist_scan_result(result)

    with sqlite3.connect(db_path) as connection:
        raw_count = connection.execute("SELECT COUNT(*) FROM raw_snapshots").fetchone()[0]
        thesis_count = connection.execute(
            "SELECT COUNT(*) FROM recommendation_theses"
        ).fetchone()[0]

    assert raw_count == len(result.all_candidates)
    assert thesis_count == len(result.ranked_candidates)
