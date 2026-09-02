# Windows Scheduler Setup

Dawnstrike can run locally with Windows Task Scheduler. The app remains
research/watchlist only; no broker orders are placed.

This page's helper commands are local-development examples only. Production
uses the exact-SHA five-task contract and protected activation procedure in
`operations/runtime_activation_and_rollback.md`; never register, repair, or
replay production tasks from this page.

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

The repository contains older local scheduler helpers, but they are not a
production registration or repair path. Use the dashboard only with a
disposable local research database.

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
return or replay it as forward evidence. The former July 2026 manual
`record-forward-gap` procedure is archived and authorizes no current mutation.
A production gap must be emitted by the exact-SHA scheduled finalizer or by a
separately reviewed, protected, exact-date recovery operation after proving the
absence of conflicting calendar, report, and ledger evidence.

The strict-schema record is sequence-chained and bound to a separately chained,
HMAC-signed anchor journal containing the exact ledger digest. The signing key
lives only in `C:\r\dawnstrike-state\secrets\runtime.env`, outside PaperOps
state, and is never printed or published. Calendar truth then reports
`passed_with_warnings`, the rendered calendar labels the session
`Missing - not zero`, and any tampering or conflicting later forward evidence
fails closed.
