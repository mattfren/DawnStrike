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
