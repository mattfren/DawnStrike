# OPS-02 finite observation cohort

OPS-02 prepares a finite denominator of ten published XNYS sessions beginning
on 2026-09-10. The checked-in exchange calendar supplies each session identity,
UTC window, and early-close status. The cohort has no renewal path: after the
ten records are terminal, an operator must prepare a new explicitly reviewed
cohort.

Each session requires a fresh date-matched scope declaration containing the
181 mover census and the fixed DIA/IWM/QQQ/SPY/TLT reference panel. The full
census is retained even when a source route only supplies the five reference
symbols. A deterministic stratified sample of at most twelve mover symbols is
recorded with inclusion probabilities for price-path availability; sampling
does not narrow the census or create eligibility. A source receipt arriving
after the decision deadline is recorded as degraded and remains ineligible for
timely decisions.

The coordinator consumes the existing separately scheduled SIP capture route.
It does not contact a provider, register a scheduled task, mutate active
runtime/trading state, or renew itself. `--execute` only binds already retained
receipt/state/page artifacts through the checked-in producer and observer.
Corporate-action-dependent labels remain ineligible until the entitlement
receipt proves that endpoint.

## Prepare

The following command writes the durable plan and state under the isolated
program-verification root. The future per-session inputs are expected at
`C:\r\dawnstrike-ops02-inputs-20260910\scopes\<YYYY-MM-DD>\scope.json` and
`C:\r\dawnstrike-ops02-inputs-20260910\captures\<YYYY-MM-DD>\capture_run_receipt.json`.

```powershell
& 'C:\Program Files\Dawnstrike\Python313\python.exe' 'C:\r\dawnstrike-ops02-cohort-20260910\scripts\run_r2_cohort.py' prepare `
  --output-root 'C:\r\dawnstrike-program-verification-20260909\OPS02\cohort' `
  --input-root 'C:\r\dawnstrike-ops02-inputs-20260910\captures' `
  --scope-root 'C:\r\dawnstrike-ops02-inputs-20260910\scopes' `
  --repo-root 'C:\r\dawnstrike-ops02-cohort-20260910' `
  --python 'C:\Program Files\Dawnstrike\Python313\python.exe' `
  --start-date 2026-09-10 `
  --source-config-sha256 57b70a84187844789cb8a92684fbac9690e1abf50dac275b2c570fecdeb79b28 `
  --entitlement-receipt 'C:\r\dawnstrike-capture-config-20260830\alpaca-sip-entitlement-receipt.json' `
  --runtime-env 'C:\r\dawnstrike-state\secrets\runtime.env' `
  --max-pages 1000 --max-events 10000 --max-bytes 67108864 `
  --max-rss-bytes 268435456 --max-wall-seconds 1800
```

## Start, stop, and resume

After independent source-safeguard approval, the smallest supported observer
process is the following manual command. It has a fixed ten-session horizon.

```powershell
& 'C:\Program Files\Dawnstrike\Python313\python.exe' 'C:\r\dawnstrike-ops02-cohort-20260910\scripts\run_r2_cohort.py' resume `
  --output-root 'C:\r\dawnstrike-program-verification-20260909\OPS02\cohort' `
  --input-root 'C:\r\dawnstrike-ops02-inputs-20260910\captures' `
  --scope-root 'C:\r\dawnstrike-ops02-inputs-20260910\scopes' `
  --repo-root 'C:\r\dawnstrike-ops02-cohort-20260910' `
  --python 'C:\Program Files\Dawnstrike\Python313\python.exe' `
  --execute
```

Request a cooperative stop by creating the durable marker, then wait for the
process to exit:

```powershell
New-Item -ItemType File -Force 'C:\r\dawnstrike-program-verification-20260909\OPS02\cohort\.cohort.stop' | Out-Null
```

Resume with the same command after the process has exited and the marker has
been removed:

```powershell
& 'C:\Program Files\Dawnstrike\Python313\python.exe' -c "from pathlib import Path; Path(r'C:\r\dawnstrike-program-verification-20260909\OPS02\cohort\.cohort.stop').unlink(missing_ok=True)"
& 'C:\Program Files\Dawnstrike\Python313\python.exe' 'C:\r\dawnstrike-ops02-cohort-20260910\scripts\run_r2_cohort.py' resume `
  --output-root 'C:\r\dawnstrike-program-verification-20260909\OPS02\cohort' `
  --input-root 'C:\r\dawnstrike-ops02-inputs-20260910\captures' `
  --scope-root 'C:\r\dawnstrike-ops02-inputs-20260910\scopes' `
  --repo-root 'C:\r\dawnstrike-ops02-cohort-20260910' `
  --python 'C:\Program Files\Dawnstrike\Python313\python.exe' `
  --execute
```

No task registration or heartbeat is created by these commands.
