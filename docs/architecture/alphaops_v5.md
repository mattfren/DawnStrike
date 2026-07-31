# AlphaOps v5 Prospective Contract

AlphaOps v5 is a research-only, simulated-paper contract. It cannot submit,
route, or automate broker orders.

## Activation and identity

- Activation: `2026-07-31T00:00:00-04:00`
- Strategy: `alphaops_v5`
- Strategy version: `dawnstrike-alphaops-v5.0.0`
- Account: `alphaops_v5_simulated`
- Simulated opening equity: `$100,000.00`
- Policy: `alphaops-v5-official-paper-policy-2026-07-31`
- Cost model: `alphaops-v5-cost-model-50bps-0.005ps`

The boundary is prospective. AlphaOps v4 rows remain outside this account and
must not be rewritten, reclassified, or used to infer unsupported account
returns.

## One official-paper predicate

A candidate is eligible only when every deterministic check passes:

```text
decision == clean_edge
alert_gate in {PASS, ALERT_OK}
manual_confirmation_required == false
required evidence is complete and current
entry time is in [09:30, 15:30) America/New_York
quote age <= 360 seconds and no future bar is used
target is independently derived from market structure
actual after-cost reward/risk >= 1.50
chase <= 2%
stop distance <= 15%
gap is in [0%, 50%]
premarket dollar volume >= $1,000,000
spread <= 200 bps
source confidence >= 80
source count >= 2
float, catalyst, halt, SEC, and corporate-action evidence pass
risk and symbol-notional limits pass
```

`probability_fallback`, `watch_only`, `NEEDS_CONFIRMATION`,
`legacy_body_recovered`, missing-source, no-trade, and malformed-risk rows are
research-only. They cannot create an official paper position.

The feasibility score explains how much of the contract passed; it never
overrides a failed predicate.

## Independent target and costs

The target basis must be one of:

- sourced resistance;
- premarket range extension;
- ATR extension;
- VWAP structure;
- prior resistance.

The engine rejects targets derived directly from risk. It models 50 bps entry
slippage, 50 bps exit slippage, and `$0.005` per share per side before testing
the 1.50 reward/risk floor.

## Risk sizing

The frozen account limits are 0.25% simulated equity risk per position and 10%
gross symbol notional:

```text
risk_per_share =
  abs(expected_entry - stop) + conservative_per_share_cost_buffer

shares =
  floor(min(
    max_symbol_notional / expected_entry,
    simulated_account_risk_budget / risk_per_share
  ))
```

A zero, negative, missing, or non-finite input blocks sizing. The decision
trace records every observed value, threshold, check result, computed cost,
share count, and a deterministic fingerprint.

## Canonical accounting

The v5 ledger tracks beginning equity, cash, sourced position marks, realized
gross P&L, unrealized gross P&L, modeled costs, realized net P&L, external
flows, ending equity, cash benchmark, market benchmark, and excess return.

For every complete row:

```text
ending_equity =
  beginning_equity
  + external_flows
  + realized_net_pnl
  + unrealized_net_change

daily_net_return =
  (ending_equity - external_flows - beginning_equity) / beginning_equity
```

An observed eligible day with no position is `NO_TRADE` and may have an
observed zero return. Missing marks, costs, outcomes, benchmarks, or account
basis remain null and produce `PENDING`, `MISSING`, `PARTIAL`, or
`UNAVAILABLE` instead of `0.00%`.

Performance, Calendar, daily finalize, readiness, and public artifacts consume
this same ledger. Calendar and Performance are therefore views of one
calculation, not separate return engines.

## Safety invariants

- `broker_execution_enabled` is always false.
- No broker adapter or order-placement path is introduced.
- Missing truth is never converted to zero.
- Only complete, sourced, reconciled outcomes may enter learning.
- V4 history remains frozen and separate from prospective v5 account truth.
