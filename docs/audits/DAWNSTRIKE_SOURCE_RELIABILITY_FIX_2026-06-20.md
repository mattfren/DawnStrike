# Dawnstrike Source Reliability Fix - 2026-06-20

## Files changed

- `README.md`
- `config/web_sources.example.yaml`
- `docs/BROWSER_SOURCE_EXTRACTION.md`
- `docs/FORMULA.md`
- `docs/FREE_SHADOW_MODE.md`
- `docs/PREMARKET_INTELLIGENCE.md`
- `docs/TELEGRAM_NOTIFICATIONS.md`
- `docs/URL_INGESTION.md`
- `docs/WEB_AUTO_PILOT.md`
- `intraday_scanner/cli.py`
- `intraday_scanner/formula.py`
- `intraday_scanner/models.py`
- `intraday_scanner/notifiers/telegram_formatter.py`
- `intraday_scanner/providers/browser_table_provider.py`
- `intraday_scanner/providers/public_table_provider.py`
- `intraday_scanner/providers/web_source_base.py`
- `intraday_scanner/scoring.py`
- `intraday_scanner/services/premarket_intelligence.py`
- `intraday_scanner/services/scan_service.py`
- `intraday_scanner/services/web_collection_service.py`
- `intraday_scanner/storage/sqlite_store.py`
- `tests/fixtures/investing_premarket.html`
- `tests/fixtures/marketwatch_movers.html`
- `tests/fixtures/stockanalysis_premarket.html`
- `tests/fixtures/tradingview_premarket_extracted.html`
- `tests/test_premarket_intelligence.py`
- `tests/test_scoring.py`
- `tests/test_web_autopilot.py`

## Commands run

- `py -m pip install -e ".[dev]"` passed.
- `py -m pytest -p no:cacheprovider` passed: `132 passed`.
- `py -m ruff check .` passed.
- `py -m mypy intraday_scanner` passed: `Success: no issues found in 75 source files`.
- `py -m compileall intraday_scanner app.py tests` passed.
- `git diff --check` passed with CRLF conversion warnings only.
- `rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation" intraday_scanner app.py scripts` returned no matches.

## Source-doctor before/after

Before this pass, the latest source hardening report showed:

- `tradingview_premarket`: `rows_extracted=100`, `rows_normalized=0`.
- `barchart_premarket`: failed with `no_candidate_table`.
- `local_inbox`: empty.
- Browser extraction was optional and not required for static public sources.

After this pass:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

Result:

- `local_inbox`: enabled, empty.
- `stockanalysis_premarket`: enabled, `rows_extracted=20`, `rows_normalized=0`, `rows_rejected=10`, top reason `missing_price`.
- `tradingview_premarket`: enabled, `rows_extracted=100`, `rows_normalized=99`, `rows_rejected=1`, top reason `invalid_numeric_format`.
- `tradingview_premarket_browser`: disabled fallback.
- `marketwatch_movers`: disabled fallback.
- `investing_premarket`: disabled fallback.
- `barchart_premarket` and `barchart_premarket_browser`: disabled because Barchart is commonly blocked and should not be bypassed.
- Aggregate rejection counts: `{"invalid_numeric_format": 1, "missing_price": 10}`.

## Which source normalized rows

`tradingview_premarket` produced the usable candidate set. The collector persisted:

- `outputs\source_doctor\tradingview_premarket\premarket_snapshot.csv`
- `outputs\source_doctor\tradingview_premarket\extracted_rows.csv`
- `outputs\source_doctor\tradingview_premarket\rejected_rows.csv`
- `outputs\source_doctor\tradingview_premarket\normalization_debug.json`

The follow-up collection command:

```powershell
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto_test --persist --print
```

Result summary:

- `status`: `success`
- `sources_attempted`: `3`
- `sources_succeeded`: `1`
- `rows_extracted`: `120`
- `rows_normalized`: `99`
- `candidate_count`: `99`
- `top_failure_reason`: `missing_price`
- `rejection_reason_counts`: `{"invalid_numeric_format": 1, "missing_price": 10}`

## Rejection diagnostics

Source normalization now writes machine-readable diagnostics:

- Per-source `extracted_rows.csv`
- Per-source `rejected_rows.csv`
- Per-source `normalization_debug.json`
- Aggregate `outputs\source_doctor\extracted_rows.csv`
- Aggregate `outputs\source_doctor\rejected_rows.csv`
- Aggregate `outputs\source_doctor\normalization_debug.json`

The rejection reasons are explicit enough to tell the operator whether a page shape changed, a required price field is missing, or a number format needs a new parser rule.

## Telegram source failure behavior

The no-source path remains a single source-health notification path. The compact formatter now includes the top failure reason and a practical next step:

```text
try again during premarket or drop CSV into data\inbox\screener
```

Offline tests cover the no-source dry-run daemon path and confirm it does not also send outcome reminders or scan summaries when no candidate source produced rows.

The configured one-cycle Telegram daemon verification was also run:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

Result:

- `status`: `complete`
- `cycles`: `1`
- collection `status`: `success`
- `sources_attempted`: `3`
- `sources_succeeded`: `1`
- `rows_extracted`: `120`
- `rows_normalized`: `99`
- `candidate_count`: `99`
- `top_failure_reason`: `missing_price`
- `rejection_reason_counts`: `{"invalid_numeric_format": 1, "missing_price": 10}`
- notifications: `sent=2`, `skipped=2`
- no Telegram token or chat ID was printed.

## Config to use next

Use this hierarchy:

1. `local_inbox`
2. `stockanalysis_premarket`
3. `tradingview_premarket`
4. `tradingview_premarket_browser` optional fallback
5. `marketwatch_movers` optional fallback
6. `investing_premarket` optional fallback
7. `barchart_premarket` and `barchart_premarket_browser` disabled unless they are publicly reachable without login, CAPTCHA, paywall, or anti-bot bypass

Exact setup:

```powershell
Copy-Item config\web_sources.example.yaml config\web_sources.yaml
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto_test --persist --print
```

## No-trading safety result

No broker connection, order placement, or trading execution code was added. The safety grep returned no implementation matches for order-submission APIs or broker execution terms.

The app remains research-only.
