# Dawnstrike E2E Notification Automation - 2026-06-20

Scope: build complete end-to-end zero/low-cost automation so Dawnstrike can run
as notification-only research/watchlist and paper-validation software.

Result: PASS for the zero-key notification-only automation layer.

Safety boundary: no broker order execution, no broker trading credentials, no
paid/live provider call by default, no fake returns, no lookahead bypass, and no
performance claim from manual/free data.

## Files Changed

- `.gitignore`
- `README.md`
- `app.py`
- `config\automation.example.yaml`
- `docs\E2E_AUTOMATION.md`
- `docs\NOTIFICATION_ONLY_WORKFLOW.md`
- `docs\URL_INGESTION.md`
- `docs\WINDOWS_TASK_SCHEDULER_AUTOMATION.md`
- `docs\FREE_SHADOW_MODE.md`
- `docs\SCREENER_AUTOMATION.md`
- `intraday_scanner\cli.py`
- `intraday_scanner\dashboard\data_loader.py`
- `intraday_scanner\notifiers\__init__.py`
- `intraday_scanner\notifiers\service.py`
- `intraday_scanner\notifiers\windows.py`
- `intraday_scanner\services\e2e_automation_service.py`
- `intraday_scanner\storage\sqlite_store.py`
- `scripts\run_automation_once.bat`
- `scripts\run_automation_daemon.bat`
- `scripts\register_dawnstrike_automation.ps1`
- `tests\test_e2e_automation.py`
- `data\inbox\outcomes\.gitkeep`
- `data\processed\outcomes\.gitkeep`
- `data\failed\outcomes\.gitkeep`

## Implemented

- `automation-run --mode once|daemon|dry-run`
- `automation-morning`
- `automation-monitor-open`
- `automation-outcomes`
- `automation-summary`
- `automation-daemon`
- `url-ingest-screener`
- local screener inbox source priority
- safe URL ingestion policy and command
- official call persistence before outcomes
- notification event persistence/dedupe
- missing source notifications
- top-pick and avoid-warning notifications
- manual monitor required notification when no current-price source exists
- outcome missing reminder with exact template path
- outcome import, archive, audit, report, and summary notification
- daemon dry-run/max-cycle path
- Windows Task Scheduler scripts
- optional Windows local notifier via BurntToast when explicitly configured
- dashboard automation status and latest notification visibility

## Verification Commands

| Command | Result |
| --- | --- |
| `py -m pip install -e ".[dev]"` | PASS |
| `py -m pytest -p no:cacheprovider` | PASS; 83 tests |
| `py -m ruff check .` | PASS |
| `py -m mypy intraday_scanner` | PASS; 62 source files |
| `py -m compileall intraday_scanner app.py tests` | PASS |
| `py -m streamlit run app.py --server.port 8502` | PASS; existing server returned HTTP 200 |

## Automation Flow Proof

Seeded raw screener fixture:

```powershell
Copy-Item tests\fixtures\raw_screener_aliases.csv data\inbox\screener\automation_verify_2026-06-20.csv
```

Morning:

```powershell
py -m intraday_scanner.cli automation-morning --config config\automation.example.yaml --db-path data\automation_test.sqlite --out-root outputs\automation_test --notify
```

Result:

- source: `data\inbox\screener\automation_verify_2026-06-20.csv`
- normalized rows: 4
- official call timestamp: `2026-06-20T23:25:22+00:00`
- ranked: 3
- avoid: 1
- top ticker: `NOVA`
- source archived to `data\processed\screener`
- notifications sent for started, source found, scan completed, top picks, and avoid warning

Market-open monitor:

```powershell
py -m intraday_scanner.cli automation-monitor-open --db-path data\automation_test.sqlite --out-root outputs\automation_test --max-iterations 1 --notify
```

Result: PASS. With no reliable current-price source configured, automation sent
and persisted a `manual_monitor_required` monitor event instead of fabricating
prices.

Outcome missing:

```powershell
py -m intraday_scanner.cli automation-outcomes --db-path data\automation_test.sqlite --out-root outputs\automation_test --notify
```

Result: PASS. Automation created/surfaced
`data\inbox\outcomes\outcomes_2026-06-20.csv` and sent outcome missing, lunch
reminder, and close reminder notifications for `NOVA`, `RIFT`, and `WIDE`.

Outcome present:

- Wrote outcome rows with entry timestamps after the saved official call.
- Reran `automation-outcomes`.

Result:

- imported: 3
- archived to `data\processed\outcomes`
- audited rows: 3
- generated `free_shadow_report.json`
- sent `audit_completed`

Daily summary:

```powershell
py -m intraday_scanner.cli automation-summary --db-path data\automation_test.sqlite --out-root outputs\automation_test --notify
```

Result: PASS. Latest summary had `outcomes_available=true`,
`missing_outcome_count=0`, top 3 `NOVA`, `RIFT`, `WIDE`, and top3 equal-weight
manual shadow return `5.0%`. This is a manual/free fixture proof, not a live
performance claim.

Daemon dry-run:

```powershell
py -m intraday_scanner.cli automation-daemon --config config\automation.example.yaml --db-path data\automation_test.sqlite --out-root outputs\automation_test --dry-run --max-cycles 1 --notify
```

Result: PASS. One dry-run cycle wrote `logs\automation_2026-06-20.log` and
reported planned stages: morning, monitor_open, outcomes, summary.

Top-level wrapper:

- `automation-run --mode once` is covered by offline tests and runs morning,
  monitor, outcomes, and summary in sequence.
- `automation-run --mode dry-run` is covered by offline tests and routes through
  the daemon dry-run path.

## Notification Proof

`data\automation_test.sqlite` after proof:

- `notifications_sent`: 13
- `monitor_events`: 1
- `alerts_sent`: 0

Latest persisted notifications included:

- `daily_summary`
- `audit_completed`
- `close_reminder`
- `lunch_reminder`
- `outcome_missing`

Notification dedupe was tested offline. Daily-summary dedupe now includes outcome
state/report timestamp so a no-outcomes summary cannot suppress a later
outcomes-available summary.

## SQLite Row Counts

From `data\automation_test.sqlite`:

- `scan_runs`: 1
- `ranked_candidates`: 3
- `top_explosive`: 1
- `avoid_list`: 1
- `notifications_sent`: 13
- `monitor_events`: 1
- `automation_runs`: 7
- `manual_outcomes`: 3
- `manual_audit_trades`: 3
- `manual_audit_summary`: 1
- `shadow_reports`: 1
- `provider_health`: 31

## Output Files Generated

- `outputs\automation_test\2026-06-20\morning\premarket_snapshot.csv`
- `outputs\automation_test\2026-06-20\morning\ranked_candidates.csv`
- `outputs\automation_test\2026-06-20\morning\top_explosive.csv`
- `outputs\automation_test\2026-06-20\morning\avoid_list.csv`
- `outputs\automation_test\2026-06-20\morning\run_summary.json`
- `outputs\automation_test\2026-06-20\outcomes\outcome_reminder.json`
- `outputs\automation_test\2026-06-20\outcomes\outcome_summary.json`
- `outputs\automation_test\2026-06-20\outcomes\audit\manual_audit_trades.csv`
- `outputs\automation_test\2026-06-20\outcomes\audit\manual_audit_summary.json`
- `outputs\automation_test\2026-06-20\outcomes\shadow_report\free_shadow_report.json`
- `outputs\automation_test\2026-06-20\summary\daily_summary.json`
- `logs\automation_2026-06-20.log`

## Dashboard Smoke

Streamlit on `http://127.0.0.1:8502/` returned HTTP 200. Dashboard loader tests
also verify automation status, latest run, latest notification, and health data.

## No-Trading Safety Result

Implementation-only search:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission" intraday_scanner app.py scripts
```

Result: PASS. No implementation matches.

Broad search matched only docs/audit/test text that explicitly warns against
order execution.

## Zero-Key Behavior

Works with zero secrets:

- local inbox screener automation
- deterministic normalization
- Free Shadow scan
- official call persistence
- console notifications
- missing source notification
- top-pick notification
- avoid warning notification
- manual monitor required notification
- outcome reminder
- outcome import/audit/report when CSV is provided
- daily summary
- dashboard status
- daemon dry-run

Improves with optional secrets:

- Discord webhook notifications
- Telegram notifications
- SMTP email notifications
- optional Windows local notifications if BurntToast is installed

Still requires paid/live data later:

- reliable live current-price monitoring
- live-grade catalyst/news validation
- broad paid/common-stock universe coverage
- provider-validated performance analysis

## Exact Start Commands

One pass:

```powershell
scripts\run_automation_once.bat
```

Daemon:

```powershell
scripts\run_automation_daemon.bat
```

Register at login:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_dawnstrike_automation.ps1 -AtLogon
```

Register market weekdays:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\register_dawnstrike_automation.ps1 -MarketDays
```

## Environment Notes

- `.env`: not present
- `.streamlit\secrets.toml`: not present
- Git status: valid repo on `main...origin/main` with expected modified/new
  files from this implementation pass.

## Acceptance Criteria

- Drop screener file and receive top-pick notification: PASS
- No screener source sends failure notification: PASS
- Missing outcomes send reminder: PASS
- Present outcomes import, audit, report, notify, and archive: PASS
- Official calls and notifications persisted: PASS
- Dashboard reflects automation state: PASS
- Offline tests pass: PASS
- No secrets required: PASS
- No order execution path: PASS
