# One-percent account/session target contract

This contract is the accounting boundary for Dawnstrike's research-only
paper accounts. It is shared by V5 and V6 producers and is deliberately
independent of signal quality, strategy selection, position sizing, and risk
admission.

## Canonical identity

For every expected market session, the account return is calculated from the
total account equity, not from a trade, signal, or average of per-symbol
returns:

```text
R_t = (ending_equity_t - beginning_equity_t - external_flows_t)
      / beginning_equity_t
```

Equity values are persisted as integer cents. `beginning_equity_t` must be
positive. Missing inputs produce a missing result; they are never coerced to
zero. The target is exactly one percentage point (`0.01` as a fractional
return, or `1.00` percentage points). A complete session is marked
`COMPLETE_TARGET_MET` when its canonical return is at least the target and
`COMPLETE_TARGET_NOT_MET` otherwise.

## Expected-session completeness

`expected_market_sessions` is the denominator. The system must create one
expected session row for each exchange session in the governed calendar before
evaluating account performance. A row that is absent from that calendar is
not eligible to be reported as a no-trade day.

Every account/session ledger row carries the expected session ID, evidence
mode, experiment/arm identity when applicable, and lineage hash. V5 and V6
rows must retain these fields through persistence and publication.

## Status semantics

| Status | Meaning | Return allowed |
| --- | --- | --- |
| `COMPLETE_TARGET_MET` | Complete authoritative account evidence; return met or exceeded 1%. | Numeric |
| `COMPLETE_TARGET_NOT_MET` | Complete authoritative account evidence; return below 1%. | Numeric |
| `NO_TRADE` | An authoritative session receipt proves no trade and no account change. | Exactly 0% |
| `PENDING` | The expected session exists but final evidence is not complete. | Null |
| `MISSING` | Expected session or required account evidence is absent. | Null |
| `DEGRADED` | Evidence is present but fails a quality/reconciliation requirement. | Null |
| `QUARANTINED` | Evidence is isolated pending investigation and cannot be used. | Null |

`NO_TRADE` is only valid with an authoritative receipt that identifies the
account/session. A caller-provided boolean, an empty trade list, or a
self-generated hash is not an authoritative receipt. Missing, degraded, and
quarantined rows remain blocking states and cannot be counted as zeros.

## Research boundary

All rows created under this contract are immutable research records:

```text
research_only = true
broker_execution_enabled = false
```

The one-percent target is an evaluation criterion only. It must never control
position size, force a trade, bypass risk admission, or enable a broker order
surface. Historical provider data uses `retrospective_research`; observations
captured during the live prospective protocol use `forward_observed`. These
cohorts cannot satisfy one another's evidence requirements.

## Schema sidecar

Migration 31 adds the append-only evidence sidecar tables:

- `expected_market_sessions`
- `intraday_capture_runs`
- `committed_fill_truth_receipts`
- `experiment_trial_ledger`

The existing governed schema marker remains 30 because migrations 31 onward
are additive sidecars consumed by stores that retain legacy schema markers.
The migration is idempotent and installs update/delete guards on each table.
