# Dawnstrike no-picks incident audit — 2026-08-27

## Verdict

The 08:00 CT Morning task did not crash and the authenticated discovery feeds were not empty.
The registered runtime completed collection and ranking, but the publication boundary correctly
failed closed after the core-row CSV/model round-trip discarded the freshness proof required for
Tier 1 research publication. The mover lane was independently limited by above-ceiling fallback
data. The resulting zero-name slate was therefore a publication-integrity defect, not evidence
that the S&P 500 and Nasdaq-100 contained no research candidates.

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
3. The cycle produced seven signals, but the frozen five-name research slate published zero.
4. The exact slate safety blocker was `freshness_missing_or_not_current`. Its coverage limitations
   were `core_enrichment_not_data_eligible` and
   `mover_secondary_fallback_above_ceiling`.
5. The run contract consequently reported zero ranked-research, paper-plan-qualified,
   alertable, and official selections. It retained `research_only=true` and
   `broker_execution=disabled`.

The production path writes core discovery rows to CSV and reloads them through `SnapshotRow`
before safety-qualified slate construction. At the registered SHA, `freshness_status` was not a
snapshot/candidate model field, so an in-memory `FRESH` row became freshness-unknown after the
round-trip. The slate gate then rejected it exactly as designed. A separate global mover-fallback
ceiling also limited otherwise independent core evidence at this old runtime.

## Separate operational blocker

At the same observation, `scheduler-doctor` remained `BLOCKED_EXTERNAL` because the prior day's
Finalize task retained result `267014`. That release-governance failure is separate from the
successful Aug. 27 Morning task and does not explain the zero-name slate. It must still be cleared
by a legitimate scheduled Finalize result before production proof can close.

## Remediation and acceptance boundary

The strategy-remediation candidate must preserve freshness and lane provenance through the exact
CSV/model path, bind the row contents ranked under each core coverage receipt, and keep failure
and replay identities on one immutable cycle timestamp. Acceptance requires hostile regressions,
the cross-lane suite, full repository verification, an independent P0/P1 audit, a clean exact SHA
on `main`, and evidence from the next legitimate scheduled Morning-to-Finalize chain. An
out-of-window Morning replay is not valid evidence.
