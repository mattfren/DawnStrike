# Dawnstrike no-picks incident audit — 2026-08-27

## Verdict

The 08:00 CT Morning task did not crash and the authenticated discovery feeds were not empty.
The registered runtime completed collection and ranking, but two freshness-lineage defects
prevented safe Tier 1 publication. The scheduled path converted the local market date to noon UTC,
so valid near-08:00 CT core observations were classified as future. Separately, the mover path did
not carry a verified enrichment-freshness verdict through the snapshot model. The mover lane was
also limited by above-ceiling fallback data. The resulting zero-name slate was therefore a
publication-integrity defect, not evidence that the S&P 500 and Nasdaq-100 contained no research
candidates.

This audit is read-only. Morning was not rerun. Broker execution remained disabled.

## Exact production evidence

- Registered runtime SHA: `5190ab6beb1b81556bfc70640c43a4cff48bd1f8`
- Scheduled task: `Dawnstrike AlphaOps Morning`
- Last run: `2026-08-27 08:00:01-05:00`
- Task result: `0`
- Daily run ID: `daily-47b2ef52dbe25e13a8b01ee2`
- Daily status at observation: `IN_PROGRESS`; Morning collection and ranking delivery were
  `COMPLETE` while the intraday monitor remained active.
- `alpha_cycle.json` SHA-256:
  `9057be9fa6a67324c7cab033fe830b260867ec9c2df9f4d0d0c68eb9e4eb9282`
- `ranked_research_slate.json` SHA-256:
  `400dbebf6a697666e8636e1adc6a61e34d411078f24b1ca4ceee0028728ad217`
- `alpha_run_contract.json` SHA-256:
  `87f1fb41603ee93e220bac04ef0106efb48ffef065660eebaf3b5a2f0504ff85`
- `alpha_morning-2026-08-27.receipt.json` SHA-256:
  `0058461c24bfa9ab3cdc22790d37416bb7224956390f2e89f27d04802cb78c54`

## What actually happened

1. Mover collection succeeded with 83 candidates.
2. The point-in-time core-universe contract was `READY`. Eleven authenticated Alpaca batch
   receipts requested and returned all 519 unique members with no missing, unknown, or duplicate
   symbols.
3. The daily cycle started at `2026-08-27T13:00:06.1432111Z`, but `--market-date 2026-08-27`
   caused the Alpha cycle to use `2026-08-27T12:00:00Z` as its observation time. All 519 core rows
   were consequently marked stale/future. Ten source timestamps—including AAPL, MSFT, GOOGL,
   AMZN, AMD, and MU—were actually within the configured 600-second window relative to the real
   cycle start (including the allowed 60-second provider-clock tolerance).
4. The cycle produced seven mover signals, but those rows had no authenticated freshness verdict
   after enrichment/model serialization, so the frozen five-name research slate published zero.
5. The exact slate safety blocker was `freshness_missing_or_not_current`. Its coverage limitations
   were `core_enrichment_not_data_eligible` and
   `mover_secondary_fallback_above_ceiling`.
6. The run contract consequently reported zero ranked-research, paper-plan-qualified,
   alertable, and official selections. It retained `research_only=true` and
   `broker_execution=disabled`.

The scheduled wrapper supplied only a market date. At the registered SHA, `alpha_cycle` converted
that date to noon UTC rather than receiving one actual cycle observation timestamp; premarket
quotes collected during the following hour therefore looked future-dated. The production path
also writes rows to CSV and reloads them through `SnapshotRow` before safety-qualified slate
construction. `freshness_status` was not a snapshot/candidate model field, and the mover
enrichment path did not derive a freshness verdict from its current content-hashed observation.
The slate gate rejected the resulting unknown rows exactly as designed. A separate global
mover-fallback ceiling also limited otherwise independent core evidence at this old runtime.

## Separate operational blocker

At the same observation, `scheduler-doctor` remained `BLOCKED_EXTERNAL` because the prior day's
Finalize task retained result `267014`. That release-governance failure is separate from the
successful Aug. 27 Morning task and does not explain the zero-name slate. It must still be cleared
by a legitimate scheduled Finalize result before production proof can close.

The scheduled Aug. 27 Finalize later ran at `17:30:02-05:00` and cleared this separate blocker
without manual intervention. The old-SHA run completed at `17:33:03-05:00` with 14/14 stage rows,
zero failed or missing stages, a `READY` finalizer receipt, Task Scheduler result `0`, and
`research_only=true` / `broker_execution_enabled=false`. Calendar truth was honestly `NO_TRADE`.
That proves terminal integrity for registered SHA
`5190ab6beb1b81556bfc70640c43a4cff48bd1f8`; it does not prove the remediation candidate or
retroactively create Aug. 27 picks.

## Remediation and acceptance boundary

The strategy-remediation candidate must pass one actual observation timestamp from the scheduled
wrapper, preserve freshness and lane provenance through the exact CSV/model path, derive mover
freshness only from a current content-hashed enrichment observation, bind the row contents ranked
under each core coverage receipt, and keep failure and replay identities on the same immutable
cycle timestamp. Acceptance requires hostile regressions, the cross-lane suite, full repository
verification, an independent P0/P1 audit, a clean exact SHA on `main`, and evidence from the next
legitimate scheduled Morning-to-Finalize chain. An out-of-window Morning replay is not valid
evidence.

The current integration candidate implements explicit as-of propagation, exact core
lineage/freshness and row binding, lane-aware fallback ceilings, frozen-slate retry and cross-scan
governance, collision-safe selection identities, and fail-closed no-broker publication. It also
adds atomic watcher admission, exact lifecycle identity/proof binding, committed-FillTruth learning
quarantine, and official performance/Calendar FillTruth gating. These implementation facts do not
replace the next legitimate scheduled-chain acceptance proof.
