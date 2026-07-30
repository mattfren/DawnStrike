# Deployment verification

Status: `BLOCKED_EXTERNAL` for local Vercel packaging and
`BLOCKED_APPROVAL_REQUIRED` for production promotion.

The candidate configuration uses native static output at `build/public` and
only `api/health.py` and `api/readiness.py`. The public artifact verifier rejects
SQLite, database, scanner, Telegram, UI-runtime, secret, and path leakage.

The Vercel CLI was invoked against the linked project and stopped before any
deployment because `uv` is not installed in the local PATH. No preview or
production deployment was created by this candidate. The clean-source portion
of the local gate now passes at the committed candidate SHA; the copied
real-data snapshot is still correctly rejected as degraded/not ready. Once
`uv` is available, run the stage build, `vercel build --prod`, one `vercel
deploy --prebuilt`, and the browser/health/readiness proof before requesting
promotion approval.

## Latest Vercel build evidence

- Building from the full repository failed at 502.21 MB because the root
  `pyproject.toml` pulls the scanner dependency graph into the functions.
- Building from `build/vercel-stage` with the explicit Dawnstrike project ID
  succeeded. The prebuilt output is approximately 456 KB and contains only
  `api/health` and `api/readiness` functions plus static output.
- The final candidate verifier still fails closed with
  `snapshot_not_publishable` and `readiness_not_publishable`; therefore no
  preview deployment was created and no alias was changed.
- An initial stage-root build without `--project` auto-created the separate
  Vercel project `vercel-stage` (`prj_xecuX03xwHT01EdsYewnezFUpeoG`). It has no
  deployment or traffic; future runs must pass the Dawnstrike project ID
  explicitly.
