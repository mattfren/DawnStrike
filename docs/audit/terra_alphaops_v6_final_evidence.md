# Terra AlphaOps V6 implementation evidence

Status: **implementation complete on the isolated V6 branch; production release deliberately gated**
Date: 2026-08-01

## Delivered controls

- Clean V6 branch/worktree created from deployed SHA `692e785cf8304a8045e88ab221dc644d4eb2e9e7`.
- Durable-state SQLite snapshot captured before migration; SHA-256 recorded in `terra_alphaops_v6_initial_audit.md`.
- Every scheduled Python stage now emits a native-process receipt with exit code, duration, stdout/stderr paths, and hashes.
- Missing real source configuration is a recorded failed stage, not a silent fallback to example configuration.
- Scheduler diagnosis now exits nonzero unless all tasks are locally verified. Task definitions require a password-logon identity with network and encrypted-secret access, start-when-available, and battery-safe execution. S4U is explicitly rejected because Windows denies S4U access to the network and encrypted files.
- Public build outputs remove absolute runtime/state roots and artifact verification rejects host-path disclosure.
- Calendar remains a primary public view and V6 evidence is displayed as a shadow challenger with explicit forward thresholds.
- V6 ledger migration is additive. Decision input lineage, cost model, safety vetoes, outcomes, model runs, walk-forward evaluation, and proposed experiments are separate immutable records.
- V6 uses an after-cost net excess label, strict expanding-window validation, conditional activation/return analysis, lower-confidence-bound utility, and adverse-tail penalty.
- Missing data remains null/ineligible. The legacy edge calibrator no longer manufactures zero-return samples.
- No broker execution, automated order placement, LLM scoring, or automatic strategy promotion was added.

## Verification completed

- Focused regression suite: 60 passed.
- `ruff check`: passed.
- `mypy intraday_scanner`: passed (187 source files).
- PowerShell parser validation: passed for all modified runners and task registration scripts.
- Full `pytest`: **580 passed in 261.12s**.
- After the final V6 release-metadata addition, the V6/daily/scheduler/public-artifact focused gate passed (15 tests), with lint and type checking passing again.
- Copy-on-write static-public build produced Calendar and `data/v6-learning.json`, returned exit `2` for degraded readiness as designed, and the public artifact path scan was clean. The verifier also refused promotion (`snapshot_not_publishable`, `readiness_not_publishable`, and dirty-source proof) as designed.
- A `scheduler-doctor` run against the actual runtime returned exit `2` and `BLOCKED_EXTERNAL`; it identified all four tasks as `Interactive`, battery-unsafe, and previously failed. This is expected until the V6 registration is performed.

## Release gate still open

Production remains correctly unready because the runtime lacks a real `config/web_sources.yaml`; only the placeholder example is available. That configuration must contain a real accountable user-agent contact and be verified with `web-source-doctor`. The task-registration scripts now additionally require an approved password-logon Windows identity that can access the network, encrypted secrets, Telegram, the runtime, and the durable state root. Until both prerequisites exist, no live data collection, trustworthy return label, readiness-200 publication, or claimed performance improvement is possible.

## Performance truth

`PERFORMANCE_STATUS=WAITING_FOR_FORWARD_EVIDENCE`

V6 needs at least 60 forward sessions and 100 valid, closed, sourced, after-cost paper labels before human review can even consider a change. It makes no promise of returns and never promotes itself.
