# Dawnstrike Free Shadow Mode - 2026-06-20

Scope: build and validate the zero-dollar Free Shadow Mode workflow up to, but
not including, adding secrets.

Safety boundary: no API keys were added, no paid/live provider APIs were called,
no broker order execution was added, and no trades were submitted. Template
manual returns are validation fixtures for workflow proof only; they are not
performance claims.

## Executive Result

Result: PASS for the offline/manual Free Shadow Mode workflow.

Dawnstrike can now:

- print a ChatGPT normalization prompt
- normalize a manual screener CSV into Dawnstrike's canonical snapshot shape
- run and persist a labeled manual shadow scan
- persist ranked candidates, top explosive picks, avoid list, theses, risk flags,
  and snapshot rows
- import manual outcomes with no-lookahead validation
- audit manual outcomes without treating missing prices as zero
- generate top1/top3/top5 manual shadow reports
- show shadow-mode records in the dashboard
- build an offline/free fixture universe without secrets

## Files Changed

- `.gitignore`
- `README.md`
- `app.py`
- `intraday_scanner/cli.py`
- `intraday_scanner/models.py`
- `intraday_scanner/dashboard/data_loader.py`
- `intraday_scanner/services/free_shadow_mode.py`
- `intraday_scanner/storage/sqlite_store.py`
- `templates/chatgpt_screener_to_snapshot_prompt.md`
- `templates/chatgpt_outcomes_to_csv_prompt.md`
- `templates/manual_premarket_snapshot_template.csv`
- `templates/manual_outcomes_template.csv`
- `docs/FREE_SHADOW_MODE.md`
- `docs/MANUAL_UPLOADS.md`
- `docs/FREE_DATA_PIPELINE.md`
- `docs/DATA_QUALITY.md`
- `tests/test_free_shadow_mode.py`
- `docs/audits/DAWNSTRIKE_FREE_SHADOW_MODE_2026-06-20.md`

## Commands Run

| Command | Result |
|---|---|
| `py -m pip install -e ".[dev]"` | PASS; only known Scripts-not-on-PATH warning. |
| `py -m pytest -p no:cacheprovider` | PASS; 59 passed. |
| `py -m ruff check .` | PASS |
| `py -m mypy intraday_scanner` | PASS; no issues in 59 source files. |
| `py -m compileall intraday_scanner` | PASS |
| `py -m intraday_scanner.cli init-db --db-path data\shadow.sqlite` | PASS |
| `py -m intraday_scanner.cli print-upload-prompt` | PASS; output captured to `outputs\shadow_prompt_check.txt` during verification. |
| `py -m intraday_scanner.cli import-manual-snapshot --input templates\manual_premarket_snapshot_template.csv --out outputs\shadow_manual_snapshot --db-path data\shadow.sqlite --persist` | PASS; 3 manual rows normalized and persisted. |
| `py -m intraday_scanner.cli free-shadow-scan --snapshot outputs\shadow_manual_snapshot\premarket_snapshot.csv --db-path data\shadow.sqlite --out-dir outputs\shadow_scan --persist --print` | PASS; ranked=3, avoid=0, top=NOVA. |
| `py -m intraday_scanner.cli import-manual-outcomes --input templates\manual_outcomes_template.csv --db-path data\shadow.sqlite --persist` | PASS; inserted=3, skipped=0. |
| `py -m intraday_scanner.cli audit-manual-outcomes --db-path data\shadow.sqlite --out-dir outputs\shadow_audit --persist` | PASS; 3 manual rows audited, 1 partial row due missing close. |
| `py -m intraday_scanner.cli free-shadow-report --db-path data\shadow.sqlite --out-dir outputs\shadow_report --persist` | PASS; top1/top3/top5 report generated with manual/free labels. |
| `py -m intraday_scanner.cli build-free-universe --out data\universe_us_common.csv` | PASS; fixture/free universe generated, accepted=6, rejected=2. |
| `py -m pytest tests\test_streamlit_app.py -p no:cacheprovider` | PASS; dashboard AppTest smoke passed. |
| HTTP `http://127.0.0.1:8502/` | PASS; existing server returned 200. |
| Safety search for order execution terms | PASS; only docs/audit text matched, no implementation path found. |
| `git status --short --branch` | BLOCKED; existing `.git` remains invalid. No automatic Git repair was performed. |

## Test Result

Offline test gate passed:

- `59 passed`
- Ruff clean
- MyPy clean
- Compileall clean

New coverage includes prompt output, manual snapshot import, missing enrichment
handling, dollar-volume and gap calculations, no-lookahead outcome rejection,
manual outcome import, unavailable metric handling, equal-weight top baskets,
compounded curves, shadow report generation, dashboard loader fields, valid
templates, and free universe fixture mode.

## Output Files Generated

- `outputs/shadow_prompt_check.txt`
- `outputs/shadow_manual_snapshot/premarket_snapshot.csv`
- `outputs/shadow_scan/ranked_candidates.csv`
- `outputs/shadow_scan/top_explosive.csv`
- `outputs/shadow_scan/avoid_list.csv`
- `outputs/shadow_scan/scan_summary.json`
- `outputs/shadow_audit/manual_audit_trades.csv`
- `outputs/shadow_audit/manual_audit_summary.json`
- `outputs/shadow_report/free_shadow_report.json`
- `outputs/shadow_report/free_shadow_equity_curve.csv`
- `outputs/universe_rejected.csv`
- `outputs/universe_build_summary.json`
- `data/universe_us_common.csv`

## SQLite Table Row Counts

Database: `data\shadow.sqlite`

| Table | Rows |
|---|---:|
| `alerts_sent` | 0 |
| `avoid_list` | 0 |
| `candidates` | 3 |
| `manual_audit_summary` | 1 |
| `manual_audit_trades` | 3 |
| `manual_outcomes` | 3 |
| `manual_snapshot_rows` | 3 |
| `manual_snapshot_uploads` | 1 |
| `monitor_events` | 0 |
| `notifications_sent` | 0 |
| `paper_audit_summary` | 0 |
| `paper_audit_trades` | 0 |
| `performance_cumulative` | 0 |
| `performance_daily` | 0 |
| `provider_health` | 1 |
| `ranked_candidates` | 3 |
| `raw_snapshots` | 3 |
| `recommendation_theses` | 3 |
| `scan_runs` | 1 |
| `setup_monitor_checks` | 0 |
| `shadow_reports` | 1 |
| `snapshots` | 3 |
| `top_explosive` | 1 |

## Manual Workflow Proof

Manual snapshot import:

- normalized 3 rows
- calculated missing `dollar_volume`
- calculated missing `gap_pct`
- set `source=manual_upload`
- set `data_source_kind=manual`
- set `shadow_mode=true`
- set `paid_data=false`
- preserved missing `catalyst_url` as unknown and recorded `coverage_warning`

Shadow scan:

- persisted `scan_runs`, `raw_snapshots`, `snapshots`, `candidates`,
  `ranked_candidates`, `top_explosive`, `recommendation_theses`, and
  `provider_health`
- output ranked candidates with score breakdown, catalyst URL, risk flags,
  breakout trigger, pullback zone, invalidation level, first target, stretch
  target, and exit bias

Manual outcomes:

- matched outcomes to saved recommendations by ticker/date/timestamp
- rejected rows before the saved recommendation in tests
- persisted manual outcomes with `manual_uploaded_data=true`
- did not overwrite existing outcome rows unless `--replace` is supplied

Manual audit:

- calculated 1m, 5m, 15m, lunch, close, high-after-entry, low drawdown, max
  favorable excursion, and max adverse excursion where prices were supplied
- left missing close return blank for the partial row
- marked that missing close metric as `close_return_status=unavailable`
- generated top1/top3/top5 equal-weight close baskets and compounded curves

Shadow report:

- `manual_uploaded_data=true`
- `shadow_mode=true`
- `paid_data=false`
- `provider_validated=false`
- includes source mix, data quality summary, missing enrichment count, top baskets,
  lunch/close/high metrics, drawdown, hit rate, average, median, best/worst pick,
  best/worst day, and compounded curves

## Dashboard Smoke Result

Streamlit AppTest passed. The existing server at `http://127.0.0.1:8502/`
returned HTTP 200.

The dashboard now loads and displays:

- Free Shadow Mode instructions
- latest manual/shadow scan labels
- top explosive picks
- ranked candidates and avoid list
- `data_source_kind`
- `shadow_mode`
- coverage warnings
- data quality score
- missing enrichment count
- uploaded manual outcomes
- manual audit status
- top1/top3/top5 report cards
- historical shadow calls through scan history and recommendation history

The tab structure stayed unchanged to preserve the existing app smoke contract.

## No-Trading Safety Result

Search terms:

- `submit_order`
- `place_order`
- `create_order`
- `TradingClient`
- `alpaca.trading`
- `broker execution`
- `auto trade`
- `order submission`

Findings:

- No order placement implementation was found.
- No Alpaca trading client path was found.
- Matches were documentation/audit text only.
- Dawnstrike remains research/watchlist and paper-validation software only.

## Repo Hygiene

- `.git` is still invalid; no automatic repair or reinitialization was attempted.
- `docs/GIT_REPAIR.md` exists.
- `.env` does not exist.
- `.streamlit/secrets.toml` does not exist.
- `.gitignore` contains `.env`, `.venv/`, `outputs/`, `logs/`,
  `data/*.sqlite`, `data/raw/`, `data/cache/`, `__pycache__/`, and
  `.streamlit/secrets.toml`.
- Because Git is invalid, tracked-secret status cannot be proven from Git.

## What Works With Zero Keys

- Manual screener CSV normalization
- ChatGPT prompt printing for CSV normalization
- Manual shadow scan
- SQLite persistence for manual snapshots, scans, theses, outcomes, audits, and
  reports
- Manual outcome audit
- Free shadow report
- Dashboard review
- Offline starter universe fixture generation
- Console-only local operation

## What Still Requires Free Alpaca / News Keys

- Real read-only Alpaca market-data pulls
- Free/IEX live validation through Alpaca
- NewsAPI or Finnhub headline checks
- External notification delivery if using Discord, Telegram, or email

## What Still Requires Paid Data

- Full-market, low-latency tape coverage
- Complete premarket venue coverage
- Robust float/short-interest/corporate-action enrichment
- Large point-in-time historical datasets with survivorship controls
- Institutional-grade validation of edge and slippage

## Remaining Blockers

| Blocker | Type | Detail |
|---|---|---|
| Git metadata invalid | Manual hygiene | `.git` is not recognized. `docs/GIT_REPAIR.md` still exists; no automatic reinit was performed. |
| Manual data accuracy | Data quality | Manual outcomes are only as accurate as the uploaded file. |
| Free universe fixture | Data quality | `build-free-universe` runs with zero keys using fixture mode; replace with a broad U.S. common-stock universe before serious live validation. |
| Live market validation | Secrets/live-market | Requires read-only provider keys and market-hour proof. |
| Paid-grade confidence | Paid data | Serious confidence requires broad historical and live validation beyond manual/template rows. |

## Exact Windows Daily Workflow

```powershell
cd C:\Users\MattFields\Dawnstrike

py -m intraday_scanner.cli print-upload-prompt

py -m intraday_scanner.cli import-manual-snapshot `
  --input data\manual\morning_snapshot_YYYY-MM-DD.csv `
  --out outputs\manual_snapshot_YYYY-MM-DD `
  --db-path data\shadow.sqlite `
  --persist

py -m intraday_scanner.cli free-shadow-scan `
  --snapshot outputs\manual_snapshot_YYYY-MM-DD\premarket_snapshot.csv `
  --db-path data\shadow.sqlite `
  --out-dir outputs\shadow_scan_YYYY-MM-DD `
  --persist `
  --print

py -m intraday_scanner.cli import-manual-outcomes `
  --input data\manual\outcomes_YYYY-MM-DD.csv `
  --db-path data\shadow.sqlite `
  --persist

py -m intraday_scanner.cli audit-manual-outcomes `
  --db-path data\shadow.sqlite `
  --out-dir outputs\shadow_audit_YYYY-MM-DD `
  --persist

py -m intraday_scanner.cli free-shadow-report `
  --db-path data\shadow.sqlite `
  --out-dir outputs\shadow_report `
  --persist

py -m streamlit run app.py --server.port 8502
```

Optional zero-key universe starter:

```powershell
py -m intraday_scanner.cli build-free-universe --out data\universe_us_common.csv
```

## Final Statement

Free Shadow Mode is implemented and validated offline. It works with zero
secrets and clearly labels manual/free data. It does not place orders, submit
trades, hold broker trading credentials, or claim paid/live performance.
