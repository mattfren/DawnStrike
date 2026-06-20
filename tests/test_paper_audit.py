from intraday_scanner.config import ScannerConfig
from intraday_scanner.providers.csv_provider import read_snapshot_csv
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.scoring import score_universe
from intraday_scanner.services.audit_service import calculate_audit, run_paper_audit


def test_paper_audit_calculates_lunch_close_and_high_returns(tmp_path):
    result = score_universe(
        read_snapshot_csv("sample_data/premarket_snapshot_sample.csv"), ScannerConfig()
    )
    scan_dir = tmp_path / "scan"
    write_scan_outputs(result, scan_dir)
    audit_dir = tmp_path / "audit"
    paths = run_paper_audit(
        scan_dir / "ranked_candidates.csv",
        "sample_data/minute_bars/2026-06-18.csv",
        audit_dir,
        ScannerConfig(slippage_bps=50),
        top_n=3,
    )
    assert paths["trades"].exists()
    assert paths["summary"].exists()
    trades_text = paths["trades"].read_text(encoding="utf-8")
    summary_text = paths["summary"].read_text(encoding="utf-8")
    assert "low_drawdown_pct" in trades_text
    assert "slippage_bps" in trades_text
    assert "avg_lunch_return_pct" in summary_text
    assert "cumulative_returns" in summary_text
    assert "max_drawdown_pct" in summary_text


def test_paper_audit_converts_central_time_bars_to_eastern_windows():
    result = calculate_audit(
        [{"ticker": "TZ", "breakout_trigger": "11", "score": "99"}],
        [
            {
                "ticker": "TZ",
                "timestamp": "2026-06-18T08:30:00-05:00",
                "open": "10",
                "high": "10.5",
                "low": "9.5",
                "close": "10.2",
                "volume": "1000",
            },
            {
                "ticker": "TZ",
                "timestamp": "2026-06-18T11:30:00-05:00",
                "open": "11.8",
                "high": "12.2",
                "low": "11.5",
                "close": "12",
                "volume": "1000",
            },
            {
                "ticker": "TZ",
                "timestamp": "2026-06-18T14:59:00-05:00",
                "open": "12.8",
                "high": "13.2",
                "low": "12.6",
                "close": "13",
                "volume": "1000",
            },
        ],
        ScannerConfig(slippage_bps=0),
        top_n=1,
    )

    assert result.trades[0]["entry_time"] == "2026-06-18T08:30:00-05:00"
    assert result.trades[0]["lunch_return_pct"] == 20.0
    assert result.trades[0]["close_return_pct"] == 30.0


def test_paper_audit_uses_only_bars_after_recommendation_timestamp():
    result = calculate_audit(
        [
            {
                "ticker": "NOVA",
                "breakout_trigger": "11",
                "score": "99",
                "timestamp": "2026-06-18T10:00:00-04:00",
            }
        ],
        [
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-18T09:30:00-04:00",
                "open": "10",
                "high": "10.5",
                "low": "9.8",
                "close": "10.1",
                "volume": "1000",
            },
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-18T10:00:00-04:00",
                "open": "12",
                "high": "13",
                "low": "11.8",
                "close": "12.5",
                "volume": "1000",
            },
        ],
        ScannerConfig(slippage_bps=0),
        top_n=1,
    )

    assert result.trades[0]["audit_status"] == "audited"
    assert result.trades[0]["entry_time"] == "2026-06-18T10:00:00-04:00"
    assert result.trades[0]["entry_price"] == 12
    assert result.summary["audit_unavailable_count"] == 0


def test_paper_audit_marks_missing_post_signal_bars_unavailable():
    result = calculate_audit(
        [
            {
                "ticker": "NOVA",
                "breakout_trigger": "11",
                "score": "99",
                "timestamp": "2026-06-18T10:00:00-04:00",
            }
        ],
        [
            {
                "ticker": "NOVA",
                "timestamp": "2026-06-18T09:30:00-04:00",
                "open": "10",
                "high": "10.5",
                "low": "9.8",
                "close": "10.1",
                "volume": "1000",
            }
        ],
        ScannerConfig(slippage_bps=0),
        top_n=1,
    )

    assert result.trades[0]["audit_status"] == "unavailable"
    assert "recommendation timestamp" in result.trades[0]["audit_reason"]
    assert result.summary["trade_count"] == 0
    assert result.summary["audit_unavailable_count"] == 1
