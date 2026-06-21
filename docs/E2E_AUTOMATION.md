# End-To-End Automation

Dawnstrike can run as a zero/low-cost notification-only research workflow. It
does not place trades, store broker trading credentials, bypass websites, or
claim live performance from manual/free data.

## Daily Flow

1. Put a screener export in `data\inbox\screener`, or explicitly configure an
   allowed public URL source.
2. Start the automation daemon or run one automation pass.
3. Receive notifications for source status, top picks, avoid warnings, monitor
   status, outcome reminders, audits, and daily summaries.
4. If outcomes are missing, save the requested CSV to
   `data\inbox\outcomes\outcomes_YYYY-MM-DD.csv`.
5. The next outcome pass imports, audits, reports, archives, and notifies.

## Commands

Run one notification-only pass:

```powershell
py -m intraday_scanner.cli automation-run --mode once --config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
```

Run only the morning scan:

```powershell
py -m intraday_scanner.cli automation-morning --config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
```

Run the market-open monitor:

```powershell
py -m intraday_scanner.cli automation-monitor-open --db-path data\shadow_real.sqlite --out-root outputs\automation --max-iterations 1 --notify
```

If no reliable current-price source is configured, this sends a manual monitor
required notification instead of fabricating prices.

Run outcomes:

```powershell
py -m intraday_scanner.cli automation-outcomes --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
```

Run the daily summary:

```powershell
py -m intraday_scanner.cli automation-summary --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
```

Dry-run the daemon:

```powershell
py -m intraday_scanner.cli automation-daemon --config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\automation --dry-run --max-cycles 1 --notify
```

## Outputs

Morning outputs:

- `outputs\automation\YYYY-MM-DD\morning\premarket_snapshot.csv`
- `outputs\automation\YYYY-MM-DD\morning\ranked_candidates.csv`
- `outputs\automation\YYYY-MM-DD\morning\top_explosive.csv`
- `outputs\automation\YYYY-MM-DD\morning\avoid_list.csv`
- `outputs\automation\YYYY-MM-DD\morning\run_summary.json`

Outcome outputs:

- `outputs\automation\YYYY-MM-DD\outcomes\outcome_summary.json`
- `outputs\automation\YYYY-MM-DD\outcomes\audit\manual_audit_trades.csv`
- `outputs\automation\YYYY-MM-DD\outcomes\audit\manual_audit_summary.json`
- `outputs\automation\YYYY-MM-DD\outcomes\shadow_report\free_shadow_report.json`

Summary output:

- `outputs\automation\YYYY-MM-DD\summary\daily_summary.json`

Logs:

- `logs\automation_YYYY-MM-DD.log`

## Persistence

Automation persists:

- official scan calls in `scan_runs`, `ranked_candidates`, `top_explosive`, and
  `avoid_list`
- recommendation theses before outcomes are known
- notifications in `notifications_sent`
- monitor warnings in `monitor_events` and `alerts_sent`
- automation status in `automation_runs`
- outcomes, audits, and shadow reports when outcome files are present

## Zero-Key Behavior

With no secrets, console notifications work. Discord, Telegram, email, and
Windows local notifications are optional. Missing external notification settings
are recorded as health rows and do not block console notification flow.

## Web Auto-Pilot Layer

The next automation layer can collect from configured safe web/free sources and
send Telegram-ready messages:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

It still follows the same research-only constraints: no broker order execution,
no hidden credentials, no bypassing restricted websites, no fake data, and no
lookahead outcomes. If no current-price source is configured, it sends a manual
monitor notice instead of pretending to perform five-minute checks.

## Safety

The automation:

- prefers local CSV exports
- labels manual/free data clearly
- does not call paid/live providers unless explicitly configured
- does not scrape protected pages
- does not bypass login, CAPTCHA, paywall, anti-bot, or site restrictions
- does not place orders or connect to broker trading APIs
