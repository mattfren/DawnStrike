# LUNA WORK PACKAGE 004 — missed opportunities and discovery metrics

## 1. Mission

Implement the downstream research layer that answers:

> There appeared to be at least one excellent trade today. Why did Dawnstrike
> not surface it early enough?

The package must distinguish a correct no-trade session from a discovery
failure, attribute supported misses to the required taxonomy, and compute
honest discovery metrics with explicit numerators, denominators, definitions,
and unavailable states.

This is retrospective research only. It must not promote a strategy, mount a
runtime, change operator UI, access current or external data, migrate an active
database, or create a live/broker path.

## 2. Controlling requirements

Address additive evidence toward REQ-MISS-001/002, REQ-METRIC-001,
REQ-OUT-001/002/003, REQ-DATA-003/005/006, REQ-TRACE-001, REQ-PERSIST-001,
REQ-TEST-001/002, and REQ-DOC-001.

The only miss categories are:

- `UNIVERSE_MISS`
- `DATA_MISS`
- `FEATURE_MISS`
- `ANOMALY_MISS`
- `REGIME_MISCLASSIFICATION`
- `STRATEGY_MISS`
- `SCORING_MISS`
- `QUALITY_GATE_MISS`
- `EXECUTION_FILTER`
- `UNKNOWN`

Required metrics are:

- `DAILY_OPPORTUNITY_RECALL`
- `TOP_1_RECALL`
- `TOP_3_RECALL`
- `TOP_5_RECALL`
- `PRECISION_AT_1`
- `PRECISION_AT_3`
- `PRECISION_AT_5`
- `FALSE_POSITIVE_RATE`
- `NO_TRADE_ACCURACY`

Luna must not mark any requirement PASS, finding CLOSED, strategy validated, or
metric production-ready.

## 3. Mandatory source intake

Before editing, read:

- the controlling pasted brief;
- `docs/quant-refactor/01-target-architecture.md`;
- `docs/quant-refactor/02-requirements-ledger.md`;
- `docs/quant-refactor/03-audit-ledger.md`;
- `docs/quant-refactor/11-wp003-increment-b-sol-audit.md`;
- `docs/quant-refactor/12-wp003-increment-c-sol-audit.md`;
- the final opportunity preparation/result/trace/risk/outcome/persistence
  contracts and tests;
- any existing missed-opportunity, research-metric, validation, or backtest
  implementation that might be reusable.

Report KEEP/ADAPT/REPLACE/DEFER decisions for existing related code. Do not
reuse a float/dict or current-data implementation merely because its name is
similar.

## 4. Global invariants

- Future labels and missed-opportunity code remain unreachable from the
  real-time opportunity package root, features, discovery, regime, registry,
  ranking, risk, gate, and pipeline imports.
- Remove the weak eager core `MissedOpportunityRecord` and `MissCategory` only
  after proving no mounted consumer depends on them. Replacement contracts are
  explicit downstream imports and are not package-root exports.
- Every hindsight qualification is derived by a pure, versioned, content-bound
  policy from exact stored future evidence. A caller may not assert
  `qualified=True`, a category, a metric value, or an earliest surfacing time.
- Qualification must distinguish an executable-trade claim from a weaker
  price-move proxy. Unsupported cost, liquidity, fill, halt, action, session,
  or coverage truth cannot be silently treated as clear or zero.
- To make `UNIVERSE_MISS` possible, qualification input may cover symbols that
  were not evaluated, but its source membership and future observations must be
  explicit, bounded, content-hashed, and independent of the real-time run.
- Every session replay uses exact stored `PipelineResult`, run persistence,
  outcome batch, outcome persistence receipt, and correction-head state. No
  current database row, wall clock, provider call, or relabeling callback may
  change a historical result.
- Earliest surfacing is derived across an exact ordered set of decision-time
  runs. Define discovered, strategy-eligible, ranked/top-K, WATCH/TAKE, and
  too-late states separately; do not overload one timestamp.
- Category assignment follows one documented deterministic precedence. A
  category is emitted only when exact stage evidence proves it. Ambiguous or
  unavailable attribution is `UNKNOWN`, never a guessed stronger label.
- `REGIME_MISCLASSIFICATION` cannot be inferred solely because a later price
  move occurred. It requires explicit retrospective regime evidence or remains
  `UNKNOWN`/another proven stage failure.
- Correct no-trade is a session disposition, not a miss category. Missing or
  incomplete hindsight qualification cannot establish correct no-trade.
- Metrics bind exact definition versions, cohort/session identities,
  numerator, denominator, value, unit, evidence status, and limitations. A zero
  numerator is not the same as a missing denominator. Undefined metrics are
  null with an explicit reason.
- Precision, recall, false-positive rate, and no-trade accuracy must declare
  their matching unit and denominator population. Deduplication across symbols,
  directions, strategies, horizons, and repeated intraday runs must be explicit
  and deterministic.
- All records and batches are immutable, strict-JSON round-trippable,
  content-bound, research-only, and `promotion_eligible=False`.
- Mutating hindsight evidence may change only downstream qualification, miss,
  metric, and later persistence identities. It can never change an original
  universe, feature, candidate, evaluation, rank, decision, trace, or run ID.
- Use exact Decimal arithmetic. No fake zero, lossy float, pickle, opaque dict,
  mutable latest state, or parallel strategy/backtest rule body.
- Keep modules small and acyclic. No runtime, UI, network, broker, scheduler,
  notification, deployment, or active-database import.

## 5. Increment A — qualification and miss classification

Begin with a read-only design checkpoint. Do not edit until Sol approves it.
The checkpoint must specify:

- exact qualification source, policy, evidence/status, opportunity, session
  replay, miss record, and reconciliation-batch contracts;
- how market-wide symbols outside the evaluated universe are represented;
- exact executable-trade versus price-move-proxy semantics;
- qualification formulas, directions, reference/entry/exit rules, session and
  horizon handling, costs, ambiguity, halt/action/missing coverage, and
  unsupported states;
- exact multi-run same-session ordering and earliest-surfacing definitions;
- the deterministic stage-evidence category precedence;
- correct-no-trade, discovered, caught, missed, too-late, pending, censored,
  unavailable, and unknown session/record states;
- direct/from-JSON tamper and Cartesian reconciliation invariants;
- final module graph and import-firewall plan;
- bounded focused tests and exact gate commands.

The pure increment-A API must consume accepted immutable stored inputs and
explicit future source evidence, then emit exactly one disposition for every
hindsight-qualified opportunity. It must also retain qualified opportunities
that were caught so recall can be computed later. Empty, incomplete, and
correct-no-trade sessions need explicit deterministic outputs.

Increment A does not persist miss records or compute aggregate discovery
metrics. Pause for Sol audit when the pure contracts and classifier are green.

## 6. Increment B — discovery metrics

Begin only after Sol accepts increment A. Add pure per-session and multi-session
metric reconciliation for the nine required metric names. The design checkpoint
must give exact hand-calculable formulas and population/matching rules before
editing.

At minimum test:

- numerator zero with a valid denominator;
- denominator zero/unavailable;
- one and multiple qualified opportunities;
- repeated runs for the same symbol/strategy without double counting;
- long/short and multi-strategy matching;
- top-1/3/5 rank boundaries and ties under the accepted deterministic rank;
- surfaced false positives versus unevaluated/nonqualified negatives;
- correct no-trade, false no-trade, and incomplete-truth sessions;
- mixed complete/pending/censored/unavailable sessions;
- direct and consistently rehashed formula/cohort/identity tamper;
- future-evidence mutation isolation.

Pause for Sol audit after increment B. Do not start persistence automatically.

## 7. Increment C — append-only miss/metric persistence and replay

Begin only after Sol accepts increment B and supplies the schema design hold
point. If persistence is justified, use a forward migration after schema 28,
append-only content-bound receipts, correction lineage where needed, exact old
read compatibility, read-only replay, disposable migration rehearsals, and no
mutable current table. Do not alter the accepted outcome history.

## 8. Prohibited shortcuts

- no caller-authored qualification/category/metric output;
- no declaring every later winner an executable trade;
- no using outcome labels in real-time features, scores, rank, risk, or gate;
- no classifying every rejected winner as regime or quality-gate failure;
- no stop-first fallback for ambiguous bars;
- no denominator default to one and no unavailable value default to zero;
- no current-data lookup during historical analysis;
- no weak replacement DTO in core models or package-root eager export;
- no persistence, runtime mount, UI, active DB, network, broker, scheduler,
  deploy, commit, or push in increment A.

## 9. Verification and handoff

Every increment handoff must list exact files, contracts, formulas, decisions,
tests, commands, counts, times, limitations, blockers, and requirement IDs
addressed as evidence only. Run focused tests, affected opportunity/outcome
regressions, whole Ruff, full mypy, compileall, and diff-check. Sol may request
independent attacks or a module split before acceptance.
