# Dawnstrike quant-refactor target architecture

## Architectural decision

Build an additive deterministic opportunity core under
`intraday_scanner/v2/opportunity/`. Reuse existing providers, DataTruth,
intraday evidence, indicators, strategy identity, V5/V6 governance, PaperOps,
and public truth contracts. Do not fork strategy rules into unrelated live and
backtest implementations.

The first release is research-only and feature-flagged. Existing AlphaOps V5
production-facing paper decisions remain authoritative until a strategy is
independently promoted. Experimental DS strategies may produce research WATCH,
PASS, or INSUFFICIENT_DATA records; they may not produce production TAKE.

## Pipeline

```text
Tradeable market snapshot
  -> universe eligibility and exclusion trace
  -> immutable normalized FeatureSnapshot
  -> strategy-independent OpportunityCandidate discovery
  -> MarketRegime + SecurityRegime classification
  -> versioned StrategyDefinition registry
  -> symbol + strategy StrategyEvaluation
  -> evidence-backed expectancy and uncertainty
  -> cross-market RankedOpportunity list
  -> execution, liquidity, correlation, and risk evaluation
  -> separate absolute quality gate
  -> TAKE | WATCH | PASS | INSUFFICIENT_DATA
  -> OutcomeRecord for every evaluated pair
  -> MissedOpportunityRecord and discovery metrics
  -> chronological research and validation loop
```

## Domain contracts

All contracts are immutable dataclasses or enums, timezone-aware where a time is
present, deterministically serializable, and content-hashable.

- `FeatureSnapshot`: symbol, decision time, market date, universe identity,
  source/evidence identities, raw observed inputs, normalized features,
  unavailable features, data quality, and causal trace.
- `OpportunityCandidate`: symbol, decision time, anomaly types/strengths,
  discovery reasons, feature snapshot identity, and discovery rank before any
  strategy requirement.
- `MarketRegime` and `SecurityRegime`: typed state, measurements, confidence,
  availability, and evidence identity.
- `StrategyDefinition`: DS ID/version, lifecycle state, supported direction,
  required features, compatible regimes/timeframe, heuristic/empirical label,
  evaluator identity, and failure modes.
- `StrategyEvaluation`: exact symbol-plus-strategy result, eligibility,
  rejection reasons, setup geometry, evidence, expectancy, and uncertainty.
- `RankedOpportunity`: evaluation identity, relative rank, documented score
  components, concentration cluster, and no quality decision.
- `TradeDecision`: ranked identity, TAKE/WATCH/PASS/INSUFFICIENT_DATA, gate
  results, risks, entry/invalidation/target logic, and research-only flag.
- `OutcomeRecord`: decision identity plus strictly future label data stored away
  from the feature path.
- `MissedOpportunityRecord`: hindsight-qualified opportunity, discovery status,
  miss category, earliest surfacing time, and causal evidence.
- `BacktestRun` and `ValidationRun`: immutable configuration, data snapshot,
  code/strategy hashes, chronological partitions, assumptions, metrics, and
  result status.
- `DecisionTrace`: structured stage-by-stage inputs, outputs, exclusions,
  component scores, evidence hashes, and limitations.

Existing DTOs may be adapted when their semantics match. Names alone do not
justify duplicates.

## Universe engine

The universe engine accepts a point-in-time symbol membership snapshot and
records both admissions and exclusions. It must distinguish:

- tradeable common equities;
- unsupported asset/security types;
- missing or stale membership truth;
- price/liquidity/data-quality exclusions;
- provider coverage gaps.

The initial implementation can consume the existing Alpaca screening universe,
Nasdaq directory, and retained source rows. It must not call a mover list a full
market universe. Coverage status is explicit.

## Feature engine

Feature computation is causal, deterministic, and staged.

Stage 1 features are cheap: multi-horizon returns, gap, range position, dollar
volume, relative volume, volume acceleration, range/volatility expansion, and
data quality.

Stage 2 adds candidate-only features: session VWAP or explicitly labeled OHLCV
VWAP proxy, VWAP displacement/slope/reclaim, ATR/realized volatility,
market-relative strength, session segment, and catalyst facts.

Every feature has a value or an explicit unavailable reason. Cross-sectional
z-scores and percentiles require a timestamp-aligned eligible cross-section.
Rolling statistics use only observations available at or before decision time.

True CVD, aggressor imbalance, depth, sector-relative measures, beta adjustment,
and consolidated spread are provider-capability-gated. No OHLCV-derived field
may be labeled true CVD.

## Opportunity discovery

Discovery operates only on universe and feature snapshots. It does not call a
strategy evaluator. Initial anomaly types include relative-volume/volume
acceleration, price acceleration, unusual gap, range/volatility expansion or
compression, VWAP displacement/reclaim/loss, market-relative strength/weakness,
failed extension, breakout/breakdown, exhaustion, catalyst abnormal response,
and liquidity anomaly where supported.

Each anomaly records the normalization method, cross-section/window size,
threshold source, availability, and strength. Before empirical calibration,
thresholds are explicitly `HEURISTIC`.

## Regime engine

Produce both market and security regimes with states:

`TREND_UP`, `TREND_DOWN`, `MEAN_REVERTING`, `VOLATILITY_EXPANSION`,
`VOLATILITY_COMPRESSION`, `CHOP`, `BREAKOUT`, `BREAKDOWN`, `EXHAUSTION`,
`UNKNOWN`, and `INSUFFICIENT_DATA`.

Initial measurements may use directional efficiency, rolling returns, VWAP
slope/proxy slope, realized volatility ratios, range persistence, and
autocorrelation. Hurst, ADX, variance ratio, and breadth are extensions that
must be empirically justified and capability-gated.

## Strategy registry

The registry owns unique `(strategy_id, version)` identities and lifecycle:

`EXPERIMENTAL`, `RESEARCH_PASS`, `VALIDATION_PASS`, `OOS_PASS`,
`PAPER_TRADING`, `PRODUCTION_ELIGIBLE`, `DEGRADED`, `DISABLED`, `REJECTED`.

Initial experimental families:

- DS-MOM-001 high relative-volume momentum continuation;
- DS-MOM-002 VWAP pullback continuation;
- DS-MOM-003 opening-range expansion;
- DS-MR-001 extreme VWAP displacement mean reversion;
- DS-REV-001 failed extension/exhaustion reversal;
- DS-REV-002 failed breakout/breakdown;
- DS-RS-001 market-relative strength continuation;
- DS-OF-001 and DS-OF-002 registered disabled when aggressor-side evidence is
  unavailable.

Existing AlphaOps V5 becomes an adapter/definition with its existing frozen
identity; it is not silently renamed or rewritten.

## Expectancy and evaluation

Evaluate symbol plus strategy. A strategy can reject a candidate with explicit
reasons. Eligible evaluations contain entry, invalidation, target logic, regime
fit, data quality, and execution feasibility.

Expectancy evidence records sample size, win probability, average winner/loser
R, expectancy R, profit factor, MFE/MAE, holding time, relevant regime sample,
confidence interval, stability, and evidence cohort. Missing evidence remains
null and yields INSUFFICIENT_DATA; it is never converted to zero or a validated
statistic.

Heuristic score components are labeled and cannot be displayed as empirical.

## Ranking and quality gate

Ranking and gating are separate pure functions.

Ranking orders all eligible symbol-plus-strategy evaluations using documented
components: validated expectancy, uncertainty, anomaly strength, regime fit,
catalyst evidence, relative strength, liquidity/execution, data quality, and
concentration penalties. A breakdown and normalization receipt is mandatory.

The absolute gate evaluates lifecycle, evidence sufficiency, after-cost reward
and risk, spread/liquidity, stale/missing data, safety vetoes, and concentration.
A rank-1 item can PASS. TAKE requires `PRODUCTION_ELIGIBLE` plus all empirical
and risk gates. Experimental strategies cannot TAKE.

## Correlation and concentration

Initial concentration uses direction, strategy family, and available sector
labels. Correlation coefficients are used only when a causal, sufficiently long
return history exists. Missing sector/correlation truth is explicit and may
reduce quality or force WATCH.

## Persistence

Add schema migrations only after domain/core audit. Persistence is append-only
for snapshots, candidates, evaluations, ranked opportunities, decisions,
outcomes, missed opportunities, traces, backtest runs, and validation runs.

Future labels live in outcome/research tables and cannot be imported by the
real-time feature engine. Deterministic IDs and foreign keys allow exact TAKE,
WATCH, PASS, and INSUFFICIENT_DATA reconstruction.

## Research validity

The research harness reuses the same feature, discovery, regime, strategy,
ranking, and gate functions used by live research. Only data adapters and clocks
differ.

Required controls: chronological train/research, validation, locked OOS,
walk-forward folds, purge/embargo where labels overlap, timestamp audits,
survivorship documentation, transaction-cost BASE/2X/3X stress, realistic
stop/target ordering, parameter perturbation/plateaus, bootstrap intervals,
multiple-hypothesis awareness, negative controls, simple-baseline comparison,
and cross-regime stability.

No OOS result is legitimate until data, strategy, parameters, and holdout are
frozen. Insufficient external data produces EXTERNAL_DATA_BLOCKED, not a fake
backtest.

## Outcome and missed-opportunity loop

Record every evaluated opportunity, including taken, watched, passed,
top-ranked, and threshold-near-miss pairs. Future MFE/MAE, target/stop ordering,
simulated R, horizon returns, and times-to-event are strictly post-decision.

After session close, compare hindsight-qualified opportunities with the stored
decision set and classify misses as `UNIVERSE_MISS`, `DATA_MISS`,
`FEATURE_MISS`, `ANOMALY_MISS`, `REGIME_MISCLASSIFICATION`, `STRATEGY_MISS`,
`SCORING_MISS`, `QUALITY_GATE_MISS`, `EXECUTION_FILTER`, or `UNKNOWN`.

Metrics include DAILY_OPPORTUNITY_RECALL, TOP_1/3/5_RECALL,
PRECISION_AT_1/3/5, FALSE_POSITIVE_RATE, and NO_TRADE_ACCURACY. Metric
denominators, hindsight qualification rules, and unavailable states are stored.

## Product projection

Add a bounded read model for “Today's Best Opportunities” only after domain,
persistence, and trace audits pass. The UI must distinguish validated vs
heuristic statistics, show data limitations, and display
`NO QUALIFYING TRADE CURRENTLY EXISTS` when appropriate.

The product remains research/paper only. No broker SDK, credential, order route,
or autonomous execution task is part of this architecture.

