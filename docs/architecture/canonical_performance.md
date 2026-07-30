# Canonical performance contract

Dawnstrike has one performance read model: `CanonicalPerformanceService` in
`intraday_scanner/performance/service.py`. The public site consumes only the
bounded snapshot written by `write_public_snapshot`; it does not calculate
returns in browser code.

## Truth rules

- Official forward paper, AlphaOps research, historical backtest, and shadow
  challenger rows stay in separate cohorts.
- Raw facts, calculated metrics, and model metadata remain distinct.
- Missing exit, benchmark, fee, slippage, or source evidence remains `null`.
  It is never converted to zero.
- Gross observed return is separate from net after-cost return.
- Realized and unrealized rows remain separate. Unresolved rows are labeled
  `missing_outcome` and excluded from realized totals.
- Money is reconciled in integer cents. Every row carries source references,
  source hash, input hash, observation time, and reconciliation time.
- No output authorizes a broker order or claims personalized investment advice.
- Public snapshots carry explicit `unknown` safety-evidence states for source
  quality, halt status, corporate-action status, and liquidity evidence when
  those inputs are not available. Unknown is displayed as not reported and
  fails closed; it is never interpreted as a clean safety result.

## Public contract

`data/performance.json` is capped at 250 rows and 250 KiB compressed using
deterministic gzip sizing. Its manifest records the payload hash, input hash,
row count, raw byte count, compressed byte count, compression method, status,
and research-only flags. The raw byte count remains visible so artifact size
and transfer size cannot be conflated. Statuses are explicit:

- `complete`: complete observed daily records;
- `no_trade`: an explicit daily record says no trade;
- `degraded`: upstream records are partial or unresolved;
- `no_data`: there is no daily record to publish.

The readiness endpoint returns HTTP 200 only for `complete` or explicit
`no_trade`. `degraded` and `no_data` return HTTP 503.
