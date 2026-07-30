# Deployment verification

Status: `PREVIEW_VERIFIED` for the isolated preview (truthfully degraded) and
`BLOCKED_APPROVAL_REQUIRED` for production promotion.

The candidate configuration uses native static output at `build/public` and
only `api/health.py` and `api/readiness.py`. The public artifact verifier rejects
SQLite, database, scanner, Telegram, UI-runtime, secret, and path leakage.

The current local adapter correction was rebuilt read-only from the copied
database and the external PaperOps source at candidate SHA
`6886900de5960a90c8693e476c8c32d26f864375`. Build
`b0988a1e450a54fceb0e` produced data hash
`5487936639b61c95695500f7975cb901fc2c7f00e4665b158c7ce7ebbb03a7aa`,
425 canonical rows, 222 daily records, 633,502 raw snapshot bytes /
42,293 deterministic-gzip bytes, 190 accepted PaperOps rows, 0 quarantined
rows, 21 component-scope warnings, and readiness HTTP 503. This local
artifact is not a deployment.

The Vercel CLI initially reported that `uv` was not available in the local
PATH. `uv` was then installed in the local development environment. A Windows
PowerShell UTF-8 BOM in the stage writer was also corrected because Vercel
rejected the staged JSON before the build could start. The dependency-free
stage build then completed against the explicit Dawnstrike project ID. The
clean-source portion of the local gate passes at the committed candidate SHA;
the real-data snapshot is still correctly rejected as degraded/not ready. A
truthful preview was deployed without changing production; promotion remains
blocked until the data and approval gates close.

## Latest Vercel build evidence

- Building from the full repository failed at 502.21 MB because the root
  `pyproject.toml` pulls the scanner dependency graph into the functions.
- Building from `build/vercel-stage` with the explicit Dawnstrike project ID
  `prj_5pef3EZF1u5YadebEz3dFjnkWOXy` succeeded. The native `.vercel/output`
  contains 18 files and a direct scan found zero forbidden files. The build
  used Vercel CLI 58.4.0 and the generated public manifest records
  `source_sha=6886900de5960a90c8693e476c8c32d26f864375`,
  `build_id=b0988a1e450a54fceb0e`,
  `data_hash=5487936639b61c95695500f7975cb901fc2c7f00e4665b158c7ce7ebbb03a7aa`,
  `market_date=2026-07-29`, `snapshot_bytes=633,502`, and
  `snapshot_compressed_bytes=42,293`.
- The fresh corrected preview is deployment
  `dpl_9UXadeGZsJTBoQt6g8BLdxopYYVg` at
  `https://dawnstrike-command-center-x3-qseiaiddm-mattfrens-projects.vercel.app`.
  Inspect reports `READY`, target `preview`, and only the two expected
  Python functions. It was deployed without `--prod`.
- Vercel CLI `curl` proof on that exact deployment: `/api/health` returns 200
  with the corrected source SHA/build ID, research-only true, and live trading
  false; `/api/readiness` returns HTTP 503 with
  `snapshot_not_publishable` and `pipeline_not_ready`,
  `reconciliation_status=PARTIAL`, and the corrected data hash;
  `/build-manifest.json` matches the same source SHA, build ID, and data hash.
- The local current-artifact browser pass renders the four sections, safety
  evidence panel, unknown-state copy, gzip-size text, and no console errors.
  Direct browser navigation to the hosted preview is protected by Vercel login
  in this environment, so hosted visual proof is not claimed beyond the static
  artifact and authenticated CLI API checks.
- A live recheck of the existing production alias remains the old X3 surface:
  `/api/health` returns 200 while `/api/readiness` returns HTTP 500
  (`FUNCTION_INVOCATION_FAILED`). The old health payload still exposes
  scanner, Telegram, and cron routes. This candidate has not altered it.
- An initial stage-root build without `--project` auto-created the separate
  Vercel project `vercel-stage` (`prj_xecuX03xwHT01EdsYewnezFUpeoG`). It has no
  deployment or traffic; future runs must pass the Dawnstrike project ID
  explicitly.

## Persistence safety and shared database write incident

The fresh candidate build was correctly source-clean but was invoked with the
shared database path. Its persistence-enabled finalize chain wrote 425 derived
performance rows, 222 daily rows, and one console notification (`id=92`) into
the shared DB. Raw positions, fills, outcomes, and broker state were not
changed. No pre-write backup with the original 5/2 derived counts was found;
production promotion and further shared-DB writes were blocked pending the
owner decision. The owner has now approved retaining and auditing the derived
state; that approval is recorded in
`shared-db-retention-approval.md` and does not authorize production promotion.
The candidate now rejects any `--db-path` outside its repository before opening
the database; the guard is covered by `tests/test_public_build_safety.py`.

Read-only raw-table comparison against the existing evidence copy shows no
raw-data delta: `paper_positions` 7 rows/hash
`bfc24644cee76cfa48a7fec48741ee1c896636375cfc99ceb5bd0b35be5f0c74`,
`paper_trade_fills` 14 rows/hash
`833187f8ec93dab3dc6a06c4e9e3862e1e224651a435f9119b9dc089053285ce`,
`signal_outcomes` 8 rows/hash
`31e7de936c6666321695651406cabc03707b8c524cd3fe3faa700f4461914d18`, and
`historical_signals` 228 rows/hash
`b9115a500a89b2305e3e6d06572761299530a67b8d4c9abe104bc85ddacfd93b` match
exactly. The incident is confined to derived canonical tables and the one
console notification.

## Fresh live production recheck — 2026-07-30 06:08:50 America/Chicago

The production alias remains unchanged at the old X3 deployment. The live
`/api/health` response returned HTTP 200 and `status=ok`, with
`live_trading_enabled=false` and `research_only=true`, but still listed
`/api/scanner`, `/api/telegram`, `/api/cron_morning`, and
`/api/cron_after_close`; it also reported `static_ui_served_by_rewrite=true`.
The response exposed a safety inconsistency: `env.present.TELEGRAM_DRY_RUN=true`
was reported alongside `telegram_dry_run=false` and
`telegram_ready_for_external_send=true`. No production mutation was made.

The live `/api/readiness` response again returned HTTP 500
`FUNCTION_INVOCATION_FAILED` rather than a controlled readiness response. This
is a production failure in the old deployment, not evidence against the
isolated candidate's controlled HTTP 503 degraded response.
