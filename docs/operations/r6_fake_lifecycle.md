# R6 authenticated fake lifecycle

This packet exercises a source-bound v2 `StrategyDecisionReceipt` through the
existing `apply_alert_gate` consumer, then creates an immutable entry intent
for a network-free fake broker.  The intent binds source identity, source
configuration hash, market date, account, host, producer code SHA, and plan
hash.  The adapter rejects stale or mismatched bindings, forged hashes,
missing risk inputs, and the exclusive 15:30 ET V5 entry cutoff.

The fake runtime risk surface remains separate from the existing V5 and
portfolio authorities.  The runtime-equivalent defaults are 0.5% risk, 10%
symbol cap, three concurrent positions, five entries per day, and 2% daily
loss.  V5 is evaluated independently at its existing stricter bound; the
portfolio authority is not replaced or widened by this packet.

`CanonicalLedger` is an isolated SQLite account ledger.  Fill IDs are the
idempotency key, cash is reconciled in cents, fees are retained, and unresolved
close failures leave the position open.  Ambiguous entry acknowledgements are
looked up by client ID before any retry.  The entry kill switch only blocks new
entries; exit management remains available.  The close driver begins its exit
path at ten minutes before the supplied session close.

The broker class and fixtures are explicitly fake and carry the policy ID
`dawnstrike-r6-fake-broker-v1`.  They do not load credentials, construct URLs,
or call a provider.  Passing tests demonstrate plumbing and accounting only;
they do not qualify actual broker fills, strategy edge, economic returns,
adaptive value, or retirement readiness.  R3-owned service and dataset files
were not modified.
