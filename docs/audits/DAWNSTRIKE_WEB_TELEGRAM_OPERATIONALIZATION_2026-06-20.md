# Dawnstrike Web Telegram Operationalization - 2026-06-20

Scope: operations/setup/verification pass for local notification-only
operation. This pass did not add broker execution, trading credentials, fake
returns, or website bypass behavior.

Result: PASS for local console/dry-run operation. Real Telegram is
`BLOCKED_BY_SECRETS` because `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` are not
present.

## Git Safety

- Repo root: `C:\Users\MattFields\Dawnstrike`
- Branch/status before commit: `main...origin/main` with scoped source/docs/test/script changes
- Remote origin:
  - fetch: `https://github.com/mattfren/DawnStrike.git`
  - push: `https://github.com/mattfren/DawnStrike.git`
- `.gitignore` excludes:
  - `.env`
  - `.venv/`
  - `outputs/`
  - `logs/`
  - `data/*.sqlite`
  - `data/raw/`
  - `data/cache/`
  - `__pycache__/`
  - `.streamlit/secrets.toml`
  - local `config/web_sources.yaml`
  - local `config/automation.yaml`
- Runtime DB and generated output/log files are ignored.
- Local operational configs were created but are intentionally not committed.

## Local Configs

Created local ignored files when missing:

- `config\web_sources.yaml`
- `config\automation.yaml`

Safe local settings:

- local inbox source enabled
- public URL table sources disabled
- SEC/halt live fetches disabled until `YOUR_EMAIL_HERE` is replaced in the user-agent
- `respect_robots: true`
- `timeout_seconds: 15`
- `rate_limit_seconds: 5`
- `save_raw: false`
- automation notifications default to console only
- manual outcome reminders remain enabled

Updated `config\telegram.example.env` with non-secret setup instructions.

## Environment Readiness

`.env`: missing.

Optional environment keys present:

- `TELEGRAM_BOT_TOKEN`: missing
- `TELEGRAM_CHAT_ID`: missing
- `OPENAI_API_KEY`: missing
- `NEWS_API_KEY`: missing
- `FINNHUB_API_KEY`: missing
- `DISCORD_WEBHOOK_URL`: missing
- `SMTP_HOST`: missing
- `SMTP_PORT`: missing
- `SMTP_USER`: missing
- `SMTP_PASSWORD`: missing

No secret values were printed.

## Verification Commands

```powershell
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall intraday_scanner app.py tests
```

Results:

- editable install: PASS
- pytest: `100 passed`
- Ruff: PASS
- mypy: PASS, 71 source files
- compileall: PASS

## Database Setup

Operational DB was missing and was created with:

```powershell
py -m intraday_scanner.cli init-db --db-path data\shadow_real.sqlite
```

The SQLite file is ignored and was not staged.

## Telegram

Dry-run:

```powershell
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
```

Result: PASS. A deduped dry-run Telegram notification attempt was persisted.

Real Telegram:

```text
REAL_TELEGRAM_SEND_BLOCKED_BY_MISSING_TELEGRAM_SECRETS
```

Required keys:

- `TELEGRAM_BOT_TOKEN`
- `TELEGRAM_CHAT_ID`

## Web Telegram Dry-Run

Command:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify console --dry-run --max-cycles 1
```

Result: PASS.

Observed behavior:

- local inbox checked
- no source rows found
- no fake market data generated
- no crash
- no-source notification emitted
- dry-run notification attempt persisted with `dry_run: true`
- log written to `logs\web_telegram_2026-06-20.log`

## Local Inbox End-To-End Fixture

Fixture copied:

```powershell
tests\fixtures\raw_screener_aliases.csv -> data\inbox\screener\automation_smoke_2026-06-20.csv
```

Command:

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify console --max-cycles 1
```

Result: PASS.

Evidence:

- source found: `local_inbox`
- rows normalized: 4
- scan run persisted: top ticker `NOVA`
- ranked candidates persisted: 3
- top explosive persisted: 1
- avoid list persisted: 1
- top-pick notification persisted
- risk/avoid notification persisted for `HALT`
- manual-monitor notification emitted because no current-price source exists
- outcome-needed notification emitted
- source archived to:
  `data\processed\screener\automation_smoke_2026-06-20_web_processed.csv`
- original inbox file removed
- no broker/order execution path used

## Outcome Reminder

Command:

```powershell
py -m intraday_scanner.cli automation-outcomes --db-path data\shadow_real.sqlite --out-root outputs\automation --notify
```

Result: PASS.

Evidence:

- status: `missing`
- reminder path: `data\inbox\outcomes\outcomes_2026-06-20.csv`
- required columns printed
- notifications persisted for missing outcome, lunch reminder, and close reminder
- no fake returns generated

## Dashboard Smoke

HTTP check:

```powershell
Invoke-WebRequest http://127.0.0.1:8502/
```

Result: HTTP 200 with Streamlit marker.

Dashboard loader proof from `data\shadow_real.sqlite`:

- automation status: present
- web source status: present
- latest notification: present
- latest top picks: present
- missing outcomes: present
- data-quality warnings/status: present
- output paths: present
- top ticker: `NOVA`

The dashboard retains the research-only/no-trading boundary text.

## Windows Scripts

Verified scripts exist and use `py -m intraday_scanner.cli`:

- `scripts\run_web_telegram_once.bat`
- `scripts\run_web_telegram_daemon.bat`
- `scripts\register_web_telegram_automation.ps1`
- `scripts\run_automation_once.bat`
- `scripts\run_automation_daemon.bat`

Safe script check:

```powershell
scripts\run_web_telegram_once.bat
```

Result: PASS. With no Telegram secrets and no inbox file, the script completed
without a crash and did not send a real Telegram message.

Exact local commands:

```powershell
scripts\run_web_telegram_once.bat
scripts\run_web_telegram_daemon.bat
powershell -ExecutionPolicy Bypass -File scripts\register_web_telegram_automation.ps1
```

Scheduled task registration was not run automatically.

## SQLite Row Counts

Database: `data\shadow_real.sqlite`

- `scan_runs`: 1
- `ranked_candidates`: 3
- `top_explosive`: 1
- `avoid_list`: 1
- `notifications_sent`: 10
- `automation_runs`: 8
- `web_fetch_runs`: 1
- `source_health`: 7

## Output Paths Generated

- `outputs\web_telegram\2026-06-20\collect\premarket_snapshot.csv`
- `outputs\web_telegram\2026-06-20\collect\local_inbox\premarket_snapshot.csv`
- `outputs\web_telegram\2026-06-20\scan\ranked_candidates.csv`
- `outputs\web_telegram\2026-06-20\scan\top_explosive.csv`
- `outputs\web_telegram\2026-06-20\scan\avoid_list.csv`
- `outputs\web_telegram\2026-06-20\web_telegram_cycle_summary.json`
- `outputs\automation\2026-06-20\outcomes\outcome_summary.json`
- `logs\web_telegram_2026-06-20.log`

All output/log/SQLite artifacts are ignored and not staged.

## No-Trading Safety

Implementation safety search:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation" intraday_scanner app.py scripts
```

Result: PASS, no implementation matches.

The software remains research/watchlist only.

## Staged-File Safety

Required staged-file safety check result before commit: PASS.

Confirmed no staged:

- `.env`
- `*.sqlite`
- `outputs/`
- `logs/`
- `data/raw/`
- `data/cache/`
- `.streamlit/secrets.toml`

## Still Requires Secrets

- real Telegram: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`
- optional AI summaries: `OPENAI_API_KEY`
- optional news enrichment: `NEWS_API_KEY`, `FINNHUB_API_KEY`, or other provider keys
- optional email/Discord: SMTP/Discord env vars

## Still Requires Paid/Live Data

- reliable current-price monitoring
- provider-grade outcome capture
- full tape/volume/spread validation

## Acceptance Criteria

- Git repo is safe: PASS
- Tests/lint/typecheck pass: PASS
- Dry-run notification works: PASS
- Local inbox automation works: PASS
- Real Telegram works if secrets exist: BLOCKED by missing secrets
- Generated artifacts and secrets are not committed: PASS
- Dashboard loads: PASS
- No order execution exists: PASS
- Final report written: PASS
