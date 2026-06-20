# Historical Ingestion

Historical ingestion is for research and paper-trading validation only. It does
not prove live performance unless you supply real point-in-time market data.

Validate/copy minute bars:

```powershell
py -m intraday_scanner.cli ingest-minute-bars `
  --input sample_data\minute_bars\2026-06-18.csv `
  --out-dir outputs\historical_bars
```

Build historical snapshots and optionally persist a scan:

```powershell
py -m intraday_scanner.cli backfill-snapshots `
  --minute-bars sample_data\builder\premarket_bars_sample.csv `
  --previous-close sample_data\builder\previous_close_sample.csv `
  --metadata sample_data\builder\metadata_sample.csv `
  --out-dir outputs\historical_backfill `
  --db-path data\scanner.sqlite `
  --persist
```

Timestamp rules:

- Minute-bar timestamps must include timezone offsets.
- For no-lookahead simulation, provide only bars available by the configured
  signal time.
- Fixture outputs are labeled `fixture_only`.

Parquet ingestion is supported when local `pandas`/`pyarrow` are installed.

