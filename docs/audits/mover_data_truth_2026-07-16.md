# Dawnstrike mover data-truth audit — 2026-07-16

## Verdict

The retained mover-review history is not eligible to train or validate a
highest-mover strategy.

The audit read the repository-local `data/shadow_real.sqlite` database in
SQLite read-only mode. It did not mutate the database.

## Retained evidence

| Evidence | Count | Finding |
|---|---:|---|
| `daily_market_movers` | 195 | 190 are StockAnalysis premarket rows mislabeled as daily movers; 5 local rows lack the new descriptive EOD contract |
| `alpha_feature_vectors` | 211 | 19 dates; important liquidity, catalyst, corporate-action, and outcome truth is incomplete |
| `alpha_outcome_labels` | 0 | No eligible labels |
| `signal_outcomes` | 3 | None satisfy complete sourced-outcome eligibility |
| `daily_review_runs` | 20 | All depend on invalid mover semantics |
| `daily_review_items` | 203 | Quarantined from learning |
| `learning_backfeed_events` | 300 | Quarantined; none may be applied |
| `normalized_source_rows` | 5,742 | Useful as point-in-time input lineage, not as realized mover outcomes |

The five local rows dated 2026-06-21 match the checked-in fixture content and
are not accepted as production evidence. The other 190 mover rows came from
`https://stockanalysis.com/markets/premarket/`, with premarket price and volume
columns rather than realized regular-session OHLCV.

## Release blockers found

1. No semantically valid descriptive end-of-day mover corpus was retained.
2. Premarket rows were mislabeled as realized daily movers.
3. No source-complete, after-cost mover outcome corpus exists.
4. Historical review and backfeed rows depend on those invalid labels.
5. Missing risk truth was sometimes represented as false instead of unknown.

## Remediation shipped in this branch

- Public daily-gainer collection now runs only for the current published
  exchange session after its official close and can never reuse a premarket URL.
- Daily gainers are tagged `descriptive_eod_movers` and
  `prospective_signal_eligible=false`.
- A separate prospective mover contract requires cutoff-safe universe,
  context, bar, catalyst, and source lineage.
- Historical review IDs, review items, and backfeed events tied to invalid
  mover dates are written to a non-mutating quarantine manifest.
- New paper returns require next-bar fills, complete bar grids, official session
  closes, adverse slippage, fees, and immutable source artifacts.
- Missing outcomes remain null and cannot enter learning or promotion metrics.

## Audit command

```powershell
py -m intraday_scanner.v2.mover_pattern_lab audit `
  --db-path data/shadow_real.sqlite `
  --output-root data/v2_mover_pattern_lab
```

The expected current status is `blocked` until valid forward mover evidence is
accumulated. That blocked result is a data-truth finding, not a runtime failure.
