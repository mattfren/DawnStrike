# Weekly challenger evidence adapter

The scheduled weekly path invokes `strategy-challenger-evaluate-weekly` only
after the exact-release Daily Finalize gate. The adapter reads one fixed local
contract, `state-root/strategy_challenger_evidence_manifest.json`; it does not
accept caller-supplied eligibility flags, latest snapshots, replay returns, or
counterfactual outcomes. Manifest paths must remain below the approved state
root and every referenced input is checked against its declared SHA-256 hash.

The current checkout has no authenticated CommitBridge/FillTruth producer.
`intraday_scanner.alpha.fill_truth.has_authenticated_committed_fill_truth` is
intentionally fail-closed, so the adapter writes one immutable date receipt
with `NOT_EVALUABLE_AUTHENTICATED_FILL_TRUTH_MISSING` and null metrics when
that producer is absent. JSON self-hashes, PaperOps replay, daily OHLC, and
caller-provided fill hashes cannot satisfy this gate. A future producer must
provide point-in-time closed after-cost FillTruth joined to each prospective
shadow decision, with source-manifest, code-SHA, frozen-window/calendar, and
same-SHA Finalize lineage before evaluation can become eligible.

Malformed or conflicting manifests, wrong date/SHA/window, changed referenced
bytes, and retrospective/counterfactual decision payloads fail nonzero. Honest
absence of evidence completes as non-evaluable; no champion or broker policy is
changed and no missing value is converted to zero.
