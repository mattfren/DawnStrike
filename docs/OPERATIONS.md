# Operations

On Windows, prefer `py -m intraday_scanner.cli ...` unless `intraday-scan.exe`
is on PATH. Install the local app and dev tools with:

```powershell
py -m pip install -e ".[dev]"
```

Live scans require a supplied symbol universe (`--symbols`, `--symbols-file`, or
`--universe-file`) plus read-only market-data secrets. Sample and fixture returns
are not live performance claims.

## Daily Local Workflow

1. Activate your environment if you use one.
2. Refresh a snapshot from sample data, a provider script, or another data pipeline.
3. Run the scanner.
4. Review `ranked_candidates.csv`, `top_explosive.csv`, and `avoid_list.csv`.
5. Monitor saved recommendations during the session.
6. Run paper audit after market data is available.
7. Build the performance report.
8. Open the dashboard.

## Windows Commands

```powershell
py -m pip install -e .

intraday-scan init-db --db-path data\scanner.sqlite

intraday-scan scan ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --out-dir outputs\sample_scan ^
  --db-path data\scanner.sqlite ^
  --persist ^
  --print

intraday-scan audit-latest ^
  --db-path data\scanner.sqlite ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\sample_audit ^
  --persist

intraday-scan paper-audit ^
  --ranked outputs\sample_scan\ranked_candidates.csv ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\sample_audit ^
  --top-n 3 ^
  --slippage-bps 50

intraday-scan notify --db-path data\scanner.sqlite --dry-run

intraday-scan morning-run ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --out-dir outputs\latest_scan ^
  --db-path data\scanner.sqlite ^
  --notify

intraday-scan monitor-open ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --provider csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --news-provider auto ^
  --sec-rss

intraday-scan performance-report --db-path data\scanner.sqlite --persist

intraday-scan tune-strategy ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\tuning

intraday-scan scheduler

py -m streamlit run app.py
```

## Provider Credentials

Create `.env` from `.env.example` and set Alpaca market-data credentials:

```powershell
ALPACA_API_KEY_ID=...
ALPACA_API_SECRET_KEY=...
ALPACA_DATA_FEED=iex
```

Missing credentials fail before any provider request. Secrets are not printed.

Run live provider-backed monitoring after a persisted scan exists:

```powershell
intraday-scan monitor-open ^
  --provider alpaca ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --continuous ^
  --news-provider auto ^
  --sec-rss
```

## Notifications

`console` notifications require no secrets. Email, Discord, and Telegram are configured from `.env`; missing values fail with clear errors. Sent event keys are stored in SQLite to prevent duplicate alerts for the same scan/ticker/channel.

## Live Risk Feeds

The monitor runs without external feeds by default. Add `--news-provider auto`
to use NewsAPI first, then Finnhub, based on configured keys. Use
`--news-provider newsapi` or `--news-provider finnhub` to force a vendor. Add
`--sec-rss` to pull SEC Atom filings for offering, shelf, warrant, ATM, and
dilution-risk checks. Missing news keys fail clearly and write provider health
for the dashboard.

## Monitor Risk Thresholds

The local monitor evaluates each saved recommendation against configurable
failure triggers:

- `INTRADAY_MONITOR_DROP_FROM_WATCH_PCT`: invalidate when price falls this far
  below the saved watch price.
- `INTRADAY_MONITOR_VOLUME_COLLAPSE_RATIO`: flag fading momentum when current
  dollar volume falls below this ratio of the original setup volume.
- `INTRADAY_MONITOR_REJECTION_RANGE_PCT`: flag breakout rejection when price
  tests the breakout area but falls back below it inside this range threshold.

## Timezone Assumptions

Premarket, signal, lunch exit, and close exit settings are interpreted as exchange-local Eastern Time for sample data and Alpaca bars. Users operating from Central Time should keep the config values in ET unless their upstream data has already been converted.

## Troubleshooting

- Missing snapshot columns: compare your file with `docs/DATA_CONTRACT.md`.
- Empty ranked list: check gap, dollar-volume, share-volume, and price thresholds.
- Empty dashboard latest-output mode: run a scan first or switch to sample CSV mode.
- Alpaca 401 or 403: check keys and data entitlement. Do not paste secrets into logs.
- SQLite errors: check `INTRADAY_DATABASE_PATH` parent directory permissions.
