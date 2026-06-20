# Secrets Required For Live Mode

Sample mode, dashboard mode, tests, paper audits, strategy tuning, and console
notifications run without secrets.

The only remaining blockers for true live production mode are user-supplied API
keys and notification secrets.

## Required For Alpaca Live Market Data

```powershell
ALPACA_API_KEY_ID=
ALPACA_API_SECRET_KEY=
ALPACA_DATA_FEED=iex
```

Without these, `intraday-scan live-scan --provider alpaca ...` exits with a clear
missing-credential error and does not log secret values.

## Optional Future Market Data Providers

```powershell
POLYGON_API_KEY=
DATABENTO_API_KEY=
```

Provider abstractions are present. These vendors are not wired as live providers
yet, so their keys are optional and currently non-blocking.

## Optional News/AI Risk Feeds

```powershell
OPENAI_API_KEY=
NEWS_API_KEY=
BENZINGA_API_KEY=
FINNHUB_API_KEY=
```

The default headline classifier is deterministic and offline. `NEWS_API_KEY`
and `FINNHUB_API_KEY` are live monitor feed keys for
`--news-provider newsapi`, `--news-provider finnhub`, or `--news-provider auto`.
`OPENAI_API_KEY` is reserved for a future model-backed classifier. `BENZINGA_API_KEY`
is present for a future provider adapter and is currently non-blocking.

SEC RSS monitoring uses `--sec-rss` and does not require a secret.

Live market scans also need a universe source. Provide `--symbols`,
`--symbols-file`, or `--universe-file`; Alpaca market data alone is not treated
as market-wide mover discovery. Some enrichment fields may require local CSV,
SEC/news feeds, or another future data provider.

## Optional Notification Channels

Console notifications need no secrets. External channels need:

```powershell
DISCORD_WEBHOOK_URL=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
SMTP_HOST=
SMTP_PORT=587
SMTP_USER=
SMTP_PASSWORD=
```

The app also accepts the legacy `INTRADAY_*` notification variable names.

## Never Commit

Do not commit `.env`, Streamlit secrets, API keys, webhook URLs, SMTP passwords,
or broker credentials. Dawnstrike has no broker execution path and should not be
given broker trading credentials.
