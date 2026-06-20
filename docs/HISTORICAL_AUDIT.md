# Historical Audit

Historical audit output is research/paper-trading evidence only. Sample outputs
are fixture-only and are not proven live performance.

Audit entry semantics are explicit:

- `--entry-mode open`: enter at the first eligible post-signal bar open.
- `--entry-mode breakout`: enter only after `breakout_trigger` is touched after
  the signal/recommendation timestamp; otherwise the row is
  `audit_status=no_entry_trigger`.

Use module-entry commands on Windows:

```powershell
py -m intraday_scanner.cli audit-latest `
  --db-path data\scanner.sqlite `
  --minute-bars sample_data\minute_bars\2026-06-18.csv `
  --out-dir outputs\latest_audit `
  --entry-mode breakout `
  --persist
```

Dawnstrike audits saved recommendations against later price data. It is designed
to prevent lookahead bias:

- Recommendations are persisted before audit.
- Audit reads ranked candidates from the saved scan or from a historical ranked CSV.
- Returns are calculated only from bars at or after the configured entry window
  and the saved recommendation/snapshot timestamp.
- If no post-signal bars are available for a recommendation, the row is persisted
  as `audit_status=unavailable` and excluded from return averages.
- Backfills must label any incomplete point-in-time data assumptions.

## Latest Persisted Audit

```powershell
intraday-scan audit-latest ^
  --db-path data\scanner.sqlite ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\latest_audit ^
  --persist
```

## Metrics

The audit writes per-trade rows with:

- entry time and slippage-adjusted entry price
- +1, +5, and +15 minute return
- lunch return
- close return
- high-of-day return after entry
- low-after-entry drawdown
- max favorable excursion
- max adverse excursion
- audit status and reason when post-signal data is unavailable

The summary includes:

- average and median returns
- hit rate
- best and worst close return
- max drawdown
- top 1, top 3, and top 5 equal-weight return groups
- unavailable audit count

## Performance Report

After persisted audits exist:

```powershell
intraday-scan performance-report --db-path data\scanner.sqlite --persist
```

This creates cumulative performance rows in SQLite and prints a JSON report.
