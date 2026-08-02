# LUNA EXECUTION PROMPT — FINISH DAWNSTRIKE WITHOUT INVENTING RETURNS

You own the final controlled completion of Dawnstrike. Start in
`C:\r\dawnstrike-terra-v6` on branch `codex/terra-alphaops-v6`. Treat commit
`13087069` and `docs/audit/dawnstrike_final_hardening_audit_2026-08-02.md` as
the local baseline. Never modify the dirty original checkout
`C:\Users\MattFields\Dawnstrike`.

## Non-negotiable constraints

- Dawnstrike is research-only and paper-audit only. Never add broker execution,
  order placement, automated trading, personalized investment advice, or LLM
  recommendation logic.
- Missing truth stays missing. Never convert a missing source, benchmark,
  account, fee, slippage, outcome, delivery, or provider entitlement into zero,
  an estimate, a fixture, or a claim.
- Do not claim improved returns, profitability, readiness, delivery, or a live
  source until the exact evidence below exists.
- Do not deploy, promote, migrate the durable DB, register tasks, or modify
  Vercel until the explicit relevant gate is satisfied. Never capture a
  password, key, token, or secret in logs or chat.

## Phase 0 — re-verify before mutation

1. Record branch, HEAD, dirty status, runtime SHA, public health/readiness,
   durable DB SHA/schema/quick-check, current scheduler configuration, source
   config presence, and public build hashes.
2. Make a new timestamped protected DB backup outside runtime. Migrate two
   copies only; initialize twice; verify quick-check, schema, legacy counts,
   and backup hash. Do not touch the live DB.
3. Run and preserve receipts for:

```powershell
py -m pytest
py -m ruff check .
py -m mypy intraday_scanner
py -m compileall -q intraday_scanner scripts
node --check web\assets\dawnstrike.js
py -m pip_audit -r requirements.lock
py -m bandit -r intraday_scanner scripts -ll -f json -o build\bandit-current.json
```

Also parse all PowerShell scripts, run the tracked-file secret scan, build an
SBOM, run `alpha-alert-replay` read-only, and hash every produced receipt.
Stop on any failure.

## Phase 1 — source and universe truth (external approval gate)

Require all of these before enabling collection or writing a universe:

- Named approved primary and independent outcome/benchmark providers, including
  SPY and IWM coverage.
- Approved point-in-time US common-stock universe provider, terms reference,
  entitlement reference, and accountable contact email.
- Secure runtime-only key/config location; never commit `web_sources.yaml` or
  a credential.
- A dated raw artifact with identity/ticker history, active/delisted listing
  truth, corporate actions, OTC/common-stock/US truth, market cap, 20-day
  dollar volume, and source reference per member.

Build, preview, and register only with the current hardened commands:

```powershell
py -m intraday_scanner.cli alpha-v6-build-universe --source-contract <contract.json> --raw-artifact <raw-artifact.json> --out <candidate.json>
py -m intraday_scanner.cli alpha-v6-preview-universe --db-path <durable-db> --input <candidate.json>
py -m intraday_scanner.cli alpha-v6-register-universe --db-path <durable-db> --input <candidate.json> --source-contract <contract.json> --raw-artifact <raw-artifact.json> --confirm-preview-hash <exact-preview-hash>
```

The build must produce `READY_FOR_PREVIEW`; a hand-edited candidate, raw list,
stale hash, source mismatch, or `BLOCKED_EXTERNAL_APPROVAL` is a stop. Preserve
the contract/raw/candidate/preview hashes and the immutable registered version.

## Phase 2 — run truth and learning discipline

1. Keep daily EOD work label/dataset/drift-only with idempotent receipts.
2. Keep V6 refitting Monday-only through `Dawnstrike AlphaOps V6 Weekly
   Training`; it must share the daily lock and the scheduler doctor must verify
   it alongside morning, monitor, EOD, and finalization.
3. Keep all families on date-grouped, expanding, purged/embargoed OOF folds;
   use the same eligible data and negative controls. Do not promote a challenger
   automatically.
4. Run one immutable tagged experiment at a time, tag decision arms only at
   creation, and evaluate its holdout exactly once after its frozen date.
5. Run `alpha-attribution --paper-ops-root <bounded-root>` after EOD. Preserve
   V4/V5/V6/sampled-reject/PaperOps semantic boundaries; never pool daily
   aggregate PaperOps P&L with trade P&L.

## Phase 3 — operational cutover (interactive-authority gate)

Only after Phases 0–2 are green, copy the exact committed SHA to the runtime,
make another backup/rehearsal, then obtain the approved Windows credential in a
local interactive prompt:

```powershell
scripts\register_alphaops_tasks.ps1 -RunAsCredential (Get-Credential) -ReplaceExisting
scripts\register_daily_finalize_task.ps1 -RunAsCredential (Get-Credential) -ReplaceExisting
```

Run `scheduler-doctor`. Require password/service logon, start-when-available,
battery-safe settings, correct runtime/state roots, all five task receipts, no
legacy root, and a dated full-chain dry run. A missing credential or failed
task is a stop—not a reason to weaken the doctor.

## Phase 4 — publication and product proof

Build a staged Vercel preview from the exact runtime SHA. Before requesting
promotion, prove:

- public-artifact/private-data scan passes;
- snapshot, calendar, manifest, readiness, and headers bind to the exact
  source/data/build hashes;
- Calendar, Research, Decision Replay, and System render correctly in a real
  browser with keyboard-accessible calendar filters;
- health is 200, readiness is 200, and the readiness body names no failed gate;
- Telegram dry-run/delivery receipt and rollback procedure are verified.

Never promote a readiness-503 or degraded artifact.

## Final stop conditions and report

Stop at any absent approval, source failure, source gap, universe rejection,
test/security failure, migration mismatch, task failure, browser failure,
readiness failure, or forward-evidence shortfall. Do not patch around it.

Return only the exact commit/runtime/deployment IDs; backup/migration/source/
candidate/preview hashes; task/dry-run/public-artifact/browser/Telegram/
rollback receipts; full gate outputs; remaining blockers; forward sample
counts; and the next exact command. State explicitly that performance remains
unproven until at least 60 forward sessions, 100 closed after-cost labels,
complete source/benchmark coverage, positive purged OOF, calibration and
interval proof, untouched holdout, acceptable drawdown/concentration, and
recorded manual approval all exist.
