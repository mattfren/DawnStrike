# WP006 Sol Audit — Durable Validation and Locked-OOS Governance

Date: 2026-08-16
Decision: **WP006 ACCEPTED**

## Scope adjudicated

Sol reviewed the implementation and durable packet for schema-30 validation
persistence and database-enforced one-time locked-OOS consumption. This
acceptance covers software, schema, serialization, transaction, replay,
read-only, and evidence invariants only. It does not claim that a real holdout
was opened, that empirical edge exists, or that any strategy is promotable.

## Independent evidence checks

- Independently rehashed all 49 entries in
  `docs/quant-refactor/evidence/wp006-20260816/evidence-manifest.json`: zero
  missing files, length mismatches, or SHA-256 mismatches.
- Independently rehashed all 19 frozen source entries in
  `source-hashes.json`: zero missing files, length mismatches, or SHA-256
  mismatches.
- Confirmed manifest SHA-256
  `6d519bd28f467dcf87952b2c9558f6b7180e48529c884d99f175cde398d9c511`.
- Confirmed recorded gates: persistence `15/15`, robustness `19/19`, accepted
  main regression `656/656`, and affected regression `139/139`, all exit `0`.
- Confirmed Ruff, mypy over 315 source files, compileall, diff-check, import
  firewall, schema evidence, and collection reconciliation all exit `0`.

## Semantic adjudication

- `BEGIN IMMEDIATE` encloses schema validation, collision checks, the receipt
  insert, every ordered OOS-session insert, full from-JSON reconstruction,
  row/inventory reconciliation, and commit. Any failure rolls back the whole
  bundle.
- Exact retries are accepted only when the stored canonical projections and
  ordered inventory match. Same identity with different content fails closed.
- Three partial unique indexes independently guard the exact semantic lock,
  frozen declaration authority, and exact holdout inventory for consumed rows.
  Caller-selected IDs, result changes, inventory changes, and policy/identity
  aliases cannot manufacture a second fresh claim.
- The two governed tables are append-only through no-update/no-delete triggers;
  schema validation fingerprints their tables, indexes, triggers, columns,
  unique keys, and foreign key.
- Invalid, retrospective, reused, missing-evidence, and non-predeclared states
  cannot be fresh-lock eligible. Every persisted receipt is research-only,
  non-promotional, lifecycle-neutral, and unable to authorize TAKE.
- Read replay uses the existing URI `mode=ro` connector with query-only
  enforcement. Writable initialization and append are rejected when the store
  is configured read-only.

## Active-state safety

Sol independently reopened
`C:\r\dawnstrike-state\shadow_real.sqlite` with URI `mode=ro`, enabled
`PRAGMA query_only=ON`, and observed `query_only=1`, `quick_check=ok`, and
schema version `26`. The SHA-256 remained
`78f4a39fb31f389c05ef7ab626a74f89f840243fa79ff678ac05bad8379f93e6`,
length remained `198836224`, and no WAL, SHM, or journal sidecar existed.

## Limitations preserved

- Synthetic fixtures prove implementation invariants; they are not empirical
  validation results.
- Active state remains unmigrated at schema 26.
- The authority guard is intentionally conservative: one frozen declaration
  authority cannot be reused to create another successful claim even if a
  later result or inventory projection changes.
- No profitability, promotion, production, deployment, broker, or live-order
  claim is made.

## Decision

**WP006 ACCEPTED.** WP007 may begin as a disabled-by-default, read-only product
projection over persisted research evidence. It may not add writes, fabricate
data, enable promotion, open a real holdout, or alter the active database.
