# CYCLE-001 active-state incident

## Scope and decision

- Audited base: `ba39a5353045b7d417936ed1aed0ee4802169759`
- Remediation branch/start SHA: `codex/luna-harvest-v1-remediation` / `7ecd08eef2d6ffd4ab4bd3d7e7a657bfe930e34d`
- Active database (not modified by this cycle): `C:\r\dawnstrike-state\shadow_real.sqlite`
- Recovery decision: **PRESERVE_SCHEMA_26**. Do not restore schema 21 and do not rewrite the registry timestamp.

SOL measured the active database before and after independent `mode=ro`/`query_only` probes as SHA-256 `4B958BB4C89311592EF2D5D0511A7390F7CC2118B8766407352570B0B7E6262A`; it was schema 26, 121 tables, `quick_check=ok`, with no WAL/SHM sidecars. The immutable pre-migration snapshot is `C:\r\dawnstrike-luna-evidence-20260809\active-baseline.sqlite`, SHA-256 `6CB1D052C1E0F7FB6C5416E21E6A6A330E8CEA615B12B43C1F206523F33C9893`, schema 21, 110 tables, `quick_check=ok`.

## Root cause and evidence

Before this cycle, `scenario_doctor` in `intraday_scanner/services/scenario_intelligence_service.py` constructed a writable `SQLiteScanStore`, called `initialize()`, then called `_register_scenario_policy()`. `SQLiteScanStore.initialize()` in `intraday_scanner/storage/sqlite_store.py` ran migrations 22–26; registration rewrote the policy row.

SOL reproduced that starting-code behavior on a disposable copy of `active-baseline.sqlite`: `py -m intraday_scanner.cli scenario-doctor --db-path <copy>` exited 2/NOT_READY but changed the disposable SHA from `6CB1D...F33C9893` to `DD5A...5A35`, migrated 21/110 tables to 26/121 tables, and changed registry `created_at` to `2026-08-09T22:59:28Z`. The disposable copy was deleted. This is not an active-database hash.

Logical comparison under read-only probes found 100 common tables unchanged; eight existing V6 tables gained nullable lineage columns with identical common-column row digests; 11 evidence tables were added; none were dropped. The only common-table data differences were `schema_version` and `scenario_model_registry`. Baseline registry/payload creation time was `2026-08-09T12:30:16Z`; active registry/payload creation time is `2026-08-09T19:55:33Z`. Other registry values match and `broker_execution_enabled=false`.

PRESERVE_SCHEMA_26 is selected because schema-26 structure is internally consistent (`quick_check=ok`), no unrelated common-table rows changed, and destructive rollback would create a larger integrity risk. The wrong timestamp remains documented incident evidence, not silently erased.

No active DB, runtime mount, scheduler task, public artifact, deployment, or publication was changed in this cycle.
