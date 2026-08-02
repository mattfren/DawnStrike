# AlphaOps V6 release runbook

## Preconditions

Do not promote a static build when any of these checks fail:

1. The clean source SHA has passed tests, lint, and type checking.
2. The durable SQLite backup checksum is recorded.
3. `config/web_sources.yaml` exists in the runtime, has a real accountable contact in its user agent, and passes `web-source-doctor`.
4. The source configuration is not the example file and no required source is merely a placeholder.
5. A dated, source-backed small-cap universe snapshot has been registered before the market session. Its source lineage, membership status, ticker history, and corporate-action fields must be complete; the JSON template is `config/alpha_v6_universe.production.template.json` and must never be used unchanged.
6. The production scheduler uses a dedicated password-logon Windows identity that can access the network, encrypted secrets, the runtime and durable state roots, starts when available, and does not stop/refuse runs on battery. S4U is prohibited: it has no network or encrypted-file access.

## First rehearsal

Run a dated, copy-on-write rehearsal with `--notify console`. Preserve every process receipt, source artifact, daily-run stage row, V6 decision, V6 outcome/terminal-missing receipt, and public artifact verifier result. A weekend or market-closed date may prove scheduler mechanics but does not prove live source collection.

Register the tasks only from the approved runtime, supplying the approved Windows identity interactively so its password never appears in shell history or source control:

```powershell
$credential = Get-Credential
.\scripts\register_alphaops_tasks.ps1 -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state -RunAsCredential $credential -ReplaceExisting
.\scripts\register_daily_finalize_task.ps1 -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state -RunAsCredential $credential -ReplaceExisting
py -m intraday_scanner.cli alpha-v6-preview-universe --db-path C:\r\dawnstrike-state\shadow_real.sqlite --input C:\r\dawnstrike-state\source-universe\alpha_v6_universe-YYYY-MM-DD.json
# Review the added, removed, and changed tickers. Copy preview_hash_sha256 exactly.
py -m intraday_scanner.cli alpha-v6-register-universe --db-path C:\r\dawnstrike-state\shadow_real.sqlite --input C:\r\dawnstrike-state\source-universe\alpha_v6_universe-YYYY-MM-DD.json --confirm-preview-hash <preview_hash_sha256>
py -m intraday_scanner.cli scheduler-doctor --root C:\r\dawnstrike-runtime --state-root C:\r\dawnstrike-state
```

## Required production proof

1. Run `scheduler-doctor`; its exit code must be zero and `status` must be `LOCAL_VERIFIED`.
2. Run the morning task on a market session. It must use the real source config; example fallback is prohibited.
3. Run EOD capture and V6 learning. Activated V6 outcomes require independent sourced SPY and IWM observations; missing data must create a terminal-missing or ineligible receipt, not a zero label.
4. Build the public artifact. `verify_public_artifact.py` must pass without `--allow-degraded`.
5. Verify preview and production health/readiness in a browser. Health alone is insufficient; readiness must be HTTP 200.
6. Confirm the deployed SHA and public calendar hash bind to the source SHA and canonical performance snapshot.

## Current blocked condition

The runtime currently has no `config/web_sources.yaml` or dated registered V6 universe snapshot. It is unsafe to copy either template because its source identity fields are placeholders. A production-contract scan now fails closed when any candidate lacks point-in-time universe membership. This is the only acceptable behavior; synthetic, placeholder, or changing-universe source truth must not start the learning loop.

The input's `source_lineage` must include a non-placeholder `source_id`,
ISO-8601 `retrieved_at`, `raw_artifact_sha256`, and
`configuration_hash_sha256`. Registration refuses a stale or altered preview;
the operator must review the exact diff first. If a source version is proven
bad, do not edit it: append an audited forward restore instead:

```powershell
py -m intraday_scanner.cli alpha-v6-restore-universe `
  --db-path C:\r\dawnstrike-state\shadow_real.sqlite `
  --universe-id <prior_universe_id> `
  --as-of YYYY-MM-DD `
  --operator operator@example.com `
  --reason "verified upstream constituent error"
```
