# AlphaOps V6 release runbook

## Preconditions

Do not promote a static build when any of these checks fail:

1. The clean source SHA has passed tests, lint, and type checking.
2. The durable SQLite backup checksum is recorded.
3. `C:\r\dawnstrike-state\config\web_sources.yaml` exists in durable state, has a real accountable contact in its user agent, and passes semantic contract validation. Runtime-local fallback is prohibited.
4. The source configuration is not the example file and no required source is merely a placeholder.
5. A dated, source-backed small-cap universe snapshot has been registered before the market session. Build its review candidate from the approved source contract and exact recorded raw artifact; hand-written universe JSON is not accepted. Its source lineage, membership status, ticker history, and corporate-action fields must be complete.
6. The production scheduler uses a dedicated password-logon Windows identity that can access the network, encrypted secrets, the runtime and durable state roots, starts when available, and does not stop/refuse runs on battery. S4U is prohibited: it has no network or encrypted-file access.

## Alpaca forward-only mode

When no licensed point-in-time universe is available, prospective paper
learning may start without pretending to validate historical strategy edge:

1. Copy `config\web_sources.forward_alpaca.template.yaml` to the durable private
   path `C:\r\dawnstrike-state\config\web_sources.yaml`; replace
   `REQUIRED_ACCOUNTABLE_EMAIL` with the operator contact. The morning runner
   reads this state-owned file exclusively so clean runtime cutovers cannot remove
   it or silently substitute an ignored checkout file.
2. Add `INTRADAY_OUTCOME_CAPTURE_PROVIDER_ORDER=alpaca,yahoo` to the private
   runtime environment. Alpaca is the read-only primary outcome source; Yahoo
   is retained solely for bounded independent reconciliation.
   AlphaOps premarket enrichment follows the same order: Alpaca IEX is queried
   first. A systemic Alpaca/auth/network failure blocks the cycle. Yahoo is used
   only for bounded symbol-specific missing/stale IEX bars after successful
   Alpaca requests. If symbol-level fallback demand exceeds the 25% ceiling,
   Yahoo is not called and the entire cycle is labeled
   `DATA_INELIGIBLE` even when a few Alpaca rows were usable. The Telegram
   message must report the verified fraction and must not call incomplete
   coverage a valid market no-edge result. Systemic Alpaca
   authentication, network, or HTTP failures still fail the cycle closed.
   The ranking snapshot contains only verified rows and carries the market-data
   provider, source URL, completion timestamp, fallback state, and immutable
   observation hash into candidates and field-level lineage. The separate
   source-row audit snapshot preserves the pre-enrichment values unchanged.
   Fixture configurations use an explicit rehearsal-only, non-learning ranking
   policy and never fetch live market data.
   Every row records the provider that supplied its range; the run summary
   reports fallback count and ratio. Intraday monitoring remains Alpaca and
   fails closed when no fresh, complete IEX bar exists.
   IEX is a single-exchange testing feed and is not adequate as the sole source
   for small-cap premarket selection. For production-quality current coverage,
   subscribe the Alpaca account to real-time SIP and set `ALPACA_DATA_FEED=sip`.
   If SIP is unavailable, keep the cycle data-ineligible and add an independent
   consolidated provider adapter; do not raise the Yahoo fallback ceiling.
3. Keep `production_contract: false`. The doctor reports this explicitly as
   `FORWARD_RESEARCH_ONLY`; it is never labeled a historical production
   contract. This configuration must not register a
   V6 point-in-time universe, backfill historical performance, or claim model
   superiority. Missing or conflicting source evidence remains ineligible.

The daily ledger produced in this mode is a prospective research record only;
it does not loosen any no-order-execution boundary.

## First rehearsal

Run a dated, copy-on-write rehearsal with `--notify console`. Preserve every process receipt, source artifact, daily-run stage row, V6 decision, V6 outcome/terminal-missing receipt, and public artifact verifier result. A weekend or market-closed date may prove scheduler mechanics but does not prove live source collection.

Register the tasks only from the approved runtime, supplying the approved Windows identity interactively so its password never appears in shell history or source control:

```powershell
$credential = Get-Credential
.\scripts\register_alphaops_tasks.ps1 -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state -RunAsCredential $credential -ReplaceExisting
.\scripts\register_daily_finalize_task.ps1 -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state -RunAsCredential $credential -ReplaceExisting
py -m intraday_scanner.cli alpha-v6-build-universe --source-contract C:\r\dawnstrike-state\source-universe\alpha_v6_source-contract.json --raw-artifact C:\r\dawnstrike-state\source-universe\alpha_v6_raw-YYYY-MM-DD.json --out C:\r\dawnstrike-state\source-universe\alpha_v6_candidate-YYYY-MM-DD.json

py -m intraday_scanner.cli alpha-v6-preview-universe --db-path C:\r\dawnstrike-state\shadow_real.sqlite --input C:\r\dawnstrike-state\source-universe\alpha_v6_candidate-YYYY-MM-DD.json
# Review the added, removed, and changed tickers. Copy preview_hash_sha256 exactly.
py -m intraday_scanner.cli alpha-v6-register-universe --db-path C:\r\dawnstrike-state\shadow_real.sqlite --input C:\r\dawnstrike-state\source-universe\alpha_v6_candidate-YYYY-MM-DD.json --source-contract C:\r\dawnstrike-state\source-universe\alpha_v6_source-contract.json --raw-artifact C:\r\dawnstrike-state\source-universe\alpha_v6_raw-YYYY-MM-DD.json --confirm-preview-hash <preview_hash_sha256>
py -m intraday_scanner.cli scheduler-doctor --root C:\r\dawnstrike-runtime --state-root C:\r\dawnstrike-state
```

`ReuseExistingPrincipal` is only valid for an approved Windows service account.
Windows requires `RunAsCredential` again when any password-logon task definition
changes; the registration script fails before mutation if that credential is
omitted. For a service-account installation only:

```powershell
.\scripts\register_alphaops_tasks.ps1 -RuntimeRoot C:\r\dawnstrike-runtime -StateRoot C:\r\dawnstrike-state -ReuseExistingPrincipal
```

## Required production proof

1. Run `scheduler-doctor`; its exit code must be zero and `status` must be `LOCAL_VERIFIED`.
   The doctor validates each configured trigger start boundary, not the moving
   next-run timestamp of a repeating task. A failed last-run result is ignored
   only when its timestamp provably predates the current trigger definition.
2. Run the morning task on a market session. It must use the real source config; example fallback is prohibited.
3. Run EOD capture and V6 learning. Activated V6 outcomes require independent sourced SPY and IWM observations; missing data must create a terminal-missing or ineligible receipt, not a zero label.
4. Build the public artifact. `verify_public_artifact.py` must pass without `--allow-degraded`.
5. Verify preview and production health/readiness in a browser. Health alone is insufficient; readiness must be HTTP 200.
6. Confirm the deployed SHA and public calendar hash bind to the source SHA and canonical performance snapshot.

## Forward-only boundary

The durable forward-only source contract permits prospective paper evidence but
does not create a dated historical V6 universe snapshot. A production-contract
scan still fails closed when any candidate lacks point-in-time universe
membership. Synthetic, placeholder, or changing-universe source truth must not
start a historical learning loop.

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
