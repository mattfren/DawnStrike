# OPS-02 finite observation cohort

OPS-02 prepares a finite denominator of ten published XNYS sessions beginning
on 2026-09-10. The checked-in exchange calendar supplies each session identity,
UTC window, and early-close status. The cohort has no renewal path: after the
ten records are terminal, an operator must prepare a new explicitly reviewed
cohort.

Each session requires a fresh date-matched scope declaration containing the
actual date-bound mover census with producer completeness and source-count
proof, plus the fixed DIA/IWM/QQQ/SPY/TLT reference panel. Cardinality is
allowed to vary by day; rows are never padded, truncated, or silently dropped.
The full census is retained even when a source route only supplies the five
reference symbols. A deterministic stratified sample of at most twelve mover
symbols is recorded with inclusion probabilities for price-path availability;
sampling does not narrow the census or create eligibility. A source receipt
arriving after the decision deadline is recorded as degraded and remains
ineligible for timely decisions.

The coordinator consumes the existing separately scheduled SIP capture route.
It does not contact a provider, register a scheduled task, mutate active
runtime/trading state, or renew itself. `--execute` only binds already retained
receipt/state/page artifacts through the checked-in producer and observer.
Corporate-action-dependent labels remain ineligible until the entitlement
receipt proves that endpoint.

The capture service's hard provider page limit remains 100. The cohort's
`max-pages=1000` setting is an offline observer/derivative bound only; it does
not expand provider requests. The September 9 development replay used a
separate offline diagnostic override of 10000 pages solely because its retained
10,000-event derivative exceeds the normal local page bound.

## Actual source binding

The existing retained September 9 receipt/state and the mover handoff,
premarket snapshot, enrichment audit, ranked watch, and avoid artifacts are
bound through read-only pointers into an isolated development date folder. The
adapter verifies every source hash and date, records producer completeness, and
does not copy or rewrite retained raw artifacts. This command is development
evidence for September 9 only; it cannot satisfy a September 10 prospective
session:

```powershell
& 'C:\Program Files\Dawnstrike\Python313\python.exe' 'C:\r\dawnstrike-ops02-cohort-20260910\scripts\prepare_r2_cohort_inputs.py' `
  --handoff 'C:\r\dawnstrike-state\outputs\alpha_cycle\2026-09-09\paperops_universe_handoff.json' `
  --snapshot 'C:\r\dawnstrike-state\outputs\alpha_cycle\2026-09-09\web_collect\premarket_snapshot.csv' `
  --enriched 'C:\r\dawnstrike-state\outputs\alpha_cycle\2026-09-09\premarket_enrichment\premarket_snapshot_enriched_rows_audit.csv' `
  --ranked 'C:\r\dawnstrike-state\outputs\alpha_cycle\2026-09-09\scan\ranked_candidates.csv' `
  --avoid 'C:\r\dawnstrike-state\outputs\alpha_cycle\2026-09-09\scan\avoid_list.csv' `
  --capture-receipt 'C:\r\dawnstrike-forward-runs\forward_observed\6a4fd8458e017b746776399edcbbf6c4\capture_run_receipt.json' `
  --output-root 'C:\r\dawnstrike-ops02-inputs-20260910\development' `
  --market-date 2026-09-09
```

The resulting `capture-source.json` points to the retained receipt and its
exact state hash. The resulting `scope.json` carries a variable source count,
complete producer proof, and the five-symbol reference panel. A missing mover
census does not prevent a truthful fixed-panel raw capture; it keeps mover
decisions in `MISSING_DEPENDENCY` and panel coverage separately degraded.

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

Prepare the observer-only process configuration after the plan is written. It
records the existing capture-task completion trigger and exact finite command;
it does not register a Windows task or start a process:

```powershell
& 'C:\Program Files\Dawnstrike\Python313\python.exe' 'C:\r\dawnstrike-ops02-cohort-20260910\scripts\prepare_r2_cohort_job.py' `
  --plan 'C:\r\dawnstrike-program-verification-20260909\OPS02\cohort\cohort-plan.json' `
  --output 'C:\r\dawnstrike-program-verification-20260909\OPS02\cohort\job-config.json' `
  --repo-root 'C:\r\dawnstrike-ops02-cohort-20260910' `
  --runner 'C:\r\dawnstrike-ops02-cohort-20260910\scripts\run_r2_cohort.py'
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
