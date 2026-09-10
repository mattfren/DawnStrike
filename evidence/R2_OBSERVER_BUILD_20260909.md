# R2 observer build packet

Date: 2026-09-09  
Branch: `codex/dawnstrike-remediation-r2-20260909`  
Base: `7d3f108bfb40b9cd182876bf69c9050e58d5720b`  
Worktree: `C:\r\dawnstrike-remediation-r2-20260909`

## Delivered

This branch adds an additive `intraday_scanner.observation` sidecar and the
noninteractive entrypoint `scripts/run_isolated_observer.py`. It records the
full declared census across `original_small_cap_gap` and
`liquid_reference_panel`, preserving `selected`, `rejected`, `unselected`, and
`missing_input` rows. Raw events use stable content-derived IDs and append-only
JSONL. Cursors are atomic and replay is idempotent under reordered input.

The runner validates the immutable manifest and optional expected hashes,
rejects repository/runtime/state output roots, bounds pages/events/bytes/retries,
uses a fail-closed lock, records quarantine and explicit receipt statuses, and
sets `research_only=true`, `broker_execution_enabled=false`, and
`order_capability=false` on receipts. Missing source is `MISSED_SESSION`; an
explicit empty source is `EMPTY`; delayed-only source is `PARTIAL`. No source,
training, return, alpha-cycle, dataset-builder, scheduler, task, or active
runtime files are modified.

The existing provider capture remains the supported acquisition route. The R2
sidecar consumes its retained raw-events file, so its receipt must be joined to
the provider receipt and source/configuration hashes by the independent
verifier. The exact start, stop, and same-command resume instructions are in
`docs/operations/r2_observation_sidecar.md`.

## Verification

```text
py -3.13 -m pytest tests/test_isolated_observer.py tests/test_capture_operations.py tests/test_daily_intraday_capture_runner.py tests/test_intraday_evidence_store.py tests/test_intraday_evidence_contracts.py -q
39 passed

py -3.13 -m ruff check intraday_scanner/observation scripts/run_isolated_observer.py tests/test_isolated_observer.py
All checks passed

py -3.13 scripts/run_isolated_observer.py --help
PASS

git diff --check
PASS
```

Coverage includes full census, duplicate and reordered replay, atomic cursor,
interrupted trailing append recovery, lock contention, unsafe-root rejection,
manifest/source-config mismatch, delayed timing, missing source, stop marker,
and event-bound enforcement.

## Supported continuation boundary

As of the checked-in 2026 calendar, the next session after 2026-09-09 is the
regular XNYS session on 2026-09-10. The calendar verification command and the
bounded observer start/stop/resume commands are documented, but no scheduler,
heartbeat, task registration, credential, or active runtime mutation has been
performed. A runner receipt alone does not prove provider capture.

## Usage and provenance

The account usage baseline supplied for this packet is codex primary at 53%
used in the 10080-minute window, with zero purchased credits and
`spendControlReached=false`. No reset was consumed. The available usage view
does not provide per-model token or cost metering, so no Luna-specific cost
split is claimed. The invocation requested `gpt-5.6-luna`; independent runtime
model attestation remains unavailable and is intentionally not asserted here.
