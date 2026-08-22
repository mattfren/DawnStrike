# Strategy decision receipts — 2026-08-22

## Audit status and provenance

Lane C owns only this document and `2026-08-22-proof.json`. The source and
test implementation was not changed in this lane.

The implementation snapshot is the last source-code implementation commit
(`9f021ec8…`); its full commit and tree identities are recorded in the proof
JSON. The proof-parent and PR-head snapshot is `ae142eab…`, which includes the
static-security repair and the scanner-false-positive test repair. The branch
was clean at that snapshot. The documentation commit created by this lane is
deliberately not written into the JSON as its own SHA: doing so would make the
proof self-referential. The containing proof commit is returned by the lane
handoff and its parent is recorded in the JSON.

PR #42 is open, targets `codex/strategy-learning-remediation-20260821`, and
points at the audited pre-proof branch head. Both the push and pull-request CI
runs passed for that exact head. This lane did not push the documentation-only
commit, modify PR metadata, merge, deploy, or change runtime/state.

## Registry and policy

Every manifest includes the common `HARD_MARKET` conditions (`valid_symbol`,
point-in-time OHLCV, positive price and volume, source identity/freshness, and
source-conflict checks), common `HARD_RISK` conditions (halt, entry/stop/target
geometry, 1.50R, risk budget, and spread), and advisory float, secondary
source, historical sample, and catalyst conditions. Strategy-specific core and
contextual conditions are defined in `condition_registry.py` for:

1. `ts_momentum_sma_atr` — trend, extension, volatility, offering/dilution,
   corporate action, adverse event.
2. `donchian_breakout_20_10` — breakout quality, extension, participation,
   volatility, catalyst, filing risk, offering/dilution. Missing catalyst alone
   does not block a technically valid research pick.
3. `cross_sectional_relative_strength` — rank membership/margin, sector
   concentration, identity, and sector/industry. Unresolved sector uses one
   explicit `UNKNOWN` bucket and remains conditional; it is not diversified.
4. `pullback_reclaim_uptrend` — slope, waterfall, reclaim, adverse event,
   offering/dilution, regulatory event.
5. `volatility_contraction_breakout` — participation, dead liquidity, regime,
   contraction breakout, earnings, regulatory event, catalyst timing.
6. `failed_breakout_reversal_short` — rejection, squeeze, failed-breakout
   confirmation, borrow/locate, offering/dilution, squeeze event, corporate
   action. Missing borrow is conditional and blocks paper entry.
7. `bullish_fvg_continuation` — gap, participation, trend, explicit daily-OHLC
   proxy disclosure, intraday microstructure, catalyst, offering/dilution. A
   daily proxy cannot establish paper-entry eligibility.
8. `gap_up_continuation` — gap, close location, trend, participation, data
   quality, corporate-action basis, catalyst, offering/dilution. Unresolved
   corporate-action basis is conditional; a confirmed split-only gap is not an
   ordinary continuation catalyst.
9. `gap_up_continuation_atr` — the gap-up manifest plus ATR normalization and
   volatility-event context.

The deterministic policy preserves the existing 1.50R floor and score
threshold. Hard market, hard risk, and strategy-core failures block. Advisory
gaps produce `PICK_WITH_DISCLOSED_GAPS`; execution-only gaps produce
`CONDITIONAL_PICK` and keep paper-entry eligibility false. Broker execution is
always false. Missing evidence is never converted to zero, false, safe, or
passed.

Sanitized tier examples are fixture-only and contain no market claims:

- `QUALIFIED_PICK`: all blocking conditions pass; reward/risk is 2.0R.
- `PICK_WITH_DISCLOSED_GAPS`: only `catalyst_identified` is missing.
- `CONDITIONAL_PICK`: `borrow_or_locate_verified` is missing.
- `BLOCKED_SAFETY`: `not_currently_halted` fails.
- `BLOCKED_DATA`: point-in-time OHLCV is missing.

## AI and source trust

`StrategyGapResolver` uses the existing OpenAI Responses pattern with
`web_search`, strict JSON-schema output, `store=False`, `max_retries=0`,
bounded tool calls, and actual response-model identity. It permits only
contextual claim types, requires cited URLs and point-in-time publication
timestamps, hashes citation identity, enforces ticker/entity matching, rejects
prompt injection and forbidden market/trade fields, preserves contradictions
and unknowns, and degrades provider failures to disclosed gaps. AI cannot
provide price, volume, VWAP, spread, float, entry, stop, target, reward/risk,
return, probability, sizing, or a recommendation. Deterministic policy alone
makes the final decision.

Resolution is limited by the existing maximum-symbol, maximum-tool-call, and
timeout configuration. Same-day cache identity includes market date, symbol,
condition set, source identity, prompt version, and model. Unit tests mock AI
and provider calls; no network request or provider token cost was incurred by
the local test run.

## Persistence and operator surfaces

The additive migration creates `strategy_decision_receipts`,
`strategy_condition_results`, `strategy_evidence_claims`, and
`strategy_evidence_resolution_runs`. It is forward-only and idempotent;
canonical JSON and SHA-256 identity are stored, exact duplicate payloads are
reused, and a same-ID/different-payload attempt fails closed. Prior signal and
performance rows are not rewritten, and the protected runtime/state database
was not opened or mutated.

Shadow mode computes and persists receipts while leaving legacy selection and
alerting unchanged. Telegram includes tier, receipt ID, disclosed gaps, and
entry-confirmation state when receipt fields are present. Daily learning
consumes receipts as research evidence only and never changes policy,
promotes a strategy automatically, or enables broker execution.

## Changed files audited against the base

The branch diff from the required base contains only the directive allowlist.
The complete grouping is recorded in the proof JSON:

- Contracts and registry: decisioning contracts, registry, policy, and service.
- Resolver and persistence: evidence resolver, AI resolver, migrations, and
  SQLite receipt storage.
- Integration and operator: configuration, Alpha cycle integration, and
  Telegram formatting.
- Learning and tests: daily learning plus the six strategy decision test files.
- Documentation and proof: this document and `2026-08-22-proof.json`.

## Verification and remaining gaps

The focused strategy receipt command passed with 15 tests. Local Ruff, mypy,
compileall, JavaScript syntax, Bandit, JSON parsing, diff checks, and the
owned-document detect-secrets scan passed. The local unconstrained full-suite
command was not completed: its initial run exposed the legacy schema-marker
compatibility issue, and the post-repair heavy fixture rerun was interrupted.
No local full-suite PASS claim is made.

Exact-SHA CI is stronger than that local limitation for the implementation
snapshot: push run `32587040383` and PR run `32587042757` both ran at the
audited branch head, with all 16 pytest shards, Python/public-contract checks,
dependency/static/SBOM checks, and Windows schedule/secret checks passing.
Those runs do not cover the new documentation-only commit because it was not
pushed. The proof JSON records this distinction, the exact PR URL, the
implementation/tree identity, and the proof-parent identity.

The remaining evidence gap is therefore local full-suite termination after the
compatibility repair, plus the absence of CI for this unpushed documentation
commit. Production, runtime/state, schedulers, deployment, merge, and broker
execution remain untouched; automatic policy change and promotion remain
disabled; missing outcomes are not treated as zero.
