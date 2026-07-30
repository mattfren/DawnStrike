# Proof matrix

Candidate: `codex/dawnstrike-10of10`

Runtime source SHA: `51f79ff2a738110b486111d85c4d93cfda9f4ec8`

Isolated checkout: `C:\r\dawnstrike-10of10-20260729`

| Proof | Result | Evidence |
|---|---|---|
| Full pytest suite | `PASS` | `py -m pytest -q` completed at 100%. |
| Ruff | `PASS` | `py -m ruff check .` -> all checks passed. |
| Mypy | `PASS` | No issues in 109 source files. |
| Compile | `PASS` | `py -m compileall -q intraday_scanner scripts`. |
| JavaScript syntax | `PASS` | `node --check web/assets/dawnstrike.js`. |
| PowerShell parsing | `PASS` | All 17 scripts parsed without errors. |
| Git whitespace/worktree | `PASS` | `git diff --check`; runtime build started from a clean source SHA. |
| Probability doctor | `PASS / FAIL-CLOSED` | `Uncalibrated`, sample 0, collect sourced forward outcomes. |
| Scheduler doctor | `LOCAL_VERIFIED` | Task enabled/Ready, next 17:30, normal never-run code `0x41303`. |
| Dashboard doctor | `IN_PROGRESS` | Canonical tables present; readiness exactly degraded/503. |
| Real-data reconciliation | `PASS / PARTIAL` | 431 canonical rows, 223 daily rows, 49 issues, 28 missing outcomes. |
| PaperOps adapter | `PASS / PARTIAL` | 190 accepted, 0 quarantined, 21 component-scope warnings, 0 source-return mismatches. |
| Public payload bound | `PASS` | 250 rows, 632,789 raw bytes, 42,206 gzip bytes. |
| Readiness truth | `PASS` | Degraded snapshot -> `not_ready`, HTTP 503, no green state. |
| Shared DB safety | `PASS` | Read-only online backup; stable SHA-256 before/after final production rehearsal. |
| UI 360x800 | `PASS` | Zero page/header/nav overflow; production mobile rendered with current data. |
| UI 390x844 | `PASS` | Zero page/header/nav overflow on exact local artifact. |
| UI 768x1024 | `PASS` | Zero page/header/nav overflow on exact local artifact. |
| UI 1280x720 | `PASS` | All ten KPI cards end by pixel 607; zero overflow. |
| UI 1440x900 | `PASS` | Zero page/header/nav overflow on exact local artifact. |
| UI navigation | `PASS` | Overview, Performance, Research, and System each activate correctly. |
| UI pagination | `PASS` | Ten rows per page; next/previous boundary state verified. |
| UI DOM integrity | `PASS` | Zero malformed tooltip attributes; missing values remain `Not reported`. |
| Browser logs | `PASS` | Final production desktop and mobile warning/error logs empty. |
| Preview | `PREVIEW_VERIFIED` | `dpl_CgpNe75UctboW7BavVTZWqLH7wQG` matches source/build/data hashes. |
| Production | `PRODUCTION_VERIFIED` | `dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6`; all three aliases exact. |
| Health/readiness | `PASS` | Health 200/alive; readiness controlled 503/not_ready. |
| Forbidden routes | `PASS` | Scanner, Telegram, and cron test routes return 404. |
| Vercel error logs | `PASS` | No error logs found for final production deployment. |
| Rollback | `PASS` | All aliases moved to prior `dpl_3h12...`, exact hashes verified, then restored to final `dpl_AbTs...`. |
| Daily task registration | `LOCAL_VERIFIED` | Exactly one 17:30 production publication owner; legacy X3 publisher disabled. |

## Intentionally non-green

The real dataset does not contain the truth needed for a complete official
return. Benchmark rows are absent, cost/opening-equity evidence is incomplete,
28 outcomes are missing, and 49 reconciliation issues remain. Production
therefore publishes null unsupported returns and a visible 503 degraded state.
Strategy validation remains `WAITING_FOR_FORWARD_EVIDENCE`.
