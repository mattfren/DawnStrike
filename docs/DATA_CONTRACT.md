# Data Contract

## Canonical Snapshot Schema

Every scanner input row must include:

- `ticker`
- `company`
- `premarket_price`
- `previous_close`
- `premarket_high`
- `premarket_low`
- `premarket_volume`
- `dollar_volume`
- `gap_pct`
- `float_shares`
- `market_cap`
- `spread_pct`
- `short_float_pct`
- `has_news`
- `catalyst_headline`
- `catalyst_url`
- `current_halt`
- `recent_offering`
- `reverse_split_90d`
- `source`
- `as_of_timestamp`

Boolean values accept `true`, `false`, `1`, `0`, `yes`, and `no`. Tickers are normalized to uppercase. Numeric fields must be parseable numbers. `as_of_timestamp` should include a timezone offset when available. All listed columns must be present; nullable values such as `catalyst_url`, `float_shares`, `market_cap`, and `short_float_pct` may be blank when the provider does not supply them.

## Scan Output Schema

`ranked_candidates.csv`, `top_explosive.csv`, and `avoid_list.csv` contain:

- `rank`
- `ticker`
- `company`
- `score`
- `gap_pct`
- `dollar_volume`
- `float_rotation_pct`
- `range_position_pct`
- `data_quality_score`
- `liquidity_tier`
- `setup_grade`
- `volatility_signature`
- `equation_version`
- `premarket_price`
- `previous_close`
- `premarket_high`
- `premarket_low`
- `premarket_volume`
- `breakout_trigger`
- `pullback_zone`
- `invalidation_level`
- `first_target`
- `stretch_target`
- `risk_flags`
- `best_exit_bias`
- `score_breakdown`
- `avoid_reasons`
- `source`
- `as_of_timestamp`

`score_breakdown` is JSON. `risk_flags` and `avoid_reasons` are semicolon-delimited for CSV readability.

The current `score_breakdown` keys are:

- `gap_curve`
- `liquidity_thrust`
- `float_rotation`
- `range_control`
- `squeeze_catalyst`
- `execution_quality`
- `data_quality`
- `risk_penalty`

## Paper Audit Output

`paper_audit_trades.csv` contains regular-open entry, +1/+5/+15 minute returns, lunch exit, close exit, high-after-entry, low-after-entry, slippage bps, return percentages, max favorable excursion, max adverse excursion, and low-after-entry drawdown.

`paper_audit_summary.json` contains average, median, best/worst, hit-rate, max-drawdown, and cumulative top 1/top 3/top 5 lunch/close/high audit returns. This is research infrastructure only and does not represent trading advice.

## SQLite Persistence

SQLite stores:

- `scan_runs`
- `snapshots`
- `raw_snapshots`
- `ranked_candidates`
- `top_explosive`
- `avoid_list`
- `recommendation_theses`
- `setup_monitor_checks`
- `monitor_events`
- `alerts_sent`
- `paper_audit_trades`
- `paper_audit_summary`
- `performance_daily`
- `performance_cumulative`
- `provider_health`
- `notifications_sent`
