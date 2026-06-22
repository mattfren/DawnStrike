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

## AlphaOps Truth Layer

`alpha-report` summarizes top1/top3/top5, average and median return, win rate,
worst day, setup/source/catalyst/risk buckets, score deciles, outlier
dependency, alpha bucket performance, best/worst buckets, max drawdown, missing
outcome rate, real days collected, and whether evidence is sufficient. Fewer
than 20 real shadow days are reported as insufficient sample. Fewer than 60 real
market days is not strong evidence.

## Historical Ledger Attribution

`attribute-returns` is the authoritative AlphaOps return-attribution command.
It reads `historical_signals` and `signal_outcomes`, then writes
`signal_return_attribution` and `daily_signal_performance`.

```powershell
py -m intraday_scanner.cli attribute-returns --db-path data\shadow_real.sqlite --out-dir outputs\return_attribution --persist
```

It calculates scenario returns for one minute, five minutes, fifteen minutes,
lunch, close, target levels, invalidation, and high opportunity. High
opportunity is labeled as opportunity, not realized return. Recommended returns
only exist when a saved monitor/exit event provides an explicit exit price.

`historical-report` exports the full ledger, outcome, attribution, daily
performance, cumulative curve, and accuracy bucket files:

```powershell
py -m intraday_scanner.cli historical-report --db-path data\shadow_real.sqlite --out-dir outputs\historical_report
```

## Historical Alpha Calendar

The dashboard `Historical Calendar` tab, `calendar-report`, and
`historical-report` read the same persisted database and present a day-by-day
audit ledger. Calendar returns prefer persisted historical attribution rows and
fall back to older manual audit rows only when the new tables are absent.
Missing outcomes are shown as `Outcome needed`, stay pending, and are excluded
from equal-weight return math.

The calendar separates scenario returns from recommended-exit returns:

- Scenario fields: 1 minute, 5 minute, 15 minute, lunch, and close.
- Recommended exits: explicit saved exit rows or saved monitor exit events.
- High-after-entry is opportunity, not realized return.

Evidence labels are conservative: fewer than 20 audited market days is
insufficient, 20 to 59 is early evidence, and 60 or more is stronger evidence.
The calendar remains research-only and does not add broker/order behavior.
