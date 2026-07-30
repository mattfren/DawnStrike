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
| Vercel packaging | `LOCAL_VERIFIED` | Clean build source SHA `6e172db6d32844d93f63057ac28df50f052ce1d6` produces an 18-file, 834,208-byte native stage with zero forbidden files; the real snapshot is 607,268 raw bytes / 37,457 deterministic-gzip bytes and remains correctly rejected as degraded/not ready. |
| Preview deployment | `IN_PROGRESS` | Minimal Vercel build passes; candidate verification is waiting on publishable real-data readiness. |
| Production promotion | `BLOCKED_APPROVAL_REQUIRED` | Do not promote until preview, rollback, and owner approval are recorded. |
| Strategy evidence | `WAITING_FOR_FORWARD_EVIDENCE` | The existing dataset does not prove the required forward sample or benchmark evidence. |

## Known current truth

The current source database has unresolved research outcomes and incomplete
cost/benchmark inputs. The PaperOps export adds 190 daily rows, of which 105
are quarantined by cents-level identity checks. Therefore the candidate
intentionally shows only supported observations while leaving unsupported
after-cost return or excess return unreported. That is a data-quality
limitation, not a UI failure to hide.

## Luna execution order

1. Review the isolated candidate and evidence files.
2. Run the full local proof matrix and verify the static artifact.
3. Merge only the intended files after owner review; do not copy dirty X3/X4
   runtime or Telegram code into the candidate.
4. Register the single daily finalize task against the approved checkout and
   rerun `scheduler-doctor` plus a dated finalize rehearsal.
5. Repair or attest the PaperOps source identities until the real-data
   verifier is publishable; do not bypass the quarantine gate.
6. Deploy a preview from one exact clean SHA and run the browser proof matrix.
7. Obtain explicit production approval, then promote that exact preview SHA.
8. Record rollback proof and keep strategy quality in
   `WAITING_FOR_FORWARD_EVIDENCE` until the specified forward evidence exists.
