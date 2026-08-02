# TERRA EXECUTION PROMPT — CLOSE DAWNSTRIKE WITHOUT INVENTING PERFORMANCE

You are the principal release, data-integrity, and research-systems engineer
for Dawnstrike. Execute this as one controlled program. Do not claim
profitability, improved returns, production readiness, current source data, or
successful Telegram delivery without the named evidence.

## Scope and non-negotiable rules

- Work only in `C:\r\dawnstrike-terra-v6` on `codex/terra-alphaops-v6`.
  Never modify, migrate, schedule, deploy from, or clean
  `C:\Users\MattFields\Dawnstrike`.
- Preserve research-only operation. No broker connections, orders, execution,
  automated trading, personalized investment advice, or LLM recommendation
  logic.
- Missing source, fee, slippage, benchmark, outcome, account, or delivery truth
  remains missing. Never replace it with zero, a fixture, a template, an
  estimate, or a retrospective claim.
- Do not daily-refit, auto-select/promote a model, reuse a holdout, backfill an
  experiment arm, or deploy a readiness-503 artifact.
- Do not print secrets. Redact values, not environment-key names.

## Current verified baseline

- Candidate HEAD: `cd1acded13c223a82655939103963ebbbc4d966d`.
- Runtime and public production currently serve old SHA
  `692e785cf8304a8045e88ab221dc644d4eb2e9e7`.
- Production is alive but `/api/readiness` is HTTP 503 with
  `safety_evidence_unverified`, `snapshot_not_publishable`, and
  `pipeline_not_ready`.
- Durable DB: `C:\r\dawnstrike-state\shadow_real.sqlite`, SHA-256
  `5B65C7DC823806F1570015F6F4922E4B1FA73D2B2A84BE2097A14F611D3FFF3F`,
  schema 13, quick-check `ok`, 10 outcomes, 254 signals, 109 notifications,
  and 16 paper fills.
- Fresh protected backup:
  `C:\r\dawnstrike-state\migration-backups\phase0-20260802T040839Z\shadow_real.sqlite`.
  Current schema-18 migration is copy-on-write, additive, and idempotent.
- Alert replay artifact:
  `C:\r\dawnstrike-state\repro\phase0-20260802-alpha-alert-replay.json`.
  It proves 66 legacy stored alert flags disagree with current policy and all
  five sourced close losses would be blocked decision-time only.
- No real `config\web_sources.yaml`, no source-backed V6 universe, and no
  validated primary/independent provider contract exist.
- Scheduler registration code is correct but live tasks are interactive,
  battery-unsafe, and three latest runs failed. A password-logon identity is
  required before changing them.
- Bandit has 0 high and 30 medium baseline findings: 13 B310 and 17 B608.

## Phase 0 — freeze and verify before every mutation

1. Read `AGENTS.md`, this prompt, and
   `docs/audit/dawnstrike_extreme_hardening_audit_2026-08-02.md` completely.
2. Record branch, HEAD, merge base, dirty status, runtime SHA, durable DB hash,
   schema version, quick-check, required table counts, source-config presence,
   task XML hashes/settings/results, public health/readiness body, deployment
   build/source SHA, and current time.
3. Create a new timestamped DB backup outside runtime. Copy it twice; migrate
   both copies; run quick-check before/after/reopen; run initialize twice;
   verify retained legacy counts and schema 18; prove the protected backup hash
   did not change. Stop on a mismatch.
4. Run `alpha-alert-replay` against the durable DB and preserve the output hash.
   A replayed decision must not access outcomes until after gate evaluation.
5. Run all gates:

```powershell
py -m pytest
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
node --check web\assets\dawnstrike.js
py -m pip_audit -r requirements.lock
py -m bandit -r intraday_scanner scripts -ll -f json -o build\bandit-current.json
git diff --check
```

Also parse every PowerShell script, execute the tracked-file secret scan,
generate the SBOM, and save all receipts/hashes.

## Phase 1 — remove known legacy security exposure

1. Replace every B310 call with a tested HTTPS URL validator that rejects
   non-HTTPS schemes, credentials in URLs, unapproved hosts, redirects to
   unapproved hosts, and uncontrolled user-agent/header construction. Permit
   only explicit domains for Alpaca, SEC, Nasdaq, Yahoo, configured approved
   providers, and Telegram. Apply the same rule to the two notification scripts.
2. Replace every B608 dynamic identifier interpolation with a narrow,
   centralized identifier allowlist. Keep values parameterized. For `IN` lists,
   generate placeholders from a bounded list and test empty, malformed, and
   hostile values. Never accept a table/order/column identifier from a request,
   config, database payload, or CLI argument without allowlist resolution.
3. Add regression tests for every rejection path. Shrink the Bandit baseline;
   never add a new ignore merely to make a scan green.
4. Re-run Bandit. Report the exact residual count and reason if any legitimate
   finding cannot be eliminated safely in this pass.

## Phase 2 — source and universe truth

Do not fabricate this phase. If provider terms, entitlement, accountable email,
or universe approval is missing, prepare the validated adapter/config contract
and stop with the exact required input.

When approved inputs exist:

1. Build provider-specific, terms-compliant point-in-time collectors under the
   existing provider/service boundaries. Persist raw artifact hash, source ID,
   retrieval time, config hash, source version, listing status, ticker changes,
   delistings, corporate actions, liquidity, and all eligibility fields.
   Missing critical fields block a member.
2. Configure real `config\web_sources.yaml` only in the secure runtime boundary.
   Require an accountable user-agent contact, primary plus independent quote/
   outcome providers, SPY/IWM coverage, and an approved universe version.
3. Generate the dated universe JSON, then preview and register only with the
   exact preview hash:

```powershell
py -m intraday_scanner.cli alpha-v6-preview-universe --db-path <durable-db> --input <dated-universe.json>
py -m intraday_scanner.cli alpha-v6-register-universe --db-path <durable-db> --input <dated-universe.json> --confirm-preview-hash <exact-hash>
```

4. Run the source doctor. Any stale, missing, degraded, unsourced, or
non-independent critical input closes the day and blocks publication.

## Phase 3 — learning and return truth

1. Keep `alpha-v6-daily-monitor` to sourced outcome/label/dataset/drift
   evidence and idempotent receipts. Keep `alpha-v6-train-weekly` to Monday
   refit and chronological purged all-family OOF evidence.
2. Keep the regularized baseline active for research. Treat challengers as
   evidence only. Use failure attribution to propose exactly one immutable,
   prospective hypothesis at a time.
3. Register baseline/candidate hashes before the start date. Tag arms only at
   decision creation. Evaluate each frozen holdout once and never promote
   automatically.
4. Keep Calendar/account returns, research outcomes, backtests, shadow output,
   cash, SPY, and IWM separate. Every display needs cohort, date range,
   denominator, costs, benchmark, source lineage, model version, and missing
   data state.

## Phase 4 — operational cutover and release

Only after Phases 0–3 pass:

1. Commit the intentional candidate. Copy that exact SHA to
   `C:\r\dawnstrike-runtime`. Perform a fresh backup/rehearsal before the one
   authorized live migration.
2. With the approved credential supplied interactively, use
   `scripts\register_alphaops_tasks.ps1 -RunAsCredential (Get-Credential) -ReplaceExisting`
   and `scripts\register_daily_finalize_task.ps1 -RunAsCredential (Get-Credential) -ReplaceExisting`.
   Do not capture or log the password. Confirm password logon, battery-safe,
   wake, retry, no-overlap, exact runner/runtime/state root, and successful
   morning/monitor/EOD/weekly/finalize receipts with `scheduler-doctor`.
3. Run a dated copy-on-write chain with Telegram dry-run. Preserve native
   process receipts, source health, universe receipt, outcome labels, V6
   receipts, Calendar, and public-artifact verification.
4. Build a staged Vercel preview. Require clean artifact/private-data scans,
   headers, hash binding, Calendar/Research/System browser proof, health 200,
   readiness 200, and tested rollback. Promote only after all of those pass.

## Stop conditions and final report

Stop immediately at any missing provider approval/entitlement, accountable
email, universe terms, password-logon identity, source truth, failed test,
failed migration rehearsal, readiness failure, or forward-evidence gate. Do
not deploy a degraded artifact.

The final report contains only: commit/SHA, backup/migration hashes, command
results, residual security findings, source/universe IDs and hashes, task proof,
daily/weekly receipts, experiment/holdout evidence, Vercel preview/production
IDs, browser proof, Telegram receipt, rollback proof, forward sample counts,
and exact external/time gates. Do not claim strategy improvement until 60
forward sessions, 100 closed after-cost labels, complete source/benchmark
coverage, positive purged OOF, calibration/interval proof, untouched holdout,
acceptable drawdown/concentration, and a manual approval record exist.
