# Performance reconciliation

Current source database: `C:\Users\MattFields\Dawnstrike\data\shadow_real.sqlite`.
The source tables contain seven official paper positions, fourteen fills, eight
outcome rows, and 228 research signals. The canonical service keeps the
cohorts separate.

Read-only as-of evidence from a copied database at
`C:\r\dawnstrike-10of10-evidence\shared.sqlite`:

- 235 canonical rows: 228 `alphaops_signal_research` and 7
  `official_forward_paper`;
- 32 daily cohort records;
- 25 missing-outcome discrepancies, printed by the CLI;
- no benchmark observations and no portfolio-equity observations;
- CLI exit code `2` because the discrepancies remain unexplained;
- the bounded diagnostic snapshot is 232,001 bytes for 235 rows;
- readiness remains HTTP 503 with `snapshot_status=degraded` and
  `upstream_status=not_recorded`.

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
