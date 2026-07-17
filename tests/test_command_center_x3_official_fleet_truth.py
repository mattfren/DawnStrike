from __future__ import annotations

from intraday_scanner.v2.command_center_x2.adapters import _aggregate_return


def test_month_return_compounds_daily_fleet_without_counting_sleeve_equity_twice() -> None:
    july_16_pnl = [
        21.32332408,
        -45.80419720,
        -34.78249387,
        0.0,
        0.0,
        0.0,
        0.0,
    ]
    rows: list[dict[str, object]] = []
    for session_date, pnl_values in (
        ("2026-07-15", [0.0] * 7),
        ("2026-07-16", july_16_pnl),
    ):
        for index, pnl in enumerate(pnl_values):
            rows.append(
                {
                    "date": session_date,
                    "strategy_id": f"official_{index}",
                    "daily_return_pct": pnl / 100_000.0,
                    "total_pnl": pnl,
                    "ending_equity": 100_000.0 + pnl,
                }
            )

    assert _aggregate_return(rows, "daily_return_pct") == "-0.008466%"
