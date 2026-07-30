# Shared derived-state retention approval

Status: `LOCAL_VERIFIED`

Owner decision recorded in the Codex task on 2026-07-30: retain the current
derived read model after the accidental persistence-enabled build. This is
approval to retain and audit derived state only. It is not approval for
scheduler registration, production promotion, broker execution, or any raw
data rewrite.

## State at approval verification

The shared database was opened read-only at:

`C:\Users\MattFields\Dawnstrike\data\shadow_real.sqlite`

Observed at `2026-07-30T05:30:48-05:00`, SHA-256
`ED9FA44363E595DE993D333E71803949F9254F5269867041E6D13F8D5EEF8164`.

| Table | Rows |
|---|---:|
| `paper_positions` | 7 |
| `paper_trade_fills` | 14 |
| `signal_outcomes` | 8 |
| `historical_signals` | 228 |
| `portfolio_performance_rows` | 425 |
| `portfolio_daily_performance` | 222 |
| `benchmark_performance` | 0 |
| `automation_runs` | 0 |
| `notifications_sent` | 92 |

The derived state remains non-green: the current isolated adapter reports 46
reconciliation issues (25 missing-outcome warnings and 21 PaperOps
component-scope warnings), 190 accepted PaperOps rows, 0 quarantined rows,
and 0 source return-field mismatches. Raw-table hashes match the existing
evidence copy; no raw positions, fills, outcomes, or historical signals were
changed. Readiness remains HTTP 503 because upstream benchmark/outcome truth is
still incomplete.

## Remaining boundaries

- Keep the public readiness response at HTTP 503 until reconciliation closes.
- Keep strategy evidence at `WAITING_FOR_FORWARD_EVIDENCE`.
- Register the daily task only from an approved merged checkout.
- Obtain separate explicit approval before production promotion.
