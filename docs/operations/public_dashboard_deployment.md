# Public dashboard deployment

The production surface is a read-only Vercel publication of `build/public`.
The local database, scanner, outcome capture, and Telegram delivery remain
outside Vercel.

Git deployments are disabled in `vercel.json`. A Git clone cannot produce the
durable-state-backed `build/public` artifact, and must never fall back to the
repository-root static files. Only the verified prebuilt flow below may update
the Dawnstrike deployment or production aliases.

## Candidate flow

```powershell
py scripts\build_public.py --db-path data\shadow_real.sqlite --paper-ops-root data\v2_paper_ops_live --out-dir build\public --date YYYY-MM-DD
py scripts\verify_public_artifact.py --root build\public
pwsh -File scripts\build_vercel_public_stage.ps1
pwsh -File scripts\verify_vercel_candidate.ps1
npx vercel build --yes --cwd build\vercel-stage --project <PROJECT_ID> --output .vercel-output-stage
```

The stage contains only `public/`, the two minimal Python endpoints, and a
minimal `vercel.json` and dependency-free `pyproject.toml`. Build it from the
stage root, not the full repository root; the full root includes scanner
dependencies and exceeds Vercel's function-size limit. It must be built from a
clean exact Git SHA. The build manifest records source SHA, build ID, data
hash, generated time, and file hashes. A degraded or missing snapshot is a
controlled 503, not a green deployment.

The exact verified prebuilt deployment is the only artifact eligible for
promotion. Production promotion is outside this local candidate and requires
the directive's explicit approval packet.
