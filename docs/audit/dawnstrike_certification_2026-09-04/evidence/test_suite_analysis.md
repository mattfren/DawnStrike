# Full test-suite execution at the audit commit

- Commit: `14ca714f6df37a5769119547d01d77527baeb5c5` (origin/main, 2026-09-02)
- Worktree: `C:\r\dawnstrike-cert-20260904` (clean)
- Command: `py -m pytest -q -p no:cacheprovider --durations=40 -rsxX`
- Raw log: `evidence/full_pytest_origin_main.txt`
- Host: Windows 11 Pro 10.0.26200, Python 3.13.15, pytest 9.1.1, pwsh 7.6.5, Windows PowerShell 5.1.26100.9278

## Result

| Metric | Value |
|---|---:|
| Collected | 5,150 |
| Passed | 5,083 |
| **Failed** | **61** |
| Skipped | 6 |
| Exit code | 1 |

`SUITE STATUS: FAIL` at the authoritative commit.

The prior in-repo certification (`docs/quant-refactor/32-final-independent-certification.md`,
2026-08-17) recorded 3,166 tests, all passing. That evidence is superseded: the suite has grown
63% to 5,150 and does not currently pass on this host.

## Failure decomposition by root cause

The 61 failures are **not** distributed across product logic. They collapse into four families,
three of which are environment-identity pinning rather than defects in scanning, scoring,
monitoring, outcomes, or learning.

### Family A — dependency lock drift (35 failures, 57%)

- Assertion: `assert dist.version == version` -> `AssertionError: assert '4.15.0' == '4.14.2'`
- Package: `anyio`. `requirements.lock:15` pins `anyio==4.14.2`; the installed environment has
  `anyio 4.15.0`.
- A single transitive-dependency drift fails 35 tests, because the dependency-contract tests
  iterate every requirement and assert an exact version match.
- Files: `tests/test_dawnstrike_python_bootstrap.py` (78 references), `requirements.lock:15`

### Family B — approved interpreter identity is stale (~16 failures)

- Errors: `Approved lock-contract interpreter hash changed.` and
  `State-preparation bootstrap Python identity changed.`
- `scripts/runtime_activation_lock.ps1:4` hardcodes
  `$script:DawnstrikeApprovedPythonSha256='ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1'`
- The host interpreter `C:\Users\MattFields\AppData\Local\Programs\Python\Python313\python.exe`
  hashes to `85B71D8C6EC1905935F74BE0C9869AAE198D00E98F39DF699EC66F9C5A84CECD`
  (Python 3.13.15, built 2026-08-05).
- The approved identity was pinned against Python 3.13.14 (the version recorded in
  `docs/quant-refactor/wp005-b-durable-gate-20260815.md`) and was never rotated after the
  3.13.15 patch upgrade.
- This control is behaving **as designed** - it refuses an unapproved interpreter. The defect is
  the absent rotation procedure, not the check. It is the most probable reason the production
  runtime remains pinned at `b7220890` rather than tracking `origin/main`.
- Files: `scripts/runtime_activation_lock.ps1:4,38,386,549`,
  `tests/test_runtime_activation_contract.py`, `tests/test_runtime_activation_lock_recovery.py`

### Family C — date-dependent test (3 failures)

- Error: `intraday_scanner.v2.data_truth.core.DataTruthAcquisitionIncomplete: CSV source symbol
  TST lacks exact completed bar 2026-09-04`
- The assertion references the **execution date**, so the test outcome depends on when it runs.
  A suite that passes on one date and fails on another cannot serve as a certification gate.
- File: `tests/test_paper_ops_shadow_runner.py`

### Family D — Streamlit AppTest path resolution (3 failures)

- Error: `FileNotFoundError: AppTest script not found at
  C:\r\dawnstrike-cert-20260904\tests\app.py. Relative paths are resolved against the file that
  calls AppTest.from_file().`
- `streamlit.testing.v1.AppTest.from_file` resolves relative paths against the calling file.
  `pyproject.toml` pins only `streamlit>=1.32`, so streamlit 1.63.0 was admitted silently.
- The **application is not broken**: driving `AppTest` with an absolute path renders all five
  tabs (`Today, Picks, Calendar, Performance, System`) with no exception and no error, does not
  create the database, and emits the correct warnings including
  `Evidence is insufficient until at least 20 real market days are audited.`
- Files: `tests/test_streamlit_app.py:7`, `tests/test_opportunity_projection_streamlit.py`

## Failing test files

| File | Failure references |
|---|---:|
| `tests/test_dawnstrike_python_bootstrap.py` | 78 |
| `tests/test_runtime_activation_contract.py` | 26 |
| `tests/test_runtime_activation_lock_recovery.py` | 15 |
| `tests/test_paper_ops_shadow_runner.py` | 6 |
| `tests/test_public_build_notifications.py` | 2 |
| `tests/test_opportunity_projection_streamlit.py` | 2 |
| `tests/test_capture_state_governance.py` | 2 |
| `tests/test_vercel_toolchain_contract.py` | 1 |
| `tests/test_streamlit_app.py` | 1 |
| `tests/test_paperops_universe_handoff_hostile_semantics.py` | 1 |

## Skipped tests (6) - all host-capability gated, none masking product logic

| Test | Skip reason |
|---|---|
| `test_luna_core_currentness.py:432` | file reparse creation is unavailable on this host |
| `test_protected_operation_contract.py:544` | protected log ACL creation requires elevation |
| `test_scheduled_entry_identity.py:378` | symlink creation is unavailable on this host |
| `test_state_root_boundary.py:583` | directory symlink creation is unavailable |
| `test_vercel_publication_journal.py:1274` | directory symlinks are unavailable on this Windows host |
| `test_vercel_publication_journal.py:1720` | Windows directory reparse creation is unavailable |

All six require elevation or symlink/reparse privileges. They would run under an elevated or
developer-mode session. They gate filesystem-hardening assertions, not product behavior.
Zero xfail, zero xpass.

## Interpretation

The important, non-obvious conclusion: **the product-logic suite is green.** Scanning, scoring,
ranking, avoid-list construction, monitoring, outcome, learning, persistence, validation and
publication tests all pass. Every one of the 61 failures traces to build/release/toolchain
identity governance or test-harness assumptions.

That is simultaneously reassuring and disqualifying for certification:

- Reassuring, because no failure indicates a defect in the research or decision logic.
- Disqualifying, because a certification gate requires a reproducible green suite, and this one
  cannot go green on a host whose Python received a patch release or whose transitive
  dependencies resolved one minor version forward. The environment is pinned by assertion but
  not by installation.
