# Luna core-universe currentness

The Luna core contract is a research-only input.  Broker execution remains
disabled.  S&P 500 membership is represented by the State Street SPY tracker
holdings file and is labeled `tracker_holdings_proxy`; it is not an official
S&P DJI constituent feed.  Nasdaq-100 membership for the Aug. 27 release is
the official Nasdaq SOD Weightings export, not the historical July replay.

The Aug. 27 Nasdaq source is the release anchor:

`https://indexes.nasdaq.com/Index/ExportWeightings/NDX?tradeDate=08%2F27%2F2026&timeOfDay=SOD`

Its ZIP container byte count and SHA-256 are intentionally not pinned because
official downloads vary archive metadata. The stable trust root is the
authenticated dated URL, exact ten-member workbook
structure, decompressed member/content digest
`6c8fe9543904412a8ceed93c9554ebad4b64213603e3b1cdccf09ec8ca8a269b`, and
canonical 102-member digest
`c5e8bb1294642e0812f8a8d20f8c015548d41c64bfc6bef0aa0187994828a0ed`.  The
validator hashes each persisted raw byte snapshot, checks that stable root,
parses every XML member and all membership cells from that same snapshot, and
persists the observed raw SHA as evidence.  A changed member set, a recomputed
manifest hash, a wrong date, a stale observation, a download failure, or an
unknown workbook/schema remains `DATA_UNAVAILABLE`.

## Refresh procedure

The production active pointer is refreshed only by the exact-SHA scheduled
Morning runner. Do not invoke `refresh_luna_core_universe.py` directly against
`C:\r\dawnstrike-state`, and do not manually install a captured workbook. The
scheduled runner locks the complete Python source set derived from the exact
Git tree, invokes the protected interpreter, and supplies the session date.
An operator may inspect independently captured bytes and their SHA-256 in a
disposable evidence directory, but that inspection does not authorize a
production pointer mutation.

The prior active generation is
retained until a staged directory containing the raw bytes and manifest
validates `READY`; one atomic active-pointer swap then activates the pair.  A
post-swap validation reopens that exact installed pair and rolls the pointer
back if it is not `READY`.  The generation is content-addressed by market date,
the stable decompressed workbook digest, and the retained SPY proxy digest, so
an unchanged same-day retry revalidates and reuses the active bytes without a
new pointer swap. If an interrupted prior attempt left the same deterministic
generation name inactive, the refresh preserves that entry under a unique
`.orphan.` name before building a clean generation; unknown bytes are never
deleted as recovery evidence. An exclusive refresh lock rejects concurrent
writers. A successful scheduled refresh must report `status: READY` and
`ndx_member_count: 102`. It
writes the raw capture and manifest under an immutable generation below
`$state\config\luna_core_universe_generations\`, then atomically swaps
`$state\config\luna_core_universe.json` to an authenticated active pointer.
If source download/schema/attestation validation fails, the active pointer and
prior generation remain unchanged; do not substitute a same-length download.

If the active pointer is completely absent, use the explicit one-time
State Street bootstrap mode only while the scheduler is quiescent: either
before today's Morning task has run, or after today's EOD and finalizer have
both run in that order. The requested market date must be the host's current
date:

```powershell
C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe `
  -NoProfile -ExecutionPolicy Bypass `
  -File 'C:\Program Files\Dawnstrike\bin\dawnstrike_release_launcher.ps1' `
  -Mode BootstrapUniverse `
  -CandidateRoot C:\r\dawnstrike-runtime `
  -ExpectedSha <exact-current-origin-main-sha> `
  -RuntimeRoot C:\r\dawnstrike-runtime `
  -StateRoot C:\r\dawnstrike-state `
  -MarketDate <host-current-YYYY-MM-DD>
```

Run that command from an elevated administrator console. The protected mode
admits only the pinned official State Street SPY holdings trust root,
attests both live workbooks, installs one content-addressed generation, and
must report `status: READY`, `proxy_bootstrapped: true`,
`ndx_member_count: 102`, and `spy_member_count: 503`. It never replaces an
existing pointer, never substitutes for a missing caller-specified proxy path,
and never changes broker, task, database, or notification configuration.
Finding any existing output entry (including
a malformed file, directory, or dangling symbolic link), rejects bootstrap
before ordinary refresh logic runs. Direct checkout invocation is rejected.

The market date is required and is part of the source/currentness attestation.
For a later session, omit `--ndx-artifact`: the refresh requests the exact dated
Nasdaq SOD URL itself and accepts the response only when it replays to the
release anchor's governed workbook structure, static schema members, and
canonical 102-symbol set. A local later-date workbook has no authenticated date
provenance and is rejected. A changed member set, stale State Street capture,
or renamed older evidence fails closed. The scheduled path passes its explicit
session date and omits the core manifest when validation fails, leaving the
core lane `DATA_UNAVAILABLE` while the mover lane is independent.

The scheduled Morning path runs this bounded refresh before `alpha-cycle`.  On
refresh failure it deliberately omits the core manifest for that run, so the
core lane records `DATA_UNAVAILABLE`/shortfall while the independent mover
lane can continue.  No broker or execution setting is changed by this refresh.

The Aug. 27 release anchor does not authorize a future changed constituent set.
Such a change requires a new reviewed trust root; a fresh observation timestamp
alone cannot authorize it.
