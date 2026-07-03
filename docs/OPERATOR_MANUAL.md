# Dawnstrike Operator Manual

## Safety Boundary

Dawnstrike is research/watchlist software. It does not place broker orders,
execute trades, store broker trading credentials, or guarantee returns. You make
all broker decisions manually outside the app.

## A. Fully Scheduled Mode

### What Windows Task Scheduler Runs

The active task registration script is `scripts/register_alphaops_tasks.ps1`.
It creates three weekday tasks:

| Task | Time | Command | Log |
| --- | --- | --- | --- |
| `Dawnstrike AlphaOps Morning` | 8:10 AM CT | `py -m intraday_scanner.cli alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram` | `logs\alpha_morning.log` |
| `Dawnstrike AlphaOps Monitor 5m` | 8:35 AM CT, every 5 minutes for 6 hours | `py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify telegram` | `logs\alpha_monitor.log` |
| `Dawnstrike AlphaOps EOD Report` | 3:15 PM CT | `py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report && py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist && py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report` | `logs\alpha_report.log` |

If `schtasks /Query /TN "Dawnstrike AlphaOps EOD Report" /V /FO LIST` still
shows only `alpha-report`, re-register tasks with:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_alphaops_tasks.ps1
```

### What To Expect In Telegram

Morning:

- `Dawnstrike Alpha Watch` if clean watchlist names are available.
- `Dawnstrike Alpha Check` if no clean edge is available.

During the day:

- `Dawnstrike Alpha Monitor` messages. If no live/current price source is
  configured, the monitor can say manual review is required.

End of day:

- The scheduled report writes files. Telegram summary behavior depends on the
  command path and notification settings.

### Where Logs Live

- `logs\alpha_morning.log`
- `logs\alpha_monitor.log`
- `logs\alpha_report.log`
- `logs\web_telegram_YYYY-MM-DD.log` for the older web Telegram daemon path
- `logs\automation_YYYY-MM-DD.log` for automation service runs

### Where Outputs Live

- `outputs\alpha_cycle`
- `outputs\alpha_report`
- `outputs\source_doctor`
- `outputs\manual_audit`
- `outputs\calendar_report`
- `outputs\web_telegram`
- `outputs\automation`

These output folders are local artifacts and are ignored by Git.

### If No Telegram Message Arrives

1. Check scheduled tasks:

```powershell
schtasks /query | findstr /I "Dawnstrike AlphaOps"
schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST
```

2. Check logs:

```powershell
notepad logs\alpha_morning.log
```

3. Check AlphaOps state:

```powershell
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
```

4. Test Telegram without sending a real message:

```powershell
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
```

5. Run the source doctor:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

## B. Manual One-Off Mode

Use these commands when you want to run the workflow yourself.

### Source Doctor

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

Purpose: checks whether the configured public/manual sources are returning
usable rows.

### AlphaOps Morning Cycle

```powershell
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram
```

Purpose: collects rows, scores them, persists scan/features/signals, and sends a
watchlist or no-clean-edge Telegram message.

### AlphaOps Status

```powershell
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
```

Purpose: shows latest scan ID, signal count, feature count, outcome labels,
source reliability, setup memory, evidence days, and research-only status.

### AlphaOps Report

```powershell
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

Purpose: writes `alpha_report.json` and `alpha_report.md` based on saved outcome
labels and learning state.

## C. Outcome Workflow

### Where Outcome CSV Goes

Put the file here:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

Use `templates\manual_outcomes_template.csv` as the model.

### Required Columns

The source code requires these columns:

```text
date,ticker,entry_time,entry_price,price_1m,price_5m,price_15m,lunch_price,close_price,high_after_entry,low_after_entry,halted,source,notes
```

Blank timed prices are allowed when you truly do not have the value. Missing
returns remain unavailable; they are not counted as zero.

### Import Outcomes

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
```

### Audit Outcomes

```powershell
py -m intraday_scanner.cli audit-manual-outcomes --db-path data\shadow_real.sqlite --out-dir outputs\manual_audit --persist
```

This creates audited rows such as 1-minute return, 5-minute return, 15-minute
return, lunch return, close return, high-after-entry return, and low-after-entry
drawdown.

### Learn From Outcomes

```powershell
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
```

This creates AlphaOps outcome labels, setup memory, source reliability updates,
and a truth report.

### Attribute Historical Returns

```powershell
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
```

This calculates paper/scenario returns from the saved historical signal ledger
and imported outcomes. It does not count missing outcomes as zero.

### Historical Report

```powershell
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
```

This writes the historical ledger summary, accuracy buckets, missing-outcome
status, and evidence label.

### Report Results

```powershell
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

## D. Dashboard

Start the dashboard:

```powershell
py -m streamlit run app.py --server.port 8502
```

Open:

```text
http://127.0.0.1:8502/
```

Look at `Today` first:

1. Status banner: clean watchlist, watch only, no clean edge, outcome needed, or
   source problem.
2. Main pick: the top watchlist name if one exists.
3. Top 3 watchlist: the three highest display-ready names.
4. What to do next: scan, data, Telegram, manual watch levels, and outcome
   reminders.
5. Risk summary: avoid count, top avoid reason, data warnings, missing outcomes.

Then use:

- `Picks` for readable tables and latest notifications.
- `Calendar` for historical daily outcomes and missing outcome status.
- `Performance` for evidence status and audited performance summaries.
- `System` for run controls, source health, logs, and advanced diagnostics.
