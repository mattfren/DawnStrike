# Final Repo Audit

This repo is a local research/watchlist engine with offline tests and optional
free public-source collection. It is not an order execution system.

## Current Audit Matrix

| Area | Status | Evidence |
| --- | --- | --- |
| CLI commands | PASS | `py -m intraday_scanner.cli --help` exposes scan, web collection, Telegram, outcome, audit, performance, tuning, and scheduler commands. |
| Web source collection | PASS | `web_auto_collect` tries local inbox, StockAnalysis, TradingView, optional browser/public fallbacks, then halts/SEC enrichment. |
| Browser extraction | PASS | Browser extractor is optional and reports install/login/table failures clearly. |
| Normalization | PASS | Fixtures cover StockAnalysis, TradingView, MarketWatch rejection diagnostics, Investing, and browser tables. |
| Telegram | PASS | Compact watchlist, source check, alert, outcome reminder, and summary formatters exist with dedupe persistence. |
| Scoring | PASS | Signal Engine v3 emits component scores, source lineage, config hash, expected bucket, and confidence bucket. |
| Outcomes/audit | PASS | Official calls are required before manual outcomes; pre-call outcomes are rejected. |
| Performance | PASS | Reports include top baskets, compounded curves, buckets, sample warning, best/worst pick/day, and outlier warning. |
| Dashboard | PASS | Shows picks, source status/confidence, Telegram preview, audit/performance, model/config fields, and no-trading boundary. |
| Security boundary | PASS | No order execution path is implemented in app, scanner services, or scripts. |
| Artifact hygiene | PASS | `.gitignore` excludes secrets, SQLite DBs, outputs, logs, cache, raw data, and Streamlit secrets. |

## Blockers

Live source availability, Telegram delivery, and real forward performance require
current public pages or operator-provided secrets/data. Public web data remains
unverified shadow data until validated against a paid/live source.
