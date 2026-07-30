# Deployment verification

Status: `IN_PROGRESS` for local Vercel packaging and
`BLOCKED_APPROVAL_REQUIRED` for production promotion.

The candidate configuration uses native static output at `build/public` and
only `api/health.py` and `api/readiness.py`. The public artifact verifier rejects
SQLite, database, scanner, Telegram, UI-runtime, secret, and path leakage.

The Vercel CLI initially reported that `uv` was not available in the local
PATH. `uv` was then installed in the local development environment. A Windows
PowerShell UTF-8 BOM in the stage writer was also corrected because Vercel
rejected the staged JSON before the build could start. The dependency-free
stage build then completed against the explicit Dawnstrike project ID. No
preview or production deployment was created by this candidate. The
clean-source portion of the local gate passes at the committed candidate SHA;
the copied real-data snapshot is still correctly rejected as degraded/not
ready. A preview must wait for a publishable approved snapshot and the full
browser/health/readiness proof.

## Latest Vercel build evidence

- Building from the full repository failed at 502.21 MB because the root
  `pyproject.toml` pulls the scanner dependency graph into the functions.
- Building from `build/vercel-stage` with the explicit Dawnstrike project ID
  `prj_5pef3EZF1u5YadebEz3dFjnkWOXy` succeeded. The prebuilt output contains 18
  files and 834,208 bytes, with only `api/health` and `api/readiness`
  functions plus static output; a direct scan found zero forbidden files. The
  build used Vercel CLI 58.4.0 and the generated public manifest records:
  `source_sha=81f0fb52e1ae624064b21352e287e6e95216f071`,
  `build_id=c2e169708e2cb4bd4a00`,
  `data_hash=ade3190c0ee9d6992f8d99a032f03e1f061be2f3af811c7da943e7909b7ef918`,
  `market_date=2026-07-29`, `snapshot_bytes=607268`, and
  `snapshot_compressed_bytes=37463`.
- The final candidate verifier fails closed with
  `snapshot_not_publishable` and `readiness_not_publishable`; the artifact
  reports 425 canonical rows, 156 discrepancies, `snapshot_status=degraded`,
  and readiness HTTP 503.
  The compressed-size and 250-row bounds pass; the only verifier failures are
  the intentional `snapshot_not_publishable` and `readiness_not_publishable`
  gates. Therefore no preview deployment was created and no alias was changed.
- A live recheck of the existing production alias remains the old X3 surface:
  `/api/health` returns 200 while `/api/readiness` returns HTTP 500
  (`FUNCTION_INVOCATION_FAILED`). The old health payload still exposes
  scanner, Telegram, and cron routes. This candidate has not altered it.
- An initial stage-root build without `--project` auto-created the separate
  Vercel project `vercel-stage` (`prj_xecuX03xwHT01EdsYewnezFUpeoG`). It has no
  deployment or traffic; future runs must pass the Dawnstrike project ID
  explicitly.
