# v2 PaperOps Architecture

## Product boundary

PaperOps is Dawnstrike's deterministic, research-only paper execution and
strategy-comparison service. It never submits a broker order. Its job is to
retain every strategy decision, simulate the declared execution policy, mark
and close positions from sourced market bars, calculate after-cost returns,
and make the result reproducible from the append-only ledger.

The objective is improving out-of-sample expectancy with controlled risk.
Neither PaperOps nor any honest trading model can guarantee that every trade or
day will have a non-negative return.

## Canonical root and modes

Scheduled operation uses `data/v2_paper_ops_live`. Root precedence is:

1. an explicit function or `--output-root` argument;
2. `DAWNSTRIKE_PAPER_OPS_ROOT`; and
3. `data/v2_paper_ops_live`.

`data/v2_paper_ops` is a legacy/demo tree and is not current production paper
truth.

Modes are permanently separated:

- `forward` is evidence produced from decisions frozen before their outcome;
- `replay` is historical research and never becomes forward evidence; and
- `demo` is synthetic demonstration evidence and is never performance truth.

Replay initializes the destination's frozen policy and strategy manifests,
executes in a sibling staging root, verifies reconciliation, calendar, ledger
rebuild, immutable source-bar truth, and blotter truth there, then promotes
only the replay-owned artifacts. Promotion snapshots the destination and rolls
it back on failure.
Forward state is never replaced by replay state.

## Immutable DataTruth snapshots

Every new daily DataTruth build retains its normalized OHLCV, exact source CSV,
and raw provider payloads under `data/v2_data_truth/snapshots/<snapshot_id>/`.
The snapshot ID is a full SHA-256 content identity over the normalized artifact,
all retained source artifacts, provider/timeframe, symbols, and requested and
accepted date boundaries. Existing retained bytes are verified and are never
silently replaced.

Manifest schema `v2.data_truth_manifest.v2` exposes
`snapshot_relative_path`, `normalized_artifact_path`,
`raw_artifact_paths`, `normalized_artifact_hash`, `raw_artifact_hashes`,
`snapshot_content_hash`, and `manifest_payload_hash`. Artifact paths are durable
and relative to the configured DataTruth root. The named loader
`load_datatruth_snapshot(snapshot_id, output_root)` verifies the manifest and
every declared byte before returning a dataset whose `source_path` is the
retained normalized artifact. `manifests/latest.json`,
`normalized/latest_ohlcv.csv`, and shared raw copies remain mutable convenience
aliases and are never authoritative named-snapshot evidence.

## Strategy and policy identity

The active fleet is the deterministic, versioned v2 catalog; fleet size is
derived from eligible catalog identities rather than hard-coded. A series is
identified by mode, strategy ID, strategy version, execution-policy version,
and strategy-semantics fingerprint. The semantics fingerprint is a bounded
SHA-256 over the declared strategy contract, parameters, signal implementation,
and containing implementation module. Changing code or parameters under the
same version fails closed; a new version is required.

The execution-policy manifest contains a SHA-256 over all material execution
and risk configuration. Changing fees, slippage, risk limits, universe, fill
rules, or other policy semantics under the same policy version also fails
closed.

Every decision, order, fill, position, close, account row, calendar row, fleet
report, and Telegram digest carries or verifies this exact lineage.

## Daily lifecycle

`run-day` performs the scheduler-ready phases:

1. recover any durable transaction journal;
2. initialize and validate config, strategy, and policy manifests;
3. preflight the exact date, completed market session, source snapshot, and
   configured universe;
4. emit one accepted, rejected, skipped, or `no_setup` decision per
   strategy/symbol obligation;
5. convert accepted decisions to risk-checked pending orders;
6. fill no earlier than the next valid completed bar open;
7. evaluate gaps, stops, targets, timeouts, and marks;
8. atomically commit lifecycle events and state;
9. write calendar/account truth; and
10. require reconciliation, immutable source-bar truth, calendar truth, ledger
    rebuild, and trade-blotter verification before the day is reportable.

A `no_setup` decision is useful selectivity evidence but is not a trade.
`not_triggered`, pending, open, and unresolved states have no realized trade
return. They must remain `N/A`, not `0%`.

## Execution and risk semantics

The default policy uses a completed daily signal and a next-valid-open paper
fill. Entry and exit fees and directional slippage are explicit. Gap-through
stops and targets execute at the observed open before intrabar touch logic;
when both stop and target are touched in one daily bar, stop-first ordering is
the conservative assumption. Forward positions may carry and are marked to a
completed close; they are not falsely labeled EOD-flat.

Risk checks include positive finite configuration, after-cost reward/risk,
per-trade risk budget, maximum daily loss, maximum open risk, maximum gross
exposure, maximum concurrent positions, duplicate strategy/symbol exposure,
and long-cash affordability. Short results remain research estimates because
borrow availability, locate fees, and carry costs are not yet independently
sourced.

## Durable commit and recovery

Lifecycle events and their state updates commit through
`state/paper_transaction_pending.json`. Its transaction ID is a SHA-256 of the
canonical events and state updates. Recovery validates the schema, checksum,
event identities, and every target path before applying anything. The append is
idempotent; the journal is removed only after ledger and state writes complete.

Standalone reconciliation, calendar verification, ledger rebuild, trade
blotter, fleet reporting, and Telegram publication must recover or fail on a
pending transaction before reading truth. A malformed JSONL tail is
quarantined and repaired only by the locked append path; ordinary readers fail
closed instead of silently discarding it.

## Accounting and trade blotter

The ledger at `ledger/paper_ledger.jsonl` is the primary audit trail. Derived
state is reproducible from it. Ledger rebuild compares all account components
(starting equity, current equity, realized P&L, and unrealized P&L), open
exposure, trade counts, costs, and calendar returns. Unexpected account series,
mode conflicts, missing coverage, and warnings are blocking failures.

`exports/paper_trade_blotter.json` joins the exact chain:

`decision -> order -> fill -> position -> close`

The join validates IDs, symbol, direction, mode, strategy/version, policy,
semantics fingerprint, quantity, fill price, stop, target, and source snapshot.
The `v2.paper_trade_blotter.v2` row preserves the signal `run_id` and
`data_snapshot_id` and records fill provenance separately as `fill_run_id` and
`fill_data_snapshot_id`; the fill run is taken from the canonical fill event
envelope and checked against its payload. Close provenance remains separate in
`close_run_id` and `close_data_snapshot_id`.
For a close, gross P&L, entry and exit fees, net P&L, and R-multiple are
recomputed from the recorded economics and frozen cost policy. Open trades have
unrealized P&L but no realized return. Champion, challenger, archived, replay,
and forward series are summarized separately.

## Learning and challenger governance

Every active registered strategy continues to emit daily decisions even when one
is paused, preserving opportunity and rejection evidence. A sufficiently
sampled current forward series with quarantined evidence can receive an exact
governance overlay that blocks new champion entries; it does not erase the
strategy or historical decisions.

New strategy and execution-policy identities use the explicit
`next_market_session_after_registration` activation boundary. Forward scanning
and calendar rows are blocked before the later exact-series inception date;
replay remains counterfactual. This one-session activation boundary prevents a
catalog write that straddles the market close from backdating evidence into the
just-completed session. Legacy identities retain their recorded
`first_eligible_session` boundary.

Frozen shadow challengers use namespaced accounts and lifecycle state. They
must be registered before eligible outcomes exist, run on the same sourced
forward sessions and policy as their champion, and are evaluated on
walk-forward folds plus an untouched holdout. The system may propose a
candidate for audited manual review. It cannot automatically promote a
challenger, edit strategy code, or enable broker execution.

## Primary commands

```powershell
py -m intraday_scanner.v2.paper_ops run-day --date YYYY-MM-DD --mode forward --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops reconcile --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops verify-calendar --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops rebuild-ledger --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops verify-source-bars --mode forward --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops blotter --date YYYY-MM-DD --mode forward --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops verify-blotter --mode forward --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops shadow-run --date YYYY-MM-DD --mode forward --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops challenger-evaluate --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops evidence --output-root data\v2_paper_ops_live
```

## Known evidence limits

Yahoo daily OHLCV is public research data, not an exchange-grade execution
feed. Corporate actions, split/dividend treatment, and independent provider
reconciliation are not complete enough to call the P&L broker-exact. Daily bars
cannot prove intraday path beyond the declared conservative assumptions.
Forward evidence must accumulate after this policy is deployed before any
performance or improvement claim is statistically credible.
