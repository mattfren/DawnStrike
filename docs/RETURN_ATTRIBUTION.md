# Return Attribution

Dawnstrike return attribution answers: after a saved research signal, what would
paper/scenario returns have been under explicit, repeatable rules?

These are not executed trades. They are not proof of future returns.

## Entry Policies

The primary entry policy is:

- `first_available_after_signal`: the imported entry price/time after the saved
  signal timestamp.

Additional scenario rows may be calculated when data supports them:

- `trigger_touch`: entry at the saved watch trigger after outcome data proves the
  high after entry reached that trigger.
- `manual_entry`: reserved for explicitly imported manual entries.
- `market_open`: unavailable unless an open price is supplied.
- `breakout_confirmation`: unavailable unless confirmation data exists.

If an entry cannot be calculated, it is marked unavailable.

## Exit Policies

Scenario exit policies:

- `one_min`
- `five_min`
- `fifteen_min`
- `lunch`
- `close`
- `target_1`
- `target_2`
- `invalidation`
- `high_opportunity`

`high_opportunity` is opportunity, not realized return. It must not be read as an
exit that the system could have captured.

Recommended returns are only counted when there is an explicit saved exit signal,
such as a persisted monitor exit with a price. If no explicit exit exists, the
dashboard shows `recommended_exit_policy=not_recorded`.

## Portfolio Math

Top1, Top3, and Top5 are equal-weight paper baskets by saved rank.

Rules:

- Missing outcomes make the basket unavailable for that policy.
- Missing prices stay unavailable, not 0%.
- Partial outcomes can support the policies that have prices and leave the rest
  unavailable.
- Compounded curves use audited daily basket returns only.

## Evidence Labels

- Fewer than 20 audited market days: `Not enough history yet.`
- 20 to 59 audited market days: `Early evidence.`
- 60 or more audited market days: `Stronger evidence.`

No statistical confidence is shown before the sample is large enough.

## Outputs

`attribute-returns` writes:

- `signal_return_attribution.csv`
- `daily_signal_performance.csv`
- `cumulative_equity_curve.csv`
- `missing_outcomes.csv`
- `attribution_summary.json`
- `attribution_report.md`

`historical-report` writes:

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
