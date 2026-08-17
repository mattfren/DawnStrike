# WP007 Sol Audit — Honest Read-Only Product Projection

Date: 2026-08-16
Decision: **WP007 ACCEPTED**

## Scope adjudicated

Sol reviewed the implementation and durable packet for the disabled-by-default
research projection mounted in the existing Streamlit Today tab and static
Overview view. This acceptance covers the read model, rendering, public build,
readiness, verification, and safety boundaries only. It does not claim a real
holdout result, empirical edge, profitability, promotion, or deployment.

## Independent evidence checks

- Independently rehashed all 100 entries in
  `docs/quant-refactor/evidence/wp007-20260816/evidence-manifest.json`: zero
  missing files, length mismatches, or SHA-256 mismatches.
- Independently rehashed all 17 frozen source entries in `source-hashes.json`:
  zero missing files, length mismatches, or SHA-256 mismatches.
- Confirmed manifest SHA-256
  `e61c25051b77106613f292c3b24a51af1802c5d6a159fcee0e15f08e43108843`.
- Confirmed recorded gates: focused projection `28/28`, relevant public
  compatibility `36/36`, rendered compatibility `12/12`, accepted validation
  persistence `15/15`, accepted robustness `19/19`, accepted main regression
  `656/656`, and affected regression `139/139`, all exit `0`.
- Confirmed Ruff, mypy over 318 source files, compileall, Node syntax,
  PowerShell parsing, diff-check, import firewall, and collection reconciliation
  all exit `0`.
- Independently reran the focused projection collection: `28/28`, exit `0`.

## Semantic adjudication

- The feature flag is false by default and only normalized `1`, `true`, `yes`,
  or `on` enables it. The disabled branch returns before any database open.
- The enabled adapter opens only with the repository read-only connector,
  verifies query-only mode and a supported schema, then obtains the latest
  immutable `PipelineResult` through `OpportunityStore(read_only=True)` replay.
  It does not recreate decisions, join presentation data, or write state.
- `DISABLED`, `DATA_UNAVAILABLE`, `NO_QUALIFYING`, and `QUALIFYING` are distinct
  states. Missing, invalid, unsupported, empty, or unreplayable state cannot be
  displayed as a correct no-trade result. The exact no-trade sentence appears
  only for a valid persisted run with no qualifying decisions.
- The canonical immutable projection is bounded to five deterministic rows,
  preserves missing numeric truth as null, and exposes explicit evidence kind,
  lifecycle, validation wording, limitations, risk, and veto fields. Every
  projection remains research-only with order execution disabled.
- Streamlit rendering adds no unsafe HTML or action control. Static rendering
  uses DOM creation plus `textContent`, bounds rows again at five, and remains
  hidden while disabled. Existing tab and navigation inventories are unchanged.
- The disposable public artifact binds the projection bytes and manifest into
  the build hash, file inventory, readiness verifier, Vercel packaged state,
  and public artifact verification. It does not publish or deploy anything.

## Active-state safety

Sol independently reopened
`C:\r\dawnstrike-state\shadow_real.sqlite` with URI `mode=ro`, enabled
`PRAGMA query_only=ON`, and observed `query_only=1`, `quick_check=ok`, and
schema version `26`. The SHA-256 remained
`78f4a39fb31f389c05ef7ab626a74f89f840243fa79ff678ac05bad8379f93e6`,
length remained `198836224`, mtime ticks remained `639224336496872805`, and no
WAL, SHM, or journal sidecar existed.

## Limitations preserved

- Synthetic fixtures prove implementation and presentation invariants; they
  are not empirical validation results.
- Active state remains unmigrated at schema 26 and the feature remains off by
  default.
- `DATA_UNAVAILABLE` is the honest enabled-state result until compatible
  persisted opportunity runs exist.
- No profitability, promotion, production, deployment, broker, or live-order
  claim is made.

## Decision

**WP007 ACCEPTED.** All planned implementation work packages are now accepted.
The only authorized next step is one fresh-context, read-only independent final
audit followed by bounded repair, if required, and final certification.
