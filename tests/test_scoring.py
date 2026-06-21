from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.scoring import score_snapshot, score_universe


def test_scoring_ranks_expected_ticker_first():
    rows = read_snapshot_csv("sample_data/premarket_snapshot_sample.csv")
    result = score_universe(rows, ScannerConfig())
    assert result.ranked_candidates[0].ticker == "NOVA"
    assert result.ranked_candidates[0].score_breakdown["liquidity_thrust"] > 0
    assert result.ranked_candidates[0].equation_version == "dawnstrike-v2.0"
    assert result.ranked_candidates[0].setup_grade in {"A+", "A", "B", "C"}
    assert result.ranked_candidates[0].float_rotation_pct > 0
    assert (
        result.ranked_candidates[0].breakout_trigger
        > result.ranked_candidates[0].snapshot.premarket_high
    )


def test_scoring_edge_cases_are_flagged():
    rows = {
        row.ticker: row for row in read_snapshot_csv("sample_data/premarket_snapshot_sample.csv")
    }
    config = ScannerConfig()
    assert "current_halt" in score_snapshot(rows["HALT"], config).avoid_reasons
    assert "low_dollar_volume" in score_snapshot(rows["THIN"], config).avoid_reasons
    assert "recent_offering" in score_snapshot(rows["OFFER"], config).risk_flags
    assert "wide_spread" in score_snapshot(rows["WIDE"], config).risk_flags
    assert "sub_min_price" in score_snapshot(rows["SUBP"], config).risk_flags
    assert "extreme_gap_above_300_pct" in score_snapshot(rows["MOON"], config).risk_flags
    assert score_snapshot(rows["HALT"], config).setup_grade == "AVOID"


def test_no_previous_close_and_zero_volume_are_safe():
    row = read_snapshot_csv("sample_data/premarket_snapshot_sample.csv")[0]
    broken = type(row)(
        ticker="ZERO",
        company="Zero Close",
        premarket_price=2.0,
        previous_close=0.0,
        premarket_high=2.2,
        premarket_low=1.8,
        premarket_volume=0,
        float_shares=None,
        market_cap=None,
        spread_pct=0.5,
        short_float_pct=None,
        has_news=False,
        current_halt=False,
        recent_offering=False,
        reverse_split_90d=False,
        source="test",
        as_of_timestamp=row.as_of_timestamp,
    )
    scored = score_snapshot(broken, ScannerConfig())
    assert scored.gap_pct == 0
    assert "no_previous_close" in scored.avoid_reasons
    assert "zero_volume" in scored.risk_flags
    assert scored.data_quality_score < 8


def test_supplied_gap_pct_is_used_when_previous_close_missing():
    row = read_snapshot_csv("sample_data/premarket_snapshot_sample.csv")[0]
    public_row = type(row)(
        ticker="WEBG",
        company="Web Gap",
        premarket_price=5.0,
        previous_close=0.0,
        premarket_high=5.0,
        premarket_low=5.0,
        premarket_volume=500_000,
        float_shares=None,
        market_cap=50_000_000,
        spread_pct=1.0,
        short_float_pct=None,
        has_news=False,
        current_halt=False,
        recent_offering=False,
        reverse_split_90d=False,
        source="stockanalysis_premarket",
        as_of_timestamp=row.as_of_timestamp,
        dollar_volume=2_500_000,
        gap_pct=42.0,
    )

    scored = score_snapshot(public_row, ScannerConfig())

    assert scored.gap_pct == 42.0
    assert "no_previous_close" in scored.risk_flags
    assert "no_previous_close" not in scored.avoid_reasons
