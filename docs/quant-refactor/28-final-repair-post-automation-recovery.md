# Final repair post-automation recovery decision

Date: 2026-08-16
Owner: Sol
Status: authorized bounded recovery after attributed external drift

## Material event

The final repair shard proof began from active-state SHA-256
`3ec2ffd2b83181ee14b918b88c87beac5c4831e28bd893e718d38f1acb69805c`.
After shard 15, the active database was:

- path: `C:\r\dawnstrike-state\shadow_real.sqlite`
- SHA-256: `733298401a4f3d1d57d459a2a28db45b2242bb2161a0b4f62dabef5289ada1fa`
- length: `198836224`
- mtime UTC: `2026-08-16T23:33:04.7531682Z`
- sidecars: none
- read-only probe: `query_only=1`, `quick_check=ok`

The sole current-timestamp database value is the replacement
`scenario_model_registry` row for
`dawnstrike-news-scenario-v1:gpt-5.6-terra`, with
`created_at=2026-08-16T23:33:04Z`, `UNCALIBRATED`, `sample_count=0`,
`research_only`, and `broker_execution_enabled=false`.

## Attribution

This drift is attributed to a separate Codex automation, not to an audit
shard:

- automation: `dawnstrike-daily-production-proof`
- session: `01a00ce9-8665-7da1-b410-a6251b6ef760`
- session artifact:
  `C:\Users\MattFields\.codex\sessions\2026\08\16\rollout-2026-08-16T18-30-26-01a00ce9-8665-7da1-b410-a6251b6ef760.jsonl`
- started: `2026-08-16T23:30:29Z`
- completed: `2026-08-16T23:36:24Z`
- authorized automation actions included a fresh runtime fetch and
  `scenario-doctor`
- runtime `.git\FETCH_HEAD` mtime UTC:
  `2026-08-16T23:32:07.6128851Z`
- `scenario-doctor` calls the production `_register_scenario_policy` path,
  which uses `INSERT OR REPLACE` and assigns `utc_now_iso()` to
  `scenario_model_registry.created_at`
- installed Windows Dawnstrike tasks did not run at the drift timestamp
- the nearby audit-shard PowerShell event was the read-only
  `scheduler-doctor` task inventory query

The automation completed before this recovery decision. The audit shard 05
process that survived its first interrupt was independently PID/parent-chain
verified and terminated. No shard/test process remains authorized from the
failed proof window.

## Frozen candidate retained

- source/test file count: `572`
- source/test aggregate SHA-256:
  `f6802f805f42af1414973ffdc69c61402199b04f6444a479af1c78510265eae0`
- canonical pytest inventory: `3156`
- inventory SHA-256:
  `2fd3ff0b4fb5c965d1fb3fbc4efd6789e66b54d84364484fb86764e1b229b8d9`
- sealed passing shards: `00-04` and `08-15`
- incomplete shards: `05-07`
- source/test edits remain forbidden

## Sol adjudication

The event is an external-writer collision with the audit window, not evidence
that the frozen candidate or a shard wrote the active database. Because the
writer is identified and terminal, the condition is locally recoverable
without modifying or restoring active state.

The prior pre-automation active-state baseline is superseded only for the
remaining proof window. Historical evidence is preserved and not rewritten.

## Luna recovery capsule

TASK:
Complete the exact frozen 16-shard proof after the attributed automation
collision.

WHY:
Shards 00-04 and 08-15 are sealed green against the exact frozen candidate;
05 was interrupted and 06-07 were not run. Completing those three shards
under a new post-automation state-invariance window is the shortest valid
path to final certification.

AUTHORITATIVE INPUTS:

- this decision
- `docs/quant-refactor/evidence/final-repair-completion-20260816/source-test-freeze.json`
- `docs/quant-refactor/evidence/final-repair-completion-20260816/canonical-pytest-inventory.json`
- shard evidence in that same directory
- active database path above

SCOPE:

- read-only repository and active-state inspection
- evidence-only writes under
  `docs/quant-refactor/evidence/final-repair-completion-20260816/`
- execute exact shards 05, 06, and 07 sequentially
- seal the combined 16-shard result and final active-state comparison

DO NOT:

- edit source or tests
- refresh frozen source/test or inventory identities
- restore, migrate, copy over, or write the active database
- use an operator write marker
- run providers, brokers, deployment, publication, or promotion
- stage, commit, push, or deploy
- reuse the incomplete shard-05 result as a pass

ACCEPTANCE:

1. No unauthorized shard/test process remains.
2. Fresh read-only active-state baseline is SHA
   `733298401a4f3d1d57d459a2a28db45b2242bb2161a0b4f62dabef5289ada1fa`,
   length `198836224`, mtime
   `2026-08-16T23:33:04.7531682Z`, schema 26, `query_only=1`,
   `quick_check=ok`, and zero sidecars.
3. Frozen source/test and inventory identities match exactly.
4. Shards 05, 06, and 07 each exit 0 with 197 selected/passed nodes and
   zero failed, skipped, xfailed, or xpassed nodes.
5. Existing shard 00-04 and 08-15 evidence revalidates with no missing or
   duplicate nodes across all 3156 selections.
6. Post-run active state exactly matches the fresh baseline.
7. A durable combined result and evidence manifest are written.

VERIFY:

- use the existing evidence wrapper and `scripts/run_pytest_shard.py`
- rehash all 572 frozen source/test files before and after
- validate all 16 shard manifests against the canonical inventory
- capture active state only with SQLite URI `mode=ro`,
  `PRAGMA query_only=ON`, and an immutable/read-only connection

REPAIR AUTHORITY:

Evidence-command retry only when the command itself did not start or an
evidence wrapper failed before pytest execution. No code, test, baseline, or
inventory repair is authorized.

ESCALATE ONLY IF:

- source/test or inventory hash drift occurs
- active state changes again
- any shard has a test failure or excluded outcome marker
- exact coverage has a missing or duplicate node

RETURN:

`PASS`, `HASH_DRIFT`, or `REPAIR_REQUIRED`, with exact counts, identities,
active-state comparison, evidence index, and manifest hash.
