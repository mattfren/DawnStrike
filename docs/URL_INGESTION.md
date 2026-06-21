# URL Ingestion Policy

URL ingestion is disabled by default. Prefer local CSV exports.

Use URL ingestion only for public pages where access is allowed. Dawnstrike must
not bypass logins, CAPTCHAs, paywalls, anti-bot systems, robots restrictions, or
site terms.

## Command

```powershell
py -m intraday_scanner.cli url-ingest-screener --url https://example.com/table --out outputs\url_ingest --allowed-domain example.com
```

Rules:

- `http` and `https` only
- host must be in configured allowed domains
- safe timeout
- extracts HTML tables only
- no credential storage
- no aggressive scraping
- URL-ingested data is labeled unverified

URL data is not paid/live-grade validation. Serious live accuracy still requires
real provider data later.

## Web Auto-Pilot Commands

```powershell
py -m intraday_scanner.cli web-ingest-public-table --url https://allowed.example/table --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_ingest\manual --persist --print

py -m intraday_scanner.cli web-auto-collect --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto\today --persist --print
```

The Web Auto-Pilot labels public table rows with `data_source_kind=web_url`,
`shadow_mode=true`, `paid_data=false`, and
`coverage_warning=url_table_unverified`. It does not invent missing prices,
volume, float, catalysts, or returns. If required market columns are missing,
it writes a failure or warning instead of producing a fake row.
