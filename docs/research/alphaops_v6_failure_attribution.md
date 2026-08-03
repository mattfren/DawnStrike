# AlphaOps V6 failure attribution

Status: `PERFORMANCE_STATUS=WAITING_FOR_FORWARD_EVIDENCE`

This is an evidence boundary, not a performance claim. V6 has no sourced,
closed forward labels yet. It therefore cannot attribute a V6 return or infer a
winning policy. Missing outcomes remain null and are excluded from every return
metric.

## Revalidated historical findings

The pre-V5 V4 record contains eight closed positions: two wins, six losses,
gross P&L of `-$487.61`, summed allocation return of `-6.0953%`, and profit
factor `0.2639`. BIYA accounts for roughly 71% of gross losses. Those values
are small-sample historical evidence, not a valid account-level return series.

The observed failure decomposition is:

| Failure family | V4 evidence | V5 / V6 status | Response |
|---|---|---|---|
| Data failure | Spread, float, halt, SEC and prior-close truth were often absent or unverified. | V5 blocks unknown safety truth; V6 records it as a veto. | Do not admit official paper rows with missing evidence. |
| Source conflict | Most historical source confidence was 22–34.5. | No verified V5/V6 outcome set exists. | Retain provider disagreement and fail closed. |
| Selection error | All eight fills were C/D, `NEEDS_CONFIRMATION`, weak-source setups. | V5 has no eligible results. | V6 preserves selected, blocked, and policy-rejected decisions. |
| Entry timing/chase | Entries occurred after close or near forced liquidation. | No V6 labels. | V5 frozen entry window and V6 simulated-fill label. |
| Liquidity/spread | The legacy ledger had no sourced spread evidence. | Unknown remains a block. | Capture sourced spread and capacity fields before eligibility. |
| Incorrect stop/target | Invalidations were 25–51%; targets were often risk-derived. | No V6 source labels. | Separate stop-first/target-first, MFE and MAE labels. |
| Sizing/concentration | BIYA dominated loss concentration. | V5 no eligible account series. | Retain concentration and tail-loss gates. |
| Catalyst weakness | Historical rows had no clear catalyst and weak confidence. | No V6 causal labels. | Record catalyst lineage; no future status may repair it. |
| Regime mismatch | V4 used a four-label gap/volume heuristic. | V6 regime cohort is descriptive until samples exist. | Compare only in purged forward folds. |
| Tail event loss | One uncapped loss explains much of the historical drawdown. | No V6 tail sample. | Conservative lower-bound utility includes adverse-tail penalty. |
| Outcome/reconciliation failure | Current state began with zero AlphaOps outcome labels and 55 reconciliation issues. | V6 does not create a return when reconciliation is absent. | Terminal-missing receipts and data-quality labels. |

## Current V6 evidence

V6 writes a decision for every observed candidate. It tracks only a safety-clear
shadow cohort, deterministically samples policy-rejected candidates for regret
research, and persists source hashes, feature hashes, cost assumptions, vetoes,
and point-in-time timestamps. The live evidence counts remain the source of
truth in the database and public `data/v6-learning.json` projection.

No strategy is profitable, calibrated, promoted, or even return-model-trainable
until the full forward-evidence gate is met.
