# Dawnstrike post-hardening audit — 2026-08-02

## Verdict

The code path is materially safer and more auditable. Dawnstrike is **not**
ready to claim improved return rates or to publish a ready production state.
The remaining gap is production truth, not a cosmetic dashboard change.

## What is now hardened

- Daily V6 work only synchronizes sourced outcomes, labels, datasets, drift, and
  an idempotent daily receipt. Weekly work alone may refit models and create
  all-family purged walk-forward evidence.
- Challenger comparison is out-of-fold and chronological. The baseline remains
  selected; a controlled gradient challenger is evidence-only, never an
  automatic policy change.
- Failure attribution separates setup/regime, source quality, liquidity,
  catalyst, volatility, entry timing, estimated costs, exit/invalidation, and
  missing truth. The public UI exposes aggregates only.
- Experiments require immutable baseline/candidate configuration hashes and an
  untouched tagged prospective holdout. No historic row is retroactively
  assigned to an experiment and no result can auto-promote.
- A V6 universe now requires source ID, retrieval time, raw artifact hash, and
  configuration hash. It has non-mutating preview/diff, explicit preview-hash
  confirmation, immutable registration, and audited forward restore.
- XML parsing now uses `defusedxml`. CI uses hash-locked dependencies, pinned
  Actions, dependency audit, SBOM, Bandit no-regression baseline, secret gate,
  public-artifact security tests, and PowerShell parsing.
- Calendar, decision replay, and the research surface were preserved. The UI
  now shows daily/weekly freshness and a failure-attribution panel without
  fabricated returns.

## Proof collected

- Protected durable backup: schema 13 copied and migrated twice to schema 18;
  both `PRAGMA quick_check` results were `ok`. The protected backup was not
  changed.
- A build against that durable-copy database correctly remained non-publishable
  (`readiness=503`, no-data/degraded snapshot) but passed the new public
  artifact path/credential/holdout security scan.
- A receipt-backed full `py -m pytest -q` run exited 0. Focused V6, holdout,
  universe, UI, and security tests also passed. Mypy passed for 205 source
  files; Ruff, JavaScript syntax, Bandit no-regression, secret baseline,
  PowerShell parsing, and `pip-audit` passed. A reproducible CycloneDX 1.6
  SBOM was generated with 118 components.
- Rendered local proof showed the Calendar, research failure-attribution panel,
  and client navigation rendering without browser errors.

## Current production truth (refreshed 2026-08-02)

`https://dawnstrike-command-center-x3.vercel.app/api/health` is alive (HTTP
200) but serves source SHA `692e785cf8304a8045e88ab221dc644d4eb2e9e7` and
build `f42ac827fe55324ae491`. `/api/readiness` is HTTP 503.

Its explicit failed checks are `safety_evidence_unverified`,
`snapshot_not_publishable`, and `pipeline_not_ready`. The last recorded EOD
task result was nonzero; required upstream stages were missing; the last source
watermark was 2026-07-30. This is an honest degraded publication, not a valid
performance record.

## Exact remaining work

1. Obtain an approved point-in-time small-cap universe source and real source
   configuration: accountable contact, provider identities, terms/entitlements,
   primary and independent quote/outcome data, and benchmark coverage. Do not
   copy a template or use a fixture as production truth.
2. Build the provider-specific fetch adapter only after its contract is known;
   persist raw artifact, lineage, listing history, corporate actions, and the
   dated membership snapshot. Review the generated universe diff, then register
   with its exact preview hash.
3. Commit this clean candidate, copy that exact SHA to the runtime, back up the
   durable DB again, rehearse migration, then migrate only after the copy-on-
   write proof still passes.
4. Register the revised Windows tasks using the real password-logon identity.
   The identity must access the network, encrypted configuration, state root,
   and Telegram. Re-run the scheduler doctor and repair the EOD task failure.
5. Run dated source collection, paper reconciliation, outcome capture, daily
   V6 monitor, and weekly V6 training. Publish only if all required stages are
   complete (or an explicit valid no-trade day) and readiness is 200.
6. Deploy a Vercel preview from the clean staged artifact; prove source/build/
   data hashes, headers, artifact scan, Calendar, Research, System, health,
   and readiness. Promote only after explicit production approval and a tested
   rollback path.
7. Accumulate prospective evidence. Do not claim strategy improvement until the
   frozen gates are met: 60 forward sessions, 100 closed after-cost labels,
   source and benchmark coverage, positive purged OOF, calibration/interval
   evidence, untouched holdout, drawdown and concentration limits, and manual
   approval.
8. Burn down the 30 reviewed legacy Bandit medium findings (13 URL-open, 17
   dynamic-SQL) with allowlists/parameterization and focused regressions. The
   baseline blocks new findings but is not a substitute for removing legacy
   risk.

## Non-negotiable stop conditions

- No broker execution, orders, automated trading, personalized advice, or
  LLM-generated financial scoring.
- Never treat missing returns, fees, slippage, benchmark observations, or
  outcomes as zero.
- Never refit daily, select a challenger automatically, tune on holdout, or
  backfill experiment arms.
- Never promote a degraded artifact or a build whose source SHA, data hash,
  readiness, and browser proof do not match.
