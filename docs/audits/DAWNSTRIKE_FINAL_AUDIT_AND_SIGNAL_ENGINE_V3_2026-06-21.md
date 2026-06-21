# Dawnstrike Final Audit and Signal Engine v3 - 2026-06-21

## 1. Executive Summary

Dawnstrike is now a local research/watchlist and paper-validation engine with
Signal Engine v3, source-health diagnostics, compact Telegram alerts, no-lookahead
outcome handling, honest performance reporting, and operator docs. It remains
research only. No order execution path was added.

## 2. What Is Built

- Public/free source collection through local inbox, StockAnalysis, TradingView,
  and optional disabled fallbacks.
- Signal Engine v3 with explainable components and source lineage.
- SQLite persistence for scans, recommendations, source health, notifications,
  outcomes, audits, and performance reports.
- Compact Telegram watchlist, alert, source-check, outcome-needed, and summary
  messages.
- Streamlit dashboard with latest picks, source status, Telegram preview,
  model/config fields, audit/performance views, and no-trading boundary.

## 3. What Changed

- Upgraded formula version to `dawnstrike-signal-engine-v3`.
- Added v3 candidate fields: total, explosive, tradability, catalyst, risk,
  source confidence, expected bucket, confidence bucket, source lineage, model
  version, and config hash.
- Replaced legacy emoji action strings with allowed labels.
- Treated unknown float as a penalty, not a fabricated rotation.
- Added source confidence, stale status, per-source diagnostics, and duplicate
  source conflict metadata.
- Reworked Telegram watchlists into the compact operator format.
- Added performance buckets, sample warnings, outlier warnings, and best/worst
  day fields.
- Added fixture-only tuning labels and overfit-risk warnings.
- Hardened outcome automation so reminder templates are not imported as completed
  outcomes.

## 4. Source Collection Status

PASS. Live verification on `config\web_sources.yaml` normalized 110 rows from
StockAnalysis and TradingView and deduped them into 103 candidates. Local inbox
was empty. SEC and halt enrichment were disabled in the active config.

## 5. Source-Doctor Results

`web-source-doctor` result:

- status: `complete`
- enabled candidate sources: `local_inbox`, `stockanalysis_premarket`,
  `tradingview_premarket`
- enabled universe sources: `nasdaq_symbols`
- enabled enrichment sources: none
- rows extracted: 120
- rows normalized: 110
- rows rejected: 0
- candidate count: 110
- source confidence: 85.0
- stale data status: `fresh`
- next action: run `web-auto-collect`

## 6. Scoring Algorithm Details

Signal Engine v3 ranks high-volatility premarket candidates by combining gap
quality, liquidity, float rotation, range control, catalyst quality, execution
quality, data quality, and risk penalties. The score is explainable and clamped
to 0-100.

## 7. Signal Engine v3 Formula

```text
score =
  gap_curve
  + liquidity_thrust
  + float_rotation
  + range_control
  + squeeze_catalyst
  + execution_quality
  + data_quality
  - risk_penalty
```

Derived v3 surfaces include `explosive_score`, `tradability_score`,
`catalyst_score`, `risk_score`, `expected_return_bucket`, and
`confidence_bucket`.

## 8. Score Component Table

| Component | Inputs | Purpose |
| --- | --- | --- |
| gap_curve | gap percent | Rewards meaningful gaps and fades exhaustion. |
| liquidity_thrust | dollar and share volume | Filters thin names. |
| float_rotation | known float and premarket volume | Rewards real float pressure. |
| range_control | high/low/current price | Rewards holding near highs. |
| squeeze_catalyst | headline, short float, float scarcity | Scores catalyst and squeeze context. |
| execution_quality | spread and price band | Penalizes difficult execution context. |
| data_quality | required fields and timestamps | Prevents weak data from ranking too high. |

## 9. Risk Penalty Table

| Risk | Effect |
| --- | --- |
| current halt | hard penalty and avoid reason |
| recent offering | major penalty and avoid reason |
| reverse split | risk flag and penalty |
| unknown float | penalty, no fabricated rotation |
| stale source | risk penalty |
| wide spread | risk flag and penalty |
| sub-min price | avoid reason |
| low dollar/share volume | avoid reason |
| low source confidence | risk penalty |

## 10. Data Quality Rules

Every row carries source, source URL when available, extraction mode, source
timestamp, extracted timestamp, stale flag, source confidence, source count,
preferred source, merge reason, and conflict flags. Public URL rows remain
unverified shadow data.

## 11. Catalyst Classification Rules

Catalysts are categorized as `confirmed_catalyst`, `soft_catalyst`,
`sympathy_momentum`, `no_clear_catalyst`, `dilution_risk`, or
`legal/regulatory_risk`. Missing or vague headlines reduce confidence.

## 12. Telegram Examples

Watchlist messages now use:

```text
🚀 Dawnstrike Watchlist
⏱ 8:15 CT | 3 picks | Source: web/manual

1) NOVA - 88.1 | +89% | $5.20
   🎯 $5.48 | 🛑 $3.20
   📰 FDA phase 2 data
   ⚠️ none

🚫 Avoid: 1
Research only. No orders placed.
```

Source-failure messages use `📡 Dawnstrike Source Check`, show attempted sources,
and give one next action.

## 13. Outcome Audit Rules

PASS. Manual outcomes require an official saved call first. Outcomes before the
recommendation timestamp are rejected. Missing values remain unavailable, not
zero. Top1/Top3/Top5 are equal-weight, and compounded curves are reported.

## 14. Strategy Tuning Status

PASS. `tune-strategy` labels fixture-only runs, reports overfit risk, uses a
balanced objective, and recommends collecting 20+ real shadow days when real
outcomes are insufficient.

## 15. Dashboard Status

PASS. Dashboard surfaces current picks, source confidence, source failures, data
quality warnings, avoid list, Telegram preview, audit/performance summaries,
model version, config hash, and the research-only boundary.

## 16. Tests Run

- `py -m pip install -e ".[dev]"` - passed
- `py -m pytest -p no:cacheprovider` - 134 passed
- `py -m ruff check .` - passed
- `py -m mypy intraday_scanner` - passed
- `py -m compileall intraday_scanner app.py tests` - passed
- `git diff --check` - passed with CRLF warnings only

## 17. Pass/Fail Table

| Requirement | Status |
| --- | --- |
| source doctor clear results | PASS |
| StockAnalysis normalization | PASS |
| TradingView normalization | PASS |
| Signal Engine v3 fields | PASS |
| compact Telegram | PASS |
| outcome reminder tickers | PASS |
| no-data single source message | PASS |
| no-lookahead audit math | PASS |
| honest sample-size reporting | PASS |
| tests pass offline | PASS |
| no order execution exists | PASS |

## 18. No-Trading Safety Proof

Safety grep was run:

```powershell
rg -n "submit_order|place_order|create_order|TradingClient|alpaca\.trading|broker execution|auto trade|order submission|buy recommendation|sell recommendation" intraday_scanner app.py scripts
```

Result: no matches.

## 19. Remaining Blockers

- Public pages can change shape or availability without notice.
- SEC and halt enrichment are disabled in the active config.
- Real forward performance still needs completed outcome CSVs over multiple days.
- Telegram depends on secrets present in the operator environment.

## 20. What Requires Paid/Live Data

- Verified premarket quotes, previous close, spread, and volume.
- Reliable float, short interest, market cap, halt, and SEC risk enrichment.
- Historical intraday bars for broad backtests.
- Real forward outcome history for empirical expected return.

## 21. Exact Morning Command

```powershell
py -m intraday_scanner.cli web-source-doctor --config config\web_sources.yaml --out-dir outputs\source_doctor --print
py -m intraday_scanner.cli web-auto-collect --config config\web_sources.yaml --db-path data\shadow_real.sqlite --out-dir outputs\web_auto_test --persist --print
```

## 22. Exact Daemon Command

```powershell
py -m intraday_scanner.cli web-telegram-daemon --config config\web_sources.yaml --automation-config config\automation.yaml --db-path data\shadow_real.sqlite --out-root outputs\web_telegram --ai-mode none --notify telegram --max-cycles 1
```

## 23. Exact Outcome Command

```powershell
py -m intraday_scanner.cli import-manual-outcomes --input data\inbox\outcomes\outcomes_YYYY-MM-DD.csv --db-path data\shadow_real.sqlite --persist
py -m intraday_scanner.cli audit-manual-outcomes --db-path data\shadow_real.sqlite --out-dir outputs\manual_audit --persist
py -m intraday_scanner.cli free-shadow-report --db-path data\shadow_real.sqlite --out-dir outputs\shadow_report --persist
```

## 24. Exact Dashboard Command

```powershell
py -m streamlit run app.py --server.port 8502
```

## 25. Exact Git Commit/Push Instructions

```powershell
git add README.md app.py docs intraday_scanner tests
git commit -m "Add Signal Engine v3 and final Dawnstrike hardening"
git push origin main
```

Do not add `.env`, SQLite databases, `outputs`, `logs`, `data/raw`,
`data/cache`, or Streamlit secrets.
