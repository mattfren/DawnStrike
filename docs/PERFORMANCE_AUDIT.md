# Performance Audit

Dawnstrike performance reporting is paper/shadow validation only. It does not
claim profitability.

## Rules

- A saved recommendation must exist before an outcome row is accepted.
- Outcome timestamps before the recommendation timestamp are rejected.
- Missing outcome values remain unavailable; they are not treated as zero.
- Each audited metric carries an audit status.
- Top1, Top3, and Top5 results are equal-weight baskets.
- Compounded curves are reported separately from simple cumulative sums.
- Manual/free/fixture/paid data labels remain visible.

## Report Fields

`performance-report` includes average and median close return, hit rate, worst
drawdown, max adverse excursion, max favorable excursion, best/worst pick,
best/worst day, setup/source/catalyst/score-decile buckets, sample sizes,
compounded curves, and outlier dependency warnings.

If the close-return sample size is below 20, the report says:

```text
insufficient sample size.
```
