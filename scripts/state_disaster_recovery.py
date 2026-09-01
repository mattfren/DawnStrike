"""Fail-closed, research-only durable SQLite backup and restore planning.

The backup is made with SQLite's online backup API while the source remains
open.  A completed backup is an atomic directory containing the database,
manifest, and receipt.  ``restore_verify`` and ``restore_plan`` never
overwrite a state database.  ``restore_exact`` is the small, governed
recovery primitive used only after state preparation has sealed an exact
pre-mutation backup and source identity.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote

_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION  # noqa: E402

SCHEMA_VERSION = 1
DB_NAME = "shadow_real.sqlite"
MANIFEST_NAME = "manifest.json"
RECEIPT_NAME = "receipt.json"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_SHA = re.compile(r"^[0-9a-fA-F]{40}$")
_REPARSE_POINT = 0x400
_MANIFEST_KEYS = frozenset(
    {
        "artifact",
        "schema_version",
        "backup_id",
        "created_at",
        "source_db_name",
        "source_live_main_file_bytes",
        "source_live_main_file_sha256",
        "source_live_main_file_hash_semantics",
        "source_release_sha",
        "source_logical_snapshot_sha256",
        "source_quick_check",
        "source_schema_version",
        "source_sqlite_user_version",
        "backup_db_name",
        "backup_db_bytes",
        "backup_db_sha256",
        "backup_logical_snapshot_sha256",
        "backup_quick_check",
        "backup_schema_version",
        "backup_sqlite_user_version",
        "research_only",
        "broker_execution_enabled",
        "manifest_sha256",
    }
)
_RECEIPT_KEYS = frozenset(
    {
        "artifact",
        "schema_version",
        "backup_id",
        "created_at",
        "manifest_sha256",
        "source_release_sha",
        "source_live_main_file_sha256",
        "source_logical_snapshot_sha256",
        "source_schema_version",
        "backup_schema_version",
        "backup_db_sha256",
        "backup_logical_snapshot_sha256",
        "status",
        "write_mode",
        "automatic_restore",
        "research_only",
        "broker_execution_enabled",
        "receipt_sha256",
    }
)


class RecoveryValidationError(ValueError):
    """An input or recovery artifact is not safe to use."""


def _canonical_json(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":")) + "\n").encode(
        "utf-8"
    )


def _self_hash(payload: dict[str, Any], field: str) -> str:
    unsigned = {key: value for key, value in payload.items() if key != field}
    return hashlib.sha256(_canonical_json(unsigned)).hexdigest()


def _sha256(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            size += len(chunk)
            digest.update(chunk)
    return size, digest.hexdigest()


def _file_hash_or_empty(path: Path) -> str:
    """Hash an optional SQLite sidecar without following reparses."""

    _assert_no_reparse_components(path)
    if not path.is_file():
        return hashlib.sha256(b"").hexdigest()
    return _sha256(path)[1]


def _snapshot_hashes(path: Path) -> dict[str, str]:
    return {
        "db_sha256": _sha256(path)[1],
        "wal_sha256": _file_hash_or_empty(path.with_name(path.name + "-wal")),
        "shm_sha256": _file_hash_or_empty(path.with_name(path.name + "-shm")),
    }


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _is_reparse_point(path: Path) -> bool:
    try:
        attributes = int(getattr(path.lstat(), "st_file_attributes", 0))
    except OSError:
        return False
    return path.is_symlink() or bool(attributes & _REPARSE_POINT)


def _assert_no_reparse_components(path: str | Path) -> Path:
    absolute = Path(os.path.abspath(Path(path).expanduser()))
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        if current.exists() and _is_reparse_point(current):
            raise RecoveryValidationError(
                f"reparse-point path component is forbidden: {current}"
            )
    return absolute


def _resolve(path: str | Path) -> Path:
    supplied = _assert_no_reparse_components(path)
    resolved = supplied.resolve()
    _assert_no_reparse_components(resolved)
    return resolved


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _validate_roots(
    source_db: Path, backup_root: Path, state_root: str | Path | None
) -> None:
    # The source's parent is the conservative default state root.  Callers
    # running against a mounted state root should pass it explicitly.
    resolved_state = _resolve(state_root) if state_root is not None else source_db.parent
    if _is_within(backup_root, resolved_state) or _is_within(resolved_state, backup_root):
        raise RecoveryValidationError(
            "backup root must be separate from and outside the state root"
        )
    if _is_within(source_db, backup_root):
        raise RecoveryValidationError("source database must be outside the backup root")
    if _is_within(source_db, resolved_state / "secrets"):
        raise RecoveryValidationError("secret files cannot be used as a backup source")
    if backup_root == source_db:
        raise RecoveryValidationError("backup root cannot be the source database")


def _db_metadata(path: Path, *, read_only: bool, immutable: bool = False) -> dict[str, Any]:
    if read_only:
        query = "mode=ro&immutable=1" if immutable else "mode=ro"
        uri = f"file:{quote(path.as_posix(), safe='/:')}?{query}"
        connection = sqlite3.connect(uri, uri=True, timeout=30)
    else:
        connection = sqlite3.connect(path, timeout=30)
    try:
        connection.execute("PRAGMA query_only = ON")
        quick_check = str(connection.execute("PRAGMA quick_check").fetchone()[0])
        if quick_check != "ok":
            raise RecoveryValidationError(f"SQLite quick_check failed: {quick_check}")
        table = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'schema_version'"
        ).fetchone()
        if table is None:
            raise RecoveryValidationError("Dawnstrike schema_version table is missing")
        row = connection.execute("SELECT MAX(version) FROM schema_version").fetchone()
        if row is None or row[0] is None:
            raise RecoveryValidationError("Dawnstrike schema_version table is empty")
        schema = int(row[0])
        if schema < 1 or schema > CURRENT_SCHEMA_VERSION:
            raise RecoveryValidationError(
                f"unsupported Dawnstrike application schema version: {schema}"
            )
        return {
            "quick_check": quick_check,
            "schema_version": schema,
            "sqlite_user_version": int(connection.execute("PRAGMA user_version").fetchone()[0]),
        }
    except sqlite3.Error as exc:
        raise RecoveryValidationError(f"SQLite validation failed: {exc}") from exc
    finally:
        connection.close()


def _logical_snapshot_sha256(path: Path, *, immutable: bool) -> str:
    """Hash SQLite's logical online snapshot without writing beside ``path``.

    ``immutable=1`` is intentional for sealed backup validation: even a
    metadata read must not make SQLite create a WAL or SHM sidecar in the
    three-file bundle.  A canonical SQL dump is stable across physical page
    layouts and reflects committed WAL pages observed by the connection.
    """

    query = "mode=ro&immutable=1" if immutable else "mode=ro"
    uri = f"file:{quote(path.as_posix(), safe='/:')}?{query}"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=30)) as connection:
            connection.execute("PRAGMA query_only = ON")
            image = "\n".join(connection.iterdump()).encode("utf-8")
    except (AttributeError, sqlite3.Error) as exc:
        raise RecoveryValidationError("SQLite logical snapshot failed") from exc
    return hashlib.sha256(image).hexdigest()


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    _assert_no_reparse_components(path)
    _assert_no_reparse_components(path.parent)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    try:
        _assert_no_reparse_components(temporary)
        with temporary.open("xb") as handle:
            handle.write(_canonical_json(payload))
            handle.flush()
            os.fsync(handle.fileno())
        _assert_no_reparse_components(path)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _fsync_directory(path: Path) -> None:
    # Directory fsync is unavailable on some Windows Python builds.  The
    # rename itself remains atomic; this best-effort flush is supplementary.
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


def _bundle_id(created_at: str, source_sha: str, requested: str | None) -> str:
    if requested is not None:
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", requested):
            raise RecoveryValidationError("backup id contains unsafe characters")
        return requested
    return f"{created_at.replace(':', '').replace('-', '').replace('.', '')}-{source_sha[:16]}"


def _complete_bundle(path: Path) -> bool:
    return path.is_dir() and (path / DB_NAME).is_file() and (path / MANIFEST_NAME).is_file() and (
        path / RECEIPT_NAME
    ).is_file()


def create_backup(
    source_db: str | Path,
    backup_root: str | Path,
    *,
    source_sha: str,
    state_root: str | Path | None = None,
    retention: int = 7,
    backup_id: str | None = None,
    created_at: str | None = None,
    expected_db_sha256: str | None = None,
    expected_wal_sha256: str | None = None,
    expected_shm_sha256: str | None = None,
    expected_logical_snapshot_sha256: str | None = None,
) -> dict[str, Any]:
    """Create or idempotently return one validated, atomic backup bundle.

    When expected snapshot hashes are supplied they are the caller's locked
    proof (activation uses the exact state-preparation receipt).  The source
    is checked immediately before and after SQLite's online copy, so a commit
    that lands after the preflight proof cannot be silently included.

    CLI integration must pass ``--expected-db-sha256``,
    ``--expected-wal-sha256``, ``--expected-shm-sha256``, and
    ``--expected-logical-snapshot-sha256`` from that locked proof.
    """

    if retention < 1:
        raise RecoveryValidationError("retention must be at least one")
    source = _resolve(source_db)
    root = _resolve(backup_root)
    if not source.is_file():
        raise FileNotFoundError(f"source database not found: {source}")
    if source.name != DB_NAME:
        raise RecoveryValidationError(
            f"source database basename must be exactly {DB_NAME}"
        )
    _validate_roots(source, root, state_root)
    source_size, source_hash = _sha256(source)
    if source_sha is None or not _GIT_SHA.fullmatch(source_sha):
        raise RecoveryValidationError(
            "source release SHA is required and must be exactly 40 hex characters"
        )
    normalized_source_sha = source_sha.lower()
    expected_hashes = {
        "db_sha256": expected_db_sha256,
        "wal_sha256": expected_wal_sha256,
        "shm_sha256": expected_shm_sha256,
        "logical_snapshot_sha256": expected_logical_snapshot_sha256,
    }
    for field, value in expected_hashes.items():
        if value is not None and not _SHA256.fullmatch(value):
            raise RecoveryValidationError(f"expected {field} is invalid")
    expected_file_hashes = tuple(
        expected_hashes[field] for field in ("db_sha256", "wal_sha256", "shm_sha256")
    )
    if any(value is not None for value in expected_file_hashes) and not all(
        value is not None for value in expected_file_hashes
    ):
        raise RecoveryValidationError(
            "expected source snapshot requires all db, WAL, and SHM hashes"
        )
    if any(value is not None for value in expected_hashes.values()) and (
        expected_logical_snapshot_sha256 is None
    ):
        raise RecoveryValidationError(
            "expected source snapshot requires a logical snapshot hash"
        )
    metadata = _db_metadata(source, read_only=True)
    timestamp = created_at or _utc_now()
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}T.*Z", timestamp):
        raise RecoveryValidationError("created_at must be an RFC3339 UTC timestamp")
    bundle_name = _bundle_id(timestamp, source_hash, backup_id)
    _assert_no_reparse_components(root.parent)
    _assert_no_reparse_components(root)
    root.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(root)
    final_dir = root / bundle_name
    temporary_dir = Path(tempfile.mkdtemp(prefix=f".incomplete-{bundle_name}-", dir=root))
    _assert_no_reparse_components(temporary_dir)
    existing_result: dict[str, Any] | None = None
    try:
        target = temporary_dir / DB_NAME
        source_uri = f"file:{quote(source.as_posix(), safe='/:')}?mode=ro"
        source_logical_snapshot = _logical_snapshot_sha256(source, immutable=False)
        source_snapshot_before = _snapshot_hashes(source)
        source_snapshot_before["logical_snapshot_sha256"] = source_logical_snapshot
        if any(
            source_snapshot_before[field] != expected_hashes[field]
            for field in expected_hashes
            if expected_hashes[field] is not None
        ):
            raise RecoveryValidationError(
                "source snapshot does not match the expected locked proof"
            )
        with closing(sqlite3.connect(source_uri, uri=True, timeout=30)) as source_connection:
            source_connection.execute("PRAGMA query_only = ON")
            with closing(sqlite3.connect(target, timeout=30)) as target_connection:
                source_connection.backup(target_connection, pages=128, sleep=0.05)
                target_connection.commit()
        source_logical_snapshot_after = _logical_snapshot_sha256(source, immutable=False)
        source_snapshot_after = _snapshot_hashes(source)
        source_snapshot_after["logical_snapshot_sha256"] = source_logical_snapshot_after
        if source_snapshot_after != source_snapshot_before:
            raise RecoveryValidationError(
                "source SQLite logical snapshot or file snapshot changed during online backup"
            )
        backup_size, backup_hash = _sha256(target)
        # An immutable read avoids SQLite creating -wal/-shm sidecars next to
        # the sealed backup while still proving the online-backup result.
        backup_metadata = _db_metadata(target, read_only=True, immutable=True)
        if backup_metadata != metadata:
            raise RecoveryValidationError("source and backup SQLite metadata differ")
        backup_logical_snapshot = _logical_snapshot_sha256(target, immutable=True)
        if backup_logical_snapshot != source_logical_snapshot:
            raise RecoveryValidationError(
                "online backup logical snapshot does not match its source"
            )
        if expected_logical_snapshot_sha256 is not None and (
            backup_logical_snapshot != expected_logical_snapshot_sha256
        ):
            raise RecoveryValidationError(
                "online backup does not match the expected locked logical snapshot"
            )
        if {item.name for item in temporary_dir.iterdir()} != {DB_NAME}:
            raise RecoveryValidationError(
                "online backup database staging directory contains unexpected files"
            )
        manifest: dict[str, Any] = {
            "artifact": "dawnstrike-durable-state-backup",
            "schema_version": SCHEMA_VERSION,
            "backup_id": bundle_name,
            "created_at": timestamp,
            "source_db_name": source.name,
            # These are explicitly observational: a live SQLite main file
            # may lag committed WAL pages. The online-backup hash below is the
            # authoritative snapshot lineage.
            "source_live_main_file_bytes": source_size,
            "source_live_main_file_sha256": source_hash,
            "source_live_main_file_hash_semantics": (
                "observational_main_database_only_wal_may_be_pending"
            ),
            "source_release_sha": normalized_source_sha,
            "source_logical_snapshot_sha256": source_logical_snapshot,
            "source_quick_check": metadata["quick_check"],
            "source_schema_version": metadata["schema_version"],
            "source_sqlite_user_version": metadata["sqlite_user_version"],
            "backup_db_name": DB_NAME,
            "backup_db_bytes": backup_size,
            "backup_db_sha256": backup_hash,
            "backup_logical_snapshot_sha256": backup_logical_snapshot,
            "backup_quick_check": backup_metadata["quick_check"],
            "backup_schema_version": backup_metadata["schema_version"],
            "backup_sqlite_user_version": backup_metadata["sqlite_user_version"],
            "research_only": True,
            "broker_execution_enabled": False,
        }
        manifest["manifest_sha256"] = _self_hash(manifest, "manifest_sha256")
        _atomic_json(temporary_dir / MANIFEST_NAME, manifest)
        receipt: dict[str, Any] = {
            "artifact": "dawnstrike-durable-state-backup-receipt",
            "schema_version": SCHEMA_VERSION,
            "backup_id": bundle_name,
            "created_at": timestamp,
            "manifest_sha256": manifest["manifest_sha256"],
            "source_release_sha": normalized_source_sha,
            "source_live_main_file_sha256": source_hash,
            "source_logical_snapshot_sha256": source_logical_snapshot,
            "source_schema_version": metadata["schema_version"],
            "backup_schema_version": backup_metadata["schema_version"],
            "backup_db_sha256": backup_hash,
            "backup_logical_snapshot_sha256": backup_logical_snapshot,
            "status": "PASS",
            "write_mode": "sqlite_online_backup_atomic_bundle",
            "automatic_restore": False,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        receipt["receipt_sha256"] = _self_hash(receipt, "receipt_sha256")
        _atomic_json(temporary_dir / RECEIPT_NAME, receipt)
        _fsync_directory(temporary_dir)
        if final_dir.exists():
            if not _complete_bundle(final_dir):
                raise RecoveryValidationError(
                    f"backup id already has an incomplete bundle: {final_dir}"
                )
            existing = validate_backup(final_dir, backup_root=root)
            if (
                existing["backup_db_sha256"] != backup_hash
                or existing.get("backup_logical_snapshot_sha256")
                != backup_logical_snapshot
                or existing.get("source_logical_snapshot_sha256")
                != source_logical_snapshot
                or existing.get("source_release_sha") != normalized_source_sha
                or existing["schema_version"] != backup_metadata["schema_version"]
            ):
                raise RecoveryValidationError(
                    "backup id exists for a different source snapshot or release"
                )
            existing_result = existing
        else:
            os.replace(temporary_dir, final_dir)
        _fsync_directory(root)
    finally:
        if temporary_dir.exists():
            shutil.rmtree(temporary_dir)

    if existing_result is not None:
        _apply_retention(root, retention)
        return {
            **existing_result,
            "write_performed": False,
            "created": False,
            "reused": True,
        }
    result = validate_backup(final_dir, backup_root=root)
    _apply_retention(root, retention)
    return {
        **result,
        "write_performed": True,
        "created": True,
        "reused": False,
    }


def _strict_object_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    """Reject duplicate JSON names after unescaping and case folding."""

    result: dict[str, object] = {}
    seen: set[str] = set()
    for key, value in pairs:
        normalized = key.casefold()
        if normalized in seen:
            raise RecoveryValidationError("recovery JSON contains duplicate keys")
        seen.add(normalized)
        result[key] = value
    return result


def _load_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(
            path.read_text(encoding="utf-8"), object_pairs_hook=_strict_object_pairs
        )
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, RecoveryValidationError) as exc:
        raise RecoveryValidationError(f"invalid recovery JSON: {path}") from exc
    if not isinstance(value, dict):
        raise RecoveryValidationError(f"recovery JSON must be an object: {path}")
    return value


def validate_backup(
    bundle: str | Path, *, backup_root: str | Path | None = None
) -> dict[str, Any]:
    """Validate hashes, SQLite integrity, schema, and containment."""

    path = _resolve(bundle)
    root = _resolve(backup_root) if backup_root is not None else path.parent
    if not _is_within(path, root) or path == root:
        raise RecoveryValidationError("backup bundle is outside backup root")
    if not _complete_bundle(path):
        raise RecoveryValidationError("backup bundle is incomplete")
    expected_files = {DB_NAME, MANIFEST_NAME, RECEIPT_NAME}
    if {item.name for item in path.iterdir()} != expected_files:
        raise RecoveryValidationError("backup bundle contains partial or unexpected files")
    for name in expected_files:
        _assert_no_reparse_components(path / name)
    manifest = _load_json(path / MANIFEST_NAME)
    receipt = _load_json(path / RECEIPT_NAME)
    if frozenset(manifest) != _MANIFEST_KEYS:
        raise RecoveryValidationError("manifest fields do not match the strict contract")
    if frozenset(receipt) != _RECEIPT_KEYS:
        raise RecoveryValidationError("receipt fields do not match the strict contract")
    if manifest["artifact"] != "dawnstrike-durable-state-backup":
        raise RecoveryValidationError("manifest artifact type mismatch")
    if manifest["schema_version"] != SCHEMA_VERSION:
        raise RecoveryValidationError("manifest schema version mismatch")
    if not isinstance(manifest["backup_id"], str) or path.name != manifest["backup_id"]:
        raise RecoveryValidationError("manifest/bundle identity mismatch")
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", manifest["backup_id"]):
        raise RecoveryValidationError("manifest backup id is unsafe")
    if not isinstance(manifest["created_at"], str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T.*Z", manifest["created_at"]
    ):
        raise RecoveryValidationError("manifest creation time is invalid")
    for hash_field in (
        "source_live_main_file_sha256",
        "backup_db_sha256",
    ):
        if not isinstance(manifest.get(hash_field), str) or not _SHA256.fullmatch(
            manifest[hash_field]
        ):
            raise RecoveryValidationError(f"manifest {hash_field} is invalid")
    for hash_field in (
        "source_logical_snapshot_sha256",
        "backup_logical_snapshot_sha256",
    ):
        value = manifest.get(hash_field)
        if not isinstance(value, str) or not _SHA256.fullmatch(value):
            raise RecoveryValidationError(f"manifest {hash_field} is invalid")
    for size_field in ("source_live_main_file_bytes", "backup_db_bytes"):
        if not isinstance(manifest.get(size_field), int) or manifest[size_field] < 0:
            raise RecoveryValidationError(f"manifest {size_field} is invalid")
    if manifest.get("source_db_name") != DB_NAME or manifest.get("backup_db_name") != DB_NAME:
        raise RecoveryValidationError("manifest database name mismatch")
    if manifest.get("source_live_main_file_hash_semantics") != (
        "observational_main_database_only_wal_may_be_pending"
    ):
        raise RecoveryValidationError("manifest source hash semantics are invalid")
    release_sha = manifest.get("source_release_sha")
    if not isinstance(release_sha, str) or not _GIT_SHA.fullmatch(release_sha):
        raise RecoveryValidationError(
            "manifest source release SHA is required and invalid"
        )
    if not isinstance(manifest["source_schema_version"], int) or not isinstance(
        manifest["backup_schema_version"], int
    ):
        raise RecoveryValidationError("manifest schema metadata is invalid")
    if manifest["manifest_sha256"] != _self_hash(manifest, "manifest_sha256"):
        raise RecoveryValidationError("manifest self-hash mismatch")
    if receipt.get("receipt_sha256") != _self_hash(receipt, "receipt_sha256"):
        raise RecoveryValidationError("receipt self-hash mismatch")
    if receipt.get("manifest_sha256") != manifest["manifest_sha256"]:
        raise RecoveryValidationError("receipt/manifest hash mismatch")
    if receipt.get("backup_id") != manifest["backup_id"]:
        raise RecoveryValidationError("receipt/manifest identity mismatch")
    if (
        receipt.get("artifact") != "dawnstrike-durable-state-backup-receipt"
        or receipt.get("schema_version") != SCHEMA_VERSION
        or receipt.get("status") != "PASS"
        or receipt.get("write_mode") != "sqlite_online_backup_atomic_bundle"
        or receipt.get("backup_db_sha256") != manifest["backup_db_sha256"]
        or receipt.get("source_release_sha") != manifest.get("source_release_sha")
        or receipt.get("source_live_main_file_sha256")
        != manifest["source_live_main_file_sha256"]
        or receipt.get("source_logical_snapshot_sha256")
        != manifest.get("source_logical_snapshot_sha256")
        or receipt.get("backup_logical_snapshot_sha256")
        != manifest.get("backup_logical_snapshot_sha256")
        or receipt.get("source_schema_version") != manifest["source_schema_version"]
        or receipt.get("backup_schema_version") != manifest["backup_schema_version"]
        or receipt.get("created_at") != manifest["created_at"]
        or receipt.get("automatic_restore") is not False
        or receipt.get("research_only") is not True
        or receipt.get("broker_execution_enabled") is not False
    ):
        raise RecoveryValidationError("recovery receipt contract mismatch")
    db = path / DB_NAME
    size, digest = _sha256(db)
    if size != manifest["backup_db_bytes"] or digest != manifest["backup_db_sha256"]:
        raise RecoveryValidationError("backup database hash or size mismatch")
    # Validation must be observational: never create sidecars inside a sealed
    # three-file bundle merely by opening its database.
    metadata = _db_metadata(db, read_only=True, immutable=True)
    if metadata["quick_check"] != manifest["backup_quick_check"]:
        raise RecoveryValidationError("backup quick_check mismatch")
    if metadata["schema_version"] != manifest["backup_schema_version"]:
        raise RecoveryValidationError("backup schema mismatch")
    logical_snapshot = _logical_snapshot_sha256(db, immutable=True)
    if logical_snapshot != manifest["backup_logical_snapshot_sha256"]:
        raise RecoveryValidationError("backup logical snapshot hash mismatch")
    if manifest["source_quick_check"] != "ok" or manifest["backup_quick_check"] != "ok":
        raise RecoveryValidationError("recovery artifact is not known-good")
    if (
        manifest.get("research_only") is not True
        or manifest.get("broker_execution_enabled") is not False
    ):
        raise RecoveryValidationError("recovery artifact safety flags are invalid")
    return {
        "status": "PASS",
        "backup_id": manifest["backup_id"],
        "bundle_path": str(path),
        "manifest_sha256": manifest["manifest_sha256"],
        "backup_db_sha256": digest,
        "backup_logical_snapshot_sha256": manifest.get(
            "backup_logical_snapshot_sha256"
        ),
        "source_logical_snapshot_sha256": manifest.get(
            "source_logical_snapshot_sha256"
        ),
        "source_live_main_file_sha256": manifest["source_live_main_file_sha256"],
        "source_release_sha": manifest.get("source_release_sha"),
        "created_at": manifest["created_at"],
        "schema_version": metadata["schema_version"],
        "quick_check": metadata["quick_check"],
        "write_performed": False,
    }


def _apply_retention(root: Path, retention: int) -> None:
    valid: list[tuple[str, Path]] = []
    for candidate in root.iterdir():
        if not candidate.is_dir() or candidate.name.startswith(".incomplete-"):
            continue
        try:
            result = validate_backup(candidate, backup_root=root)
        except (OSError, RecoveryValidationError, sqlite3.Error):
            continue
        valid.append((str(result["created_at"]), candidate))
    valid.sort(key=lambda item: item[0], reverse=True)
    # Never delete the newest known-good artifact; retention=1 is therefore
    # safe even if all older bundles are invalid or concurrently changing.
    for _, candidate in valid[retention:]:
        if len(valid) - 1 < 1:
            break
        shutil.rmtree(candidate)


def restore_verify(
    bundle: str | Path,
    target_db: str | Path,
    *,
    backup_root: str | Path | None = None,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    """Validate a backup and describe a non-mutating restore verification."""

    verified = validate_backup(bundle, backup_root=backup_root)
    target = _resolve(target_db)
    if state_root is not None:
        resolved_state = _resolve(state_root)
        if not _is_within(target, resolved_state):
            raise RecoveryValidationError("restore target must be contained by state root")
        if backup_root is not None:
            resolved_root = _resolve(backup_root)
            if _is_within(resolved_root, resolved_state) or _is_within(
                resolved_state, resolved_root
            ):
                raise RecoveryValidationError(
                    "backup root must be separate from and outside the state root"
                )
    return {
        **verified,
        "status": "VERIFY",
        "target_db": str(target),
        "target_exists": target.exists(),
        "would_overwrite": target.exists(),
        "write_performed": False,
        "automatic_overwrite": False,
    }


def _restore_archive_sidecar(
    source: Path, archive: Path, *, label: str
) -> dict[str, Any]:
    """Move one live SQLite sidecar to an attempt-bound archive idempotently."""

    _assert_no_reparse_components(source)
    _assert_no_reparse_components(archive.parent)
    _assert_no_reparse_components(archive)
    if source.exists() and not source.is_file():
        raise RecoveryValidationError(f"{label} is not a regular file")
    if archive.exists():
        if not archive.is_file():
            raise RecoveryValidationError(f"{label} archive is not a regular file")
        if source.exists():
            _, source_hash = _sha256(source)
            _, archive_hash = _sha256(archive)
            if source_hash != archive_hash:
                raise RecoveryValidationError(f"{label} archive conflicts with live sidecar")
            source.unlink()
            if source.exists():
                raise RecoveryValidationError(f"{label} sidecar could not be retired")
        return {"path": str(archive), "sha256": _sha256(archive)[1], "moved": False}
    if not source.exists():
        return {"path": "NONE", "sha256": hashlib.sha256(b"").hexdigest(), "moved": False}
    archive.parent.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(archive.parent)
    _assert_no_reparse_components(source)
    _assert_no_reparse_components(archive)
    os.replace(source, archive)
    if source.exists() or not archive.is_file():
        raise RecoveryValidationError(f"{label} sidecar archive was not proven")
    return {"path": str(archive), "sha256": _sha256(archive)[1], "moved": True}


def restore_exact(
    bundle: str | Path,
    target_db: str | Path,
    *,
    backup_root: str | Path,
    state_root: str | Path,
    expected_source_sha: str,
    expected_before: dict[str, str] | None = None,
    archive_root: str | Path | None = None,
    attempt_id: str | None = None,
) -> dict[str, Any]:
    """Restore one sealed state-preparation snapshot.

    Unlike VERIFY and PLAN, this operation is explicitly mutating.  It is
    only admissible with a candidate release SHA and the pre-mutation hashes
    sealed while the governed runtime lock was held.  Existing WAL/SHM
    sidecars are archived before the verified online-backup image is installed
    so SQLite cannot overlay the restored snapshot.

    SQLite online backups can have a different physical main-file encoding
    than their source.  The source physical hashes and the authoritative
    logical snapshot therefore remain in the result; the installed image is
    proved by its sealed backup hash and logical snapshot hash.
    """

    if not isinstance(expected_source_sha, str) or not _GIT_SHA.fullmatch(
        expected_source_sha
    ):
        raise RecoveryValidationError("restore source release SHA is invalid")
    state = _resolve(state_root)
    root = _resolve(backup_root)
    target = _resolve(target_db)
    if not state.is_dir():
        raise RecoveryValidationError("restore state root must be a directory")
    if target.name != DB_NAME or not target.is_file():
        raise RecoveryValidationError("restore target must be an existing shadow_real.sqlite")
    if not _is_within(target, state) or target == state:
        raise RecoveryValidationError("restore target must be contained by state root")
    if _is_within(root, state) or _is_within(state, root):
        raise RecoveryValidationError(
            "backup root must be separate from and outside the state root"
        )
    verified = validate_backup(bundle, backup_root=root)
    if verified.get("source_release_sha") != expected_source_sha.lower():
        raise RecoveryValidationError("restore backup is bound to a different release")
    bundle_path = _resolve(bundle)
    manifest = _load_json(bundle_path / MANIFEST_NAME)
    expected = expected_before or {}
    for field in ("db_sha256", "wal_sha256", "shm_sha256", "logical_snapshot_sha256"):
        value = expected.get(field)
        if value is not None and not _SHA256.fullmatch(str(value)):
            raise RecoveryValidationError(f"expected pre-restore {field} is invalid")
    if expected.get("db_sha256") is not None and manifest.get(
        "source_live_main_file_sha256"
    ) != expected["db_sha256"]:
        raise RecoveryValidationError("restore backup does not bind the pre-mutation main file")
    if expected.get("logical_snapshot_sha256") is not None and manifest.get(
        "source_logical_snapshot_sha256"
    ) != expected["logical_snapshot_sha256"]:
        raise RecoveryValidationError(
            "restore backup does not bind the pre-mutation logical snapshot"
        )

    bundle_hash = str(verified["manifest_sha256"])
    attempt = attempt_id or bundle_hash
    if not isinstance(attempt, str) or not re.fullmatch(r"[0-9a-f]{64}", attempt):
        raise RecoveryValidationError("restore attempt identity is invalid")
    archive = (
        _resolve(archive_root)
        if archive_root is not None
        else state / "receipts" / "state-preparation" / "recovery"
    )
    if not _is_within(archive, state) or archive == state:
        raise RecoveryValidationError("restore sidecar archive must be inside StateRoot")
    _assert_no_reparse_components(archive)
    archive.mkdir(parents=True, exist_ok=True)
    _assert_no_reparse_components(archive)

    sidecars: dict[str, Any] = {}
    for suffix in ("-wal", "-shm"):
        sidecars[suffix] = _restore_archive_sidecar(
            target.with_name(target.name + suffix),
            archive / f"restore-{attempt}{suffix}",
            label=f"SQLite {suffix} sidecar",
        )

    source_db = bundle_path / DB_NAME
    _assert_no_reparse_components(source_db)
    temporary = target.with_name(f".{target.name}.restore-{attempt}.tmp")
    _assert_no_reparse_components(temporary)
    if temporary.exists():
        raise RecoveryValidationError("restore temporary image already exists")
    try:
        with source_db.open("rb") as source_handle, temporary.open("xb") as target_handle:
            shutil.copyfileobj(source_handle, target_handle, length=1024 * 1024)
            target_handle.flush()
            os.fsync(target_handle.fileno())
        _assert_no_reparse_components(target)
        _assert_no_reparse_components(temporary)
        if _sha256(temporary)[1] != verified["backup_db_sha256"]:
            raise RecoveryValidationError("restore temporary image hash changed")
        try:
            os.replace(temporary, target)
        except PermissionError:
            # A read-only SQLite handle can remain open briefly on Windows
            # after a failed inventory probe.  Replacing the directory entry
            # is the preferred atomic path; in this narrow governed recovery
            # window fall back to a verified full-file copy rather than
            # deleting the live database and losing the sealed source.
            shutil.copyfile(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)
    if not target.is_file() or _sha256(target)[1] != verified["backup_db_sha256"]:
        raise RecoveryValidationError("restored database hash was not proven")
    restored_logical = _logical_snapshot_sha256(target, immutable=True)
    if restored_logical != verified["backup_logical_snapshot_sha256"]:
        raise RecoveryValidationError("restored database logical snapshot was not proven")
    metadata = _db_metadata(target, read_only=True, immutable=True)
    if metadata["quick_check"] != "ok":
        raise RecoveryValidationError("restored database quick_check failed")
    return {
        **verified,
        "status": "RESTORED",
        "target_db": str(target),
        "target_db_sha256": _sha256(target)[1],
        "target_logical_snapshot_sha256": restored_logical,
        "pre_restore_db_sha256": manifest["source_live_main_file_sha256"],
        "pre_restore_logical_snapshot_sha256": manifest[
            "source_logical_snapshot_sha256"
        ],
        "sidecars": sidecars,
        "write_performed": True,
        "automatic_overwrite": True,
        "exact_logical_snapshot": True,
        "restore_attempt": attempt,
    }


def restore_plan(
    bundle: str | Path,
    target_db: str | Path,
    *,
    backup_root: str | Path | None = None,
    state_root: str | Path | None = None,
) -> dict[str, Any]:
    """Produce an explicit restore plan.  No copy or overwrite is performed."""

    verified = restore_verify(
        bundle, target_db, backup_root=backup_root, state_root=state_root
    )
    return {**verified, "status": "PLAN", "action": "MANUAL_RESTORE_REQUIRED"}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--source-db", required=True)
    backup.add_argument("--backup-root", required=True)
    backup.add_argument("--state-root")
    backup.add_argument("--retention", type=int, default=7)
    backup.add_argument("--source-sha", required=True)
    backup.add_argument("--backup-id")
    backup.add_argument("--created-at")
    backup.add_argument("--expected-db-sha256")
    backup.add_argument("--expected-wal-sha256")
    backup.add_argument("--expected-shm-sha256")
    backup.add_argument("--expected-logical-snapshot-sha256")
    for name, function_name in (("restore-verify", "verify"), ("restore-plan", "plan")):
        command = subparsers.add_parser(name)
        command.add_argument("--bundle", required=True)
        command.add_argument("--target-db", required=True)
        command.add_argument("--backup-root")
        command.add_argument("--state-root")
        command.set_defaults(restore_function=function_name)
    # A grouped spelling is useful for operators and remains explicit.
    restore = subparsers.add_parser("restore")
    restore.add_argument("mode", choices=("VERIFY", "PLAN"))
    restore.add_argument("--bundle", required=True)
    restore.add_argument("--target-db", required=True)
    restore.add_argument("--backup-root")
    restore.add_argument("--state-root")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "backup":
            result = create_backup(
                args.source_db,
                args.backup_root,
                state_root=args.state_root,
                retention=args.retention,
                source_sha=args.source_sha,
                backup_id=args.backup_id,
                created_at=args.created_at,
                expected_db_sha256=args.expected_db_sha256,
                expected_wal_sha256=args.expected_wal_sha256,
                expected_shm_sha256=args.expected_shm_sha256,
                expected_logical_snapshot_sha256=args.expected_logical_snapshot_sha256,
            )
        elif args.command == "restore" or args.command.startswith("restore-"):
            is_plan = (
                getattr(args, "restore_function", None) == "plan"
                or getattr(args, "mode", "VERIFY") == "PLAN"
            )
            function = restore_plan if is_plan else restore_verify
            result = function(
                args.bundle,
                args.target_db,
                backup_root=args.backup_root,
                state_root=args.state_root,
            )
        else:
            raise RecoveryValidationError("unknown recovery command")
    except (OSError, RecoveryValidationError, sqlite3.Error, ValueError) as exc:
        print(
            json.dumps(
                {"status": "FAILED", "error": f"{type(exc).__name__}: {exc}"},
                indent=2,
                sort_keys=True,
            )
        )
        return 2
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
