# Quant Refactor Recovery Ledger

Last updated: 2026-08-17

## Recovery anchor

- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- HEAD: `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- This ledger records recovery state only. It does not replace Sol adjudication or certify completion.

## Accepted audit history

| Audit records | Work package | Recovery status |
|---|---|---|
| `05` | WP001 | Accepted |
| `06`-`09` | WP002 A/B1/B2/B3 | Accepted |
| `10`-`12` | WP003 A/B/C | Accepted |
| `13`-`15` | WP004 A/B/C | Accepted |
| `16` | WP005-A | Accepted |
| `17` | WP005-B | Accepted |
| `19` | WP005-C | Accepted |
| `21` | WP006 | Accepted |
| `23` | WP007 | Accepted |

## WP002 legacy full gate

- Terminal result: `1837 passed`
- Exit code: `0`
- Duration: `10536.170s`
- Log: `C:\Users\MattFields\AppData\Local\Temp\dawnstrike-wp002-full-20260812-181225.log`
- Log SHA-256: `5FC71C01B0FFC3B97566A13217DB63AE32D04024212A70898A281E4F93C08C27`
- Source and test hashes were frozen for the gate.
- Limitation: documentation overlap prevents treating this as a whole-worktree cleanliness or immutability claim.

## WP005-B recovery state

Status: **Accepted by Sol on 2026-08-15.**

Completed repair scope:

- Exact `Decimal` context handling landed.
- Integer-microsecond duration handling landed.
- Independent Luna supplied-evidence review: `CLEAN`.
- Narrow gate: `2 passed`, exit `0`, `241.3s`.
- Metric gate: `35 passed`, exit `0`, `1577.3s`.
- Five-file gate: `389 passed`, exit `0`, `2226.5s`.
- Focused Ruff, seven-target mypy, compile, and diff checks: clean.
- Durable broad gate: `656 passed`, with zero failures, skips, xfails, or xpasses; exit `0`; `9102.9s`.
- Affected regression gate: `139 passed`, exit `0`, `135.9s`.
- Durable packet: `docs/quant-refactor/wp005-b-durable-gate-20260815.md`.
- Fresh repository-durable packet:
  `docs/quant-refactor/evidence/wp005-b-20260815/evidence-packet.md`.
- Sol adjudication: `docs/quant-refactor/17-wp005-increment-b-sol-audit.md`.
- Repair cycles before the final frozen gate: `1`.
- Gate-time modifications: none.
- Pre/post source hashes were identical.
- The existing dirty tree was expected and shared; no unauthorized gate-time mutation was observed.
- No stale worker lease remains; the gate leases were released after terminal completion.

The evidence packet is reconstructed from captured terminal tool output because the original direct gate did not write a separate redirected stdout log. It must not be represented as a raw stdout-file artifact.

## Preserved next work

- WP005-C is accepted in
  `docs/quant-refactor/19-wp005-increment-c-sol-audit.md`.
- WP006 is accepted in
  `docs/quant-refactor/21-wp006-sol-audit.md`.
- WP007 is accepted in
  `docs/quant-refactor/23-wp007-sol-audit.md`.
- All planned implementation work packages are accepted.
- Final audit is pending.
- Final certification is pending.

## Current recovery boundary

The accepted audit chain through WP007 is preserved. The next and only
critical-path package is one fresh-context, read-only independent final audit.
No final audit or certification is claimed complete.

## Live recovery inspection - 2026-08-15

- A bounded `Win32_Process` inspection found no Dawnstrike-related `python` or
  `pytest` process. The legacy WP002 gate therefore has no surviving process or
  legitimate lease owner.
- The legacy WP002 log still exists at
  `C:\Users\MattFields\AppData\Local\Temp\dawnstrike-wp002-full-20260812-181225.log`
  and still hashes to
  `5FC71C01B0FFC3B97566A13217DB63AE32D04024212A70898A281E4F93C08C27`.
- The worktree remains the isolated
  `C:\r\dawnstrike-quant-refactor-20260811` lane on
  `codex/sol-quant-refactor-20260811` at
  `bec32fe752b91f4e1357236a538a6dfea5da56bf`. Its dirty state is the preserved
  accepted implementation chain, not a clean-tree claim.
- Every current WP005-B source/test hash matches the frozen values recorded in
  `wp005-b-durable-gate-20260815.md`.
- The older DS-Heavy raw gate path is an empty file, and its post-hash and exit
  artifacts are absent. The reconstructed packet is useful history but does
  not satisfy the new controlling brief's repository-relative raw-log
  requirement by itself.
- Recovery decision: legacy WP002 ownership is released. One fresh-context
  Luna owner may acquire the WP005-B durable verification lease and reproduce
  the unchanged frozen gate into repository-durable evidence. WP005-C remains
  paused until Sol adjudicates that evidence.

## Final immutable recovery gate - 2026-08-17

- The post-repair source/test candidate froze at 572 files with aggregate
  SHA-256 `1a37b90920ec480a16a6b453575f1a46841324243cc84c529b3c9331db3a0f07`.
- Canonical pytest inventory remained exactly 3,156 unique nodes with SHA-256
  `2fd3ff0b4fb5c965d1fb3fbc4efd6789e66b54d84364484fb86764e1b229b8d9`.
- Two disjoint gate owners ran shards 00-07 and 08-15. All 3,156 nodes passed
  exactly once; failures, skips, xfails, xpasses, missing nodes, and duplicates
  were all zero.
- The active SQLite state stayed byte-identical at SHA-256
  `733298401a4f3d1d57d459a2a28db45b2242bb2161a0b4f62dabef5289ada1fa`,
  length 198,836,224, schema 26, `query_only=1`, `quick_check=ok`, and zero
  sidecars. All inspection used immutable read-only mode.
- The earlier state change was attributed to the separate authorized Codex
  `dawnstrike-daily-production-proof` automation, not a shard/test process.
  The fresh gate began after that automation completed and observed no drift.
- Durable result:
  `docs/quant-refactor/evidence/final-immutable-gate-20260817/final-combined-result.json`.
- Evidence manifest SHA-256:
  `2ce2a041d064492624e81bad9b3643dca6209772d43f6b7ff78474a99e4c2683`
  across 127 validated entries.
- Recovery status: `PASS`. Commit binding and a fresh independent post-commit
  audit remain the only certification steps.
