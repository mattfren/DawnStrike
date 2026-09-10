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

First run the existing authenticated, read-only provider capture into a
dedicated external root. The receipt and state/page artifacts are the only
inputs accepted by the producer adapter; no credential is passed as a command
argument:

```powershell
py -3.13 scripts/capture_intraday_evidence.py `
  --provider alpaca --feed sip --evidence-mode forward_observed `
  --symbols-file C:\r\dawnstrike-r2-evidence\symbols.txt `
  --symbols-file-sha256 <symbols-file-sha256> `
  --market-date 2026-09-10 --exchange-session-id XNYS:2026-09-10:regular `
  --utc-start 2026-09-10T13:30:00+00:00 --utc-end 2026-09-10T14:00:00+00:00 `
  --db-path C:\r\dawnstrike-r2-evidence\capture.sqlite `
  --evidence-root C:\r\dawnstrike-r2-evidence\capture `
  --run-root C:\r\dawnstrike-r2-evidence\runs `
  --code-sha <candidate-code-sha> `
  --source-config-hash <source-config-sha256> `
  --operator-entitlement-metadata C:\r\dawnstrike-r2-evidence\entitlement.json `
  --operator-entitlement-metadata-sha256 <entitlement-sha256> `
  --env-file C:\r\dawnstrike-r2-evidence\runtime.env `
  --include-trades --include-quotes --include-corporate-actions
```

The scope declaration is an upstream, source-backed declaration with exactly
the two named scopes and full membership statuses. It must be retained beside
the capture receipt. Bind the authenticated receipt and retained page
artifacts into the R2 manifest and raw-event stream:

```powershell
py -3.13 scripts/build_observation_inputs.py `
  --capture-receipt C:\r\dawnstrike-r2-evidence\runs\<run>\capture_run_receipt.json `
  --scope-declaration C:\r\dawnstrike-r2-evidence\scope-declaration.json `
  --output-root C:\r\dawnstrike-r2-evidence\observer-inputs-2026-09-10 `
  --decision-deadline 2026-09-10T14:00:00+00:00 `
  --repository-root C:\r\dawnstrike-remediation-r2-20260909
```

The adapter preserves the source receipt hash, provider/feed, source-config
hash, code SHA, capture request/completion times, session identity, and page
artifact hash on each event. Keep the generated input root immutable and use a
separate external observer evidence root. The observer command is offline with
respect to providers and has explicit page, event, byte, and retry bounds:

```powershell
py -3.13 scripts/run_isolated_observer.py `
  --manifest C:\r\dawnstrike-r2-evidence\observer-inputs-2026-09-10\universe-manifest.json `
  --source-events C:\r\dawnstrike-r2-evidence\observer-inputs-2026-09-10\raw-events.jsonl `
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
  --manifest C:\r\dawnstrike-r2-evidence\observer-inputs-2026-09-10\universe-manifest.json `
  --source-events C:\r\dawnstrike-r2-evidence\observer-inputs-2026-09-10\raw-events.jsonl `
  --output-root C:\r\dawnstrike-r2-evidence\observer-2026-09-10 `
  --repository-root C:\r\dawnstrike-remediation-r2-20260909
```

The command is prepared and tested by this branch; no scheduler, heartbeat,
task registration, active runtime state, or provider credentials are changed
by it. A runner receipt is not evidence that the provider capture occurred.
The receipt must be joined to the existing provider capture receipt and its
source/configuration hashes by the independent R2 verifier.
