# URL Ingestion Policy

URL ingestion is disabled by default. Prefer local CSV exports when possible.

Use URL ingestion only for public pages where access is allowed. Dawnstrike must
not bypass logins, CAPTCHAs, paywalls, anti-bot systems, robots restrictions, or
site terms.

## Static Public Tables

Use the configured source hierarchy before adding ad-hoc URLs. The preferred
public source is `stockanalysis_premarket`. TradingView can expose premarket
rows with source-specific column names such as `Pre-Mkt Price`, `Pre-Mkt Vol`,
and `Pre-Mkt Gap %`; Dawnstrike maps those when they are present. MarketWatch
and Investing.com are optional fallback sources and may expose incomplete or
blocked tables.

```powershell
py -m intraday_scanner.cli web-ingest-public-table --url https://allowed.example/table --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_ingest\manual --persist --print
```

Rules:

- `http` and `https` only
- host must be in configured allowed domains
- safe timeout
- extracts exposed HTML tables only
- no credential storage
- no aggressive scraping
- URL-ingested data is labeled unverified

Rows are labeled:

```text
data_source_kind=web_url
coverage_warning=url_table_unverified
```

If Barchart or another site returns `no_candidate_table`, no rows are fabricated.
If a page requires login, CAPTCHA, paywall access, or anti-bot review,
Dawnstrike reports the block and stops; do not bypass the protection.
Run:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

Then use a local CSV, another enabled candidate source, or the optional browser
extractor.

Source doctor also writes row-level diagnostics:

```text
outputs\source_doctor\extracted_rows.csv
outputs\source_doctor\rejected_rows.csv
outputs\source_doctor\normalization_debug.json
```

## Browser-Rendered Tables

See `docs\BROWSER_SOURCE_EXTRACTION.md`.

Browser-rendered rows are still unverified shadow data. Serious live accuracy
requires a real market-data provider later.
