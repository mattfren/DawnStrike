# WP005-B Repository-Durable Verification Evidence

- Terminal material delta: `PASS`
- Work package: `WP005-B`
- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- HEAD: `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- Current durable-run repair cycles: `0`
- Gate-time WP005-B implementation/test modifications: `none`
- Adjudication: not performed by this packet

## Commands and terminal results

| Gate | Exact command | Exit | Passed | Failed | Skipped | Xfailed | Xpassed | Start UTC | End UTC | Duration seconds |
|---|---|---:|---:|---:|---:|---:|---:|---|---|---:|
| main | `py -m pytest tests/test_opportunity_validation_metrics.py tests/test_opportunity_validation.py tests/test_opportunity_metric_persistence.py tests/test_opportunity_discovery_metrics.py tests/test_opportunity_miss_persistence.py tests/test_opportunity_missed.py tests/test_opportunity_outcomes.py tests/test_opportunity_outcome_persistence.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider` | 0 | 656 | 0 | 0 | 0 | 0 | 2026-08-15T17:15:22.410160Z | 2026-08-15T19:47:33.050417Z | 9130.481095 |
| affected | `py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider` | 0 | 139 | 0 | 0 | 0 | 0 | 2026-08-15T19:48:19.789942Z | 2026-08-15T19:50:26.532614Z | 126.74115 |
| static-ruff | `py -m ruff check .` | 0 | n/a | n/a | n/a | n/a | n/a | 2026-08-15T19:50:39.849137Z | 2026-08-15T19:50:40.044291Z | 0.19515 |
| static-mypy | `py -m mypy intraday_scanner` | 0 | n/a | n/a | n/a | n/a | n/a | 2026-08-15T19:50:46.527178Z | 2026-08-15T19:50:48.512945Z | 1.985743 |
| static-compileall | `py -m compileall -q intraday_scanner scripts` | 0 | n/a | n/a | n/a | n/a | n/a | 2026-08-15T19:50:53.383892Z | 2026-08-15T19:50:53.696787Z | 0.312889 |
| static-diff-check | `git diff --check` | 0 | n/a | n/a | n/a | n/a | n/a | 2026-08-15T19:50:59.708839Z | 2026-08-15T19:50:59.817900Z | 0.109059 |

## Collection and source integrity

- Preflight collection: `656` items; required `656`.
- Main excluded result markers: `{"deselected": 0, "errors": 0, "failed": 0, "skipped": 0, "xfailed": 0, "xpassed": 0}`.
- Every pre/post frozen source hash matched, and the final hashes equal the controlling frozen values.
- No surviving gate process was found before lease release.
- The pre-existing shared dirty worktree is not represented as clean or immutable.

## Durable artifacts

- `main`: `docs/quant-refactor/evidence/wp005-b-20260815/main.stdout.log`, `docs/quant-refactor/evidence/wp005-b-20260815/main.stderr.log`, `docs/quant-refactor/evidence/wp005-b-20260815/main.command.txt`, `docs/quant-refactor/evidence/wp005-b-20260815/main.exit.json`.
- `affected`: `docs/quant-refactor/evidence/wp005-b-20260815/affected.stdout.log`, `docs/quant-refactor/evidence/wp005-b-20260815/affected.stderr.log`, `docs/quant-refactor/evidence/wp005-b-20260815/affected.command.txt`, `docs/quant-refactor/evidence/wp005-b-20260815/affected.exit.json`.
- `static-ruff`: `docs/quant-refactor/evidence/wp005-b-20260815/static-ruff.stdout.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-ruff.stderr.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-ruff.command.txt`, `docs/quant-refactor/evidence/wp005-b-20260815/static-ruff.exit.json`.
- `static-mypy`: `docs/quant-refactor/evidence/wp005-b-20260815/static-mypy.stdout.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-mypy.stderr.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-mypy.command.txt`, `docs/quant-refactor/evidence/wp005-b-20260815/static-mypy.exit.json`.
- `static-compileall`: `docs/quant-refactor/evidence/wp005-b-20260815/static-compileall.stdout.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-compileall.stderr.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-compileall.command.txt`, `docs/quant-refactor/evidence/wp005-b-20260815/static-compileall.exit.json`.
- `static-diff-check`: `docs/quant-refactor/evidence/wp005-b-20260815/static-diff-check.stdout.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-diff-check.stderr.log`, `docs/quant-refactor/evidence/wp005-b-20260815/static-diff-check.command.txt`, `docs/quant-refactor/evidence/wp005-b-20260815/static-diff-check.exit.json`.
- Preflight: `docs/quant-refactor/evidence/wp005-b-20260815/preflight.json` and `preflight-collection.*`.
- Lease record: `docs/quant-refactor/evidence/wp005-b-20260815/gate-lease.json`.
- This packet records verification evidence only; it does not self-adjudicate the work package.
