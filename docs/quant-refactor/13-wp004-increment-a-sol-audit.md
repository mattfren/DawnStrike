# SOL audit — WP004 increment A missed-opportunity qualification and classification

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts the downstream-only, pure hindsight-qualification,
authoritative stored-session replay, earliest-surfacing, missed-opportunity
classification, and session-disposition core. It does not accept discovery
metrics, miss persistence, an end-of-day runtime mount, chronological
validation, stress testing, read-only operator projection, UI, active-database
migration, external data acquisition, broker execution, deployment, or the
global mission.

## Accepted scope

- removal of the weak eager `MissCategory` and `MissedOpportunityRecord` from
  `intraday_scanner/v2/opportunity/models.py` and the opportunity package root;
- strict downstream enums, policy, horizon, numeric evidence, and shared
  contract boundaries in
  `intraday_scanner/v2/opportunity/miss_contracts.py`;
- source-body validation in
  `intraday_scanner/v2/opportunity/miss_source_validation.py`;
- market-wide qualification scope, member, execution, regime, and source
  evidence in `intraday_scanner/v2/opportunity/miss_sources.py`;
- exact Decimal path and qualification logic in
  `intraday_scanner/v2/opportunity/miss_qualification_logic.py` and immutable
  assessment/opportunity/batch reconciliation in
  `intraday_scanner/v2/opportunity/miss_qualification.py`;
- directional per-run projection in
  `intraday_scanner/v2/opportunity/miss_projection.py`;
- exact current-head session inventory and replay in
  `intraday_scanner/v2/opportunity/miss_replay.py`;
- timing and disposition policy in
  `intraday_scanner/v2/opportunity/miss_reconciliation_policy.py`;
- deterministic category, record, and batch reconciliation in
  `intraday_scanner/v2/opportunity/miss_reconciliation.py`;
- the explicit downstream-only facade in
  `intraday_scanner/v2/opportunity/missed.py`;
- qualification, path, inventory, replay, surfacing, taxonomy, tamper,
  isolation, and import-boundary tests in
  `tests/test_opportunity_missed.py`.

No real-time opportunity or storage package root exports or imports the miss
modules. No persistence schema, active database, mounted runtime, UI, network,
broker, scheduler, deployment, commit, or push path was used or changed.

## Accepted implementation facts

- Qualification consumes a content-bound, market-complete or explicitly
  bounded source scope. Complete authority requires exact provider/source
  artifacts, member reconciliation, session coverage, causal fetch/freeze
  times, and nonempty source bodies. Unknown or incomplete source truth cannot
  establish a market-wide negative.
- Qualification policy and horizons are immutable and versioned. Horizons use
  exact UTC session bounds and integer timedelta alignment. The reference is
  the exact first fully supported interval at the declared entry anchor.
- Directional stop/target geometry and all metrics use exact `Decimal`
  arithmetic. Entry-bar target or stop contact, same-bar ambiguity, gap-through
  ambiguity, halts, corporate actions, missing bars, mixed adjustment bases,
  and unresolved safety fail closed with explicit censored, pending, or
  unavailable states. No ambiguity is converted to a stop-first fallback.
- A target-first move becomes `EXECUTABLE_TRADE` only when exact empirical
  quote, NBBO, liquidity, safety, quantity, cost, and after-cost risk evidence
  support it. Otherwise the strongest allowed positive is the distinct
  `PRICE_MOVE_PROXY`; unavailable inputs remain null and never become zero.
- Every qualified opportunity has a strategy-agnostic, content-bound session,
  symbol, direction, policy, and horizon identity. Qualification retains exact
  source observations and independent retrospective regime evidence, remains
  research-only, and is never promotion eligible.
- Session inventory binds the exact ordered stored `PipelineResult`, run
  persistence receipt, current `OutcomeLabelBatch`, outcome persistence head,
  and full correction chain through `CurrentOutcomeReplay`. Complete
  authoritative inventory is derived only from available, scope-complete
  source evidence observed through session close. Premarket runs are allowed
  only inside the explicit query interval.
- Reconciliation projects one coherent directional candidate/evaluation/rank/
  decision/trace pair per run. It selects the deepest surfacing stage with
  deterministic rank, strategy, evaluation, decision-time, and run tie-breaks;
  it never synthesizes a TAKE from a sibling pair or credits an opposite-
  direction anomaly.
- Earliest discovered, strategy-eligible, ranked, top-1, top-3, top-5, WATCH,
  and TAKE times are separate fields. Best on-time rank is separately bounded
  by the strict latest-useful cutoff. A run exactly at the cutoff is too late;
  only `decision_at < cutoff` is on time.
- A positive WATCH or TAKE proves `CAUGHT` even under bounded inventory.
  `MISSED` and `TOO_LATE` require complete authoritative run inventory.
  Incomplete uncaught evidence remains `UNKNOWN`.
- Category precedence is deterministic and evidence-backed:
  `UNIVERSE_MISS`, `DATA_MISS`, `FEATURE_MISS`, `ANOMALY_MISS`,
  `REGIME_MISCLASSIFICATION`, `STRATEGY_MISS`, `SCORING_MISS`,
  `QUALITY_GATE_MISS`, `EXECUTION_FILTER`, then `UNKNOWN`. The selected exact
  pair controls pair-level attribution; a failing sibling cannot override it.
  Regime misclassification requires independent exact retrospective regime
  bodies and rerun classifiers, never the winning future price path alone.
- Every hindsight-qualified opportunity emits exactly one immutable record,
  including caught opportunities. Heterogeneous caught/missed/too-late records
  yield `MIXED`; bounded or unresolved evidence preserves `UNKNOWN`, `PENDING`,
  `CENSORED`, or `UNAVAILABLE`.
- `CORRECT_NO_TRADE` and `FALSE_POSITIVE` are session dispositions, not miss
  categories. Either requires complete-market qualification, every assessment
  conclusively `NOT_QUALIFIED`, and complete authoritative run inventory.
  Any stored WATCH or TAKE then yields `FALSE_POSITIVE`; otherwise the session
  is `CORRECT_NO_TRADE`.
- Direct and strict JSON construction recompute source, policy, horizon, path,
  metric, inventory, replay, projection, category, disposition, timestamp,
  limitation, Cartesian product, and stable identities. Unknown fields,
  consistent-rehash substitutions, cross-pair projections, changed schemas,
  and caller-authored category/disposition values fail closed.
- Mutating valid future observations changes only downstream source,
  qualification, opportunity, miss-record, and reconciliation-batch identities.
  Stored universe, preparation, evaluation, rank, decision, trace, run, and
  session-replay identities remain unchanged.
- The final graph is acyclic and bounded. The largest implementation files are
  781 lines/35.5 KB for sources and 775 lines/30.7 KB for reconciliation; the
  other miss modules are smaller. Core opportunity, pipeline, and both storage
  adapters load zero miss modules until the explicit `missed` facade is
  imported.

## Sol adversarial findings remediated

1. Early qualification drafts could rely on incomplete authority, weak source
   lineage, float regime reconstruction, hidden safety assumptions, or
   under-bound execution references. Final contracts require exact scope/member
   artifact subsets, provider/session/fetch chronology, Decimal feature bodies,
   classifier reruns, exact quote/source bindings, and fail-closed safety and
   adjustment states.
2. The first path resolver did not fully bind entry-bar contact, halt-gap,
   session-status, action-at-horizon, and interval-alignment boundaries.
   Long/short entry-bar target or stop contact now censors, known halt gaps are
   horizon-local, action at the horizon censors unless a terminal event already
   occurred, and timedelta alignment is exact integer arithmetic.
3. The first reconciliation draft treated cutoff equality as on time, could
   synthesize projection fields from different same-direction strategies, and
   credited a symbol candidate discovered only by the opposite directional
   anomaly family. Final projection is coherent, direction-aware, and uses a
   strict cutoff; exact boundary and sibling-pair regressions cover it.
4. The first session summarizer could emit a complete negative despite pending,
   censored, or unavailable assessments and did not classify every heterogeneous
   caught/missed/late set as mixed. Final precedence preserves unresolved states
   and requires all assessments to be conclusively negative before correct-no-
   trade or false-positive classification.
5. Replay limitations could be omitted between source receipt and inventory,
   and ranked surfacing lacked explicit top-1/top-3/top-5 timestamps. Final
   direct invariants bind exact limitation projection and recompute all three
   top-K times plus best on-time rank.
6. Initial tests covered only two taxonomy outputs, and a later attempted matrix
   merely passed category enum constants through a precedence helper. The final
   matrix executes the production universe/data/anomaly/rich-feature/regime/
   strategy/gate classifiers with exact typed evidence, covers all ten outputs
   and ambiguity, proves precedence collisions, and round-trips representative
   early-category records and batches.
7. Qualification and reconciliation grew beyond the bounded-module target
   during hardening. Behavior-preserving partitions now isolate source
   validation, qualification logic, run projection, and reconciliation policy;
   all implementation modules are below 800 physical lines and 40 KB.

One intermediate split compile failed because a tool-display truncation marker
entered a mechanically reconstructed facade; the exact prior resolver block was
restored before tests ran. Other intermediate issues were fixture-only: a
firewall probe named nonexistent `regime.py` instead of `regimes.py`, a taxonomy
test namespace omitted delegated snapshot properties, and Ruff corrected import
order or unused imports after file moves. No production behavioral failure was
observed in the final focused or broad gates.

## Independent proof

Sol independently inspected the final source, qualification, replay,
projection, category, session-disposition, strict-construction, and import
paths. Sol repeatedly ran the focused suite during adversarial review; the final
authoritative focused command was:

```powershell
py -m pytest tests/test_opportunity_missed.py -q -p no:cacheprovider
```

Sol result on the final taxonomy-remediated state: `46 passed`, exit 0, 162.4
seconds. Focused Ruff passed, mypy reported no issues in all ten miss source
modules, and focused compileall passed.

Final combined opportunity/outcome/persistence gate:

```powershell
py -m pytest tests/test_opportunity_missed.py tests/test_opportunity_outcomes.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py tests/test_opportunity_outcome_persistence.py -q -p no:cacheprovider
```

Result: `475 passed`, exit 0, 1006.1 seconds. Independent collection reconciled
exactly as 46 missed-opportunity + 73 outcomes + 23 run persistence + 5
migration + 9 contracts + 18 features + 57 pipeline + 200 universe/risk + 44
outcome persistence.

Affected storage/data-truth gate:

```powershell
py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

Result: `139 passed`, exit 0, 113.2 seconds. Independent collection reconciled
as 10 read-only store + 46 no-persist semantics + 83 data-truth/paper-ops.

Final static and compile proof also passed: whole-repository Ruff; mypy across
276 source files; compileall for `intraday_scanner` and `scripts`; diff-check;
and the fresh-process/AST import firewall. No source or test edit occurred during
the long gates.

## Requirement adjudication

No global requirement or finding is closed. Increment A supplies additive
evidence toward REQ-ARCH-001, REQ-SAFE-001/002, REQ-DATA-002/003/005/006,
REQ-MISS-001/002, REQ-TRACE-001, REQ-TEST-001/002, and REQ-DOC-001. Acceptance
still depends on the nine required discovery metrics, miss/metric persistence,
shared chronological validation and BASE/2X/3X cost stress, empirical/external-
data evidence, disabled-by-default mounted read-only projection, operator UI,
active-database migration, and final clean-worktree end-to-end proof.
