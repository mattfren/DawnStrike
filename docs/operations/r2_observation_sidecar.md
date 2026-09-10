# R2 observation sidecar

The R2 sidecar records the declared universe before a decision and consumes a
bounded JSONL file of already retained raw observations. It is a read-only
research artifact writer: it has no decision, training, return, broker, or
order capability. Existing `capture_intraday_operations.py` remains the
provider-owned source acquisition route. This sidecar does not replace or
modify that route.

The manifest must declare exactly these two scopes:

- `original_small_cap_gap`
- `liquid_reference_panel`

Each symbol is retained in the census as `selected`, `rejected`, `unselected`,
or `missing_input`; the runner never narrows the denominator to winners.
Events are assigned `contemporaneous` or `delayed` from their `available_at`
timestamp relative to the immutable `decision_deadline`. A delayed-only run is
`PARTIAL`, and an unavailable source is `MISSED_SESSION`; an explicit empty
JSONL file is the only healthy `EMPTY` result.

## Bounded noninteractive operation

The checked-in calendar says the next eligible session after 2026-09-09 is the
regular XNYS session on 2026-09-10. Verify that identity immediately before a
run:

```powershell
py -3.13 -c "from datetime import date; from intraday_scanner.market_calendar import market_session; print(market_session(date(2026,9,10)).to_dict())"
```

After the existing provider capture has produced a retained raw-events JSONL
file, start one sidecar run in an external evidence root. The command is
offline with respect to providers and has explicit page, event, byte, and
retry bounds:

```powershell
py -3.13 scripts/run_isolated_observer.py `
  --manifest C:\r\dawnstrike-r2-evidence\universe-2026-09-10.json `
  --source-events C:\r\dawnstrike-r2-evidence\raw-events-2026-09-10.jsonl `
  --output-root C:\r\dawnstrike-r2-evidence\observer-2026-09-10 `
  --repository-root C:\r\dawnstrike-remediation-r2-20260909 `
  --max-pages 1000 --max-events 10000 --max-bytes 4194304 --retries 3
```

Stop between pages by creating the cooperative stop marker:

```powershell
New-Item -ItemType File -Force C:\r\dawnstrike-r2-evidence\observer-2026-09-10.stop | Out-Null
```

Resume with the same command after removing the marker. The append-only event
IDs and atomic cursor make the replay idempotent and tolerate reordered input:

```powershell
Remove-Item -LiteralPath C:\r\dawnstrike-r2-evidence\observer-2026-09-10.stop -Force
py -3.13 scripts/run_isolated_observer.py `
  --manifest C:\r\dawnstrike-r2-evidence\universe-2026-09-10.json `
  --source-events C:\r\dawnstrike-r2-evidence\raw-events-2026-09-10.jsonl `
  --output-root C:\r\dawnstrike-r2-evidence\observer-2026-09-10 `
  --repository-root C:\r\dawnstrike-remediation-r2-20260909
```

The command is prepared and tested by this branch; no scheduler, heartbeat,
task registration, active runtime state, or provider credentials are changed
by it. A runner receipt is not evidence that the provider capture occurred.
The receipt must be joined to the existing provider capture receipt and its
source/configuration hashes by the independent R2 verifier.
