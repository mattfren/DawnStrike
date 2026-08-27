# Luna core-universe currentness

The Luna core contract is a research-only input.  Broker execution remains
disabled.  S&P 500 membership is represented by the State Street SPY tracker
holdings file and is labeled `tracker_holdings_proxy`; it is not an official
S&P DJI constituent feed.  Nasdaq-100 membership for the Aug. 27 release is
the official Nasdaq SOD Weightings export, not the historical July replay.

The Aug. 27 Nasdaq source is:

`https://indexes.nasdaq.com/Index/ExportWeightings/NDX?tradeDate=08%2F27%2F2026&timeOfDay=SOD`

The governed capture is exactly 8,439 bytes with SHA-256
`42b2f48f1365a54cca3109efcd084b47303f6d7877534737dccd455b7eda0ffc`.  The
manifest validator hashes the persisted bytes itself, checks the exact source
root, parses the workbook schema, and compares all 102 symbols.  A changed
member set, a recomputed manifest hash, a wrong date, a stale observation, a
download failure, or a schema failure remains `DATA_UNAVAILABLE`.

## Refresh procedure

Run from the deployed runtime checkout after placing the independently
captured exact bytes at a temporary path.  The prior manifest is retained until
the complete two-index candidate validates `READY`:

```powershell
$state = "C:\r\dawnstrike-state"
$raw = "C:\r\incoming\ndx-sod-2026-08-27.xlsx"
(Get-Item -LiteralPath $raw).Length
(Get-FileHash -LiteralPath $raw -Algorithm SHA256).Hash.ToLower()
py C:\r\dawnstrike-runtime\scripts\refresh_luna_core_universe.py `
  --state-root $state `
  --proxy-manifest "$state\config\luna_core_universe.json" `
  --ndx-artifact $raw
```

The command must report `status: READY`, `ndx_member_count: 102`, and the
governed SHA.  It writes the raw capture under
`$state\config\luna_core_universe_evidence\` and replaces
`$state\config\luna_core_universe.json` only after validation.  If the source
does not hash to the governed capture, stop and preserve the resulting
`DATA_UNAVAILABLE` evidence; do not substitute a same-length download.

The next scheduled Morning run must receive that manifest explicitly (or use
the default path above), and its logs must retain the contract hash and raw
artifact hash.  No broker or execution setting is changed by this refresh.
