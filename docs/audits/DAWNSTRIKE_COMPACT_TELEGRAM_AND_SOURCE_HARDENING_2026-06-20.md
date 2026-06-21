# Dawnstrike Compact Telegram And Source Hardening - 2026-06-20

## Files changed

- `intraday_scanner/notifiers/telegram_formatter.py`
- `intraday_scanner/notifiers/webhooks.py`
- `intraday_scanner/services/time_utils.py`
- `intraday_scanner/services/web_collection_service.py`
- `intraday_scanner/services/e2e_automation_service.py`
- `intraday_scanner/providers/browser_table_provider.py`
- `intraday_scanner/providers/web_source_base.py`
- `intraday_scanner/cli.py`
- `intraday_scanner/config.py`
- `app.py`
- `config/web_sources.example.yaml`
- `config/automation.example.yaml`
- `pyproject.toml`
- `tests/test_web_autopilot.py`
- `tests/test_e2e_automation.py`
- `tests/fixtures/browser_table_fixture.html`
- `tests/fixtures/browser_grid_fixture.html`
- `tests/fixtures/browser_blocked_fixture.html`
- `tests/fixtures/browser_no_table_fixture.html`
- `docs/TELEGRAM_NOTIFICATIONS.md`
- `docs/WEB_AUTO_PILOT.md`
- `docs/URL_INGESTION.md`
- `docs/BROWSER_SOURCE_EXTRACTION.md`
- `README.md`

## Tests run

- `py -m pip install -e ".[dev]"` passed.
- `py -m pytest -p no:cacheprovider` passed: `114 passed`.
- `py -m ruff check .` passed.
- `py -m mypy intraday_scanner` passed: `Success: no issues found in 74 source files`.
- `py -m compileall intraday_scanner app.py tests` passed.
- `py -m pytest tests\test_web_autopilot.py -p no:cacheprovider` passed: `28 passed`.
- `py -m pytest tests\test_e2e_automation.py -p no:cacheprovider` passed: `16 passed`.
- `git diff --check` passed with only CRLF conversion warnings.

## Message before/after example

Before:

```text
timestamp: 2026-06-21T02:31:20+00:00
top1 return: n/a
top3 return: n/a
top5 return: n/a
missing outcome count: 3
dashboard URL: http://127.0.0.1:8502/
Returns are manual/free shadow results only.
```

After:

```text
🚀 Dawnstrike Watchlist
⏱ 9:53 CT | 3 picks | Source: manual

1) NOVA — 88.1 | +89% | $5.20
   🎯 Trigger $5.48 | 🛑 $3.20
   📰 FDA phase 2 data
   ⚠️ Risk: none

2) RIFT — 66.6 | +45% | $2.40
   🎯 Trigger $2.56 | 🛑 $1.77
   📰 Strategic supply agreement
   ⚠️ Risk: none

3) WIDE — 57.9 | +58% | $6.80
   🎯 Trigger $7.19 | 🛑 $5.86
   📰 Low float sympathy move
   ⚠️ Risk: wide_spread

🚫 Avoid: 1
Top reason: current_halt

Research only. No orders placed.
```

## Source doctor result

Command:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

Result summary:

- `nasdaq_symbols`: `universe_only`; does not generate premarket picks.
- `barchart_premarket`: attempted, `failed`, `no_candidate_table`.
- `tradingview_premarket`: attempted, `no_valid_rows`, `rows_extracted=100`, `rows_normalized=0`.
- `local_inbox`: attempted, `empty` after the fixture run archived the input file.
- Candidate sources were enabled, so `candidate_source_required=false`.

## Browser extractor status

Browser extractor is optional and currently not installed in this environment:

```text
BROWSER_EXTRACTOR_NOT_AVAILABLE: run py -m pip install -e ".[browser]" and py -m playwright install chromium
```

Offline browser tests passed using local fixtures for table, grid, login/CAPTCHA,
no-table, and missing dependency cases. Browser rows are labeled
`data_source_kind=browser_url` and
`coverage_warning=browser_rendered_public_table_unverified`.

## Telegram compact sample

The fixture flow copied `tests\fixtures\raw_screener_aliases.csv` to
`data\inbox\screener\compact_message_test.csv`, ran one Telegram cycle, and
persisted a compact `top_picks` notification. The run sent two messages and
skipped two existing duplicate keys from the already-used local DB.

Outcome reminder payload from the same cycle included:

```json
{
  "tickers": ["NOVA", "RIFT", "WIDE"],
  "status": "missing",
  "reminder_path": "data\\inbox\\outcomes\\outcomes_2026-06-20.csv"
}
```

## No-trading safety result

Command:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation" intraday_scanner app.py scripts
```

Result: no implementation matches. Exit code was `1`, meaning no matches.

No broker/order/trading execution code was added.

## Exact next commands

For daily operation:

```powershell
Copy-Item config\web_sources.example.yaml config\web_sources.yaml
Copy-Item config\automation.example.yaml config\automation.yaml
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

For optional browser extraction:

```powershell
py -m pip install -e ".[browser]"
py -m playwright install chromium
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

For validation:

```powershell
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall intraday_scanner app.py tests
```
