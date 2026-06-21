# Notification-Only Workflow

Use this when you want Dawnstrike to run locally and only tell you what happened.

## Morning

1. Export your allowed screener CSV or copied table.
2. Save it under `data\inbox\screener`.
3. Run the daemon or scheduled task.
4. Receive:
   - morning scan started
   - data source succeeded or failed
   - top picks
   - avoid warnings

The official call timestamp is saved before any outcome file is accepted.

## Monitor

At market open, Dawnstrike checks saved official calls only when a reliable
current-price source is available. Without one, it sends a manual monitor
required notification.

Monitor alerts are research controls:

- `WATCH`
- `CAUTION`
- `THESIS BROKEN`
- `INVALIDATED`

## Lunch And Close

If no outcome file exists, Dawnstrike sends a reminder with:

- ticker list
- required CSV columns
- exact save path
- command the automation uses after the file appears

Save outcomes to:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

## End Of Day

When outcomes are present, Dawnstrike imports them, audits them, writes the Free
Shadow report, archives the file, and sends a summary. If outcomes are missing,
the daily summary says so directly.

## Dashboard

The dashboard is optional. It shows latest automation status, latest source,
latest notification, top picks, monitor events, missing outcomes, output paths,
logs path, and a health checklist.

## Web + Telegram Auto-Pilot

To collect configured local/free web sources, run the scan, and send
Telegram-ready research messages:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

Use `--notify console --dry-run --max-cycles 1` first if Telegram secrets are
not configured. Missing source data, blocked tables, missing outcomes, and
missing live price feeds are reported as notifications instead of being hidden.
