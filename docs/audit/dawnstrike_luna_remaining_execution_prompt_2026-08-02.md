# LUNA EXECUTION PROMPT — CLOSE DAWNSTRIKE FOR REAL

You are the principal release, data-integrity, and research-systems engineer
for Dawnstrike. Execute this as one controlled program. Do not declare
success, profitability, improved returns, or production readiness without the
named proof. Preserve research-only/no-broker/no-LLM-scoring boundaries.

## Objective

Turn Dawnstrike into a trustworthy paper-research system whose Calendar,
return record, model research, notifications, source freshness, and deployment
all say exactly what is proven. The product is not complete until it is
operationally honest. Better returns are an evidence goal, never a promised
output.

## Known starting truth

- Production URL: `https://dawnstrike-command-center-x3.vercel.app`.
- Production health is 200 but it serves source SHA
  `692e785cf8304a8045e88ab221dc644d4eb2e9e7`; readiness is 503 with
  `safety_evidence_unverified`, `snapshot_not_publishable`, and
  `pipeline_not_ready`.
- The last production source watermark is 2026-07-30 and the EOD task last
  failed. Do not describe this as current performance.
- The durable DB is `C:\r\dawnstrike-state\shadow_real.sqlite`; a protected
  schema-13 backup already exists. Current candidate schema is 18.
- Real `config\web_sources.yaml`, approved provider entitlement, and a dated
  registered V6 universe are absent. Templates are not production data.
- This code already includes: daily/weekly V6 split, idempotent receipts,
  all-family OOF evidence, failure attribution, tagged one-time holdouts,
  strict universe preview/confirm/forward-restore, Calendar, public artifact
  scan, and CI/security controls. Preserve all of it.

## Immutable rules

1. Work in an isolated worktree from current `origin/main`; preserve unrelated
   user files. Record branch, HEAD, merge base, status, runtime SHA, DB hash/
   schema/quick-check/counts, Vercel deployment/build/SHA, task definitions,
   and source config presence before any write. Redact values, not key names.
2. No fake source rows, universe, fills, slippage, fees, benchmarks, labels,
   returns, browser proof, or Telegram receipt. Missing remains missing.
3. No broker execution, order placement, trade automation, personalized advice,
   or LLM recommendation/scoring logic.
4. No daily refit; no automatic model selection/promotion; no holdout reuse;
   no experiment-arm backfill; no release merely because health is 200.
5. A provider, entitlement, accountable contact, Windows password-logon
   identity, or production approval that has not been supplied is an external
   gate. State the exact missing input; do not invent it.

## Phase 0 — verify and protect

1. Validate the current repo and runtime independently. Verify the live
   health, readiness body, source SHA, build ID, source watermark, and task
   exit codes; do not use an old audit as current truth.
2. Create a timestamped, checksummed durable DB backup outside the runtime.
3. Copy the backup twice; migrate both copies to the candidate schema; run
   quick-check before/after/reopen/idempotent-init; restore a third copy; prove
   the protected backup hash is unchanged. Stop on any mismatch.
4. Run:

```powershell
py -m pytest
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
node --check web\assets\dawnstrike.js
py -m pip_audit -r requirements.lock
py -m bandit -r intraday_scanner scripts -ll -b config\security\bandit-baseline.json
```

Also parse every `scripts\*.ps1`, execute the secret baseline against tracked
files, generate a reproducible SBOM, and run `git diff --check`.

## Phase 1 — make input truth real

1. Ask only for the missing accountable inputs: provider and entitlement,
   accountable user-agent email, secure key location, approved universe terms,
   primary and independent price/outcome sources, and the Windows task identity.
2. Implement a provider-specific, terms-compliant point-in-time universe
   collector under existing service/provider boundaries. It must preserve raw
   artifact hash, source ID, retrieval time, config hash, source version,
   membership status, symbol changes, delistings, corporate actions, and
   liquidity/eligibility fields. Critical unknowns block membership.
3. Emit a candidate JSON with required `source_lineage`. Run:

```powershell
py -m intraday_scanner.cli alpha-v6-preview-universe --db-path <durable-db> --input <dated-universe.json>
```

Review additions, removals, status/action changes, and hash. Register only with
the exact returned hash:

```powershell
py -m intraday_scanner.cli alpha-v6-register-universe --db-path <durable-db> --input <dated-universe.json> --confirm-preview-hash <exact-hash>
```

Use `alpha-v6-restore-universe` only for an audited forward restore; never edit
history.
4. Configure real sources through secure environment/config boundaries. Run the
source doctor. A missing, degraded, stale, unsourced, or non-independent source
must fail the day closed.

## Phase 2 — make the algorithm learn honestly

1. Keep daily `alpha-v6-daily-monitor` limited to outcome/label/dataset/drift
   evidence. Keep `alpha-v6-train-weekly` to Monday/week-only refit and purged
   all-family OOF evaluation.
2. Compare only identical chronological folds. Keep regularized baseline as the
   active research family. Gradient challenger output is research evidence only.
3. Use failure attribution to form exactly one hypothesis per experiment:
   setup/regime, source quality, liquidity, catalyst, volatility, timing,
   costs, or exits. Register immutable baseline and candidate hashes before the
   prospective start. Tag arms at decision creation. Do not retag history.
4. Evaluate holdout exactly once with sourced complete outcomes and no
   look-ahead. Promotion remains blocked until all frozen gates pass and a human
   approval receipt is persisted.
5. Publish every metric with cohort, denominator, date range, costs, benchmark,
   source lineage, model/candidate version, and missing-data state. Calendar
   totals, paper returns, research returns, backtests, shadow results, cash,
   SPY, and IWM must remain separate.

## Phase 3 — operational and release closure

1. Commit the clean candidate. Copy that exact SHA to the runtime. Back up and
   migrate the durable DB only after Phase 0 proof passes.
2. Register/re-register all tasks from the exact runtime with the supplied
   password-logon identity. Verify morning, monitor, EOD, weekly training, and
   daily-finalize task XML, working directory, state root, last result, next
   run, retry, no overlap, and wake settings. Repair the existing EOD failure.
3. Run a dated copy-on-write rehearsal with notifications dry-run. Preserve
   native process receipts, hashes, stage rows, source health, universe receipt,
   outcomes, V6 receipts, Calendar, and public artifact verification.
4. Build a clean staged Vercel artifact. Require path/token/credential/private
   receipt/raw holdout scan, security-header parity, source/build/data hash
   binding, Calendar and Research browser proof, health 200, readiness 200,
   and a tested rollback before production promotion.
5. Pin CI actions by commit SHA. Keep the hash lock, SBOM, dependency audit,
   public DTO scan, Bandit no-regression gate, and secret baseline. Replace the
   30 reviewed legacy Bandit findings with real URL allowlists and SQL
   parameterization/identifier allowlists plus tests; do not refresh the
   baseline blindly.

## Stop conditions and final report

Stop and report the exact blocker if any prerequisite cannot be obtained or any
proof fails. Do not deploy a degraded artifact. Do not claim return improvement
until there are at least 60 forward sessions, 100 closed after-cost labels,
complete source/benchmark evidence, positive purged OOF, calibration/interval
proof, untouched holdout evidence, acceptable drawdown/concentration, and a
manual approval record.

Final report must contain only evidence: commit/SHA, migration report and
backup hashes, test/lint/type/security outputs, source/universe IDs and hashes,
task proof, daily/weekly receipt IDs, experiment/holdout receipts, Vercel
preview and production IDs/URLs, browser proof, Telegram receipt, rollback
proof, forward-evidence counts, and exact remaining external/time gates.
