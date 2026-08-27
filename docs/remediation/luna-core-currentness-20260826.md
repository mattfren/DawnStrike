# Luna core-universe currentness

The Luna core contract is a research-only input.  Broker execution remains
disabled.  S&P 500 membership is represented by the State Street SPY tracker
holdings file and is labeled `tracker_holdings_proxy`; it is not an official
S&P DJI constituent feed.  Nasdaq-100 membership for the Aug. 27 release is
the official Nasdaq SOD Weightings export, not the historical July replay.

The Aug. 27 Nasdaq source is:

`https://indexes.nasdaq.com/Index/ExportWeightings/NDX?tradeDate=08%2F27%2F2026&timeOfDay=SOD`

The governed workbook is exactly 8,439 bytes.  Its ZIP container SHA-256 is
intentionally not pinned because official downloads vary archive metadata; the
stable trust root is the authenticated URL, exact ten-member workbook
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

Run from the deployed runtime checkout after placing an independently
captured source workbook at a temporary path.  The prior active generation is
retained until a staged directory containing the raw bytes and manifest
validates `READY`; one atomic active-pointer swap then activates the pair.  A
post-swap validation reopens that exact installed pair and rolls the pointer
back if it is not `READY`.  The generation is content-addressed by market date,
the stable decompressed workbook digest, and the retained SPY proxy digest, so
an unchanged same-day retry revalidates and reuses the active bytes without a
new pointer swap.  An exclusive refresh lock rejects concurrent writers:

```powershell
$state = "C:\r\dawnstrike-state"
$raw = "C:\r\incoming\ndx-sod-2026-08-27.xlsx"
(Get-Item -LiteralPath $raw).Length
(Get-FileHash -LiteralPath $raw -Algorithm SHA256).Hash.ToLower()
py C:\r\dawnstrike-runtime\scripts\refresh_luna_core_universe.py `
  --state-root $state `
  --proxy-manifest "$state\config\luna_core_universe.json" `
  --ndx-artifact $raw `
  --market-date 2026-08-27
```

The command must report `status: READY` and `ndx_member_count: 102`.  It
writes the raw capture and manifest under an immutable generation below
`$state\config\luna_core_universe_generations\`, then atomically swaps
`$state\config\luna_core_universe.json` to an authenticated active pointer.
If source download/schema/attestation validation fails, the active pointer and
prior generation remain unchanged; do not substitute a same-length download.

The market date is required and is part of the source/currentness attestation.
This release has a governed source only for `2026-08-27`; an invocation for a
later session fails closed, even if the older workbook is freshly downloaded
or given a new observation timestamp. The scheduled path passes its explicit
session date to the refresh and omits the core manifest when that date is not
governed, leaving the core lane `DATA_UNAVAILABLE` while the mover lane is
independent.

The scheduled Morning path runs this bounded refresh before `alpha-cycle`.  On
refresh failure it deliberately omits the core manifest for that run, so the
core lane records `DATA_UNAVAILABLE`/shortfall while the independent mover
lane can continue.  No broker or execution setting is changed by this refresh.

This release is intentionally governed for `2026-08-27` only.  A later
scheduled market date is rejected until a new dated official source and trust
root are reviewed; a fresh observation timestamp cannot reactivate Aug-27
evidence for a later session.
