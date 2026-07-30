# Deployment verification

Status: `IN_PROGRESS` for local Vercel packaging and
`BLOCKED_APPROVAL_REQUIRED` for production promotion.

The candidate configuration uses native static output at `build/public` and
only `api/health.py` and `api/readiness.py`. The public artifact verifier rejects
SQLite, database, scanner, Telegram, UI-runtime, secret, and path leakage.

The Vercel CLI initially reported that `uv` was not available in the local
PATH. `uv` was then installed in the local development environment and the
dependency-free stage build completed against the explicit Dawnstrike project
ID. No preview or production deployment was created by this candidate. The
clean-source portion of the local gate passes at the committed candidate SHA;
the copied real-data snapshot is still correctly rejected as degraded/not
ready. A preview must wait for a publishable approved snapshot and the full
browser/health/readiness proof.

## Latest Vercel build evidence

- Building from the full repository failed at 502.21 MB because the root
  `pyproject.toml` pulls the scanner dependency graph into the functions.
- Building from `build/vercel-stage` with the explicit Dawnstrike project ID
  `prj_5pef3EZF1u5YadebEz3dFjnkWOXy` succeeded. The prebuilt output is
  approximately 456 KB and contains only `api/health` and `api/readiness`
  functions plus static output. The generated public manifest records:
  `source_sha=2dd26cba24eae0b189c6f7890fd86faa0a9c71b4`,
  `build_id=1c4e911cd52bb8156de7`,
  `data_hash=4f1bfb9dacb968d989e0ab842c1cdbfdfa120b88299a56246fa18625d019504a`,
  `market_date=2026-07-29`, and `snapshot_bytes=232001`.
- The final candidate verifier fails closed with
  `snapshot_not_publishable` and `readiness_not_publishable`; the artifact
  reports 235 rows, `snapshot_status=degraded`, and readiness HTTP 503.
  Therefore no preview deployment was created and no alias was changed.
- A live recheck of the existing production alias remains the old X3 surface:
  `/api/health` returns 200 while `/api/readiness` returns HTTP 500
  (`FUNCTION_INVOCATION_FAILED`). The old health payload still exposes
  scanner, Telegram, and cron routes. This candidate has not altered it.
- An initial stage-root build without `--project` auto-created the separate
  Vercel project `vercel-stage` (`prj_xecuX03xwHT01EdsYewnezFUpeoG`). It has no
  deployment or traffic; future runs must pass the Dawnstrike project ID
  explicitly.
