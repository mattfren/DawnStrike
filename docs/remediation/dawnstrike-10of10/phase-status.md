# Dawnstrike 10/10 remediation status

This packet is the implementation handoff for Luna. It records what is
implemented in this isolated candidate and what still requires evidence or
approval.

| Area | Candidate state | Closure gate |
|---|---|---|
| Truth model | `LOCAL_VERIFIED` | Canonical rows, cents reconciliation, PaperOps cohort separation/quarantine, and missing-outcome tests pass. |
| Daily publication | `LOCAL_VERIFIED` | One lock/retry/finalize chain writes snapshot, readiness, and stage manifest. |
| Risk controls | `LOCAL_VERIFIED` | Unknown safety inputs fail closed; live execution remains disabled. |
| Public UI | `LOCAL_VERIFIED` | Static responsive site, semantic navigation, bounded payload, and zero axe violations in the local browser pass. |
| Vercel packaging | `LOCAL_VERIFIED` | Clean build source SHA `808bbceabdfc931d83fe9a1d827375a7d622a586` produces an 18-file native stage with zero forbidden files; the real snapshot is 632,094 raw bytes / 38,426 deterministic-gzip bytes and remains correctly rejected as degraded/not ready. External persistence database paths now fail closed before opening. |
| Preview deployment | `PREVIEW_VERIFIED` | Deployment `dpl_H9oNEQrV9TBwCSkKtxa5f7hz5Auj` is READY but truthfully degraded; health, readiness, and build-manifest checks agree on SHA/build/data hash. Hosted browser visual proof is limited by Vercel Deployment Protection; current static artifact proof passes locally. |
| Production promotion | `BLOCKED_APPROVAL_REQUIRED` | Do not promote until preview, rollback, and owner approval are recorded. |
| Strategy evidence | `WAITING_FOR_FORWARD_EVIDENCE` | The existing dataset does not prove the required forward sample or benchmark evidence. |

## Known current truth

The current source database has unresolved research outcomes and incomplete
cost/benchmark inputs. The PaperOps export adds 190 daily rows: 85 are
accepted, 105 are quarantined, and 131 source issues remain; cents-level
identity checks also report 85 source return-field mismatches. The reconciled
artifact contains 425 canonical rows, 156 discrepancies, 142 eligible rows,
and only 12 observed rows (8.4507% coverage). Therefore the candidate
intentionally shows only supported observations while leaving unsupported
after-cost return or excess return unreported. That is a data-quality
limitation, not a UI failure to hide.

Recovery blocker: the fresh build was accidentally run against the shared
database and persisted the derived canonical read model. The shared DB now
contains 425 canonical rows, 222 daily rows, and one added console notification
instead of the pre-build 5/2 derived rows and 91 notifications. No raw source
rows or broker state were changed. Do not claim the shared database remained
read-only until an owner-approved recovery or retention decision is recorded.

## Luna execution order

1. Review the isolated candidate and evidence files.
2. Run the full local proof matrix and verify the static artifact.
3. Merge only the intended files after owner review; do not copy dirty X3/X4
   runtime or Telegram code into the candidate.
4. Register the single daily finalize task against the approved checkout and
   rerun `scheduler-doctor` plus a dated finalize rehearsal.
5. Repair or attest the PaperOps source identities until the real-data
   verifier is publishable; do not bypass the quarantine gate.
6. Review the already verified preview and rerun the browser proof matrix
   after Vercel access is available.
7. Obtain explicit production approval, then promote that exact preview SHA.
8. Record rollback proof and keep strategy quality in
   `WAITING_FOR_FORWARD_EVIDENCE` until the specified forward evidence exists.
