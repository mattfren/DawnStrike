# Dawnstrike Mover Pattern Lab

## Purpose

Mover Pattern Lab turns timestamped, point-in-time market observations into
auditable same-session paper evidence. It is a research and simulated-paper
boundary only. It has no broker connection, live order path, or self-editing
strategy logic.

The goal is not to promise a target return. The goal is to make every claim
reproducible:

1. What was knowable at the decision cutoff?
2. Which frozen strategy version accepted or rejected the setup?
3. What was the next executable paper observation?
4. What happened after spread, slippage, and fees?
5. Did the result survive chronological validation and an untouched test?

## Two datasets that must never be mixed

descriptive_eod_movers is produced after the close. It explains what the day's
realized leaders had in common. It is never eligible to emit a historical
morning paper signal.

prospective_mover_snapshots is frozen at an explicit intraday cutoff. Only
features available by feature_cutoff_at may enter it. Only this dataset may emit
a paper signal.

Every prospective row must also prove when and how its symbol entered the
universe. Accepted methods are premarket screen, scheduled universe, prior
session watchlist, or live intraday scan. A closing-winner or EOD-ranked
universe is rejected.

Using a closing top-gainer list to select a 9:45 a.m. trade is direct lookahead
bias and is blocked by contract.

## Point-in-time features

The initial contract retains:

- prior close, session open, current price, and split-adjustment status;
- completed opening-range high and low;
- running VWAP through the cutoff;
- cumulative volume and dollar volume through the cutoff;
- same-clock relative volume: current cumulative RTH volume divided by the
  median cumulative volume through the same clock time over prior valid
  sessions;
- executable spread;
- verified halt, offering, reverse-split, and source-conflict state;
- catalyst source, publication time, and verification state; and
- immutable source references and hashes.

The combined spread, halt, corporate-action, source-conflict, and catalyst
context observation must be timestamped no later than the cutoff and no more
than five minutes old. Multiple context rows per symbol/day are allowed so each
cutoff uses the newest eligible observation without borrowing from the future.

Unknown is not false. A missing halt, corporate-action, catalyst, or spread
check blocks or skips a paper signal according to the frozen strategy contract.

## Frozen forward-paper hypotheses

### mover_opening_drive_rvol_v1 at v1.0

Tests whether a completed opening-range drive with strong same-clock relative
volume, sufficient dollar volume, a VWAP hold, and executable liquidity has
positive same-session after-cost expectancy.

### mover_verified_catalyst_gap_hold_v1 at v1.0

Tests whether a split-adjusted gap with a timestamped verified catalyst,
opening-range retention, abnormal flow, VWAP support, and executable liquidity
has positive same-session after-cost expectancy.

These names are hypotheses, not performance claims. Their logic and parameters
are immutable within version v1.0.

## Paper lifecycle

For every snapshot and strategy, the lab writes a paper-signal, rejected, or
skipped decision. A paper-signal decision produces one signal per
strategy/symbol/session.

Paper entry uses the open of the first complete bar whose grid-aligned open is
not earlier than the authoritative forward receipt, never any price that was
already in progress when the system received the source. The expected bar
interval is explicit and every bar from entry through exit must be present.
Long fills receive adverse slippage. Fees are charged on entry and exit. Stops
and targets are evaluated on subsequent same-session bars. If a bar touches
both, the stop is applied first. A gap through a stop exits at the adverse bar
open, not the stale stop. Any remaining position closes only when the official
exchange-close bar is retained, including early closes. Missing or misaligned
bars produce a pending outcome with return null, never zero.

Snapshots, decisions, signals, bar fragments, and reconciliation observations
are content-addressed. Appending a later bar or context observation creates a
new run artifact without changing an earlier cutoff identity.

Each strategy version has an independent evidence series. No live execution is
possible.

## Evidence and promotion gates

The lab reports discovery, validation, and locked chronological test slices.
Feature correlations are calculated only on the discovery slice and are
reported with a Bonferroni multiple-testing threshold.

A version can reach manual_review_candidate only after all gates pass:

- at least 30 forward sessions;
- at least 30 closed, source-complete paper trades;
- at least 95% resolved evidence coverage across emitted signals;
- positive after-cost expectancy;
- a positive lower 95% confidence bound; and
- positive validation and locked-test slices.

Automatic promotion remains disabled. Historical replay cannot become forward
evidence.

## Operator commands

    py -m intraday_scanner.v2.mover_pattern_lab init
    py -m intraday_scanner.v2.mover_pattern_lab audit --db-path data/shadow_real.sqlite
    py -m intraday_scanner.v2.mover_pattern_lab build-snapshots --bars-csv PATH --context-csv PATH --date YYYY-MM-DD --bar-timestamp-semantics bar_close
    py -m intraday_scanner.v2.mover_pattern_lab paper-scan --snapshots PATH --expected-market-dates YYYY-MM-DD
    py -m intraday_scanner.v2.mover_pattern_lab reconcile --signals PATH --bars-csv PATH --bar-timestamp-semantics bar_close
    py -m intraday_scanner.v2.mover_pattern_lab analyze --scan-manifest PATH --reconcile-manifest PATH
    py -m intraday_scanner.mover_pattern_operator_cli --config config/mover_daily_workflow.json --stage scan --cutoff 09:45
    py -m intraday_scanner.mover_pattern_operator_cli --config config/mover_daily_workflow.json --stage reconcile
    py -m intraday_scanner.v2.mover_pattern_lab verify

The manual research wrappers preserve returned content-addressed paths. The
configured daily operator also persists per-cutoff state, calls cumulative core
analysis, verifies evidence, and uses the existing durable notifier receipts.
See `docs/operations/mover_pattern_daily_workflow.md` for input contracts and
safe scheduler registration.

```powershell
.\scripts\run_mover_pattern_scan.ps1 `
  -BarsCsv PATH `
  -ContextCsv PATH `
  -MarketDate YYYY-MM-DD `
  -EvidenceMode historical_replay `
  -BarIntervalMinutes 5

.\scripts\run_mover_pattern_reconcile.ps1 `
  -SignalsPath PATH `
  -ScanManifest PATH `
  -BarsCsv PATH `
  -BarIntervalMinutes 5
```

Input bar timestamps must be timezone-aware bar-close timestamps. Context CSV
rows require market_date, symbol, context_observed_at, universe_selected_at,
universe_source_ref, and universe_selection_method. Future or outcome fields
are rejected. Unknown risk facts remain blank and cause a skipped decision.

`analyze` emits cumulative compatible strategy evidence and a clickable
day-by-day strategy calendar. A complete evaluated no-setup day is a truthful
0% cash return; a missing scan, unresolved signal, or incomplete outcome remains
null.
