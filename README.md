# Dawnstrike Intraday Scanner

Dawnstrike is an aggressive intraday research scanner. It ranks high-volatility premarket momentum names, separates candidates from avoid/do-not-touch names, and supports offline paper audits. It does not place trades and does not provide financial advice.

Windows note: prefer module-entry commands until your Python Scripts directory is on PATH:

```powershell
py -m pip install -e ".[dev]"
py -m intraday_scanner.cli --help
```

Sample returns are fixture-only. Live scans need read-only market-data secrets and a universe source such as `--symbols`, `--symbols-file`, or `--universe-file`. Alpaca market data does not supply every enrichment field by itself, so use enrichment files/providers for float, market cap, short float, catalyst URL, halt, and offering context when available. Dawnstrike has no auto-trading or broker order-execution path.

## Free Shadow Mode

Free Shadow Mode is the zero-dollar paper-validation loop. It lets you manually
import a premarket screener CSV, run the normal scoring engine, save the exact
calls, upload manual outcomes after lunch/close, and build top1/top3/top5 shadow
reports. Data is labeled manual/free and is not treated as paid/live validation.

```powershell
py -m intraday_scanner.cli auto-shadow-from-screener --input data\inbox\screener\morning_export.csv --db-path data\shadow.sqlite --out-dir outputs\auto_shadow\morning_export --ai-normalizer none --persist --print
py -m intraday_scanner.cli watch-screener-inbox --inbox data\inbox\screener --db-path data\shadow.sqlite --out-root outputs\auto_shadow --ai-normalizer none --poll-seconds 10
py -m intraday_scanner.cli print-upload-prompt
py -m intraday_scanner.cli import-manual-snapshot --input templates\manual_premarket_snapshot_template.csv --out outputs\shadow_manual_snapshot --db-path data\shadow.sqlite --persist
py -m intraday_scanner.cli free-shadow-scan --snapshot outputs\shadow_manual_snapshot\premarket_snapshot.csv --db-path data\shadow.sqlite --out-dir outputs\shadow_scan --persist --print
py -m intraday_scanner.cli import-manual-outcomes --input templates\manual_outcomes_template.csv --db-path data\shadow.sqlite --persist
py -m intraday_scanner.cli audit-manual-outcomes --db-path data\shadow.sqlite --out-dir outputs\shadow_audit --persist
py -m intraday_scanner.cli evaluate-intelligence-outcomes --db-path data\shadow.sqlite --out-dir outputs\intelligence_outcomes --persist
py -m intraday_scanner.cli free-shadow-report --db-path data\shadow.sqlite --out-dir outputs\shadow_report --persist
```

See `docs\SCREENER_AUTOMATION.md`, `docs\FREE_SHADOW_MODE.md`, `docs\MANUAL_UPLOADS.md`,
`docs\E2E_AUTOMATION.md`, `docs\NOTIFICATION_ONLY_WORKFLOW.md`,
`docs\FREE_DATA_PIPELINE.md`, `docs\DATA_QUALITY.md`, and
`docs\PREMARKET_INTELLIGENCE.md`.

Notification-only automation:

```powershell
py -m intraday_scanner.cli automation-run --mode once --config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
py -m intraday_scanner.cli automation-daemon --config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
```

Web Auto-Pilot with Telegram-ready notifications:

```powershell
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto\today --persist --print
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.example.yaml --out-dir outputs\source_doctor --print
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.example.yaml --automation-config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify console --dry-run --max-cycles 1
```

Use `--notify telegram` after setting `TELEGRAM_BOT_TOKEN` and
`TELEGRAM_CHAT_ID`. Telegram messages use compact emoji-based text, not raw JSON.
At least one candidate source is required for picks: `local_inbox`,
`public_table_url`, or `browser_table_url`. The practical source hierarchy is
local inbox first, then StockAnalysis, TradingView, optional MarketWatch,
optional Investing.com, and Barchart browser only as a disabled fallback.
`nasdaq_symbols` is universe-only and does not generate premarket picks. If a
public page such as Barchart returns `no_candidate_table` or a login/CAPTCHA
block, run `web-source-doctor`, use a local CSV, enable another candidate
source, or install the optional browser extractor:

```powershell
py -m pip install -e ".[browser]"
py -m playwright install chromium
```

Public URL and browser-rendered data are unverified shadow data. Dawnstrike has
no order execution path and does not bypass protected pages. See `docs\WEB_AUTO_PILOT.md`,
`docs\TELEGRAM_NOTIFICATIONS.md`, `docs\URL_INGESTION.md`, and
`docs\BROWSER_SOURCE_EXTRACTION.md`.

AlphaOps v4 adaptive research layer:

```powershell
py -m intraday_scanner.cli alpha-morning --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_morning --notify console --dry-run
py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify console --dry-run
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

AlphaOps persists feature vectors, source reliability, setup memory, shadow
outcome labels, and no-trade decisions. Fewer than 20 real shadow days are shown
as insufficient sample, not proven expectancy. See `docs\ALPHAOPS_V4.md` and
`docs\PLAYBOOK_ENGINE.md`.

## Install

```powershell
py -m pip install -e .
```

For development tools:

```powershell
py -m pip install -e ".[dev]"
```

## Run the Sample Scanner

```powershell
intraday-scan scan ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --out-dir outputs\sample_scan ^
  --db-path data\scanner.sqlite ^
  --persist ^
  --print
```

Outputs:

- `outputs\sample_scan\ranked_candidates.csv`
- `outputs\sample_scan\top_explosive.csv`
- `outputs\sample_scan\avoid_list.csv`
- `outputs\sample_scan\scan_summary.json`

## Run the Dashboard

```powershell
py -m streamlit run app.py
```

The dashboard can read sample CSV data, latest output files, or SQLite. It does not require Alpaca credentials for sample mode.

Use the `Run Center` tab to run the local workflow from the web UI:

- initialize SQLite
- run the scan
- run the paper audit
- preview notification events
- monitor the saved setup list against a fresh snapshot
- register/check the 5-minute local monitor tasks
- run the full sample backtest in one click

## Dawnstrike Signal Engine v3

The scanner uses `dawnstrike-signal-engine-v3`, an explainable research model
that emits total, explosive, tradability, catalyst, risk, data-quality,
expected-return-bucket, confidence-bucket, source-lineage, model-version, and
config-hash fields. See `docs\SIGNAL_ENGINE_V3.md` and `docs\FORMULA.md`.

## Premarket Intelligence Layer

Every output row includes a simple research label, catalyst category, premarket
structure read, float-rotation label, confirmation-first review levels, data
confidence score, data warnings, source confidence, and probability fields.
Labels are limited to `WATCH`, `BREAKOUT WATCH`, `HIGH VOLATILITY WATCH`,
`CAUTION`, `AVOID`, `INVALIDATED`, `THESIS BROKEN`, and `OUTCOME NEEDED`.
See `docs\PREMARKET_INTELLIGENCE.md`.

## Expectancy Model

The dashboard includes an expectancy model that estimates expected paper return, confidence, and a likely range from scanner features plus paper-audit outcomes. Confidence is capped when audit history is sparse. See `docs\EXPECTANCY_MODEL.md`.

## Run a Paper Audit

```powershell
intraday-scan paper-audit ^
  --ranked outputs\sample_scan\ranked_candidates.csv ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\sample_audit ^
  --top-n 3 ^
  --slippage-bps 50
```

Audit the latest persisted scan directly:

```powershell
intraday-scan audit-latest ^
  --db-path data\scanner.sqlite ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\latest_audit ^
  --persist
```

## Initialize SQLite

```powershell
intraday-scan init-db --db-path data\scanner.sqlite
```

SQLite stores scan runs, snapshots, ranked candidates, top explosive picks, avoid lists, paper audits, and sent notification keys.

## Monitor Saved Setups

After a persisted scan exists, check whether the earlier ranked names are still following the original setup levels:

```powershell
intraday-scan monitor-setups ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --provider csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist
```

To include live headline/filing risk checks during monitoring, add one of the
news providers plus optional SEC RSS:

```powershell
intraday-scan monitor-open ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --provider csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --news-provider auto ^
  --sec-rss
```

For a 5-minute local loop:

```powershell
.\scripts\run_monitor_loop.ps1
```

See `docs\AUTOMATION.md` for the web UI flow and Windows Scheduled Task setup.

## Notifications

Console notifications work without credentials:

```powershell
intraday-scan notify --db-path data\scanner.sqlite --dry-run
intraday-scan notify --db-path data\scanner.sqlite
```

Set `INTRADAY_NOTIFIER_CHANNELS=email,discord,telegram` and the matching `.env` values to use external channels. See `docs\NOTIFICATIONS.md`.

## Configure Alpaca Market Data

Copy `.env.example` to `.env` and set:

```powershell
ALPACA_API_KEY_ID=your_key
ALPACA_API_SECRET_KEY=your_secret
ALPACA_DATA_FEED=iex
```

Run a provider-backed scan:

```powershell
intraday-scan live-scan ^
  --provider alpaca ^
  --symbols TSLA,NVDA,AMD ^
  --out-dir outputs\live_scan ^
  --print
```

If Alpaca credentials are missing, the command fails with an actionable error. Secrets are read from environment variables or `.env`; they are never printed.

## Build a Snapshot From Bars

```powershell
build-snapshot ^
  --minute-bars sample_data\builder\premarket_bars_sample.csv ^
  --previous-close sample_data\builder\previous_close_sample.csv ^
  --metadata sample_data\builder\metadata_sample.csv ^
  --out outputs\builtin_snapshot.csv
```

## Test

```powershell
py -m pytest
py -m ruff check .
py -m mypy intraday_scanner
```

Tests are offline and use sample data or temporary fixtures.

## Production-Style Local Workflow

These commands are research/watchlist and paper-trading only. Dawnstrike never
submits broker orders and never stores broker credentials.

Initialize storage:

```powershell
intraday-scan init-db --db-path data\scanner.sqlite
```

Run the morning workflow in sample mode:

```powershell
intraday-scan morning-run ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --out-dir outputs\latest_scan ^
  --db-path data\scanner.sqlite ^
  --notify
```

Run a safe one-pass market-open monitor check:

```powershell
intraday-scan monitor-open ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist
```

Run continuous 1-minute monitoring:

```powershell
intraday-scan monitor-open ^
  --provider alpaca ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --continuous
```

Use `--provider csv` for sample snapshots and `--provider alpaca` for live
market-data snapshots once Alpaca secrets are set. Use `--news-provider auto`
to select NewsAPI or Finnhub from configured keys, or force
`--news-provider newsapi` / `--news-provider finnhub`. Use `--sec-rss` to check
SEC Atom filings for dilution/offering risk. Alerts are deduped by scan, ticker,
event type, and severity.

Monitor thresholds are configurable with
`INTRADAY_MONITOR_DROP_FROM_WATCH_PCT`,
`INTRADAY_MONITOR_VOLUME_COLLAPSE_RATIO`, and
`INTRADAY_MONITOR_REJECTION_RANGE_PCT`.

Audit the latest saved recommendations:

```powershell
intraday-scan audit-latest ^
  --db-path data\scanner.sqlite ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\latest_audit ^
  --persist
```

Build historical performance:

```powershell
intraday-scan performance-report --db-path data\scanner.sqlite --persist
```

Tune strategy parameters on fixtures:

```powershell
intraday-scan tune-strategy ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\tuning
```

Test notifications:

```powershell
intraday-scan notify-test
```

Print the local schedule:

```powershell
intraday-scan scheduler
```

See `docs\PROVIDER_SETUP.md`, `docs\HISTORICAL_AUDIT.md`,
`docs\SCHEDULER_WINDOWS.md`, `docs\TUNING.md`, and
`docs\SECRETS_REQUIRED.md` for production setup details.
