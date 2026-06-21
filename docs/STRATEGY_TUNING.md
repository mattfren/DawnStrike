# Strategy Tuning

Run:

```powershell
py -m intraday_scanner.cli tune-strategy
```

The tuner scores parameter sets by median close return, drawdown, hit rate,
outlier dependence, sample size, and data quality. It does not optimize only for
high-of-day.

## Fixture-Only Mode

When there are not enough real persisted outcomes, tuning is labeled
`fixture_only` with `walk_forward_mode=blocked_insufficient_real_outcomes` and
`overfit_risk=high`. Treat fixture output as a regression check only.

Recommended next step: collect 20+ real shadow days before changing production
weights.

## Historical Mode

When enough dated historical outcomes exist, use a date split or walk-forward
review and compare train/test stability before changing weights.
