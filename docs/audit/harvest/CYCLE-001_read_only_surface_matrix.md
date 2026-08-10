# CYCLE-001/CYCLE-002 read-only CLI surface matrix

All observer stores use URI `mode=ro` plus `PRAGMA query_only=ON`; absent databases fail closed. Artifact output in a supplied directory is permitted and does not mutate SQLite.

| CLI surface | Classification | SQLite behavior by default | Explicit persistence |
|---|---|---|---|
| `audit-manual-outcomes` | observer | read-only | `--persist` writes audit rows |
| `free-shadow-report` | observer | read-only | `--persist` writes shadow report |
| `web-source-doctor` | filesystem/input-only | no SQLite | n/a |
| `daily-orchestrator-status` | observer | read-only | none |
| `alpha-status` | observer | read-only | none |
| `alpha-doctor` | filesystem/input-only | no SQLite | n/a |
| `alpha-report` | observer | read-only | none |
| `scenario-doctor` | observer | read-only | none |
| `scenario-report` | observer | read-only | none |
| `historical-report` | observer | read-only | none |
| `calendar-report` | observer | read-only | none |
| `paper-audit` | no-DB-by-default | no SQLite | `--persist` writes audit rows |
| `audit-latest` | observer | read-only | `--persist` writes audit rows |
| `backfill-audit` | no-DB-by-default | no SQLite | `--persist` writes audit rows |
| `performance-report` | observer | read-only | `--persist` writes performance report |
| `probability-doctor` | observer | read-only | none |
| `scheduler-doctor` | filesystem/input-only | no SQLite | n/a |
| `dashboard-doctor` | observer | read-only | none |
| `import-manual-outcomes` | optional-persist reader | read-only validation | `--persist` imports rows |
| `evaluate-intelligence-outcomes` | optional-persist reader | read-only evaluation | `--persist` writes evaluation |
| `alpha-v6-attribution` | observer | read-only; attribution uses `persist=False` | none |
| `alpha-v6-research-packet` | observer | read-only; writes packet artifacts only | none |
| `alpha-v6-preview-universe` | observer | read-only; preview only | none |
| `outcome-gap` | observer | read-only | none |
| `alpha-attribution` | observer | read-only | none |
| `attribute-returns` | optional-persist observer | read-only; notification is rejected before side effects | `--persist` writes attribution rows and permits notification |
| `alpha-alert-replay` | observer | read-only; writes replay artifact only | none |

The AST registry test exhaustively guards parser names containing `status`, `audit`, `report`, `doctor`, or `verify`; its explicit supplemental handler registry guards the nine non-keyword observer/optional-persist paths above. Capture, reconciliation, learning, lifecycle, publication, stage, experiment registration, restore, and holdout-evaluation commands intentionally retain writable stores.

Schema-26 test proof uses a disposable writer-created database and a fully populated `scenario_model_registry` row. It hashes the whole database and every user table (with selected `schema_version` and `scenario_model_registry` hashes called out), verifies table count, and invokes the actual observer matrix including daily status, receipt verification, dashboard loader, and the four originally missed `--persist` observers.
