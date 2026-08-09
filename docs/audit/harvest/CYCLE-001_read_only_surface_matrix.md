# CYCLE-001 read-only surface matrix

| Surface | Prior behavior | Remediation | DB guarantee | Proof |
|---|---|---|---|---|
| `scenario-doctor` | initialized/migrated and registered policy | `SQLiteScanStore(read_only=True)`; registration removed | URI `mode=ro`, `query_only=ON` | self-contained schema-21 timestamp/SHA test |
| `scenario-report` | initialized store | read-only store | URI `mode=ro`, `query_only=ON` | store read-only test |
| `alpha-status`, `alpha-report` | writable store/status initialized | read-only store | URI `mode=ro`, `query_only=ON` | focused service suite |
| outcome gap/operator dashboard/static data | writable store or bare connection | read-only store/helper | URI `mode=ro`, `query_only=ON` | focused dashboard/outcome tests |
| alpha attribution | persisted diagnostic cases | renders diagnostic cases only | URI `mode=ro`, `query_only=ON` | focused attribution suite |
| historical attribution/report | initialized writable store when non-persist | `read_only=not persist`; historical report read-only | URI `mode=ro`, `query_only=ON` | code path review/focused suite |
| release doctors/alert replay | bare SQLite connection / `mode=ro` only | canonical helper | URI `mode=ro`, `query_only=ON` | canonical connection test |
| daily status/receipt verifier | writable store construction | read-only store construction | URI `mode=ro`, `query_only=ON` | daily focused suite |

Explicit writer operations (cycles, capture, reconciliation persistence, learning, lifecycle, publication, and stage recording) retain writable store semantics. Snapshot publication functions still deliberately write their publication manifests and are not observer surfaces.
