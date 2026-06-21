# Dawnstrike Web Auto-Pilot

Dawnstrike Web Auto-Pilot is a notification-only research workflow. It can read
allowed local/free public sources, normalize premarket candidates, enrich them
with SEC/halt risk where available, run the scanner, and send Telegram-ready
research messages.

It does not place orders, store broker trading credentials, bypass logins,
solve CAPTCHAs, bypass paywalls, or fabricate missing market data.

## Configure Sources

Start from:

```powershell
Copy-Item config\web_sources.example.yaml config\web_sources.yaml
```

The safest zero-dollar source is still `local_inbox`:

```text
data\inbox\screener
```

Public table sources are disabled by default. Enable only sources you are
allowed to access, keep `allowed_domains` tight, and expect failures when a
page blocks automation or changes table structure.

## Commands

Build the free universe:

```powershell
py -m intraday_scanner.cli web-build-universe --config config\web_sources.yaml --db-path data\shadow_real.sqlite --persist
```

Ingest one allowed public table:

```powershell
py -m intraday_scanner.cli web-ingest-public-table --url https://allowed.example/table --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_ingest\manual --persist --print
```

Collect local/web candidates:

```powershell
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto\today --persist --print
```

Run one notification-only auto-pilot cycle:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

Run continuously:

```powershell
scripts\run_web_telegram_daemon.bat
```

## Outputs

The collector writes:

- `outputs\web_auto\...\premarket_snapshot.csv`
- `outputs\web_auto\...\source_summary.json`
- `outputs\web_auto\...\data_quality_report.json`
- raw artifacts when `save_raw: true`

SQLite persists fetch runs, source health, raw artifacts, normalized source
rows, SEC risk events, halt events, AI summaries, scan runs, and notifications.

## Zero-Key Behavior

With no secrets, the local inbox, fixture-style local files, public table
normalization, SEC/halt collection, scan, dashboard, and console notifications
can run. Telegram requires `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.

Paid/live market data is still required for reliable automatic 5-minute current
price monitoring. Without that source, Dawnstrike sends a manual monitor notice.
