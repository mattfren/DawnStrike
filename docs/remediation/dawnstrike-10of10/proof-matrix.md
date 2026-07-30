# Proof matrix

Candidate: `codex/dawnstrike-10of10`; the latest clean build SHA is recorded in
the ignored diagnostic `build/public/build-manifest.json`.
The candidate is isolated at `C:\r\dawnstrike-10of10-20260729`.

| Proof | Result | Evidence |
|---|---|---|
| Full pytest suite | PASS: 211 passed | `py -m pytest` |
| Ruff | PASS | `py -m ruff check .` |
| Mypy | PASS: 107 source files | `py -m mypy intraday_scanner` |
| Focused canonical tests | PASS: 8 passed | `tests/test_canonical_performance.py` |
| Compile | PASS | `py -m compileall -q api intraday_scanner scripts` |
| Real-database copied reconcile | PASS/FAIL-CLOSED | 235 rows, 32 daily records, 25 discrepancies, CLI exit 2 |
| Diagnostic public build | PASS/DEGRADED | 235 rows, 232,001 bytes, snapshot `degraded`, readiness HTTP 503 |
| Artifact verifier | FAIL-CLOSED | Clean source SHA passes; real-data stage rejects `snapshot_not_publishable` and `readiness_not_publishable` |
| Readiness truth | PASS | `degraded` snapshot -> `not_ready`, HTTP 503 |
| Static UI 360x800 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Static UI 390x844 | PASS | no horizontal overflow |
| Static UI 768x1024 | PASS | no horizontal overflow |
| Static UI 1280x720 | PASS | no horizontal overflow |
| Static UI 1440x900 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Semantic navigation | PASS | All four public controls activate their matching visible panels |
| Accessibility | PASS: 0 violations, 33 passes | agent-browser axe 4.12.1 |
| Browser console/page errors/network | PASS | Current Playwright pass returned empty console/page-error/request-failure channels |
| Vercel-native build | PASS/NO_DEPLOY | Minimal stage build succeeds at 456,406 bytes with two functions; no deployment because candidate data readiness is not publishable |
| Daily task registration | WAITING_APPROVED_CHECKOUT | registration script is ready; isolated candidate intentionally has no live DB |

## Non-green by design

The copied real dataset is partial: unresolved outcomes, absent benchmark rows,
and incomplete cost inputs remain visible. The candidate publishes the bounded
snapshot but holds readiness at 503 and leaves unsupported after-cost/excess
return fields unreported. Strategy quality remains
`WAITING_FOR_FORWARD_EVIDENCE` until the required forward sample is actually
observed.
