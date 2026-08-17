# Final independent certification

Date: 2026-08-17  
Independent role: fresh read-only Luna auditor  
Sol disposition: accepted  
Terminal verdict: `READY`

## Audited commit chain

The auditor inspected a clean branch at final evidence-seal commit
`848aa3a1:7b2ce74b:43d726b2:df5da133:83315e2f`. Its sole parent was candidate
commit `fabca37f:dcb61c9a:2e7825b9:03ddd456:adf1ec85`. The sealing commit changed
243 evidence, ledger, and reviewed secret-baseline paths and changed zero of the
580 filtered source/test paths.

Fresh portable identity capture found every checkout file equal to its HEAD
blob and every final-HEAD source/test blob equal to the candidate blob. The
aggregate remained
`8ca0cf1c:266529db:88536bca:9fdf2d69:897fb557:5d9621cb:587b5bdf:45110a4a`.

## Evidence and gate result

The auditor independently rehashed the final-commit gate manifest with zero
missing, length-mismatched, hash-mismatched, or extra evidence files. The
canonical inventory remained 3,166 unique nodes with digest
`90360b41:ba6b42d5:b8317fe9:7ff95703:7d251a59:f8174e5d:76799f51:b218b781`.
All sixteen shard manifests were exact ordinal partitions: fourteen shards of
198 and two shards of 197. Reparsed result markers showed:

- selected, unique, and passed: 3,166;
- failed, skipped, xfailed, xpassed, missing, and duplicate: zero;
- shard and pytest exit codes: zero.

The evidence-manifest digest was
`a48c3ce2:b27cac96:cf820bd2:ec867bd7:bc770bf1:7f5119bb:8d3d0314:69eae1c1` and the
combined-result digest was
`7e0881f5:9e8fe0cd:4a07e287:8526b8a9:a3ec113c:cd55cdc7:f3b45cca:5574f41b`.

## Fresh independent checks

The auditor ran fresh, read-only verification after the evidence seal:

- Ruff: pass;
- mypy across 324 source files: pass;
- compileall with bytecode outside the checkout: pass;
- pip check: pass;
- exact tracked-file detect-secrets: pass;
- Bandit: pass with zero medium/high findings;
- every tracked JavaScript and all 55 tracked PowerShell files: syntax pass;
- `git diff --check`: pass;
- focused safety, mounted opportunity, canonical-return, and active-isolation
  suite: 1,119 passed in 618.11 seconds;
- unauthorized pytest/shard process count before and after: zero.

The ledgers parsed to exactly 63 requirement rows, all `PASS`; the evidence
matrix preserved 56 `PASS` plus seven non-promotional `PASS_SOFTWARE_ONLY`
rows. Thirteen findings are `CLOSED`. FINDING-011 alone remains
`EXTERNAL_DATA_BLOCKED`.

## Critical and high closure

Independent source and contract inspection confirmed:

- C-01: watcher intent, exact authenticated source observation, canonical
  entry receipt, ordered replay, capture/reconciliation, labels, training, and
  holdout use strict canonical return truth. Legacy/pathless booleans cannot
  reach learning or promotion eligibility.
- H-02/H-03: the opportunity path has a real disabled-by-default non-test
  research CLI/service mount, explicit retained absolute inputs, active-DB
  rejection, shared current/historical producer, experimental research-only V5
  adapter, causal read-only catalyst adapter, evidence-identity cache, and safe
  structured failure receipts.
- H-05: CI runs whole-repository Ruff and the Windows-safe NUL-delimited exact
  tracked secret helper.
- C-00: the candidate excludes the active database and persistence; active
  reads fail closed and use immutable query-only mode.

Unresolved critical findings: zero.  
Unresolved high findings: zero.

## Live active-state adjudication

The active database is legitimately advanced by the separate enabled
five-minute research monitor rooted at `C:\r\dawnstrike-runtime`. The auditor
did not disable, modify, or invoke it. Every database inspection used
`mode=ro&immutable=1` with `PRAGMA query_only=ON`; the final probe returned
`quick_check=ok`, schema 26, no sidecars, and stable identity during the read.

Observed 19:55, 20:00, 20:05, and 20:10 UTC state shifts aligned exactly with
separate runtime stage records marked `COMPLETE`, exit zero, while the
scheduled-task result remained zero. No candidate access or unattributed
mutation occurred. The terminal classification is
`AUTHORIZED_EXTERNAL_RUNTIME_DRIFT`, not candidate hash drift.

## Certification boundary

FINDING-011 is solely an external-data limitation. This certification does not
claim empirical edge, consolidated historical entitlement, real-data holdout
success, strategy promotion, production TAKE eligibility, live execution,
deployment, or publication.

`CERTIFICATION: READY`
