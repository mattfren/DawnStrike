# Luna work package 005 — chronological validation and execution stress

## 1. Authority and objective

This work package begins only after Sol accepts WP004 increment C. Luna owns
implementation; Sol owns architecture, adversarial review, requirement status,
and authorization of each increment.

The objective is to assemble the accepted market-first opportunity, outcome,
miss, and metric artifacts into an honest chronological research harness. The
harness must prevent future-data access, target/timestamp leakage, invalid
time-series splits, post-hoc locked-OOS claims, survivorship overstatement, and
unrealistic execution claims. It must reuse the exact accepted opportunity
pipeline and stored artifacts rather than implement another strategy engine.

No legitimate profitability, OOS, promotion, or production-readiness claim is
authorized by this prompt. Synthetic fixtures prove software invariants only.
Inadequate retained external evidence must remain `EXTERNAL_DATA_BLOCKED` or
the exact equivalent, never zero-filled or described as validated.

## 2. Controlling sources to read before design

Read completely before any edit:

- the controlling pasted brief for this refactor;
- `docs/quant-refactor/00-current-state-audit.md`;
- `docs/quant-refactor/01-target-architecture.md`;
- `docs/quant-refactor/02-requirements-ledger.md`;
- `docs/quant-refactor/03-audit-ledger.md`;
- `docs/quant-refactor/15-wp004-increment-c-sol-audit.md`;
- accepted opportunity pipeline, risk, outcome, outcome-persistence, miss,
  metric, and metric-persistence contracts and tests;
- `intraday_scanner/alpha/v6/validation.py`;
- `intraday_scanner/services/alpha_v6_holdout_service.py`;
- `intraday_scanner/alpha/v6/registry.py` and dataset builder;
- `intraday_scanner/v2/backtest/engine.py`;
- `intraday_scanner/v2/backtest/intraday_engine.py`;
- `intraday_scanner/v2/backtest/intraday_metrics.py`;
- `intraday_scanner/v2/paper_ops/experiment_registry.py` and
  `lifecycle_backtest.py`;
- their split, holdout, promotion, timeout, path, and metric tests.

Inspect the actual final worktree and preserve every unrelated dirty file.
Do not use an active/default database or external network/provider call.

## 3. Nonnegotiable boundaries

- Research-only and paper-only. No broker or live-order path.
- No mounted runtime, UI, scheduler, operator database, deployment, commit,
  push, or package-root eager export in increments A through C.
- No separate implementation of discovery, features, regimes, strategy
  evaluation, ranking, gate, fill/path ordering, or outcome labeling.
- Stored `PipelineResult` and accepted replay bodies are the authoritative
  decision-time output. If a later causal rerun is added, it must call the same
  accepted `prepare_opportunity_pipeline` and `run_opportunity_pipeline`
  functions, not copy their rules.
- Future outcome, miss, and metric modules remain downstream-only and cannot be
  imported by real-time feature, discovery, regime, strategy, ranking, risk,
  gate, or pipeline modules.
- Do not delegate quantitative authority to the legacy float/dict V6 validator
  or either legacy/v2 backtest engine. Reuse only concepts or immutable source
  contracts that survive exact compatibility review. New calculations use
  typed immutable contracts and exact `Decimal` where numeric truth matters.
- No caller-authored split status, leakage result, OOS status, metric result,
  stress result, fragility verdict, or promotion decision.
- Missing, pending, censored, unsupported, provisional, nonconsolidated, or
  bounded truth stays explicit and null where unavailable; it never becomes
  zero, clear, empirical, complete, or negative by default.
- Keep implementation modules coherent and below 40 KB. The existing 916-line
  metric store is frozen and must not grow in WP005.
- Pause after every increment for Sol audit. Do not begin the next increment
  automatically.

## 4. Increment A — frozen corpus and chronological split controls

Implement only increment A after the required no-edit design checkpoint. Do
not implement trading metrics, cost stress, bootstrap, perturbation,
promotion, persistence, or runtime behavior yet.

### 4.1 Required result

Add a downstream-only immutable validation corpus and pure split engine that:

1. consumes exact accepted `CurrentOutcomeReplay` bodies and their embedded
   persisted `PipelineResult`, outcome receipt/batch, evaluation, decision,
   risk, trace, universe, configs, and future observation lineage;
2. freezes a canonical unique ordered set of session sources without flattening
   missing or censored records away;
3. selects an exact declared outcome horizon by ID/hash and rejects unknown,
   ambiguous, absent, or mixed substitutions;
4. derives the evaluation-level research population from embedded bodies while
   retaining PASS, INSUFFICIENT, WATCH, TAKE, nonrankable, no-entry, pending,
   censored, unavailable, and complete states;
5. partitions by whole exchange session, never by individual evaluation or
   symbol, so same-session rows cannot cross research/validation/OOS boundaries;
6. creates deterministic expanding walk-forward folds only from the declared
   research and validation regions;
7. purges any earlier sample whose selected label horizon or required source
   availability overlaps the next evaluation region, with boundary equality
   treated as overlap;
8. applies a declared nonnegative embargo over the exact ordered session
   inventory without inferring an exchange calendar;
9. keeps the locked OOS region outside every fold, fit, threshold selection,
   perturbation, baseline selection, and metric used for tuning;
10. distinguishes genuinely predeclared locked OOS from a retrospective
    holdout. A plan created after the first OOS decision/observation cannot be
    called locked OOS;
11. records point-in-time universe and survivorship status/limitations for each
    session. Current-membership proxy or unknown membership blocks strong
    survivorship-safe claims;
12. emits content-bound split/fold/leakage receipts whose direct and strict JSON
    construction recomputes every membership, boundary, purge, embargo,
    chronology, lineage, limitation, and identity.

### 4.2 Exact contract expectations

The design may improve names, but it must provide explicit immutable concepts
equivalent to:

- validation source/session binding;
- validation corpus and corpus policy;
- selected outcome-horizon binding;
- point-in-time universe/survivorship evidence status;
- split role (`TRAIN_RESEARCH`, `VALIDATION`, `LOCKED_OOS`, plus explicit
  `PURGED` and `EMBARGOED` exclusions);
- holdout integrity status that separates predeclared locked OOS,
  retrospective-only holdout, consumed/previously evaluated evidence if known,
  and unavailable truth;
- split plan and exact session allocation;
- expanding walk-forward fold and fold collection;
- timestamp/leakage audit receipt;
- one final increment-A preparation receipt that embeds and reconciles the
  corpus, policy, split, folds, and leakage audit.

Public DTOs must embed enough exact source bodies to verify themselves. A weak
public child that only references caller-provided IDs/hashes is prohibited; a
private recomputed child is acceptable when the public parent embeds and
recomputes its full source.

All timestamps must be aware and exact-offset where source contracts require
it. UTC is required for corpus freeze, plan declaration, and validation audit
times. `frozen_at` and `recorded_at` must cover every persistence, outcome,
source-observation, safety, action, and capability time actually used. A
post-decision observation may label a row but may never appear in its embedded
decision-time pipeline feature/risk/gate inputs.

Locked-OOS one-time use cannot be established by an in-memory caller assertion.
Increment A must either classify it as not yet durably enforced or design a
content-bound access receipt whose uniqueness is explicitly deferred to a
later audited append-only adapter. Do not claim REQ-BT-002 closure in A.

### 4.3 Causality and split rules

- Sort sessions by exact session-open/session-close identities, not filenames
  or locale-formatted dates.
- Reject duplicate session identity with different bodies, duplicate run or
  evaluation identities with different content, and a run assigned to a
  different session than its outcome source.
- Train/research must precede validation; validation must precede locked OOS.
  Regions are nonempty when the policy declares them required.
- Selected label end/availability for a fitting row must be strictly before the
  next evaluation region's first decision after purge. Equality is leakage and
  must purge or reject the row according to the declared policy.
- Embargoed sessions are explicit exclusions. They cannot count toward minimum
  training, validation, or OOS sample sizes.
- Folds are expanding and deterministic. Training sets may grow; validation
  windows must be disjoint in their evaluation role unless the policy
  explicitly defines rolling diagnostics that are never aggregated as
  independent OOS observations.
- The locked OOS set is identical across every representation and absent from
  fold training/validation. Any overlap, reorder, omission, injection, or
  reclassification fails closed.
- A synthetic plan declared after its OOS begins must emit
  retrospective-only/invalid-lock evidence, not a locked status.
- Mutation of a future observation may change corpus/split/audit identities but
  cannot change the embedded original universe, preparation, evaluation, rank,
  decision, trace, or run identities.

### 4.4 Increment-A test matrix

At minimum add deterministic tests for:

- three or more chronological regions with exact session allocations;
- valid expanding folds and whole-session grouping;
- train/validation and validation/OOS boundary equality;
- horizon overlap purge, exact equality purge, and nonoverlapping retention;
- zero, one, and multiple embargo sessions;
- same-session cross-partition attack;
- locked OOS injected into training or fold validation;
- a genuinely predeclared synthetic lock versus a retrospective holdout;
- duplicate/cross-content session, run, evaluation, horizon, and universe
  identities;
- incomplete/pending/censored/unavailable outcomes retained without fake label;
- point-in-time, current-membership-proxy, and unknown survivorship states;
- missing/late source availability and corpus freeze chronology;
- direct and consistently rehashed omission, injection, reorder, boundary,
  role, fold, audit, limitation, and identity tamper;
- unknown JSON fields, duplicate keys, Decimal floats, malformed/private/secret
  IDs, naive datetimes, and schema-version drift;
- future-observation mutation isolation;
- fresh-process and AST import firewall proving core opportunity, pipeline,
  persistence, runtime, UI, network, broker, and scheduler paths neither import
  nor expose validation modules.

Use bounded synthetic accepted bodies. Do not present their returns as evidence
of edge.

### 4.5 Increment-A hold point

Before editing, send Sol one no-edit design checkpoint containing:

- exact contracts, fields, enums, stable identities, and module graph;
- exact corpus row/session reconciliation and horizon-selection rules;
- exact split, purge, embargo, fold, lock, and survivorship equations;
- direct/from-JSON invariant matrix;
- shared-versus-new helper decisions after inspecting existing V6/backtest
  code;
- exact files and focused tests;
- blockers and any architectural decision required.

Wait for Sol approval. After implementation, run the focused validation suite,
accepted opportunity/outcome/miss/metric regressions, whole Ruff, full mypy,
compileall, and diff-check, then pause for Sol audit.

## 5. Increment B — exact trading metrics and BASE/2X/3X stress

Do not begin until Sol accepts increment A and issues the metric/stress design
hold point.

Increment B will compute the required trading metrics from the frozen accepted
outcome population, with canonical Decimal formulas, explicit sample/status
truth, declared aggregation units, exact benchmark overlap, full segmentation,
and no annualization when unsupported. It will include BASE/2X/3X spread,
entry/exit slippage, and fee scenarios over identical trade/path bodies.

Higher costs must never improve the same trade's after-cost result. Cost stress
may change profitability/eligibility conclusions but cannot change entry,
target/stop order, horizon, MFE/MAE, gross return, or source lineage. Missing or
provisional cost/NBBO/quantity truth remains null/blocked, not zero. Censored,
ambiguous, unavailable, and unattainable records never become simulated fills.

Required metrics include at least total trades, wins, losses, win rate, average
winner/loser R, expectancy R, profit factor, Sharpe and Sortino where
appropriate, max drawdown and duration, MFE, MAE, holding time, and explicit
long/short, strategy, regime, weekday, time-of-day, month, year, liquidity,
volatility, market-state, and catalyst segmentation with unavailable buckets.

## 6. Increment C — overfitting and stability controls

Do not begin until Sol accepts increment B.

Sol accepted increment B on 2026-08-15. Luna implemented the additive,
downstream-only increment-C candidate without changing the accepted A/B
contracts. It adds a pre-confirmatory calibration policy, exact
strategy/version/direction populations, deterministic whole-session bootstrap
intervals, exact-population causal perturbation and negative-control evidence,
predeclared regime and simple-baseline checks, trial and complexity limits,
future-data sentinels, and explicit control vetoes. Locked OOS is rejected as a
calibration region. The only complete non-veto result is the explicitly
non-promotional `NO_CONTROL_VETO`; the layer cannot mutate lifecycle state or
authorize TAKE/live behavior.

The implementation and durable verification handoff is
`luna/005-c-overfitting-stability-controls-handoff.md`. This is Luna evidence
for Sol adjudication only. No requirement or finding is marked closed here.

## 7. Later increments deliberately excluded

Validation persistence and one-time locked-OOS consumption, disabled-by-default
read-only runtime projection, operator UI, active-database migration, external
data acquisition, and final certification require separate Sol design and
audit. Do not start them from this prompt.

## 8. Verification and handoff

Every handoff must list exact files, contracts, formulas, decisions, tests,
commands, counts, elapsed times, limitations, blockers, and requirement IDs
addressed as evidence only. Luna must not mark a ledger row PASS/CLOSED or call
the system validated, profitable, production-ready, or certified. Sol may
request independent attacks, a module split, or a narrower rerun before
acceptance.
