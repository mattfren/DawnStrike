# Proof matrix

Candidate: `codex/dawnstrike-10of10` at the commit produced from this packet.
The candidate is isolated at `C:\r\dawnstrike-10of10-20260729`.

| Proof | Result | Evidence |
|---|---|---|
| Full pytest suite | PASS: 187 passed | `py -m pytest` |
| Ruff | PASS | `py -m ruff check .` |
| Mypy | PASS: 102 source files | `py -m mypy intraday_scanner` |
| Focused canonical tests | PASS: 8 passed | `tests/test_canonical_performance.py` |
| Compile | PASS | `py -m compileall -q intraday_scanner api scripts/verify_public_artifact.py` |
| Real-database copied build | PASS | 235 rows, 32 daily records, 219,678 bytes |
| Artifact verifier | PASS | `scripts/verify_public_artifact.py --root build/public` |
| Readiness truth | PASS | `degraded` snapshot -> `not_ready`, HTTP 503 |
| Static UI 360x800 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Static UI 390x844 | PASS | no horizontal overflow |
| Static UI 768x1024 | PASS | no horizontal overflow |
| Static UI 1280x720 | PASS | no horizontal overflow |
| Static UI 1440x900 | PASS | no horizontal overflow; screenshot in `evidence/` |
| Semantic navigation | PASS | Performance view activates through the public control |
| Accessibility | PASS: 0 violations, 33 passes | agent-browser axe 4.12.1 |
| Browser console/page errors | PASS | cleared console and error channels returned no errors |
| Vercel-native build | WAITING_EXTERNAL | CLI stopped because `uv` is not installed; no deployment or promotion was made |
| Daily task registration | WAITING_APPROVED_CHECKOUT | registration script is ready; isolated candidate intentionally has no live DB |

## Non-green by design

The copied real dataset is partial: unresolved outcomes, absent benchmark rows,
and incomplete cost inputs remain visible. The candidate publishes the bounded
snapshot but holds readiness at 503 and leaves unsupported after-cost/excess
return fields unreported. Strategy quality remains
`WAITING_FOR_FORWARD_EVIDENCE` until the required forward sample is actually
observed.
