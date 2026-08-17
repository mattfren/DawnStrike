# SOL audit — WP003 increment B causal outcome contracts

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts the downstream-only causal outcome contracts, exact
post-decision source evidence, pure all-evaluation-by-horizon labeler, and
retrospective research records. It does not accept outcome persistence,
supersession/replay storage, missed-opportunity analysis, empirical strategy
validity, mounted AlphaOps behavior, UI, scheduling, external data acquisition,
broker execution, deployment, or the global mission.

## Accepted scope

- removal of the weak v1 `OutcomeRecord` from core opportunity models and the
  package-root eager export;
- downstream contracts and strict serialization helpers in
  `intraday_scanner/v2/opportunity/outcome_contracts.py`;
- causal bar, coverage, series, and dataset evidence in
  `intraday_scanner/v2/opportunity/outcome_sources.py`;
- shared exact state, path, and metric resolution in
  `intraday_scanner/v2/opportunity/outcome_resolution.py`;
- content-bound `OutcomeRecord` v3 and its direct invariants in
  `intraday_scanner/v2/opportunity/outcome_records.py`;
- `OutcomeLabelBatch` v2 and pure Cartesian label reconciliation in
  `intraday_scanner/v2/opportunity/outcome_replay.py`;
- the explicit downstream-only facade in
  `intraday_scanner/v2/opportunity/outcomes.py`;
- the focused causal, directional, ambiguity, censoring, tamper, identity, and
  import-boundary tests in `tests/test_opportunity_outcomes.py`.

No outcome table, persistence adapter, active database, runtime mount, UI,
network, broker, scheduler, notification, deployment, commit, or push behavior
changed.

## Accepted implementation facts

- Importing the opportunity package root, models, features, discovery, regimes,
  registry, ranking, risk, gate, pipeline, or opportunity store loads no
  `intraday_scanner.v2.opportunity.outcome*` module. Outcome callers must import
  the explicit `opportunity.outcomes` submodule.
- The outcome implementation is partitioned into bounded acyclic modules:
  contracts, sources, resolution, records, replay, and facade. No legacy float
  path replay, backtest engine, UI, runtime, network, broker, or current-time
  lookup is delegated into this layer.
- Horizons bind the exact decision time, explicit exchange session, elapsed or
  session-close rule, and UTC end. The label policy fixes planned-price-touch
  entry and conservative censoring for entry-bar, same-bar, and gap-through
  ambiguity. It is research-only and never promotion-eligible.
- Every retained bar is an exact-Decimal immutable `IntradayBar` wrapped with a
  content-bound observation identity, exact interval, source-derived
  availability, source request/session scope, and embedded bar hash. Intervals
  begin strictly after the decision time.
- Observation series bind and revalidate the exact coverage receipt,
  provider/feed/entitlement, request and observed bounds, retained bar body,
  explicit missing intervals, market-status facts, corporate actions, source
  artifacts, and sanitized lineage. Complete, partial, halt-gap, unresolved,
  and hard-unavailable coverage states cannot contradict their retained bodies.
- Observation datasets bind the exact sorted symbol series, full source
  artifact-hash set, causal freeze time, and research-only state. No source fact
  or used observation may postdate the dataset freeze.
- `OutcomeRecord` v3 embeds its exact source series, source freeze, and full
  horizon-local observation slice. Direct construction and JSON rehydration
  re-run coverage/halt/action/pending/path resolution and recheck reference and
  horizon prices, entry/exit/touch intervals, exact metric formulas, exact
  source lineage, availability times, reasons, limitations, and identity.
- The path resolver distinguishes no entry, not applicable, unattainable fill,
  entry-bar ambiguity, same-bar ambiguity, gap-through ambiguity, target first,
  stop first, horizon exit, pending horizon, missing bars, halt censorship,
  corporate-action censorship, and unsupported evidence. It never silently
  converts missing or ambiguous truth to zero, loss, or no trade.
- PASS and INSUFFICIENT decisions retain counterfactual path truth when their
  planned geometry is available. Disabled/no-geometry pairs remain explicit and
  still receive one outcome per declared horizon.
- Terminal target/stop truth supported before a later gap, halt, action, or
  still-open horizon is retained as partial evidence. Entry-bar extrema do not
  establish post-fill ordering; post-entry target and stop in the same bar are
  censored rather than assigned stop-first.
- Reference horizon return uses the directional first-post-decision open to the
  exact supported horizon close. MFE/MAE exclude entry-bar extrema, use exact
  Decimal stop-distance units, and do not consume unknowable post-exit extrema.
  Gross R and after-cost R are recomputed from the modeled path and exact bound
  execution-risk quantity/cost evidence. Unsupported values remain null with
  explicit reasons.
- Touch times are interval lower/upper bounds, not falsely precise timestamps.
  Metric values, methods, causal observation IDs, and observed times use one
  canonical exact order.
- `OutcomeLabelBatch` v2 embeds and revalidates the accepted `PipelineResult`,
  its append-only persistence receipt, the full source dataset, policy,
  canonical horizons, and deterministic evaluation-major Cartesian output.
  Every evaluation and horizon has exactly one record; empty evaluations allow
  an exact empty product.
- Mutating a future bar changes downstream source, record, and batch identities
  only. The stored preparation, evaluation, decision, trace, and run identities
  remain unchanged. Every policy, record, dataset, and batch is explicitly
  retrospective/research-only and non-promotable.

## Sol adversarial findings remediated

1. The initial public record stored only source observation IDs and hashes.
   Consistently rehashed standalone payloads could change a reference price from
   104 to 999, shift an entry interval by one second with a matched touch bound,
   or change horizon-exit MFE/MAE to 99/-99 while retaining the same claimed
   source identities. The enclosing batch rejected the drift, but
   `OutcomeRecord.from_dict` accepted it.
2. `KNOWN_HALT_GAPS` initially censored every horizon for the symbol. A gap
   beginning exactly at a completed early horizon's end incorrectly made that
   earlier horizon `HALT_CENSORED`. Receipt gaps are now filtered by exact
   horizon overlap before halt censorship is applied.
3. Source-body embedding alone was not enough to make a standalone record
   authoritative. A complete horizon exit could be coherently relabeled as
   missing, and a complete target-first path as partial, while retaining source
   lineage. Records now embed the exact source series and re-run the shared
   state/path resolver before accepting statuses, reasons, metrics, or identity.
4. Moving record validation into replay temporarily grew one module to 1,903
   lines. The final behavior-preserving split restores bounded ownership and an
   acyclic dependency graph without changing public facade names or serialized
   identity payloads.

## Independent proof

Sol independently reproduced the original record attacks and the non-local halt
receipt before remediation. The original direct record accepted
`reference_price=999`, a 119-second forged upper touch bound, and MFE/MAE
`99/-99`; the early horizon was incorrectly halt-censored. After remediation,
Sol independently ran the seven focused source-binding, coherent-relabel,
horizon-local halt, and import-firewall tests; all seven passed.

A fresh Python process importing only the opportunity root, pipeline, and
opportunity store reported an empty set of loaded `opportunity.outcome*`
modules. An AST dependency check confirmed the acyclic downstream graph.

```powershell
py -3 -m pytest tests/test_opportunity_outcomes.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Sol result: `383 passed`, exit 0, 236.1 seconds.

```powershell
py -3 -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

Sol result: `139 passed`, exit 0, 111.5 seconds.

Luna's final-state whole-repository gates also passed: Ruff exit 0; mypy
`261 source files`, exit 0; compileall exit 0; diff-check exit 0 with inherited
line-ending warnings only. Sol independently rechecked focused Ruff, all six
outcome modules with mypy, opportunity-package compileall, and diff-check.

## Requirement adjudication

No global requirement or finding is closed. Increment B supplies additive
evidence toward REQ-ARCH-001/002/003, REQ-SAFE-001/002, REQ-DATA-005/006,
REQ-OUT-001/002/003, REQ-TRACE-001, REQ-OBS-001, REQ-BT-001, and
REQ-TEST-001/002. Acceptance still depends on append-only outcome persistence
and supersession/replay, missed-opportunity metrics, empirical validation,
disabled-by-default read-only projection, and final end-to-end proof.
