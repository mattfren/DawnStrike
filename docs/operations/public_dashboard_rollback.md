# Public dashboard rollback

Keep the prior production deployment addressable during the seven-market-day
rollback window, but do not mutate an alias with `vercel promote`,
`vercel rollback`, or `vercel alias`. Those commands bypass the durable
publication journal, the three-alias compare-and-swap boundary, and the sealed
rollback contract.

The only supported operator recovery is convergence of the exact nonterminal
publication journal created by the mounted production runtime. Supply the
journal's exact market date and the clean runtime's exact `origin/main` SHA:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1' `
  -Mode RecoverPublication `
  -CandidateRoot C:\r\dawnstrike-runtime `
  -ExpectedSha <EXACT_CURRENT_ORIGIN_MAIN_SHA> `
  -MarketDate YYYY-MM-DD `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state
```

Run it from an elevated administrator console. The installed launcher holds
every exact-SHA recovery helper read-locked and invokes the wrapper's
`RecoveryOnly` path; direct invocation of either checkout script is rejected.
Recovery never stages or promotes fresh bytes. It either completes the
uniquely sealed candidate when every governed alias still matches it, or
compensates all three aliases to their exact recorded prior deployments and
seals that outcome. Foreign, mixed, missing, or changed provider state fails
closed. `NO_NONTERMINAL_CURRENT_OPERATION` means no provider mutation was
performed.

There is deliberately no ad hoc rollback command after a journal is already
`COMPLETE`. A post-completion rollback requires a separately reviewed governed
implementation and authorization bound to that exact completed journal; until
then, preserve the prior deployment and evidence without changing aliases.

After compensation, verify the sealed compensation receipt, `/api/health`,
`/api/readiness`, the public data hash, and the Overview/Performance/System
surfaces. Preserve the failing deployment URL, source SHA, build ID, data hash,
readiness response, journal, and compensation receipt in the evidence packet.
Legacy local operator code remains available until a separate cleanup approval
closes the rollback window.

The publisher records and verifies rollback bytes against each alias's
canonical immutable deployment URL; it separately re-inspects the mutable alias
for compare-and-swap identity. Current journal v3 rollback records carry an
exact nested `rollback_contract`. Normal targets use
`READY_SOURCE_MANIFEST`. The only permitted non-ready target uses
`PINNED_LEGACY_CLOCK_STALE` and must match the one-time attestation documented
in `public_dashboard_deployment.md` across the complete three-alias set. No
other stale, missing-source-manifest, partial-alias, or mixed target is a valid
rollback. A prior successful v3 migration in the durable journal history also
blocks reuse of the pinned legacy target; only a terminal v4 compensation leaves
the one-time migration authorization available for retry.
