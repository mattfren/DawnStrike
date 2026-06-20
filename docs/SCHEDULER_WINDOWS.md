# Windows Scheduler Setup

Dawnstrike can run locally with Windows Task Scheduler. The app remains
research/watchlist only; no broker orders are placed.

Use `py -m intraday_scanner.cli ...` in scheduled actions unless you have added
the Python Scripts directory to PATH. Scheduler JSON includes market-day,
holiday, early-close, retry, and skip-reason fields from the local static market
calendar fallback. It does not override missing API secrets or missing live
market data.

Print the default schedule:

```powershell
intraday-scan scheduler
```

Default Central Time plan:

- 8:00 AM: build/pull premarket snapshot
- 8:10 AM: run scanner and persist recommendations
- 8:15 AM: send recommendation alerts
- 8:30 AM: start 1-minute market-open monitor
- 11:30 AM: lunch audit
- 3:00 PM: close audit
- End of day: performance update

## Manual Commands

Morning scan:

```powershell
intraday-scan morning-run ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --out-dir outputs\latest_scan ^
  --db-path data\scanner.sqlite ^
  --notify
```

One-pass monitor check:

```powershell
intraday-scan monitor-open ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist
```

Continuous 1-minute monitor:

```powershell
intraday-scan monitor-open ^
  --provider alpaca ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --continuous
```

## Existing Helper Script

The repo also includes:

```powershell
.\scripts\register_dawnstrike_tasks.ps1
```

Use the dashboard `5-Min Monitor` button or this script for the current local
5-minute task setup. Use `monitor-open --continuous` when you want 1-minute
market-open monitoring.
