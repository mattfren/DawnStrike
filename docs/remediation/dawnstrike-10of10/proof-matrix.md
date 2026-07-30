# Proof matrix

Candidate: `codex/dawnstrike-10of10`; the latest clean build SHA is recorded in
the ignored diagnostic `build/public/build-manifest.json`.
The candidate is isolated at `C:\r\dawnstrike-10of10-20260729`.

| Proof | Result | Evidence |
|---|---|---|
| Full pytest suite | PASS | `py -m pytest -q` |
| Ruff | PASS | `py -m ruff check .` |
| Mypy | PASS: 109 source files | `py -m mypy intraday_scanner` |
| Focused canonical tests | PASS: 17 passed | PaperOps, scheduler, canonical, and snapshot tests |
| Compile | PASS | `py -m compileall -q intraday_scanner scripts` |
| PowerShell parse | PASS: 16 scripts | Windows PowerShell parser pass |
| Real-database copied reconcile | PASS/FAIL-CLOSED | 425 rows, 222 daily records, 46 discrepancies, CLI exit 2; PaperOps 190 accepted / 0 quarantined / 21 warnings |
| Diagnostic public build | PASS/DEGRADED | Clean candidate SHA `6886900de5960a90c8693e476c8c32d26f864375`; 425 canonical rows, 222 daily records, 633,502 raw bytes / 42,293 deterministic-gzip bytes, 250 public rows, snapshot `degraded`, readiness HTTP 503 |
| Artifact verifier | FAIL-CLOSED | Build source SHA `6886900de5960a90c8693e476c8c32d26f864375` rejects only `snapshot_not_publishable` and `readiness_not_publishable`; compressed-size and row-limit checks pass |
| Readiness truth | PASS | `degraded` snapshot -> `not_ready`, HTTP 503 |
| Persistence target safety | PASS/FAIL-CLOSED | External `--db-path` rejected before opening the database; covered by `tests/test_public_build_safety.py` |
| Static UI 360x800 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Static UI 390x844 | PASS | no horizontal overflow |
| Static UI 768x1024 | PASS | no horizontal overflow |
| Static UI 1280x720 | PASS | no horizontal overflow |
| Static UI 1440x900 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Semantic navigation | PASS | All four public controls activate their matching visible panels |
| Accessibility | PASS: 0 violations, 33 passes | agent-browser axe 4.12.1 |
| Browser console/page errors/network | PASS | Current-artifact browser pass returned empty console/page-error channels; hosted preview visual access is limited by Vercel Deployment Protection |
| Vercel-native build | PREVIEW_VERIFIED | Explicit Dawnstrike project build succeeds with Vercel CLI 58.4.0; exact preview `dpl_9UXadeGZsJTBoQt6g8BLdxopYYVg` exposes matching source/build/data hashes, zero live trading, and readiness HTTP 503. |
| Daily task registration | BLOCKED_EXTERNAL | `Dawnstrike 10of10 Daily Finalize` is absent; registration script is ready but intentionally not run |

## Non-green by design

The copied real dataset is partial: unresolved outcomes, absent benchmark rows,
incomplete cost inputs, 21 PaperOps component-scope warnings, and 25 missing
outcomes remain visible. The candidate writes the bounded diagnostic snapshot
but holds readiness at 503 and leaves unsupported after-cost/excess return
fields unreported. Strategy quality remains
`WAITING_FOR_FORWARD_EVIDENCE` until the required forward sample is actually
observed.

The fresh build was mistakenly pointed at the shared DB and persisted the
derived 425/222 read model plus one console notification. The owner approved
retaining and auditing that state; it remains an audit incident, not a green
publication result.
