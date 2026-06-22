# Dawnstrike Explained

## One-Paragraph Version

Dawnstrike is a research and watchlist system for aggressive intraday stock
setups. Each morning it collects premarket mover data from configured sources,
normalizes the rows, scores the tickers, blocks risky or low-quality names,
sends a Telegram watchlist or a "no clean edge" message, and stores the result
in SQLite so the dashboard, monitor, outcome audit, and learning reports can
review what happened later.

## What Dawnstrike Is Not

Dawnstrike is not a broker, trading bot, or order system. It does not buy
stocks, sell stocks, submit orders, store broker trading credentials, or
guarantee returns. A watchlist alert means "review this manually"; it is not a
command to trade.

## Daily Routine

The intended Windows Task Scheduler workflow is defined in
`scripts/register_alphaops_tasks.ps1`. If Task Scheduler was registered before a
script update, run the registration script again so the Windows task command
matches the repo.

| Time | Scheduled task | What it runs | Result |
| --- | --- | --- | --- |
| 8:10 AM CT | `Dawnstrike AlphaOps Morning` | `alpha-cycle` | Collects sources, scores candidates, sends Telegram watchlist or no-clean-edge message. |
| 8:35 AM CT | `Dawnstrike AlphaOps Monitor 5m` | `alpha-monitor` every 5 minutes for 6 hours | Re-checks saved AlphaOps names and sends manual review/status messages. |
| 3:15 PM CT | `Dawnstrike AlphaOps EOD Report` | `alpha-report`, `attribute-returns`, `historical-report` | Writes end-of-day evidence, return-attribution, and historical-report files from saved outcomes. |

Outcome data is still manual unless a future reliable price feed is added. The
app can remind you that outcomes are missing, but it cannot invent the actual
prices.

## What Data It Uses

Current configured candidate sources are:

- Local screener CSV inbox: `data\inbox\screener`
- StockAnalysis premarket public table
- TradingView premarket public table
- Nasdaq symbol directory for universe support only

Optional sources exist but are currently disabled in `config\web_sources.yaml`:

- TradingView browser-rendered table
- MarketWatch movers
- Investing.com premarket table
- Barchart table/browser fallback
- Nasdaq halt RSS
- SEC EDGAR risk enrichment

Public web rows are useful for a zero-dollar shadow system, but they are
unverified free web data. The pages can change shape, be stale, block requests,
omit fields, or disagree with each other.

## What The Telegram Message Means

Telegram is an alert channel, not a trading command.

- `Dawnstrike Alpha Watch`: Dawnstrike found one to three names worth manual
  review. Read the watch level, exit line, target, confidence, and risk text.
- `Dawnstrike Alpha Check`: Dawnstrike did not find a clean enough edge. This
  is a valid result.
- `Dawnstrike Alpha Monitor`: Dawnstrike is checking earlier saved names. If no
  live/current price feed is configured, it tells you manual review is needed.
- `Outcome Data Needed`: outcome prices are missing. Add the outcome CSV after
  close so the system can learn.
- `Dawnstrike Shadow Results`: a research summary from saved outcome data. It is
  not proof of future returns.

## What "No Clean Edge Today" Means

"No clean edge today" means the system chose not to send a watchlist because the
data, risk, source quality, score, or evidence did not clear the filter. This is
not a crash. It is the system refusing to force a bad pick.

Common reasons:

- No usable rows came back from the sources.
- Public sources disagreed.
- Previous close, high, low, volume, or float data was missing.
- Source confidence was low.
- The top names were halted, had offering/dilution risk, had no volume, or had
  hard avoid flags.
- The AlphaOps score was too weak.
- The drawdown/risk bucket was too high.

## What "Outcome Data Needed" Means

The system saved picks, but no outcome CSV has been imported for those tickers
yet. Until outcomes are imported, returns stay pending. Missing outcomes are not
counted as zero and are not treated as wins or losses.

Use:

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
py -m intraday_scanner.cli audit-manual-outcomes --db-path data\shadow_real.sqlite --out-dir outputs\manual_audit --persist
py -m intraday_scanner.cli alpha-learn --db-path data\shadow_real.sqlite
```

## What "Not Enough Evidence Yet" Means

AlphaOps requires at least 20 real audited market days before it treats outcomes
as a meaningful empirical sample. Before 20 days, the model still runs, but it
labels expectancy as insufficient sample. Stronger evidence starts at 60 or more
real audited days.

This is intentional. A few big winners can make a small sample look good even
when the process is not reliable.

## Why The System May Refuse To Show Picks

Dawnstrike may refuse picks when:

- Sources returned no candidates.
- Candidate source data is stale, incomplete, or conflicting.
- Risk governor found hard avoid flags.
- The no-trade filter found no clean candidate above the minimum AlphaOps score.
- The system needs outcome evidence before trusting an edge bucket.

No-trade is a feature, not a bug. It keeps the system from inventing conviction
when the inputs are weak.

## How To Judge The Strategy

Do not judge Dawnstrike from sample fixtures or one strong day. The useful test
is repeated, real, point-in-time shadow use:

1. Let the morning workflow run.
2. Review Telegram and the dashboard.
3. Manually decide what to do outside Dawnstrike.
4. Import outcome data after the close.
5. Run the audit and learning commands.
6. Wait until there are at least 20 audited market days.
7. Treat 60+ days as a stronger evidence base.

Only audited outcomes from real days can support performance claims.
