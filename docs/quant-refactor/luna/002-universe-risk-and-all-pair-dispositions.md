# LUNA WORK PACKAGE 002 — point-in-time universe, risk truth, and all-pair dispositions

Luna owns implementation only. Sol owns acceptance, requirement status, and
finding closure. Start only after Sol records the WP001 audit result.

## 1. Objective

Extend the additive research-only opportunity core so a run begins from a
point-in-time universe receipt, carries provider/execution capability truth,
records controlled lifecycle-transition evidence, and emits exactly one
absolute disposition for every strategy evaluation.

Target stage order:

`UniverseSnapshot -> cheap FeatureSnapshot -> OpportunityCandidate -> rich
FeatureSnapshot -> regimes -> every symbol-strategy evaluation -> global pair
ranking -> every-pair disposition -> DecisionTrace`.

## 2. Why this package is next

WP001 proves deterministic discovery/ranking/gating primitives but intentionally
leaves these gaps open:

- callers provide an unversioned symbol tuple rather than point-in-time
  membership/admission/exclusion evidence;
- provider, halt, corporate-action, spread, fee, slippage, and sizing truth are
  not canonical gate inputs;
- lifecycle transition validation has no immutable transition receipt;
- rejected, insufficient, and disabled evaluations have no `TradeDecision`;
- the run trace starts at dataset/features instead of universe truth.

Do not add historical outcome labels, persistence, migrations, CLI, UI,
schedulers, networking, deployment, or mounted AlphaOps behavior in WP002.

## 3. Source Luna must inspect

- all WP001 files under `intraday_scanner/v2/opportunity/`;
- all three `tests/test_opportunity_*.py` files;
- `docs/quant-refactor/00-current-state-audit.md` through
  `04-execution-log.md`;
- `intraday_scanner/v2/contracts/data.py`;
- `intraday_scanner/v2/data/market.py`;
- `intraday_scanner/v2/data_truth/*` for receipt conventions only;
- `intraday_scanner/v2/risk/engine.py` and its tests;
- `intraday_scanner/alpha/v5_policy.py` risk geometry only; do not copy V5;
- existing provider capability and corporate-action/halt contracts/tests.

## 4. Required contracts

Add immutable, deterministically serializable contracts with stable identities:

1. `UniversePolicy`
   - versioned allowed security types;
   - common stock default;
   - explicit ETF/ADR opt-in fields;
   - OTC, warrants, rights, units, and preferred shares excluded unless a
     versioned policy explicitly includes them;
   - price/liquidity/admission rules labeled heuristic where not empirically
     validated.
2. `UniverseMember`
   - symbol, membership status, security type, venue, first-seen/as-of times;
   - admission/exclusion reason codes;
   - eligibility, data availability, halt status, corporate-action status;
   - no missing status represented as false or zero.
3. `UniverseSnapshot`
   - as-of/decision time, policy ID/hash, provider receipt IDs, coverage counts,
     included/excluded members, source/dataset identity, limitations;
   - exact count reconciliation and unique symbols.
4. `ProviderCapabilityReceipt`
   - provider/feed/entitlement identity;
   - bars, trades, quotes, consolidated/NBBO, aggressor classification,
     corporate actions, halts, and historical coverage states;
   - `AVAILABLE`, `UNAVAILABLE`, `UNSUPPORTED`, or `UNKNOWN`, never inferred;
   - sanitized limitations with no secret or host-private value.
5. `ExecutionRiskEvidence`
   - evaluation identity and decision time;
   - observed/provisional/unavailable spread, slippage, fees, total round-trip
     cost, staleness, halt/action status, entry/stop/target geometry, quantity,
     planned loss, account/risk-cap identities, and aggregate concentration;
   - source/method/evidence kind for every numeric value;
   - missing input remains null/unavailable.
6. `StrategyLifecycleTransition`
   - strategy ID/version, from/to states, requested/effective times, actor type,
     validation/run evidence IDs, reason, policy version, content identity;
   - transition function returns this receipt and cannot auto-promote;
   - invalid transitions fail without a receipt.
7. Amend `TradeDecision`
   - require `evaluation_id`;
   - allow `ranked_id=None` only for an explicitly non-rankable disposition;
   - retain lifecycle, all checks/vetoes/limitations, and `research_only=True`;
   - exactly one decision identity per evaluation per run.

Use a schema-version bump for changed contracts. WP001 is unmounted, so make a
coherent contract change now rather than compatibility aliases or duplicate
types.

## 5. Universe behavior

Implement pure builders/adapters only:

- construct a `UniverseSnapshot` from dataset symbols plus caller-supplied
  point-in-time security metadata/capability receipts;
- never look up current metadata during historical/as-of construction;
- every requested symbol is included or explicitly excluded;
- absent metadata is `UNKNOWN` and excluded or insufficient according to
  policy, not assumed common stock;
- an explicit empty universe stays empty;
- duplicated/conflicting membership facts fail closed;
- feature computation receives only eligible included symbols;
- benchmark membership is retained separately and cannot silently become a
  trade candidate;
- trace stage 1 records the universe input/output identities and counts.

Do not build a network provider or claim full-market coverage. Fixture/adaptor
coverage must be labeled bounded.

## 6. Risk behavior

Create a pure opportunity risk adapter/gate input. Reuse trustworthy geometry
helpers by delegation where possible; do not copy V5 rules.

- validate LONG `stop < entry < target` and SHORT
  `target < entry < stop`;
- validate positive prices, quantity, stop distance, planned loss, and declared
  caps;
- separate gross reward/risk from after-cost reward/risk;
- observed quote spread is distinct from provisional spread assumptions;
- unsupported consolidated/NBBO truth remains unavailable;
- unknown halt/corporate-action status blocks TAKE;
- stale observation, breached planned-loss cap, invalid quantity, unavailable
  sizing/account truth, or after-cost reward/risk below threshold blocks TAKE;
- warning-only `reward_risk_below_minimum` behavior in the legacy v2 risk
  engine must not be copied into the new gate;
- WP002 still has no live execution or broker order path.

An experimental pair may remain WATCH only when its research watch checks pass;
its decision must visibly retain all missing execution-proof limitations. It
must never TAKE.

## 7. Every-pair disposition

For each `StrategyEvaluation` in a pipeline run, emit exactly one decision:

- `ELIGIBLE` and globally ranked: apply the absolute gate with the matching
  `ExecutionRiskEvidence`;
- `REJECTED`: `PASS` with the evaluator rejection and non-rankable reason;
- `INSUFFICIENT_DATA`: `INSUFFICIENT_DATA` with exact missing evidence;
- `DISABLED`: `PASS` with disabled lifecycle/capability veto;
- no evaluation may be silently absent from decisions;
- decisions and evaluations reconcile one-to-one by ID;
- only eligible pairs appear in relative ranking;
- rank 1 may still PASS or be insufficient;
- quality gate must not depend on list position.

Update traces so every pair has a reconstructible final disposition. If a
candidate has multiple pairs, use one trace per pair or introduce an explicit
pair-trace identity; do not overload one candidate-level final decision.

## 8. Pipeline inputs and identities

Replace the free symbol tuple as the authoritative membership input with a
`UniverseSnapshot` (a bounded convenience builder may remain for fixtures).

The run identity must bind:

- universe snapshot ID;
- dataset/content identity;
- benchmark snapshot ID where present;
- registry definition hashes;
- feature/discovery/ranking/gate/risk policy versions;
- capability receipt IDs;
- evaluation/rank/decision/trace identities.

Reject strategy-expectancy or strategy-risk mappings with unknown keys,
duplicate version ambiguity, wrong evaluation identity, future timestamps, or
wrong symbol/strategy metadata. Do not silently drop malformed values.

## 9. Tests required

Add focused tests with hand-computed assertions:

1. Universe policy/member/snapshot validation, exact count reconciliation,
   duplicate/conflict rejection, explicit-empty behavior, and deterministic
   hash/round trip.
2. Common stock admission plus ETF/ADR opt-in; OTC/warrant/right/preferred/unit
   exclusion; unknown metadata remains unknown and does not default eligible.
3. Point-in-time causality: future membership, capability, halt, action, quote,
   or risk evidence is rejected.
4. Benchmark retained but not evaluated as a candidate.
5. Capability truth: IEX-like receipt cannot claim SIP/NBBO; OHLCV cannot claim
   aggressor/CVD; unavailable fields remain null.
6. Execution-risk hand calculations for gross R, round-trip cost, after-cost R,
   fixed-fractional planned loss, and boundary comparisons.
7. Invalid directional geometry, zero/negative/non-finite values, stale quote,
   unknown halt/action, over-cap loss, and below-minimum after-cost R fail
   closed.
8. Lifecycle transition matrix and immutable receipt identity; no skipped state
   or automatic production promotion.
9. One-to-one evaluation/decision reconciliation covering eligible, rejected,
   insufficient, disabled, rank-1 PASS, experimental WATCH/no-TAKE, and a fully
   evidenced synthetic `PRODUCTION_ELIGIBLE` TAKE fixture.
10. Mismatched universe/evaluation/rank/risk/expectancy identities reject.
11. End-to-end two-symbol/two-strategy fixture proving global ranking trace
    inputs and pair-specific final decisions.
12. Import/no-live/no-network/no-persistence boundaries and existing WP001,
    v2 risk, V5, DataTruth, and AlphaOps regressions.

## 10. Prohibited shortcuts

- no fake universe coverage, quote, spread, cost, account, or performance data;
- no treating UNKNOWN as false/clear/zero;
- no current-data lookup for a historical decision;
- no ranking rejected/disabled pairs just to manufacture a decision;
- no forced TAKE fixture without complete synthetic evidence explicitly labeled
  fixture-only;
- no CVD from OHLCV;
- no persistence/migration/CLI/UI/scheduler/deployment/broker change;
- no modification of active DB or external state;
- no weakening WP001 or existing safety tests.

## 11. Requirements addressed

WP002 may address REQ-ARCH-003, REQ-SAFE-001/002, REQ-DATA-001/002/004/005/006,
REQ-FEAT-007, REQ-STRAT-003, REQ-LIFE-001, REQ-EVAL-001, REQ-RANK-001/002/003,
REQ-GATE-001/002, REQ-RISK-001/002, REQ-TRACE-001, REQ-OBS-001, and part of
REQ-TEST-001. Luna must not mark them PASS.

## 12. Commands

```powershell
py -m pytest tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
py -m pytest tests/test_alpha_risk_geometry.py tests/test_alpha_tail_risk_controls.py tests/test_v2_strategy_catalog_expansion.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
py -m pytest -q -p no:cacheprovider
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
git diff --check
git status --short
```

If test files differ, report exact replacements and why.

## 13. Required Luna report

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
PROOF_POINT_IN_TIME_UNIVERSE
PROOF_CAPABILITY_TRUTH
PROOF_EVERY_EVALUATION_HAS_ONE_DECISION
PROOF_EXPERIMENTAL_NEVER_TAKES
PROOF_NO_LIVE_OR_PERSISTENCE_PATH
```

Do not claim PASS, CLOSED, certified, production-ready, or mission complete.
