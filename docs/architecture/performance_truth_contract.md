# Performance truth contract

This is the directive-facing contract for the single canonical performance
read model. The calculation owner is
`intraday_scanner.performance.service.CanonicalPerformanceService`; the bounded
publication owner is
`intraday_scanner.services.public_snapshot_service.write_public_snapshot`.

Raw source tables are not public metrics. They are reconciled into typed,
cohort-separated rows and daily records with source references, calculation
version, input hash, generated timestamp, market date, coverage, and evidence
state. Missing costs, benchmark observations, equity observations, and
outcomes remain null.

The four public cohort identifiers are:

- `official_forward_paper`
- `alphaops_signal_research`
- `historical_backtest`
- `shadow_challenger`

The official daily return is only a portfolio return when opening portfolio
equity is sourced for that market date. Trade-notional return is a diagnostic,
never a substitute for portfolio return. Cumulative return, drawdown, and
excess return remain unavailable when their required inputs are unavailable.

PaperOps daily summaries are an input export, not a second ledger. The
canonical adapter reads
`data/v2_paper_ops_live/calendar/strategy_daily_returns.csv`, derives return
from opening and ending equity, and keeps replay rows in
`historical_backtest` and forward rows in `shadow_challenger`. It validates
total-P&L and after-cost equity identities to the cent. Any row that fails,
duplicates identity, or lacks a required component is retained as
`quarantined` for audit and contributes no valid return. The source's own
`daily_return_pct` field is diagnostic only when it disagrees with the derived
equity return.
