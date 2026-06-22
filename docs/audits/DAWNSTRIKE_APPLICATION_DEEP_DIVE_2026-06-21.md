# Dawnstrike Application Deep Dive - 2026-06-21

## 1. Executive Summary

Dawnstrike is a local research/watchlist application for aggressive intraday
stock setups. It collects premarket mover data, normalizes and scores
candidates, applies AlphaOps risk/no-trade logic, sends Telegram watchlist or
no-clean-edge messages, persists evidence to SQLite, displays the result in
Streamlit, and learns only from imported/audited outcome data.

It does not place broker orders, execute trades, store broker trading
credentials, or guarantee returns.

Current live-state verification found:

- Source doctor status: `complete`
- Candidate count from current source doctor run: `110`
- Source confidence: `85.0`
- Enabled candidate sources: local inbox, StockAnalysis, TradingView
- Latest AlphaOps signal count: `20`
- Real audited outcome days: `0`
- Evidence status: not enough evidence
- Order execution enabled: `false`
- Dashboard HTTP status: `200`

## 2. Plain-English Overview

Every morning, Dawnstrike tries to answer one question: "Are there any clean,
high-volatility names worth watching manually?" If yes, it sends a short
Telegram watchlist and shows the same information in the dashboard. If no, it
sends no-clean-edge instead of forcing a bad pick.

Dawnstrike is not a trading bot. It gives research context, levels, risk flags,
and outcome tracking. You still decide manually what to do at your broker.

## 3. What Is Automated

Automated today:

- Weekday scheduled AlphaOps morning scan.
- Public/manual source collection.
- Canonical row normalization.
- Signal Engine v3 scoring.
- AlphaOps v4 feature generation and scoring.
- Risk governor and no-trade review.
- Telegram notification dispatch.
- SQLite persistence.
- 5-minute scheduled AlphaOps monitor command.
- End-of-day AlphaOps report command.
- Dashboard loading from SQLite.
- Outcome reminder display.

## 4. What Is Manual

Still manual:

- Broker decisions.
- Broker order entry.
- Verifying public/free source data.
- Adding screener CSV fallback files when public sources fail.
- Capturing outcome prices unless a future price feed is added.
- Importing/auditing outcome CSVs.
- Interpreting whether the evidence is good enough for real-money use.

## 5. Daily Timeline

From `scripts/register_alphaops_tasks.ps1`:

| Time | Task | Command |
| --- | --- | --- |
| 8:10 AM CT | `Dawnstrike AlphaOps Morning` | `alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram` |
| 8:35 AM CT | `Dawnstrike AlphaOps Monitor 5m` | `alpha-monitor --db-path data\shadow_real.sqlite --notify telegram`, repeated every 5 minutes for 6 hours |
| 3:15 PM CT | `Dawnstrike AlphaOps EOD Report` | `alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report` |

## 6. Data Sources

Configured active candidate sources:

- `local_inbox`: `data\inbox\screener`
- `stockanalysis_premarket`: public table URL
- `tradingview_premarket`: public table URL

Configured active universe source:

- `nasdaq_symbols`: universe only, not a pick source

Configured but disabled sources:

- TradingView browser-rendered fallback
- MarketWatch movers
- Investing.com premarket
- Barchart public/browser sources
- Nasdaq halt RSS
- SEC EDGAR enrichment

Public/free web data is unverified shadow data. It is useful, but it can be
stale, incomplete, blocked, or wrong.

## 7. Source Reliability

Source doctor verification result:

- Status: `complete`
- Candidate count: `110`
- Rows extracted: `120`
- Rows normalized: `110`
- Rows rejected: `0`
- Source confidence: `85.0`
- Stale status: `fresh`
- Browser extractor: available
- Local inbox: empty
- StockAnalysis: success, 10 rows normalized
- TradingView: success, 100 rows normalized

Source reliability is persisted and later updated by outcome labels. Current
AlphaOps report showed three reliability buckets:

- local inbox: reliability score `50.0`
- StockAnalysis: reliability score `50.0`
- TradingView: reliability score `100.0`

These are operating diagnostics, not proof of trading edge.

## 8. Scoring And AlphaOps Logic

Signal Engine v3 scores the snapshot first. It weighs:

- gap curve
- liquidity thrust
- float rotation
- range control
- squeeze/catalyst
- execution quality
- data quality
- risk penalty

AlphaOps v4 then scores candidates with:

- base scanner score
- explosive score
- catalyst score
- execution score
- expected edge score
- source reliability adjustment
- risk score

AlphaOps is rule-first. Offline ML only activates if there are enough dated
outcome rows and a date-split test beats the rule baseline. Current state has
insufficient ML data.

## 9. No-Trade Logic

No-trade means Dawnstrike refused to force a weak watchlist. This can happen
when:

- sources returned no usable rows
- source status is failed/empty/no data
- every candidate is blocked by hard avoid rules
- source confidence is too low
- top candidate risk score is too weak
- Alpha score is below alert threshold
- drawdown risk is high

No-trade is a feature, not a bug.

## 10. Telegram Flow

Telegram events are created as `NotificationEvent` records and dispatched
through configured notifiers. Telegram formatting is in
`intraday_scanner/notifiers/telegram_formatter.py`.

Primary message types:

- `Dawnstrike Alpha Watch`: watchlist exists.
- `Dawnstrike Alpha Check`: no clean edge.
- `Dawnstrike Alert`: manual review alert.
- `Outcome Data Needed`: outcome CSV missing.
- `Dawnstrike Shadow Results`: outcome/evidence summary.

Telegram messages are watchlist/status alerts, not orders. Tokens and chat IDs
are not printed in the docs or final report.

## 11. Dashboard Guide

The simplified Streamlit dashboard tabs are:

- `Today`
- `Picks`
- `Calendar`
- `Performance`
- `System`

`Today` is the operating screen. It shows status, main pick, top three
watchlist, next steps, risk summary, and no-orders footer.

`Picks` shows readable watchlist and avoid tables.

`Calendar` shows daily status and missing outcomes.

`Performance` answers whether there is enough evidence.

`System` contains technical/admin controls and diagnostics.

## 12. Outcome/Audit Workflow

Outcome CSV path:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

Required columns:

```text
date,ticker,entry_time,entry_price,price_1m,price_5m,price_15m,lunch_price,close_price,high_after_entry,low_after_entry,halted,source,notes
```

Commands:

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
py -m intraday_scanner.cli audit-manual-outcomes --db-path data\shadow_real.sqlite --out-dir outputs\manual_audit --persist
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

Missing outcomes remain pending. They are not counted as zero.

## 13. Learning Loop

Learning source code:

- `intraday_scanner/services/learning_service.py`
- `intraday_scanner/alpha/outcome_labeler.py`
- `intraday_scanner/alpha/setup_memory.py`
- `intraday_scanner/alpha/performance_truth.py`

Learning steps:

1. Load AlphaOps signals.
2. Load manual outcomes.
3. Match outcomes to signals.
4. Create outcome labels.
5. Persist setup memory.
6. Update source reliability from outcomes.
7. Build truth report.
8. Persist AlphaOps learning run.

Current verification shows `0` outcome labels and `0` real audited days.

## 14. Performance Evidence Status

Current `alpha-status` and `alpha-report` results:

- `real_days_collected`: `0`
- `enough_evidence`: `false`
- `outcome_label_count`: `0`
- `setup_memory_count`: `0`
- truth report warning: fewer than 20 real days
- strong evidence: false
- can claim success: false

The system cannot claim profitability from the current evidence.

## 15. Scheduled Tasks

Task query results:

- `Dawnstrike AlphaOps Morning`: Enabled, Ready, next run 2026-06-22 8:10 AM,
  last result `0`.
- `Dawnstrike AlphaOps Monitor 5m`: Enabled, Ready, next run 2026-06-22
  8:35 AM, repeats every 5 minutes for 6 hours, last result `267011`.
- `Dawnstrike AlphaOps EOD Report`: Enabled, Ready, next run 2026-06-22
  3:15 PM, last result `267011`.

The `267011` values appeared on Monitor and EOD because they have not yet run
under the current schedule state.

Older tasks are present but disabled:

- `Dawnstrike Daily Scan`
- `Dawnstrike Setup Monitor 5m`
- `Dawnstrike Web Telegram AutoPilot`

## 16. Logs And Outputs

Important logs:

- `logs\alpha_morning.log`
- `logs\alpha_monitor.log`
- `logs\alpha_report.log`
- `logs\web_telegram_YYYY-MM-DD.log`
- `logs\automation_YYYY-MM-DD.log`

Important outputs:

- `outputs\alpha_cycle`
- `outputs\alpha_report`
- `outputs\source_doctor`
- `outputs\manual_audit`
- `outputs\calendar_report`
- `outputs\web_telegram`
- `outputs\automation`

Logs and outputs are ignored by Git.

## 17. How To Troubleshoot

First checks:

```powershell
schtasks /query | findstr /I "Dawnstrike AlphaOps"
schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST
notepad logs\alpha_morning.log
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

Common fixes:

- No Telegram: check task log, run telegram dry-run, verify secrets in env.
- No source data: use source doctor, then local CSV fallback.
- Stale dashboard: refresh or restart Streamlit and confirm DB path.
- Outcome reminders: import outcome CSV, audit, learn, report.
- Insufficient sample: keep collecting real audited days.

See `docs/TROUBLESHOOTING.md` for the full table.

## 18. How To Improve The System

Best staged path:

1. Keep current free automation stable.
2. Collect at least 20 real audited market days.
3. Add a free read-only price/data API if possible.
4. Add paid Level 1 data when reliability matters.
5. Add better float, short interest, SEC, halt, and news enrichment.
6. Tune the model only from audited real outcomes.
7. Keep broker workflow manual unless a separate safety design is approved.

## 19. Current Blockers

Current practical blockers:

- `0` real audited outcome days.
- `0` AlphaOps outcome labels.
- Manual outcome workflow is still required.
- Public/free sources are unverified.
- Local inbox is empty.
- Enrichment sources for halt/SEC are disabled.
- Paid/live current-price monitoring is not wired into the active monitor.

## 20. What Requires Paid Data

Paid or higher-quality data would improve:

- reliable premarket high/low/volume/spread
- current price monitoring
- automated outcome capture
- float and short interest coverage
- news/catalyst quality
- halt/offering/SEC risk coverage
- reduced source conflict

Dawnstrike can operate in free shadow mode without paid data, but paid data is
the path to better reliability.

## 21. No-Trading Safety Proof

Command run:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts tests docs README.md
```

Broad search result:

- Matches are safety tests and documentation/audit text that prohibit order
  execution or record prior safety searches.

Implementation-only command:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts
```

Implementation-only result:

- No matches.

Conclusion:

- No broker order API, trading client, market/limit order helper, auto-trading
  path, buy/sell recommendation path, or order submission implementation exists.

## 22. Exact Commands

Verification commands run:

```powershell
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall intraday_scanner app.py tests
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
schtasks /query | findstr /I "Dawnstrike AlphaOps"
schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST
schtasks /Query /TN "Dawnstrike AlphaOps Monitor 5m" /V /FO LIST
schtasks /Query /TN "Dawnstrike AlphaOps EOD Report" /V /FO LIST
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8502/
```

Results:

- Editable install: PASS
- Pytest: PASS, `164 passed`
- Ruff: PASS
- Mypy: PASS, `Success: no issues found in 91 source files`
- Compileall: PASS
- Source doctor: PASS, status `complete`
- Alpha status: PASS, status `ok`
- Alpha report: PASS, wrote `outputs\alpha_report`
- Scheduled task query: PASS
- Dashboard HTTP check: PASS, `200`
- No-trading safety search: PASS, no implementation matches

## Source Inspection Coverage

Inspected:

- `README.md`
- `app.py`
- `.gitignore`
- `config\web_sources.example.yaml`
- `config\automation.example.yaml`
- redacted shape of `config\web_sources.yaml`
- redacted shape of `config\automation.yaml`
- `scripts\register_alphaops_tasks.ps1`
- `scripts\run_web_telegram_once.bat`
- `scripts\run_web_telegram_daemon.bat`
- `intraday_scanner\cli.py`
- `intraday_scanner\alpha\*`
- `intraday_scanner\services\*`
- `intraday_scanner\providers\*`
- `intraday_scanner\notifiers\*`
- `intraday_scanner\dashboard\*`
- `intraday_scanner\storage\sqlite_store.py`
- `docs\*`
- `tests\*`
- `templates\*`

No secrets were included in this report.
