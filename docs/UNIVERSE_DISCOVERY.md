# Universe Discovery

Dawnstrike live scans need a symbol universe. Alpaca market-data snapshots can
scan supplied symbols, but this repo does not assume a market-wide Alpaca mover
endpoint.

Use a local universe CSV:

```csv
ticker
NOVA
RIFT
WIDE
```

Run:

```powershell
py -m intraday_scanner.cli live-scan `
  --provider alpaca `
  --universe-file sample_data\universe_sample.csv `
  --db-path data\scanner_live.sqlite `
  --out-dir outputs\live_scan `
  --persist `
  --print
```

If no `--symbols`, `--symbols-file`, or `--universe-file` is supplied, live scan
fails with an actionable error. If Alpaca secrets are missing, it fails before
requesting data and does not log API keys.

Provider health records count telemetry when live snapshots are returned:

- `symbols_requested`
- `symbols_returned`
- `symbols_with_premarket_volume`
- `symbols_passing_filters`
- `snapshot_row_count`
- `candidate_count`
- `top_explosive_count`

Sample data and sample universe files are fixtures only. Real live validation
needs a broad U.S. common-stock universe file, and full-market quality depends
on how complete and clean that supplied universe is.
