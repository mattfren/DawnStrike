# Deployment verification

Status: `BLOCKED_EXTERNAL` for local Vercel packaging and
`BLOCKED_APPROVAL_REQUIRED` for production promotion.

The candidate configuration uses native static output at `build/public` and
only `api/health.py` and `api/readiness.py`. The public artifact verifier rejects
SQLite, database, scanner, Telegram, UI-runtime, secret, and path leakage.

The Vercel CLI was invoked against the linked project and stopped before any
deployment because `uv` is not installed in the local PATH. No preview or
production deployment was created by this candidate. The local source/artifact
gate also rejects the current diagnostic build because the checkout is dirty
and the copied real-data snapshot is degraded. Once `uv` is available and the
candidate is committed cleanly, run the stage build, `vercel build --prod`, one
`vercel deploy --prebuilt`, and the browser/health/readiness proof before
requesting promotion approval.
