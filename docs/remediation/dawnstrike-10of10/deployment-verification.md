# Deployment verification

Status: `PRODUCTION_VERIFIED`

Verification time: 2026-07-30, America/Chicago.

## Exact artifact

| Field | Value |
|---|---|
| Project ID | `prj_5pef3EZF1u5YadebEz3dFjnkWOXy` |
| Vercel CLI | `58.4.0` |
| Source SHA | `51f79ff2a738110b486111d85c4d93cfda9f4ec8` |
| Build ID | `5ef6a274f37fd1dbae87` |
| Data hash | `3a134cc971e69a6f01a50c63687f9440883e82e833afa09bd120d319270cd56d` |
| Market date | `2026-07-29` |
| Preview ID | `dpl_CgpNe75UctboW7BavVTZWqLH7wQG` |
| Preview URL | `https://dawnstrike-command-center-x3-pq5i29540-mattfrens-projects.vercel.app` |
| Production ID | `dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6` |
| Production URL | `https://dawnstrike-command-center-x3-lzqxcvoqn-mattfrens-projects.vercel.app` |
| Public alias | `https://dawnstrike-command-center-x3.vercel.app` |

The worktree was clean when the build started. The minimal stage was built
once, deployed as the preview, verified, promoted without a second build, and
resolved to the promoted clone by its exact `originalDeploymentId`. The
publisher then assigned all three production aliases deterministically.

All three aliases resolve to
`dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6`:

- `dawnstrike-command-center-x3.vercel.app`;
- `dawnstrike-command-center-x3-mattfrens-projects.vercel.app`;
- `dawnstrike-command-center-x3-mattfren-mattfrens-projects.vercel.app`.

## Runtime proof

- `/api/health`: HTTP 200, `status=alive`, exact source/build IDs,
  `research_only=true`, and `live_trading_enabled=false`.
- `/api/readiness`: controlled HTTP 503, `status=not_ready`,
  `snapshot_status=degraded`, and the exact production data hash.
- `/build-manifest.json`: exact source SHA, build ID, data hash, market date,
  generated timestamp, and per-file hashes.
- `/api/scanner`: HTTP 404.
- `/api/telegram`: HTTP 404.
- `/api/cron/daily`: HTTP 404.
- Vercel error-log query for the production deployment returned no logs.
- Final production browser checks passed desktop, mobile, four-section
  navigation, pagination, and empty warning/error logs.

The public snapshot has 250 bounded detail rows, 632,789 raw bytes, and 42,206
deterministic-gzip bytes. The verifier passed the approved-degraded
publication policy. It contains no local SQLite database, scanner engine,
Telegram sender, Streamlit UI, or broker execution route.

## Data state

The deployed runtime is healthy; readiness is correctly red because the source
truth is incomplete. The build contains 431 canonical rows, 223 daily rows,
49 reconciliation issues, 28 missing outcomes, no benchmark observations, and
incomplete official cost/equity inputs. Unsupported return fields remain null.

The shared source database hash was
`A9CF497463BBA78591D72BB038C7C3374D4B308895D07975E47FE0DB3CE8CEE4`
before and after the final production publication. The publisher used a
read-only SQLite online backup into the isolated checkout.
