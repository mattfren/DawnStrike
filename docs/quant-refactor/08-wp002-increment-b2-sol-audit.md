# SOL audit — WP002 increment B2 all-evaluation decisions

## Verdict

`ACCEPTED_AS_ADDITIVE_FOUNDATION`

This verdict accepts only the TradeDecision v2 contract, typed pre-decision run
context, risk-aware absolute gate, and pure one-decision-per-evaluation
reconciler. It does not accept the legacy pipeline compatibility path as
authoritative, UniverseSnapshot integration, pair traces, persistence, mounted
behavior, empirical validity, or the global mission.

## Accepted scope

- `DecisionRunBinding`, `DecisionRunContext`, and `TradeDecision` v2 in
  `intraday_scanner/v2/opportunity/models.py`;
- the risk-aware gate, decision-context builder, and pure reconciler in
  `intraday_scanner/v2/opportunity/quality_gate.py`;
- public opportunity-package exports;
- minimal unmounted WP001 pipeline compatibility;
- focused contract, pipeline, and universe/risk/decision tests.

No persistence, UI, network, broker, scheduler, database, deployment, commit,
or push behavior changed.

## Accepted implementation facts

- `DecisionRunContext` is computed from ordered evaluation identities and
  hashes, statuses and lifecycles, eligible rank identities and hashes, supplied
  risk-receipt identities and hashes, decision time, gate-policy identity and
  version, and the research-only boundary.
- The context is content-bound and non-empty. Risk receipts may be supplied only
  for eligible evaluations; the pure reconciler requires them exactly for every
  eligible evaluation. Riskless eligible bindings remain allowed only for the
  explicitly non-authoritative WP001 compatibility path.
- `TradeDecision` embeds and revalidates the exact evaluation, optional rank,
  typed run context, pair metadata, optional risk identity/hash, non-rankable
  reason, lifecycle, checks, vetoes, rationale, limitations, and research-only
  state.
- Ranked and non-rankable decisions have exact canonical ordered gate-check
  schemas. Every canonical check is mandatory, so direct construction or JSON
  rehydration cannot omit, inject, reorder, or demote a required check.
- The gate obtains gross and after-cost reward/risk only from matching
  `ExecutionRiskEvidence`; it never reads
  `StrategyEvaluation.after_cost_reward_risk` as execution proof.
- `TAKE` requires production lifecycle, watch and take score thresholds, data
  quality, liquidity, gross and after-cost geometry, an available matching risk
  policy threshold, no execution-risk veto, complete observed-or-derived
  empirical risk numerics, and available positive empirical expectancy with
  sufficient sample and bounded uncertainty.
- Any missing or provisional mandatory proof blocks `TAKE`. A valid unavailable
  minimum-after-cost policy remains explicit and produces
  `INSUFFICIENT_DATA`; it does not abort decision reconciliation.
- Non-production eligible pairs may `WATCH` only when the research watch checks
  pass. Disabled or rejected lifecycles cannot `WATCH`; no non-production state
  can `TAKE`.
- Relative rank is not a quality decision. Rank 1 may `PASS`, and the gate does
  not depend on list position.
- The pure reconciler rejects duplicate/unknown/noncontiguous ranks, unknown or
  non-eligible risk keys, mismatched evaluation/rank/risk identities, duplicate
  pairs, and strategy-version ambiguity. It preserves evaluation order and
  emits exactly one content-unique decision for every evaluation.
- Non-rankable mappings are deterministic: `REJECTED` and `DISABLED` become
  `PASS`; `INSUFFICIENT_DATA` remains `INSUFFICIENT_DATA`; each retains an exact
  status reason and evaluation limitations.
- A fully evidenced bounded synthetic production fixture produces `TAKE` even
  when the legacy evaluation-level after-cost field is deliberately `-99`,
  proving the new risk receipt is authoritative at this gate.

## Sol adversarial findings remediated

1. a valid unavailable risk-policy threshold raised an exception instead of
   yielding one fail-closed disposition;
2. an unavailable numeric could incorrectly satisfy the empirical-risk check;
3. a consistently rehashed `TAKE` could omit all canonical checks except one
   fabricated passing check;
4. direct construction accepted an empty decision-run context;
5. the public context builder and direct binding contract accepted risk evidence
   attached to a non-eligible evaluation.

## Independent proof

Sol independently reran the four original reproducers. The forged `TAKE`, empty
context, and non-eligible risk context are now rejected. The valid unavailable
policy receipt now emits `INSUFFICIENT_DATA`, with policy availability `None`
and empirical-risk completeness `False`.

```powershell
py -m pytest tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

Result: `267 passed`, exit 0.

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

No global requirement or finding is closed. B2 supplies additive evidence toward
REQ-SAFE-001/002, REQ-EVAL-001, REQ-RANK-001/002/003, REQ-GATE-001/002,
REQ-RISK-001/002, REQ-TRACE-001, and REQ-TEST-001. Complete acceptance still
depends on the authoritative UniverseSnapshot pipeline input, mounted
one-decision-per-pair reconciliation, pair-level traces, persistence,
validation, read-only UI projection, and final end-to-end proof.
