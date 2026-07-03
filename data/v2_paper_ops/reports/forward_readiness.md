# PaperOps Forward Readiness

- Status: `ready_with_warnings`
- Data status: `reconciled_with_minor_diffs`
- Ledger rebuild: `passed`
- Calendar truth: `passed`
- Pending orders: `1`
- Open positions: `0`
- Eligible tomorrow: cross_sectional_relative_strength, volatility_contraction_breakout
- Blocked: none
- Quarantined: bullish_fvg_continuation, donchian_breakout_20_10, failed_breakout_reversal_short, pullback_reclaim_uptrend, ts_momentum_sma_atr

## Warnings

- None.

## Suggested Commands

- `py -m intraday_scanner.v2.data_truth build --date 2026-06-29 --no-fetch`
- `py -m intraday_scanner.v2.paper_ops run-day --date 2026-06-29`
- `py -m intraday_scanner.v2.paper_ops rebuild-ledger`
- `py -m intraday_scanner.v2.paper_ops verify-calendar`
- `py -m intraday_scanner.v2.paper_ops evidence`
- `py -m intraday_scanner.v2.paper_ops readiness`
