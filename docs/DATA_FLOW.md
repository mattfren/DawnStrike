# Dawnstrike Data Flow

## 1. Inputs

### StockAnalysis

Configured in `config\web_sources.yaml` as `stockanalysis_premarket`, type
`public_table_url`. This is an enabled public/free web source for premarket
candidates.

### TradingView

Configured as `tradingview_premarket`, type `public_table_url`. This is also an
enabled public/free web source for premarket candidates.

### Local Inbox

Configured as `local_inbox`, type `local_inbox`, path:

```text
data\inbox\screener
```

This is the first practical fallback. Drop a screener CSV here when public
sources fail or need manual replacement.

### Manual Outcomes

Outcome files go here:

```text
data\inbox\outcomes\outcomes_YYYY-MM-DD.csv
```

The template is `templates\manual_outcomes_template.csv`.

### Optional Future Sources

The repo supports optional sources such as Alpaca market data, NewsAPI,
Finnhub, SEC RSS/EDGAR, Nasdaq halt RSS, browser-rendered tables, and CSV
enrichment files. Paid/live data would improve reliability, but the current
active AlphaOps workflow is built around zero-dollar/manual/public shadow data.

## 2. Normalization

### Canonical Snapshot Rows

Source rows are normalized into the canonical snapshot shape from
`intraday_scanner/models.py` and `docs/DATA_CONTRACT.md`. Important fields:

- ticker
- company
- previous close
- premarket price
- premarket high/low
- premarket volume
- dollar volume
- gap percent
- float shares
- market cap
- spread percent
- short float percent
- catalyst/headline fields
- halt/offering/reverse split flags
- source
- timestamp

### Previous Close Missing

If previous close is missing, normalization can still preserve the row, but the
row receives warnings and lower confidence. Risk logic adds a soft penalty for
missing previous close. Gap calculations are less trustworthy.

### High/Low Range Missing

If premarket high or low is missing, the scanner may use current/pre-market
price where available and mark the coverage warning. Risk logic adds soft
penalties for missing high and missing low. Range position and target/exit reads
become less reliable.

### Source Confidence

Source confidence is based on whether a source returned rows, how many rows were
normalized, how many were rejected, whether the source is public/free, and
whether the data is stale. A failed or empty source has low or zero confidence.

### Duplicate Tickers

`web_auto_collect` groups rows by ticker, picks the best row by data quality,
source priority, and timestamp, records source count, and marks consensus:

- `single_source`
- `multi_source_clean`
- `multi_source_conflict`

Conflicts can include price, gap, or volume differences across sources.

## 3. Outputs

### Scan Runs

The scan writes/persists:

- scan summary
- ranked candidates
- top explosive candidates
- avoid list
- recommendation theses

### Ranked Candidates

Ranked candidates are non-avoid scanner names sorted by score and liquidity.
Avoid names are kept separately so the dashboard can show why they were blocked.

### Alpha Signals

AlphaOps converts ranked candidates into AlphaOps signals with:

- alpha score
- edge bucket
- confidence bucket
- expected return bucket
- risk flags
- can-alert flag
- no-trade reason
- setup key
- watch/exit/target levels

### Telegram Notifications

Notifications are stored in `notifications_sent` and sent through configured
notifiers. Telegram messages are compact summaries, not raw JSON.

### Outcome Labels

Manual outcomes plus saved AlphaOps signals become `alpha_outcome_labels`.
Labels include timed winners/returns, high-after-entry opportunity, low
drawdown, setup key, source, rank, and score context.

### Performance Reports

Reports summarize audited outcomes, evidence status, win rate, drawdown,
outlier dependency, setup/source/risk buckets, and top1/top3/top5 behavior.
The historical ledger also writes return-attribution reports after outcomes are
imported.

## 4. Storage

Important SQLite tables from `intraday_scanner/storage/sqlite_store.py`:

| Table | What it stores |
| --- | --- |
| `scan_runs` | One persisted scan run with source/config/summary JSON. |
| `ranked_candidates` | Ranked non-avoid scanner candidates. |
| `top_explosive` | Top explosive/watchlist candidates. |
| `avoid_list` | Candidates blocked by scanner risk/quality filters. |
| `recommendation_theses` | Human-readable recommendation thesis rows. |
| `notifications_sent` | Dedupe key, channel, sent time, and notification payload. |
| `source_health` | Aggregated source health and diagnostic payloads. |
| `provider_health` | Provider count/status telemetry. |
| `web_fetch_runs` | Web/source collection run summaries. |
| `web_fetch_results` | Individual web/source result rows. |
| `raw_source_artifacts` | Saved raw artifacts when configured. |
| `normalized_source_rows` | Canonical rows produced by source collection. |
| `manual_outcomes` | Imported manual outcome CSV rows. |
| `manual_audit_trades` | Audited returns from manual outcomes. |
| `manual_audit_summary` | Manual audit summary JSON. |
| `performance_daily` | Persisted paper performance report. |
| `performance_cumulative` | Cumulative performance snapshots. |
| `alpha_feature_vectors` | AlphaOps model feature JSON for each candidate. |
| `alpha_signals` | AlphaOps scored signals and no-trade metadata. |
| `alpha_outcome_labels` | Outcome labels used for learning. |
| `alpha_learning_runs` | Learning run payloads and truth reports. |
| `alpha_source_reliability` | Source reliability statistics and score. |
| `alpha_setup_memory` | Setup-level outcome memory. |
| `historical_signals` | Point-in-time AlphaOps picks and no-trade days used by the historical ledger. |
| `signal_events` | Created, notified, monitor, invalidation, and thesis-break events for historical signals. |
| `signal_outcomes` | Imported point-in-time outcome prices matched to historical signals. |
| `signal_return_attribution` | Scenario/paper returns by entry and exit policy. Missing outcomes stay pending. |
| `daily_signal_performance` | Daily top1/top3/top5 paper return summaries from attributed outcomes. |
| `halt_events` | Nasdaq halt events when enabled. |
| `sec_risk_events` | SEC risk/enrichment events when enabled. |

DB files under `data\*.sqlite` are ignored by Git.
