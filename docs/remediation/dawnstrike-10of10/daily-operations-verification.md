# Daily operations verification

Status: `LOCAL_VERIFIED`; first unattended scheduled invocation is pending.

## Registered publication owner

Exactly one enabled Windows task owns public finalization:

`Dawnstrike 10of10 Daily Finalize`

It runs daily at 17:30 America/Chicago:

```text
powershell.exe -NoProfile -ExecutionPolicy Bypass
  -File "C:\r\dawnstrike-10of10-20260729\scripts\run_daily_finalize.ps1"
  -ProjectRoot "C:\r\dawnstrike-10of10-20260729"
  -SourceRoot "C:\Users\MattFields\Dawnstrike"
  -PublicationMode Production
  -VercelProjectId "prj_5pef3EZF1u5YadebEz3dFjnkWOXy"
  -AllowDegraded
```

The legacy `Dawnstrike X3 Vercel Daily Publish` task is disabled. Morning,
monitor, and EOD research tasks remain upstream operators; none of them owns
Vercel publication.

Task Scheduler reports:

- state: `Ready`;
- enabled: true;
- next run: 2026-07-30 17:30 America/Chicago;
- last run: Windows never-run sentinel, 1999-11-30;
- result: `267011` / `0x41303`, meaning the newly registered task has not run.

`scheduler-doctor` recognizes that sentinel and returns `LOCAL_VERIFIED`.

## Rehearsal proof

The exact scheduled script and arguments were run directly for market date
2026-07-29. It:

1. opened the shared source DB read-only;
2. created an isolated SQLite online backup;
3. rebuilt 431 canonical rows and 223 daily rows;
4. emitted a bounded public snapshot and controlled readiness;
5. built and verified one Vercel preview;
6. promoted that exact preview;
7. assigned and verified all production aliases;
8. returned `PRODUCTION_VERIFIED`.

Final rehearsal identifiers:

- preview: `dpl_CgpNe75UctboW7BavVTZWqLH7wQG`;
- production: `dpl_AbTsQJzj1EvMQ5Bd51naboU1BMv6`;
- source SHA: `51f79ff2a738110b486111d85c4d93cfda9f4ec8`;
- build ID: `5ef6a274f37fd1dbae87`;
- data hash:
  `3a134cc971e69a6f01a50c63687f9440883e82e833afa09bd120d319270cd56d`.

The source DB hash was identical before and after this rehearsal.

An earlier rehearsal overlapped the approved 08:10 AlphaOps Morning task.
That upstream task added six historical signals and one notification, changing
the source DB hash from `ED9F...` to `A9CF...`. The publication process did not
write those rows; its subsequent stable-hash rehearsal proves read-only
behavior.

## Remaining operations evidence

The daily system is configured to publish by 18:30 as complete, explicit
no-trade, or visibly degraded. It is not yet `PRODUCTION_VERIFIED` as an
unattended scheduler because the newly registered task has not reached its
first 17:30 trigger. After that trigger, verify:

- Task Scheduler result 0;
- same-day market date on production;
- final task/build/data IDs;
- controlled 200 or 503 readiness matching the stage manifest;
- no duplicate publication owner.
