# WP007 Luna handoff — honest read-only product projection

Date: 2026-08-16  
Owner: Luna implementation lane  
Terminal candidate: `PASS_CANDIDATE_FOR_SOL_ADJUDICATION`

This handoff is an implementation and verification record, not WP007
acceptance. No requirement or finding is closed here. Sol remains the only
acceptance authority.

## Handoff identity

- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- Unchanged HEAD: `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- Design authority: `docs/quant-refactor/22-wp007-sol-design-decision.md`
- Evidence root: `docs/quant-refactor/evidence/wp007-20260816/`

The accepted WP001-WP006 dirty and untracked state was preserved. In
particular, the existing missing-SQLite fallback hunk in `app.py`, the storage
migration/export changes, and their existing tests are not WP007 work.

## Implemented contract and read boundary

`intraday_scanner/dashboard/opportunity_projection.py` defines one immutable,
deterministic, canonical projection with exactly four states: `DISABLED`,
`DATA_UNAVAILABLE`, `NO_QUALIFYING`, and `QUALIFYING`. It preserves `None` as
JSON `null`, bounds public text and collections, rejects path/secret/SQL/markup
content, keeps evidence kind, strategy lifecycle, and validation wording as
separate fields, and caps qualifying rows at five in persisted pipeline rank
order. Every projection and row remains research-only with order execution
disabled.

`intraday_scanner/dashboard/opportunity_projection_store.py` recognizes the
feature flag `DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED` only when its
normalized value is `1`, `true`, `yes`, or `on`. The disabled branch returns
before any database open or query. The enabled adapter opens SQLite through
URI `mode=ro`, verifies `PRAGMA query_only=ON`, selects only the deterministic
latest run identity, closes that connection, and reconstructs the result only
through `OpportunityStore(read_only=True).load_run`. Missing database,
unsupported/old schema, no run, corrupt database, read failure, and replay
failure become bounded path-free `DATA_UNAVAILABLE` reasons.

Only a successfully replayed `PipelineResult` with no `WATCH` or `TAKE`
decision emits the exact sentence `NO QUALIFYING TRADE CURRENTLY EXISTS.`
Absence, corruption, or unsupported state can never become a correct no-trade
claim.

## Existing product surfaces

- Streamlit renders the projection in the existing `Today` tab after the
  existing Today content and before the permanent no-orders footer. Disabled
  rendering is a no-op. The five existing tab labels and existing controls are
  unchanged.
- The static dashboard mounts a hidden-by-default panel inside the existing
  `Overview` view. The six existing navigation views are unchanged. JavaScript
  creates DOM nodes and assigns persisted strings through `textContent`; it
  does not inject persisted HTML. Disabled state remains hidden.
- Both renderers expose every required qualifying-row field and render missing
  values as `Not available`, never zero.

## Governed public artifact integration

The existing disposable public-build path writes canonical
`data/opportunity-projection.json` and
`data/opportunity-projection.json.manifest.json`. The manifest binds payload
SHA-256, byte count, state, row count, row bound, research-only truth, and the
execution boundary. The projection hash is incorporated into the build SHA,
build manifest, file-hash inventory, verifier, readiness checks, and packaged
Vercel public state. The default build output is a disabled payload. No live
publication or deployment occurred.

## WP007-owned files

New implementation and tests:

- `intraday_scanner/dashboard/opportunity_projection.py`
- `intraday_scanner/dashboard/opportunity_projection_store.py`
- `intraday_scanner/dashboard/opportunity_projection_render.py`
- `tests/test_opportunity_projection.py`
- `tests/test_opportunity_projection_public.py`
- `tests/test_opportunity_projection_streamlit.py`

Narrow additive mounts, publication compatibility, and tests:

- `app.py` — only the projection imports and Today-tab render call are WP007.
- `web/index.html`
- `web/assets/dawnstrike.js`
- `web/assets/dawnstrike.css`
- `scripts/build_public.py`
- `scripts/verify_public_artifact.py`
- `scripts/build_vercel_public_stage.ps1`
- `api/readiness.py`
- `tests/test_public_build_notifications.py`
- `tests/test_vercel_health_readiness.py`
- `tests/test_vercel_public_stage.py`

Documentation and evidence:

- `docs/quant-refactor/04-execution-log.md` — WP007 additive entry only.
- `docs/quant-refactor/luna/007-read-only-product-projection-handoff.md`
- `docs/quant-refactor/evidence/wp007-20260816/**`

No storage schema, opportunity persistence, validation persistence, or
accepted WP001-WP006 semantic file is WP007-owned.

## Verification and repair disclosure

The authoritative exact commands, raw stdout/stderr, UTC start/end timestamps,
elapsed durations, exits, independent collections, source hashes,
modification inventory, process-survivor proof, and active-state proof live in
the evidence root and are bound by its final manifest.

One bounded implementation repair cycle corrected three static typing issues
in the new modules before the final frozen gates. Two bounded test/evidence
correction cycles were used: one Streamlit assertion was changed from exact to
substring matching to reflect Streamlit's rendered title shape, and the import
firewall allowlist was corrected for two already accepted persistence modules.
No accepted contract or runtime behavior was changed by those corrections.

## Active-state and safety boundary

`C:\r\dawnstrike-state\shadow_real.sqlite` was inspected only through URI
`mode=ro` with `PRAGMA query_only=ON`. The before/after proof records SHA-256,
byte length, timestamp, schema version, `quick_check`, and WAL/SHM/journal
sidecars. The enabled adapter honestly reports `DATA_UNAVAILABLE` against that
schema-26 active state; it does not migrate it.

No real holdout was opened or run. Synthetic fixtures prove software and
presentation invariants only; they do not establish empirical edge or
profitability. No TAKE authorization, promotion, lifecycle mutation,
provider/network call, broker/order action, scheduler change, active-state
write/migration, deployment, commit, stage, push, or primary-checkout action
occurred.
