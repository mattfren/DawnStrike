# Intraday Evidence Spine

The intraday evidence spine is an append-only, source-fact layer for retained
market data. It is deliberately separate from strategy signals, paper
execution, outcomes, promotion state, and broker integrations.

## Contract boundary

Every retained record carries a provider, feed, entitlement context, request
window, UTC fetch timestamp, code SHA, raw and normalized content hashes,
retention status, and exchange-session identity. Prices carry an explicit
adjustment basis. The contracts reject non-UTC timestamps and are frozen
dataclasses with deterministic JSON serialization.

The evidence types cover:

- unadjusted OHLCV plus VWAP bars;
- trade prints with exchange, conditions, and optional sequence;
- quotes with feed, bid/ask prices and sizes, and exchanges;
- halt, resume, and LULD status intervals;
- corporate-action symbol mappings;
- raw/normalized artifact manifests; and
- coverage receipts.

Coverage is expressed only with source-data statuses:
`COMPLETE`, `PARTIAL_MISSING_INTERVALS`, `NO_DATA`, `KNOWN_HALT_GAPS`,
`ENTITLEMENT_DENIED`, `SOURCE_CONFLICT`, `CORPORATE_ACTION_UNRESOLVED`,
`HASH_MISMATCH`, `FUTURE_DATA_REJECTED`, and `DATA_INELIGIBLE`.

## Storage boundary

Schema migration 22 adds indexed, append-only lineage tables for provider
capability receipts, artifact manifests, coverage receipts, and legacy policy
classifications. SQLite stores identity, query fields, hashes, paths, status,
and extensible JSON payloads; it does not store the raw compressed bytes.

Raw and normalized artifacts are gzip-compressed outside SQLite beneath
`DAWNSTRIKE_INTRADAY_EVIDENCE_ROOT` (default `data/intraday_evidence`),
partitioned as:

```text
<provider>/<feed>/<artifact-kind>/<market-date>/<symbol>/<raw|normalized>-<sha256>.bin.gz
```

The store refuses to write when retention is not permitted. Artifact identity
is independent of content hash, making repeat writes idempotent. A different
hash for the same provider/feed/kind/symbol/session/request identity raises
`SOURCE_CONFLICT`. Files are written to a same-directory temporary path and
atomically renamed into place.

## Operational boundary

This layer is historical and research-only. Adding these contracts or
migrations does not authorize live collection, broker access, automatic
promotion, or a profitability claim. Any future collector must record the
provider capability receipt and coverage receipt before its data can become an
eligible research input.
