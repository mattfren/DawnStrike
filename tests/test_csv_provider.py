import pytest

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.providers import BaseProvider, CSVProvider
from intraday_scanner.providers.csv_provider import read_snapshot_csv


def test_csv_provider_loads_sample_rows():
    provider = CSVProvider("sample_data/premarket_snapshot_sample.csv")
    rows = provider.get_premarket_snapshot(["NOVA", "RIFT"], ScannerConfig())
    assert [row.ticker for row in rows] == ["NOVA", "RIFT"]
    assert rows[0].dollar_volume == 7_800_000
    assert round(rows[0].gap_pct, 2) == 89.09
    assert rows[0].catalyst_headline == "FDA fast-track catalyst"
    assert rows[0].catalyst_url == ""
    assert isinstance(provider, BaseProvider)


def test_csv_provider_reports_missing_columns(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("ticker,premarket_price\nNOVA,5.20\n", encoding="utf-8")
    with pytest.raises(SnapshotValidationError, match="missing required column"):
        read_snapshot_csv(bad)
