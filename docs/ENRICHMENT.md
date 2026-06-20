# Enrichment

Dawnstrike does not fabricate missing float, market-cap, short-interest, halt,
offering, or catalyst fields. Unknown values remain blank/null/false and reduce
data-quality scoring where the formula expects richer data.

Implemented enrichment providers:

- `CsvEnrichmentProvider`: local CSV metadata keyed by `ticker`.
- `SECEnrichmentProvider`: can flag dilution/offering risk from SEC filing data.

Supported enrichment fields:

- `float_shares`
- `market_cap`
- `short_float_pct`
- `current_halt`
- `recent_offering`
- `reverse_split_90d`
- `has_news`
- `catalyst_headline`
- `catalyst_url`
- `spread_pct`

CSV enrichment command example:

```powershell
py -m intraday_scanner.cli scan `
  --snapshot sample_data\premarket_snapshot_sample.csv `
  --enrichment-file data\enrichment.csv `
  --db-path data\scanner.sqlite `
  --persist
```

Alpaca alone may not provide every enrichment field needed for high-confidence
small-cap intraday filtering. Use provider health and the dashboard readiness
panel to see enrichment completeness.

