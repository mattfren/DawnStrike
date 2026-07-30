# Performance reconciliation

Current source database: `C:\Users\MattFields\Dawnstrike\data\shadow_real.sqlite`.
The source tables contain seven official paper positions, fourteen fills, eight
outcome rows, and 228 research signals. The canonical service keeps the
cohorts separate.

Copied-source evidence from a database at
`C:\r\dawnstrike-10of10-evidence\shared.sqlite`, reconciled with the PaperOps
calendar export:

- 425 canonical rows: 228 `alphaops_signal_research`, 7
  `official_forward_paper`, 63 `historical_backtest`, and 127
  `shadow_challenger`;
- 222 daily cohort records;
- 156 discrepancies: 25 missing-outcome warnings plus 131 PaperOps
  reconciliation errors;
- no benchmark observations and no portfolio-equity observations;
- CLI exit code `2` because the discrepancies remain unexplained;
- the bounded diagnostic snapshot is 632,094 raw bytes and 38,425 deterministic-
  gzip bytes for 250 public rows; the compressed bound passes;
- readiness remains HTTP 503 with `snapshot_status=degraded` and
  `upstream_status=not_recorded`.

The canonical PaperOps adapter reads only
`data/v2_paper_ops_live/calendar/strategy_daily_returns.csv`. Its source hash
is `ea0d0cb102e1c9ca21c096724b194be0615383304496e2636b8cce4f21e15a11`.
It found 190 source rows, accepted 85, quarantined 105, and recorded 131
row-level issues. The source's `daily_return_pct` field is not trusted for
calculation: 85 rows disagree with the return derived from opening and
ending equity. Replay rows remain `historical_backtest`; forward rows remain
`shadow_challenger`. Quarantined rows contribute neither valid returns nor
valid equity-derived performance.

The raw seven-position source P&L reconciles exactly to `-$459.6706` using
the source's four-decimal dollar values. That diagnostic is not a portfolio
return: opening equity is absent, costs are incomplete, and benchmark evidence
is absent. The public return fields therefore remain null rather than treating
notional or missing data as equity.

The service no longer uses summed trade notionals as portfolio return. Daily
return is calculated only when an opening-equity observation exists. The
notional sum remains a diagnostic exposure/allocation field. Gross trade return,
net P&L, fees, slippage, benchmark return, excess return, and portfolio return
are separate fields.

The current seven-trade source statistic remains a diagnostic and must not be
presented as a validated portfolio return. Missing benchmark, equity, and cost
evidence keeps readiness degraded.

## Current authoritative revalidation

The same reconciliation was rerun read-only against
`C:\Users\MattFields\Dawnstrike\data\shadow_real.sqlite` and
`C:\Users\MattFields\Dawnstrike\data\v2_paper_ops_live` as of
`2026-07-29` before the fresh build. It returned `status=DEGRADED`, `row_count=425`,
`daily_count=222`, `issue_count=156`, and CLI exit code `2`. The input hash was
`c5b422baac4fc37a02cefa9a0851b9343f0859ad7efce3dad2c3ab85da0d7891` and the
latest isolated public output hash is
`c5a70f448e5a3269bc35fa512b7f885c1e3a3e48d4ac9afaaf83b240f35cea64`.
PaperOps reported 190 source rows, 85 accepted, 105 quarantined, 131 issues,
and 85 source return-field mismatches. The pre-build shared database had only
5 portfolio-performance rows, 2 daily-performance rows, and 0 benchmark
rows, so it could not support a green canonical return publication. The fresh
build was then mistakenly run with the shared DB as its persistence target;
the shared DB now contains the 425/222 derived rows and one additional console
notification. Exact recovery of the prior 5/2 derived state requires an
owner-supplied backup or an explicit retention decision. The owner has approved
retaining the current derived state; it remains degraded and non-publishable.
