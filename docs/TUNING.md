# Strategy Tuning

`intraday-scan tune-strategy` runs offline scenario tests against a point-in-time
snapshot and later minute bars. It does not use future prices during scoring.
Audit returns are calculated only after the saved signal/entry window.

Run fixture tuning:

```powershell
intraday-scan tune-strategy ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --minute-bars sample_data\minute_bars\2026-06-18.csv ^
  --out-dir outputs\tuning
```

Outputs:

- `outputs\tuning\strategy_tuning_results.csv`
- `outputs\tuning\strategy_tuning_summary.json`

The default command is labeled fixture-only because it uses included sample data.
Use real historical point-in-time snapshots and matching minute bars before making
production conclusions.

Tuned parameters currently include:

- gap weight
- liquidity weight
- float rotation weight
- range control weight
- catalyst weight
- execution/tradability weight
- risk penalty weight
- gap floor
- dollar-volume floor

Metrics include top 1/top 3/top 5 returns across +1, +5, +15 minutes, lunch,
close, and high-of-day, plus hit rate, average/median return, max drawdown, and
best/worst pick.
