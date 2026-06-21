# Browser Source Extraction

Browser extraction is an optional fallback for public pages that render tables
with JavaScript. It is disabled by default.

Install:

```powershell
py -m pip install -e ".[browser]"
py -m playwright install chromium
```

Example disabled source:

```yaml
- name: barchart_premarket_browser
  type: browser_table_url
  enabled: false
  url: "https://www.barchart.com/stocks/pre-market-trading"
  wait_selector: "table, [role='grid'], .bc-table-scrollable-inner"
  wait_seconds: 10
  max_rows: 150
```

Rules:

- Do not bypass logins, CAPTCHAs, paywalls, anti-bot controls, or protected sites.
- Do not use credentials.
- Respect `allowed_domains`.
- Save failure reports when blocked or no table is found.
- Never invent missing fields.

Rows are labeled:

```text
data_source_kind=browser_url
coverage_warning=browser_rendered_public_table_unverified
```

If Playwright is missing, Dawnstrike reports:

```text
BROWSER_EXTRACTOR_NOT_AVAILABLE: run py -m pip install -e ".[browser]" and py -m playwright install chromium
```

Browser data is public, unverified shadow data. Dawnstrike still has no order
execution path.

Current source guidance:

- Prefer `stockanalysis_premarket` before browser sources.
- Use TradingView static/browser sources only with the built-in source-specific
  mapping; TradingView table headers can differ from normal screener exports.
- Keep Barchart browser disabled unless you explicitly want to test it. Barchart
  often blocks public extraction with login, CAPTCHA, or anti-bot review.
