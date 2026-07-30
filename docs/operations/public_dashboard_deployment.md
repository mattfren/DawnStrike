# Public dashboard deployment

The production surface is a read-only Vercel publication of `build/public`.
The local database, scanner, outcome capture, and Telegram delivery remain
outside Vercel.

## Candidate flow

```powershell
py scripts\build_public.py --db-path data\shadow_real.sqlite --out-dir build\public --date YYYY-MM-DD
py scripts\verify_public_artifact.py --root build\public
pwsh -File scripts\build_vercel_public_stage.ps1
pwsh -File scripts\verify_vercel_candidate.ps1
```

The stage contains only `public/`, the two minimal Python endpoints, and a
minimal `vercel.json`. It must be built from a clean exact Git SHA. The build
manifest records source SHA, build ID, data hash, generated time, and file
hashes. A degraded or missing snapshot is a controlled 503, not a green
deployment.

The exact verified prebuilt deployment is the only artifact eligible for
promotion. Production promotion is outside this local candidate and requires
the directive's explicit approval packet.
