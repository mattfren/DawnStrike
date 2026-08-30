# Luna Cycle 6: Finalize and Morning boundary

This tranche hardens Daily Finalize retries and records a terminal
`SKIPPED_NOT_APPLICABLE` state for closed or non-session dates. The terminal
state carries an explicit `NO_TRADE` funnel, while return, P&L, and picks stay
`null`; it is accepted by daily orchestration as a completed non-session
observation, not as current-day readiness.

Independent Morning core/mover concurrency is intentionally bounded at one
active acquisition lane. Both lanes are assembled by the single `alpha-cycle`
process: mover collection completes before core discovery, and the shared
`cycle_decision_at` is passed to both. This preserves the release-bound
identity, ordered core coverage receipts, and the existing provider pacing
budget. The same boundary remains in place for mover/core enrichment because
those calls share Alpaca market/news endpoints and write receipt fields that
are part of the frozen artifact.

Parallel child acquisition is therefore not safe at this contract boundary.
It requires a process-wide provider limiter, deterministic receipt coordinator,
and failure/promotion protocol before it can be enabled. The hostile contract
test also runs delayed core batches and asserts one active provider call plus
byte-identical receipt hashes, so an accidental future overlap fails closed.
