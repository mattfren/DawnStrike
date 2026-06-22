# Dawnstrike Application Deep Dive - 2026-06-22

## 1. Executive Summary

Dawnstrike is a local research and watchlist system for aggressive intraday
stock setups. It collects premarket mover data, normalizes public/manual rows,
scores candidates, applies AlphaOps risk and no-trade logic, sends Telegram
watchlist or no-clean-edge messages, persists the evidence in SQLite, displays
the current state in Streamlit, and learns only from imported/audited outcome
data.

Dawnstrike does not place broker orders, execute trades, store broker trading
credentials, or guarantee returns.

Current verification found:

- Source doctor status: `complete`
- Candidate count from current source doctor run: `110`
- Rows extracted: `120`
- Rows normalized: `110`
- Rows rejected: `0`
- Source confidence: `85.0`
- Enabled candidate sources: local inbox, StockAnalysis, TradingView
- Latest AlphaOps signal count: `20`
- Feature vector count: `27`
- Real audited outcome days: `0`
- Outcome labels: `0`
- Evidence status: insufficient sample / not enough history yet
- Order execution enabled: `false`
- Dashboard HTTP status: `200`

## 2. Plain-English Overview

Every morning, Dawnstrike asks: "Are there any clean, high-volatility names
worth watching manually?" If yes, it saves the names, sends a Telegram watchlist,
and shows the same plain-English state in the dashboard. If no, it sends a
no-clean-edge result instead of forcing a weak pick.

The app is not a trading bot. It gives watch levels, risk flags, data quality
context, and outcome tracking. The operator still decides manually what to do at
the broker outside Dawnstrike.

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
- Five-minute scheduled AlphaOps monitor command.
- End-of-day AlphaOps report command.
- Return-attribution and historical-report commands in the repo task script.
- Dashboard loading from SQLite.
- Outcome reminder display.

## 4. What Is Manual

Still manual:

- Broker decisions.
- Broker order entry.
- Verifying public/free source data.
- Adding screener CSV fallback files when public sources fail.
- Capturing outcome prices unless a future read-only price feed is added.
- Importing/auditing outcome CSVs.
- Judging whether evidence is strong enough for real-money use.

## 5. Daily Timeline

The intended schedule is defined in `scripts/register_alphaops_tasks.ps1`:

| Time | Task | Script command |
| --- | --- | --- |
| 8:10 AM CT | `Dawnstrike AlphaOps Morning` | `alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify telegram` |
| 8:35 AM CT | `Dawnstrike AlphaOps Monitor 5m` | `alpha-monitor --db-path data\shadow_real.sqlite --notify telegram`, repeated every 5 minutes for 6 hours |
| 3:15 PM CT | `Dawnstrike AlphaOps EOD Report` | `alpha-report`, then `attribute-returns --persist`, then `historical-report` |

Important current-state finding: the registered Windows EOD task still shows the
older `alpha-report`-only command. Re-run
`powershell -ExecutionPolicy Bypass -File scripts\register_alphaops_tasks.ps1`
to make Task Scheduler match the current repo script.

## 6. Data Sources

Current configured candidate sources:

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

Public/free web data is unverified shadow data. It can be stale, incomplete,
blocked, changed by the website, or wrong. The latest live state still has
missing float, missing previous close, missing enrichment, and unverified public
URL warnings on AlphaOps data.

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
- StockAnalysis: success, `10` rows normalized
- TradingView: success, `100` rows normalized

Source reliability is later updated from real outcome labels. Current AlphaOps
report source reliability:

- local inbox: `50.0`
- StockAnalysis: `50.0`
- TradingView: `100.0`

These are operating diagnostics, not proof of trading edge.

## 8. Scoring And AlphaOps Logic

Signal Engine v3 scores the normalized snapshot first. It weighs:

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

The model is rule-first. Offline ML only activates if enough dated outcome rows
exist and date-split evaluation beats the rule baseline. Current state has
insufficient ML/evidence data.

## 9. No-Trade Logic

No-trade means Dawnstrike refused to force a weak watchlist. This can happen
when:

- sources returned no usable rows
- source status is failed, empty, or no data
- every candidate is blocked by hard avoid rules
- source confidence is too low
- top candidate risk score is too weak
- Alpha score is below alert threshold
- drawdown risk is high

No-trade is an intended safety feature.

## 10. Telegram Flow

Telegram events are created as `NotificationEvent` records and dispatched
through configured notifiers. Formatting lives in
`intraday_scanner/notifiers/telegram_formatter.py`.

Primary message types:

- `Dawnstrike Alpha Watch`: a watchlist exists.
- `Dawnstrike Alpha Check`: no clean edge.
- `Dawnstrike Alert`: manual review alert.
- `Outcome Data Needed`: outcome CSV is missing.
- `Dawnstrike Shadow Results`: outcome/evidence summary.
- `Dawnstrike Accuracy`: historical signal ledger attribution summary.

Telegram messages are watchlist/status alerts, not orders. Tokens and chat IDs
are not printed in this report.

## 11. Dashboard Guide

The simplified Streamlit dashboard tabs are:

- `Today`
- `Picks`
- `Calendar`
- `Performance`
- `System`

`Today` is the operating screen. It shows the current status, main pick, top
three watchlist names, next steps, risk summary, and research-only safety text.

`Picks` shows readable watchlist and avoid tables.

`Calendar` shows daily status, missing outcomes, and scenario return
accountability.

`Performance` answers whether there is enough audited evidence.

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
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

Missing outcomes remain pending. They are not counted as zero.

## 13. Learning Loop

Learning source code:

- `intraday_scanner/services/learning_service.py`
- `intraday_scanner/services/return_attribution_service.py`
- `intraday_scanner/alpha/outcome_labeler.py`
- `intraday_scanner/alpha/setup_memory.py`
- `intraday_scanner/alpha/performance_truth.py`

Learning steps:

1. Load AlphaOps signals and historical signals.
2. Load imported outcomes.
3. Match outcomes to saved point-in-time signals.
4. Create outcome labels and return-attribution rows.
5. Persist setup memory and source reliability.
6. Build truth and historical reports.
7. Keep missing outcomes pending.

Current verification shows `0` outcome labels and `0` real audited days.

## 14. Performance Evidence Status

Current `alpha-status`, `alpha-report`, and `historical-report` results:

- `real_days_collected`: `0`
- `audited_day_count`: `0`
- `enough_evidence`: `false`
- `outcome_label_count`: `0`
- `historical outcome_count`: `0`
- `historical attribution_count`: `0`
- truth report warning: fewer than 20 real days
- strong evidence: false
- can claim success: false

The system cannot claim profitability from the current evidence.

## 15. Scheduled Tasks

Task query results:

- `Dawnstrike AlphaOps Morning`: Enabled, Ready, next run
  `6/22/2026 8:10:00 AM`, last result `0`.
- `Dawnstrike AlphaOps Monitor 5m`: Enabled, Ready, next run
  `6/22/2026 8:35:00 AM`, repeats every 5 minutes for 6 hours, last result
  `267011`.
- `Dawnstrike AlphaOps EOD Report`: Enabled, Ready, next run
  `6/22/2026 3:15:00 PM`, last result `267011`.

Current registered task commands:

- Morning: `alpha-cycle ... --notify telegram`
- Monitor: `alpha-monitor ... --notify telegram`
- EOD: `alpha-report ... --out-dir outputs\alpha_report`

The repo script now defines a broader EOD command with `attribute-returns` and
`historical-report`, but the registered Windows EOD task has not been updated
yet.

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
- `outputs\return_attribution`
- `outputs\historical_report`
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

- No Telegram: check task log, run Telegram dry-run, verify secrets in env.
- No source data: use source doctor, then local CSV fallback.
- EOD task is old: re-register `scripts\register_alphaops_tasks.ps1`.
- Stale dashboard: refresh or restart Streamlit and confirm DB path.
- Outcome reminders: import outcome CSV, audit, learn, attribute, report.
- Insufficient sample: keep collecting real audited days.

See `docs/TROUBLESHOOTING.md` for the full table.

## 18. How To Improve The System

Best staged path:

1. Keep current free automation stable.
2. Re-register scheduled tasks so active EOD matches the current script.
3. Collect at least 20 real audited market days.
4. Add a free read-only price/data API if possible.
5. Add paid Level 1 data when reliability matters.
6. Add better float, short interest, SEC, halt, and news enrichment.
7. Tune the model only from audited real outcomes.
8. Keep broker workflow manual unless a separate safety design is approved.

## 19. Current Blockers

Current practical blockers:

- `0` real audited outcome days.
- `0` AlphaOps outcome labels.
- Manual outcome workflow is still required.
- Public/free sources are unverified.
- Local inbox is empty.
- Enrichment sources for halt/SEC are disabled.
- Paid/live current-price monitoring is not wired into the active monitor.
- Active EOD scheduled task is still `alpha-report` only and should be
  re-registered to match the repo script.
- Latest public-source AlphaOps rows still show missing float, missing previous
  close, low source count, and unverified public URL warnings.

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

Implementation-only command:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts
```

Implementation-only result:

- Exit code `1`
- No matches

Broad command:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts tests docs README.md
```

Broad result:

- Matches are documentation/audit files and tests that prohibit order execution
  or verify no order execution exists.

Conclusion:

- No implementation path for broker order APIs, trading clients, market/limit
  orders, auto-trading, buy/sell recommendations, or order submission was found.

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
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
schtasks /query | findstr /I "Dawnstrike AlphaOps"
schtasks /Query /TN "Dawnstrike AlphaOps Morning" /V /FO LIST
schtasks /Query /TN "Dawnstrike AlphaOps Monitor 5m" /V /FO LIST
schtasks /Query /TN "Dawnstrike AlphaOps EOD Report" /V /FO LIST
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8502/
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts tests docs README.md
git diff --check
```

Results:

- Editable install: PASS
- Pytest: PASS, `170 passed`
- Ruff: PASS
- Mypy: PASS, `Success: no issues found in 92 source files`
- Compileall: PASS
- Source doctor: PASS, status `complete`, `110` normalized candidates
- Alpha status: PASS, status `ok`, `0` real audited days
- Alpha report: PASS, wrote `outputs\alpha_report`
- Attribute returns: PASS, status `complete`, `0` attributed returns because
  no historical outcomes are imported
- Historical report: PASS, status `complete`, evidence `Not enough history yet.`
- Scheduled task query: PASS
- Dashboard HTTP check: PASS, `200`
- No-trading implementation search: PASS, no implementation matches
- `git diff --check`: PASS with line-ending warnings only

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
