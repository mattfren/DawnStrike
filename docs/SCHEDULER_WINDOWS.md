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

## AlphaOps EOD Truth Rules

The EOD runner always captures shadow research outcomes, but official
reconciliation and canonical learning run only when the frozen
`official_telegram` cohort contains real signals and every exact `signal_id`
has a conclusive sourced outcome. An explicit `NO_TRADE` member with matching
Telegram delivery proof records those stages as `SKIPPED_NOT_APPLICABLE`.
Aggregate shadow outcome gaps remain diagnostic: they cannot authorize or
block the official cohort. Missing cohort, delivery, or exact outcome evidence
still fails closed, and missing shadow outcomes remain missing and
learning-ineligible.

If a historical PaperOps forward session never ran, do not synthesize a zero
return or replay it as forward evidence. After confirming that the session has
no calendar row, completed report, or ledger event, record the terminal gap:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass `
  -File C:\r\dawnstrike-runtime\scripts\ensure_forward_gap_signing_key.ps1 `
  -StateRoot C:\r\dawnstrike-state
. C:\r\dawnstrike-runtime\scripts\import_dawnstrike_environment.ps1
Import-DawnstrikeEnvironment -StateRoot C:\r\dawnstrike-state
py -m intraday_scanner.v2.paper_ops record-forward-gap `
  --date 2026-07-31 `
  --reason-code scheduler_run_absent `
  --output-root C:\r\dawnstrike-state\v2_paper_ops_live
```

The strict-schema record is sequence-chained and bound to a separately chained,
HMAC-signed anchor journal containing the exact ledger digest. The signing key
lives only in `C:\r\dawnstrike-state\secrets\runtime.env`, outside PaperOps
state, and is never printed or published. Calendar truth then reports
`passed_with_warnings`, the rendered calendar labels the session
`Missing - not zero`, and any tampering or conflicting later forward evidence
fails closed.
