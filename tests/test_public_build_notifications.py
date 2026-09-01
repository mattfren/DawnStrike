import hashlib
import shutil
import sqlite3
from pathlib import Path

from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from scripts import build_public


def test_success_evidence_is_emitted_only_after_irreversible_commit() -> None:
    source = Path(build_public.__file__).read_text(encoding="utf-8")
    security = source.index("violations = scan_public_artifact(output_root)")
    promotion = source.index("_promote_public_artifact(", security)
    commit = source.index("operation.commit()", promotion)
    notification = source.index("notification = _record_build_notification(", commit)
    result_write = source.index("_write_private_finalize_result(", notification)
    assert security < promotion < commit < notification < result_write
    tombstone = source.index('"status": "IN_PROGRESS"')
    assert tombstone < source.index("source = _source_metadata(root)")


def test_public_build_records_auditable_finalize_notification(
    tmp_path: Path, monkeypatch, request
) -> None:
    monkeypatch.delenv("DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED", raising=False)
    monkeypatch.setattr(
        build_public,
        "_source_metadata",
        lambda root: {
            "source_sha": build_public.run_git(root, "rev-parse", "HEAD").stdout.strip(),
            "source_clean": True,
        },
    )
    db_path = tmp_path / "notification.sqlite"
    SQLiteScanStore(db_path).initialize()
    token = hashlib.sha256(str(tmp_path).encode("utf-8")).hexdigest()[:16]
    output = Path("build") / f"test-public-{token}"
    operation_prefix = f".{output.name}-build-operation"

    def cleanup() -> None:
        shutil.rmtree(output, ignore_errors=True)
        for suffix in (".json", ".json.tmp", ".lock"):
            (output.parent / f"{operation_prefix}{suffix}").unlink(missing_ok=True)

    cleanup()
    request.addfinalizer(cleanup)
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
            output.as_posix(),
            "--date",
            "2026-07-29",
            "--retry-limit",
            "0",
            "--deployment-url",
            "https://preview.example.test",
        ]
    )

    assert status == 2
    assert not output.exists()
    with sqlite3.connect(db_path) as connection:
        row = connection.execute(
            """
            SELECT channel, payload_json
            FROM notifications_sent
            WHERE event_key LIKE 'dawnstrike:daily-finalize:%'
            """
        ).fetchone()
    assert row is None
