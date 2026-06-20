# Automation

Dawnstrike can run the scan, paper audit, and setup monitor without placing trades. The monitor checks whether earlier ranked setups are still following the planned path: hold invalidation, reclaim breakout, and work toward the first target.

## Web UI Workflow

Use the dashboard `Run Center` tab:

1. Click `Initialize DB`.
2. Click `Run Scan`.
3. Click `Run Audit` after minute bars are available.
4. Click `Monitor Setups` to rescan the saved watchlist against the current snapshot.
5. Click `Register 5m Tasks` to install the local Windows Scheduled Tasks.
6. Click `Task Status` to confirm the task state.
7. Open the `Monitor` tab to review confirming, watching, extended, fading, and invalidated names.

The monitor writes:

- `outputs\latest_monitor\setup_monitor_checks.csv`
- `outputs\latest_monitor\setup_monitor_summary.json`
- SQLite rows in `setup_monitor_checks` when persistence is enabled.

## Five-Minute Monitor Loop

For a local foreground loop:

```powershell
.\scripts\run_monitor_loop.ps1
```

The default interval is 300 seconds. The loop keeps checking the latest persisted ranked candidates until you stop it.

To run one pass:

```powershell
.\scripts\run_monitor_once.ps1
```

To run the sample scan, audit, and monitor chain:

```powershell
.\scripts\run_sample_backtest.ps1
```

## Windows Scheduled Tasks

Register local scheduled tasks:

```powershell
.\scripts\register_dawnstrike_tasks.ps1
```

The same registration can be run from the dashboard `Run Center` with `Register 5m Tasks`.

This creates:

- `Dawnstrike Daily Scan`
- `Dawnstrike Setup Monitor 5m`

The 5-minute task calls `monitor-setups`; it does not place orders. Update the `Snapshot`, `DbPath`, and output parameters when you switch from sample data to a live snapshot pipeline.

## CLI Commands

One monitor pass:

```powershell
intraday-scan monitor-setups ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist
```

Five-minute loop:

```powershell
intraday-scan monitor-loop ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --interval-seconds 300
```

## Status Meanings

- `confirming`: price is above the original breakout trigger and below target.
- `watching`: setup is intact, but breakout has not confirmed.
- `extended`: price is already beyond the original target zone.
- `fading`: price has lost the pullback zone or is sitting low in the live range.
- `invalidated`: price broke invalidation or a hard risk flag appeared.
- `missing`: no current snapshot row exists for the ticker.

These statuses are research controls, not trade instructions.
