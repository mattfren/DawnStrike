# 02 — Functional and operational evidence

Commit `14ca714f`. Everything below was executed or queried directly. The primary checkout, the
production runtime and the live database were never modified; all database reads used
`mode=ro&immutable=1` with `PRAGMA query_only=ON`.

## Installation and clean build — `PARTIAL`

Full detail: `evidence/clean_build_reproducibility.md`.

A fresh venv (`C:\r\dawnstrike-cleanenv-20260904`, Python 3.13.15) following the documented
`py -m pip install -e ".[dev]"` **installs successfully** (`INSTALL_EXIT=0`): dependencies resolve,
the editable wheel builds, `intraday_scanner` imports.

It does **not** produce a suite-passing environment. The documented path never consults
`requirements.lock`, so it resolves `anyio 4.15.0` against the lock's `anyio==4.14.2`, and the
repository's own contract test fails in the clean venv with the identical error seen in the full
run. This is systemic, not machine-local.

## Test suite — `FAIL` at baseline

Full detail: `evidence/test_suite_analysis.md`. Raw: `evidence/full_pytest_origin_main.txt`.

| Metric | Value |
|---|---:|
| Collected | 5,150 |
| Passed | 5,083 |
| **Failed** | **61** |
| Skipped | 6 |
| xfail / xpass | 0 |
| Exit | 1 |

The prior in-repo certification (`docs/quant-refactor/32-final-independent-certification.md`,
2026-08-17, `CERTIFICATION: READY`) recorded 3,166 tests all passing. The suite has since grown 63%
and does not pass. **That certification is superseded and must not be cited as current evidence.**

The 61 failures collapse into four families, none of which is a defect in scanning, scoring,
monitoring, outcome or learning logic:

| Family | Count | Cause |
|---|---:|---|
| A — dependency lock drift | 35 | `anyio` 4.15.0 vs pinned 4.14.2 |
| B — approved interpreter identity stale | ~16 | pin `ef8f5102…` vs host `85B71D8C…` (Python 3.13.15) |
| C — date-dependent test | 3 | asserts a completed bar for the *execution* date |
| D — Streamlit `AppTest` path | 3 | relative path resolves to `tests/app.py` |

**The product-logic suite is green.** That is simultaneously reassuring (no research-logic defect
surfaced) and disqualifying (a certification gate requires a reproducible green suite).

Six skips are all host-capability gated — symlink/reparse creation and ACL elevation. They gate
filesystem-hardening assertions, not product behaviour. **A skipped test is not a passing test**, but
none of these masks product logic.

## Live operational reality — `FAIL`

This is the most important section of the audit, and none of it is visible from the repository alone.

### Three-way state divergence

| Tree | Commit | Date | Status |
|---|---|---|---|
| Primary checkout | `ba39a535` | 2026-08-07 | **440 commits behind**, dirty |
| `origin/main` (audited) | `14ca714f` | 2026-09-02 | authoritative |
| Production runtime | `b7220890` | 2026-08-30 | **what actually executes** |

The primary checkout differs from `origin/main` by 1,486 files and 547,081 insertions. Auditing it
would have produced almost entirely stale findings — which is precisely how the two prior audit
documents in that checkout (both dated 2026-08-09 against `ba39a535`) came to be stale.

### Every functional stage has failed 100% of the time

`daily_run_stages`, live DB, 2026-08-31 → 2026-09-04:

| Stage | Status | Count |
|---|---|---:|
| `intraday_monitor` | **FAILED** | 344 |
| `morning_collection` | **FAILED** | 5 |
| `ranking_delivery` | **FAILED** | 5 |
| `eod_outcome_capture` | **FAILED** | 4 |
| `paper_reconciliation` | **FAILED** | 4 |
| `paperops_forward` | **FAILED** | 4 |
| `alpha_learning` | **FAILED** | 4 |
| `canonical_performance` | DEGRADED | 4 |
| `readiness` | DEGRADED | 4 |
| `publication` | **COMPLETE** | 4 |

`monitor_interval_gaps`: 79 `MISSED_INTERVAL` per day for four consecutive days, 35 more today —
**zero observed intervals**. No `alpha_monitor-*.stdout.log` or `trade_watch-*.stdout.log` has ever
been written: the monitoring CLI has never executed.

Scheduled-task last results: Morning `2`, Daily Finalize `2`, Delayed SIP Capture `2147942402`
(`ERROR_FILE_NOT_FOUND`).

Root cause, reproduced directly against the real artifacts — see `05_REMEDIATION_PROGRAM.md` WP-1.
**Fixed and regression-tested.**

### Database forensics

`C:\r\dawnstrike-state\shadow_real.sqlite`, schema 30 (initialised 2026-08-30),
`PRAGMA quick_check = ok`. **145 tables, 105 empty.**

Every outcome/trade/learning table is empty: `signal_outcomes`, `paper_positions`,
`paper_trade_fills`, `paper_audit_trades`, `manual_outcomes`, `alpha_outcome_labels`,
`alpha_v6_outcomes`, `strategy_learning_labels`, `alpha_learning_runs`, `trade_intents`,
`committed_fill_truth_receipts`, `outcome_capture_attempts` — all `0`.

`paper_account_daily_ledger`, official cohort, 2026-07-31 → 2026-09-03 = **25 trading days**:
21 rows `MISSING`, 4 rows `NO_TRADE`, `trade_count = 0` on every row,
`target_return_pct 1.0` / `target_status PENDING` throughout.

The system does generate real ranked candidates daily — 2026-09-03 produced TLYS, SMMT, AVAV, NTSK,
DLTH, BIAF, SNOW, CHPT with genuine source references. They simply never become trades.

## Core user journeys

| Journey | Status | Evidence |
|---|---|---|
| Install from clean state | `PARTIAL` | installs; lock not honoured |
| Offline CSV scan | `PASS` | `ranked=4 avoid=4 top=NOVA`, exit 0 |
| Scan determinism | `PASS` | byte-identical outputs across runs |
| Empty universe | `PASS` | header-only outputs, counts 0, no crash |
| Avoid-list correctness | `PASS` | OFFER/SUBP/THIN/HALT correctly excluded |
| Dashboard render | `PASS` | 5 tabs, no exception, correct warnings |
| Dashboard empty state | `PASS` | *"Evidence is insufficient until at least 20 real market days are audited."* |
| Pick → monitor | `FAIL` | monitor never executed |
| Monitor → outcome | `FAIL` | zero outcomes |
| Outcome → day grade | `FAIL` | no data |
| Grade → lesson → apply | `FAIL` | all learning tables empty |
| Publication gating | `PASS` | refused to publish while degraded |
| Failure notification | `PASS` | Telegram `sent: true`, 4 of 5 days |

Dashboard verification (absolute-path `AppTest`, empty DB): `exception=ElementList()`,
`error=ElementList()`, tabs `['Today','Picks','Calendar','Performance','System']`, database **not**
created, warnings correct.

## Reliability and fail-visibility — the standout strength

The system fails **visibly and safely**, which I verified rather than assumed:

- **Missing ≠ zero.** `missing_is_not_zero: true` on gap receipts; `MISSING` days keep
  `net_return_pct = NULL` while genuine no-trade days record
  `return_basis: "explicit_no_trade_observed_zero"` with `observed_zero: true`. The two are never
  conflated.
- **Refuses to publish degraded truth.** 2026-09-03 finalize: `status DEGRADED`,
  `upstream_status failed`, **`deployment_url: null`**,
  `next_action: "missing_or_degraded_upstream_truth_or_safety"`. The public dashboard was **not**
  updated with broken data.
- **Operator is told.** `stage_failure_notification` receipts show `sent: true` on 08-31, 09-01,
  09-02, 09-03.
- **Research-only boundary is stamped everywhere.** Every receipt carries `"research_only": true`
  and `"broker_execution_enabled": false`.

The gap is not honesty — it is that a failure repeating identically for five consecutive days
produced no escalation beyond a per-stage notification.

## Security and configuration — `PASS` with one caveat

- No broker SDK, order route, or execution credential anywhere in the tree.
- Secrets live outside the repository (`C:\r\dawnstrike-state\secrets\runtime.env`); `.env` is
  git-ignored; a `detect-secrets` baseline is maintained. I read only key *names*, never values.
- `defusedxml` is a declared dependency; URL ingestion is domain-allowlisted; a browser extractor is
  optional and disabled by default.
- Interpreter identity is pinned by SHA-256 **and** Authenticode signer/thumbprint — a stronger
  supply-chain control than most projects of this size attempt.

Caveat: that control is currently stale (Family B) and is very likely why production remains three
days behind `origin/main`. The control is right; the rotation procedure is missing.

## Deployment drift — `HIGH`

The registered task invokes the monitor with only `-RuntimeRoot` and `-StateRoot`. At `origin/main`
the monitor adds three **mandatory** parameters (`ExpectedSha`, `LaunchManifestPath`,
`LaunchManifestSha256`). Upgrading the runtime without re-registering the tasks would break the
pipeline. Task registration and stage-script signatures must move together — see operator packet OP-1.
