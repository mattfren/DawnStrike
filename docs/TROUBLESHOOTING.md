# Troubleshooting

Production task definitions are immutable exact-SHA inputs. Preserve XML,
logs, locks, and journals when diagnosing them; never repair or rerun a
production stage with a checkout registration script. Use
`operations/runtime_activation_and_rollback.md`.

## Fast Checks

```powershell
schtasks /query | findstr /I "Dawnstrike AlphaOps"
schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST
notepad logs\alpha_morning.log
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

## Practical Table

| Problem | Likely cause | Check | Fix |
| --- | --- | --- | --- |
| No Telegram message | Task did not run, secrets missing, dedupe skipped duplicate, or source produced no event | `schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST`; `notepad logs\alpha_morning.log`; `py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite` | Confirm task exists, inspect log, run dry-run, then run real Telegram test after secrets are configured. |
| Telegram token missing | Environment variables are absent | `py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite` | Set `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment; do not commit them. |
| Task Scheduler did not run | Task disabled, principal/credential issue, wrong action identity, or prior stage failure | `schtasks /query | findstr /I "Dawnstrike"`; preserve exact task XML and receipts | Run the read-only scheduler doctor, then use protected activation recovery; do not re-register ad hoc. |
| Source doctor says no data | Public pages changed, blocked, stale, or local inbox empty | `py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print` | Try again during premarket, drop a CSV into `data\inbox\screener`, or enable a working source. |
| StockAnalysis/TradingView changed page shape | HTML table headers changed or table disappeared | Inspect `outputs\source_doctor` and source debug files | Use local CSV fallback, update source mapping, or enable browser extraction if appropriate. |
| Source conflict | Multiple sources disagree on price/gap/volume | Dashboard risk flags or `source_summary.json` conflict flags | Treat the ticker as unverified; do not force a pick. |
| No clean edge | Risk, score, source quality, or evidence did not pass | `alpha-status`; `outputs\alpha_cycle\alpha_cycle.json` | Wait, re-scan, or use manual CSV fallback. No-trade is valid. |
| Dashboard shows stale data | Dashboard is reading old DB/output path or Streamlit needs reload | Check sidebar DB path and latest timestamp | Set DB to `data\shadow_real.sqlite`, refresh, or restart Streamlit. |
| Outcome reminders keep showing | Missing CSV rows for saved picks | Calendar tab; `data\inbox\outcomes` | Add `outcomes_YYYY-MM-DD.csv`, import, audit, and learn. |
| Returns are n/a | Outcome prices are blank, not imported, or attribution has not been run | `outputs\manual_audit\manual_audit_trades.csv`; `outputs\return_attribution\signal_return_attribution.csv` | Fill missing prices where available, rerun import/audit, then run `py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist`. |
| Alpha report says insufficient sample | Fewer than 20 real audited market days | `py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite` | Continue collecting outcomes until at least 20 real days. |
| Scheduled task Last Result not 0 | CLI error, source error, path error, or missing env | `schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST`; durable log and receipt | Preserve the failed evidence and diagnose the exact guarded stage; do not bypass it with a manual production run. |
| Logs missing | Task never ran or `logs` directory was deleted | `Get-ChildItem logs` | Preserve scheduler and filesystem evidence. For production, diagnose the guarded task and follow the protected repair path; do not manually rerun it. Manual commands are for a disposable local diagnostic environment only. |
| Database missing | `data\shadow_real.sqlite` not created yet or wrong path | `Test-Path data\shadow_real.sqlite`; `alpha-status` | Run `alpha-cycle` once or check configured DB path. |
| Browser extractor missing | Playwright/browser extra not installed | `py -m pip install -e ".[browser]"` then `py -m playwright install chromium` | Install optional browser dependencies, then enable browser source only when needed. |
| Public source blocked | Page blocks scripts, bots, login, CAPTCHA, or paywall | Source doctor failure reason | Do not bypass protections; use local CSV or another allowed source. |
| Accidentally stale output folder | You are reading old files in `outputs` | Compare file timestamps | Prefer SQLite dashboard mode or rerun the command with a fresh out-dir. |

## Useful Commands

### Query All Dawnstrike AlphaOps Tasks

```powershell
schtasks /query | findstr /I "Dawnstrike AlphaOps"
```

### Query One Task In Detail

```powershell
schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST
schtasks /Query /TN "Dawnstrike AlphaOps Monitor 5m" /V /FO LIST
schtasks /Query /TN "Dawnstrike AlphaOps EOD Full Report" /V /FO LIST
```

### Open Logs

```powershell
notepad logs\alpha_morning.log
notepad logs\alpha_monitor.log
notepad logs\alpha_report.log
```

### Check AlphaOps State

```powershell
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
```

### Check Sources

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

### Run Manual AlphaOps Cycle

```powershell
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram
```

### Run Dashboard

```powershell
py -m streamlit run app.py --server.port 8502
```
