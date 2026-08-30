import json
import sqlite3
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION
from scripts.state_disaster_recovery import (
    RecoveryValidationError,
    create_backup,
    restore_plan,
    restore_verify,
    validate_backup,
)


def _database(path: Path, value: str = "observed") -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("PRAGMA user_version = 999")
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (26, '2026-08-30T00:00:00+00:00')")
        connection.execute("CREATE TABLE evidence (value TEXT)")
        connection.execute("INSERT INTO evidence VALUES (?)", (value,))


def _make_backup(tmp_path: Path, *, backup_id: str = "backup-one") -> tuple[Path, Path, Path]:
    state = tmp_path / "state"
    root = tmp_path / "backups"
    state.mkdir()
    source = state / "shadow_real.sqlite"
    _database(source)
    result = create_backup(source, root, state_root=state, backup_id=backup_id)
    assert result["write_performed"] is True
    assert result["created"] is True
    assert result["reused"] is False
    return source, root, Path(result["bundle_path"])


def test_online_backup_bundle_is_self_hashed_and_research_safe(tmp_path: Path) -> None:
    source, root, bundle = _make_backup(tmp_path)
    validated = validate_backup(bundle, backup_root=root)
    assert validated["quick_check"] == "ok"
    assert validated["schema_version"] == 26
    assert json.loads((bundle / "receipt.json").read_text())["automatic_restore"] is False
    with sqlite3.connect(source) as connection:
        assert connection.execute("SELECT value FROM evidence").fetchone() == ("observed",)


def test_application_schema_is_bound_not_sqlite_user_version(tmp_path: Path) -> None:
    _, root, bundle = _make_backup(tmp_path)
    manifest = json.loads((bundle / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["source_schema_version"] == 26
    assert manifest["backup_schema_version"] == 26
    assert manifest["source_sqlite_user_version"] == 999


def test_valid_sqlite_without_dawnstrike_schema_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    source = state / "shadow_real.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute("CREATE TABLE unrelated (value TEXT)")
    with pytest.raises(RecoveryValidationError, match="schema_version table is missing"):
        create_backup(source, tmp_path / "backups", state_root=state)


def test_empty_dawnstrike_schema_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    source = state / "shadow_real.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
    with pytest.raises(RecoveryValidationError, match="schema_version table is empty"):
        create_backup(source, tmp_path / "backups", state_root=state)


@pytest.mark.parametrize("version", [0, CURRENT_SCHEMA_VERSION + 1, -1])
def test_incompatible_dawnstrike_schema_is_rejected(tmp_path: Path, version: int) -> None:
    state = tmp_path / "state"
    state.mkdir()
    source = state / "shadow_real.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (?, 'fixture')", (version,))
    with pytest.raises(RecoveryValidationError, match="unsupported|empty"):
        create_backup(source, tmp_path / "backups", state_root=state)


def test_tamper_and_partial_bundles_fail_closed(tmp_path: Path) -> None:
    _, root, bundle = _make_backup(tmp_path)
    (bundle / "receipt.json").write_text("{}", encoding="utf-8")
    with pytest.raises(RecoveryValidationError, match="self-hash"):
        validate_backup(bundle, backup_root=root)
    partial = root / "partial"
    partial.mkdir()
    (partial / "shadow_real.sqlite").write_bytes(b"partial")
    with pytest.raises(RecoveryValidationError, match="incomplete"):
        validate_backup(partial, backup_root=root)
    (bundle / "receipt.json.tmp").write_text("partial", encoding="utf-8")
    with pytest.raises(RecoveryValidationError, match="unexpected"):
        validate_backup(bundle, backup_root=root)


def test_database_corruption_fails_closed(tmp_path: Path) -> None:
    _, root, bundle = _make_backup(tmp_path)
    db = bundle / "shadow_real.sqlite"
    db.write_bytes(db.read_bytes() + b"corruption")
    with pytest.raises(RecoveryValidationError, match="hash or size"):
        validate_backup(bundle, backup_root=root)


def test_roots_must_be_separate_and_outside_state(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    source = state / "shadow_real.sqlite"
    _database(source)
    with pytest.raises(RecoveryValidationError, match="separate"):
        create_backup(source, state / "backups", state_root=state)
    with pytest.raises(RecoveryValidationError, match="separate"):
        create_backup(source, state, state_root=state)
    secret = state / "secrets"
    secret.mkdir()
    secret_db = secret / "secret.sqlite"
    _database(secret_db)
    with pytest.raises(RecoveryValidationError, match="secret"):
        create_backup(secret_db, tmp_path / "secret-backups", state_root=state)


def test_retry_with_backup_id_is_idempotent(tmp_path: Path) -> None:
    source, root, bundle = _make_backup(tmp_path, backup_id="retry")
    first = (bundle / "manifest.json").read_bytes()
    second = create_backup(source, root, state_root=source.parent, backup_id="retry")
    assert Path(second["bundle_path"]) == bundle
    assert second["write_performed"] is False
    assert second["created"] is False
    assert second["reused"] is True
    assert (bundle / "manifest.json").read_bytes() == first
    assert [p for p in root.iterdir() if p.is_dir()] == [bundle]


def test_backup_id_cannot_be_reused_for_different_source_or_release(tmp_path: Path) -> None:
    source, root, bundle = _make_backup(tmp_path, backup_id="identity")
    other_state = tmp_path / "other-state"
    other_state.mkdir()
    other_source = other_state / "shadow_real.sqlite"
    _database(other_source, "changed")
    with pytest.raises(RecoveryValidationError, match="different source"):
        create_backup(other_source, root, state_root=other_state, backup_id="identity")
    create_backup(
        source,
        root,
        state_root=source.parent,
        backup_id="release",
        source_sha="a" * 40,
    )
    with pytest.raises(RecoveryValidationError, match="different source"):
        create_backup(
            source,
            root,
            state_root=source.parent,
            backup_id="release",
            source_sha="b" * 40,
        )
    assert bundle.exists()


def test_invalid_source_release_sha_fails_closed(tmp_path: Path) -> None:
    source, root, _ = _make_backup(tmp_path)
    with pytest.raises(RecoveryValidationError, match="exactly 40"):
        create_backup(source, root, state_root=source.parent, source_sha="not-a-git-sha")


def test_retention_never_deletes_last_good_and_ignores_invalid(tmp_path: Path) -> None:
    state = tmp_path / "state"
    root = tmp_path / "backups"
    state.mkdir()
    source = state / "shadow_real.sqlite"
    _database(source)
    create_backup(
        source,
        root,
        state_root=state,
        backup_id="first",
        created_at="2026-08-30T00:00:00.000000Z",
    )
    first = root / "first"
    create_backup(
        source,
        root,
        state_root=source.parent,
        backup_id="second",
        created_at="2026-08-30T01:00:00.000000Z",
        retention=1,
    )
    assert first.exists() is False
    second = root / "second"
    assert validate_backup(second, backup_root=root)["status"] == "PASS"
    (root / ".incomplete-manual").mkdir()
    create_backup(
        source,
        root,
        state_root=source.parent,
        backup_id="third",
        created_at="2026-08-30T02:00:00.000000Z",
        retention=1,
    )
    assert validate_backup(root / "third", backup_root=root)["status"] == "PASS"
    assert (root / ".incomplete-manual").exists()


def test_restore_verify_and_plan_never_overwrite(tmp_path: Path) -> None:
    _, root, bundle = _make_backup(tmp_path)
    target = tmp_path / "state" / "shadow_real.sqlite"
    before = target.read_bytes()
    verified = restore_verify(bundle, target, backup_root=root, state_root=target.parent)
    planned = restore_plan(bundle, target, backup_root=root, state_root=target.parent)
    assert verified["status"] == "VERIFY"
    assert planned["status"] == "PLAN"
    assert planned["write_performed"] is False
    assert target.read_bytes() == before


def test_restore_target_containment_is_enforced(tmp_path: Path) -> None:
    _, root, bundle = _make_backup(tmp_path)
    with pytest.raises(RecoveryValidationError, match="contained"):
        restore_plan(
            bundle,
            tmp_path / "outside.sqlite",
            backup_root=root,
            state_root=tmp_path / "state",
        )
