# LUNA EXECUTION DIRECTIVE — DAWNSTRIKE 10/10 REMEDIATION

Date: 2026-07-29  
Repository: `C:\Users\MattFields\Dawnstrike`  
Status: controlling remediation directive; implementation has not started  
Production target: `https://dawnstrike-command-center-x3.vercel.app`

## 1. Role

You are Luna, the bounded implementer, verifier, evidence recorder, and release
operator for this remediation. You are not the product strategist.

Execute the ordered phases below. Make reasonable implementation decisions
inside the stated architecture and invariants. Do not replace this directive
with a new roadmap, create another Command Center generation, or stop at an
analysis-only report.

Continue until:

1. every implementation phase that can be completed now is implemented and
   verified;
2. a preview candidate is proven end to end;
3. the first production promotion is either explicitly approved and verified,
   or is correctly stopped at its approval gate; and
4. any time-dependent strategy-evidence gate is operating daily and is reported
   as `WAITING_FOR_FORWARD_EVIDENCE`, never falsely marked complete.

## 2. Controlling goal

Turn Dawnstrike from a sprawling research artifact collection into one calm,
truthful, paper-only research product with:

- one canonical performance ledger;
- one published return methodology;
- one responsive dashboard;
- one reproducible deployment artifact;
- one fail-closed daily pipeline;
- one evidence-based strategy promotion process; and
- no broker execution.

The finished product must answer these questions immediately:

1. What did Dawnstrike select today?
2. What actually entered the paper portfolio?
3. What happened, after fees and slippage?
4. How has the official paper portfolio performed against its benchmark?
5. How complete and trustworthy is the evidence?
6. Is the system fresh and operational?

No honest implementation can guarantee profitable returns. “10/10 returns”
means 10/10 measurement, evidence, risk discipline, and research process.
Profitability must be earned through forward evidence. If no strategy qualifies,
the correct outcome is “No strategy validated,” not relaxed gates or invented
confidence.

## 3. Authority and source hierarchy

For this remediation, use this hierarchy:

1. This directive.
2. `AGENTS.md`.
3. Current mounted runtime behavior and persisted evidence.
4. Existing tests and architecture that do not conflict with items 1–3.
5. Older roadmaps and audit artifacts as historical context only.

`docs/roadmap/dawnstrike_v2_execution_plan.md` is not the controlling plan for
this remediation. Do not execute its broad expansion roadmap. The immediate
goal is consolidation and deletion of product ambiguity, not more subsystems.

## 4. Revalidate before changing anything

The audit baseline was:

- branch: `codex/command-center-x3-vercel`;
- local HEAD: `67f02726c915aad7ce5a857567d3d3fcc1b0bf98`;
- `origin/main`: `268b4946bc149f00ec6cc7fd31a040be3baa86a6`;
- dirty paths: approximately 1,207;
- `app.py`: approximately 7,829 lines;
- database: `data/shadow_real.sqlite`;
- production readiness: HTTP 500 from a missing
  `intraday_scanner.public_data` module;
- focused X3 tests: four passed despite the production failure;
- current paper positions: seven closed trades, $7,000 summed notional,
  -$459.6706 realized P&L, and 28.57% winners;
- probability status: uncalibrated;
- current scheduler status: not ready because the EOD task returned code 1;
- daily Vercel publication continued after that EOD failure.

These facts are evidence, not timeless assumptions. Revalidate them and record
the new baseline.

## 5. Non-negotiable invariants

### Product and truth

- Missing truth remains missing. Never coerce it to zero.
- Keep these cohorts visibly and computationally separate:
  - official forward paper portfolio;
  - AlphaOps signal research and scenario outcomes;
  - historical backtests;
  - shadow challengers.
- Never call backtest rows “paper rows,” “official paper evidence,” or current
  trades.
- Never display a return without its cohort, period, denominator, methodology,
  fee/slippage treatment, sample size, coverage, and as-of timestamp.
- The dashboard must consume canonical read models. It must not independently
  recompute return truth.
- Telegram, dashboard, CSV/JSON exports, and CLI reports must use the same
  canonical identifiers and metrics.

### Research and trading safety

- Preserve research-only and paper-only operation.
- Do not add broker execution, order placement, trade automation, or a live
  order endpoint.
- Do not use an LLM to generate financial recommendations, scores, targets,
  entries, exits, or strategy promotion decisions.
- Do not claim personalized investment advice.
- Do not claim a strategy is validated without the forward-evidence gates in
  this directive.
- Keep missing outcomes ineligible for learning.

### Repository and release safety

- Preserve the dirty shared checkout.
- Work in a clean isolated worktree created from the current `origin/main`.
- Do not use `git reset --hard`, broad checkout/revert commands, force-push, or
  broad recursive deletion.
- Do not copy the dirty checkout wholesale into the remediation worktree.
- Port a dirty/untracked file only after documenting its source, diff, purpose,
  and tests in a transfer manifest.
- Do not weaken or rewrite a failing test merely to make a gate pass.
- Do not create `Command Center X4`, `X5`, another Apex, another dashboard
  generation, or a parallel return engine.
- Do not commit generated databases, screenshots, deployment staging trees,
  caches, or large daily output bundles.

### External and production actions

- Preview deployment is allowed only after local gates pass.
- Before the first production promotion, show the user:
  - exact candidate SHA;
  - exact preview URL and deployment ID;
  - exact public fields and derived data being exposed;
  - health/readiness proof;
  - data hash and as-of date;
  - rollback target and command.
- Obtain explicit approval before the first production promotion and before
  materially expanding the class of published operational data.
- After the schema and daily publication scope are explicitly approved, the
  approved daily job may continue automatically within that exact scope.
- Do not delete or permanently disable a legacy path until the new production
  path has passed its rollback window and the user approves destructive cleanup.

## 6. Allowed status vocabulary

Use only these completion states:

- `NOT_STARTED`
- `IN_PROGRESS`
- `LOCAL_VERIFIED`
- `PREVIEW_VERIFIED`
- `PRODUCTION_VERIFIED`
- `WAITING_FOR_FORWARD_EVIDENCE`
- `BLOCKED_APPROVAL_REQUIRED`
- `BLOCKED_EXTERNAL`
- `FAILED`

Do not describe a category as 10/10, done, shipped, or production-ready unless
all of its objective gates below have passed.

## 7. Objective 10/10 scorecard

### 7.1 UI and product design: 10/10 gate

All must pass:

- One canonical web product, with no competing deployed/local information
  architecture.
- Four primary sections maximum:
  - Overview
  - Performance
  - Research
  - System
- The first desktop viewport shows:
  - latest market date and freshness;
  - official portfolio daily and cumulative return;
  - benchmark and excess return;
  - dollar P&L;
  - drawdown;
  - open positions;
  - evidence coverage;
  - current system state.
- No historical backtest trade appears in the current paper portfolio.
- No more than five primary navigation items.
- No unexplained internal terms such as OMEGA, artifact-linked, provenance rail,
  shadow-only-not-validated, or quality score.
- Responsive proof at 360×800, 390×844, 768×1024, 1280×720, and 1440×900.
- Zero horizontal page overflow at every target viewport.
- No clipped tables, calendar columns, nav items, status text, or controls.
- Keyboard navigation works with visible focus.
- Semantic headings, table headers, labels, status text, and color-independent
  meaning meet WCAG 2.2 AA expectations.
- No console errors, failed network requests, or uncaught promises.
- Initial static UI payload is at most 500 KB compressed, excluding the data
  snapshot.
- The public data snapshot is at most 250 KB compressed and never ships all
  9,329 historical trade rows.
- Rendered screenshot tests and semantic tests both pass.

### 7.2 Return reporting: 10/10 gate

All must pass:

- One canonical performance service owns all displayed calculations.
- Official paper, AlphaOps research, and backtest returns have different typed
  cohort identifiers and cannot be combined accidentally.
- Opening equity + net P&L = ending equity within $0.01.
- Position P&L equals the sum of fills within $0.01.
- Daily portfolio return uses opening portfolio equity, not summed trade
  notionals.
- Cumulative return compounds canonical daily returns.
- Fees and slippage are explicit and included in net results.
- Realized and unrealized P&L are separate.
- Benchmark observations align to the same market dates and return policy.
- Excess return is portfolio return minus benchmark return for the same period.
- Missing outcomes never enter averages, win rates, equity curves, or learning
  labels.
- Every metric includes source references, calculation version, input hash,
  generated timestamp, market date, coverage, and evidence state.
- Historical seven-trade paper evidence reconciles exactly to the source rows.
- Existing PaperOps fleet rows reconcile or are explicitly quarantined with an
  exact discrepancy report.
- Dashboard, Telegram, CLI, CSV, and JSON values agree to their documented
  display precision.
- A deterministic rebuild from the same inputs produces byte-identical
  canonical JSON after excluding the generated timestamp.

### 7.3 Strategy evidence and return-improvement process: 10/10 gate

The implementation and daily evidence system can be completed now. Strategy
validation itself is time-dependent.

All must pass before any strategy is called validated:

- At least 60 real market days and at least 100 closed forward paper trades for
  the exact strategy, version, cohort, and execution policy.
- At least 98% eligible outcome coverage.
- Positive net expectancy after fees and conservative slippage.
- Profit factor of at least 1.20.
- Positive cumulative excess return against both cash and the registered market
  benchmark.
- Maximum forward drawdown no worse than 8%.
- No single trade contributes more than 25% of total gains or total losses.
- Walk-forward and untouched holdout results are positive after costs.
- Slippage stress remains positive at 1.5× the observed conservative slippage.
- No source, ticker, one-day, one-month, or one-setup concentration explains the
  result.
- The strategy survives an explicit no-lookahead audit.
- Model probabilities are not shown as calibrated unless the calibration sample
  and quality gates pass. Until then they remain “Uncalibrated” and are not used
  as a promotion claim.
- Promotion is a deterministic policy decision with a versioned evidence
  manifest, never a UI button or LLM opinion.

If these gates have not matured, report
`WAITING_FOR_FORWARD_EVIDENCE`. That is a correct 10/10 research process.

### 7.4 Deployment reliability: 10/10 gate

All must pass:

- Deployment source is a clean, exact, recorded Git SHA.
- The deploy artifact contains a manifest with source SHA, public data hash,
  build ID, market date, generated timestamp, and file hashes.
- Static HTML/CSS/JS/JSON is served natively from Vercel’s static output, not
  through `api/ui.py`.
- The public deployment excludes Streamlit, pandas, NumPy, PyArrow, scanner
  engines, local SQLite, and Telegram send code.
- `/api/health` is a liveness endpoint and never claims readiness.
- `/api/readiness` returns 200 only when snapshot integrity, required fields,
  freshness, and approved pipeline state pass; otherwise it returns 503 with
  precise reasons.
- No deployed route raises an import error.
- Preview is built once, verified, and that exact deployment is promoted; do
  not rebuild separately for production.
- Rollback to the prior production deployment is documented and rehearsed.
- Production alias, deployment ID, source SHA, build ID, and data hash all
  match.
- Production browser, console, network, health, readiness, and data checks pass.
- No deployment is created from a dirty source tree or a mixture of `HEAD` and
  untracked runtime code.
- No daily data failure can silently publish a “passed” state.

### 7.5 Safety and trust boundaries: 10/10 gate

All must pass:

- No broker adapter capable of placing an order is enabled or reachable.
- No public/admin endpoint can send a Telegram, mutate research state, or run
  the scanner from the Vercel deployment.
- Secrets are read only from environment configuration, never published,
  logged, or embedded in snapshots.
- Public snapshots pass secret and path scans.
- Public source quality, halt status, SEC/corporate-action status, and
  liquidity evidence are explicit.
- Unknown critical safety evidence fails closed.
- All published language says research-only/paper-only and avoids personalized
  advice.
- Missing or degraded evidence is visible in the top-level UI.
- Audit records are append-only or immutable-by-identity and every overwrite is
  rejected or versioned.
- Safety tests include negative controls.

### 7.6 Daily operations and freshness: 10/10 gate

All must pass:

- Exactly one scheduled finalize/publish chain owns daily publication.
- Morning, monitor, EOD, outcome capture, reconciliation, snapshot generation,
  preview verification, and promotion have explicit stage manifests.
- Every stage is idempotent for a market date and protected by a lock.
- A failed upstream stage cannot produce a green downstream state.
- By 18:30 America/Chicago on each market day, production reflects that day as
  one of:
  - complete;
  - explicit no-trade;
  - degraded with returns pending and exact missing evidence.
- A degraded snapshot may publish only with null unavailable returns and a
  visible degraded state. It must never fabricate zero or retain a green
  readiness badge.
- Outcome capture retries are bounded and recorded.
- A later successful reconciliation replaces the degraded snapshot with a new
  version for the same market date.
- Success and failure notifications include market date, stage, build ID,
  coverage, deployment URL, and next action.
- Scheduler doctor, public readiness, and latest manifest agree.

## 8. Phase 0 — Contain, isolate, and baseline

### Required actions

1. Inspect:

   ```powershell
   git status --short
   git branch --show-current
   git rev-parse HEAD
   git fetch origin --prune
   git rev-parse origin/main
   git worktree list --porcelain
   ```

2. Record exact output in:

   `docs/remediation/dawnstrike-10of10/baseline.md`

3. Create a clean isolated worktree from current `origin/main`, using a unique
   `codex/` branch. Preferred shape:

   ```powershell
   git worktree add C:\r\dawnstrike-10of10-20260729 -b codex/dawnstrike-10of10 origin/main
   ```

   If either path or branch exists, choose a unique suffix. Do not reuse an
   unrelated worktree.

4. Inventory the current dirty implementation candidates, especially:

   - `intraday_scanner/public_data/`
   - `scripts/publish_x3_daily.ps1`
   - `api/`
   - `vercel_dawnstrike/`
   - `vercel.json`
   - `intraday_scanner/v2/command_center_x3/`
   - `intraday_scanner/storage/migrations.py`
   - `intraday_scanner/storage/sqlite_store.py`
   - current scheduler scripts.

5. Create:

   `docs/remediation/dawnstrike-10of10/dirty-transfer-manifest.md`

   For each ported file, record:

   - source path;
   - source checkout SHA;
   - tracked/untracked state;
   - summary of diff;
   - why it is needed;
   - tests that prove it;
   - whether it changes published data or runtime behavior.

6. Export the exact Windows Scheduled Task definition for the current X3 daily
   publisher. Disable only that daily publisher after recording its exact name,
   action, schedule, last result, and restoration command. Do not disable
   morning, monitor, or EOD research tasks.

7. Capture the current production deployment ID, alias, source metadata,
   health/readiness responses, and rollback target.

### Phase 0 stop conditions

- Stop and report if another active lane owns any target file and safe isolation
  is not possible.
- Stop before disabling any task if its exact identity and restore command are
  not proven.
- Stop before porting a dirty file whose behavior cannot be explained.

### Phase 0 exit gate

- Clean worktree proven.
- Baseline artifact written.
- Dirty-transfer manifest started.
- Broken daily publisher safely contained and reversible.
- Existing production left available as rollback reference.

## 9. Phase 1 — Establish one canonical performance truth

### Architecture

Use the existing schema and services as raw evidence. Do not build a second
independent ledger.

Treat these as raw or transitional inputs:

- `paper_positions`
- `paper_trade_fills`
- `signal_outcomes`
- `signal_return_attribution`
- `daily_signal_performance`
- `strategy_evaluations`
- `strategy_paper_trades`
- `daily_strategy_scorecards`
- `benchmark_observations`
- `benchmark_performance`
- `data/v2_paper_ops_live/ledger/paper_ledger.jsonl`
- `data/v2_paper_ops_live/calendar/strategy_daily_returns.csv`

Add one canonical read-model layer above them.

### Required files

Add:

- `intraday_scanner/performance/__init__.py`
- `intraday_scanner/performance/contracts.py`
- `intraday_scanner/services/canonical_performance_service.py`
- `intraday_scanner/services/public_snapshot_service.py`
- `docs/architecture/performance_truth_contract.md`
- `tests/test_canonical_performance_contracts.py`
- `tests/test_canonical_performance_reconciliation.py`
- `tests/test_public_performance_snapshot.py`

Update:

- `intraday_scanner/storage/migrations.py`
- `intraday_scanner/storage/sqlite_store.py`
- `intraday_scanner/services/alpha_paper_reconciliation_service.py`
- `intraday_scanner/services/return_attribution_service.py`
- `intraday_scanner/services/performance_service.py`
- `intraday_scanner/alpha/performance_truth.py`
- `intraday_scanner/v2/paper_ops/calendar_truth.py`
- `intraday_scanner/dashboard/data_loader.py`
- `intraday_scanner/dashboard/operator_models.py`
- `intraday_scanner/cli.py`

### Required contracts

Define typed, versioned contracts for:

- `PerformanceCohort`
  - `official_forward_paper`
  - `alphaops_signal_research`
  - `historical_backtest`
  - `shadow_challenger`
- `EvidenceState`
  - `complete`
  - `no_trade`
  - `pending`
  - `degraded`
  - `missing`
  - `not_eligible`
- `ReturnMethodology`
- `DailyPortfolioPerformance`
- `TradePerformance`
- `BenchmarkPerformance`
- `EvidenceCoverage`
- `CanonicalPerformanceSnapshot`
- `PublicSnapshotManifest`

Every public metric must carry:

- cohort;
- strategy/version where applicable;
- execution-policy version;
- market date and timezone;
- opening and ending equity;
- realized P&L;
- unrealized P&L;
- fees;
- slippage;
- net P&L;
- daily return;
- cumulative return;
- benchmark return;
- excess return;
- drawdown;
- exposure;
- trade count;
- win/loss/flat counts;
- evidence coverage;
- missing count;
- calculation version;
- source references;
- input hash;
- generated timestamp.

### Additive migration

Add migration version 5 or the next free version. At minimum add:

- `portfolio_daily_performance`
- `public_snapshot_manifests`

`portfolio_daily_performance` must be unique by market date, portfolio/cohort,
strategy version where relevant, and execution-policy version.

Do not destructively rewrite historical raw tables. Backfill into canonical
tables with provenance and deterministic IDs.

### Required CLI

Add:

```powershell
py -m intraday_scanner.cli performance-reconcile `
  --db-path data/shadow_real.sqlite `
  --paper-ops-root data/v2_paper_ops_live `
  --as-of YYYY-MM-DD `
  --print
```

It must:

- run read-only by default;
- require an explicit `--persist` flag to write;
- report all cohort discrepancies;
- emit nonzero status on unexplained reconciliation differences;
- never infer missing data as zero.

### Reconciliation proof

Create a sanitized golden fixture representing:

- one winning closed trade;
- one losing closed trade;
- one no-trade day;
- one pending outcome;
- one incomplete source row;
- one benchmark observation;
- fees and slippage;
- one backtest row that must remain excluded from paper totals.

Prove:

- the current seven closed paper positions reconcile to -$459.6706;
- their summed notional statistic remains labeled as a diagnostic, not a
  portfolio return;
- the official portfolio return uses portfolio equity;
- July 29 AlphaOps signal research, SKYQ paper fill, PaperOps fleet return, and
  historical backtest values remain separate;
- no 9,329-row historical corpus is loaded into the current paper book.

### Phase 1 exit gate

- Canonical contracts and migration tests pass.
- Raw-to-canonical reconciliation is deterministic.
- Every current discrepancy is explained or blocks the phase.
- Dashboard and publication code can consume one snapshot without recomputing
  returns.

## 10. Phase 2 — Make outcomes and daily truth reliable

### Required files

Update:

- `intraday_scanner/services/alpha_outcome_capture_service.py`
- `intraday_scanner/services/alpha_paper_reconciliation_service.py`
- `intraday_scanner/services/return_attribution_service.py`
- `intraday_scanner/services/calendar_report_service.py`
- `intraday_scanner/services/trade_watcher_service.py`
- `intraday_scanner/storage/sqlite_store.py`
- `scripts/run_alphaops_eod_full.bat`
- `scripts/register_alphaops_tasks.ps1`

Add:

- `intraday_scanner/services/daily_finalize_service.py`
- `tests/test_daily_finalize_service.py`
- `tests/test_outcome_completeness_gate.py`
- `tests/test_daily_pipeline_idempotency.py`

### Required daily stage manifest

For each market date record:

1. source collection;
2. candidate normalization;
3. selection;
4. delivery;
5. paper fills;
6. outcome capture;
7. paper reconciliation;
8. canonical performance;
9. public snapshot;
10. preview deployment;
11. production promotion.

Each stage must include:

- stage version;
- input hashes;
- output hashes;
- start/end timestamps;
- status;
- attempt count;
- warnings;
- exact error;
- next action.

### Required behavior

- Use deterministic market-date idempotency keys.
- Reject duplicate fills and duplicate outcomes.
- Preserve immutable source lineage.
- Retry transient outcome-source failures with bounded backoff.
- Distinguish:
  - not triggered;
  - triggered but not filled;
  - filled and open;
  - closed with sourced outcome;
  - unresolved;
  - invalid source;
  - explicit no-trade.
- Block learning on unresolved or unsourced outcomes.
- Do not block an honest degraded public status snapshot.
- A degraded public snapshot must set unavailable returns to null and list
  exact missing rows.
- Replace a degraded snapshot only through a new immutable manifest version.

### Data-quality gates

For a candidate to become eligible for an official paper entry, require:

- timestamped source;
- source-quality label;
- current price and reference price;
- verifiable session status;
- halt-status evidence;
- SEC/corporate-action risk evidence;
- liquidity evidence;
- spread/slippage estimate;
- deterministic entry, invalidation, and target levels.

Unknown critical evidence is a hard block, not a warning.

### Phase 2 exit gate

- A full fixture market day runs twice with byte-equivalent logical output.
- Missing outcomes produce degraded/null truth, never zero.
- Complete outcomes produce canonical performance.
- Learning remains blocked until eligibility passes.
- EOD failure cannot be hidden by a later publisher success.

## 11. Phase 3 — Repair the strategy and risk process

Do not tune against the current seven trades. Implement policy changes in shadow
mode first and compare them against the frozen current policy.

### Required files

Update:

- `intraday_scanner/alpha/alert_gate.py`
- `intraday_scanner/alpha/risk_governor.py`
- `intraday_scanner/alpha/edge_calibrator.py`
- `intraday_scanner/alpha/calibration.py`
- `intraday_scanner/alpha/truth_guard.py`
- `intraday_scanner/alpha/selection_diagnostics.py`
- `intraday_scanner/services/alpha_cycle_service.py`
- `intraday_scanner/services/trade_watcher_service.py`
- `intraday_scanner/v2/paper_ops/strategy_evidence.py`
- `intraday_scanner/v2/paper_ops/challenger_evaluation.py`

Add:

- `intraday_scanner/alpha/promotion_policy.py`
- `intraday_scanner/alpha/execution_policy.py`
- `tests/test_alpha_execution_policy.py`
- `tests/test_strategy_promotion_policy.py`
- `tests/test_alpha_tail_risk_controls.py`
- `tests/test_alpha_data_quality_blocks.py`

### Immediate risk controls

Add versioned, configurable controls with conservative defaults:

- No new same-day paper entry within 30 minutes of mandatory EOD flattening.
- No position whose expected holding window extends beyond the available
  session.
- Position size from stop distance and portfolio risk, not fixed $1,000
  notional alone.
- Maximum 0.25% portfolio risk per position.
- Maximum 1.00% portfolio loss per day.
- Maximum 10% portfolio notional in one ticker.
- Maximum two correlated open positions unless a tested portfolio policy says
  otherwise.
- Hard block for unknown halt status.
- Hard block for unresolved reverse-split/corporate-action risk.
- Hard block for unknown critical SEC risk when the setup depends on a catalyst.
- Hard block when conservative spread plus slippage destroys required edge.
- Hard block when required source fields are missing or conflicting.
- Kill the current day’s new entries after the daily loss limit is hit.
- Keep forced EOD flattening, but prevent last-minute entries that make it
  self-defeating.

These values are initial safety limits, not profitability claims. Version them
and test them in shadow mode before changing the official cohort.

### Required research process

For every active strategy/version:

- produce walk-forward splits;
- preserve one untouched holdout;
- test fee and slippage sensitivity;
- test liquidity and spread buckets;
- test source buckets;
- test price, float, catalyst, halt, reverse-split, and time-of-day buckets;
- report concentration by ticker, day, month, setup, source, and outlier;
- report benchmark-relative returns;
- report no-trade days;
- report missing-data exclusions;
- quarantine negative-expectancy or tail-dependent variants;
- never promote a challenger from the UI.

### Probability policy

- Continue showing `Uncalibrated` until the sample and calibration gates pass.
- Do not convert deterministic scores into probability language.
- Require a documented sample threshold, Brier score versus baseline, expected
  calibration error, and holdout reliability before showing calibrated
  probability.
- If calibration remains insufficient, omit probability from the decision card
  rather than presenting a decorative percentage.

### Phase 3 exit gate

- Current policy and remediated policy run side by side in shadow mode.
- Late-entry SKYQ-type behavior is blocked by a regression test.
- BIYA-type tail loss is bounded by portfolio-risk tests.
- Promotion policy emits `WAITING_FOR_FORWARD_EVIDENCE` until all evidence
  thresholds mature.
- No strategy is falsely labeled validated.

## 12. Phase 4 — Build one product surface

### Architecture decision

Build one framework-free static web application backed by the canonical public
snapshot. Do not add React, Next.js, another UI framework, or another generated
Command Center.

Use:

- `web/index.html`
- `web/assets/dawnstrike.css`
- `web/assets/dawnstrike.js`
- accessible semantic HTML;
- lightweight native SVG charts;
- generated canonical JSON;
- one build directory: `build/public/`.

Add:

- `web/index.html`
- `web/assets/dawnstrike.css`
- `web/assets/dawnstrike.js`
- `scripts/build_public_dashboard.ps1`
- `scripts/open_public_dashboard.ps1`
- `tests/test_public_dashboard_contract.py`
- `tests/test_public_dashboard_semantics.py`

Update:

- `intraday_scanner/services/public_snapshot_service.py`
- `scripts/render_dashboard_qa.py`
- `intraday_scanner/dashboard/render_qa.py`
- `.gitignore`

### Product information architecture

#### Overview

Above the fold:

- product status: complete, no-trade, degraded, stale, or offline;
- market date and last successful update;
- official daily return;
- official cumulative return;
- benchmark return;
- excess return;
- net dollar P&L;
- drawdown;
- open paper positions;
- evidence coverage.

Then:

- today’s official selections;
- blocked/no-trade explanation;
- open and closed paper positions;
- “What changed today?” plain-language summary.

#### Performance

- official equity curve;
- benchmark curve;
- excess-return curve;
- drawdown curve;
- daily and cumulative table;
- realized versus unrealized P&L;
- fees and slippage;
- trade ledger;
- strategy/version and methodology;
- sample size and evidence coverage.

#### Research

Separate panels for:

- AlphaOps signal research;
- uncalibrated probability status;
- forward strategy evidence;
- historical backtests;
- shadow challengers.

Backtests must be labeled historical and cannot affect current paper totals.
Default to summary cards and paginated detail. Do not ship thousands of raw rows.

#### System

- data freshness;
- stage manifest;
- scheduler state;
- outcome coverage;
- source quality;
- latest build SHA;
- latest data hash;
- health versus readiness;
- exact failure reason and next action.

### Language rules

Use plain language:

- “Paper portfolio” instead of “PaperOps” in primary UI.
- “Research signals” instead of “AlphaOps” in primary UI.
- “System checks” instead of “OMEGA.”
- “Data incomplete” instead of “artifact-linked.”
- “Not validated” instead of “shadow_only_not_validated.”

Technical IDs may appear in an expandable details panel.

### Local/deployed parity

The exact `build/public` artifact must be:

- served locally by `scripts/open_public_dashboard.ps1`;
- used by browser QA;
- used by Vercel preview;
- promoted unchanged to production.

Do not maintain a separate Streamlit information architecture. During
transition, keep `app.py` available only as a legacy operator fallback. After
parity and rollback gates pass, remove it from the default launch path in a
separate, approved cleanup.

### Browser acceptance

At every target viewport:

```javascript
document.documentElement.scrollWidth <= window.innerWidth
```

Also prove:

- all four sections load;
- no console errors;
- no failed requests;
- keyboard focus is visible;
- chart values match source JSON;
- missing values render `Pending`, `N/A`, or an exact degraded message;
- historical rows never appear in current paper cards;
- system status changes when fixture readiness changes.

### Phase 4 exit gate

- One UI artifact passes semantic and rendered QA.
- Desktop and mobile screenshots are saved in the evidence packet.
- Old X3 is no longer the candidate production surface.
- No self-awarded “quality score 100” remains.

## 13. Phase 5 — Replace the Vercel deployment architecture

### Required architecture

The Vercel project is a read-only publication surface.

It must not:

- run the local scanner;
- run Telegram delivery;
- mutate the local research database;
- host an ephemeral duplicate research state;
- serve static assets through a Python function.

Remove from the production deployment:

- `api/ui.py`;
- `/api/scanner`;
- `/api/telegram`;
- Vercel scanner/Telegram cron routes;
- full `intraday_scanner` runtime packaging;
- `vercel_dawnstrike/runtime.py` as a production dependency.

Retain or replace only:

- a tiny `/api/health` liveness endpoint;
- a tiny `/api/readiness` endpoint that reads the generated public manifest;
- static UI and JSON.

The deployment staging root must include only approved public files, minimal
health/readiness code, and Vercel configuration. It must not include the full
repository, SQLite database, logs, private outputs, or secrets.

### Required files

Update:

- `vercel.json`
- `api/health.py`
- `api/readiness.py`

Add:

- `scripts/build_vercel_public_stage.ps1`
- `scripts/verify_vercel_candidate.ps1`
- `tests/test_vercel_public_stage.py`
- `tests/test_vercel_health_readiness.py`
- `docs/operations/public_dashboard_deployment.md`
- `docs/operations/public_dashboard_rollback.md`

Retire from the deployment stage:

- `api/ui.py`
- `api/scanner.py`
- `api/telegram.py`
- `api/cron_morning.py`
- `api/cron_after_close.py`

Do not delete those files from the repository until call-site, rollback, and
user-approval gates pass.

### Static serving

Configure Vercel to serve the static output directly from the configured output
directory. The official Vercel project configuration supports
`outputDirectory`; use `public` in the minimal deployment stage or another
single documented static directory.

Reference:

- `https://vercel.com/docs/project-configuration/vercel-json#outputdirectory`
- `https://vercel.com/docs/builds/configure-a-build#output-directory`

Remove all UI rewrites to `api/ui`.

### Health contract

`GET /api/health`:

- returns 200 if the minimal publication service can respond;
- returns `status: alive`, not `ready` or `passed`;
- includes deployment/build ID and source SHA;
- does not import scanner modules;
- does not inspect private/local runtime state.

### Readiness contract

`GET /api/readiness`:

- validates public manifest schema;
- validates required file hashes;
- validates source SHA/build ID/data hash consistency;
- validates market-date freshness using America/Chicago and the market calendar;
- validates publication state;
- returns 200 only for `complete` or explicit `no_trade`;
- returns 503 for degraded, stale, corrupt, missing, or mismatched state;
- includes exact failed checks;
- uses only the Python standard library or another dependency-free minimal
  runtime.

The UI must use readiness, not health, for its status badge.

### Build/deploy flow

Pin the Vercel CLI version in one configuration location. Use:

1. build the static product and manifest;
2. build the minimal deployment stage;
3. run local secret/path/size/hash checks;
4. run browser QA against that stage;
5. run `vercel build --prod`;
6. deploy once with `vercel deploy --prebuilt`;
7. verify the preview;
8. obtain production approval;
9. promote the exact verified deployment with `vercel promote`;
10. verify the production alias.

Vercel recommends separating build from deploy with a prebuilt artifact and
promoting the already-verified deployment:

- `https://vercel.com/docs/deployments`
- `https://vercel.com/docs/cli`

### Phase 5 exit gate

- Static assets are CDN-served without `api/ui`.
- Health/readiness have distinct semantics.
- Readiness returns controlled 503 responses, never import tracebacks.
- Minimal function artifacts contain no heavy Python dependencies.
- Preview deployment matches exact SHA and data hash.
- Rollback command and target are proven.

## 14. Phase 6 — Make daily updates reliable

### Scheduling architecture

Keep research execution local because the operating SQLite database and source
collection are local. Do not pretend an ephemeral Vercel function is the same
system.

Retain:

- AlphaOps morning task;
- five-minute monitor task;
- EOD research/report task.

Replace the independent X3 publisher with one finalize/publish task that depends
on current-day stage truth.

Preferred schedule:

- EOD research/report: existing post-close schedule;
- daily finalize/publish: 17:30 America/Chicago on market days;
- bounded follow-up retry: no later than 18:30 America/Chicago.

Use the repository market calendar. Do not run weekend/holiday “market day”
publication as though it were new evidence.

### Required files

Add:

- `scripts/run_daily_finalize.ps1`
- `scripts/register_daily_finalize_task.ps1`
- `scripts/restore_previous_publish_task.ps1`
- `tests/test_daily_publish_gate.py`
- `tests/test_daily_publish_degraded_state.py`
- `tests/test_daily_publish_task_registration.py`

Update:

- `scripts/register_alphaops_tasks.ps1`
- `intraday_scanner/services/scheduler_doctor_service.py`
- `intraday_scanner/cli.py`

### Required finalize flow

1. Acquire a market-date lock.
2. Validate clean release source SHA.
3. Read the EOD stage result.
4. Retry outcome capture when failures are transient.
5. Reconcile paper fills and outcomes.
6. Build canonical performance.
7. Classify current day:
   - complete;
   - explicit no-trade;
   - degraded/pending.
8. Build an immutable public snapshot.
9. Validate schema, hashes, secrets, paths, sizes, and public-data policy.
10. Build the minimal Vercel stage.
11. Run local browser QA.
12. Build and deploy a preview.
13. Verify preview health, readiness, UI, console, network, SHA, and data hash.
14. Promote only when allowed by the standing publication approval.
15. Verify production.
16. Send success or failure notification.
17. Release the lock and preserve the stage manifest.

### Degraded-day rule

Daily freshness is more important than pretending incomplete returns are final.

If outcomes remain incomplete by the deadline:

- publish a degraded snapshot only if the public schema has already been
  approved;
- show the current market date;
- show exact missing counts and tickers where approved;
- set unavailable performance values to null;
- return readiness 503;
- show “Returns pending — evidence incomplete” above the fold;
- schedule a later reconciliation attempt;
- never display the prior day’s return as if it belongs to today.

### Cron/security note

Remove Vercel research crons unless Dawnstrike is deliberately migrated to a
durable hosted datastore in a separately approved project. If any Vercel cron
remains, it must verify `Authorization: Bearer <CRON_SECRET>`. Vercel cron
invocations are production-only, use UTC schedules, and do not automatically
retry failed invocations:

- `https://vercel.com/docs/cron-jobs`
- `https://vercel.com/docs/cron-jobs/manage-cron-jobs`

### Phase 6 exit gate

- Exactly one daily publisher is enabled.
- A fixture complete day publishes green.
- A fixture no-trade day publishes green with no-trade semantics.
- A fixture missing-outcome day publishes degraded/null truth and readiness
  503.
- Re-running the same day is idempotent.
- A failed EOD stage cannot publish a green state.
- Scheduler doctor, task state, manifest, and public readiness agree.

## 15. Phase 7 — Replace superficial QA with product QA

### Remove false assurance

Retire the use of:

- viewport metadata as proof of mobile quality;
- string existence as proof of semantic truth;
- self-awarded quality score 100;
- build completion as proof of readiness;
- deployment `READY` as proof that runtime and data are correct.

Update:

- `intraday_scanner/v2/command_center_x3/qa.py`
- `tests/test_v2_command_center_x3.py`
- `tests/test_command_center_x3_official_fleet_truth.py`

During transition, make these tests clearly legacy. After cutover, remove them
from release gating only when replacement tests cover all relevant behavior.

### Required test matrix

#### Unit

- return formulas;
- compounding;
- drawdown;
- benchmark/excess;
- fees/slippage;
- missing values;
- cohort separation;
- position sizing;
- daily loss limits;
- late-entry block;
- promotion policy;
- manifest hashing;
- freshness.

#### Integration

- DB raw rows → canonical snapshot;
- PaperOps ledger → canonical snapshot;
- AlphaOps selection → paper fill → outcome → daily performance;
- degraded outcome → null public return;
- canonical snapshot → UI;
- stage manifest → readiness.

#### Browser

- all target viewports;
- no overflow;
- no clipping;
- keyboard navigation;
- no console errors;
- no network errors;
- charts match JSON;
- backtest/paper separation;
- degraded and stale states;
- offline/error rendering.

#### Deployment

- minimal stage allowlist;
- secret scan;
- absolute/local path scan;
- bundle size;
- source SHA;
- data hash;
- preview health;
- preview readiness;
- production alias;
- rollback.

### Required commands

At minimum run:

```powershell
py -m pip install -e ".[dev,browser]"
py -m pytest
py -m ruff check .
py -m mypy intraday_scanner
py -m intraday_scanner.cli probability-doctor --db-path data/shadow_real.sqlite --print
py -m intraday_scanner.cli scheduler-doctor --root . --print
py -m intraday_scanner.cli dashboard-doctor --db-path data/shadow_real.sqlite --print
git diff --check
```

Also run all new focused suites and the rendered browser QA command.

If a global gate fails for a clearly unrelated pre-existing reason, preserve the
exact failure and distinguish it from scoped success. Do not rewrite unrelated
tests.

### Phase 7 exit gate

- All scoped tests pass.
- Full suite, Ruff, and mypy results are recorded honestly.
- Rendered proof exists for all target viewports and states.
- Release score is a gate matrix with evidence, not a decorative number.

## 16. Phase 8 — Preview, cut over, and preserve rollback

### Preview packet

Before requesting production approval, provide:

- branch and exact SHA;
- clean worktree proof;
- changed-file list;
- migration version;
- canonical reconciliation report;
- public schema and example;
- public fields/data classification;
- local screenshots;
- preview URL and deployment ID;
- preview source SHA;
- preview build ID;
- preview data hash;
- preview health/readiness responses;
- console/network result;
- performance/bundle size result;
- exact production promotion command;
- exact rollback target and command;
- known limitations.

### Production gate

After explicit approval:

1. Promote the exact preview deployment. Do not rebuild.
2. Verify the production alias.
3. Confirm source SHA, build ID, and data hash.
4. Check `/api/health`.
5. Check `/api/readiness`.
6. Open Overview, Performance, Research, and System in a clean browser session.
7. Check desktop and mobile.
8. Check console and network.
9. Check Vercel error logs.
10. Trigger one safe read-only daily-publication rehearsal.
11. Record rollback readiness.

### Rollback window

- Keep the prior production deployment addressable.
- Keep the legacy local operator fallback available.
- Run a minimum seven-market-day rollback window.
- Do not delete legacy dashboard source or data during this window.
- If a critical truth, readiness, security, or availability defect appears,
  roll back immediately and preserve evidence.

### Legacy retirement candidates

After the rollback window, create a separate approval packet for:

- `data/v2_command_center/`
- `data/v2_command_center_x/`
- `data/v2_command_center_x2/`
- `data/v2_command_center_x3/`
- `data/v2_interface_apex/`
- dormant Command Center source packages;
- `api/ui.py`;
- Vercel scanner/Telegram/cron routes;
- the old daily X3 publisher;
- default Streamlit launch paths no longer needed.

Use call-site scans, tests, deployment manifests, and a recoverable archive/tag.
Do not perform broad deletion under this directive without that approval.

## 17. Phase 9 — Evidence maturation

After technical production verification:

- collect forward paper evidence every market day;
- publish coverage and sample sufficiency;
- keep strategies unvalidated until thresholds pass;
- run weekly strategy evidence reports;
- quarantine strategies that breach risk or evidence thresholds;
- do not tune repeatedly on the same forward sample;
- preserve frozen policy versions and challenger comparisons.

The remediation may be:

- UI: `PRODUCTION_VERIFIED`
- Return reporting: `PRODUCTION_VERIFIED`
- Deployment: `PRODUCTION_VERIFIED`
- Safety: `PRODUCTION_VERIFIED`
- Daily operations: `PRODUCTION_VERIFIED`
- Strategy validation: `WAITING_FOR_FORWARD_EVIDENCE`

This is the only honest near-term closure shape unless sufficient forward
evidence already exists and independently passes every gate.

## 18. Commit and delivery strategy

Use small, reviewable commits:

1. baseline and contracts;
2. storage migration and canonical performance;
3. outcomes/finalize pipeline;
4. risk and promotion policy;
5. canonical UI;
6. Vercel static/minimal runtime;
7. scheduler/publisher;
8. QA and documentation;
9. release evidence.

Before each commit:

- inspect `git status --short`;
- ensure no unrelated files are included;
- run the phase’s focused tests;
- run `git diff --check`.

Do not push or open a PR until local gates pass. Do not promote production until
the production approval gate passes.

## 19. Required final evidence packet

Create:

- `docs/remediation/dawnstrike-10of10/baseline.md`
- `docs/remediation/dawnstrike-10of10/dirty-transfer-manifest.md`
- `docs/remediation/dawnstrike-10of10/performance-reconciliation.md`
- `docs/remediation/dawnstrike-10of10/strategy-evidence-status.md`
- `docs/remediation/dawnstrike-10of10/ui-verification.md`
- `docs/remediation/dawnstrike-10of10/deployment-verification.md`
- `docs/remediation/dawnstrike-10of10/daily-operations-verification.md`
- `docs/remediation/dawnstrike-10of10/rollback-rehearsal.md`
- `docs/remediation/dawnstrike-10of10/final-scorecard.md`

The final scorecard must list each objective gate as:

- PASS with evidence;
- FAIL with exact failure;
- BLOCKED with exact external/approval blocker;
- WAITING with the required future evidence.

## 20. Reporting contract

At the end of every phase report:

- outcome;
- files changed;
- tests run and exact results;
- reconciliation or runtime evidence;
- current category statuses;
- remaining blockers;
- next phase.

Do not say “should work.” Say what was proven.

If blocked:

- continue safe, non-overlapping phases where possible;
- do not broaden authority;
- do not invent credentials or approval;
- stop at the exact external or destructive boundary;
- provide the exact command or user decision needed to resume.

## 21. Final definition of done

Dawnstrike is only fully remediated when:

- one canonical public product is live;
- all return surfaces agree by contract;
- historical backtests cannot masquerade as paper performance;
- mobile and desktop rendered QA pass;
- production readiness is truthful;
- daily publication is idempotent and fail closed;
- deployed artifacts are clean-SHA, minimal, hashed, and reproducible;
- no Vercel endpoint can mutate research or send Telegram;
- no broker execution exists;
- rollback is proven;
- strategy status remains evidence-based;
- every 10/10 score is backed by the objective evidence matrix.

Anything less must remain explicitly partial.
