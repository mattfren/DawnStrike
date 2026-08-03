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
`data/v2_paper_ops_live/calendar/strategy_daily_returns.csv`, keeps replay
rows in `historical_backtest` and forward rows in `shadow_challenger`, and
derives each daily return from the verified change in ending equity from the
prior observation in the same mode/strategy/version/policy series. In this
export, `total_pnl` is normally that daily equity delta, while
`realized_pnl` and `unrealized_pnl` can be scoped to the current event/mark
rather than the full equity history. The adapter therefore validates both
daily total-P&L continuity and the available component/cost identities. A
component-scope mismatch remains an auditable warning when the daily equity
delta is proven; rows without a provable equity/P&L identity, duplicate
identity, or required component are retained as `quarantined` and contribute
no valid return. The source's own `daily_return_pct` field is diagnostic only
when it disagrees with the derived equity return. PaperOps cost fields remain
`reported_not_reconciled` unless the source proves that they are included in
the observed equity delta.

## Frozen V5 / V6 comparison

`dawnstrike.account-comparison.v1` is the only V5/V6/cash/SPY/IWM comparison
contract. Canonical reconciliation persists it with an exact input hash before
the public projection receives bounded aggregates.

- V5 and V6 each require one immutable paper-account ledger row per market
  date, with sourced opening/ending equity, flows, positions, fees, spread,
  slippage, P&L, a source hash, and an explicit `COMPLETE` or `NO_TRADE`
  state.
- Cash is the named `cash_zero_interest_v1` reference; its return is never a
  default for missing data.
- SPY and IWM each require one same-session sourced close return with lineage.
- A V6 decision, outcome, signal return, or equal-weight basket is never a V6
  account return. Missing V6 ledger evidence yields
  `WAITING_FOR_AUTHORITATIVE_V6_ACCOUNT_LEDGER` with null metrics.
- Promotion remains false until all five aligned series are complete.
