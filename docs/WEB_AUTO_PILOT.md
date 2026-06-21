# Dawnstrike Web Auto-Pilot

Dawnstrike Web Auto-Pilot is a notification-only research workflow. It reads
allowed local/free public sources, normalizes premarket candidates, enriches
them with SEC/halt risk when enabled, runs the scanner, and sends compact
Telegram-ready messages.

It does not place orders, submit trades, store broker trading credentials,
bypass logins, solve CAPTCHAs, bypass paywalls, or fabricate missing market
data.

## Candidate Source Hierarchy

Copy the example config:

```powershell
Copy-Item config\web_sources.example.yaml config\web_sources.yaml
```

At least one candidate source is required for picks. Dawnstrike tries enabled
candidate sources in this practical order and dedupes by ticker:

1. `local_inbox`
2. `stockanalysis_premarket`
3. `tradingview_premarket`
4. `tradingview_premarket_browser`
5. `marketwatch_movers`
6. `investing_premarket`
7. `barchart_premarket_browser`

The safest source is `local_inbox`, enabled by default:

```text
data\inbox\screener
```

Drop a screener CSV into that folder before running the daemon.

`stockanalysis_premarket` is the preferred public table to try first. TradingView
is enabled as a candidate source and uses source-specific mapping for columns
such as pre-market price, pre-market volume, pre-market gap percent, market cap,
and relative volume. Barchart is optional/browser-only by default because it
often requires login, CAPTCHA, or anti-bot review.

`nasdaq_symbols` is universe-only. It helps build a symbol universe, but it does
not generate premarket mover picks by itself. SEC and halt sources are
enrichment-only and also do not create picks.

## Source Doctor

Run this when no picks appear:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

Run source doctor during actual premarket hours when possible. The doctor lists
enabled sources, classifies each as candidate, universe, or enrichment, checks
whether the local inbox has files, safely tests enabled public table sources,
prints candidate count, source confidence, stale-data status, rejection reasons,
and the next action, then writes:

```text
outputs\source_doctor\source_doctor.json
outputs\source_doctor\extracted_rows.csv
outputs\source_doctor\rejected_rows.csv
outputs\source_doctor\normalization_debug.json
```

If a source returns `no_candidate_table`, `missing_volume`,
`missing_gap_or_previous_close`, or `blocked_or_login_required`, no rows are
fabricated. Use a local CSV, try again during premarket, or enable another
allowed public source.

## Optional Browser Source

Static extraction may fail on JavaScript-rendered pages. Browser extraction is
disabled by default and must be explicitly enabled per source.

Install:

```powershell
py -m pip install -e ".[browser]"
py -m playwright install chromium
```

Browser extraction does not bypass logins, CAPTCHAs, paywalls, anti-bot controls,
or protected sites. Browser rows are labeled:

```text
data_source_kind=browser_url
coverage_warning=browser_rendered_public_table_unverified
```

Public URL data is unverified shadow data.

## Commands

Collect local/web candidates:

```powershell
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto\today --persist --print
```

Run one notification-only cycle:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

Run continuously:

```powershell
scripts\run_web_telegram_daemon.bat
```

## Outputs

- `outputs\web_auto\...\premarket_snapshot.csv`
- `outputs\web_auto\...\source_summary.json`
- `outputs\web_auto\...\data_quality_report.json`
- `outputs\source_doctor\source_doctor.json`
- `outputs\source_doctor\extracted_rows.csv`
- `outputs\source_doctor\rejected_rows.csv`
- `outputs\source_doctor\normalization_debug.json`

SQLite persists fetch runs, source health, raw artifacts, normalized rows, scan
runs, and notification attempts.

Every normalized web row carries source, source URL when available, extraction
mode, source timestamp, extraction timestamp, stale flag, and source confidence.
Duplicate ticker rows are reconciled by quality/source priority and annotated
with source count, preferred source, merge reason, and conflict flags.
