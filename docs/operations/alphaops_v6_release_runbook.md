# AlphaOps V6 release runbook

## Preconditions

Do not promote a static build when any of these checks fail:

1. The clean source SHA has passed tests, lint, and type checking.
2. The durable SQLite backup checksum is recorded.
3. `config/web_sources.yaml` exists in the runtime, has a real accountable contact in its user agent, and passes `web-source-doctor`.
4. The source configuration is not the example file and no required source is merely a placeholder.
5. The production scheduler uses a dedicated password-logon Windows identity that can access the network, encrypted secrets, the runtime and durable state roots, starts when available, and does not stop/refuse runs on battery. S4U is prohibited: it has no network or encrypted-file access.

## First rehearsal

Run a dated, copy-on-write rehearsal with `--notify console`. Preserve every process receipt, source artifact, daily-run stage row, V6 decision, V6 outcome/terminal-missing receipt, and public artifact verifier result. A weekend or market-closed date may prove scheduler mechanics but does not prove live source collection.

Register the tasks only from the approved runtime, supplying the approved Windows identity interactively so its password never appears in shell history or source control:

```powershell
$credential = Get-Credential
.\scripts\register_alphaops_tasks.ps1 -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state -RunAsCredential $credential -ReplaceExisting
.\scripts\register_daily_finalize_task.ps1 -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state -RunAsCredential $credential -ReplaceExisting
py -m intraday_scanner.cli scheduler-doctor --root C:\r\dawnstrike-runtime --state-root C:\r\dawnstrike-state
```

## Required production proof

1. Run `scheduler-doctor`; its exit code must be zero and `status` must be `LOCAL_VERIFIED`.
2. Run the morning task on a market session. It must use the real source config; example fallback is prohibited.
3. Run EOD capture and V6 learning. Missing data must create a terminal-missing receipt, not a zero label.
4. Build the public artifact. `verify_public_artifact.py` must pass without `--allow-degraded`.
5. Verify preview and production health/readiness in a browser. Health alone is insufficient; readiness must be HTTP 200.
6. Confirm the deployed SHA and public calendar hash bind to the source SHA and canonical performance snapshot.

## Current blocked condition

The runtime currently has no `config/web_sources.yaml`. It is unsafe to copy the example because its user-agent contact is a placeholder and several providers are intentionally disabled. The morning runner now fails closed with `source_config_missing` until a real configuration is supplied. This is the only acceptable behavior; synthetic or placeholder source truth must not start the learning loop.
