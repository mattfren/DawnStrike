# Dawnstrike final hardening audit — 2026-08-02

## Verdict

The local candidate is materially hardened but is **not ready for production
promotion** and cannot honestly claim improved returns. Its next requirement is
real, approved, forward evidence—not more retrospective tuning.

Candidate code is recorded at `13087069` on `codex/terra-alphaops-v6`. It is
isolated from the old runtime and public deployment; neither was changed.

## What the evidence says about returns

The only sourced historical triggered-close cohort remains five observations:
`-1.3438%`, `-20.2778%`, `-10.8696%`, `-24.8305%`, and `-0.2173%` gross close
return. Its mean is `-11.5078%`. This is a signal-level, pre-cost cohort—not a
paper-account return, a benchmark-relative result, or proof of a new strategy.

The alert replay control found 66 legacy records marked alertable that current
decision-time policy rejects; each of the five sourced losses is among those
rejected records. That establishes a historical filter defect, not future
profitability.

## Local gaps closed in this candidate

| Area | Hardened behavior | Proof boundary |
| --- | --- | --- |
| V6 point-in-time universe | `alpha-v6-build-universe` materializes only a hashed raw artifact matched to a source contract. It quarantines incomplete, OTC, unresolved, non-common, invalid-liquidity, and unresolved-corporate-action records. | No provider is approved, so no production candidate has been built or registered. |
| Universe registration | Preview requires a deterministic candidate; registration re-materializes it from the exact contract and raw artifact and refuses a mismatch before a DB write. It still requires the current preview hash. | The Python domain API remains trusted in-process code; the operator CLI is the enforced production boundary. |
| Daily/weekly learning | EOD runs label-only V6 monitoring. A separate Monday task runs the only model refit path. Weekly training now takes the same daily state lock. | Live task registration still fails its external identity/settings gate. |
| Scheduler audit/rollback | The doctor now verifies the weekly V6 task as a first-class required DAG member; task restore includes it. | Existing live tasks are old runtime tasks and were not modified. |
| Causal attribution | `alpha-attribution` reports distinct V4, V5, V6, sampled V6-reject, and PaperOps streams. It exposes source, selection, catalyst, liquidity, entry, exit, and concentration categories without pooling incompatible P&L units or filling missing values with zero. | PaperOps remains a daily aggregate, explicitly not a trade-level return series. |
| Calendar/product truth | Calendar, research, decision replay, public-artifact safety, and rendered-dashboard contract coverage remain in the test suite. | No new Vercel preview or browser run was performed in this pass. |

## Local verification completed

```text
Focused V6/scheduler/attribution/product test set: 44 passed
py -m pytest -q: exit 0, reached [100%]
py -m ruff check <changed paths>: All checks passed
py -m mypy intraday_scanner: Success, 209 source files
py -m compileall -q intraday_scanner scripts: success
PowerShell parse of every scripts/*.ps1: success
py -m pip_audit -r requirements.lock: No known vulnerabilities found
raw Bandit (-ll): 0 findings
tracked-file detect-secrets scan: passed
CycloneDX SBOM SHA-256: 08D8EE71630110F24345A940886B6332AA94F97E847611C8F506B7807F351828
```

Bandit emits informational messages for existing justified `# nosec B608`
annotations; its raw results array is empty. The full-suite console result is
also preserved in `build/full-suite-hardening-20260802.stdout.log`.

## Exact gates still outside this checkout

1. **Approved source truth.** Provide the named primary and independent price/
   outcome sources, point-in-time US common-stock universe provider, terms
   acceptance reference, entitlement reference, accountable contact email, and
   secure source configuration/key locations. Provider presence in an
   environment is not entitlement proof.
2. **A dated source artifact.** The approved provider must produce the exact
   dated raw artifact with ticker identity/history, listing/delisting and
   corporate-action status, US/common-stock/OTC truth, market cap, 20-day dollar
   volume, and per-record source references. Missing critical truth stays
   rejected.
3. **Unattended task authority.** An approved Windows password-logon identity
   must be entered interactively. It needs network, encrypted-secret,
   `C:\r\dawnstrike-state`, Telegram, and Vercel access. Do not put the
   password in a file, terminal history, or chat.
4. **Runtime/release proof.** Copy the exact committed candidate to the runtime
   only after a fresh DB backup and copy-on-write migration rehearsal. Then
   verify all five tasks, one dated full chain, staged public artifacts, Vercel
   preview, browser surfaces, headers, health, readiness, and rollback.
5. **Forward performance evidence.** Require at least 60 sessions, 100 closed
   after-cost labels, complete source/benchmark coverage, positive purged OOF,
   calibration/interval evidence, one untouched holdout, acceptable drawdown
   and concentration, and recorded manual approval. Time is a real gate.

## Correct production universe sequence

```powershell
py -m intraday_scanner.cli alpha-v6-build-universe `
  --source-contract C:\r\dawnstrike-state\source-universe\alpha_v6_source-contract.json `
  --raw-artifact C:\r\dawnstrike-state\source-universe\alpha_v6_raw-YYYY-MM-DD.json `
  --out C:\r\dawnstrike-state\source-universe\alpha_v6_candidate-YYYY-MM-DD.json

py -m intraday_scanner.cli alpha-v6-preview-universe `
  --db-path C:\r\dawnstrike-state\shadow_real.sqlite `
  --input C:\r\dawnstrike-state\source-universe\alpha_v6_candidate-YYYY-MM-DD.json

py -m intraday_scanner.cli alpha-v6-register-universe `
  --db-path C:\r\dawnstrike-state\shadow_real.sqlite `
  --input C:\r\dawnstrike-state\source-universe\alpha_v6_candidate-YYYY-MM-DD.json `
  --source-contract C:\r\dawnstrike-state\source-universe\alpha_v6_source-contract.json `
  --raw-artifact C:\r\dawnstrike-state\source-universe\alpha_v6_raw-YYYY-MM-DD.json `
  --confirm-preview-hash <exact-current-preview-hash>
```

If build returns `BLOCKED_EXTERNAL_APPROVAL`, stop. Do not edit a candidate by
hand, substitute a template, or register a raw ticker list.
