# WP007 durable evidence packet

Date: 2026-08-16  
Candidate: `PASS_CANDIDATE_FOR_SOL_ADJUDICATION`  
Acceptance authority: Sol only

## Repository identity and scope

- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- Expected and observed unchanged HEAD:
  `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- WP007 source ownership is enumerated exactly in
  `modification-inventory.json`; content hashes and byte lengths are in
  `source-hashes.json`.
- Accepted WP001-WP006 dirty/untracked paths are separately enumerated as
  preserved pre-existing state. `app.py` ownership is restricted to the three
  opportunity imports and Today render call; its pre-existing missing-database
  fallback is excluded.

Every executed command is recorded verbatim in `<gate>.command.txt`. Raw
stdout and stderr are in `<gate>.stdout.txt` and `<gate>.stderr.txt`. UTC
start/end timestamps, elapsed seconds, exit code, and raw byte counts are in
`<gate>.exit.json`.

## Final execution gates

| Gate | Collection/result | Exit | Elapsed seconds |
|---|---:|---:|---:|
| Focused projection contract/store/render/static | 28/28 | 0 | 18.098 |
| Relevant public build/contract/semantics/DOM/stage | 36/36 | 0 | 28.502 |
| Existing rendered Streamlit compatibility | 12/12 | 0 | 8.551 |
| Accepted validation persistence | 15/15 | 0 | 2650.708 |
| Accepted validation robustness | 19/19 | 0 | 446.452 |
| Exact accepted WP005-B main command | 656/656 | 0 | 9811.088 |
| Exact accepted affected command | 139/139 | 0 | 123.948 |
| `py -m ruff check .` | All checks passed | 0 | see exit JSON |
| `py -m mypy intraday_scanner` | 318 source files | 0 | 1.806 |
| `py -m compileall -q intraday_scanner scripts` | clean | 0 | 0.425 |
| `node --check web/assets/dawnstrike.js` | clean | 0 | 0.114 |
| PowerShell parse of touched stage builder | clean | 0 | 0.377 |
| `git diff --check` | clean; line-ending warnings only | 0 | see exit JSON |
| AST/fresh-process import firewall | 77 files, 0 violations | 0 | 1.067 |

The main command ran from `2026-08-16T06:40:20.859726+00:00` through
`2026-08-16T09:23:52.129202+00:00`. The persistence command ran from
`2026-08-16T06:40:20.826338+00:00` through
`2026-08-16T07:24:31.582897+00:00`. All other exact UTC timestamps are in the
corresponding exit JSON.

## Independent collection reconciliation

| Inventory | Expected | Observed | Exit | Elapsed seconds |
|---|---:|---:|---:|---:|
| Focused | 28 | 28 | 0 | 3.207 |
| Public compatibility | 36 | 36 | 0 | 3.862 |
| Validation persistence | 15 | 15 | 0 | 2.169 |
| Validation robustness | 19 | 19 | 0 | 2.095 |
| Exact main | 656 | 656 | 0 | 3.349 |
| Exact affected | 139 | 139 | 0 | 6.964 |

Per-file collection counts are preserved in the six collection stdout files.
The exact accepted main and affected commands were copied from the WP006
evidence command files without changing their file lists or pytest flags. The
new WP007 tests are deliberately covered by the focused/public commands and do
not create unexplained drift in the exact 656/139 accepted inventories.

## Active-state read-only invariance

Before capture: `2026-08-16T06:40:02.045059+00:00`.  
After capture: `2026-08-16T09:24:14.001260+00:00`.

Both captures used SQLite URI `mode=ro`, confirmed `PRAGMA query_only=1`, and
returned `quick_check=ok`, schema `26`. Before and after values are identical:

- SHA-256:
  `78f4a39fb31f389c05ef7ab626a74f89f840243fa79ff678ac05bad8379f93e6`
- byte length: `198836224`
- mtime ns: `1786836849687280500`
- mtime UTC: `2026-08-15T23:34:09.687280+00:00`
- WAL/SHM/journal sidecars: none

The raw observations are in `active-state-before.stdout.txt` and
`active-state-after.stdout.txt`; the machine comparison is in
`active-state-invariance.json`.

## Repair and evidence-correction disclosure

- Implementation repair cycle 1: corrected three static typing issues in the
  new projection modules before final gates.
- Test/evidence correction cycle 1: changed one Streamlit rendered-title
  assertion from exact equality to a substring assertion matching the actual
  AppTest element shape.
- Test/evidence correction cycle 2: corrected the import-firewall allowlist for
  two accepted persistence modules whose storage dependency is intentional.

No accepted contract or runtime behavior changed during the test/evidence
corrections. Final gates were rerun after the applicable correction. No further
repair cycle was used.

## Limitations and prohibited claims

- Synthetic fixtures establish deterministic software, persistence replay,
  publication-integrity, and presentation safety only.
- No real locked OOS/holdout was opened or run, and no empirical edge or
  profitability is claimed.
- Active state is schema 26; an explicitly enabled adapter therefore reports
  honest `DATA_UNAVAILABLE` rather than migrating, writing, or claiming a
  correct no-trade state.
- The feature remains disabled by default and never authorizes TAKE, order
  routing, execution, promotion, or lifecycle mutation.
- No active-state write/migration, network/provider query, broker/order action,
  scheduler mutation, deployment, primary-checkout touch, commit, stage, push,
  or branch-history action occurred.

`processes.post.json` records the final gate-worker survivor check. The final
evidence manifest covers every evidence file other than the manifest and its
own SHA sidecar; `evidence-manifest.sha256` is the external seal.
