# Dawnstrike Web Auto-Pilot

Dawnstrike Web Auto-Pilot is a notification-only research workflow. It reads
allowed local/free public sources, normalizes premarket candidates, enriches
them with SEC/halt risk when enabled, runs the scanner, and sends compact
Telegram-ready messages.

It does not place orders, submit trades, store broker trading credentials,
bypass logins, solve CAPTCHAs, bypass paywalls, or fabricate missing market
data.

## Enable One Candidate Source

Copy the example config:

```powershell
Copy-Item config\web_sources.example.yaml config\web_sources.yaml
```

At least one candidate source is required for picks:

- `local_inbox`
- `public_table_url`
- `browser_table_url`

The safest source is `local_inbox`, enabled by default:

```text
data\inbox\screener
```

Drop a screener CSV into that folder before running the daemon.

`nasdaq_symbols` is universe-only. It helps build a symbol universe, but it does
not generate premarket mover picks by itself. SEC and halt sources are
enrichment-only and also do not create picks.

## Source Doctor

Run this when no picks appear:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

The doctor lists enabled sources, classifies each as candidate, universe, or
enrichment, checks whether the local inbox has files, safely tests enabled
public table sources, and writes:

```text
outputs\source_doctor\source_doctor.json
```

If Barchart returns `no_candidate_table`, the page likely did not expose a
static public table. Use a local CSV, enable another allowed candidate source,
or install the optional browser extractor.

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

SQLite persists fetch runs, source health, raw artifacts, normalized rows, scan
runs, and notification attempts.
