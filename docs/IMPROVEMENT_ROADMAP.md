# Improvement Roadmap

## Stage 1: Current Free Automation

Current system:

- public web sources
- local CSV fallback
- Telegram notifications
- manual outcome import
- SQLite evidence store
- dashboard
- 20-day shadow test

Goal: prove the workflow is reliable before spending money or adding more
complexity.

Operator focus:

- let the scheduled workflow run
- import outcomes every market day
- fix source failures quickly
- do not tune based on sample fixtures

## Stage 2: Free Read-Only API Improvement

Possible upgrades:

- free Alpaca/IEX or similar read-only market data
- automated outcome capture if available
- better current-price monitoring
- fewer manual outcome rows

Benefits:

- less manual work
- cleaner monitor checks
- better timestamp consistency

Limits:

- free feeds may be delayed, incomplete, or limited
- enrichment fields may still be missing

## Stage 3: Paid Level 1 Data

Possible upgrades:

- real-time SIP or high-quality market data
- reliable premarket high/low/volume/spread
- live current-price monitoring
- automated outcome capture

Benefits:

- lower source conflict
- better entry/exit simulation
- more reliable monitor status
- fewer stale/free-source failures

This is likely the biggest quality jump if Dawnstrike is used daily.

## Stage 4: Better Enrichment

Useful enrichment:

- float
- short interest
- SEC filings
- halt feeds
- news/catalyst provider
- offering/dilution detection
- reverse split history

Benefits:

- fewer unknown-float penalties
- better risk governor decisions
- fewer dangerous low-quality names
- stronger catalyst scoring

## Stage 5: Evidence-Based Model Tuning

Only tune after real evidence exists:

- minimum: 20 audited real market days
- stronger: 60+ audited real market days
- preferred: multiple market regimes

Tune from:

- audited outcomes
- setup memory
- source reliability
- risk flag impact
- score deciles
- drawdown and outlier dependency

Do not tune from:

- sample fixtures
- cherry-picked screenshots
- missing outcome rows
- high-of-day-only fantasy exits

## Stage 6: Optional Broker Workflow

There is no current order execution path.

If a broker workflow is ever considered later, the safer first step is still
manual approval:

1. Dawnstrike sends research alert.
2. Operator reviews.
3. Operator approves or rejects.
4. Any broker action remains explicit and controlled.

Fully automated order execution should not be added without a separate design,
risk review, audit trail, kill switch, position sizing rules, compliance review,
and explicit user decision.

## Highest-Value Next Improvements

1. Automate outcome collection from a reliable read-only price feed.
2. Add stronger float/short interest/SEC enrichment.
3. Improve live monitor current-price data.
4. Keep collecting at least 20 real audited days.
5. Review risk flag impact after 20+ days.
6. Revisit scoring weights only after evidence is real.
