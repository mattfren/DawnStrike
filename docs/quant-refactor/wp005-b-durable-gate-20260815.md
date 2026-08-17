# WP005-B Durable Gate Evidence - 2026-08-15

## Identity

- Work package: `WP005-B`
- Evidence status: `READY FOR SOL ADJUDICATION`
- Worktree: `C:\r\dawnstrike-quant-refactor-20260811`
- Branch: `codex/sol-quant-refactor-20260811`
- HEAD: `bec32fe752b91f4e1357236a538a6dfea5da56bf`
- Repair cycles before final frozen gate: `1`
- Adjudication: not performed by this packet

## Implemented repair scope

- Exact `Decimal` context handling.
- Integer-microsecond duration handling.
- Independent Luna supplied-evidence review result: `CLEAN`.

## Pre-gate focused evidence

| Check | Result |
|---|---|
| Narrow tests | `2 passed`, exit `0`, `241.3s` |
| Metric tests | `35 passed`, exit `0`, `1577.3s` |
| Five-file tests | `389 passed`, exit `0`, `2226.5s` |
| Focused Ruff | Clean |
| Focused mypy | Clean across seven targets |
| Compile check | Clean |
| Diff check | Clean |

## Durable broad gate command

```powershell
py -m pytest tests/test_opportunity_validation_metrics.py tests/test_opportunity_validation.py tests/test_opportunity_metric_persistence.py tests/test_opportunity_discovery_metrics.py tests/test_opportunity_miss_persistence.py tests/test_opportunity_missed.py tests/test_opportunity_outcomes.py tests/test_opportunity_outcome_persistence.py tests/test_opportunity_persistence.py tests/test_intraday_evidence_migration.py tests/test_opportunity_contracts.py tests/test_opportunity_features.py tests/test_opportunity_pipeline.py tests/test_opportunity_universe_risk.py -q -p no:cacheprovider
```

## Environment

- Python: `3.13.14`
- pytest: `9.1.1`
- Platform: `Windows NT 10.0.26200.0`

## Timing

- Derived start UTC: `2026-08-15T13:16:03.3469057Z`
- Observed end UTC: `2026-08-15T15:47:46.2469057Z`
- Duration: `9102.9s`

The start timestamp is derived by subtracting the reported duration from the observed terminal end timestamp.

## Collection accounting

| Test file | Collected |
|---|---:|
| `tests/test_opportunity_validation_metrics.py` | 35 |
| `tests/test_opportunity_validation.py` | 24 |
| `tests/test_opportunity_metric_persistence.py` | 53 |
| `tests/test_opportunity_discovery_metrics.py` | 32 |
| `tests/test_opportunity_miss_persistence.py` | 34 |
| `tests/test_opportunity_missed.py` | 46 |
| `tests/test_opportunity_outcomes.py` | 73 |
| `tests/test_opportunity_outcome_persistence.py` | 45 |
| `tests/test_opportunity_persistence.py` | 23 |
| `tests/test_intraday_evidence_migration.py` | 7 |
| `tests/test_opportunity_contracts.py` | 9 |
| `tests/test_opportunity_features.py` | 18 |
| `tests/test_opportunity_pipeline.py` | 57 |
| `tests/test_opportunity_universe_risk.py` | 200 |
| **Total** | **656** |

## Terminal result

- Progress terminator: `[100%]`
- Passed: `656`
- Failed: `0`
- Skipped: `0`
- Xfailed: `0`
- Xpassed: `0`
- Exit code: `0`
- Duration: `9102.9s`

## Affected regression gate

```powershell
py -m pytest tests/test_sqlite_read_only_store.py tests/test_no_persist_sqlite_semantics.py tests/test_v2_data_truth_paper_ops.py -q -p no:cacheprovider
```

- Passed: `139`
- Failed: `0`
- Exit code: `0`
- Duration: `135.9s`

## Frozen source hashes

All values are SHA-256. Each pre-gate value matched its corresponding post-gate value.

| Frozen target | Pre-gate SHA-256 | Post-gate SHA-256 |
|---|---|---|
| Contracts | `3089A58805778152AB8328662BA1859E76096734D052B0609A03BC105B4897B5` | `3089A58805778152AB8328662BA1859E76096734D052B0609A03BC105B4897B5` |
| Math | `C8FDB06D24090F24FFE87AD1CEF9AF3448B49993EAC9A646D71AB04F80F88CF3` | `C8FDB06D24090F24FFE87AD1CEF9AF3448B49993EAC9A646D71AB04F80F88CF3` |
| Population | `980986F188EEE1287E00D6E8A09E3CA5E585A8A9F5377AC941CF01CDCC938A85` | `980986F188EEE1287E00D6E8A09E3CA5E585A8A9F5377AC941CF01CDCC938A85` |
| Calculations | `7663B6590843D72CD3309277C4A15826062524C69BFD3BE976EDE14CFE0B037B` | `7663B6590843D72CD3309277C4A15826062524C69BFD3BE976EDE14CFE0B037B` |
| Segments | `1C40E84BE6A185EDC1BC659ABD17FAE8EA93207CA59192F14648427CA0639BEF` | `1C40E84BE6A185EDC1BC659ABD17FAE8EA93207CA59192F14648427CA0639BEF` |
| Report | `AF421120BD19D3717EF6DA4D68281F06614E37CB4FA8820E6CA3D4E21FD7C931` | `AF421120BD19D3717EF6DA4D68281F06614E37CB4FA8820E6CA3D4E21FD7C931` |
| Facade | `44E1A8D94A25654EFA935B3C8B223B80667C8D26CB040B93B79503A2C485301D` | `44E1A8D94A25654EFA935B3C8B223B80667C8D26CB040B93B79503A2C485301D` |
| Metric test | `95578F45C28541DA1896044DDDE05439C3F9C5F4C2871CE10FD0A3B038F6A7D1` | `95578F45C28541DA1896044DDDE05439C3F9C5F4C2871CE10FD0A3B038F6A7D1` |

## Mutation and lease accounting

- Modifications during the durable broad gate: none.
- Pre/post frozen hashes: identical.
- Existing dirty-tree state: expected and shared.
- Unauthorized gate-time mutation observed: no.
- Gate leases: released after terminal completion.
- Stale worker lease: none.

## Evidence provenance and limitation

This repository artifact was reconstructed from captured terminal tool output after the durable broad gate completed. The original direct gate invocation did not redirect stdout to a separate log file.

Accordingly:

- This packet records the captured command, environment, collection accounting, terminal totals, timing, and frozen hashes.
- No raw stdout log-file path or raw stdout log-file hash is claimed.
- This packet does not establish whole-worktree cleanliness.
- This packet does not adjudicate WP005-B.
- Sol acceptance remains required before WP005-C execution.
