# LUNA/TERRA EXECUTION PROMPT — MAKE DAWNSTRIKE PRODUCTION-TRUSTWORTHY

Copy everything below into Luna or Terra. This is an execution directive, not a request for another plan.

---

You are Dawnstrike's principal research-platform, quant-validation, data-integrity, product, security, and release engineer. Execute the remaining remediation end to end. Do not return a speculative plan, do not tune to the five known losses, and do not claim profitability. Continue through every immediately executable proof gate; stop only at a genuine credential, human-decision, market-time, or forward-sample boundary.

## Mission

Turn Dawnstrike into an exceptional, research-only, paper-audit platform that:

- rejects weak evidence instead of manufacturing picks;
- learns deterministically from sourced, reconciled outcomes and sampled rejects;
- optimizes a predeclared after-cost, benchmark-relative, risk-constrained objective;
- uses leak-free walk-forward and one-time holdout evidence;
- publishes one truthful product with Calendar, replay, incidents, and exact lineage;
- updates reliably every market day from one clean runtime and one durable database;
- never connects to a broker or places an order.

Good software cannot guarantee wealth. The engineering goal is better evidence, fewer preventable losses, and a valid prospective test of whether alpha exists.

## Authoritative state

- Work only in `C:\r\dawnstrike-terra-v6` on `codex/terra-alphaops-v6`.
- First read `AGENTS.md`, `docs/audit/dawnstrike_extreme_hardening_audit_2026-08-01.md`, and `docs/audit/terra_alphaops_v6_post_audit.md` completely.
- Preserve all current uncommitted/committed hardening unless a test proves a defect.
- Do not edit, clean, reset, merge into, deploy from, or migrate `C:\Users\MattFields\Dawnstrike`; it has 1,207 dirty paths.
- Durable state is `C:\r\dawnstrike-state`. Back it up before migration and verify the backup hash and SQLite `quick_check`.
- Current runtime is `C:\r\dawnstrike-runtime` at old SHA `692e785cf8304a8045e88ab221dc644d4eb2e9e7`.
- Production is `https://dawnstrike-command-center-x3.vercel.app`, deployment `dpl_Crdhxte5H7z8hkHxduEH8BDZ4Pjd`, old source SHA `692e785c`, build `f42ac827`, readiness HTTP 503.
- Live schema is 13; branch schema is 17. Never migrate the only DB without a verified backup and copy-on-write rehearsal.
- Never print secret values. Report key names/presence only.

## Non-negotiable invariants

- Research and paper audit only. No broker SDK mutation, order route, live execution, auto-sizing of real capital, or hidden execution toggle.
- No LLM-generated scores, targets, selections, policy changes, promotions, or recommendations.
- Missing, stale, disputed, future, or single-source critical truth is null/ineligible—not zero and not silently dropped.
- Separate raw observations, calculated features, model outputs, policy decisions, outcomes, and source lineage.
- Never fabricate production data, a universe, fills, costs, benchmark observations, labels, or readiness.
- V5 remains frozen. V6 is shadow-only until every promotion gate and explicit human approval pass.
- No random row split, same-row fit/evaluation, future feature, repeated holdout inspection, multiple-testing concealment, or retrospective threshold tuning on SLND/VIVK/VRRM/NUWE/XRX.
- Keep Calendar and the current framework-free product. Do not create X4, a second dashboard, a second database, or a second return engine.
- A failed required stage must fail closed and prevent production promotion. Health 200 is not readiness 200.
- Preserve `dawnstrike-alert-gate-v2.0.0`, the Calendar cohort fix, fallback parity fix, Vercel headers, stage-path containment, V6 model/holdout/source safeguards, and all their tests.

## Execute in this exact order

### Phase 0 — freeze evidence and isolate risk

1. Record branch, HEAD, merge base, `origin/main`, worktree status, remotes, Python/package versions, runtime SHA, production deployment/SHA/build/readiness, task XML/settings/results, DB path/hash/schema/quick-check/counts, config presence, universe presence, and environment key names.
2. Recompute the ten outcomes and five activated gross close returns from the durable DB. Preserve the exact cohort and denominator. Label them signal-level gross research outcomes, never account returns.
3. Replay every historical alert through gate v2 and persist a machine-readable audit report. Prove the five completed losses are blocked for evidence available at decision time only.
4. Snapshot/backup the live DB and active task XML. Record SHA-256 hashes. Rehearse all migrations and scripts against copies.
5. If any expected hardening file is missing or altered, stop and report the exact diff before proceeding.

### Phase 1 — make alerting impossible without complete evidence

Audit every path capable of creating Telegram ticker text, a watchlist, an official paper selection, or a trade-watch intent. There must be one shared predicate. At minimum require:

- gate status `PASS`/`ALERT_OK` and `manual_confirmation_required=false`;
- `can_alert=true` with blank `no_trade_reason`;
- source confidence at least 80 and at least two reconciled sources for critical facts;
- current verified ticker/listing identity and point-in-time universe membership;
- verified clear halt, SEC risk, corporate action, stale-data, and source-quality status;
- no source, price, volume, gap, ticker, or corporate-action conflict;
- complete previous close, float, premarket range, spread/liquidity, catalyst, entry, invalidation, and sourced target basis;
- data-quality score at least 75;
- explicit A/B setup grade, MEDIUM/HIGH edge, calibrated evidence, and no `INSUFFICIENT_SAMPLE` state;
- no manufactured risk-multiple target;
- acceptable gap, stop distance, spread, chase, reward/risk, capacity, concentration, and session-time bounds;
- V5 execution and portfolio policy pass before any official paper entry.

Manual confirmation, watch-only, fallback, uncalibrated, missing-history, and public-web-only rows may appear in research diagnostics but must never appear as alertable tickers or official paper candidates. A daily `NO CLEAN EDGE` receipt is valid and preferred.

Add route-level and end-to-end negative tests for every notifier/selection path, not only unit tests around `apply_alert_gate`.

### Phase 2 — build production-grade data truth

Implement under existing provider/service boundaries:

1. A licensed/approved point-in-time U.S. small-cap universe adapter with fetch, validate, preview, diff, atomic register, and rollback commands.
2. Dated raw artifacts and hashes; retrieval timestamps; provider/dataset references; ticker/listing history; symbol changes; delistings; corporate actions; market cap and liquidity eligibility.
3. Primary and independent price/outcome reconciliation. Use Alpaca only according to the account's actual IEX/SIP entitlement; use an independent source for confirmation. A conflict remains ineligible.
4. Automated SEC risk, halt, corporate action, catalyst/news lineage, spread, liquidity, and benchmark collection with provider health and freshness receipts.
5. Strict symbol normalization that quarantines headings, annotations, malformed symbols, OTC/non-common instruments, and unresolved mappings.
6. Bounded provider concurrency, retry/backoff, rate-limit receipts, circuit breakers, cache lineage, and stale-data rejection.

Do not use `config/alpha_v6_universe.production.template.json` as production data. If no approved universe provider exists, finish and test the adapter with recorded real-source-shaped fixtures, leave registration blocked, and state the exact provider/key/entitlement required.

### Phase 3 — split daily operation from weekly research

Refactor at minimum:

- `intraday_scanner/services/alpha_v6_learning_service.py`
- `intraday_scanner/services/alpha_v6_research_service.py`
- `intraday_scanner/cli.py`
- `scripts/run_alphaops_eod.ps1`
- `scripts/register_alphaops_tasks.ps1`

Create three explicit, idempotent modes:

- **Daily monitor:** capture/reconcile outcomes, append eligible labels, score only with the previously frozen artifact, update calibration/interval/drift/coverage evidence, and publish receipts. Never refit or select a model.
- **Weekly train:** freeze a cutoff/dataset hash, fit registered challengers, create purged/embargoed out-of-fold predictions, compare models, persist one deterministic frozen artifact and receipt, and never auto-promote.
- **Research packet:** read persisted evidence only. Never train, capture, attribute, or mutate policy.

Register a distinct weekly task with noninteractive password logon, start-when-available, WakeToRun, battery-safe settings, bounded retries, no overlap, native exit receipts, and independent failure attribution.

### Phase 4 — complete leak-free model competition

Fix `intraday_scanner/alpha/v6/training.py` and `validation.py` so every eligible family—including controlled gradient boosting—uses identical:

- date-grouped expanding walk-forward folds;
- purge and embargo;
- fold-local preprocessing and feature schema;
- source/outcome eligibility;
- inverse-probability weights for sampled rejects;
- cost/benchmark assumptions;
- test dates and evaluation metrics.

Never evaluate a model on its training predictions. Persist predictions by model family and fold.

Before results are computed, freeze a primary objective and tie-breaker. Primary objective: a conservative lower confidence bound of after-cost benchmark-excess expectancy. Constraints must include drawdown, tail loss/CVaR, profit factor, turnover, concentration, capacity, calibration, interval coverage, rank lift, 1.5x/2x slippage, regime/source/liquidity/catalyst stability, and multiple-testing penalty.

The complex model wins only with material, stable, out-of-fold improvement. Otherwise retain the simpler baseline. Persist family, parameters, preprocessing, feature list/hash, prohibited fields, dataset/source/code hashes, cutoff, folds, seed, library versions, objective, rejected alternatives, artifact hash, and serving receipt.

Add negative controls for shuffled labels, future fields, outcome fields, random-row leakage, schema mismatch, same-day scoring, and deliberately impossible alpha.

### Phase 5 — learn from failure without overfitting

Replace setup/regime-only attribution with an immutable cross-version contract over all available V4/V5/V6, PaperOps, and sampled-reject decisions. Categories:

- source/data failure or conflict;
- universe/identity/corporate-action error;
- selection error and sampled-reject regret;
- catalyst quality;
- regime mismatch;
- chase/entry timing;
- spread/liquidity/capacity;
- stop/invalidation geometry;
- target/exit logic;
- sizing/concentration in paper simulation;
- tail loss;
- outcome/reconciliation quality.

For each category report eligible/missing/excluded samples, coverage, activation, after-cost returns, benchmark excess, MFE/MAE, stop/target/close path, uncertainty, and source lineage. Allow `UNATTRIBUTED_INSUFFICIENT_EVIDENCE`; never force one cause.

Each change becomes a registered, one-variable, prospective experiment with frozen hypothesis, cutoff, eligible cohort, primary metric, guardrails, minimum sample, duration, multiple-testing family, stop rule, and untouched holdout. No automatic policy update.

Treat the five known losses as incident case studies, not a training target. Ask which decision-time defect was knowable; do not use later prices to invent a filter.

### Phase 6 — finish official performance truth

Build one account-level, date-aligned, cohort-aligned, cost-aligned comparison for frozen V5, V6 shadow, cash, SPY, and IWM. It must preserve:

- opening equity, flows, positions, fills, realized/unrealized P&L, fees, spread/slippage, and ending equity;
- explicit no-trade days versus missing days;
- outcome coverage and exclusion reasons;
- benchmark source/hash and same-session alignment;
- daily and compounded net return only where the denominator is complete;
- expectancy, drawdown, profit factor, turnover, concentration, capacity, and bootstrap intervals.

Never sum overlapping signal returns and call the result an account return. Never drop missing or losing sessions. Promotion must read this persisted comparison, not caller-supplied booleans.

### Phase 7 — expose the one-time holdout safely

Add a CLI/operator workflow around the existing immutable holdout contract. Require a frozen experiment, valid cutoff/validation/holdout ordering, artifact/config/source hashes, explicit confirmation, no-lookahead checks, and append-only receipt. A second evaluation for the same experiment must fail. Public UI may show status and safe aggregates only; it must not expose rows that enable repeated tuning.

### Phase 8 — make the product exceptional

Keep the current restrained visual language and Calendar. Add only evidence-driven product depth:

- Decision Replay with raw source references, feature schema/hash, model cutoff/artifact, score components, uncertainty, cost assumptions, vetoes, and final state;
- sampled-reject counterfactuals labeled non-official with inclusion probability/IPW policy;
- categorical failure attribution with sample/coverage warnings;
- incident timeline linking sources, scheduler, outcomes, learning, build, preview, promotion, and Telegram receipts;
- frozen V5/V6/cash/SPY/IWM comparison;
- distinct daily-monitor and weekly-training freshness;
- plain-language “why no alert” and “what evidence is missing.”

Hide probabilities, expected returns, and comparisons whenever calibration, interval, reconciliation, no-lookahead, source, benchmark, cost, or coverage gates fail. Missing filtered cohort rows must stay `Missing`, never inherit another cohort's status.

Verify keyboard behavior, focus, screen-reader names, reduced motion, contrast, touch targets, no horizontal overflow, loading/error/empty/stale states, and rendered output at 360x800, 390x844, 768x1024, 1280x720, and 1440x900.

### Phase 9 — complete engineering and security gates

1. Add CI for pytest, Ruff, mypy, compileall, JavaScript syntax, PowerShell parsing, DB migration/idempotence, public artifact verification, no-lookahead negative controls, and rendered regression tests.
2. Create a reproducible locked dependency set for supported Python, retain `pyproject.toml` as the declaration, and document deterministic update procedure.
3. Generate an SBOM and run automated dependency/license/secret scans. Fail CI on actionable high/critical vulnerabilities or leaked production-like secrets. Never print secret values.
4. Keep Vercel CSP/COOP/CORP/permissions/referrer/nosniff/frame controls and verify them on preview and production responses.
5. Scan public artifacts for absolute paths, tokens, credentials, private receipts, raw holdout rows, and non-public state.
6. Fuzz or property-test symbol normalization, missing numeric fields, conflicting sources, date boundaries, duplicate events, retry/idempotence, and path containment.
7. Ensure every destructive script resolves and proves an exact target inside an approved root before delete/move.

### Phase 10 — integrate and cut over

Only after local gates pass:

1. Commit intentionally and push `codex/terra-alphaops-v6`.
2. Open a PR against current `origin/main`; resolve drift without weakening gates.
3. Require green CI and review; merge normally.
4. Create `C:\r\dawnstrike-runtime` from the exact clean merged SHA. Do not copy a dirty tree.
5. Rehearse schema 13→17 against a copied DB twice; verify restart/idempotence and rollback. Then back up and migrate durable state once.
6. Build real `config/web_sources.yaml` using an accountable contact and approved sources. Import secrets from the private state/environment without displaying values.
7. Fetch, validate, and register the real dated universe.
8. Register morning, monitor, EOD, weekly, and finalize tasks using `$credential = Get-Credential`. The user types the Windows password only into the local credential dialog.
9. Back up and disable only the known legacy tasks; never delete them. Verify no enabled task points at the dirty checkout, sample CSV, or old runtime.
10. Run every stage copy-on-write, then one local rehearsal against durable configuration with notifications dry-run.
11. Build one Vercel preview from the exact merged SHA. Verify health, readiness, source/build/data hashes, V6 JSON, Calendar, security headers, forbidden routes, browser behavior, logs, and rollback.
12. Promote that exact verified deployment only if readiness is 200. Verify every production alias resolves to the same deployment/SHA.
13. Observe one complete unattended market day through final Telegram receipt. Preserve rollback evidence for seven market days.

Never deploy a degraded build merely to make the date look fresh. A truthful 503 is preferable to fabricated current data.

## Mandatory proof commands

Run and report exact output for:

```powershell
py -m pytest
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts app.py
node --check web/assets/dawnstrike.js
git diff --check
py -m pip check
```

Also prove:

- PowerShell parser success for every changed `.ps1`;
- SQLite `quick_check`, migration, restart, idempotence, backup hash, and rollback rehearsal;
- alert-gate replay and route-level notification negatives;
- source conflict, stale/missing truth, malformed ticker, and no-lookahead negatives;
- daily-versus-weekly cadence and idempotence;
- all-family purged walk-forward parity;
- shuffled-label/multiple-testing controls;
- one-time holdout rejection on second use;
- account/benchmark/cost reconciliation parity across DB, CLI, JSON, UI, and Telegram;
- public artifact path/secret/holdout scans;
- generated-stage path containment and security-header parity;
- scheduler doctor with zero failed tasks;
- rendered browser checks at all five viewports with zero actionable console/page/accessibility defects;
- preview and production exact deployment/SHA/build/data binding;
- rollback and one unattended daily receipt chain.

## Stop conditions

- Stop before durable migration without a verified backup and copy rehearsal.
- Stop before task registration if the user has not entered a Windows credential locally.
- Stop before real source/universe activation if accountable identity, approved provider, key, entitlement, or source terms are unresolved.
- Stop before promotion if readiness is not 200 or exact SHA/build/data/rollback/browser proof is incomplete.
- Stop model selection when minimum evidence is absent. Persist `WAITING_FOR_FORWARD_EVIDENCE`; never lower thresholds.
- Stop on source disagreement and preserve null/ineligible state.
- Stop after one holdout evaluation. Never reopen it for tuning.
- Never call the strategy profitable, state of the art, best, or complete before at least 60 real forward sessions, 100 valid sourced closed paper labels, 98% eligible coverage, complete cost/benchmark truth, positive conservative after-cost benchmark-excess evidence, risk/stress/holdout gates, and manual approval.

## Required final response

Report only proven artifacts and facts:

- commit(s), PR, merge SHA, runtime SHA;
- changed files and why;
- exact test/static/security results;
- DB backup/hash/schema/migration/rollback results;
- source config presence, provider identities/entitlements, universe ID/hash/member count;
- daily/weekly model receipts, cutoffs, features, folds, artifacts, objective comparisons, drift/calibration/holdout status;
- task definitions, principals, settings, latest exits, and native receipts;
- preview/production deployment IDs, URLs, source/build/data hashes, headers, readiness, Calendar/browser proof;
- Telegram receipt and unattended-day chain;
- forward sessions/labels/coverage and exact remaining external/time blockers.

Do not say “should work.” Do not confuse local code, preview, production, freshness, readiness, or return evidence.

---
