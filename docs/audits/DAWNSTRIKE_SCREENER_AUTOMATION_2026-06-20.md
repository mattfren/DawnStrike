# Dawnstrike Screener Automation - 2026-06-20

Scope: build and validate the zero-dollar Free Shadow Mode screener automation
layer. This pass adds raw screener import, deterministic normalization, optional
AI fallback plumbing, inbox watching, daily automation, dashboard visibility,
Windows shortcuts, documentation, and offline tests.

Result: PASS for the zero-secrets screener automation workflow.

Safety boundary: Dawnstrike remains research/watchlist and paper-validation
software. No broker order execution, broker trading credential storage, paid
market-data call, or return guarantee was added.

## Implemented

- `intraday_scanner/services/screener_automation.py`
  - Creates `data\inbox\screener`, `data\processed\screener`,
    `data\failed\screener`, `data\manual`, `outputs\auto_shadow`, and `logs`.
  - Parses `.csv`, `.tsv`, and copied text tables.
  - Maps common aliases for ticker, company, price, previous close, high, low,
    volume, catalyst/headline, URL, and timestamp.
  - Computes `dollar_volume` and `gap_pct`.
  - Labels normalized rows with `data_source_kind=manual`, `shadow_mode=true`,
    `paid_data=false`, and `manual_uploaded_data=true`.
  - Preserves `raw_file_path` and `imported_at`.
  - Leaves unknown enrichment blank and records data-quality warnings.
  - Archives successful raw files to `data\processed\screener`.
  - Archives failed raw files to `data\failed\screener`.
  - Skips duplicate file hashes already persisted in SQLite.
  - Writes `run_summary.json`, normalized snapshots, ranked picks, top picks,
    avoid rows, and log events.

- `intraday_scanner/cli.py`
  - `normalize-screener-file`
  - `auto-shadow-from-screener`
  - `watch-screener-inbox`
  - `auto-shadow-daily`

- `intraday_scanner/storage/sqlite_store.py`
  - Added `screener_automation_runs`.
  - Added file-hash lookup, persist, and load helpers.

- `intraday_scanner/models.py`
  - Added `raw_file_path` and `imported_at` to snapshots and scored candidates.

- Dashboard
  - SQLite loader now exposes automation runs and automation status.
  - Free Shadow panel shows inbox path, processed/failed counts, latest raw
    file, latest normalized snapshot, latest run summary, top picks, avoid rows,
    and data warnings.

- Windows shortcuts
  - `scripts\run_auto_shadow_once.ps1`
  - `scripts\watch_screener_inbox.ps1`
  - `scripts\run_daily_shadow_scan.bat`

- Fixtures
  - `tests\fixtures\raw_screener_aliases.csv`
  - `tests\fixtures\raw_screener_messy.txt`

- Docs
  - `docs\SCREENER_AUTOMATION.md`
  - Updated `docs\FREE_SHADOW_MODE.md`
  - Updated `docs\MANUAL_UPLOADS.md`
  - Updated `README.md`

## AI Fallback Boundary

Default is `--ai-normalizer none`.

`--ai-normalizer codex-cli` checks for the local Codex CLI, uses the checked-in
ChatGPT/Codex screener prompt, runs in a read-only/no-approval mode where the CLI
supports it, and validates the returned CSV before scoring.

`--ai-normalizer openai-api` is stubbed in this zero-secrets build. It requires an
explicit `OPENAI_API_KEY` and then stops with a clear error. The test suite does
not call the network.

## Verification

| Check | Result |
| --- | --- |
| `py -m pip install -e ".[dev]"` | PASS |
| `py -m pytest -q` | PASS; 70 tests |
| `py -m ruff check .` | PASS |
| `py -m mypy intraday_scanner app.py` | PASS; 61 source files |
| `py -m compileall intraday_scanner app.py tests` | PASS |
| `py -m pytest tests\test_streamlit_app.py -q` | PASS; AppTest no exception |

## Command Proof

Normalize fixture:

```powershell
py -m intraday_scanner.cli normalize-screener-file --input tests\fixtures\raw_screener_aliases.csv --out outputs\normalized_screener_test --db-path data\shadow.sqlite --ai-normalizer none
```

Result: PASS. Deterministic parser normalized 4 rows, wrote
`outputs\normalized_screener_test\premarket_snapshot.csv`, average data quality
100.0, `missing_enrichment_count=5`, no warnings.

Auto-shadow one file:

```powershell
py -m intraday_scanner.cli auto-shadow-from-screener --input tests\fixtures\raw_screener_aliases.csv --db-path data\shadow.sqlite --out-dir outputs\auto_shadow_test --ai-normalizer none --persist --print
```

Result: PASS. Run `540d8807-fdca-4616-829d-227db547e287`; scan
`7410b3e2-7291-421e-bb3f-7acd40c8858a`; top ticker `NOVA`; 4 candidates,
3 ranked, 1 avoid. Raw file archived to
`data\processed\screener\raw_screener_aliases_processed_cfbf272067ee.csv`.
The tracked fixture was restored after the command because archiving raw inputs
is expected behavior.

Watch inbox:

```powershell
py -m intraday_scanner.cli watch-screener-inbox --inbox data\inbox\screener --db-path data\shadow.sqlite --out-root outputs\auto_shadow --ai-normalizer none --max-files 1 --poll-seconds 1
```

Result: PASS. The watcher processed one copied raw text table, stopped cleanly,
and archived the raw file to
`data\processed\screener\watch_test_2026-06-20_processed_074da460f14c.txt`.
Top ticker from that text fixture was `MESS`.

SQLite evidence from `data\shadow.sqlite` after proof:

- `scan_runs`: 3
- `ranked_candidates`: 7
- `avoid_list`: 2
- `manual_snapshot_uploads`: 4
- `screener_automation_runs`: 2

Latest persisted automation runs:

- `success` -> `outputs\auto_shadow\2026-06-20\watch_test_2026-06-20\normalized\premarket_snapshot.csv`
- `success` -> `outputs\auto_shadow_test\normalized\premarket_snapshot.csv`

## Test Coverage Added

`tests\test_screener_automation.py` covers:

- deterministic alias parsing
- `dollar_volume` calculation
- `gap_pct` calculation
- unknown enrichment remains blank
- `normalize-screener-file`
- auto-shadow persistence
- processed-file archive
- failed-file archive
- duplicate hash skip
- finite inbox watcher
- daily automation path
- missing Codex CLI clear error
- malformed Codex CLI output rejection
- dashboard loader automation status

## Safety Search

Search terms checked:

- `submit_order`
- `place_order`
- `create_order`
- `TradingClient`
- `alpaca.trading`
- broker credential/order execution terms

Result: PASS. Matches were documentation/audit warnings only. No implementation
path was found for order placement or broker trading credential storage.

Network/API check for the new automation surface found no `requests`, `httpx`, or
`urlopen` usage. The only `OPENAI_API_KEY` mention is the explicit stubbed
`openai-api` normalizer branch and its docs.

## Environment Notes

- `.env` was not present.
- `.streamlit\secrets.toml` was not present.
- `git status --short --branch` could not run because this workspace is not
  currently a Git repository: `fatal: not a git repository`.
- Generated proof files under `outputs\`, `logs\`, and `data\*.sqlite` are
  ignored by `.gitignore`.

## Production Readiness Statement

The Free Shadow screener automation is ready for zero-dollar paper testing from
raw screener exports. It can normalize files, run the scoring engine, persist the
paper call, archive the raw input, update the dashboard, and support a finite or
continuous inbox watcher.

It is not a live trading system and does not claim future performance. Real
accuracy can only be evaluated after repeated shadow runs plus manually imported
outcomes.
