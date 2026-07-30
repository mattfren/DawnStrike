import csv
from pathlib import Path

from intraday_scanner.performance.service import CanonicalPerformanceService

FIELDS = [
    "date",
    "mode",
    "strategy_id",
    "strategy_version",
    "strategy_status",
    "execution_policy_version",
    "strategy_semantics_fingerprint",
    "data_snapshot_id",
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "trades_opened",
    "trades_closed",
    "pending_orders",
    "open_positions",
    "wins",
    "losses",
    "flats",
    "average_r",
    "expectancy_r",
    "exposure_pct",
    "fees_paid",
    "slippage_estimate",
    "warnings",
    "run_id",
]


def _write_calendar(root: Path) -> None:
    calendar = root / "calendar"
    calendar.mkdir(parents=True)
    rows = [
        {
            "date": "2026-07-29",
            "mode": "replay",
            "strategy_id": "replay_strategy",
            "strategy_version": "v1",
            "execution_policy_version": "paper-policy-v1",
            "data_snapshot_id": "snapshot-replay",
            "starting_equity": "100000",
            "ending_equity": "100100",
            "realized_pnl": "0",
            "unrealized_pnl": "101",
            "total_pnl": "101",
            "daily_return_pct": "0.001",
            "trades_opened": "1",
            "trades_closed": "0",
            "open_positions": "1",
            "exposure_pct": "10",
            "fees_paid": "0",
            "slippage_estimate": "1",
            "run_id": "run-replay",
        },
        {
            "date": "2026-07-29",
            "mode": "forward",
            "strategy_id": "shadow_strategy",
            "strategy_version": "v2",
            "execution_policy_version": "paper-policy-v2",
            "data_snapshot_id": "snapshot-forward",
            "starting_equity": "100000",
            "ending_equity": "99900",
            "realized_pnl": "-50",
            "unrealized_pnl": "-20",
            "total_pnl": "-70",
            "daily_return_pct": "-0.001",
            "trades_opened": "1",
            "trades_closed": "1",
            "open_positions": "0",
            "exposure_pct": "5",
            "fees_paid": "1",
            "slippage_estimate": "1",
            "run_id": "run-forward",
        },
    ]
    with (calendar / "strategy_daily_returns.csv").open(
        "w", newline="", encoding="utf-8"
    ) as stream:
        writer = csv.DictWriter(stream, fieldnames=FIELDS)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in FIELDS})


def test_paper_ops_is_cohort_separated_and_quarantines_bad_equity_math(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    _write_calendar(root)

    result = CanonicalPerformanceService(
        tmp_path / "empty.sqlite",
        paper_ops_root=root,
    ).reconcile(persist=False, now="2026-07-29T21:00:00+00:00")

    rows = {row["record_id"]: row for row in result["rows"]}
    replay_id = "paper_ops_daily:2026-07-29:replay:replay_strategy:v1:run-replay"
    forward_id = "paper_ops_daily:2026-07-29:forward:shadow_strategy:v2:run-forward"
    assert rows[replay_id]["cohort"] == "historical_backtest"
    assert rows[forward_id]["cohort"] == "shadow_challenger"
    assert rows[replay_id]["return_pct"] == 0.1
    assert rows[forward_id]["record_status"] == "quarantined"
    daily = {
        row["strategy_id"]: row
        for row in result["daily"]
        if row["market_date"] == "2026-07-29"
    }
    assert daily["replay_strategy"]["realized_trade_count"] == 0
    assert daily["replay_strategy"]["unrealized_trade_count"] == 1
    report = result["paper_ops_reconciliation"]
    assert report["source_row_count"] == 2
    assert report["accepted_count"] == 1
    assert report["quarantined_count"] == 1
    assert report["issue_count"] == 1
    assert result["issue_count"] == 1
