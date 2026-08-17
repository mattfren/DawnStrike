# Dawnstrike quant-refactor current-state audit

Audit date: 2026-08-11  
Sol role: principal architect, quant auditor, and controller  
Implementation candidate worktree: `C:\r\dawnstrike-quant-refactor-20260811`  
Candidate branch: `codex/sol-quant-refactor-20260811`  
Candidate HEAD at audit start: `bec32fe752b91f4e1357236a538a6dfea5da56bf`  
Clean production baseline: `origin/main` at `ba39a5353045b7d417936ed1aed0ee4802169759`

## Audit authority and isolation

The controlling mission is the pasted SOL/LUNA closed-loop brief supplied on
2026-08-11. Source code, immutable artifacts, tests, and read-only database
queries outrank older documentation.

The primary checkout at `C:\Users\MattFields\Dawnstrike` was dirty before this
work began. Its Calendar/UI changes and two untracked audit documents were not
edited, staged, copied, or deleted. A separate Harvest worktree also contains
uncommitted canonical-return work. This lane is isolated from both.

The candidate branch fast-forwarded to the committed Harvest candidate because
it is a strict descendant of `origin/main` and contains reusable intraday
evidence, catalyst, validation, and observer-safety work. Adoption is not
certification: every adopted component remains subject to independent audit.

No command in Stage A mutated `C:\r\dawnstrike-runtime`,
`C:\r\dawnstrike-state`, a scheduler, a deployment, or broker state. The mounted
SQLite database was opened with URI `mode=ro`, `PRAGMA query_only=ON`, and passed
`PRAGMA quick_check`.

## Repository shape

- Python 3.13 package with Streamlit, static public assets, SQLite, Windows task
  scripts, and Vercel health/readiness endpoints.
- `app.py` is the active local Streamlit entrypoint and remains a monolith of
  roughly 4,513 lines.
- `intraday_scanner/cli.py` exposes the operational command surface.
- The candidate contains 126 top-level test files before this refactor.
- CI runs Pytest, Ruff, mypy, compileall, Node syntax validation, dependency
  audit, Bandit, SBOM generation, PowerShell parsing, and secret scanning.
- Schema migrations are explicit; the candidate's current schema version is 26.

## Mounted product and deployment surfaces

The local Streamlit product mounts `Today`, `Picks`, `Calendar`, `Performance`,
and `System`. The static public dashboard exposes Overview, Calendar,
Performance, Research, Scenarios, and System views. Neither surface currently
mounts a canonical “Today's Best Opportunities” symbol-plus-strategy view.

Vercel deploys a static artifact plus read-only `/api/health` and
`/api/readiness`. Git-triggered deployment is disabled. No scan, notification,
broker, or cron API route is exposed by `vercel.json`.

## Current production-oriented AlphaOps flow

The mounted morning path is `scripts/run_alphaops_morning.ps1` to
`intraday_scanner.cli alpha-cycle` to
`services.alpha_cycle_service.alpha_cycle`.

Observed order of operations:

1. Apply the trading-session gate.
2. Initialize the SQLite store.
3. Collect configured mover sources through `web_auto_collect`.
4. Enrich already-collected rows with premarket facts and recent news.
5. Run `ScanService`, which calls `score_universe` on the collected snapshot.
6. Verify SEC safety for ranked rows.
7. Build AlphaOps feature vectors from the already-scored candidates.
8. Run `AlphaModel.score_candidates` and alert gates.
9. Run the lightweight regime detector over the resulting signals.
10. Build V6 shadow decisions, record historical signals, select/watch/no-trade,
    persist evidence, and notify.

This is mover/setup-first. Candidate collection and formula scoring happen
before normalized cross-market anomaly discovery, market/security regime
classification, and strategy matching. It does not satisfy the controlling
market-first requirement.

## Universe and providers

The configured local source set includes a local inbox, authenticated Alpaca
screener input, StockAnalysis and TradingView public mover pages, a Nasdaq
symbol directory, Nasdaq halt RSS, and SEC EDGAR. Several alternate public
pages are disabled fallbacks.

The provider abstractions include:

- CSV snapshot and enrichment providers;
- Alpaca snapshots, one-minute bars, quotes, trades, and read-only historical
  page contracts;
- Massive/Polygon read-only bars, trades, quotes, and corporate-action page
  contracts;
- Yahoo chart bars;
- Nasdaq symbol and halt sources;
- Alpaca, NewsAPI, and Finnhub news adapters;
- SEC RSS and EDGAR filing/document acquisition.

Provider interfaces are stronger than the mounted discovery flow. The mounted
flow still begins with mover tables or an Alpaca screening universe rather than
a complete, timestamped tradeable-market cross-section.

No current evidence proves consolidated historical NBBO, depth, aggressor-side
classification, or full-market historical entitlement. Alpaca is configured as
IEX in the example environment. True CVD and order-flow strategies must remain
unavailable unless entitled trade/quote data supports defensible
classification.

## Feature state

Reusable feature code exists in three layers:

- `formula.py` and `scoring.py`: gap, liquidity thrust, float rotation, range
  position/control, catalyst, execution quality, data quality, and risk flags;
- `alpha/feature_factory.py`: grouped price/momentum, liquidity/execution,
  source quality, catalyst, risk, structure, and playbook fields;
- `v2/indicators/core.py`: deterministic SMA, ROC, realized volatility, RSI,
  ATR, Bollinger, and Donchian calculations.

The AlphaOps feature factory stores timestamped feature JSON, but most values
are raw values or fixed buckets. There is no canonical cross-sectional
FeatureSnapshot with normalized z-scores/percentiles, explicit availability,
feature lineage, and a hard `observed_at <= decision_at` invariant for the whole
opportunity path.

## Regime state

`alpha/regime_detector.py` derives only `NO_DATA`, `HOT_HIGH_BETA`, `SELECTIVE`,
or `THIN_OR_RISKY` from average candidate gap, dollar volume, and clean-row
count. It is a report label, not a broader-market plus security-specific regime
engine. It does not model trend, mean reversion, volatility expansion or
compression, breakout/breakdown, exhaustion, chop, or insufficient-data states
from causal time-series evidence.

## Strategy registry and lifecycle

Reusable strategy infrastructure exists:

- immutable `StrategySpec` and `StrategySignal` dataclasses;
- stable strategy fingerprints;
- a combined catalog with six experimental daily strategies, two experimental
  gap-continuation research strategies, and cash/buy-hold comparators;
- a causal research-only AlphaOps V5 intraday adapter;
- PaperOps experiment/governance overlays;
- deterministic promotion gates and V6 experiment/holdout registries.

The current strategy catalog is not the required DS family registry. Lifecycle
labels are fragmented across `status`, `validation_status`, V6 review records,
and PaperOps overlays. There is no single typed state machine equivalent to
EXPERIMENTAL through PRODUCTION_ELIGIBLE/DEGRADED/DISABLED/REJECTED.

## Expectancy, scoring, ranking, and gating

The legacy expectancy model is conservative about sparse samples, but it
estimates percentage return from heuristic priors plus similar paper-audit
returns. AlphaOps scores and ranks symbols, then selects a watchlist with
hard/soft filters. V5 has an independently derived target rule, after-cost
reward/risk, fixed-fractional paper risk, symbol notional caps, and a no-live
boundary.

The v2 latest scanner evaluates strategy triggers but sorts candidate cards by
setup score. It does not rank symbol-plus-strategy pairs by validated expectancy
and uncertainty across the market. The v2 risk engine records a
reward/risk-below-minimum warning but does not use that warning to set
`allowed=False`; it therefore cannot serve as the absolute quality gate.

There is always a relative first row in the existing ranking. The legacy
no-trade filter can reject all rows, but rank and absolute quality are not
modeled as separate domain stages with TAKE/WATCH/PASS/INSUFFICIENT_DATA.

## Outcome and missed-opportunity state

The candidate contains substantial outcome infrastructure:

- historical signals and point-in-time outcome rows;
- source-bar outcome capture;
- retained intraday artifact and coverage manifests;
- causal path replay with trigger-bar exclusion, same-minute ambiguity, halt
  states, MFE, and MAE;
- AlphaOps V5/V6 reconciliation and label lineage;
- daily review tables with missed-winner fields and failure-stage text.

The active mounted schema-26 database contained, during this audit:

- 279 historical signals across 33 dates;
- 243 `NO CLEAN EDGE`, 33 `WATCH`, and 3 `ENTRY WATCH` signals;
- 14 signal outcomes: 9 `complete_sourced`, 5 `not_triggered`;
- 116 V6 decisions: 9 tracked and the rest no-trade/policy/veto rows;
- 181 V6 labels, only 27 learning-eligible;
- one model run, correctly `NOT_TRAINED_INSUFFICIENT_LABELS`;
- 8 legacy closed paper positions, all with null MFE/MAE columns;
- 30 historical daily-review runs with 139 recorded missed winners.

The daily-review persistence schema contains useful missed-opportunity fields,
but no active source call to `persist_daily_review` exists in the current code.
It is therefore historical/dormant, not a functioning first-class
missed-opportunity engine. The required miss taxonomy and discovery metrics are
not implemented end to end.

## Backtesting and validation

Reusable research controls include:

- deterministic daily and causal intraday backtest engines;
- next-bar execution rules and conservative same-bar policies;
- session-return-aware intraday metrics;
- fixed/provisional execution-cost models and stress hooks;
- V6 purged expanding walk-forward splits;
- locked holdout receipts, calibration/interval checks, bootstrap expectancy,
  negative controls, multiple-testing adjustment, and catalyst ablation plans;
- deterministic promotion vetoes and no automatic promotion.

These components are not yet assembled around the required market-first DS
strategy ensemble. Available retained live evidence is insufficient for any
profitability or production-eligibility claim. Quotes, impact, partial fills,
depth, and consolidated coverage remain unproved. No legitimate backtest result
for the target architecture exists at Stage A.

## ML and LLM boundaries

V6 uses deterministic/scikit-learn research models with explicit minimum label
counts, chronological validation, frozen artifacts, and manual promotion.
Current state correctly abstains for insufficient eligible labels.

LLMs are used for cited research, scenario/catalyst claim extraction, and
explanation. No audited path makes an LLM the numeric trading authority. This
boundary must remain.

## Observability and traceability

The repository has strong component-level hashes, manifests, run contracts,
decision ledgers, strategy fingerprints, source lineage, and read-only observer
hardening. However, the complete answer to “why was this symbol in/out, what
anomaly/regime/strategies/scores/rank/gate produced the decision?” is fragmented
across tables and payloads. A single machine-readable opportunity trace does
not exist.

## Safety boundary

The platform is research/watchlist and simulated-paper only. `live_execute` is
locked, risk policy vetoes live execution, public routes do not expose order
submission, and UI copy requires manual broker action. This is a mandatory
invariant, not a feature to relax.

## Current verdict

The repository has a strong evidence and governance foundation, but the primary
mission is not complete. The decisive missing layer is an integrated,
market-first opportunity domain and pipeline that reuses the evidence spine,
then persistence, research validation, missed-opportunity metrics, and product
projection for that pipeline.

Stage A status: complete for the first implementation package. Audit remains
continuous; adopted and new code will be re-inspected after every Luna package.

