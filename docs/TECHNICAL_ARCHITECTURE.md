# Dawnstrike Technical Architecture

## System Diagram

```text
Windows Task Scheduler
  -> alpha-cycle
  -> web source collection
  -> normalization
  -> Signal Engine v3 scoring
  -> feature vectors
  -> AlphaOps scoring
  -> risk governor
  -> no-trade filter
  -> Telegram
  -> SQLite
  -> dashboard
  -> outcomes
  -> audit
  -> learning
```

## CLI Layer

`intraday_scanner/cli.py` is the command entry point. Important commands:

- `web-source-doctor`
- `web-auto-collect`
- `telegram-test`
- `alpha-cycle`
- `alpha-monitor`
- `alpha-status`
- `alpha-report`
- `attribute-returns`
- `historical-report`
- `import-manual-outcomes`
- `audit-manual-outcomes`
- `alpha-learn`
- `calendar-report`
- `automation-*`
- `paper-audit`
- `performance-report`

The CLI prints JSON/status summaries, writes files under `outputs`, and persists
state to SQLite when requested.

## Web Source Collection

Source code:

- `intraday_scanner/services/web_collection_service.py`
- `intraday_scanner/providers/web_source_base.py`
- `intraday_scanner/providers/public_table_provider.py`
- `intraday_scanner/providers/browser_table_provider.py`

`web_auto_collect`:

1. Checks local inbox first.
2. Reads enabled public table sources in priority order.
3. Optionally uses browser-rendered extraction for enabled browser sources.
4. Optionally enriches rows with halt and SEC risk sources if enabled.
5. Deduplicates duplicate tickers across sources.
6. Writes `premarket_snapshot.csv`, `source_summary.json`, and
   `data_quality_report.json`.
7. Persists fetch/source health rows when `persist=True`.

## Public Table Providers

Public table providers fetch HTML tables, choose the best table, normalize
headers, reject unusable rows, and label coverage warnings such as missing
previous close, unknown float, unverified URL data, halt status unverified, and
SEC risk unverified.

Public/free web data is shadow data. It is useful for testing but should be
verified before relying on it.

## Source Doctor

`web-source-doctor` runs each configured source and reports status, row counts,
rejection reasons, unknown field counts, source confidence, stale status, and
next action. It is the first command to run when the morning workflow returns no
data.

## Signal Engine v3

Source code:

- `intraday_scanner/scoring.py`
- `intraday_scanner/formula.py`
- `intraday_scanner/services/premarket_intelligence.py`

Signal Engine v3 scores the canonical snapshot. It produces:

- total score
- explosive score
- tradability score
- catalyst score
- risk score
- data quality score
- expected return bucket
- confidence bucket
- uncertainty bucket
- source lineage
- watch levels: breakout trigger, invalidation/exit line, first target

The formula weighs gap curve, liquidity thrust, float rotation, range control,
squeeze/catalyst, execution quality, and data quality, then subtracts risk
penalties.

## AlphaOps v4

Source code:

- `intraday_scanner/services/alpha_cycle_service.py`
- `intraday_scanner/alpha/alpha_model.py`
- `intraday_scanner/alpha/feature_factory.py`
- `intraday_scanner/alpha/risk_governor.py`
- `intraday_scanner/alpha/no_trade_filter.py`
- `intraday_scanner/alpha/edge_calibrator.py`
- `intraday_scanner/services/learning_service.py`

AlphaOps takes Signal Engine candidates and creates a second, evidence-aware
research layer:

1. Build feature vectors.
2. Load historical outcome labels.
3. Load setup memory and source reliability.
4. Apply risk governor.
5. Score each candidate using base score, explosive score, catalyst score,
   execution score, expected edge, source reliability, and risk score.
6. Use no-trade review to decide whether to alert or stand down.
7. Persist signals.
8. Send Telegram.

## Risk Governor

The risk governor creates hard avoids and soft penalties.

Hard avoids include:

- current halt
- recent/active offering or dilution
- zero volume
- sub-minimum price
- stale source
- source conflict
- missing/no source confidence

Soft penalties include:

- unknown float
- missing previous close
- missing high/low
- no catalyst
- public URL unverified
- low source count
- mega gap
- wide spread

Hard avoids block alerting. Soft penalties reduce the risk score.

## No-Trade Filter

The no-trade filter returns no-clean-edge when:

- no candidates exist
- source status is failed/empty/no data
- all candidates are blocked by risk, weak edge, or stale data
- every clean candidate has low source confidence
- the top candidate risk score is too weak

This is deliberate. The system should not force a watchlist.

## Telegram Notifier

Source code:

- `intraday_scanner/notifiers/service.py`
- `intraday_scanner/notifiers/webhooks.py`
- `intraday_scanner/notifiers/telegram_formatter.py`

Telegram messages are formatted as compact watchlist/status messages. Tokens and
chat IDs are read from environment/config and are not printed in messages.
Notification attempts are persisted in `notifications_sent` with dedupe keys.

## SQLite Persistence

Source code:

- `intraday_scanner/storage/sqlite_store.py`

SQLite is the local operating memory. It stores scans, candidates, source
health, notifications, monitor events, manual outcomes, audit rows, AlphaOps
feature vectors, AlphaOps signals, learning runs, source reliability, setup
memory, and reports.

Default operating DB:

```text
data\shadow_real.sqlite
```

SQLite DB files are ignored by Git.

## Dashboard

Source code:

- `app.py`
- `intraday_scanner/dashboard/data_loader.py`
- `intraday_scanner/dashboard/components.py`
- `intraday_scanner/dashboard/display_text.py`

The dashboard reads SQLite, output folders, or sample files and turns raw data
into plain-English display objects. The simplified tabs are:

- `Today`
- `Picks`
- `Calendar`
- `Performance`
- `System`

## Outcome/Audit Engine

Source code:

- `intraday_scanner/services/free_shadow_mode.py`
- `intraday_scanner/services/audit_service.py`
- `intraday_scanner/services/return_attribution_service.py`
- `intraday_scanner/alpha/outcome_labeler.py`

Manual outcome CSVs become manual outcome rows, then audited rows, then AlphaOps
outcome labels and historical return-attribution rows. Missing price fields
remain unavailable.

## Learning/Reporting Layer

Source code:

- `intraday_scanner/services/learning_service.py`
- `intraday_scanner/alpha/performance_truth.py`
- `intraday_scanner/alpha/setup_memory.py`
- `intraday_scanner/services/source_reliability_service.py`

Learning updates:

- outcome labels
- setup memory
- source reliability
- truth report
- evidence warnings

Empirical scoring is held back until enough real audited days exist.

## Windows Task Scheduler

`scripts/register_alphaops_tasks.ps1` registers the active scheduled workflow.
The tasks run least privilege, interactive token, weekdays only, with no broker
order execution.
