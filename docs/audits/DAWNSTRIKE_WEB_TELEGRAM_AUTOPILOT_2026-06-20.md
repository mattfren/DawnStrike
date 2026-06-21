# Dawnstrike Web Telegram Auto-Pilot Audit - 2026-06-20

Scope: build the notification-only Web Auto-Pilot layer with safe web/free
source collection, SEC/halt enrichment, optional AI research summarization,
Telegram-ready notifications, dashboard visibility, offline tests, scripts, and
operator docs.

Result: PASS for local/offline validation. The workflow remains research and
watchlist only.

## Files Changed

- `config/web_sources.example.yaml`
- `config/telegram.example.env`
- `.env.example`
- `.gitignore`
- `intraday_scanner/providers/web_source_base.py`
- `intraday_scanner/providers/public_table_provider.py`
- `intraday_scanner/providers/nasdaq_symbol_provider.py`
- `intraday_scanner/providers/nasdaq_halt_provider.py`
- `intraday_scanner/providers/sec_edgar_provider.py`
- `intraday_scanner/providers/rss_provider.py`
- `intraday_scanner/services/web_collection_service.py`
- `intraday_scanner/services/ai_research_service.py`
- `intraday_scanner/ai/research_prompts.py`
- `intraday_scanner/cli.py`
- `intraday_scanner/storage/sqlite_store.py`
- `intraday_scanner/services/free_shadow_mode.py`
- `intraday_scanner/dashboard/data_loader.py`
- `app.py`
- `scripts/run_web_telegram_daemon.bat`
- `scripts/run_web_telegram_once.bat`
- `scripts/register_web_telegram_automation.ps1`
- `tests/test_web_autopilot.py`
- `tests/fixtures/web_sources_fixture.yaml`
- `tests/fixtures/public_table_fixture.html`
- `tests/fixtures/public_table_missing_fields.html`
- `tests/fixtures/nasdaq_symbols_fixture.txt`
- `tests/fixtures/halt_rss_fixture.xml`
- `tests/fixtures/sec_submissions_fixture.json`
- `tests/fixtures/empty_screener_inbox/.gitkeep`
- `docs/WEB_AUTO_PILOT.md`
- `docs/TELEGRAM_NOTIFICATIONS.md`
- `docs/URL_INGESTION.md`
- `docs/E2E_AUTOMATION.md`
- `docs/NOTIFICATION_ONLY_WORKFLOW.md`
- `docs/DATA_QUALITY.md`
- `README.md`

Existing previous automation files remain in the worktree and are not reverted.

## Commands Run

```powershell
py -m pip install -e ".[dev]"
py -m pytest tests/test_web_autopilot.py -q
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall intraday_scanner app.py tests
py -m intraday_scanner.cli web-ingest-public-table --url https://allowed.test/fixture --config tests\fixtures\web_sources_fixture.yaml --db-path data\web_auto_test.sqlite --out-dir outputs\web_ingest_test --persist --print
py -m intraday_scanner.cli web-auto-collect --config tests\fixtures\web_sources_fixture.yaml --db-path data\web_auto_test.sqlite --out-dir outputs\web_auto_test --persist --print
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\web_auto_test.sqlite
py -m intraday_scanner.cli web-telegram-daemon --config tests\fixtures\web_sources_fixture.yaml --automation-config config\automation.example.yaml --db-path data\web_auto_test.sqlite --out-root outputs\web_telegram_test --ai-mode none --notify console --dry-run --max-cycles 1
```

Dashboard smoke:

```powershell
Invoke-WebRequest http://127.0.0.1:8502/
```

Result: HTTP 200 with Streamlit marker. The in-app browser plugin failed during
setup, so the dashboard smoke used a direct local HTTP check.

## Test Results

- `py -m pytest -p no:cacheprovider`: 100 passed.
- `py -m ruff check .`: all checks passed.
- `py -m mypy intraday_scanner`: success, 71 source files.
- `py -m compileall intraday_scanner app.py tests`: success.

## Web Source Behavior

- URL fetches allow only `http` and `https`.
- Domains must match the configured allowlist unless `--allow-unlisted-url` is
  explicitly passed.
- Fixture paths bypass network for offline tests.
- Public table extraction scores tables by ticker/symbol, price, volume, gap,
  float, market cap, headline, high, low, and previous close columns.
- Rows missing required market fields are skipped/reported. Dawnstrike does not
  invent price, volume, float, catalyst, halt status, or returns.
- Raw artifacts are persisted when `save_raw: true`.
- Source health and data-quality reports are persisted for success and failure.

## AI Behavior

- Default `--ai-mode none` is rule-only and uses supplied text.
- `codex-cli` mode fails clearly when the `codex` executable is missing.
- `openai-api` requires `OPENAI_API_KEY`.
- AI outputs are validated against required columns and never overwrite raw
  market fields.

## Telegram Behavior

- `telegram-test --dry-run` works without secrets and persists a deduped
  notification attempt.
- Real Telegram sends require `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- Messages use research labels such as `WATCH`, `HIGH VOLATILITY WATCH`,
  `BREAKOUT TRIGGER`, `CAUTION`, and `OUTCOME NEEDED`.
- Secrets are not printed.

## Data Quality Limitations

- Public URL data is labeled `web_url` and `url_table_unverified`.
- SEC/halt/news enrichment is attached only when data is actually collected.
- Missing live current-price data triggers a manual monitor notification.
- Shadow returns require manual/free outcome rows unless a real outcome source
  is later configured.

## Outputs Generated

- `outputs\web_ingest_test\extracted_tables.csv`
- `outputs\web_ingest_test\premarket_snapshot.csv`
- `outputs\web_ingest_test\extraction_summary.json`
- `outputs\web_auto_test\premarket_snapshot.csv`
- `outputs\web_auto_test\source_summary.json`
- `outputs\web_auto_test\data_quality_report.json`
- `outputs\web_telegram_test\2026-06-20\collect\premarket_snapshot.csv`
- `outputs\web_telegram_test\2026-06-20\scan\ranked_candidates.csv`
- `outputs\web_telegram_test\2026-06-20\web_telegram_cycle_summary.json`
- `logs\web_telegram_2026-06-20.log`

## SQLite Proof Counts

Database: `data\web_auto_test.sqlite`

- `web_fetch_runs`: 7
- `web_fetch_results`: 7
- `source_health`: 9
- `raw_source_artifacts`: 5
- `normalized_source_rows`: 10
- `halt_events`: 1
- `sec_risk_events`: 0 for NOVA/RIFT fixture run
- `ai_research_runs`: 1
- `ai_research_outputs`: 1
- `ai_data_warnings`: 1
- `scan_runs`: 1
- `ranked_candidates`: 1
- `avoid_list`: 1
- `notifications_sent`: 1
- `automation_runs`: 3

## No-Trading Safety

Implementation-only safety search:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation" intraday_scanner app.py scripts
```

Result: no matches.

The broader search matched only test guard strings.

## What Works With Zero Keys

- Local inbox collection.
- Fixture/offline public table ingestion.
- Nasdaq symbol/halt parsing when configured with fixtures or allowed public
  access.
- SEC fixture parsing and real SEC collection when allowed and reachable.
- Scanner run and persisted official call.
- Console and dry-run Telegram notifications.
- Dashboard visibility.

## What Requires Secrets

- Real Telegram sends require `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
- Optional OpenAI summarization requires `OPENAI_API_KEY`.
- Optional news APIs require their provider keys.

## What Still Requires Paid/Live Data

- Reliable five-minute current-price monitoring.
- Provider-grade real-time volume, spread, float, short interest, and outcome
  capture.
- Production live validation beyond manual/free shadow results.

## Windows Commands

Run once:

```powershell
scripts\run_web_telegram_once.bat
```

Run daemon:

```powershell
scripts\run_web_telegram_daemon.bat
```

Register a scheduled task:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_web_telegram_automation.ps1
```

Dry-run without Telegram secrets:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.example.yaml --automation-config config\automation.example.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify console --dry-run --max-cycles 1
```

Telegram route test:

```powershell
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite
```

## Acceptance Criteria

- Automatically collects from configured safe local/web sources: PASS.
- Normalizes and scans without manual file commands: PASS.
- Sends Telegram-ready top-pick messages: PASS.
- Sends failure/reminder/manual-monitor messages: PASS.
- Persists official calls and notification attempts: PASS.
- Does not fabricate unavailable data: PASS.
- Does not place trades: PASS.
- Offline tests pass with mocked/fixture responses: PASS.
