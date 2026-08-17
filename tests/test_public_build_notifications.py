import json
import sqlite3
from pathlib import Path

from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from scripts import build_public
from scripts.verify_public_artifact import verify


def test_public_build_records_auditable_finalize_notification(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED", raising=False)
    monkeypatch.setattr(
        build_public,
        "_source_metadata",
        lambda root: {"source_sha": "clean-fixture-sha", "source_clean": True},
    )
    db_path = tmp_path / "notification.sqlite"
    SQLiteScanStore(db_path).initialize()
    output = tmp_path / "public"
    monkeypatch.setattr(
        build_public,
        "_resolve_repository_database",
        lambda root, value: db_path,
    )

    status = build_public.main(
        [
            "--db-path",
            str(db_path),
            "--out-dir",
            str(output),
            "--date",
            "2026-07-29",
            "--retry-limit",
            "0",
            "--deployment-url",
            "https://preview.example.test",
        ]
    )

    assert status == 2
    build_manifest = json.loads((output / "build-manifest.json").read_text(encoding="utf-8"))
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT channel, payload_json
            FROM notifications_sent
            WHERE event_key LIKE 'dawnstrike:daily-finalize:%'
            """
        ).fetchone()
    assert row is not None
    assert row[0] == "console"
    payload = json.loads(row[1])
    assert payload["build_id"] == build_manifest["build_id"]
    assert payload["market_date"] == "2026-07-29"
    assert payload["deployment_url"] == "https://preview.example.test"
    assert "coverage" in payload
    assert "next_action" in payload
    projection = json.loads(
        (output / "data" / "opportunity-projection.json").read_text(encoding="utf-8")
    )
    assert projection["state"] == "DISABLED"
    assert projection["rows"] == []
    assert projection["order_execution_enabled"] is False
    verification = verify(output, allow_degraded=True)
    assert not any("opportunity" in error for error in verification["errors"])

    (output / "data" / "opportunity-projection.json").write_text(
        "{}",
        encoding="utf-8",
    )
    tampered = verify(output, allow_degraded=True)
    assert "opportunity_hash_mismatch" in tampered["errors"]
