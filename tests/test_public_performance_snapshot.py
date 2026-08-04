import hashlib
import json
import sqlite3
from pathlib import Path

from intraday_scanner.performance.snapshot import _snapshot_status, write_public_snapshot
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_public_snapshot_manifest_hashes_exact_payload(tmp_path: Path) -> None:
    db_path = tmp_path / "snapshot.sqlite"
    SQLiteScanStore(db_path).initialize()
    output = write_public_snapshot(db_path, tmp_path / "public" / "performance.json")
    payload = (tmp_path / "public" / "performance.json").read_bytes()
    manifest = json.loads(Path(output["manifest_path"]).read_text(encoding="utf-8"))
    assert manifest["payload_sha256"] == hashlib.sha256(payload).hexdigest()
    assert manifest["status"] == "no_data"
    with sqlite3.connect(db_path) as connection:
        assert (
            connection.execute("SELECT count(*) FROM public_snapshot_versions").fetchone()[0] == 1
        )


def test_snapshot_readiness_is_scoped_to_official_forward_paper() -> None:
    payload = {
        "daily": [
            {
                "market_date": "2026-08-04",
                "cohort": "official_forward_paper",
                "status": "NO_TRADE",
            },
            {
                "market_date": "2026-08-04",
                "cohort": "shadow_challenger",
                "status": "PARTIAL",
            },
        ]
    }

    assert _snapshot_status(payload, "2026-08-04") == "no_trade"
    payload["daily"][0]["status"] = "PARTIAL"
    assert _snapshot_status(payload, "2026-08-04") == "degraded"
