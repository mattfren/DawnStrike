# Intraday capture operations

`capture_intraday_operations.py` is the fail-closed wrapper for the existing
read-only provider capture. It has two explicit modes:

- `retrospective_research`: historical research evidence, never forward truth.
- `forward_observed`: delayed-SIP observations for the governed paper account.

Both modes require an exact candidate SHA, frozen symbol-manifest hash,
authoritative NYSE session/window, source-config hash, and hashed operator
entitlement receipt. The wrapper rejects `iex`, provider substitution, recent
windows, repository/runtime/state output roots, overlapping evidence roots, and
unbounded retries/pages. It writes mode-separated resumable runs and only a
sanitized result receipt. Broker execution is disabled.

## Safe readiness invocation

Run this from the exact candidate runtime. The command is preview-only unless
`--execute` is supplied:

```powershell
py -3.13 scripts/capture_intraday_doctor.py `
  --mode forward_observed --provider alpaca --feed sip `
  --candidate-sha <exact-candidate-sha> --repo-root C:\r\dawnstrike-runtime `
  --db-path C:\r\dawnstrike-forward-evidence\staging.sqlite `
  --evidence-root C:\r\dawnstrike-forward-evidence `
  --run-root C:\r\dawnstrike-forward-evidence\runs `
  --output-root C:\r\dawnstrike-forward-evidence\evidence `
  --symbols-manifest C:\r\dawnstrike-forward-evidence\symbols.json `
  --symbols-manifest-sha256 <manifest-sha256> `
  --expected-session C:\r\dawnstrike-forward-evidence\expected-session.json `
  --entitlement-receipt C:\r\dawnstrike-state\receipts\sip-entitlement.json `
  --entitlement-receipt-sha256 <receipt-sha256> `
  --source-config C:\r\dawnstrike-state\config\web_sources.yaml `
  --source-config-sha256 <source-config-sha256> `
  --env-file C:\r\dawnstrike-state\secrets\runtime.env
```

The scheduled task registration script is preview-only by default. Add
`-Create` only after the exact candidate has passed the doctor and runtime
cutover gates. It schedules delayed SIP capture at 15:20 Central on weekdays,
which is 16:20 Eastern, and does not modify existing Dawnstrike tasks. No
credentials or API keys are task arguments; credentials are loaded by the
runtime environment file at execution time.
