# Public dashboard deployment

The production surface is a read-only Vercel publication of `build/public`.
The local database, scanner, outcome capture, and Telegram delivery remain
outside Vercel.

Git deployments are disabled in `vercel.json`. A Git clone cannot produce the
durable-state-backed `build/public` artifact, and must never fall back to the
repository-root static files. Only the verified prebuilt flow below may update
the Dawnstrike deployment or production aliases.

## Offline candidate diagnostics

This block is for developer/offline artifact diagnostics only. It must not
target durable production state, deploy a production candidate, or mutate an
alias. Ambient `py`/`npx` output is not production proof. Production build,
verification, deployment, and promotion are owned solely by the exact-date,
exact-SHA `Dawnstrike 10of10 Daily Finalize` task through its protected
toolchain, daily ledger authorizations, and durable publication journal.

```powershell
py scripts\build_public.py --db-path data\shadow_real.sqlite --paper-ops-root data\v2_paper_ops_live --out-dir build\public --date YYYY-MM-DD
py scripts\verify_public_artifact.py --root build\public
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\build_vercel_public_stage.ps1
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe -NoProfile -ExecutionPolicy Bypass -File scripts\verify_vercel_candidate.ps1
npx vercel build --yes --cwd build\vercel-stage --project <PROJECT_ID> --output .vercel-output-stage
```

The stage contains only `public/`, the two minimal Python endpoints, and a
minimal `vercel.json` and dependency-free `pyproject.toml`. Build it from the
stage root, not the full repository root; the full root includes scanner
dependencies and exceeds Vercel's function-size limit. It must be built from a
clean exact Git SHA. The build manifest records source SHA, build ID, data
hash, generated time, and file hashes. A degraded or missing snapshot is a
controlled 503, not a green deployment.

An artifact produced here is not eligible for promotion. The scheduled
production publisher independently rebuilds and verifies its own exact artifact
after all daily gates pass.

## Publication journal and the one-time legacy boundary

Fresh governed publications use journal schema
`dawnstrike.vercel_publication_journal.v3`; a compensated journal uses v4 and
its compensation receipt uses
`dawnstrike.vercel_publication_compensation.v2`. Existing v1/v2/v1 evidence
remains readable and recoverable only within its original schema family.

Before provider mutation, every production alias is compare-and-swap inspected
while all build, release, source, and asset bytes are fetched from the resolved
immutable deployment URL. Current-family receipts accept only lowercase ASCII
HTTPS origins with no port, path, query, fragment, or user information. A
deployment origin must match the governed Vercel project/scope host shape and
must differ from every mutable production alias. The provider inspection and
compare-and-swap checks remain the authority for the opaque deployment-ID to
URL relationship. A normal rollback target must still be `ready`, have HTTP
200 readiness, and carry the governed deployed-source manifest.

There is one migration-only exception for the deployment that predates the
source-manifest contract. It is admissible only when all three governed aliases
resolve to deployment `dpl_H7UQb8hWkwxLVbNwSM1BAQq1t9g8` at the canonical
immutable URL, and every pinned source/tree, raw build/release manifest hash,
18-file asset map, 3,286,836-byte total, legacy build formula, source-manifest
HTTP 404, and ordered two-check clock-staleness tuple matches. The sole
authoritative canonical attestation SHA-256 is
`6846a6dd24bc905fee86d2b1c541140d2da3420cc5101c57c0793959b9efaa30`.
The earlier
`9b60bdbc520e8f467284130b5c55ed09f9f0f1fe8141054dd6199ffd3e6d6489`
draft omitted the total-byte binding and is not accepted.

This exception authorizes only an exact compensation target. It never relaxes
the new candidate's health, readiness, source-manifest, exact-SHA daily ledger,
or artifact requirements. A successfully verified v3 `COMPLETE` journal in the
durable publication history permanently consumes the migration exception. A
v4 `COMPENSATED` attempt does not consume it and may be retried. Even if an
operator later points every alias back to the legacy deployment, prior success
keeps the exception unreachable.
