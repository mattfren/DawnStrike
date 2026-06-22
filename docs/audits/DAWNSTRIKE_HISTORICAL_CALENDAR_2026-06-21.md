# Dawnstrike Historical Calendar Audit - 2026-06-21

## Scope

Built the Historical Alpha Calendar for real persisted Dawnstrike/AlphaOps data.
The feature is research/watchlist only. It does not place orders, connect broker
trading clients, or add buy/sell execution behavior.

## Files Changed

- `.gitignore`
- `README.md`
- `app.py`
- `docs/HISTORICAL_CALENDAR.md`
- `docs/OPERATOR_RUNBOOK.md`
- `docs/PERFORMANCE_AUDIT.md`
- `docs/audits/DAWNSTRIKE_HISTORICAL_CALENDAR_2026-06-21.md`
- `intraday_scanner/cli.py`
- `intraday_scanner/dashboard/data_loader.py`
- `intraday_scanner/services/alpha_cycle_service.py`
- `intraday_scanner/services/calendar_report_service.py`
- `tests/test_historical_calendar.py`
- `tests/test_streamlit_app.py`

Ignored local/generated files were not staged or required for source changes:
SQLite DBs, `outputs\`, `logs\`, pytest temp folders, and local config remain
outside the commit scope.

## Implementation Summary

- Added the `Historical Calendar` dashboard tab.
- Added calendar loaders for daily summaries, day drilldowns, daily returns,
  compounded equity curves, and missing outcomes.
- Added the `calendar-report` CLI command.
- Added report writer for:
  - `calendar_days.csv`
  - `calendar_day_details.json`
  - `calendar_equity_curve.csv`
  - `missing_outcomes.csv`
  - `calendar_report.md`
- Added day statuses:
  - `NO DATA`
  - `NO TRADE`
  - `PICKS PENDING OUTCOMES`
  - `OUTCOMES PARTIAL`
  - `AUDITED`
  - `SOURCE FAILURE`
- Added missing-outcome handling that marks rows as `Outcome needed` and never
  converts missing returns to zero.
- Added scenario-return and recommended-exit separation.
- Added monitor-exit support only when a persisted monitor/alert event exists.
- Added docs for operator flow, performance rules, and calendar report usage.

## Commands Run

```powershell
py -m pip install -e ".[dev]"
py -m pytest --basetemp .pytest_full_tmp_4 -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall intraday_scanner app.py tests
py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report
curl.exe -I http://127.0.0.1:8502/
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade" intraday_scanner app.py scripts
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade|orders_enabled" intraday_scanner app.py scripts tests docs README.md
```

## Test Results

- `py -m pip install -e ".[dev]"`: PASS.
- `py -m pytest --basetemp .pytest_full_tmp_4 -p no:cacheprovider`: PASS,
  `156 passed in 32.28s`.
- `py -m ruff check .`: PASS.
- `py -m mypy intraday_scanner`: PASS, `Success: no issues found in 90 source files`.
- `py -m compileall intraday_scanner app.py tests`: PASS.
- `tests/test_historical_calendar.py`: PASS, `9 passed`.

One intermediate pytest run failed because `tests/test_streamlit_app.py` still
expected the pre-calendar tab list. The test was updated and the full suite then
passed.

## Calendar Report Result

Command:

```powershell
py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report
```

Result:

- status: `complete`
- start: `2026-06-01`
- end: `2026-06-21`
- day_count: `21`
- audited_day_count: `0`
- missing_outcome_count: `0`
- output directory: `outputs\calendar_report`

Generated report files:

- `outputs\calendar_report\calendar_days.csv`
- `outputs\calendar_report\calendar_day_details.json`
- `outputs\calendar_report\calendar_equity_curve.csv`
- `outputs\calendar_report\missing_outcomes.csv`
- `outputs\calendar_report\calendar_report.md`

## Dashboard Smoke Result

`curl.exe -I http://127.0.0.1:8502/` returned `HTTP/1.1 200 OK`.

The Streamlit render test also passed with the new `Historical Calendar` tab in
the expected tab list.

## Return Calculation Rules

- No outcome row means no return.
- Missing metrics stay unavailable and are not converted to zero.
- Scenario returns use persisted outcome/audit fields for 1 minute, 5 minute,
  15 minute, lunch, close, high-after-entry, and low-after-entry drawdown.
- Scenario returns are labeled separately from recommended-exit returns.
- Recommended-exit returns require an explicit saved exit row or a saved monitor
  invalidation/thesis-broken event.
- Monitor-exit returns are calculated only when a saved monitor event includes a
  price and occurs after the official saved signal timestamp.
- High-after-entry is labeled as opportunity, not realized return.
- Top1/top3/top5 are equal-weight baskets using available audited picks.
- Compounded curves compound fully audited daily close-return baskets only.

## Historical Calendar Behavior Verified

- Empty/missing-table DBs load safely and show warnings.
- Optional historical tables are read when present:
  `recommendation_theses`, `source_reliability`, `alpha_reports`,
  `performance_cumulative`, and `manual_audit_summary`.
- `NO DATA`, `NO TRADE`, `PICKS PENDING OUTCOMES`, and `AUDITED` statuses are
  covered by offline tests.
- Missing outcomes appear as `Outcome needed`.
- Missing returns remain `None`/pending and are not counted as zero.
- Top1/top3/top5 baskets are equal-weight.
- Compounded equity curves use audited daily return rows.
- Scenario labels remain distinct from recommended exits.
- Monitor exits require persisted monitor-event evidence.
- Loader output is included in `load_sqlite()` for the dashboard.
- CLI writes all required report files.
- Secret-like Telegram payload text is redacted from report/detail output.

## No-Trading Safety Result

Implementation-only safety search:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation|market_order|limit_order|execute_trade" intraday_scanner app.py scripts
```

Result: PASS, no matches.

Broad repo safety search matched only existing docs/tests/audit text. No
broker/order/trading execution path was added, and the source tree has no
`orders_enabled` implementation key.

## Limitations

- The local `data\shadow_real.sqlite` calendar report currently has no audited
  days for the selected range, so it correctly reports insufficient evidence.
- Public/free data remains unverified shadow data until outcomes are imported
  and audited from real saved rows.

## Exact Operator Commands

Open dashboard:

```powershell
py -m streamlit run app.py --server.port 8502
```

Generate calendar report:

```powershell
py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report
```

Import and audit a day of outcomes:

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
py -m intraday_scanner.cli audit-manual-outcomes --db-path data\shadow_real.sqlite --out-dir outputs\manual_audit --persist
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```
