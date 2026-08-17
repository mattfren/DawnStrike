from __future__ import annotations

import hashlib
import importlib
import json
import sqlite3
import sys
import tempfile
from contextlib import closing
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
sys.path.insert(0, str(ROOT))

MIGRATIONS = importlib.import_module("intraday_scanner.storage.migrations")
VALIDATION_SCHEMA = importlib.import_module(
    "intraday_scanner.storage.opportunity_validation_schema"
)
get_schema_version = MIGRATIONS.get_schema_version
run_migrations = MIGRATIONS.run_migrations
VALIDATION_INDEXES = VALIDATION_SCHEMA.VALIDATION_INDEXES
VALIDATION_TABLES = VALIDATION_SCHEMA.VALIDATION_TABLES
VALIDATION_TRIGGERS = VALIDATION_SCHEMA.VALIDATION_TRIGGERS
validation_schema_inventory = VALIDATION_SCHEMA.validation_schema_inventory


def migrate(path: Path) -> dict[str, object]:
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        first = run_migrations(connection)
        second = run_migrations(connection)
        inventory = validation_schema_inventory(connection)
        structures = {
            table: {
                "columns": [
                    [str(row[1]), str(row[2]), int(row[3]), int(row[5])]
                    for row in connection.execute(
                        f"PRAGMA table_info({table})"
                    ).fetchall()
                ],
                "indexes": [
                    str(row[1])
                    for row in connection.execute(
                        f"PRAGMA index_list({table})"
                    ).fetchall()
                ],
                "foreign_keys": [
                    list(row)
                    for row in connection.execute(
                        f"PRAGMA foreign_key_list({table})"
                    ).fetchall()
                ],
            }
            for table in VALIDATION_TABLES
        }
        return {
            "first_migration_version": first,
            "second_migration_version": second,
            "schema_version": get_schema_version(connection),
            "quick_check": connection.execute("PRAGMA quick_check").fetchone()[0],
            "inventory": inventory,
            "inventory_sha256": hashlib.sha256(
                json.dumps(inventory, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            "structures": structures,
        }


with tempfile.TemporaryDirectory(prefix="wp006-schema-") as directory:
    first = migrate(Path(directory) / "first.sqlite")
    second = migrate(Path(directory) / "second.sqlite")

result = {
    "database_schema_version": 30,
    "expected_tables": list(VALIDATION_TABLES),
    "expected_indexes": sorted(VALIDATION_INDEXES),
    "expected_triggers": sorted(VALIDATION_TRIGGERS),
    "first": first,
    "second": second,
    "inventories_identical": first["inventory"] == second["inventory"],
    "structures_identical": first["structures"] == second["structures"],
}
print(json.dumps(result, indent=2, sort_keys=True))
if not result["inventories_identical"] or not result["structures_identical"]:
    raise SystemExit(1)
