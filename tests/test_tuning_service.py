from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.reporting import read_csv_dicts
from intraday_scanner.services.tuning_service import run_strategy_tuning, write_tuning_outputs


def test_strategy_tuning_runs_on_fixtures_and_writes_outputs(tmp_path):
    report = run_strategy_tuning(
        snapshots=read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"),
        minute_bars=read_csv_dicts("sample_data/minute_bars/2026-06-18.csv"),
        base_config=ScannerConfig(),
        fixture_only=True,
    )
    paths = write_tuning_outputs(report, tmp_path)

    assert report["fixture_only"] is True
    assert report["scenario_count"] >= 3
    assert report["best"]["scenario"]
    assert "top_3_close_return_pct" in report["best"]
    assert paths["csv"].exists()
    assert paths["summary"].exists()
