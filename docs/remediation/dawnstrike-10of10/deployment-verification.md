# Deployment verification

Status: `PREVIEW_VERIFIED_DEGRADED` for the isolated preview and
`BLOCKED_APPROVAL_REQUIRED` for production promotion.

The candidate configuration uses native static output at `build/public` and
only `api/health.py` and `api/readiness.py`. The public artifact verifier rejects
SQLite, database, scanner, Telegram, UI-runtime, secret, and path leakage.

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
  contains 18 files / 861,548 bytes including diagnostics (15 payload files /
  852,024 bytes excluding diagnostics); a direct scan found zero forbidden
  files. The build used Vercel CLI 58.4.0 and the generated public manifest
  records `source_sha=108835991179145fa3e59b9bfb954de1a8cf222f`,
  `build_id=f58708827e2dbd9f1531`,
  `data_hash=cdf2af5c886cb7cddc4c1e147b1e314feef08e15e8c0a5cbf06fd5d1b244b061`,
  `market_date=2026-07-29`, `snapshot_bytes=632,094`, and
  `snapshot_compressed_bytes=38,420`.
- A first preview exposed a real runtime packaging defect (`snapshot_missing`).
  The candidate now embeds the generated public readiness/build/snapshot state
  as a direct dependency of both minimal Python functions. The corrected
  preview is deployment `dpl_EK3mf9AHCYeaZrtivRiXXyTc2Hyb` at
  `https://dawnstrike-command-center-x3-m9pqo1ru6-mattfrens-projects.vercel.app`.
  It is `READY`, preview-targeted, and was deployed without `--prod`.
- Vercel CLI `curl` proof on that exact deployment: `/api/health` returns 200
  with `source_sha=108835991179145fa3e59b9bfb954de1a8cf222f`,
  `build_id=f58708827e2dbd9f1531`, research-only true, and live trading false;
  `/api/readiness` returns the intended HTTP 503 with only
  `snapshot_not_publishable` and `pipeline_not_ready`; `/build-manifest.json`
  matches the same source SHA, build ID, and data hash.
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
