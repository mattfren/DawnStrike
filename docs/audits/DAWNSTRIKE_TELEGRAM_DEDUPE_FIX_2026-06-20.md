# Dawnstrike Telegram Dedupe Fix - 2026-06-20

## Files changed

- `intraday_scanner/cli.py`
  - Added `telegram-test --force`.
  - Passed `force` only to the Telegram test helper.
- `intraday_scanner/services/web_collection_service.py`
  - Split test notification keys by mode:
    - `telegram_test:YYYY-MM-DD:telegram:dry_run`
    - `telegram_test:YYYY-MM-DD:telegram:real`
  - Added test-only force bypass using a unique persisted force key.
  - Persisted explicit `dry_run`, `send_attempted`, `status`, and `dedupe_bypassed` payload fields.
- `tests/test_web_autopilot.py`
  - Added offline Telegram sender tests for dry-run/real separation, real dedupe, force bypass, missing secrets, dry-run without secrets, and secret redaction.

## Commands run

- `py -m pytest tests\test_web_autopilot.py -k telegram -p no:cacheprovider`
- `py -m pip install -e ".[dev]"`
- `py -m pytest -p no:cacheprovider`
- `py -m ruff check .`
- `py -m mypy intraday_scanner`
- `py -m compileall intraday_scanner app.py tests`
- `py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite`
- `py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite`
- `py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite`
- `py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite --force`
- `rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation" intraday_scanner app.py scripts`

## Tests passed/failed

- Passed: targeted Telegram tests, `8 passed, 14 deselected`.
- Passed: full test suite, `105 passed`.
- Passed: Ruff, `All checks passed!`
- Passed: mypy, `Success: no issues found in 71 source files`.
- Passed: compileall for `intraday_scanner app.py tests`.
- Failed: none.

## Dry-run result

Command:

```text
py -m intraday_scanner.cli telegram-test --dry-run --db-path data\shadow_real.sqlite
```

Result:

```json
{
  "event_key": "telegram_test:2026-06-20:telegram:dry_run",
  "forced": false,
  "status": "dry_run"
}
```

Persisted row:

```json
{
  "channel": "telegram",
  "dry_run": true,
  "send_attempted": false,
  "status": "dry_run",
  "dedupe_bypassed": false
}
```

## Real-send result

Command:

```text
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite
```

Result:

```json
{
  "event_key": "telegram_test:2026-06-20:telegram:real",
  "forced": false,
  "status": "sent"
}
```

Persisted row:

```json
{
  "channel": "telegram",
  "dry_run": false,
  "send_attempted": true,
  "status": "sent",
  "dedupe_bypassed": false
}
```

## Duplicate result

Command:

```text
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite
```

Result:

```json
{
  "event_key": "telegram_test:2026-06-20:telegram:real",
  "status": "skipped_duplicate"
}
```

The dry-run key did not block the first real send. The second real send skipped because the real-send key already existed.

## Force result

Command:

```text
py -m intraday_scanner.cli telegram-test --db-path data\shadow_real.sqlite --force
```

Result:

```json
{
  "event_key": "telegram_test:2026-06-20:telegram:real:force:a15975564048",
  "forced": true,
  "status": "sent"
}
```

Persisted row:

```json
{
  "channel": "telegram",
  "dry_run": false,
  "send_attempted": true,
  "status": "sent",
  "dedupe_bypassed": true
}
```

## No-trading safety result

No broker/order implementation code was added or changed. Safety search over implementation paths returned no matches:

```text
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation" intraday_scanner app.py scripts
```

Exit code was `1`, meaning no matches.

## Notes

- No Telegram token or chat ID was printed by the CLI, tests, or persisted notification rows.
- `--force` is scoped to `telegram-test`; shared production notification dedupe remains unchanged.
- An older legacy row shaped like `telegram_test:YYYY-MM-DD:telegram` may remain in an existing local database, but it no longer controls the new dry-run or real-send dedupe keys.
