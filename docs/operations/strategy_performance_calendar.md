# Strategy Performance Calendar

The mounted Streamlit dashboard exposes the PaperOps strategy calendar under
**Calendar → Strategy fleet**. The older AlphaOps signal history remains
available under **Calendar → Intraday signal archive**.

## Purpose

The strategy calendar is a read-only operator view of retained paper-account
evidence. It answers four questions without rebuilding or mutating PaperOps:

1. How did every official strategy perform on each completed session?
2. How did the official fleet perform relative to the configured-universe
   benchmark and cash?
3. What account, risk, fill, position, and cost state produced each result?
4. Is the displayed result backed by current reconciliation and source-bar
   truth?

It does not place orders and is not a broker execution surface.

## Canonical inputs

The read model uses the production PaperOps root and these retained artifacts:

- calendar/strategy_daily_returns.csv
- state/strategy_registry.json
- state/strategy_challenger_registry.json
- exports/paper_trade_blotter.json
- reconciliation/reconciliation_latest.json
- reconciliation/calendar_truth_latest.json
- reconciliation/ledger_rebuild_latest.json
- reconciliation/source_bar_truth_MODE_latest.json
- reconciliation/trade_blotter_verify_latest.json

No dashboard value is sourced from generated static command-center pages.

## Evidence lanes

Forward, replay, and demo are separate evidence lanes. The UI never combines
them. When a lane has no retained rows, it is shown as empty rather than as a
zero-return period. Replay evidence is labeled historical research and is
never presented as forward performance.

Every time series is isolated by the full identity:

    mode + strategy_id + strategy_version + execution_policy_version
    + strategy_semantics_fingerprint

Official, challenger, benchmark, cash, and unregistered rows have separate
roles. Only exact registry matches are included in official fleet claims.

## Return math

Calendar values are parsed and aggregated with decimal arithmetic. Stored
return fields are fractions, so 0.01 is displayed as 1.00% exactly once.

For a completed session:

    session open equity = ending equity - total P&L

    fleet daily return =
        sum(official total P&L) / sum(official session open equity)

    fleet cumulative return =
        (sum(official ending equity) - sum(official starting equity))
        / sum(official starting equity)

Benchmark, cash, challenger, and unregistered rows are excluded from official
fleet aggregation. Benchmark excess is the official fleet return minus the
configured-universe benchmark return.

## Truth and missing-data rules

Official values display only when all core gates pass, the selected mode's
source-bar truth gate passes, and every gate is at least as new as the calendar
it verifies. A mismatched source-bar mode also blocks the lane.

Missing, non-finite, incomplete, stale, or failed evidence remains **N/A**.
Missing values are never converted to zero. Exact zero is labeled **Flat**.
The matrix reports positive, negative, flat, and missing session counts
separately.

## Operator workflow

The primary workflow is deliberately one-click:

1. Choose **Paper** or **Replay** when both result types are available.
2. Choose a month.
3. Click a retained day in the calendar.
4. Read the exact daily return for every official strategy immediately below.

The latest retained day is selected automatically. Calendar cells contain only
the day and fleet return. The selected-day panel always includes every official
strategy; a missing row remains visible as **N/A** rather than disappearing or
becoming zero. Benchmark, P&L, drawdown, positions, costs, and lineage remain
available under the single collapsed **More details** section.

The seven-column calendar fits the page on narrow screens without introducing
horizontal scrolling. Retained-day controls are native keyboard-accessible
buttons with text labels for gain, loss, flat, and missing states.

## Verification

Run the focused dashboard contract:

~~~powershell
py -m pytest tests/test_paper_ops_dashboard_calendar.py tests/test_strategy_calendar_page.py tests/test_strategy_calendar_mount.py tests/test_streamlit_app.py
py -m ruff check intraday_scanner/dashboard/paper_ops_calendar_service.py intraday_scanner/dashboard/strategy_calendar_page.py
py -m mypy intraday_scanner/dashboard/paper_ops_calendar_service.py intraday_scanner/dashboard/strategy_calendar_page.py
~~~
