# AlphaOps v4

Dawnstrike AlphaOps v4 is an adaptive research layer on top of Signal Engine v3.
It generates a feature vector for every candidate, applies risk/no-trade gates,
scores the remaining watchlist, monitors the same names, and learns from manual
shadow outcomes. It does not place orders, hold broker credentials, or execute
trades.

## Commands

```powershell
py -m intraday_scanner.cli alpha-morning --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_morning --notify console --dry-run
py -m intraday_scanner.cli alpha-cycle --config config\web_sources.example.yaml --db-path data\shadow_real.sqlite --out-dir outputs\alpha_cycle --notify console --dry-run
py -m intraday_scanner.cli alpha-monitor --db-path data\shadow_real.sqlite --notify console --dry-run
py -m intraday_scanner.cli alpha-outcomes --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-status --db-path data\shadow_real.sqlite
py -m intraday_scanner.cli alpha-doctor --config config\web_sources.example.yaml --out-dir outputs\alpha_doctor
py -m intraday_scanner.cli alpha-report --db-path data\shadow_real.sqlite --out-dir outputs\alpha_report
```

Use `--notify telegram` only after setting Telegram secrets. `--dry-run` stays
secret-free and persists notification attempts through the normal dedupe table.

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

With enough outcomes, AlphaOps uses empirical priors by setup/source/catalyst,
score decile, gap, volume, and risk buckets. Priors use shrinkage toward the
global mean so small buckets cannot dominate the result.

The implementation is rule-first. No ML model is activated unless an offline,
date-split/walk-forward evaluation beats the rule baseline without leakage.
The offline model uses dated historical feature/outcome rows only and targets
close/timed returns, not high-of-day-only optimization.

## No-Trade Is Valid

AlphaOps can send "No clean edge today" when data is stale, source confidence is
low, the top candidate is too risky, all candidates are hard-avoid, the market
is thin, or historical edge is not yet sufficient. Telegram never forces a pick.

## Persisted Evidence

SQLite tables include feature vectors, alpha signals, outcome labels, learning
runs, source reliability, and setup memory. The dashboard reads those records
and shows Alpha score, edge bucket, no-trade reason, setup memory, source
reliability, score decile, outlier dependency, missing outcome rate, real days
collected, and whether evidence is sufficient.

Without paid/live provider data, outcome quality is limited by manual/free
shadow collection. Public web rows remain unverified shadow data.
