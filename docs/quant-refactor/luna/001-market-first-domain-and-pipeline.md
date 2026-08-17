# LUNA WORK PACKAGE 001 — market-first domain and deterministic pipeline core

Luna owns implementation only. Luna must not mark requirements or findings
passed/closed and must not claim overall completion.

## 1. Objective

Create an additive, deterministic, research-only market-first opportunity core
under `intraday_scanner/v2/opportunity/`. It must prove the stage order:

`FeatureSnapshot -> OpportunityCandidate -> regimes -> strategy evaluation ->
pair ranking -> separate quality gate -> DecisionTrace`.

## 2. Why this exists

Current AlphaOps collects and setup-scores mover rows before normalized anomaly
discovery. The controlling mission requires strategy-independent discovery and
symbol-plus-strategy evaluation without changing validated production-facing
behavior prematurely.

## 3. Existing code Luna must inspect

- `docs/quant-refactor/00-current-state-audit.md`
- `docs/quant-refactor/01-target-architecture.md`
- `docs/quant-refactor/02-requirements-ledger.md`
- `docs/quant-refactor/03-audit-ledger.md`
- `intraday_scanner/v2/contracts/*`
- `intraday_scanner/v2/data/market.py`
- `intraday_scanner/v2/indicators/core.py`
- `intraday_scanner/v2/strategies/models.py`
- `intraday_scanner/v2/strategies/combined_catalog.py`
- `intraday_scanner/v2/strategy_identity.py`
- `intraday_scanner/v2/risk/engine.py`
- `intraday_scanner/alpha/v5_policy.py`
- `intraday_scanner/alpha/regime_detector.py`
- `intraday_scanner/alpha/feature_factory.py`
- `intraday_scanner/v2/scanner/latest.py`

## 4. Components to reuse

- stdlib frozen dataclasses/enums and deterministic serialization style;
- `MarketDataset`/`MarketBar` for fixture inputs;
- v2 indicators when their causal semantics fit;
- stable hashing/strategy identity helpers;
- existing V5 logic only by future adapter, not copied into this package.

Do not alter existing strategy implementations in this work package.

## 5. Required architectural behavior

1. Core modules have no SQLite, Streamlit, network, CLI, scheduler, notifier, or
   deployment imports.
2. Every timestamp is timezone-aware.
3. Every observed input is at or before decision time.
4. Discovery has no import or runtime dependency on strategy definitions.
5. Missing/unsupported values remain unavailable, never zero.
6. Ranker and quality gate are separate functions and types.
7. Experimental strategies can never yield TAKE.
8. The core is deterministic and content-hashable.
9. Existing application behavior remains unchanged; do not add a feature flag or
   mounted integration yet.

## 6. Exact interfaces and domain concepts

Create a coherent package. The exact file split may vary only if the same small
module boundaries are preserved:

- `models.py`: enums and immutable contracts for FeatureSnapshot,
  OpportunityCandidate, MarketRegime, SecurityRegime, StrategyDefinition,
  StrategyEvaluation, StrategyValidationState, ExpectancyEvidence,
  RankedOpportunity, TradeDecision, OutcomeRecord, MissedOpportunityRecord,
  BacktestRun, ValidationRun, DecisionTrace, and stage trace entries.
- `features.py`: causal raw and normalized feature snapshots from timestamp-
  aligned bars/cross-section; explicit availability and method metadata.
- `discovery.py`: strategy-independent anomaly detection.
- `regimes.py`: market and security classification.
- `registry.py`: unique strategy registry and initial DS definitions/evaluators.
- `expectancy.py`: evidence DTO builders and deterministic R metrics only; no
  invented empirical results.
- `ranking.py`: pair ranking with documented components and concentration
  labels/penalties.
- `quality_gate.py`: TAKE/WATCH/PASS/INSUFFICIENT_DATA pure gate.
- `pipeline.py`: staged orchestration and trace assembly.
- `__init__.py`: bounded public API.

Prefer value objects over dictionaries. Include deterministic `to_dict()` or
canonical serialization compatible with existing conventions.

## 7. Data contracts

At minimum:

- Data quality and feature availability are typed.
- Normalized feature values include method, sample size, and window/cross-
  section identity.
- Anomalies include type, strength, evidence kind (`HEURISTIC` or `EMPIRICAL`),
  threshold source, and availability.
- Strategy definitions include required features, compatible regimes,
  direction, lifecycle, evaluator identity, and failure modes.
- Evaluations retain eligible/rejected/insufficient reasons.
- Ranking retains every component and cannot emit a decision.
- Decisions retain every gate/veto and `research_only=True`.
- Future-label contracts exist but are not consumed anywhere in feature,
  discovery, regime, evaluation, ranking, or gate modules.

## 8. Strategy requirements

Register deterministic experimental definitions:

- DS-MOM-001 high relative-volume momentum continuation
- DS-MOM-002 VWAP pullback continuation
- DS-MOM-003 opening-range expansion
- DS-MR-001 extreme VWAP displacement mean reversion
- DS-REV-001 failed extension/exhaustion reversal
- DS-REV-002 failed breakout/breakdown
- DS-RS-001 market-relative strength continuation

Register DS-OF-001 and DS-OF-002 as DISABLED with explicit required
aggressor-side trade evidence. Do not approximate CVD from OHLCV.

All thresholds in this package are `HEURISTIC`, versioned, documented, and
excluded from validated-statistic claims. Strategy evaluators must return
insufficient when required features are unavailable.

## 9. Feature requirements

Implement only defensible features supported by OHLCV fixtures:

- multi-horizon return and price acceleration;
- gap and range position;
- ATR/normalized range and realized-volatility ratio;
- relative volume, volume acceleration, rolling z-score/percentile;
- price/volume divergence signal;
- session VWAP proxy from typical price times volume, explicitly named/labeled
  as a proxy rather than sourced trade VWAP;
- VWAP-proxy displacement/slope/reclaim/loss;
- market-relative strength when a timestamp-aligned benchmark is supplied;
- session segment and minutes since open.

All rolling/cross-sectional methods need minimum sample rules. Insufficient
sample produces unavailable state.

## 10. Regime requirements

Support the target regime enum. Use simple causal measurements such as
directional efficiency, return, volatility ratio, range persistence, and VWAP
proxy slope. Do not implement Hurst merely to satisfy a name. Store measurements
and confidence/evidence kind. Insufficient history must be a real state.

## 11. Ranking and gate requirements

Ranking inputs may use heuristic anomaly strength, regime fit, data quality,
liquidity, and available expectancy. Unknown empirical expectancy must not be
zero. Ranking must be stable under ties.

Quality gate rules:

- TAKE requires PRODUCTION_ELIGIBLE plus empirical expectancy with sufficient
  sample/uncertainty and all risk/data gates;
- every package-001 DS strategy is experimental, so no fixture may TAKE;
- a strong experimental candidate may WATCH;
- a candidate failing absolute criteria may PASS even if rank 1;
- missing mandatory evidence yields INSUFFICIENT_DATA where appropriate.

## 12. Persistence, API, and UI

None in this work package. Do not edit migrations, SQLite store, CLI, app.py,
web assets, scheduled scripts, Vercel files, active runtime, or active state.

## 13. Tests required

Add focused tests, split coherently:

1. Contract validation, timezone rejection, deterministic serialization/hash,
   enum/lifecycle uniqueness.
2. Hand-calculated feature values, sample insufficiency, cross-section
   normalization, future timestamp rejection, VWAP proxy label, benchmark
   alignment, and unsupported order flow.
3. Discovery-before-strategy proof: import boundary plus a candidate discovered
   when zero strategy matches.
4. Market and security regime fixtures covering trend, mean reversion,
   expansion/compression, chop, break/failure/exhaustion, and insufficient data.
5. Every DS family: eligible, rejected, insufficient; order-flow disabled.
6. Pair evaluation counts and explicit rejection retention.
7. Rank ordering/ties, component breakdown, rank-1 PASS, experimental no-TAKE,
   missing-evidence INSUFFICIENT_DATA.
8. Outcome/future-label isolation: changing an OutcomeRecord cannot change the
   original decision/trace hash.
9. End-to-end deterministic fixture: two identical runs produce identical
   serialized outputs and trace IDs.
10. Existing v2/AlphaOps regression tests most likely affected by shared imports.

Tests must assert behavior, not merely object construction.

## 14. Failure cases

Reject or return explicit insufficient/unavailable for:

- naive timestamps;
- observed time after decision time;
- duplicate strategy ID/version;
- missing benchmark alignment;
- zero/negative prices;
- non-monotonic or duplicate bars;
- insufficient normalization windows;
- missing required strategy features;
- unknown lifecycle/decision values;
- unsupported true CVD/order-flow;
- attempt to TAKE from any non-production lifecycle;
- malformed rank component or non-finite numeric input.

## 15. Backward compatibility

No existing import, CLI output, DB schema, AlphaOps behavior, scheduled behavior,
or UI changes. New package tests run alongside the existing suite.

## 16. Observability

DecisionTrace is structured data, not logging prose. It must record ordered
stage names, input/output identities, counts, reasons, score components,
limitations, and final decision. Do not include secrets or host-private paths.

## 17. Prohibited shortcuts

- No fake performance, win rate, expectancy, probability, sample, or confidence.
- No random time-series split.
- No future-bar access.
- No outcome import in real-time modules.
- No CVD from OHLCV.
- No LLM use.
- No giant god module.
- No copy of V5 rules.
- No production integration, migration, deployment, scheduler, broker, or active
  DB mutation.
- No weakening existing tests or no-live safety.

## 18. Acceptance criteria addressed

This package targets REQ-ARCH-001/002/003, REQ-DATA-004/005,
REQ-FEAT-001 through 007, REQ-DISC-001 through 003, REQ-REG-001 through 003,
REQ-STRAT-001 through 003, REQ-LIFE-001, REQ-EVAL-001, REQ-EV-001 through 003,
REQ-RANK-001 through 003, REQ-GATE-001/002, REQ-TRACE-001, and part of
REQ-TEST-001.

Luna does not mark them passed.

## 19. Commands Luna must run

```powershell
py -m pytest tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py -q -p no:cacheprovider
py -m pytest -q -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
git diff --check
git status --short
```

If test filenames differ, list exact replacements and why.

## 20. Evidence Luna must provide to Sol

Report exactly:

```text
FILES_CHANGED
TESTS_ADDED
TESTS_RUN
RESULTS
MIGRATIONS: none
ARCHITECTURAL_DECISIONS
KNOWN_LIMITATIONS
BLOCKERS
REQUIREMENT_IDS_ADDRESSED
PROOF_OF_NO_EXISTING_BEHAVIOR_CHANGE
PROOF_NO_TAKE_FROM_EXPERIMENTAL
PROOF_DISCOVERY_PRECEDES_STRATEGY
PROOF_NO_FUTURE_LABEL_IMPORT
```

Do not claim PASS, CLOSED, certified, or complete.

