# Performance reconciliation

## Current evidence

The read-only source database is
C:/Users/MattFields/Dawnstrike/data/shadow_real.sqlite. It contains seven
official paper positions, fourteen fills, eight outcome rows, and 228 research
signals. The isolated copied-source rehearsal uses
C:/r/dawnstrike-10of10-evidence/shared.sqlite; no shared database write was
used for this revalidation.

As of market date 2026-07-29, the canonical service reports:

- 425 rows: 228 alphaops_signal_research, 7 official_forward_paper, 63
  historical_backtest, and 127 shadow_challenger;
- 222 daily cohort records;
- 46 discrepancies: 25 missing-outcome warnings plus 21 PaperOps
  component-scope warnings;
- PaperOps source: 190 rows, 190 accepted, 0 quarantined, 21 warnings, and
  0 source return-field mismatches;
- input hash
  81b64fbc7695c1ace3b3d6f983bb802a35a9808a10979dff530974d6aad3f001;
- output hash
  9ae942a4ba0a0132e1ad2b3e60e785be16db957ea1af39712e4dd8c9ef902579;
- service status PARTIAL; the CLI remains non-green because unresolved
  outcomes and upstream readiness evidence remain.

No benchmark-performance rows are present in the source database. The
official-paper balance/return path still lacks the complete benchmark and
cost evidence required for a green public return or excess-return claim.
Missing truth remains null; it is not converted to zero.

## PaperOps semantics and adapter correction

The producer writes total_pnl as current_equity - previous_ending in
intraday_scanner/v2/paper_ops/shadow_runner.py. Its realized and unrealized
fields are not guaranteed to have the same period scope as the ending-equity
series. The canonical adapter now:

1. tracks prior ending equity within each
   mode/strategy/version/policy/semantics series;
2. derives daily P&L and return from the observed equity delta;
3. accepts a row only when the daily or cumulative P&L identity is provable;
4. retains component-scope mismatches as warnings when the daily equity delta
   is proven;
5. reports PaperOps costs as reported_not_reconciled unless their inclusion
   in the observed equity delta is proven; and
6. keeps replay and forward rows in separate cohorts.

The source's daily_return_pct field is diagnostic only. The current export
matches the derived equity return for all 190 rows. The 21 warnings identify
rows where the source's realized/unrealized component fields do not reconcile
to fixed policy starting equity even though the day-over-day equity delta is
auditable. They do not authorize an after-cost profitability claim.

## Official-paper limitations

The raw seven-position source P&L reconciles exactly to -$459.6706 using the
source's four-decimal dollar values. That is a diagnostic, not a portfolio
return: official opening-equity, benchmark, and complete cost evidence remain
incomplete. The public service therefore keeps official unsupported return,
excess-return, and calibrated-probability fields unavailable.

## Retention incident

A persistence-enabled build was accidentally pointed at the shared database.
It added the derived 425 canonical rows, 222 daily rows, and notification
id=92; raw positions, fills, outcomes, signals, and broker state were
unchanged. No trusted pre-write copy with the original 5/2 derived counts was
found. The owner approved retaining the current derived state on 2026-07-30.
That approval is scoped to retention/audit only; it does not authorize further
shared writes, scheduler registration, production promotion, or broker
execution.

## Deployment truth

Production remains the old X3 deployment
dpl_ErcbSKoHYNf595t7zHK6HxyMdLge. Its /api/health endpoint is HTTP 200, while
/api/readiness remains HTTP 500 FUNCTION_INVOCATION_FAILED; it exposes the old
scanner, Telegram, and cron surface.

The latest preview is
dpl_H9oNEQrV9TBwCSkKtxa5f7hz5Auj at
https://dawnstrike-command-center-x3-f3n649rln-mattfrens-projects.vercel.app.
It was built from implementation SHA
808bbceabdfc931d83fe9a1d827375a7d622a586, before the current PaperOps
adapter correction. Its readiness is intentionally HTTP 503 with
snapshot_not_publishable and pipeline_not_ready. A fresh local build and
preview are required before any promotion discussion.
