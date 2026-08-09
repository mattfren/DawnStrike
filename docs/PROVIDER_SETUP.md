# Provider Setup

Dawnstrike supports sample/offline mode without API keys. Live mode is provider-driven
and must fail clearly when credentials are missing. No secrets should be committed,
printed, or stored in logs.

## Sample Mode

```powershell
intraday-scan morning-run ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --db-path data\scanner.sqlite
```

Sample mode uses `CsvSnapshotProvider` and local CSV files.

## Alpaca Market Data

Copy `.env.example` to `.env` and set:

```powershell
ALPACA_API_KEY_ID=your_key
ALPACA_API_SECRET_KEY=your_secret
ALPACA_DATA_FEED=iex
```

`iex` is suitable for development but covers only one exchange. Small-cap
premarket names can legitimately have no recent IEX bars even while they trade
elsewhere. For current full-market selection, enable real-time SIP on the Alpaca
account and set `ALPACA_DATA_FEED=sip`. Dawnstrike will fail the selection closed
when sparse coverage exceeds its bounded secondary-source ceiling; it will not
convert missing bars to zero or pretend that incomplete coverage means no edge.

For a future independent secondary, use a licensed consolidated provider such
as Massive (formerly Polygon) behind the existing `MarketDataProvider` boundary.
Do not use Yahoo as the sole production selection feed or increase its 25%
fallback ceiling to conceal a primary-feed coverage problem.

Run:

```powershell
intraday-scan live-scan ^
  --provider alpaca ^
  --symbols TSLA,NVDA,AMD ^
  --db-path data\scanner.sqlite ^
  --persist
```

If credentials are missing, the command exits with a clear missing-key error and
does not log key values.

After a scan is persisted, run provider-backed open monitoring with fresh Alpaca
market-data snapshots for the saved tickers:

```powershell
intraday-scan monitor-open ^
  --provider alpaca ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --continuous
```

## News And Filing Risk Feeds

Monitoring can check live headline and SEC filing risk after recommendations
are saved. These feeds are optional and disabled by default so sample mode stays
fully offline.

NewsAPI:

```powershell
NEWS_API_KEY=your_key
```

Finnhub:

```powershell
FINNHUB_API_KEY=your_key
```

Run the monitor with automatic news-provider selection and SEC RSS:

```powershell
intraday-scan monitor-open ^
  --snapshot sample_data\premarket_snapshot_sample.csv ^
  --provider csv ^
  --db-path data\scanner.sqlite ^
  --out-dir outputs\latest_monitor ^
  --persist ^
  --news-provider auto ^
  --sec-rss
```

`--news-provider auto` chooses NewsAPI when `NEWS_API_KEY` is set, then Finnhub
when `FINNHUB_API_KEY` is set. `--sec-rss` does not require a key. Provider
success and failure states are written to SQLite provider health for the
dashboard.

## Extending Providers

Provider interfaces live in `intraday_scanner.providers.base`:

- `MarketDataProvider`
- `NewsProvider`
- `SECProvider`
- `NotificationProvider`

Vendor-specific providers should normalize data into the canonical snapshot,
news, or filing models before services consume it. This keeps Polygon, Databento,
Benzinga, Finnhub, NewsAPI, or other feeds swappable without changing scanner logic.
# Historical intraday evidence providers

Historical intraday acquisition is read-only and research-only. It does not
create orders or connect to a broker trading endpoint.

## Capability and feed identity

The existing Alpaca market-data client supports the explicit `iex` and `sip`
feed identities. A request for SIP is never silently substituted with IEX.
The Massive adapter uses `MASSIVE_API_KEY` as its primary credential and
accepts `POLYGON_API_KEY` only as a compatibility alias. Neither credential is
printed in probe receipts or logs.

Each provider exposes capability facts for bars, trades, quotes, corporate
actions, and pagination. A capability may be unavailable; it is recorded as
unavailable rather than inferred from a plan name.

## Evidence retention and acquisition controls

Set `DAWNSTRIKE_INTRADAY_EVIDENCE_ROOT` to the operator-approved retention
root. Raw and normalized compressed artifacts are stored outside SQLite and
indexed in schema 22 with provider, feed, request window, code SHA, content
hashes, and retention status. The backfill utility refuses to write when the
operator entitlement metadata does not permit retention.

`DAWNSTRIKE_INTRADAY_PAGE_LIMIT`, `DAWNSTRIKE_INTRADAY_MAX_PAGES`,
`DAWNSTRIKE_INTRADAY_BACKOFF_SECONDS`, `INTRADAY_REQUEST_TIMEOUT_SECONDS`, and
`INTRADAY_REQUEST_RETRIES` bound page size, restart work, timeout, and retry
behavior. HTTP 429 responses use bounded backoff. Every page returns a raw
payload hash and a next-page cursor so a restart can begin at the last
verified page.

## Probe behavior

Run `scripts/probe_intraday_provider.py` with an explicit output path. The
receipt records only credential presence, provider/feed identity, capability
and entitlement facts, earliest-availability fields when supplied by a live
probe, session/extended-hours/corporate-action coverage, pagination limits,
retention permission, and an estimated request/byte volume. The receipt is
content-hashed and sanitized.

If no approved Massive key and plan exist, the correct result is
`BLOCKED_EXTERNAL_MARKET_DATA_ENTITLEMENT`. Fixture-backed adapter tests may
still pass; this status is not a claim of live data coverage.
