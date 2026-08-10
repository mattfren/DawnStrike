# CYCLE-001/CYCLE-002 read-only CLI surface matrix

Observer stores fail closed before connecting when the database is absent, an active `-wal`, `-shm`, or `-journal` sidecar exists, or the canonical SQLite header declares WAL mode; otherwise they use URI `mode=ro` plus `PRAGMA query_only=ON`. Artifact output in a supplied directory is permitted and does not mutate SQLite.

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
| `free-shadow-scan` | self-contained optional-persist scan | no SQLite; writes requested scan artifacts | `--persist` writes scan and health evidence |
| `live-scan` | self-contained optional-persist scan | no SQLite when explicit symbols/universe are supplied; writes requested scan artifacts | `--persist` writes scan, provider health, and counts |
| `monitor-setups`, `monitor-loop`, `monitor-open` | observer by default | existing SQLite opened read-only; absent database or active sidecar fails closed before artifacts | `--persist` writes monitor, alert, and provider-health evidence |
| `price-observe --no-persist` | conditional observer | explicit tickers use no SQLite; implicit discovery opens existing SQLite read-only | omitting `--no-persist` writes price observations |
| `alpha-capture-outcomes` | optional-persist observer | existing SQLite opened read-only; missing/contradictory canonical selection evidence fails before artifacts | `--persist` writes sourced outcomes and audit events |
| `alpha-paper-reconcile` | optional-persist observer | existing SQLite opened read-only; absent or contradictory canonical selection evidence fails before artifacts; unresolved outcome/path truth remains explicit in the packet | `--persist` writes reconciliation trades and learning labels |
| `web-collect-sec-risk` | conditional optional-persist collector | explicit tickers use no SQLite; default ticker discovery opens existing SQLite read-only | `--persist` writes SEC risk evidence |
| `normalize-screener-file` | self-contained optional-persist normalizer | no SQLite even when `--db-path` is supplied; writes only requested normalized artifacts | `--persist` writes the manual snapshot upload |
| `auto-shadow-from-screener` | filesystem workflow with read-only duplicate discovery by default | existing SQLite opened read-only; absent database or active sidecar fails before normalization, archive, log, or scan artifacts | `--persist` writes scan and automation evidence |

Dashboard `_load_monitor_rows` and `_preview_web_alerts` also use the same existing-only read store. The monitor loader may fall back to an existing CSV when SQLite is absent; alert preview fails closed. The production scheduler's market-open monitor command includes explicit `--persist`, while interactive monitor commands remain observers by default.

The AST registry test exhaustively guards parser names containing `status`, `audit`, `report`, `doctor`, or `verify`; its supplemental registries guard the non-keyword observer paths and the eleven conditional no-persist commands above. Learning, lifecycle, publication, stage, experiment registration, restore, and holdout-evaluation commands retain writable stores because they are durable mutators.

Schema-26 test proof uses a disposable writer-created database and a fully populated `scenario_model_registry` row. It hashes the whole database and every user table (with selected `schema_version` and `scenario_model_registry` hashes called out), verifies table count, and invokes the actual observer matrix including daily status, receipt verification, dashboard loader, and the four originally missed `--persist` observers.
