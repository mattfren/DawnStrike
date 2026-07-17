# Dawnstrike Daily Paper Strategy and Learning Loop

## Purpose

Dawnstrike is a research and paper-audit system. It does not place broker
orders. Its daily job is to preserve the exact decisions that were made,
simulate their execution under explicit rules, reconcile them to sourced market
data, and retain comparable evidence without converting missing truth into a
zero return.

No honest algorithm can guarantee that every trade or day is non-negative.
Dawnstrike instead targets positive out-of-sample expectancy after costs,
bounded loss, complete attribution, and evidence-gated retirement or promotion
of strategies.

## Two Separate Strategy Horizons

Returns from these lanes must never be blended:

1. **AlphaOps v4, intraday**
   - The immutable `official_telegram` cohort is the exact set rendered in the
     morning message.
   - The five-minute watcher creates paper-only intents, fills, positions, and
     exit events for that cohort.
   - End-of-day reconciliation uses complete sourced one-minute bars to rebuild
     the authoritative first-touch entry and target, invalidation, or close
     exit under `alphaops_intraday_first_touch_v1`.
2. **PaperOps, daily swing**
   - Registered deterministic strategies run independently with separate
     strategy accounts.
   - A signal from day D cannot fill before the next valid bar after its signal.
   - `forward` evidence is production paper evidence. `replay` is historical
     research evidence and is never silently counted as forward evidence.

The current nine daily strategies are:

- `ts_momentum_sma_atr`
- `donchian_breakout_20_10`
- `cross_sectional_relative_strength`
- `pullback_reclaim_uptrend`
- `volatility_contraction_breakout`
- `failed_breakout_reversal_short`
- `bullish_fvg_continuation`
- `gap_up_continuation`
- `gap_up_continuation_atr`

## Scheduled Daily Flow

### 08:10 Central

`Dawnstrike AlphaOps Morning` collects public premarket evidence, scores the
AlphaOps model, freezes exact `signal_selections`, sends the Telegram message,
and records per-signal delivery membership and the rendered-body SHA-256.

### 08:35-15:35 Central, every five minutes

`Dawnstrike AlphaOps Monitor 5m` runs
`scripts/run_alphaops_monitor_full.bat`:

1. `alpha-monitor` observes only the exact official cohort.
2. `trade-watch --mode paper_execute` creates or updates the auditable paper
   lifecycle. `live_execute` remains locked and no broker adapter is called.

If exact Telegram membership cannot be proven, both stages fail closed rather
than consuming every ranked candidate.

### 15:15 Central

`Dawnstrike AlphaOps EOD Full Report` runs
`scripts/run_alphaops_eod_full.bat` after the US regular close:

1. Capture complete sourced one-minute AlphaOps outcomes.
2. Atomically reconcile exact selections to evaluations, paper trades,
   activation labels, return labels, and daily scorecards.
3. Run every currently eligible PaperOps strategy in `forward` mode.
4. Verify the PaperOps calendar and ledger.
5. Run each pre-registered frozen shadow challenger on the champion's exact
   date, source snapshot, universe, and execution policy, then rerun all truth
   gates.
6. Rebuild and verify the signal-to-close trade blotter, evaluate challengers,
   and score champion strategy evidence.
7. Write the horizon-separated fleet report and send the verified Telegram
   digest through its durable outbox.
8. Run daily mover review, return attribution, historical/calendar reports,
   and AlphaOps learning only after the source and reconciliation gates pass.

Any failed source, membership, reconciliation, or calendar gate returns a
nonzero scheduler result. Learning does not run through an unresolved gate.

## Truth and Return Semantics

- `not_triggered` is a conclusive activation label and a resolved no-entry. Its
  trade return is `N/A`, never `0%`.
- Missing or incomplete source coverage is unresolved. It is not a loss, win,
  flat, or zero.
- AlphaOps daily return is net return on allocated paper capital after
  configured slippage and fees. Fleet-level cumulative AlphaOps performance is
  `sum(canonical realized net P&L) / sum(canonical allocated notional)`. Daily
  return compounding is retained only as an explicitly hypothetical comparison;
  it is not reported as actual realized cumulative performance.
- PaperOps `daily_return_pct` is the incremental change from the prior session's
  ending strategy equity. `cumulative_return_pct` is ending equity relative to
  starting equity. Despite the legacy column name, both values are stored as
  decimal fractions (`0.01` means 1%).
- PaperOps drawdown is measured from the strategy account's prior equity peak.
- Open positions contribute sourced mark-to-market P&L; reports must distinguish
  unrealized evidence from closed-trade wins and losses.
- Benchmark-relative fields are horizon and mode specific. AlphaOps uses a
  persisted same-day SPY open-to-close observation as market context; PaperOps
  uses a forward PaperOps benchmark row only when one exists for the same date
  and mode. Missing benchmark truth remains `N/A`.
- The cash comparison is the explicit `cash_no_trade_baseline` catalog policy at
  0%. It is not an interest-bearing cash-rate estimate.

## Learning and Promotion Gates

- AlphaOps maintains separate activation and return datasets. A no-entry can
  teach trigger selectivity but cannot become a return label.
- Activation evidence affects scoring only at 20 or more conclusive samples and
  is capped at plus or minus five alpha-score points.
- PaperOps retains replay and forward evidence separately. Strategy evidence
  requires at least 30 forward days and 30 forward closed trades, positive
  after-cost expectancy, profit factor of at least 1.1, and drawdown no worse
  than -15% before validation is possible.
- A negative-expectancy strategy with forward closed trades is quarantined; it
  is not silently tuned on the same observations and redeployed.
- After at least ten exact forward closes, a currently quarantined champion
  can receive a version/policy/fingerprint-specific governance pause that
  blocks new entries while daily decision coverage continues. This is an early
  risk stop, not evidence that a replacement is better.
- Challengers are frozen and registered before their eligible outcomes. They
  use namespaced paper state and require matched forward sessions,
  walk-forward evidence, and an untouched holdout. Passing creates at most an
  audited manual-review proposal; automatic promotion is disabled.
- A newly registered exact strategy or execution-policy identity begins forward
  observation on the next covered market session. Replay may evaluate earlier
  dates, but it is labeled counterfactual and never merged into forward proof.
- Model or rule changes require a new strategy version so old and new evidence
  cannot be merged accidentally.

## Authoritative Storage

AlphaOps SQLite tables:

- `strategy_versions`
- `signal_selections`
- `notification_delivery_memberships`
- `trade_intents`, `paper_positions`, `paper_trade_fills`
- `strategy_evaluations`, `strategy_paper_trades`
- `strategy_learning_labels`, `daily_strategy_scorecards`

PaperOps file ledger:

- `data/v2_paper_ops_live/ledger/paper_ledger.jsonl`
- `data/v2_paper_ops_live/calendar/strategy_daily_returns.csv`
- `data/v2_paper_ops_live/exports/paper_trade_blotter.json`
- `data/v2_paper_ops_live/reports/strategy_evidence_scores.json`
- `data/v2_paper_ops_live/reports/challenger_evaluation_latest.json`

`data/v2_paper_ops_live` is the canonical scheduled-production root. The EOD
chain sets `DAWNSTRIKE_PAPER_OPS_ROOT` to that path when the variable is absent,
and passes the same value to every PaperOps phase and the fleet report. Read-only
operator adapters use the same variable and default. An explicit function or CLI
argument still wins for isolated tests and research runs. `data/v2_paper_ops` is
the original v1/demo artifact tree and must not be used as current live truth.
See `docs/architecture/paper_ops_production_root.md` for consumer scope and the
intentionally parked legacy surfaces.

Combined, horizon-separated operator artifacts:

- `outputs/strategy_fleet/strategy_fleet_report.json`
- `outputs/strategy_fleet/strategy_fleet_daily.csv`
- `outputs/strategy_fleet/strategy_fleet_summaries.csv`
- `outputs/strategy_fleet/strategy_fleet_report.md`

## Operator Checks

```powershell
py -m intraday_scanner.cli scheduler-doctor --root C:\Users\MattFields\Dawnstrike
py -m intraday_scanner.cli alpha-paper-reconcile --db-path data\shadow_real.sqlite --market-date YYYY-MM-DD --persist
py -m intraday_scanner.v2.paper_ops verify-calendar --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops rebuild-ledger --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops verify-blotter --output-root data\v2_paper_ops_live
py -m intraday_scanner.v2.paper_ops evidence --output-root data\v2_paper_ops_live
$env:DAWNSTRIKE_PAPER_OPS_ROOT = "data\v2_paper_ops_live"
py -m intraday_scanner.cli strategy-fleet-report --db-path data\shadow_real.sqlite
```

The system is operating correctly only when the scheduler, exact membership,
sourced outcome coverage, ledger reconciliation, and strategy calendar all
pass. A Telegram message by itself is not proof of a paper trade or a return.
