# Dawnstrike Non-Secret Gap Closure - 2026-06-20

Scope: targeted implementation pass against `docs/audits/DAWNSTRIKE_REPO_AUDIT_2026-06-20.md`.

Safety boundary: no auto-trading, no broker order execution, no performance claims. Dawnstrike remains a research/watchlist and paper-trading platform.

## Files Changed

Core code:
- `.env.example`
- `app.py`
- `intraday_scanner/cli.py`
- `intraday_scanner/config.py`
- `intraday_scanner/dashboard/data_loader.py`
- `intraday_scanner/models.py`
- `intraday_scanner/paper_audit.py`
- `intraday_scanner/scheduler.py`
- `intraday_scanner/services/audit_service.py`
- `intraday_scanner/services/enrichment_service.py`
- `intraday_scanner/services/historical_ingestion_service.py`
- `intraday_scanner/services/market_calendar.py`
- `intraday_scanner/services/mover_discovery_service.py`
- `intraday_scanner/services/performance_service.py`
- `intraday_scanner/services/scan_service.py`
- `intraday_scanner/storage/sqlite_store.py`
- `intraday_scanner/providers/alpaca_movers_provider.py`
- `intraday_scanner/providers/csv_enrichment_provider.py`
- `intraday_scanner/providers/csv_movers_provider.py`
- `intraday_scanner/providers/enrichment_base.py`
- `intraday_scanner/providers/movers_base.py`
- `intraday_scanner/providers/sec_enrichment_provider.py`

Tests and fixtures:
- `tests/test_non_secret_gap_closure.py`
- `tests/test_dashboard_data_loader.py`
- `sample_data/universe_sample.csv`

Docs:
- `README.md`
- `docs/GIT_REPAIR.md`
- `docs/WINDOWS_SETUP.md`
- `docs/UNIVERSE_DISCOVERY.md`
- `docs/ENRICHMENT.md`
- `docs/HISTORICAL_INGESTION.md`
- `docs/HISTORICAL_AUDIT.md`
- `docs/OPERATIONS.md`
- `docs/SCHEDULER_WINDOWS.md`
- `docs/SECRETS_REQUIRED.md`

## Commands Run

| Command | Result |
|---|---|
| `py -m pip install -e ".[dev]"` | PASS; installed dev extras into system Python. Scripts path still warns not on PATH. |
| `py -m compileall intraday_scanner` | PASS |
| `py -m pytest -p no:cacheprovider` | PASS; `53 passed in 6.77s` final run. |
| `py -m ruff check .` | PASS; all checks passed. |
| `py -m mypy intraday_scanner` | PASS; no issues in 58 source files. |
| `py -m intraday_scanner.cli --help` | PASS |
| `py -m intraday_scanner.cli init-db --db-path data\scanner_audit.sqlite` | PASS |
| `py -m intraday_scanner.cli scan --snapshot sample_data\premarket_snapshot_sample.csv --out-dir outputs\audit_scan --db-path data\scanner_audit.sqlite --persist --print` | PASS; ranked=4, avoid=4, top=NOVA. |
| `py -m intraday_scanner.cli live-scan --provider alpaca --universe-file sample_data\universe_sample.csv --db-path data\scanner_audit.sqlite --out-dir outputs\audit_live --persist --print` | BLOCKED as expected; missing Alpaca credentials, no keys logged. Universe-file path is accepted. |
| `py -m intraday_scanner.cli monitor-open --snapshot sample_data\premarket_snapshot_sample.csv --db-path data\scanner_audit.sqlite --out-dir outputs\audit_monitor --persist --max-iterations 1` | PASS; monitor checks written, deduped alert path exercised. |
| `py -m intraday_scanner.cli audit-latest ... --entry-mode open` | PASS |
| `py -m intraday_scanner.cli audit-latest ... --entry-mode breakout` | PASS |
| `py -m intraday_scanner.cli performance-report --db-path data\scanner_audit.sqlite --persist` | PASS; includes compounded equity curves. Fixture/sample data only. |
| `py -m intraday_scanner.cli notify-test --db-path data\scanner_audit.sqlite` | PASS; first run sent, second run skipped duplicate. |
| `py -m intraday_scanner.cli scheduler --json` | PASS; includes market-day, retry, skip reason. |
| `py -m intraday_scanner.cli ingest-minute-bars --input sample_data\minute_bars\2026-06-18.csv --out-dir outputs\audit_ingest` | PASS; fixture-only output. |
| `py -m intraday_scanner.cli backfill-snapshots ... --persist` | PASS; fixture-only historical snapshot and scan persisted. |
| Streamlit AppTest | PASS; zero exceptions, expected seven tabs. |
| HTTP check `http://127.0.0.1:8502/` | PASS; status 200. Existing server already running on PID 46612. |
| `git status --short --branch` | FAIL; existing `.git` metadata is still not recognized. Safe repair docs added. |

## Before / After Status

| Audit finding | Before | After | Notes |
|---|---|---|---|
| Git repo health broken | FAIL | DOCUMENTED/PARTIAL | Did not reinitialize Git destructively. Added `docs/GIT_REPAIR.md` with safe Windows repair commands. |
| Windows default operability | PARTIAL | PASS/PARTIAL | `py -m intraday_scanner.cli` works. Console scripts still require PATH; documented. |
| System Python lacks dev extras | FAIL | PASS | `py -m pip install -e ".[dev]"`, tests/lint/typecheck pass. |
| Console scripts not reliably on PATH | PARTIAL | DOCUMENTED/PARTIAL | Module-entry commands are now the recommended Windows path. |
| Full-market mover discovery missing | FAIL/PARTIAL | PARTIAL | Provider-agnostic mover discovery added. Live scans can use `--universe-file`. No fake market-wide discovery. |
| Alpaca live scan requires static symbols | PARTIAL | PARTIAL/PASS | Can now use `--symbols`, `--symbols-file`, or `--universe-file`. Secrets still required for real Alpaca calls. |
| Live snapshots lack enrichment | PARTIAL | PARTIAL | Enrichment architecture added for CSV and SEC-derived fields; unknowns remain honest. |
| Paper audit entry semantics ambiguous | PARTIAL | PASS | `--entry-mode open` and `--entry-mode breakout` added. `no_entry_trigger` status added. |
| Scheduler static/no calendar | PARTIAL | PARTIAL/PASS | Static US market holiday/early-close fallback, retry fields, skip reasons, failure health helpers added. |
| Historical ingestion/backfill missing | FAIL/PARTIAL | PARTIAL/PASS | `ingest-minute-bars` and `backfill-snapshots` added for local CSV/parquet-capable workflows. |
| Dashboard QA/drilldowns incomplete | PARTIAL | PARTIAL/PASS | AppTest passes; loader/UI now show source kind, readiness, provider counts, catalyst URL, audit status, entry mode. |
| Notify-test did not persist | PARTIAL | PASS | `notify-test --db-path` now uses dispatch/dedupe path and writes `notifications_sent`. |
| Candidate payload missing `catalyst_url` | FAIL/PARTIAL | PASS | Candidate CSV/payload and recommendation theses now carry `catalyst_url`. |

## Remaining Blockers

Secrets/live-market blockers:
- Real Alpaca live scan requires `ALPACA_API_KEY_ID` and `ALPACA_API_SECRET_KEY`.
- Real NewsAPI/Finnhub notification risk checks require their keys.
- Email/Discord/Telegram delivery requires channel secrets.
- Live behavior still needs market-hour verification with real data.

Non-secret/manual blocker:
- Git metadata is still invalid. It was not safe to reinitialize automatically because that can affect local history/state. `docs/GIT_REPAIR.md` contains the safe manual path.

Data-quality blockers that are now explicit rather than hidden:
- Alpaca alone may not supply float, market cap, short float, halt/offering, and catalyst fields.
- Full-market quality depends on the supplied universe file.
- Historical return quality depends on user-supplied point-in-time data.

## Are Remaining Blockers Secrets/Live-Market Only?

For app live operation: mostly yes. The scanner can now accept a universe file, fail cleanly without secrets, persist counts when live snapshots return, separate audit entry semantics, and show readiness data.

For repo hygiene: no. Git repair remains a manual non-secret task because the current `.git` directory is invalid and should not be rewritten automatically.

## Exact Next Windows Commands

```powershell
cd C:\Users\MattFields\Dawnstrike
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m intraday_scanner.cli init-db --db-path data\scanner_audit.sqlite
py -m intraday_scanner.cli scan --snapshot sample_data\premarket_snapshot_sample.csv --out-dir outputs\audit_scan --db-path data\scanner_audit.sqlite --persist --print
py -m intraday_scanner.cli live-scan --provider alpaca --universe-file sample_data\universe_sample.csv --db-path data\scanner_audit.sqlite --out-dir outputs\audit_live --persist --print
py -m intraday_scanner.cli monitor-open --snapshot sample_data\premarket_snapshot_sample.csv --db-path data\scanner_audit.sqlite --out-dir outputs\audit_monitor --persist --max-iterations 1
py -m intraday_scanner.cli audit-latest --db-path data\scanner_audit.sqlite --minute-bars sample_data\minute_bars\2026-06-18.csv --out-dir outputs\audit_latest --persist --entry-mode open
py -m intraday_scanner.cli audit-latest --db-path data\scanner_audit.sqlite --minute-bars sample_data\minute_bars\2026-06-18.csv --out-dir outputs\audit_latest_breakout --persist --entry-mode breakout
py -m intraday_scanner.cli performance-report --db-path data\scanner_audit.sqlite --persist
py -m intraday_scanner.cli notify-test --db-path data\scanner_audit.sqlite
py -m intraday_scanner.cli scheduler --json
py -m streamlit run app.py --server.port 8502
```

If repairing Git:

```powershell
cd C:\Users\MattFields\Dawnstrike
notepad docs\GIT_REPAIR.md
```

## No Order Execution

No broker order execution was added. No `submit_order`, `place_order`, `create_order`, broker trading client, or auto-trading path was introduced. Dawnstrike remains research/watchlist and paper-trading software only.
