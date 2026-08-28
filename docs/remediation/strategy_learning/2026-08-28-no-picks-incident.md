# Dawnstrike no-picks incident audit — 2026-08-28

## Verdict

The scheduled 08:00 CT Morning process completed successfully, discovery was not empty, and the
scanner ranked 16 mover candidates. The zero-name research slate was caused by multiple
fail-closed evidence gaps in the registered old runtime, not by an absence of market
opportunities:

1. All 16 AlphaOps v5 decision-receipt builds failed with `missing code identity`.
2. The point-in-time core contract contained 519 authenticated S&P 500/Nasdaq-100 members, but
   the old production path did not carry that full universe through current bulk enrichment and
   all strategy evaluations. The core lane therefore reported 519 snapshots but zero eligible
   or ranked rows.
3. The mover lane produced 35 enrichment-eligible rows and 16 ranked rows, but 30 of the 35
   verified rows used the secondary Yahoo source. Its 81.08% fallback ratio exceeded the
   governed 25% ceiling.
4. The frozen slate separately reported `freshness_missing_or_not_current` and
   `sec_risk_status_not_clear`. Twelve of the 16 ranked mover rows had `UNKNOWN` SEC risk status.

The direct code-identity fault is reproducible in the deployed path. Strategy receipt
construction reads `DAWNSTRIKE_CODE_SHA`, but the Morning wrapper did not resolve the runtime Git
HEAD or supply that identity to `alpha-cycle`. Neither `DAWNSTRIKE_CODE_SHA` nor
`DAWNSTRIKE_RELEASE_SHA` was present in the observed runtime environment, while the detached
runtime HEAD was `5190ab6beb1b81556bfc70640c43a4cff48bd1f8`. The remediation must therefore
bind the exact resolved runtime SHA explicitly; it must not depend on an ambient variable.

The correct result at that old SHA was therefore a fail-closed zero slate. The defect was that
the production architecture could not turn the available authenticated breadth into complete,
receipt-backed research candidates. Broker execution remained disabled.

## Exact production evidence

- Registered runtime SHA: `5190ab6beb1b81556bfc70640c43a4cff48bd1f8`
- Scheduled task: `Dawnstrike AlphaOps Morning`
- Started: `2026-08-28T08:00:22-05:00`
- Completed: `2026-08-28T08:01:39-05:00`
- Exit code: `0`
- Alpha scan ID: `aed803a2-4cae-4c8a-bd09-d8394fa44512`
- Alpha cycle status: `no_trade`
- `alpha_cycle.json` SHA-256:
  `a97a88b17ea87703343f2c24fb59fce0638f869f315d45075ccac6e93c6eb21c`
- `ranked_research_slate.json` SHA-256:
  `d902bf8a9c493d72e718df8d83573a70d36a2a5afd477f353b38edf7bb7d8c65`
- `alpha_run_contract.json` SHA-256:
  `9876a27f32eeecb80a2e08ee2b2a3774b596f80430289ca51e592ae7ea5e294e`
- `alpha_morning-2026-08-28.receipt.json` SHA-256:
  `cfe8054a963ebf31ce7b528394afe001ccd6e7d97f60b15d2632d5d016891d54`
- Core-universe contract file SHA-256:
  `a1d36c8004a408be907754f98f25c23d4a657dcabae0931e62398fef1f9c625e`
- Core-universe contract ID: `luna-core-cae11d9e09a9a0aee921658d`
- Frozen slate ID: `luna-slate-7438c9d4a9486bf17e030f11`
- Research-only: `true`
- Broker execution enabled: `false`

## Lane accounting

The run contract recorded the following exact lane counts:

| Lane | Members | Snapshots | Eligible | Ranked |
| --- | ---: | ---: | ---: | ---: |
| Core S&P 500/Nasdaq-100 union | 519 | 519 | 0 | 0 |
| Movers | 79 | 79 | 35 | 16 |

The mover collector authenticated 142 provider rows, rejected 63 non-common securities, and
normalized 79 symbols. Enrichment selected 37 rows, verified 35, and rejected two for
insufficient range. Five verified rows used Alpaca market data and 30 used governed Yahoo
fallback. These counts prove that a zero published slate must not be interpreted as a zero-input
or zero-opportunity session.

The 16 ranked symbols were `CRM`, `CIFR`, `SMCI`, `PLUG`, `NOK`, `BTG`, `PLTR`, `BULL`, `FIG`,
`PATH`, `PURR`, `OKTA`, `WULF`, `NOW`, `HL`, and `CELU`. They were research candidates only; this
list is not a retrospective recommendation. None had the complete evidence required for safe
publication at the registered runtime.

## Remediation boundary

The governed remediation must improve breadth without manufacturing selections or weakening
safety. It must:

- construct one immutable, same-date point-in-time union of S&P 500, Nasdaq-100, and mover
  symbols with exact source, scan, member, and content-hash lineage;
- carry that exact union through bulk DataTruth and every catalog strategy, rather than falling
  back to a small static fixture universe;
- require canonical typed v2 strategy receipts and authenticated finite receipt scores for every
  modern contributor;
- de-duplicate symbols while retaining exact contributor, direction, receipt, and prior-session
  lineage;
- rank up to five safe-to-study names with lane-aware coverage and explicit shortfalls when the
  target cannot honestly be met;
- preserve strict freshness, SEC, plan geometry, receipt, portfolio, watcher, and no-broker
  gates; and
- attribute subsequent outcomes only from immutable source bars and authenticated contributor
  receipts, with direction-correct long/short semantics.

“More picks” means broader valid evidence reaches the strategies. It does not mean a mandatory
fabricated pick. A data-eligible session may publish up to five de-duplicated Tier 1 research
names; Tier 2/3 plan qualification can still honestly be zero, and a genuine no-edge session
must remain zero with named blockers.

Acceptance requires clean exact-SHA integration, hostile regression coverage, the full
unfiltered repository suite, independent Sol audit, an exact push to `main`, and proof from the
next legitimate scheduled Morning-to-Finalize chain. An out-of-window Morning rerun cannot
certify this remediation and is prohibited.
