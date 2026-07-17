# PaperOps Fleet Telegram Digest

The end-of-day PaperOps digest is a research/paper notification for the seven
daily-swing strategies and the exact official AlphaOps Telegram cohort. It does
not place broker orders and does not describe paper evidence as investment
advice or a profit guarantee.

## Truth contract

The command reads the configured production PaperOps root using this
precedence: explicit `--paper-ops-root`, `DAWNSTRIKE_PAPER_OPS_ROOT`, then
`data/v2_paper_ops_live`. It joins three artifacts for the requested market
date:

- `outputs/strategy_fleet/strategy_fleet_report.json`, which must be complete;
- `calendar/strategy_daily_returns.csv`, restricted to `mode=forward`;
- `exports/strategy_decisions_forward_YYYY-MM-DD.json`, which proves whether
  each strategy had no setup, a gated/no-fill signal, a pending entry, a held
  paper position, or forward paper fill activity;
- `ledger/paper_ledger.jsonl`, which supplies the exact forward order, fill,
  opened-position, held-position, and close lineage behind the calendar counts.

For every accepted decision, the digest requires exactly one same-run order
resolution (`paper_order_created` or `paper_order_blocked`). Opens must join
order to fill to position. Closes must join back to that exact opening lineage.
Same-day ledger events must match the calendar run ID, strategy version,
execution policy, and strategy-semantics fingerprint. Calendar opened, closed,
pending, and open-position counts must equal the resolved ledger lifecycle.
The notification includes compact rows with symbol, direction, pending
next-valid-open rule or actual fill and quantity, stop/target, and—when
closed—the close reason and after-cost net P&L. Missing values remain `N/A`.

Closed-trade economics are independently recomputed before rendering. The
close event and payload must match the calendar run, opened position ID, symbol,
strategy, and direction (direction is sourced from the immutable opening; a
conflicting close direction is rejected). For quantity `q`:

- long gross P&L is `(close - entry) * q`; short gross P&L is
  `(entry - close) * q`;
- entry fee is independently recomputed as `fill * q * fee_bps / 10,000`;
- exit fee is `close * q * fee_bps / 10,000`;
- after-cost net P&L is `gross - entry_fee - exit_fee`;
- R-multiple is after-cost net P&L divided by canonical stop risk, including
  configured stop slippage plus entry and stop-exit fees.

The stored entry fee must also match the originating fill. Entry and close
slippage are independently recovered by inverting the engine's configured
directional execution multiplier, then compared with each ledger value. A
close reason must be one of the active forward engine outcomes: `stop`,
`target`, or `timeout`. Any non-finite, out-of-bounds, cross-field, gross, fee,
slippage, net, reason, or R-multiple mismatch blocks the digest instead of
publishing the stored value as exact.

The digest labels strategy-account return separately from trade return. A day
with no eligible trade is `N/A`, never `0%`. The explicit cash no-trade policy
may legitimately be `0.00%`; it is labeled as a policy comparator. Replay rows
are counted as excluded and never blended into forward results. Missing,
duplicate, partial, non-forward, or wrong-root evidence blocks delivery.
Standalone digest builds first run the canonical PaperOps transaction recovery
and then the current calendar-truth verifier. A valid
`state/paper_transaction_pending.json` is committed and removed before any
digest reads; failed recovery blocks delivery. Duplicate/missing rows,
calendar math failures, or ledger/account reconstruction mismatches also block,
preventing a post-crash or self-consistently corrupted calendar/report pair
from being published.

Before delivery, every PaperOps row is also rebound to the current live state:
`state/strategy_registry.json` must contain the exact active
`strategy_id + strategy_version + execution_policy_version` for all seven
strategies, and `state/execution_policy_manifest.json` must agree with the
configured active policy and its immutable configuration fingerprint. The
digest recomputes the complete execution-semantic payload from the current
PaperOps config; a fee, risk, universe, or other execution-affecting edit under
the same policy version blocks immediately. Policy fingerprints are canonical,
bounded SHA-256 values.

`state/strategy_semantics_manifest.json` must also bind every registry
`strategy_id@strategy_version` to the current strategy parameters, declared
logic, and implementation source. Its canonical SHA-256 fingerprint must match
the manifest configuration, registry, current imported implementation,
same-day decisions, and exact lifecycle events. Registry and both manifest
hashes are included in the digest evidence fingerprint. Therefore, internally
consistent artifacts from an older policy, version, or same-version code body
cannot send after live semantics change.

AlphaOps does not have to invent a scorecard row to let complete PaperOps truth
ship. When the official scorecard is absent, Dawnstrike accepts an Alpha section
only if the same-date `alphaops.run_contract.v1` proves a source-successful,
non-dry-run Telegram `valid_no_edge` run with zero alertable selections and its
exact SQLite selection set contains one official `NO_TRADE` identity. The message then says
`no_signal`, labels the Alpha scorecard unavailable, and keeps return `N/A`.
Missing contracts, stale dates, failed sources, partial scorecards, selected
signals that conflict with `valid_no_edge`, or any other contradiction block
the digest. Mere absence is never presented as a no-pick result.

## Durable delivery

Run the same production command used by the EOD chain:

```powershell
py -m intraday_scanner.cli strategy-fleet-telegram `
  --date YYYY-MM-DD `
  --db-path data\shadow_real.sqlite `
  --paper-ops-root data\v2_paper_ops_live `
  --fleet-report outputs\strategy_fleet\strategy_fleet_report.json `
  --notify telegram `
  --max-attempts 3
```

Before the network request, Dawnstrike writes an outbox record under
`<paper-ops-root>/notifications/paperops_fleet_digest/outbox`. Failed attempts
remain `delivery_failed` with a redacted error and are retried on the next
invocation. Confirmed notification receipts remain in SQLite
`notifications_sent`; an identical evidence fingerprint is skipped rather than
sent twice. Corrected source evidence creates a new fingerprint and therefore
a separately auditable corrected digest.

The complete structured lifecycle is written before dispatch under
`<paper-ops-root>/notifications/paperops_fleet_digest/artifacts`. Telegram text
is capped below the platform limit. When all lifecycle rows do not fit, the
message reports the omitted count and references that exact full artifact;
the data is not silently discarded. Unrelated replay or later ledger appends
are excluded from the digest evidence fingerprint, so they cannot create a
duplicate forward notification.

Telegram failures exit nonzero. The EOD batch records that failure and keeps
the durable outbox; it never prints a success line after a failed send. As with
any external API, a process crash after Telegram accepts a request but before
the SQLite receipt commits is a narrow at-least-once delivery window.

For safe local inspection without Telegram credentials, use `--notify console`.

## Scheduler order

`scripts/run_alphaops_eod_full.bat` sends the digest only after all four gates
have succeeded for the same date:

1. the complete forward run for every currently eligible PaperOps strategy;
2. calendar verification;
3. strategy-evidence scoring;
4. the complete, date-scoped horizon-separated fleet report.

The EOD batch passes the same `RUN_DATE` as both `--start` and `--end` when it
builds the digest input. Historical gaps remain visible in the cumulative
calendar and audit reports, but an older incomplete AlphaOps session cannot
silently suppress an otherwise complete current-day PaperOps digest.

If any gate fails, the digest is not sent and the EOD run exits nonzero.
