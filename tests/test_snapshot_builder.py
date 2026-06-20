import csv

from intraday_scanner.snapshot_builder import build_snapshot


def test_snapshot_builder_creates_valid_snapshot(tmp_path):
    out = tmp_path / "snapshot.csv"
    rows = build_snapshot(
        "sample_data/builder/premarket_bars_sample.csv",
        "sample_data/builder/previous_close_sample.csv",
        "sample_data/builder/metadata_sample.csv",
        out,
    )
    assert out.exists()
    assert len(rows) == 2
    assert rows[0].ticker == "NOVA"
    with out.open("r", encoding="utf-8-sig", newline="") as handle:
        loaded = list(csv.DictReader(handle))
    assert loaded[0]["premarket_volume"] == "1500000"
    assert loaded[0]["dollar_volume"] == "7800000.0"
    assert loaded[0]["gap_pct"] == "89.0909090909091"
    assert "catalyst_headline" in loaded[0]
