# Dirty transfer manifest

Source checkout: `C:\Users\MattFields\Dawnstrike`  
Source branch/SHA: `codex/command-center-x3-vercel` /
`67f02726c915aad7ce5a857567d3d3fcc1b0bf98`  
Target worktree: `C:\r\dawnstrike-10of10-20260729`  
Target branch/SHA: `codex/dawnstrike-10of10` /
`51f79ff2a738110b486111d85c4d93cfda9f4ec8`

Latest clean implementation/build source SHA used for production:
`51f79ff2a738110b486111d85c4d93cfda9f4ec8`. The verified preview is
`dpl_CgpNe75UctboW7BavVTZWqLH7wQG`; production is
`dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6`.

The candidate was created from clean `origin/main`. New remediation modules
are authored here. One shared migration file was ported intentionally because
the source checkout contains the current raw-table migration history; it was
then extended with additive canonical schema migrations and did not copy the
dirty application/runtime surface.

| Source | Source state | Target action | Reason | Proof required |
|---|---|---|---|---|
| `docs/roadmap/dawnstrike_10_of_10_luna_execution_directive_2026-07-29.md` | Untracked, SHA-256 `E3E0DA843C0E5CEE80CDAA5B3682BD2F344B7FF76EE9D87289DAF5A68223EA2A` | Copied byte-identically into target docs | Controlling execution directive | Source/target hash match; read fully before implementation |
| `intraday_scanner/public_data/` | Untracked; four source `.py` files plus generated `__pycache__` | Pending review; port source only if required by local pipeline | Current production readiness import failure references this package | Import tests, Vercel stage exclusion proof, no cache files |
| `scripts/publish_x3_daily.ps1` | Untracked | Do not port as-is; replace with controlled finalize/publish flow | Current script builds from dirty state then stages `git archive HEAD` | New publisher tests, exact-SHA stage, failure gate |
| `api/readiness.py` | Tracked baseline file; current shared checkout not modified | Refactor in target after canonical manifest exists | Current production endpoint imports the full scanner runtime and returns 500 | Readiness 200/503 contract tests and preview proof |
| `api/health.py` | Tracked baseline file; current shared checkout not modified | Refactor in target | Current health endpoint falsely implies readiness | Liveness/readiness separation tests |
| `vercel_dawnstrike/runtime.py` | Tracked baseline file; current shared checkout not modified | Remove from public deployment dependency; preserve local history until cutover | Current runtime bundles heavy dependencies and runs ephemeral scanner logic | Minimal stage allowlist and bundle inspection |
| `vercel.json` | Tracked baseline file; current shared checkout not modified | Replace UI rewrites and remove duplicate Vercel research crons | Static UI is routed through Python and Vercel automation is not local DB automation | Static route, cron, preview, and readiness proof |
| `data/v2_command_center_x3/**` | Generated/dirty artifact family | Do not transfer as the new product source | Current X3 mixes stale and current cohorts | New canonical snapshot and UI parity proof |
| `intraday_scanner/storage/migrations.py` | Dirty shared implementation; copied source file, then extended in target | Port migration history only; no runtime/UI code | Canonical tables must be additive and compatible with existing raw tables | Schema migration tests; clean target diff; no X3 runtime dependency |

## Candidate-authored surface

The following files were authored in the isolated worktree rather than copied
from the dirty checkout: `intraday_scanner/performance/**` including the
PaperOps adapter, `intraday_scanner/services/scheduler_doctor_service.py`,
`intraday_scanner/risk/**`, `intraday_scanner/services/daily_finalize_service.py`,
`scripts/build_public.py`, `scripts/verify_public_artifact.py`,
`scripts/run_daily_finalize.ps1`, `scripts/register_daily_finalize_task.ps1`,
`scripts/build_vercel_public_stage.ps1`,
`api/health.py`, `api/readiness.py`, `web/**`, and the remediation tests/docs.
Their source-level proof is recorded by the focused test and browser evidence
files in this directory.

## Explicit exclusions

- Do not transfer `__pycache__` or generated caches.
- Do not transfer `data/shadow_real.sqlite` into the deployment stage.
- Do not transfer `logs/`, private outputs, `.env` values, or Telegram secrets.
- Do not transfer the full dirty checkout.
- Do not copy historical X/X2/X3/Apex HTML bundles into the new public build.
- Do not transfer `tests/test_v2_command_center_x3.py`,
  `tests/test_command_center_x3_official_fleet_truth.py`, or
  `intraday_scanner/v2/command_center_x3/qa.py`: these files exist only in the
  dirty shared fallback, import the absent legacy X3 runtime, and are not part
  of clean `origin/main`. The replacement static UI, semantic tests, rendered
  browser proof, and artifact gates cover the candidate release surface.
- Do not register another publication task. After explicit owner approval, the
  single replacement task was registered from this isolated, clean worktree
  and the legacy X3 publisher remained disabled.
- Do not point persistence-enabled builds at the shared database. The isolated
  candidate's fresh build accidentally wrote derived canonical rows and one
  console notification to the shared database. The owner approved retaining
  and auditing that derived state; no further shared-DB writes are allowed.

## Transfer protocol

For every future transfer, append:

1. source path and exact source SHA;
2. tracked/untracked status;
3. diff summary;
4. destination path;
5. publication/data impact;
6. focused tests;
7. reviewer/phase evidence;
8. final target hash where applicable.

## Continuation transfer additions — 2026-07-30

| Target | Source | State | Boundary |
|---|---|---|---|
| `scripts/run_alphaops_eod_full.bat` | Existing shared AlphaOps EOD runner | Owned wrapper added | Requires explicit `SourceRoot`; records exit status and dated stage receipt; refuses recursion |
| `scripts/run_alphaops_monitor_full.bat` | Existing shared AlphaOps monitor runner | Owned wrapper added | Requires explicit `SourceRoot`; refuses recursion |
| `scripts/record_automation_stage.py` | New candidate code | Added | SQLite receipt only; no synthetic market data and no broker actions |
| `scripts/send_daily_finalize_notification.py` | New candidate code | Added | Optional Telegram delivery from environment secrets; idempotent DB receipt; absent secrets remain visible as `not_configured` |
