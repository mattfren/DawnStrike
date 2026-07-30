# Proof matrix

Candidate: `codex/dawnstrike-10of10`; the latest clean build SHA is recorded in
the ignored diagnostic `build/public/build-manifest.json`.
The candidate is isolated at `C:\r\dawnstrike-10of10-20260729`.

| Proof | Result | Evidence |
|---|---|---|
| Full pytest suite | PASS: 219 passed | `py -m pytest -q` |
| Ruff | PASS | `py -m ruff check intraday_scanner tests` |
| Mypy | PASS: 109 source files | `py -m mypy intraday_scanner` |
| Focused canonical tests | PASS: 12 passed | PaperOps, scheduler, canonical, and snapshot tests |
| Compile | PASS | `py -m compileall -q intraday_scanner scripts` |
| PowerShell parse | PASS: 16 scripts | Windows PowerShell parser pass |
| Real-database copied reconcile | PASS/FAIL-CLOSED | 425 rows, 222 daily records, 156 discrepancies, CLI exit 2 |
| Diagnostic public build | PASS/DEGRADED | 425 canonical rows, 222 daily records, 632,094 raw bytes / 38,425 deterministic-gzip bytes, 250 public rows, snapshot `degraded`, readiness HTTP 503 |
| Artifact verifier | FAIL-CLOSED | Build source SHA `edd228ae1e2d56631b6684458425c653b2b3814f` rejects only `snapshot_not_publishable` and `readiness_not_publishable`; compressed-size and row-limit checks pass |
| Readiness truth | PASS | `degraded` snapshot -> `not_ready`, HTTP 503 |
| Static UI 360x800 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Static UI 390x844 | PASS | no horizontal overflow |
| Static UI 768x1024 | PASS | no horizontal overflow |
| Static UI 1280x720 | PASS | no horizontal overflow |
| Static UI 1440x900 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Semantic navigation | PASS | All four public controls activate their matching visible panels |
| Accessibility | PASS: 0 violations, 33 passes | agent-browser axe 4.12.1 |
| Browser console/page errors/network | PASS | Current Playwright pass returned empty console/page-error/request-failure channels |
| Vercel-native build | PASS/NO_DEPLOY | Explicit Dawnstrike project build succeeds with Vercel CLI 58.4.0; 18 prebuilt files, 861,926 bytes, two functions, zero forbidden files |
| Daily task registration | BLOCKED_EXTERNAL | `Dawnstrike 10of10 Daily Finalize` is absent; registration script is ready but intentionally not run |

## Non-green by design

The copied real dataset is partial: unresolved outcomes, absent benchmark rows,
incomplete cost inputs, and 105 quarantined PaperOps rows remain visible. The
candidate writes the bounded diagnostic snapshot but holds readiness at 503
and leaves unsupported after-cost/excess return fields unreported. Strategy quality remains
`WAITING_FOR_FORWARD_EVIDENCE` until the required forward sample is actually
observed.
