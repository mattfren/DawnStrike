# R2 repair attempt 1

Repair base: `16962f3752d0072cf8f5c1ca5097118e64feff1f`  
Worktree: `C:\r\dawnstrike-remediation-r2-20260909`  
Date: 2026-09-10

This is a materially distinct repair from the initial offline sidecar. The
candidate is not modified by this worktree.

## Independent findings addressed

- Source rows now require the manifest session, provider/feed, capture receipt
  hash, and source-config hash; the original source session is retained.
- `available_at` must be no later than the observation process time. Future
  rows are quarantined as unavailable and cannot produce a timely receipt.
- Manifest market date, canonical XNYS session identity, checked-in trading
  calendar, and decision-deadline date are validated before collection.
- Every output root is bound to manifest/session/source identity, and repository
  roots are rejected even when the caller omits `repository_root`.
- Coverage is emitted for every declared entry. Missing observations produce
  `PARTIAL`; `missing_input` remains a separate coverage state.
- Receipt status and delayed counts derive from all retained session events on
  replay, so replay cannot upgrade a delayed session to `CAPTURED`.
- Zero-byte input is `EMPTY` only with a complete declared collection
  expectation and receipt identity. Nonempty whitespace input is `FAILED`.
- The cursor binds session, manifest, source path, byte offset, prefix hash, and
  completed source hash. Replaced, truncated, or rewound input is `BLOCKED`.

## Existing provider artifact adapter

`intraday_scanner.observation.producer.build_observation_inputs` and
`scripts/build_observation_inputs.py` now consume an authenticated existing
`capture_run_receipt.json` plus its retained `capture_run_state.json` page
artifacts and a source-backed two-scope declaration. They produce:

- `universe-manifest.json`, bound to provider/feed, session, source-config hash,
  capture receipt hash, code SHA, request interval, completion time, and a
  complete/incomplete collection expectation;
- immutable `raw-events.jsonl`, preserving provider payloads and page artifact
  hashes; and
- `producer-receipt.json` containing the output hashes and source lineage.

The producer input root remains separate from the observer output root. The
observer never appends to or rewrites the producer's raw stream.

## Exact bounded operation

1. Run the existing read-only authenticated provider capture with external
   database/evidence/run roots using `scripts/capture_intraday_evidence.py`.
2. Retain a two-scope source declaration with every symbol classified as
   `selected`, `rejected`, `unselected`, or `missing_input`.
3. Bind the capture receipt and page state:

```powershell
py -3.13 scripts/build_observation_inputs.py `
  --capture-receipt C:\r\dawnstrike-r2-evidence\runs\<run>\capture_run_receipt.json `
  --scope-declaration C:\r\dawnstrike-r2-evidence\scope-declaration.json `
  --output-root C:\r\dawnstrike-r2-evidence\observer-inputs-2026-09-10 `
  --decision-deadline 2026-09-10T14:00:00+00:00 `
  --repository-root C:\r\dawnstrike-remediation-r2-20260909
```

4. Start, stop, and resume the bounded observer using the generated manifest
   and immutable raw stream. The exact commands are in
   `docs/operations/r2_observation_sidecar.md`; resume uses the same command
   after removing the cooperative stop marker.

No provider request, credential change, scheduler/task registration, runtime
state write, broker path, order, or purchase was performed by this repair.

## Validation

```text
py -3.13 -m pytest tests/test_isolated_observer.py -q
12 passed

py -3.13 -m pytest tests/test_isolated_observer.py tests/test_capture_operations.py tests/test_daily_intraday_capture_runner.py tests/test_intraday_evidence_store.py tests/test_intraday_evidence_contracts.py tests/test_intraday_evidence_capture_service.py -q
PASS (affected set)

py -3.13 -m ruff check intraday_scanner/observation scripts/run_isolated_observer.py scripts/build_observation_inputs.py tests/test_isolated_observer.py
All checks passed

git diff --check
PASS
```

The producer test validates receipt/page hash binding, generated two-scope
manifest and event lineage, and byte-for-byte preservation of the immutable
raw input after observer execution. The subprocess test validates cooperative
stop and same-command recovery.

## Provenance and usage

The current worker session metadata file is
`C:\Users\MattFields\.codex\sessions\2026\09\09\rollout-2026-09-09T20-57-02-01a08908-5f22-7612-a171-3132e27a9d6b.jsonl`.
Its `turn_context` records `gpt-5.6-luna`; `session_meta.model` is null, so the
turn context is the narrowest available model identity evidence. The latest
thread cumulative usage record reports input `17,270,025`, cached input
`16,723,456`, output `77,993`, reasoning output `22,137`, total `17,348,018`.
Those cumulative counters are recorded once and are not added to per-turn or
last-usage subsets. The account usage window remained 53%, with zero credits;
no reset was consumed.

Fresh independent verification is required before any launch decision. This
packet does not arm a heartbeat or change program status.
