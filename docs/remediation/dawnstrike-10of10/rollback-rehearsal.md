# Rollback rehearsal

Status: `BLOCKED_APPROVAL_REQUIRED`

A production rollback was not executed because this candidate has not received
production approval. The verified preview is
`dpl_H9oNEQrV9TBwCSkKtxa5f7hz5Auj`; the rollback command and verification
sequence are documented in
`docs/operations/public_dashboard_rollback.md`.

Required evidence before production promotion: prior deployment ID, candidate
preview deployment ID, exact source SHA/build ID/data hash, successful alias
verification, and a read-only rollback rehearsal against the prior deployment.
