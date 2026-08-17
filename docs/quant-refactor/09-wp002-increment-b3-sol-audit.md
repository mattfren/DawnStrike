# SOL audit — WP002 increment B3 authoritative pipeline and pair traces

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts the unmounted two-phase opportunity preparation/finalizer,
authoritative `UniverseSnapshot` input, mounted-in-core all-evaluation
reconciliation, typed pipeline risk policy, content-bound run result, and
pair-level traces. It does not accept persistence, mounted AlphaOps behavior,
historical outcome truth, backtesting, empirical strategy validity, UI, or the
global mission.

## Accepted scope

- `StrategyExpectancyBinding` and `DecisionTrace` v2 in
  `intraday_scanner/v2/opportunity/models.py`;
- explicit missing-expectancy evaluation reasons in
  `intraday_scanner/v2/opportunity/registry.py`;
- `PreparedOpportunityPipeline`, `PipelineRiskPolicy`, `PipelineResult` v2,
  preparation/finalization functions, and pair traces in
  `intraday_scanner/v2/opportunity/pipeline.py`;
- public opportunity-package exports;
- focused preparation, pipeline, result, trace, and adversarial tests.

No persistence, UI, network, broker, scheduler, database, deployment, commit,
or push behavior changed.

## Accepted implementation facts

- Opportunity execution is intentionally two phase. Preparation creates the
  exact immutable evaluations and eligible ranks first; callers can then build
  `ExecutionRiskEvidence` against those exact evaluations before finalization.
  No guessed IDs, opaque callbacks, or weakened mappings are used.
- `run_opportunity_pipeline` no longer accepts a free symbol tuple,
  `universe_id`, benchmark symbol, or untyped expectancy mapping. It finalizes
  only a content-bound `PreparedOpportunityPipeline`, exact risk mapping, typed
  risk policy, and gate configuration.
- Preparation rejects a snapshot with the wrong decision time, dataset ID, or
  exact market-dataset content identity. Only included eligible universe members
  enter cheap features, discovery, rich features, evaluation, and ranking.
- Benchmark lineage remains separate. A benchmark can supply market-relative
  features and a market regime only when its member receipt is included,
  eligible, data-available, halt-clear, and corporate-action-clear. Excluded or
  unknown benchmark truth remains in the universe/run lineage but produces null
  market-relative features and an explicit insufficient market regime.
- `StrategyExpectancyBinding` binds one exact registered strategy ID/version and
  definition hash to embedded expectancy evidence, a causal observation time,
  source/method lineage, and a content identity. Unknown, duplicate, ambiguous,
  future, or mismatched bindings reject; absence is explicit in each evaluation.
- The prepared receipt embeds exact feature/discovery/ranking configurations,
  their identities and versions, registry definitions/evaluator hashes,
  classification bindings, snapshots, candidates, regimes, evaluations, and
  eligible-only ranks. Direct construction and JSON rehydration recheck the
  structural computations and preparation identity.
- `PipelineRiskPolicy` binds account, risk-cap, concentration, and minimum
  after-cost-R policy identities. Non-null receipt thresholds must match; valid
  unavailable policy evidence remains null and flows to a deterministic
  `INSUFFICIENT_DATA` decision instead of aborting reconciliation.
- Finalization requires risk evidence exactly for eligible evaluations and no
  others, revalidates every evaluation/symbol/strategy/version/direction/time
  binding, mounts the accepted B2 reconciler, and emits exactly one decision for
  every evaluation.
- `DecisionTrace` v2 is pair-specific and content-bound. Every evaluation has one
  trace with exact universe/member, evaluation, optional rank, optional risk,
  common decision context, final decision, and canonical eight-stage evidence.
  Global eligible evaluation inputs and ranked outputs are identical across all
  pair traces in the run.
- Locally embedded evaluation and rank objects must exactly equal those embedded
  in the final decision; consistent-rehash cross-content substitution rejects.
- `PipelineResult` v2 embeds the complete preparation, gate/risk policies,
  ordered risks, common decision context, all decisions, and all traces. Direct
  construction and JSON rehydration recompute decisions/traces and the final run
  identity.
- The run identity binds exact universe and dataset identities, benchmark
  lineage, registry/evaluator identities, feature/discovery/ranking/gate/risk
  policy identities and versions, content-bound universe capability IDs, risk
  capability receipts, expectancy/evaluation/rank/risk/context/decision/trace
  IDs and hashes, limitations, and the research-only boundary.
- The bounded two-symbol/two-strategy fixture produces four evaluations, two
  eligible global ranks and exact risk receipts, four decisions, and four pair
  traces. Disabled/non-rankable pairs remain visible; byte-deterministic and
  JSON-round-trip proofs pass.

## Sol adversarial findings remediated

1. the B3 policy validator initially rejected a valid unavailable risk threshold
   that B2 correctly classified as insufficient;
2. an explicitly excluded/unknown benchmark initially produced a benchmark
   snapshot and could drive a `BREAKOUT` market regime;
3. a trace could initially embed a modified rank while its final decision kept
   the original rank under the same rank ID;
4. the first benchmark fix still passed the excluded benchmark symbol into rich
   feature construction, making market-relative truth available as zero.

## Independent proof

Sol independently replayed all four reproducers. Valid unavailable threshold
receipts now yield four decisions/four traces with eligible decisions
`INSUFFICIENT_DATA`; cross-content rank substitution rejects; excluded/unknown
benchmark truth yields no benchmark snapshot, an insufficient market regime,
null/insufficient market-relative features, no relative anomaly, and no eligible
evaluation.

```powershell
py -m pytest tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Result: `285 passed`, exit 0.

```powershell
py -m pytest tests/test_alpha_risk_geometry.py tests/test_alpha_tail_risk_controls.py tests/test_v2_strategy_catalog_expansion.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

Result: `96 passed`, exit 0.

```powershell
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
git diff --check
```

Results: Ruff exit 0; mypy `254 source files`, exit 0; compileall exit 0;
diff-check exit 0 with inherited LF/CRLF warnings only.

## Requirement adjudication

No global requirement or finding is closed. B3 supplies additive evidence toward
REQ-ARCH-001/002/003, REQ-SAFE-001/002, REQ-DATA-001/002/004/005/006,
REQ-FEAT-007, REQ-STRAT-003, REQ-EVAL-001, REQ-RANK-001/002/003,
REQ-GATE-001/002, REQ-RISK-001/002, REQ-TRACE-001, and REQ-TEST-001. Complete
acceptance still depends on append-only persistence and byte-equivalent reload,
future-label-isolated outcomes, miss analysis, research validation, mounted
read-only projection, and final end-to-end proof.
