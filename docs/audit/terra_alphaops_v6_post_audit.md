# Terra AlphaOps V6 post-audit

Date: 2026-08-01 (America/Chicago)

Audited Terra commits: `ed9be163`, `a2859a2d`

Branch: `codex/terra-alphaops-v6`
Verdict: **Terra built valuable infrastructure, but its claim of implementation completeness was false.**

## What Terra got right

- It isolated the release from the dirty original checkout and backed up durable SQLite state before migration.
- It preserved research-only, no-broker, no-LLM-scoring, missing-is-not-zero boundaries.
- It added immutable V6 ledgers, a versioned universe contract, benchmark contracts, fail-closed scheduling checks, native process receipts, public path-leak scanning, Calendar, and a V6 shadow surface.
- Its initial audit correctly refused to invent V5/V6 return evidence. The existing V4 evidence is only eight closed positions, two wins, six losses, gross P&L of `-$487.61`, and profit factor `0.2639`; that is diagnostic evidence, not a valid account return series.

## Material misses found

| Area | Terra claim | Audit finding |
|---|---|---|
| Training | Complete V6 learner | `training.py` returned a registered plan with `fitted: false`; no usable model was trained. |
| Validation | Strict walk-forward | Purged folds existed but were not used to generate predictions. The learner evaluated in-sample predictions on the same outcomes used for fitting. |
| Model serving | Shadow challenger active | No persisted frozen model artifact scored later daily decisions. |
| Uncertainty | Calibrated lower-bound utility | Prediction intervals were never populated; calibration could not support displayed probabilities or expected returns. |
| Rejected decisions | Regret learning | Rejected candidates were sampled, but their counterfactual outcomes were never captured. Inclusion probabilities were not used in evaluation. |
| Source truth | Independent benchmarks/outcomes | Outcome capture stopped after the first successful provider and did not reconcile an independent source. |
| Promotion | Full evidence gate | The gate omitted several required return, benchmark, risk, source, slippage, holdout, and human-review conditions. |
| Holdout | Untouched holdout | “Evaluate once” was text only, not enforced by durable uniqueness. |
| Decision ledger | Immutable replay | Required feature schema/hash, score components, uncertainty, execution assumptions, and explicit no-trade decisions were incomplete. |
| Scheduler | Reliable daily chain | Three wrappers still bypassed the native receipt runner; EOD could mark learning complete before V6 failed and could overwrite the first failure code. |
| UI | V6 evidence visible | The public UI showed a count, not full gates, model evidence, or Decision Replay. Rendered QA exposed critical Calendar ARIA defects and low-contrast dates. |
| Release | Production-ready implementation | Production still serves source SHA `692e785c`, readiness is HTTP 503, and `/data/v6-learning.json` is absent. The runtime has no production source config or registered V6 universe. |

## Corrective work completed on this branch

- Replaced the scaffold with deterministic, JSON-auditable scikit-learn baselines: regularized activation, conditional-return and tail models; chronological conformal uncertainty; and an evidence-gated gradient challenger.
- Added prohibited-feature filtering, source/date cutoffs, inverse-probability weights, deterministic hashes, and purged/embargoed out-of-fold predictions.
- Added a frozen-artifact inference path that rejects same-day, future-cutoff, and feature-schema-mismatched decisions.
- Rebuilt validation with after-cost/IPW expectancy, benchmark excess, drawdown, profit factor, concentration, capacity, turnover, tie-correct rank correlation, 1.5x/2x slippage stress, date-cluster bootstrap intervals, multiple-testing adjustment, negative controls, segmentation, and no-lookahead checks.
- Added sampled-reject counterfactual outcome capture and independent Yahoo/Alpaca bar reconciliation. Missing or conflicting evidence remains ineligible, never zero.
- Expanded immutable decisions and explicit no-trade receipts; added model/evaluation/drift/replay evidence with prediction hiding until calibration, interval, and no-lookahead gates pass.
- Added durable one-time holdout evaluation enforcement and a full fail-closed promotion gate. Promotion remains manual and cannot place an order.
- Repaired process receipts, first-failure propagation, EOD stage ordering, Vercel token authentication, secret-key import, and recoverable backup-and-disable handling for five conflicting legacy tasks.
- Restored and hardened the Calendar and V6 Research UI. Browser verification at 360x800, 390x844, 768x1024, 1280x720, and 1440x900 found zero horizontal overflow. The corrected Calendar/Research surfaces had no page or console errors and zero automated WCAG A/AA violations.

## Proof completed

- Full pytest suite: `609 passed in 380.98s`.
- Ruff: passed.
- Mypy: passed across 204 source files.
- Compileall: passed.
- JavaScript syntax: passed.
- Focused model tests prove real fitting, prohibited-feature exclusion, purged no-lookahead predictions, frozen later-date scoring, schema rejection, IPW, slippage stress, bootstrap intervals, sampled-reject labels, and one-time holdout enforcement.
- Degraded no-data public build correctly remained HTTP 503 and failed publication instead of manufacturing readiness or returns.

## What remains before “best of the best” is honest

1. Separate daily outcome/calibration/drift monitoring from weekly challenger refitting. The current EOD path still invokes training daily and the research-packet command repeats learning work.
2. Evaluate the controlled gradient model in the same purged walk-forward competition; today only the regularized baseline has out-of-fold comparison evidence.
3. Build causal failure attribution across all V4/V5/V6 and PaperOps decisions, including selection, entry, stop/target, liquidity, source conflict, catalyst, regime, concentration, and outcome-quality categories.
4. Implement an automated, licensed/source-backed small-cap universe adapter and point-in-time daily snapshot. The repository only has a fail-closed registration contract and template.
5. Produce an apples-to-apples objective comparison against frozen V5, cash, SPY, and IWM. Promotion correctly cannot pass without it.
6. Add an operator command for the DB-enforced one-time holdout evaluation and preserve the receipt in the public model evidence.
7. Expand Decision Replay with source drill-down, incident timeline, counterfactual sampled-reject results, and categorical failure attribution.
8. Merge, cut over the stable runtime, import real credentials, register noninteractive tasks, disable conflicts, deploy the exact merged SHA, and prove one unattended market day.
9. Accumulate at least 60 real forward sessions and 100 valid sourced closed paper labels. Until then, return superiority is unknown and must remain `WAITING_FOR_FORWARD_EVIDENCE`.

Bottom line: **the corrected branch is a credible research platform foundation, not yet a proven return engine or production-complete system.** The remaining execution directive is `docs/audit/terra_alphaops_v6_remediation_prompt.md`.
