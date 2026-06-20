# Backtesting And Paper Audit

Paper audit measures research ideas after the fact. It does not create trading instructions or order execution.

## Audit Model

- Entry: regular open, using the first bar at or after `INTRADAY_SIGNAL_TIME`.
- Lunch exit: configurable with `INTRADAY_LUNCH_EXIT_TIME`, default `12:30` ET.
- Close exit: configurable with `INTRADAY_CLOSE_EXIT_TIME`, default `15:59` ET.
- Slippage: `INTRADAY_SLIPPAGE_BPS` is applied to entry.
- High return: best high after entry.
- Drawdown: lowest low after entry.

## Commands

Audit a ranked CSV:

```powershell
intraday-scan paper-audit `
  --ranked outputs/latest_scan/ranked_candidates.csv `
  --minute-bars sample_data/minute_bars/2026-06-18.csv `
  --out-dir outputs/latest_audit
```

Audit the latest persisted scan:

```powershell
intraday-scan audit-latest `
  --db-path data/scanner.sqlite `
  --minute-bars sample_data/minute_bars/2026-06-18.csv `
  --out-dir outputs/latest_audit `
  --persist
```

Backfill a historical ranked CSV and persist the audit:

```powershell
intraday-scan backfill-audit `
  --ranked outputs/2026-06-18/ranked_candidates.csv `
  --minute-bars sample_data/minute_bars/2026-06-18.csv `
  --out-dir outputs/2026-06-18-audit `
  --persist
```

## Summary Metrics

`paper_audit_summary.json` includes:

- average lunch/close/high returns
- median lunch/close/high returns
- close and lunch hit rates
- best and worst close return
- max low-after-entry drawdown
- cumulative top 1/top 3/top 5 lunch/close/high returns
