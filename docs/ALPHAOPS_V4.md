# AlphaOps v4

Dawnstrike AlphaOps v4 is an adaptive research layer on top of Signal Engine v3.
It generates a feature vector for every candidate, applies risk/no-trade gates,
scores the remaining watchlist, freezes the exact rows rendered in Telegram,
and learns only from reconciled paper outcomes backed by complete sourced RTH
bars. It does not place orders, hold broker credentials, or execute trades.

## Commands

```powershell
py -m intraday_scanner.cli alpha-morning --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_morning --notify console --dry-run
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify console --dry-run
py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify console --dry-run
py -m intraday_scanner.cli alpha-paper-reconcile --db-path data\shadow_real.sqlite --market-date YYYY-MM-DD --bars-csv PATH_TO_COMPLETE_RTH.csv --out-dir outputs\strategy_reconciliation --persist
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite --market-date YYYY-MM-DD
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-doctor --config config\web_sources.example.yaml --out-dir outputs\alpha_doctor
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

Use `--notify telegram` only after setting Telegram secrets. `--dry-run` stays
secret-free. Its exact cohort is recorded with delivery status `dry_run` and is
never eligible for EOD reconciliation.

The EOD source must contain complete timezone-aware one-minute bar-close rows
for every delivered symbol. Regular sessions require 09:31-16:00 ET; published
early-close sessions require 09:31 through the scheduled close. Missing bars,
naive timestamps, a changed Telegram transmission, or a failed/dry-run delivery
blocks the run with exit code `2`. Missing truth remains `N/A`, never `0%`.

Production truth is date-locked: one immutable `official_telegram` cohort is
claimed per market date. A retry cannot replace its membership, and a source
failure becomes an explicit delivered `NO_TRADE` member rather than a missing
day. Delivery is proven from Telegram's final transmitted UTF-8 bytes, SHA-256,
HTTP acknowledgement, and `message_id`; the pre-format body is not delivery
proof.

The EOD input may be the broad canonical aggregate or the canonical directory
containing `SYMBOL/YYYY-MM-DD_canonical_intraday.csv`. AlphaOps extracts only
the delivered symbols, verifies each published RTH one-minute grid, and retains
a cohort-only content-addressed CSV. Unrelated aggregate symbols do not block a
valid cohort. Missing symbols or bars remain `blocked_incomplete`, never zero.

Persisted evaluations, paper trades, learning labels, outcomes, and scorecards
are immutable. Repeating the same source evidence is idempotent; reusing an
identity with different bars, hashes, or economics is rejected and cannot enter
learning without a separately designed correction/review workflow.

## Feature Vector

Every AlphaOps signal persists `scan_id`, `ticker`, `timestamp`,
`model_version`, `config_hash`, and `feature_json`. Feature groups are:

- price/momentum
- liquidity/execution
- source/data quality
- catalyst
- risk
- structure
- playbook/setup

## Model Behavior

With fewer than 20 real shadow-trading days, AlphaOps uses rule-based scoring
and marks expectancy as insufficient sample. It still persists features,
signals, and outcomes so the model can learn.

Minimum evidence target is 20 real market days. Strong evidence target is 60+
real market days. Before those thresholds, the dashboard and reports must remain
explicit that AlphaOps is still collecting evidence.

With enough canonical after-cost outcomes, AlphaOps uses empirical priors by setup/source/catalyst,
score decile, gap, volume, and risk buckets. Priors use shrinkage toward the
global mean so small buckets cannot dominate the result.

The implementation is rule-first. No ML model is activated unless an offline,
date-split/walk-forward evaluation beats the rule baseline without leakage.
The offline model uses dated forward paper feature/outcome rows only and targets
after-cost returns, not high-of-day-only optimization. Activation and return
labels remain separate: a resolved `not_triggered` row teaches activation
selectivity but cannot become a zero-return trade. Activation adjustments stay
disabled below 20 conclusive setup observations and are capped at five score
points in either direction.

## No-Trade Is Valid

AlphaOps can send "No clean edge today" when data is stale, source confidence is
low, the top candidate is too risky, all candidates are hard-avoid, the market
is thin, or historical edge is not yet sufficient. Telegram never forces a pick.

## Persisted Evidence

SQLite tables include feature vectors, alpha signals, immutable signal
selections, notification delivery memberships, evaluations, paper trades,
separate activation/return labels, daily scorecards, learning runs, source
reliability, and setup memory. The dashboard reads those records
and shows Alpha score, edge bucket, no-trade reason, setup memory, source
reliability, score decile, outlier dependency, missing outcome rate, real days
collected, and whether evidence is sufficient.

Manual outcome imports remain available for audit/history, but production
AlphaOps learning does not consume them. Learning is blocked until the exact
day's reconciliation scorecard is complete. Public premarket rows remain
unverified shadow data, and no real return exists until sourced RTH truth passes
the complete-grid and lineage gates.
