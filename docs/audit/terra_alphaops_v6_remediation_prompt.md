# TERRA EXECUTION PROMPT — FINISH DAWNSTRIKE ALPHAOPS V6

Copy everything below into Terra. It is an execution directive, not a planning request.

---

You are Dawnstrike's principal research-platform engineer. Finish the remaining AlphaOps V6 work, integrate it, deploy it, and prove it. Do not return another plan. Continue until every immediately executable gate is proven and only genuine user credentials or forward-time market evidence remains.

## Authoritative workspace

- Work only in `C:\r\dawnstrike-terra-v6` on branch `codex/terra-alphaops-v6`.
- Treat the current branch as authoritative, including `docs/audit/terra_alphaops_v6_post_audit.md` and all Codex post-audit fixes.
- Preserve the dirty checkout at `C:\Users\MattFields\Dawnstrike`; do not edit, clean, reset, merge into, or run migrations against it.
- Preserve durable state at `C:\r\dawnstrike-state`. The pre-Terra backup is `migration-backups\terra-v6-20260801T082756Z\shadow_real.sqlite`, SHA-256 `99189B4B6CB31E8D3DDDFA6A8DFE2295620D02302BB7D3BB36F66EAD4D23663D`.
- Production is `https://dawnstrike-command-center-x3.vercel.app`; it currently serves old SHA `692e785cf8304a8045e88ab221dc644d4eb2e9e7` and readiness HTTP 503. Do not call it current or ready until the exact merged SHA is proven there.

## Non-negotiables

- Research and paper audit only. Never add broker execution, order placement, sizing automation, or a mutation route that can trade.
- Never use an LLM to create financial scores, targets, selections, promotion decisions, or recommendations.
- Missing, conflicting, stale, or unreconciled truth remains null/ineligible, never zero.
- Preserve raw facts, calculated metrics, model outputs, source lineage, and decision-time timestamps as separate fields.
- No fake production data, synthetic universe, retroactive return invention, lookahead features, random row splits, repeated forward-sample tuning, or test weakening.
- V5 remains frozen until V6 passes the complete forward gate and a human records approval. Automatic promotion is forbidden.
- Do not undo Codex's fitted-model, frozen-serving, sampled-reject, independent-reconciliation, full-promotion-gate, holdout, scheduler, Vercel-auth, Calendar, accessibility, or Decision Replay fixes.
- One product, one return engine, one durable database, one scheduler chain, one public deployment.

## Execute in this order

### 1. Revalidate and freeze the baseline

Record branch, HEAD, `origin/main`, worktree status, installed research dependencies, DB hash/schema/quick-check, task definitions/results, runtime config presence, universe registration, Vercel deployment ID/SHA, health, readiness, build ID, and `/data/v6-learning.json` status. Redact secrets. Back up the real DB before any migration. If the branch has unexpected user changes, preserve them and stop only for an actual collision.

### 2. Split daily monitoring from weekly training

Refactor at minimum:

- `intraday_scanner/services/alpha_v6_learning_service.py`
- `intraday_scanner/services/alpha_v6_research_service.py`
- `intraday_scanner/cli.py`
- `scripts/run_alphaops_eod.ps1`
- `scripts/register_alphaops_tasks.ps1`

Create explicit idempotent modes:

- Daily: capture/reconcile outcomes, build labels/dataset, score with the already frozen artifact, evaluate calibration/intervals/drift, and write evidence. It must not refit or select a new model.
- Weekly: fit registered challengers from a frozen cutoff, generate purged/embargoed out-of-fold predictions, compare models, persist one deterministic artifact/receipt, and never promote.
- Research packet: read persisted evidence only. It must not rerun training or attribution.

Add a reliable weekly scheduled task using the same password-logon, battery-safe, start-when-available, no-overlap, structured-receipt controls as the canonical daily chain. Daily and weekly failures must remain independently attributable.

### 3. Complete model competition without leakage

Extend `intraday_scanner/alpha/v6/training.py` and `validation.py` so the controlled gradient booster, when its predeclared `500 labels + 60 dates` gate is met, receives the same within-fold preprocessing, date-grouped purging, embargo, IPW, source eligibility, costs, and test dates as the regularized baseline. Never compare in-sample fit.

Predeclare a selection objective and tie-breaker before evaluation. Compare at minimum after-cost net excess expectancy, bootstrap lower bound, profit factor, drawdown, tail loss, concentration, turnover, capacity, calibration, interval coverage, rank lift, 1.5x/2x slippage, regime/source/liquidity/catalyst stability, and multiple-testing penalty. A complex model wins only with material, stable out-of-fold improvement; otherwise retain the simpler frozen model.

Persist family, hyperparameters, preprocessing, feature schema/hash, training cutoff, source/dataset/code hashes, fold receipts, random seed, library versions, objective, rejected alternatives, and artifact hash. Add negative controls proving shuffled labels and future/rank/outcome fields cannot produce a promotable artifact.

### 4. Build complete causal failure attribution

Replace the narrow setup/regime summary in:

- `intraday_scanner/services/v6_learning_service.py`
- `intraday_scanner/services/alpha_v6_research_service.py`
- `docs/research/alphaops_v6_failure_attribution.md`

Create one immutable categorical attribution contract across every available V4, V5, V6, sampled reject, and PaperOps decision/outcome. Categories must include data/source failure, source conflict, selection error/regret, entry timing/chase, liquidity/spread/capacity, stop/target logic, sizing/concentration, catalyst quality, regime mismatch, tail loss, and outcome/reconciliation failure.

For each category report eligible sample, missing/excluded counts, after-cost return distribution, benchmark excess, MFE/MAE, activation, stop/target/close path, confidence interval, source lineage, and whether evidence is descriptive or supports one prospective experiment. Never force one cause when evidence is insufficient. Produce machine-readable JSON plus concise Markdown and public-safe summaries.

### 5. Automate a point-in-time real universe

Implement a provider adapter under existing provider/service boundaries that builds a dated small-cap universe only from an approved real source. It must preserve retrieval time, dataset/source reference, raw artifact hash, ticker/listing history, delistings, symbol changes, market-cap/liquidity eligibility, and corporate actions. Unknown critical fields block membership.

Add CLI commands to fetch, validate, preview, register, diff, and roll back a universe snapshot. Registration must be atomic and idempotent. Never use `config/alpha_v6_universe.production.template.json` unchanged. If the user has not supplied an approved source/key, complete the adapter and tests with recorded fixtures, leave production registration blocked, and state the exact input needed.

### 6. Finish champion and benchmark comparison

Produce a date-aligned, cohort-aligned, cost-aligned objective comparison of V6 against frozen V5, cash, SPY, and IWM. Do not compare unlike coverage or silently drop losing/missing sessions. Publish cohort/period/denominator, outcome coverage, missing reasons, costs, benchmark source hashes, expectancy, cumulative return where supported, drawdown, profit factor, concentration, and bootstrap intervals.

Promotion criteria must consume this persisted comparison—not booleans supplied by a caller. Any missing champion or benchmark truth blocks the relevant gate. Add parity tests across DB, CLI, JSON, dashboard, and Telegram.

### 7. Finish untouched holdout operations

Add CLI/operator flow around the existing DB-enforced `alpha_v6_holdout_evaluations` contract. Require a frozen registered experiment, cutoff and holdout start validation, one-time confirmation, source/no-lookahead checks, artifact/config hashes, and an append-only receipt. A second evaluation of the same experiment must fail. Surface only status and safe evidence publicly; never expose holdout rows for iterative tuning.

### 8. Finish the operator product

Keep Calendar. Extend the existing framework-free UI—do not create X4 or another dashboard—with:

- Decision Replay source drill-down: raw-fact refs, feature schema/hash, model cutoff/artifact, costs, vetoes, score components, intervals, and decision state.
- Sampled-reject counterfactual result with explicit non-official status and IPW policy.
- Categorical failure attribution with sample/coverage warnings.
- Daily incident timeline linking source, scheduler, outcome, learning, preview, production, and notification receipts.
- Frozen V5 versus V6/cash/SPY/IWM comparison.
- Clear daily-monitor versus weekly-train freshness.

Probabilities, expected returns, and model comparison stay hidden when calibration, intervals, reconciliation, no-lookahead, or coverage gates fail. Preserve the corrected Calendar ARIA model and contrast. Verify keyboard operation, screen-reader names, progressive disclosure, and no horizontal overflow at 360x800, 390x844, 768x1024, 1280x720, and 1440x900.

### 9. Cut over safely

After all local gates pass:

1. Commit intentionally and push `codex/terra-alphaops-v6`.
2. Open a PR against current `origin/main`; resolve drift without weakening proof.
3. Merge only after CI and review gates pass.
4. Build a stable clean runtime from the exact merged SHA; never run scheduled tasks from a dirty checkout.
5. Import secrets from `C:\r\dawnstrike-state\secrets\runtime.env` without printing values.
6. Generate and validate real `C:\r\dawnstrike-runtime\config\web_sources.yaml` from user-supplied identity/provider choices.
7. Fetch/register the real dated universe.
8. Register canonical tasks with an interactively supplied password credential. Never store or print the password.
9. Run `scripts\disable_legacy_dawnstrike_tasks.ps1 -WhatIf`, inspect the exact five targets, then run it for real. It must back up XML and disable only; never delete.
10. Rehearse all stages copy-on-write, build a preview, verify it, and promote that exact deployment only when readiness is publishable.
11. Verify production aliases, source SHA, build ID, data hash, health, readiness, V6 JSON, Calendar, forbidden routes, logs, and rollback.
12. Observe one complete unattended market day through the final Telegram receipt. Preserve legacy rollback evidence for seven market days.

### 10. Proof gates

Run and report exact results:

```powershell
py -m pytest
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts app.py
node --check web/assets/dawnstrike.js
git diff --check
```

Also run PowerShell parser validation for every changed script, DB `quick_check`, migration/restart/idempotence tests, no-lookahead tests, source-conflict and missing-truth negative controls, weekly/daily cadence tests, model-family competition tests, holdout one-time tests, public secret/path scans, scheduler-doctor, web-source-doctor, performance reconciliation, public-artifact verification, rendered browser checks, Vercel preview/production inspection, and rollback rehearsal.

## Required stop conditions

- Stop before production promotion if source config, universe, scheduler identity, Vercel auth, publication readiness, exact-SHA binding, rollback, or browser proof is incomplete.
- Stop model competition when evidence gates are not met; record `WAITING_FOR_FORWARD_EVIDENCE` rather than lowering thresholds.
- Stop and preserve null when sources disagree or required truth is missing.
- Do not stop at “tests pass,” preview deploy, or health 200. Production requires readiness 200 and exact merged-SHA proof.
- Strategy success cannot be declared before at least 60 real forward sessions, 100 valid sourced closed paper labels, at least 98% eligible coverage, complete benchmark truth, positive after-cost and benchmark-relative evidence, risk/stress/holdout gates, and manual approval.

## Final response

Report only proven facts: commits, PR/merge SHA, files, tests, migrations, data/source configuration status, universe ID/hash, task definitions and results, model receipts and cutoffs, objective comparisons, preview/production deployment IDs and URLs, source/build/data hashes, browser/WCAG evidence, Telegram receipt, rollback proof, forward-evidence counts, and exact remaining external/time blockers. Never say “should work,” “profitable,” “best returns,” or “complete” without the named proof.

---
