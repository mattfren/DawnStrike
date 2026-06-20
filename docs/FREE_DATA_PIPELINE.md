# Free Data Pipeline

Dawnstrike can run without paid data by using manual screener uploads and bundled
fixtures.

## What Works With Zero Keys

- Manual premarket snapshot import.
- Free Shadow Mode scans.
- SQLite persistence for picks, theses, risk flags, outcomes, and audits.
- Manual outcome audits.
- Free Shadow Mode reports.
- Streamlit dashboard review.
- Console notifications.
- Offline starter universe generation:

```powershell
py -m intraday_scanner.cli build-free-universe --out data\universe_us_common.csv
```

The starter universe is fixture/free mode. Replace it with a broad U.S.
common-stock universe before serious live validation.

## What Still Needs Keys

Read-only Alpaca market data requires Alpaca API keys. News providers require
their own keys. Email, Discord, and Telegram notifications require channel
settings.

## What Usually Requires Paid Data

Full-market, low-latency tape coverage, complete premarket venue coverage,
institutional-grade corporate-action history, and clean survivorship-controlled
historical datasets usually require paid data.

Dawnstrike does not need or use broker trading credentials.
