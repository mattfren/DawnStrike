# Historical Signal Ledger

Dawnstrike now keeps a permanent research ledger for every official AlphaOps
signal and no-clean-edge decision. The ledger is paper validation only. It does
not place broker orders, hold broker credentials, or tell the operator to buy or
sell.

## What Gets Saved

Every AlphaOps run writes `historical_signals` before outcomes can be imported.
Each row records the timestamp, market date, ticker, rank, model/config identity,
source lineage, research label, watch level, invalidation line, targets, risk
flags, catalyst summary, and the raw signal payload as known at signal time.

Research labels are:

- `ENTRY WATCH`
- `BREAKOUT WATCH`
- `WATCH ONLY`
- `NO CLEAN EDGE`
- `EXIT SIGNAL`
- `INVALIDATED`
- `THESIS BROKEN`
- `OUTCOME NEEDED`

No-clean-edge days are saved with ticker `NO_TRADE`, the no-trade reason, source
status, and candidate count. They are visible in the dashboard as `NO TRADE`.

## Signal Events

`signal_events` records the signal lifecycle:

- `ENTRY_WATCH_CREATED`
- `NO_CLEAN_EDGE_CREATED`
- `TELEGRAM_SENT`
- `TRIGGER_TOUCHED`
- `BREAKOUT_CONFIRMED`
- `EXIT_SIGNAL`
- `INVALIDATED`
- `THESIS_BROKEN`
- `OUTCOME_IMPORTED`
- `AUDITED`

Telegram notification keys are linked back to the historical signal rows. A
dry-run or console notification can be recorded as an event, but it is not a
broker action.

## Outcome Attachment

Manual outcome imports attach to a matching `historical_signals` row by ticker,
market date, and signal timestamp. If an outcome timestamp is before the signal
timestamp, the row is rejected and written to `rejected_outcomes.csv`.

Accepted outcome columns:

```text
date,ticker,entry_time,entry_price,price_1m,price_5m,price_15m,lunch_price,close_price,high_after_entry,low_after_entry,halted,source,notes
```

Missing prices remain blank and unavailable. They are never converted to zero.

## Commands

Import outcomes:

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
```

Calculate attribution:

```powershell
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
```

Write the historical report:

```powershell
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
```
