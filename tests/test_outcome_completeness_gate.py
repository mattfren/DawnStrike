from pathlib import Path

from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_empty_raw_sources_are_not_reported_as_zero(tmp_path: Path) -> None:
    db_path = tmp_path / "outcomes.sqlite"
    SQLiteScanStore(db_path).initialize()
    result = CanonicalPerformanceService(db_path).reconcile()
    assert result["status"] == "NO_DATA"
    assert result["rows"] == []
    assert result["daily"] == []
