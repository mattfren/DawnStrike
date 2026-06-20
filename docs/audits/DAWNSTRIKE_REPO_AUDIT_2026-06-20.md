# Dawnstrike Repo Audit - 2026-06-20

Audit mode: source-level, audit-only. No product features, refactors, or order-execution paths were added.

Status language:
- PASS: verified directly by source inspection and/or command output.
- PARTIAL: implemented but incomplete, stubbed, unproven, or not fully wired.
- BLOCKED: requires secrets, live market hours, external services, or unavailable runtime inputs.
- FAIL: verified broken.
- UNKNOWN: not enough evidence.

## 1. Executive Summary

Dawnstrike is a real Python/Streamlit intraday research scanner. It has a working offline CSV workflow, a scoring formula, ranked and avoid lists, SQLite persistence, a paper-audit path, a 5-minute/1-minute monitor loop, alert generation, deduped notifications, a Streamlit dashboard, tests, and docs.

It is not production-complete for live paper trading. The sample workflow passes, but true live operation remains PARTIAL/BLOCKED because live Alpaca runs require secrets and market data, live scans require a supplied symbol list, full-market mover discovery is not implemented, live-provider behavior was not proven against real markets, scheduler logic is not market-calendar/retry robust, and return claims are fixture/sample only.

It is not ready for serious real-money consideration. There is no hidden auto-trading path, which is good, but the return model needs much larger historical validation, point-in-time live data collection, operational monitoring, failure recovery, and broker-independent risk controls before being trusted.

## 2. Overall Readiness Grade

| Area | Grade | Status | Evidence |
|---|---:|---|---|
| Offline/sample research workflow | B+ | PASS | Full sample workflow completed and persisted outputs. |
| Source quality under bundled runtime | A- | PASS | `pytest`, `ruff`, and `mypy` pass under bundled runtime. |
| Windows default operability | C | PARTIAL/FAIL | Console scripts are installed but not on default PATH; system Python lacks dev tools. |
| Live paper-trading readiness | C- | PARTIAL/BLOCKED | Read-only Alpaca adapter exists, but live secrets/market run not verified and no full-market discovery. |
| Real-money readiness | D | FAIL/PARTIAL | No execution path exists, and model is not validated enough for capital decisions. |

Overall: PARTIAL. This is a usable local research prototype with good foundations, not a production trading platform.

## 3. Capability Matrix

| Capability | Status | Notes |
|---|---|---|
| Canonical CSV snapshot scan | PASS | `CsvSnapshotProvider` plus schema validation and ranked output. |
| SQLite persistence | PASS | Expected scanner, audit, alert, monitor, performance, and health tables exist. |
| Scoring and avoid list | PASS | Explainable formula with risk penalties and avoid reasons. |
| Expected return/confidence model | PARTIAL | Implemented and sample-size capped; not statistically proven on enough history. |
| Paper audit/backtest | PARTIAL | Calculates post-call returns, slippage, drawdown, exits; entry is first eligible open, not breakout fill. |
| 5-minute/1-minute monitor | PARTIAL | Loop exists; true live data depends on Alpaca and saved picks. |
| Alerts | PARTIAL | Monitor/news/SEC alerts exist and dedupe; not every risk flag becomes a distinct event. |
| Notifications | PARTIAL/BLOCKED | Console works; email/Discord/Telegram require secrets and network. |
| Live Alpaca market data | BLOCKED/PARTIAL | Adapter is read-only; secrets absent; requires static symbol list. |
| News feeds | BLOCKED/PARTIAL | NewsAPI and Finnhub implemented; Benzinga key exists but no provider. |
| SEC filing risk | PARTIAL | SEC RSS parser exists; generic User-Agent should be replaced for production. |
| Full-market premarket movers | FAIL/PARTIAL | No real full-market discovery; symbols must be supplied. |
| Dashboard | PASS/PARTIAL | AppTest passes; reads SQLite/output/sample data, but browser visual QA was not re-run in this audit. |
| Scheduler | PARTIAL | Static schedule and PowerShell helpers exist; no holiday calendar, retry policy, or durable runner. |
| Auto trading/order execution | PASS | No order placement path found. |

## 4. Command Evidence Table

| Command | Result | Status | Notes |
|---|---|---|---|
| `py -m pip install -e .` | Installed package | PASS/PARTIAL | Installs app, but warns scripts land in Python313 Scripts path that is not on PATH. |
| `py -m pytest -p no:cacheprovider` | `No module named pytest` | FAIL | System Python lacks dev extras. |
| bundled `python -m pytest -p no:cacheprovider` | `41 passed in 5.31s` | PASS | Verified during audit. |
| `py -m ruff check .` | `No module named ruff` | FAIL | System Python lacks dev extras. |
| bundled `python -m ruff check .` | `All checks passed!` | PASS | Verified during audit. |
| `py -m mypy intraday_scanner` | `No module named mypy` | FAIL | System Python lacks dev extras. |
| bundled `python -m mypy intraday_scanner` | `Success: no issues found in 49 source files` | PASS | Verified during audit. |
| `git status --short --branch` | `fatal: not a git repository` | FAIL | `.git` exists, but Git does not recognize this folder as a valid repo. |
| `git diff --check` | not a git repository usage output | FAIL | Cannot verify dirty files or whitespace. |
| `intraday-scan --help` | default shell not on PATH; works after adding Scripts path | PARTIAL | Console script exists, but Windows PATH is not ready. |
| `build-snapshot --help` | default shell not on PATH; works after adding Scripts path | PARTIAL | Same PATH issue. |
| `paper-audit --help` | default shell not on PATH; works after adding Scripts path | PARTIAL | Same PATH issue. |
| `intraday-scan init-db --db-path data\scanner_audit.sqlite` | DB initialized | PASS | SQLite audit DB created. |
| `intraday-scan scan ... --print` | ranked=4, avoid=4, top=NOVA | PASS | Fixture/sample data only. |
| `intraday-scan morning-run ... --print` | persisted run id | PASS | Sample recommendations persisted. |
| `intraday-scan monitor-open ... --max-iterations 1` | saved/generated monitor alert; 4 watching | PASS | Sample monitor snapshot only. |
| `intraday-scan audit-latest ... --persist` | wrote audit trades/summary | PASS/PARTIAL | One unavailable audit row due missing bars. |
| `intraday-scan performance-report ... --persist` | trades=2, avg_close=13.95%, hit_rate=100% | PASS/PARTIAL | Fixture result, not real historical claim. |
| `intraday-scan tune-strategy ...` | best=base top3_close=9.3%, hit_rate=75% | PASS/PARTIAL | Fixture-only tuning. |
| `intraday-scan notify-test` | console test alert | PASS | Console notifier path works. |
| `intraday-scan scheduler --json` | schedule JSON printed | PASS/PARTIAL | Static schedule only. |
| `intraday-scan live-scan --provider alpaca ...` | missing Alpaca credential error | BLOCKED/PASS | Fails gracefully and does not log keys. |
| `intraday-scan monitor-open --provider alpaca ...` | missing Alpaca credential error | BLOCKED/PASS | Fails gracefully and does not log keys. |
| Streamlit AppTest | exceptions=0, tabs present | PASS | Tabs: Dashboard, Run, Picks, 5-Min Check, Backtest, History, Settings. |

## 5. Architecture Map

- `app.py`: Streamlit dashboard and guided UI.
- `intraday_scanner/cli.py`: command entrypoint and workflow orchestration.
- `intraday_scanner/models.py`: canonical snapshot and candidate data models.
- `intraday_scanner/config.py`: env/default configuration and validation.
- `intraday_scanner/providers/`: CSV, Alpaca, news, SEC abstractions/adapters.
- `intraday_scanner/scoring.py` and `formula.py`: ranking formula and setup levels.
- `intraday_scanner/expectancy.py`: expected-return/confidence estimates.
- `intraday_scanner/services/`: scan, audit, monitor, alert, performance, tuning, provider health, universe helpers.
- `intraday_scanner/storage/sqlite_store.py`: SQLite persistence.
- `intraday_scanner/notifiers/`: console, email, Discord, Telegram dispatch.
- `intraday_scanner/scheduler.py` and `scripts/*.ps1`: schedule descriptions and Windows helper scripts.
- `tests/`: offline test suite.
- `docs/`: architecture, data contract, operations, provider setup, secrets, scheduler, tuning, audit docs.

## 6. Data Flow Diagram

1. Snapshot source: CSV file or Alpaca market-data snapshot.
2. Validation: `SnapshotRow.from_mapping` enforces required columns and numeric/boolean parsing.
3. Scoring: `score_universe` evaluates each snapshot with `dawnstrike-v2.0`.
4. Outputs: ranked candidates, top explosive, avoid list, summary CSV/JSON.
5. Persistence: SQLite stores scan run, raw snapshots, ranked candidates, top explosive, avoid list, recommendation theses.
6. Monitoring: saved candidates are compared to a fresh CSV/Alpaca snapshot.
7. Alerts: monitor/news/SEC rules generate deduped alert records.
8. Audit: saved ranked candidates are compared to post-signal minute bars.
9. Performance: persisted audit trades are summarized.
10. Dashboard: reads sample data, latest output files, or SQLite data depending on selected source.

## 7. CLI Inventory

| Command | Source | Purpose | Inputs | Outputs/DB writes | Tested | Status |
|---|---|---|---|---|---|---|
| `scan` | `_run_scan` | Offline CSV scan | snapshot CSV | output dir; optional DB persist | Yes | PASS |
| `live-scan` | `_run_live_scan` | Alpaca-backed scan | Alpaca keys, `--symbols`/file | output dir; optional DB persist; provider health | Missing-secret tests | BLOCKED/PARTIAL |
| `morning-run` | `_run_morning_run` | Persisted morning CSV workflow | snapshot CSV, DB path | output dir and DB scan records | Yes | PASS |
| `build-snapshot` | parser + console script | Build canonical snapshot CSV | input/output args | CSV snapshot | Smoke/help only | PARTIAL |
| `paper-audit` | parser + console script | Audit ranked CSV against minute bars | ranked CSV, minute bars | audit CSV/JSON | Yes | PASS/PARTIAL |
| `init-db` | `_run_init_db` | Create SQLite tables | DB path | SQLite schema | Yes | PASS |
| `notify` | `_run_notify` | Send scan/audit notifications | DB or audit summary | notification records | Yes dry-run | PARTIAL |
| `audit-latest` | `_run_audit_latest` | Audit latest persisted scan | DB, minute bars | audit files and optional DB rows | Yes | PASS/PARTIAL |
| `backfill-audit` | `_run_backfill_audit` | Audit historical ranked CSV | ranked CSV, minute bars | audit files and optional DB rows | Unknown direct | PARTIAL |
| `monitor-setups` | `_run_monitor_setups` | Check saved picks against current snapshot | DB, snapshot/provider | monitor files, checks, alerts | Yes | PASS/PARTIAL |
| `monitor-loop` | `_run_monitor_loop` | Repeat monitor until stopped/max iterations | same as monitor | repeated monitor writes | Indirect | PARTIAL |
| `monitor-open` | `_run_monitor_open` | Market-open monitor alias | same as monitor | same as loop | Yes | PASS/PARTIAL |
| `notify-test` | `_run_notify_test` | Console notification smoke | optional DB | console, optional notification record | Workflow run | PASS |
| `performance-report` | `_run_performance_report` | Summarize persisted audit trades | DB | console, optional performance tables | Workflow run | PASS/PARTIAL |
| `tune-strategy` | `_run_tune_strategy` | Compare scoring weight scenarios | snapshot, minute bars | tuning outputs | Yes | PASS/PARTIAL |
| `scheduler` | `_run_scheduler` | Print schedule | none | text/JSON only | Yes | PARTIAL |

## 8. Provider Inventory

| Provider | Real vs stub | Credential handling | Data returned | Full-market support | Status |
|---|---|---|---|---|---|
| CSV snapshot | Real local adapter | none | canonical `SnapshotRow` rows | only supplied CSV | PASS |
| Alpaca market data | Real read-only HTTP adapter | requires `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY`; no values logged | snapshots and 1-min bars for supplied symbols | no, static symbols only | BLOCKED/PARTIAL |
| NewsAPI | Real HTTP adapter | requires `NEWS_API_KEY` | headline/source/url/time items | only watched symbols | BLOCKED/PARTIAL |
| Finnhub | Real HTTP adapter | requires `FINNHUB_API_KEY` | headline/source/url/time items | only watched symbols | BLOCKED/PARTIAL |
| Benzinga | Not implemented | key exists in env/docs | none | no | PARTIAL/FAIL |
| Polygon/Databento | Not implemented | keys exist in config | none | no | PARTIAL/FAIL |
| SEC RSS | Real RSS/Atom parser | no secret | filings for symbols; dilution language detection | only watched symbols | PARTIAL |
| Null/mock providers | Test/offline stubs | none | empty/mock data | no | PASS for tests |
| Provider health | Real service | redacts key/token/secret/password marker strings | provider/status/detail rows | n/a | PASS |

Known provider limitations:
- Alpaca `get_premarket_snapshot` requires supplied symbols and uses the snapshots endpoint. It does not discover all premarket movers.
- Alpaca snapshot rows set float, market cap, short float, halt/offering/news fields to unknown/default values, so live ranking can be less informed than enriched CSV.
- SEC RSS uses a generic User-Agent containing `contact@example.com`; production should use a real contact.

## 9. Database Schema Audit

SQLite audit DB `data\scanner_audit.sqlite` exists from the sample workflow.

| Table | Count | Columns | Write path | Read path | Status |
|---|---:|---|---|---|---|
| `scan_runs` | 1 | `id, created_at, source, config_json, summary_json` | `persist_scan_result` | `load_latest_scan`, history | PASS |
| `raw_snapshots` | 8 | `id, run_id, ticker, as_of_timestamp, payload_json` | `persist_scan_result` | no direct dashboard read | PASS/PARTIAL |
| `ranked_candidates` | 4 | `id, run_id, rank, ticker, payload_json` | `persist_scan_result` | latest scan/dashboard | PASS |
| `top_explosive` | 1 | `id, run_id, rank, ticker, payload_json` | `persist_scan_result` | latest scan/dashboard | PASS |
| `avoid_list` | 4 | `id, run_id, rank, ticker, payload_json` | `persist_scan_result` | latest scan/dashboard | PASS |
| `recommendation_theses` | 4 | `id, run_id, ticker, rank, created_at, payload_json` | `persist_scan_result` | dashboard/history | PASS |
| `setup_monitor_checks` | 4 | `id, run_id, ticker, status, checked_at, payload_json` | monitor persist | latest monitor checks | PASS |
| `monitor_events` | 1 | `id, run_id, ticker, event_type, severity, created_at, payload_json` | deduped alerts | recent events | PASS |
| `alerts_sent` | 1 | `id, alert_key, run_id, ticker, event_type, severity, sent_at, payload_json` | `record_alert` | recent alerts | PASS |
| `notifications_sent` | 0 | `id, event_key, run_id, ticker, channel, sent_at, payload_json` | notification dispatch | dedupe checks | PASS/PARTIAL |
| `paper_audit_trades` | 3 | `id, run_id, ticker, payload_json` | audit persist | audit/performance/dashboard | PASS |
| `paper_audit_summary` | 1 | `id, run_id, created_at, payload_json` | audit persist | latest audit summary | PASS |
| `performance_daily` | 1 | `id, report_date, run_id, payload_json` | performance persist | not primary UI | PASS |
| `performance_cumulative` | 1 | `id, created_at, payload_json` | performance persist | dashboard/latest report | PASS |
| `provider_health` | 4 | `id, provider, status, checked_at, detail` | provider health service | dashboard | PASS |
| `candidates`, `snapshots` | 8 each | legacy/general payload rows | `persist_scan_result` | limited | PASS/PARTIAL |

Persistence answer: every sample recommendation, monitor check, generated alert, paper-audit trade, paper-audit summary, performance report, and provider-health row was persisted. Notification persistence exists, but the sample `notify-test` console path did not populate `notifications_sent` in this DB.

## 10. Scoring Audit

The formula is `dawnstrike-v2.0` in `intraday_scanner/formula.py`.

Verified components:
- Gap curve.
- Liquidity thrust.
- Float rotation.
- Range control.
- Squeeze/catalyst score.
- Execution quality.
- Data quality.
- Risk penalty.

Default config includes `min_gap_pct=15`, minimum premarket dollar volume `500000`, share volume `100000`, price band `$0.50-$25`, `top_n=10`, `explosive_top_n=3`, and weight knobs for every formula component.

Ranking is deterministic: non-avoid candidates are sorted by score then dollar volume; avoid candidates are sorted separately. Avoid reasons include current halt, recent offering, price outside bounds, low gap, low dollar/share volume, and zero/invalid data.

Catalyst/news fields are used, but only as structured fields available at scan time. In CSV mode they come from the file. In Alpaca snapshot mode `has_news` is currently false unless enriched elsewhere.

Status: PASS for explainable scan-time ranking. PARTIAL for predictive validity because calibration needs real historical sample size and live data enrichment.

## 11. Recommendation/Persistence Audit

Recommendations are persisted into `ranked_candidates`, `top_explosive`, `avoid_list`, and `recommendation_theses`. The recommendation payload stores scan id, timestamp, rank, ticker, score, component scores, thesis, catalyst summary, risk flags, breakout trigger, pullback zone, invalidation, targets, exit bias, confidence grade, and data-quality score.

Gap: `CANDIDATE_COLUMNS` does not include `catalyst_url`, even though the snapshot schema includes it. If catalyst URL matters for later review, recommendation payloads should carry it.

Status: PASS/PARTIAL.

## 12. Paper Audit / No-Lookahead Audit

Implemented metrics:
- Entry price.
- +1m, +5m, +15m returns.
- Lunch exit.
- Close exit.
- High after entry.
- Low after entry/drawdown.
- Slippage bps.
- Top 1/3/5 equal-weight returns.
- Unavailable data rows.

No-lookahead protections:
- Bars before `signal_time` are skipped.
- If a candidate has `timestamp`, `recommendation_timestamp`, `scan_timestamp`, or `as_of_timestamp`, bars before that timestamp are skipped.
- Summary excludes unavailable trades and counts `audit_unavailable_count`.

Important limitation:
- Entry is the first eligible bar open, not the breakout trigger price. The code separately records whether a later bar triggered. This can overstate or distort a breakout strategy because it audits an immediate entry, not strictly "enter when breakout confirms."
- Sparse fixture bars can make +1m/+5m/+15m point to the same later bar, so sample returns are not proof of minute-level performance.
- Performance cumulative curve sums percentage returns rather than compounding position/account equity.

Status: PARTIAL. The implementation is useful for research, but not bulletproof return proof yet.

## 13. Monitor / Alert Audit

Monitor logic:
- `monitor-loop` repeats `monitor-setups` every `interval_seconds`.
- `monitor-open --continuous` removes max iteration; otherwise max iterations can stop the loop.
- Default monitor-open interval is 60 seconds in the CLI; docs/scripts also show 5-minute task setup.
- Active symbols default to latest ranked candidates, limited by top N unless explicit symbols are supplied.

Checks implemented:
- Halt.
- Recent offering.
- Invalidation level.
- Drop from watch price.
- First/stretch target extension.
- Breakout confirmation.
- Breakout rejection.
- Pullback loss.
- Volume collapse.
- Lower-third range weakness.
- Wide spread risk flag.

Alerts:
- Invalidated -> critical `THESIS BROKEN`.
- Fading -> high `CAUTION`.
- Extended -> medium `WATCH`.
- Offering/halt/wide_spread flags -> high risk alert.
- News/SEC alerts are available only when CLI flags enable news provider and/or SEC RSS.
- Alerts dedupe through unique `alert_key` in `alerts_sent`; persisted monitor events are written only for newly recorded alerts.

Gaps:
- Drop-from-watch, volume-collapse, and breakout-rejection are represented through status/risk flags, but not as separate event types unless the status path triggers.
- Live monitor current price comes from a fresh snapshot, not a full minute-bar path analysis.
- Real live behavior was not proven because credentials were absent.

Status: PARTIAL/BLOCKED for live.

## 14. News / SEC / AI Audit

News:
- NewsAPI and Finnhub providers call external APIs and return `NewsItem` rows.
- `auto` selects NewsAPI first, then Finnhub.
- Null and mock providers support offline/test mode.
- Benzinga is documented/reserved but not implemented.

SEC:
- SEC RSS provider parses filings for supplied symbols.
- Dilution/offering/ATM/shelf/warrant-style language is detected through `filing_has_dilution_risk`.
- Source URLs are carried in alert payloads.

AI/classification:
- The headline classifier is deterministic/rule-based, not OpenAI-backed.
- `OPENAI_API_KEY` is loaded/reserved, but source inspection found no model API call.
- Tests mock external data and do not perform live news/SEC integration.

Status: PARTIAL/BLOCKED.

## 15. Notification Audit

| Channel | Implementation | Required env | Persistence/dedupe | Status |
|---|---|---|---|---|
| Console | Real | none | optional via dispatch/store | PASS |
| Email | Real SMTP | SMTP host/port/from/to/user/password as needed | `notifications_sent` event key | BLOCKED/PARTIAL |
| Discord | Real webhook POST | `INTRADAY_DISCORD_WEBHOOK_URL` or alias | `notifications_sent` event key | BLOCKED/PARTIAL |
| Telegram | Real Bot API POST | bot token and chat id | `notifications_sent` event key | BLOCKED/PARTIAL |

Failure behavior is explicit: missing config raises `NotificationError`; HTTP/webhook failures raise `NotificationError`.

## 16. Dashboard Audit

Verified:
- Streamlit AppTest ran with zero exceptions.
- Tabs are `Dashboard`, `Run`, `Picks`, `5-Min Check`, `Backtest`, `History`, `Settings`.
- Data loader supports sample scan, output directory, and SQLite.
- SQLite loader reads latest scan, scan history, provider health, performance report, recent alerts, monitor events, recommendation history, latest monitor checks, and audit trades.
- UI includes run/test/monitor/backtest/history/settings surfaces.

Limits:
- Browser visual QA was not re-run in this audit; previous screenshots showed styling/layout problems before the current audit request.
- The dashboard is real-data capable, but the current verified data source was the sample/audit SQLite DB.
- Some UI actions still depend on local files/paths and local process permissions.

Status: PASS/PARTIAL.

## 17. Scheduler Audit

`intraday_scanner/scheduler.py` prints a static schedule:
- 08:00 build snapshot.
- 08:10 morning run.
- 08:15 push recommendations.
- 08:30 market-open monitor with Alpaca continuous mode.
- Lunch/close audit.
- Performance report.

PowerShell helpers exist:
- `scripts/register_dawnstrike_tasks.ps1`.
- `scripts/run_monitor_once.ps1`.
- `scripts/run_monitor_loop.ps1`.
- `scripts/run_sample_backtest.ps1`.
- `scripts/alpaca_live_snapshot.py`.

Gaps:
- `scheduler.py` prints schedule; it is not a durable scheduler daemon.
- Market calendar helper treats Monday-Friday as market days; no holiday/early-close logic.
- No robust retry/backoff/alerting for failed scheduled jobs.
- Task registration script is sample-oriented and uses file paths that must be configured.

Status: PARTIAL.

## 18. Test / Quality Audit

Test files: 19. Verified bundled-runtime test result: `41 passed in 5.31s`.

Covered areas:
- CLI failures and sample workflows.
- Config/env loading.
- CSV provider.
- Scoring/formula.
- Storage.
- Paper audit.
- Setup monitor.
- Alert service.
- Notifications.
- Provider health.
- SEC provider via mocked URL open.
- News/AI rules.
- Dashboard data loader.
- Streamlit AppTest.
- Scheduler.
- Tuning.
- Expectancy.

Not covered enough:
- Live Alpaca integration against real API.
- Full-market universe discovery.
- Real NewsAPI/Finnhub/SEC network integration.
- Browser-level dashboard layout regression.
- Task Scheduler registration execution.
- Large historical dataset validation.
- Strict breakout-entry audit behavior.
- Alert latency and retry/failure operations.

Quality status:
- Bundled `ruff`: PASS.
- Bundled `mypy`: PASS.
- System Python dev checks: FAIL because dev extras are not installed.

## 19. Security / No-Trading Audit

Search terms included order placement, broker execution, Alpaca trading client, buy/sell, keys, tokens, webhooks, and passwords.

Findings:
- No `submit_order`, `place_order`, `create_order`, or Alpaca `TradingClient` path found.
- Alpaca provider uses `https://data.alpaca.markets` market-data endpoints only.
- UI and docs repeatedly state research-only and no broker orders.
- `.env.example` contains key names only, not values.
- `.gitignore` ignores `.env`, `.streamlit/secrets.toml`, caches, outputs, and SQLite files.
- Test files contain fake secret-looking strings for config tests only.

Status: PASS for no hidden execution path. PARTIAL for production secrets posture because Git metadata is invalid, so tracked/untracked state could not be verified.

## 20. Documentation Audit

Docs present:
- `README.md`
- `docs/ARCHITECTURE.md`
- `docs/DATA_CONTRACT.md`
- `docs/OPERATIONS.md`
- `docs/PROVIDER_SETUP.md`
- `docs/NOTIFICATIONS.md`
- `docs/HISTORICAL_AUDIT.md`
- `docs/SCHEDULER_WINDOWS.md`
- `docs/TUNING.md`
- `docs/SECRETS_REQUIRED.md`
- `docs/AUTOMATION.md`
- `docs/BACKTESTING_AND_AUDIT.md`
- `docs/EXPECTANCY_MODEL.md`
- `docs/FORMULA.md`

Accuracy:
- Data contract matches the canonical snapshot schema.
- Secrets docs honestly say OpenAI is future/reserved and Benzinga is future/non-blocking.
- Operations docs explain sample and provider workflows.
- Scheduler docs describe Windows Task Scheduler setup.

Gaps:
- Docs use `intraday-scan` directly, but this Windows environment cannot run console scripts until the Scripts directory is on PATH or commands are run as `py -m intraday_scanner.cli`.
- Docs should be stronger that sample audit returns are fixture-only and not real historical performance.

Status: PASS/PARTIAL.

## 21. Live-Readiness Blockers

Not secrets-only. Remaining blockers include:
1. Add dev extras/tooling instructions so `py -m pytest`, `py -m ruff`, and `py -m mypy` work in the intended environment.
2. Fix Windows PATH/entrypoint setup or document `py -m intraday_scanner.cli` as the reliable command.
3. Repair/replace invalid Git metadata so status, diffs, ignored files, and commit baseline are verifiable.
4. Configure Alpaca read-only market-data credentials.
5. Build or integrate full-market premarket mover discovery; current live scan requires static symbols.
6. Enrich live snapshots with float, market cap, short float, halt, offering, and news/catalyst signals.
7. Validate live Alpaca scans during market/premarket hours and record row counts/filter counts.
8. Replace sample minute bars with real historical minute-bar datasets.
9. Decide whether audit entry should be immediate open or breakout-trigger fill, then test it.
10. Harden scheduler with market holidays, early closes, logging, retries, and failure notifications.

## 22. Real-Money-Readiness Blockers

1. No large, point-in-time historical validation set.
2. No out-of-sample performance report across market regimes.
3. No proof of live paper-trading performance over many sessions.
4. Audit entry logic does not strictly model breakout fills.
5. No verified latency model for data ingestion, alerts, and broker manual action.
6. No production-grade market calendar/retry/failure recovery.
7. No independent data-quality reconciliation across providers.
8. No capital allocation, max loss, position sizing, or daily stop policy in app.
9. No compliance review for financial advice/trading claims.
10. No broker execution path by design; user must trade manually and own execution risk.

## 23. Highest-Priority Fixes

1. Repair Git repo recognition.
2. Add a one-command Windows setup path that installs dev extras and fixes console script PATH.
3. Create a live-readiness settings page/checklist that validates all required env vars without printing values.
4. Implement full-market universe/premarket movers or a vetted symbols ingestion source.
5. Build a live snapshot enrichment pipeline for float/market cap/short float/news/halt/offering.
6. Change or explicitly label paper-audit entry semantics.
7. Add real historical minute-bar backfill ingestion and out-of-sample reports.
8. Harden scheduler with market calendar, retry logging, and failure notifications.
9. Add browser visual regression tests for dashboard layout.
10. Add live-provider smoke tests that can be run safely with read-only credentials.

## 24. Exact Windows Commands For Next Run

Recommended reliable path until console-script PATH is fixed:

```powershell
cd C:\Users\MattFields\Dawnstrike
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m intraday_scanner.cli init-db --db-path data\scanner_audit.sqlite
py -m intraday_scanner.cli scan --snapshot sample_data\premarket_snapshot_sample.csv --out-dir outputs\audit_scan --db-path data\scanner_audit.sqlite --persist --print
py -m intraday_scanner.cli monitor-open --snapshot sample_data\premarket_snapshot_sample.csv --db-path data\scanner_audit.sqlite --out-dir outputs\audit_monitor --persist --max-iterations 1
py -m intraday_scanner.cli audit-latest --db-path data\scanner_audit.sqlite --minute-bars sample_data\minute_bars\2026-06-18.csv --out-dir outputs\audit_latest --persist
py -m intraday_scanner.cli performance-report --db-path data\scanner_audit.sqlite --persist
streamlit run app.py --server.port 8502
```

If using the installed console scripts, add the Python Scripts directory to PATH first:

```powershell
$env:PATH = "C:\Users\MattFields\AppData\Local\Programs\Python\Python313\Scripts;$env:PATH"
intraday-scan --help
```

For safe live checks after adding read-only Alpaca market-data keys:

```powershell
py -m intraday_scanner.cli live-scan --provider alpaca --symbols TSLA,NVDA,AMD --db-path data\scanner_live.sqlite --out-dir outputs\live_scan --persist --print
py -m intraday_scanner.cli monitor-open --provider alpaca --db-path data\scanner_live.sqlite --out-dir outputs\live_monitor --persist --max-iterations 1
```

## 25. Final PASS/PARTIAL/BLOCKED/FAIL Table

| Item | Status |
|---|---|
| Source-level audit completed | PASS |
| Report file created | PASS |
| Offline scan workflow | PASS |
| SQLite persistence | PASS |
| Sample monitor workflow | PASS |
| Sample audit/performance workflow | PASS/PARTIAL |
| Streamlit AppTest | PASS |
| Bundled tests/lint/typecheck | PASS |
| System Python test/lint/typecheck | FAIL |
| Git repo health | FAIL |
| Console scripts in default shell | PARTIAL/FAIL |
| Alpaca live market-data adapter | BLOCKED/PARTIAL |
| Full-market live scanner | PARTIAL/FAIL |
| News/SEC/AI thesis monitoring | PARTIAL/BLOCKED |
| Notification channels | PARTIAL/BLOCKED |
| Scheduler automation | PARTIAL |
| Hidden order execution | PASS: none found |
| Live mode ready except secrets | No. PARTIAL/BLOCKED with non-secret blockers. |
| Real-money readiness | FAIL/PARTIAL |

