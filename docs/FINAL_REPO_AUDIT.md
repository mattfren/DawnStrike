# Final Repo Audit

This repo is a local research/watchlist engine with offline tests and optional
free public-source collection. It is not an order execution system.

## Current Audit Matrix

| Area | Status | Evidence |
| --- | --- | --- |
| CLI commands | PASS | `py -m intraday_scanner.cli --help` exposes scan, web collection, Telegram, outcome, audit, performance, tuning, and scheduler commands. |
| Config | PASS | Example and local config paths are separated; secrets stay in `.env` or ignored local YAML. |
| Web source collection | PASS | `web_auto_collect` tries local inbox, StockAnalysis, TradingView, optional browser/public fallbacks, then halts/SEC enrichment. |
| Browser extraction | PASS | Browser extractor is optional and reports install/login/table failures clearly. |
| TradingView/StockAnalysis normalization | PASS | Live source doctor normalized StockAnalysis and TradingView rows. Previous close and premarket range are unavailable from those public tables and are flagged. |
| Telegram | PASS | Compact watchlist, source check, alert, outcome reminder, and summary formatters exist with dedupe persistence. |
| Notification persistence/dedupe | PASS | Telegram test uses dry-run/real/force event keys and persists attempt metadata. |
| Scoring | PASS | Signal Engine v3 emits component scores, source lineage, config hash, expected bucket, and confidence bucket. |
| Formula/model versioning | PASS | Formula version is `dawnstrike-signal-engine-v3`. |
| Data quality scoring | PASS | Data quality is 0-100 and source confidence/staleness feed the candidate payload. |
| Catalyst/risk handling | PASS | Catalyst categories and halt/offering/split/spread/float/source risk flags are persisted. |
| SEC/halt enrichment | PARTIAL | Services exist; enrichment sources are disabled in the active local config used for verification. |
| Outcomes/audit | PASS | Official calls are required before manual outcomes; pre-call outcomes are rejected. |
| Performance | PASS | Reports include top baskets, compounded curves, buckets, sample warning, best/worst pick/day, and outlier warning. |
| Dashboard | PASS | Shows picks, source status/confidence, Telegram preview, audit/performance, model/config fields, and no-trading boundary. |
| Scheduler/scripts | PASS | Automation and Windows task scripts are notification/research oriented and covered by no-order safety search. |
| Tests | PASS | Offline suite passed: 134 tests. |
| Docs | PASS | Signal Engine v3, data quality, Telegram, performance, tuning, runbook, and final report docs exist. |
| Security boundary | PASS | No order execution path is implemented in app, scanner services, or scripts. |
| Artifact hygiene | PASS | `.gitignore` excludes secrets, SQLite DBs, outputs, logs, cache, raw data, and Streamlit secrets. |

## Blockers

Live source availability, Telegram delivery, and real forward performance require
current public pages or operator-provided secrets/data. Public web data remains
unverified shadow data until validated against a paid/live source.
