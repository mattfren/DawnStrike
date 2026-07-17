# Historical Alpha Calendar

Dawnstrike's Historical Calendar is a paper/shadow review view for the permanent
historical signal ledger. It helps answer one question: after the system saved
official research signals, what was later audited, what is still missing, and
what would the recorded shadow returns have been under clearly labeled scenarios?

It does not place orders, hold broker credentials, or recommend buying or
selling. Public/free data is labeled unverified shadow data.

## Web UI

Open the Streamlit dashboard and select `Historical Calendar`.

```powershell
py -m streamlit run app.py --server.port 8502
```

Default database:

```text
data\shadow_real.sqlite
```

Use the controls at the top of the tab to choose the month, date range, data
source filter, setup filter, portfolio size, and return policy.

## Day Statuses

- `NO DATA`: no persisted scan, signal, notification, or source row exists.
- `NO TRADE`: AlphaOps saved a no-trade decision.
- `PICKS PENDING OUTCOMES`: saved signals exist but no usable outcome rows exist.
- `OUTCOMES PARTIAL`: some outcomes exist, but the day is not fully audited.
- `AUDITED`: all counted picks have usable audited outcome rows.
- `SOURCE FAILURE`: the persisted source/provider health row shows failure and
  no picks are available.

Missing outcomes are shown as `Outcome needed`. They are never counted as zero.

## Return Policies

Scenario returns are informational paper metrics only:

- `scenario_1m`
- `scenario_5m`
- `scenario_15m`
- `lunch`
- `close`

Recommended-exit returns require an explicit saved exit signal or a saved monitor
exit event with a price:

- `monitor_exit_if_available`
- `recommended_exit_if_recorded`

`high_after_entry_return` is labeled as opportunity, not realized return.

## Import Outcomes

Save outcome CSVs here:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

Required columns:

```text
date,ticker,entry_time,entry_price,price_1m,price_5m,price_15m,lunch_price,close_price,high_after_entry,low_after_entry,halted,source,notes
```

Then run:

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

## CLI Report

Write the calendar report files:

```powershell
py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
```

For a specific month:

```powershell
py -m intraday_scanner.cli calendar-report --db-path data\shadow_real.sqlite --out-dir outputs\calendar_report --month 2026-06
```

The command writes:

- `calendar_days.csv`
- `calendar_day_details.json`
- `calendar_equity_curve.csv`
- `missing_outcomes.csv`
- `calendar_report.md`

`historical-report` writes the authoritative ledger exports:

- `historical_signals.csv`
- `historical_signal_events.csv`
- `historical_signal_outcomes.csv`
- `return_attribution.csv`
- `daily_performance.csv`
- `cumulative_equity_curve.csv`
- `accuracy_by_setup.csv`
- `accuracy_by_source.csv`
- `accuracy_by_score_bucket.csv`
- `missing_outcomes.csv`
- `historical_report.md`
- `historical_report.json`

## Evidence Rules

- Fewer than 20 audited real market days is insufficient evidence.
- Fewer than 60 audited real market days is early evidence.
- Top1, Top3, and Top5 are equal-weight baskets using available audited picks.
- Compounded curves only include fully audited days.
- Missing returns remain pending and are excluded from return math.
