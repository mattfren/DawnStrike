# Public dashboard rollback

Keep the prior production deployment addressable during the seven-market-day
rollback window. Roll back the alias to the exact prior deployment ID; do not
rebuild from a different SHA.

```powershell
vercel promote <PRIOR_DEPLOYMENT_URL_OR_ID> --scope <TEAM_OR_ACCOUNT>
```

After rollback, verify `/api/health`, `/api/readiness`, the public data hash,
and the Overview/Performance/System surfaces. Preserve the failing deployment
URL, source SHA, build ID, data hash, and readiness response in the evidence
packet. Legacy local operator code remains available until a separate cleanup
approval closes the rollback window.
