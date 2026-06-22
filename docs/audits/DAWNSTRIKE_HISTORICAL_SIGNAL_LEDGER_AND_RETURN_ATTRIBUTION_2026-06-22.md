# Dawnstrike Historical Signal Ledger And Return Attribution - 2026-06-22

## Summary

The historical signal ledger and return-attribution layer is wired into the
AlphaOps workflow without adding broker execution. AlphaOps now records
point-in-time historical signals, no-trade days, notification links, monitor
events, imported outcomes, return-attribution rows, daily performance rows, and
historical reports.

Current operating evidence still has `0` imported historical outcomes and `0`
audited historical days, so returns remain unavailable and the evidence label is
`Not enough history yet.`

## Files Changed

Main implementation files:

- `intraday_scanner/storage/sqlite_store.py`
- `intraday_scanner/services/return_attribution_service.py`
- `intraday_scanner/services/alpha_cycle_service.py`
- `intraday_scanner/services/free_shadow_mode.py`
- `intraday_scanner/cli.py`
- `intraday_scanner/dashboard/data_loader.py`
- `scripts/register_alphaops_tasks.ps1`
- `tests/test_historical_signal_ledger.py`
- `docs/HISTORICAL_SIGNAL_LEDGER.md`
- `docs/RETURN_ATTRIBUTION.md`
- `docs/HISTORICAL_CALENDAR.md`
- `docs/PERFORMANCE_AUDIT.md`
- `docs/OPERATOR_RUNBOOK.md`
- `docs/TELEGRAM_NOTIFICATIONS.md`
- `README.md`

Related dashboard/calendar files already present in the worktree:

- `intraday_scanner/services/calendar_report_service.py`
- `tests/test_historical_calendar.py`

## New Tables

Added to SQLite initialization:

- `historical_signals`
- `signal_events`
- `signal_outcomes`
- `signal_return_attribution`
- `daily_signal_performance`

These tables preserve point-in-time research state and do not store broker
orders.

## Signal Lifecycle

1. `alpha-cycle` collects source rows and scores AlphaOps candidates.
2. AlphaOps persists `alpha_signals`.
3. The ledger persists matching `historical_signals`.
4. Created/notified events are written to `signal_events`.
5. No-clean-edge days are recorded as no-trade historical rows.
6. Monitor checks can add later signal events such as invalidation or thesis
   break.
7. Outcome CSV import can match real outcome rows to the saved signal.
8. Attribution writes scenario/paper return rows.

## Entry And Exit Tracking

Entry policy:

- `first_available_after_signal`

Exit/scenario policies include:

- `one_min`
- `five_min`
- `fifteen_min`
- `lunch`
- `close`
- `target_1`
- `target_2`
- `invalidation`
- `high_opportunity`
- `monitor_exit_signal`, only when a real saved monitor exit exists
- `trigger_touch`, as a trigger-touch scenario

Recommended returns are only available when there is an explicit saved monitor
exit signal. Scenario returns are paper returns from imported outcomes, not real
executed trades.

## Return Attribution Rules

- Missing outcomes stay pending.
- Missing outcomes are not counted as zero.
- Outcomes before the saved signal timestamp are rejected.
- Daily top1/top3/top5 returns are equal-weight across available attributed
  rows.
- Evidence labels are:
  - `<20` audited days: `Not enough history yet.`
  - `20-59` audited days: `Early evidence.`
  - `60+` audited days: `Stronger evidence.`

## Dashboard Historical Calendar Behavior

The dashboard data loader now reads historical signal tables when available and
uses attributed returns before falling back to older manual-audit views.

Calendar status labels include:

- `No data`
- `No trade`
- `Picks pending`
- `Partial outcomes`
- `Audited`
- `Data problem`

Missing outcomes remain visible as missing; they are not hidden or treated as
losses.

## Historical Report Outputs

`historical-report` writes:

- `historical_signals.csv`
- `historical_signal_events.csv`
- `historical_signal_outcomes.csv`
- `return_attribution.csv`
- `daily_performance.csv`
- `cumulative_equity_curve.csv`
- `missing_outcomes.csv`
- `accuracy_by_setup.csv`
- `accuracy_by_source.csv`
- `accuracy_by_score_bucket.csv`
- `historical_report.json`
- `historical_report.md`

## Current Results

`attribute-returns` result:

- Status: `complete`
- Signal count: `8`
- Attribution count: `0`
- Daily count: `1`
- Missing outcome count: `0`

`historical-report` result:

- Status: `complete`
- Signal count: `8`
- Outcome count: `0`
- Attribution count: `0`
- Audited day count: `0`
- Missing outcome count: `0`
- Evidence status: `Not enough history yet.`

There are no attributed returns because no matching historical outcome rows have
been imported yet.

## Commands Run

```powershell
py -m pip install -e ".[dev]"
py -m pytest -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall intraday_scanner app.py tests
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify console --dry-run
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
curl.exe -s -o NUL -w "%{http_code}" http://127.0.0.1:8502/
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts
git diff --check
```

## Tests Passed

- Editable install: PASS
- Pytest: PASS, `170 passed`
- Ruff: PASS
- Mypy: PASS, `Success: no issues found in 92 source files`
- Compileall: PASS
- Dashboard HTTP: PASS, `200`
- No-trading implementation search: PASS, no implementation matches
- `git diff --check`: PASS with line-ending warnings only

## No-Trading Safety Result

No broker order execution path was added. The implementation-only search found
no matches for order APIs, trading clients, market/limit orders, auto-trading,
buy/sell recommendations, or order submission.

The system remains research-only and notification-only.
