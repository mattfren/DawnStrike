# WP007 Sol Design Decision — Honest Read-Only Product Projection

Date: 2026-08-16
Status: `CAPSULE_READY`

WP007 is the final product-integration package after accepted WP006. It mounts
one canonical, bounded “Today's Best Opportunities” read model in both existing
product surfaces without changing their navigation, enabling writes, or
representing synthetic/heuristic research as validated trading evidence.

## Canonical read model

- Define one immutable projection contract with states `DISABLED`,
  `DATA_UNAVAILABLE`, `NO_QUALIFYING`, and `QUALIFYING`.
- Build the projection only from a fully reconstructed, self-verifying persisted
  `PipelineResult`; never join ad hoc table fragments or recalculate decisions
  in the UI.
- Read the latest persisted opportunity run through SQLite URI `mode=ro` and
  `PRAGMA query_only=ON`. Missing database, schema below opportunity persistence,
  no persisted run, corruption, and replay failure become explicit bounded
  `DATA_UNAVAILABLE` reason codes. Public reasons must not expose local paths,
  SQL, stack traces, credentials, or unsanitized exception text.
- A verified run with no `WATCH` or `TAKE` decisions is the only source of
  `NO_QUALIFYING`, rendered exactly as
  `NO QUALIFYING TRADE CURRENTLY EXISTS.` Data absence must never be presented
  as a correct no-trade decision.
- Bound qualifying rows to five and preserve the pipeline's deterministic rank
  order. Every row binds symbol, strategy/version, direction, decision,
  lifecycle, exact evidence kind, market/security regimes, triggered anomalies,
  liquidity, why, risks/vetoes, entry, invalidation, target, and limitations.
- Missing numeric truth remains `None`/“Not available,” never zero. Heuristic,
  empirical, lifecycle, and validation labels remain separate. A heuristic or
  merely empirical research statistic must not be labeled validated.
- Every projection remains research-only and must expose no TAKE authorization,
  order route, broker action, promotion control, or lifecycle mutation.

## Feature-off behavior

- The source feature flag is `DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED` and is
  false unless its normalized value is explicitly `1`, `true`, `yes`, or `on`.
- With the flag off, Streamlit performs no opportunity-database query and the
  existing five-tab product renders unchanged.
- The public build emits or tolerates a disabled projection payload by default.
  Its existing six-view navigation remains unchanged; the bounded panel lives
  inside Overview and stays hidden when disabled.
- Enabling the public projection is an explicit build input. Static JavaScript
  only renders the canonical payload and cannot query SQLite, providers, or a
  broker.

## Product surfaces

- Streamlit: render inside the existing `Today` tab, after current Today content
  and before the permanent research/no-orders footer. Do not add, remove, or
  rename tabs or existing controls.
- Static dashboard: render inside the existing `Overview` view. Do not add,
  remove, or rename navigation views. Use safe DOM construction/escaping and
  preserve all current loading/failure behavior for other public artifacts.
- Keep all copy concise, research-only, and honest about data limitations.

## Public artifact boundary

- If a JSON writer/publication integration is needed, it writes only to an
  explicit disposable/staging destination and may read the source database only
  through the read-only adapter.
- The payload is deterministic canonical JSON, bounded, path-free, and backed by
  a SHA-256 manifest when included in the governed public publication set.
- Existing daily/public build integrity and atomic-publication guarantees must
  remain valid. No deployment or live publication is part of WP007.

## Acceptance

- Contract tests cover deterministic serialization, rank bounding/order,
  heuristic/empirical/lifecycle labeling, null preservation, limitation
  aggregation, and defensive research-only behavior.
- Store tests cover feature-off zero-open behavior, missing/old-schema/no-run,
  latest-run selection, from-JSON replay/tamper failure, query-only enforcement,
  and no database/WAL/SHM/journal creation.
- Render tests cover disabled, unavailable, qualifying, and verified no-trade
  states on both surfaces, including the exact no-trade sentence and no
  unsupported statistic.
- Existing five Streamlit tabs and six static views are byte/semantically
  unchanged while the feature is off.
- Relevant public build/publication/DOM-safety tests, Node syntax, accepted
  opportunity/validation gates, Ruff, mypy, compileall, diff-check, and import
  firewalls pass.
- Active state remains schema 26 and is unchanged by hash, size, timestamp, and
  sidecar inventory.

## Prohibited claims and actions

No real locked-OOS evaluation, empirical edge, profitability, promotion,
production readiness, provider/network call, active-state migration/write,
broker/order action, scheduler change, deployment, commit, stage, or push is
authorized by WP007.
