# Final commit evidence and Sol adjudication

Date: 2026-08-17  
Role: Sol decision plane  
Worktree: `C:\r\dawnstrike-quant-refactor-20260811`  
Branch: `codex/sol-quant-refactor-20260811`

## Verdict submitted for independent certification

`READY_FOR_FINAL_INDEPENDENT_AUDIT`

The complete software candidate is committed, checkout-to-HEAD bound, and has
passed its exact final gates. All 63 software requirements are `PASS`; 13 prior
findings are closed. FINDING-011 remains `EXTERNAL_DATA_BLOCKED` because real
consolidated historical entitlement was outside this offline program. That
external-data status is non-promotional and is not an implementation defect.

## Commit and source identity

- Candidate commit: `fabca37fdcb61c9a2e7825b903ddd456adf1ec85`.
- Candidate tree: `08d69b9e7d1dc65745c4d7835541f3b093060921`.
- Parent: `2b5e2d20fe03cdc02005a03ba88c8899a2cdff52`.
- Source/test paths: 580.
- Checkout-byte aggregate: `83bf62c2:5a9d8fa6:616a4992:b5faae95:dd9c71a0:ba59d817:f972788a:236d07de`.
- Portable HEAD Git-blob aggregate: `8ca0cf1c:266529db:88536bca:9fdf2d69:897fb557:5d9621cb:587b5bdf:45110a4a`.
- Every candidate checkout path equals its HEAD blob before and after the gate.

## Exact final gate

The canonical inventory contains 3,166 unique pytest nodes with digest
`90360b41:ba6b42d5:b8317fe9:7ff95703:7d251a59:f8174e5d:76799f51:b218b781`.
Sixteen exact manifest shards selected and passed all 3,166 nodes exactly once:

- passed: 3,166;
- failed, skipped, xfailed, xpassed, missing, duplicate: zero;
- shards 00-13: 198 nodes each;
- shards 14-15: 197 nodes each;
- all shard and pytest exit codes: zero.

The static gate also passed whole-repository Ruff, mypy, compileall, pip check,
the Windows-safe tracked-file detect-secrets helper, Node syntax, all tracked
PowerShell parsing, and `git diff --check`.

Durable evidence is under
`docs/quant-refactor/evidence/final-commit-gate-20260817/`. The evidence
manifest contains 236 entries, has zero missing/mismatched/extra files, and has
SHA-256 `a48c3ce2b27cac96cf820bd2ec867bd7bc770bf17f5119bb8d3d031469eae1c1`.
The combined-result SHA-256 is
`7e0881f59e8fe0cd4a07e2878526b8a9a3ec113ccd55cdc7f3b45cca5574f41b`.

## Active-state causal boundary

The live database at `C:\r\dawnstrike-state\shadow_real.sqlite` is not a frozen
test fixture. It is legitimately advanced every five minutes by the enabled
`Dawnstrike AlphaOps Monitor 5m` scheduled task, whose action is rooted in the
separate `C:\r\dawnstrike-runtime` checkout. The final gate did not disable,
modify, or invoke that task.

Every candidate shard command excluded the active database and persistence;
51 inventory nodes enforce active-state isolation/no-persist contracts. The
gate used the active path only for `mode=ro&immutable=1`,
`PRAGMA query_only=ON` probes. All probes remained schema 26,
`quick_check=ok`, and sidecar-free. Twenty-one observed identity changes aligned
to the PT5M task cadence and matched exit-zero, research-only, no-broker runtime
receipts. The preserved shard-03 preflight collision crossed the 17:00 UTC task
run, started no pytest, and was retried only after exact attribution.

Therefore the active-state classification is
`AUTHORIZED_EXTERNAL_RUNTIME_DRIFT`, not candidate hash drift. Any
unattributed change, candidate persistence access, sidecar, or read-health
failure would still fail closed.

## Independent-audit finding disposition

- C-00: closed by causal external-writer attribution and candidate
  non-interference proof.
- C-01: closed by strict authenticated canonical return truth across watcher,
  capture, reconciliation, labels, training, and holdout.
- H-01: closed by a committed candidate plus portable checkout-to-HEAD blob
  equality before and after all final gates.
- H-02/H-03: closed by the disabled-by-default non-test research CLI/service
  mount, shared current/historical producer, experimental V5 adapter, retained
  catalyst adapter, cache invalidation, and structured failure receipts.
- H-04: closed by the exact 63-requirement and 14-finding ledgers.
- H-05: closed by the final whole-repository security/static gates, including
  the Windows-safe exact tracked-file secret scan.
- H-06/M-01: closed by exact deterministic 16-way coverage of all 3,166 nodes.
- M-02: closed by the documented WP006/WP007 evidence seam.

Unresolved implementation findings: critical zero, high zero. FINDING-011 is
retained only as the declared external-data boundary.

## Prohibited claims

This adjudication does not claim empirical edge, provider entitlement,
real-data holdout success, strategy promotion, production TAKE eligibility,
live execution, deployment, or publication. No provider, broker, live-order,
deploy, publish, or promote action occurred.
