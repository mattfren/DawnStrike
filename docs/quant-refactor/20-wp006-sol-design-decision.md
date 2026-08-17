# WP006 Sol Design Decision — Durable Validation and Locked-OOS Governance

Date: 2026-08-16
Status: `CAPSULE_READY`

WP006 is the next critical-path package after WP005-C. It makes accepted
validation evidence durable and enforces one-time locked-OOS consumption. It
does not run a real holdout, claim empirical edge, or touch active state.

## Durable boundary

- Add one forward-only migration after the current opportunity persistence
  schema for immutable validation preparation, metric, robustness, and holdout
  access receipts.
- Persist exact canonical JSON bodies and their content hashes, not reduced
  summaries that require caller-provided objects to verify.
- Every public replay object must reconstruct and self-verify from the stored
  body alone under the accepted direct/from-JSON invariants.
- Inserts are append-only and idempotent only when both identity and exact
  content match. Same identity/different content and same semantic lock with a
  different identity fail closed.
- A write transaction is atomic across every receipt needed to establish the
  validation evidence set. Partial persistence is prohibited.

## One-time locked-OOS rule

- The consumed holdout identity is content-bound to the exact predeclared
  corpus/split/lock body, code/strategy/policy identities, and declared OOS
  session inventory.
- The database enforces one successful consumption for one exact lock via a
  unique semantic lock key independent of a caller-chosen receipt ID.
- A second attempt, alias ID, consistently rehashed body, different result,
  or altered session inventory cannot become a fresh OOS claim.
- An interrupted/failed transaction leaves no consumed receipt.
- Retrospective, reused, invalid-lock, missing-evidence, or non-predeclared
  statuses may be recorded as non-promotional evidence but cannot be converted
  into a successful fresh consumption.
- Successful consumption does not promote a strategy. It only proves that the
  exact holdout was consumed once under the frozen identities.

## Safety and operation

- Additive schema only; no destructive migration and no mutation of existing
  opportunity/outcome/miss/metric rows.
- Rehearse migrations and all writes on disposable databases only.
- Read-only replay uses URI `mode=ro` plus `PRAGMA query_only=ON` and cannot
  create a database, journal, WAL, or sidecar.
- Active/operator state at `C:\r\dawnstrike-state\shadow_real.sqlite` remains
  read-only and must not be migrated or written.
- No mounted runtime, UI, provider/network, broker, scheduler, deployment,
  branch, commit, stage, or push action is part of WP006.

## Scope

- New validation persistence modules under
  `intraday_scanner/storage/opportunity_validation_*.py` and narrow exports.
- One additive migration and its schema-fingerprint/inventory verification.
- Focused tests in `tests/test_opportunity_validation_persistence.py` plus
  narrowly necessary migration/read-only tests.
- Repository-durable evidence under
  `docs/quant-refactor/evidence/wp006-20260816/`.

## Acceptance

- Fresh insert, exact idempotent replay, collision, alias/rehashed second-use,
  rollback, tamper, corruption, missing-row, old-schema, two-migration,
  read-only, and no-sidecar tests pass.
- Stored preparation/metric/robustness/holdout bodies reconstruct
  byte-equivalently and independently revalidate their hashes and cross-body
  bindings.
- One-time lock uniqueness is database-enforced, not an in-memory assertion.
- Existing accepted WP005-C focused, WP005-B 656, affected 139, Ruff, full
  mypy, compileall, diff-check, and import firewalls remain green.
- Durable evidence binds exact commands, counts, exits, timing, hashes,
  modification inventory, repair cycles, and limitations for Sol adjudication.
