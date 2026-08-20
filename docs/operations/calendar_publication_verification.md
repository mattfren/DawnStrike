# Calendar publication verification

`scripts/verify_calendar_publication.py` is a read-only operational check for
the static Calendar publication. It does not import the runtime, open SQLite,
run a doctor, invoke Task Scheduler, write a receipt, deploy, or print secrets.

## What it verifies

The local check binds `data/calendar.json` to its Calendar manifest, the
performance manifest, the publication-set manifest, `build-manifest.json`, and
the selected fields in `readiness.json`. It also verifies every file hash
listed by the build manifest. With `--deployment-url`, it fetches (with a
cache-busting query and `Cache-Control: no-cache`) only these public paths:

```text
/api/health
/api/readiness
/data/calendar.json
/data/calendar.json.manifest.json
/data/performance.json.manifest.json
/data/publication-set.json
/build-manifest.json
```

Remote Calendar and manifest bytes are compared to the local artifact. Health,
readiness, source SHA, build ID, and performance data hash are compared to the
same local build. No authorization header or environment variable is read.

The status is one of:

| Status | Meaning |
| --- | --- |
| `CURRENT` | Local bindings pass; if a deployment URL was supplied, all public checks pass. |
| `NOT_DUE` | The expected market date is later than the artifact, or `--due-at` is still in the future. |
| `STALE` | The artifact market date is older than the explicit expected market date. |
| `HASH_MISMATCH` | A required local or public payload/manifest/file hash does not match. |
| `DEPLOYMENT_SHA_MISMATCH` | Local or public deployment source SHA differs from the expected/runtime SHA. |
| `UNAVAILABLE` | A requested public endpoint could not be checked or was not ready. |

The expected date and due time are explicit inputs so an unattended caller
cannot silently treat a weekend, holiday, or pre-finalize window as stale:

```powershell
py scripts\verify_calendar_publication.py `
  --root C:\r\dawnstrike-runtime\build\public `
  --expected-source-sha (git -C C:\r\dawnstrike-runtime rev-parse HEAD) `
  --expected-market-date 2026-08-19 `
  --deployment-url https://dawnstrike-command-center-x3.vercel.app
```

For a pre-finalize check, add `--due-at 2026-08-20T17:30:00-05:00` and expect
`NOT_DUE` until the Daily Finalize owner has run.

## Calendar pipeline and timing audit

The source pipeline is:

```text
canonical performance -> calendar_snapshot.py -> calendar.json + manifest
                      -> publication-set.json -> build/public
                      -> Daily Finalize at 17:30 America/Chicago
                      -> Vercel health/readiness/public JSON
```

The source schedule remains Morning 08:00, Monitor 08:35--15:10, EOD 15:15,
Daily Finalize 17:30, and Weekly Training Monday 21:00 America/Chicago. The
Calendar publication owner is `Dawnstrike 10of10 Daily Finalize`; upstream
research tasks do not own Vercel publication.

## 2026-08-20 read-only evidence

The active runtime was clean at
`be64a84e02bedd805bc27ea8121de7b2b4dc2300`. Its latest completed publication
was market date `2026-08-19`, build `78db9979e2ccadbd451f`, Calendar payload
`bf8a2dd379b5c3501989a863ff0bf3fb1f2cefa0816622e26243d018e1de4eef`, and
performance payload
`b6d906bccdc22da81ab1dbfc61387f92cf13c82a071f70ecb3033d6daef6be78`.

The new verifier returned `CURRENT` locally and against the production alias.
Production `/api/health`, `/api/readiness`, Calendar manifest, build manifest,
performance manifest, publication set, and Calendar JSON all returned HTTP
200 and matched the local bindings. Production readiness was `ready` with
source SHA `be64a84e02bedd805bc27ea8121de7b2b4dc2300`, build ID
`78db9979e2ccadbd451f`, market date `2026-08-19`, and research-only/broker-
disabled safety flags.

The direct read-only Task Scheduler query also showed all five canonical tasks
enabled with result `0`. The embedded scheduler summary in the 17:30 artifact
captured the Finalize task while it was still running (`267009`), whereas the
later direct query was complete. This is an observability gap: published
readiness scheduler metadata is a build-time snapshot and can lag the live
Task Scheduler state. The verifier therefore treats source/build/public JSON
identity as authoritative and does not claim that an embedded task result is
current after publication.

Existing `verify_public_artifact.py` remains the broad static safety gate. The
new check fills the operational gap it did not cover: one status vocabulary for
timing, local Calendar hash bindings, and independent live public JSON/source
SHA comparison. Browser/client cache behavior and future-date display labeling
remain separate UI concerns and are not changed here.
