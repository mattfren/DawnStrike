# URL Ingestion Policy

URL ingestion is disabled by default. Prefer local CSV exports when possible.

Use URL ingestion only for public pages where access is allowed. Dawnstrike must
not bypass logins, CAPTCHAs, paywalls, anti-bot systems, robots restrictions, or
site terms.

## Static Public Tables

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
Run:

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
```

Then use a local CSV, another enabled candidate source, or the optional browser
extractor.

## Browser-Rendered Tables

See `docs\BROWSER_SOURCE_EXTRACTION.md`.

Browser-rendered rows are still unverified shadow data. Serious live accuracy
requires a real market-data provider later.
