"""Narrow append-only facade for retained intraday evidence."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.data_truth.intraday import (
    IntradayArtifactManifest,
    IntradayCoverageReceipt,
)


class EvidenceStoreError(StorageError):
    """Base error for evidence-spine persistence failures."""


class RetentionNotPermittedError(EvidenceStoreError):
    """Raised before writing bytes when the source does not permit retention."""


class SourceConflictError(EvidenceStoreError):
    """Raised when one source identity resolves to different content."""

    status = "SOURCE_CONFLICT"


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")


class IntradayEvidenceStore:
    """Persist compressed source artifacts and append-only lineage receipts."""

    def __init__(
        self,
        db_path: str | Path,
        *,
        evidence_root: str | Path | None = None,
        code_sha: str = "unknown",
    ) -> None:
        self.db_path = Path(db_path)
        configured_root = evidence_root or os.environ.get(
            "DAWNSTRIKE_INTRADAY_EVIDENCE_ROOT"
        )
        self.evidence_root = Path(configured_root or "data/intraday_evidence").resolve()
        self.code_sha = code_sha
        self._sqlite_store = SQLiteScanStore(self.db_path)

    def initialize(self) -> None:
        self._sqlite_store.initialize()

    def store_artifact(
        self,
        *,
        provider: str,
        feed: str,
        artifact_kind: str,
        symbol: str,
        market_date: str,
        exchange_session_id: str,
        entitlement: str,
        request_start: datetime,
        request_end: datetime,
        fetched_at: datetime,
        raw_bytes: bytes,
        normalized_bytes: bytes,
        retention_allowed: bool,
        retention_status: str = "retained",
        code_sha: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> IntradayArtifactManifest:
        """Store one content-addressed raw/normalized artifact pair.

        The identity intentionally excludes content hashes.  This makes a
        second fetch idempotent while allowing a different payload for the
        same provider/feed/window to be surfaced as ``SOURCE_CONFLICT``.
        """

        if not retention_allowed:
            raise RetentionNotPermittedError(
                "intraday artifact retention is not permitted for this source"
            )
        self.initialize()
        _require_utc(request_start, "request_start")
        _require_utc(request_end, "request_end")
        _require_utc(fetched_at, "fetched_at")
        raw_hash = _sha256(raw_bytes)
        normalized_hash = _sha256(normalized_bytes)
        identity = _stable_hash(
            {
                "artifact_kind": artifact_kind,
                "exchange_session_id": exchange_session_id,
                "feed": feed,
                "market_date": market_date,
                "provider": provider,
                "request_end": request_end.isoformat(),
                "request_start": request_start.isoformat(),
                "symbol": symbol,
            }
        )
        existing = self._load_manifest_by_identity(identity)
        if existing is not None:
            if (
                existing.raw_artifact_hash_sha256 != raw_hash
                or existing.normalized_artifact_hash_sha256 != normalized_hash
            ):
                raise SourceConflictError(
                    f"{identity}: existing artifact hashes differ from the new payload"
                )
            self._ensure_artifact_files(existing, raw_bytes, normalized_bytes)
            return existing

        artifact_manifest_id = _stable_hash(
            {
                "artifact_identity": identity,
                "normalized_artifact_hash_sha256": normalized_hash,
                "raw_artifact_hash_sha256": raw_hash,
            }
        )
        raw_path = self._artifact_path(
            provider,
            feed,
            artifact_kind,
            market_date,
            symbol,
            "raw",
            raw_hash,
        )
        normalized_path = self._artifact_path(
            provider,
            feed,
            artifact_kind,
            market_date,
            symbol,
            "normalized",
            normalized_hash,
        )
        self._atomic_write_gzip(raw_path, raw_bytes)
        self._atomic_write_gzip(normalized_path, normalized_bytes)
        manifest = IntradayArtifactManifest(
            artifact_manifest_id=artifact_manifest_id,
            artifact_identity=identity,
            provider=provider,
            feed=feed,
            artifact_kind=artifact_kind,
            symbol=symbol,
            market_date=market_date,
            exchange_session_id=exchange_session_id,
            request_start=request_start,
            request_end=request_end,
            fetched_at=fetched_at,
            code_sha=code_sha or self.code_sha,
            raw_artifact_hash_sha256=raw_hash,
            normalized_artifact_hash_sha256=normalized_hash,
            raw_artifact_path=str(raw_path),
            normalized_artifact_path=str(normalized_path),
            retention_status=retention_status,
            created_at=datetime.now(timezone.utc),
            metadata={
                "entitlement": entitlement,
                **(metadata or {}),
            },
        )
        try:
            with self._sqlite_store._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO intraday_artifact_manifests (
                        artifact_manifest_id, provider, feed, artifact_kind, symbol,
                        market_date, exchange_session_id, request_start, request_end,
                        fetched_at, code_sha, raw_artifact_hash_sha256,
                        normalized_artifact_hash_sha256, raw_artifact_path,
                        normalized_artifact_path, retention_status, artifact_identity,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        manifest.artifact_manifest_id,
                        manifest.provider,
                        manifest.feed,
                        manifest.artifact_kind,
                        manifest.symbol,
                        manifest.market_date,
                        manifest.exchange_session_id,
                        manifest.request_start.isoformat(),
                        manifest.request_end.isoformat(),
                        manifest.fetched_at.isoformat(),
                        manifest.code_sha,
                        manifest.raw_artifact_hash_sha256,
                        manifest.normalized_artifact_hash_sha256,
                        manifest.raw_artifact_path,
                        manifest.normalized_artifact_path,
                        manifest.retention_status,
                        manifest.artifact_identity,
                        manifest.to_json(),
                        manifest.created_at.isoformat(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            concurrent = self._load_manifest_by_identity(identity)
            if concurrent is not None and (
                concurrent.raw_artifact_hash_sha256 != raw_hash
                or concurrent.normalized_artifact_hash_sha256 != normalized_hash
            ):
                raise SourceConflictError(
                    f"{identity}: concurrent artifact hashes differ"
                ) from exc
            if concurrent is not None:
                return concurrent
            raise EvidenceStoreError(f"could not persist artifact manifest: {exc}") from exc
        except sqlite3.Error as exc:
            raise EvidenceStoreError(f"could not persist artifact manifest: {exc}") from exc
        return manifest

    def record_coverage(self, receipt: IntradayCoverageReceipt) -> IntradayCoverageReceipt:
        """Persist a coverage fact without converting it into a strategy result."""

        self.initialize()
        identity = _stable_hash(
            {
                "feed": receipt.feed,
                "market_date": receipt.market_date,
                "provider": receipt.provider,
                "request_end": receipt.request_end.isoformat(),
                "request_start": receipt.request_start.isoformat(),
                "symbol": receipt.symbol,
                "exchange_session_id": receipt.exchange_session_id,
            }
        )
        with self._sqlite_store._connect() as connection:
            existing_row = connection.execute(
                "SELECT payload_json FROM intraday_coverage_receipts "
                "WHERE coverage_identity = ?",
                (identity,),
            ).fetchone()
            if existing_row is not None:
                existing = IntradayCoverageReceipt.from_json(str(existing_row[0]))
                if (
                    existing.source_metadata.raw_artifact_hash_sha256
                    != receipt.source_metadata.raw_artifact_hash_sha256
                    or existing.source_metadata.normalized_artifact_hash_sha256
                    != receipt.source_metadata.normalized_artifact_hash_sha256
                ):
                    raise SourceConflictError(
                        f"{identity}: coverage hashes differ from the existing receipt"
                    )
                return existing
            try:
                connection.execute(
                    """
                    INSERT INTO intraday_coverage_receipts (
                        coverage_receipt_id, provider, feed, entitlement, symbol,
                        market_date, exchange_session_id, request_start, request_end,
                        observed_start, observed_end, status, artifact_manifest_id,
                        code_sha, raw_artifact_hash_sha256,
                        normalized_artifact_hash_sha256, retention_status,
                        coverage_identity, payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt.coverage_receipt_id,
                        receipt.provider,
                        receipt.feed,
                        receipt.entitlement,
                        receipt.symbol,
                        receipt.market_date,
                        receipt.exchange_session_id,
                        receipt.request_start.isoformat(),
                        receipt.request_end.isoformat(),
                        _iso_or_none(receipt.observed_start),
                        _iso_or_none(receipt.observed_end),
                        receipt.status.value,
                        receipt.artifact_manifest_ids[0]
                        if receipt.artifact_manifest_ids
                        else None,
                        receipt.source_metadata.code_sha,
                        receipt.source_metadata.raw_artifact_hash_sha256,
                        receipt.source_metadata.normalized_artifact_hash_sha256,
                        receipt.source_metadata.retention_status,
                        identity,
                        receipt.to_json(),
                        (receipt.created_at or datetime.now(timezone.utc)).isoformat(),
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise EvidenceStoreError(f"could not persist coverage receipt: {exc}") from exc
        return receipt

    def record_capability_receipt(
        self,
        *,
        capability_receipt_id: str,
        provider: str,
        feed: str,
        entitlement: str,
        requested_at: datetime,
        request_start: datetime,
        request_end: datetime,
        fetched_at: datetime,
        raw_artifact_hash_sha256: str,
        normalized_artifact_hash_sha256: str,
        retention_status: str,
        capabilities: dict[str, Any],
        receipt_hash_sha256: str,
        code_sha: str | None = None,
    ) -> str:
        self.initialize()
        for name, value in (
            ("requested_at", requested_at),
            ("request_start", request_start),
            ("request_end", request_end),
            ("fetched_at", fetched_at),
        ):
            _require_utc(value, name)
        with self._sqlite_store._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO intraday_provider_capability_receipts (
                    capability_receipt_id, provider, feed, entitlement, requested_at,
                    request_start, request_end, fetched_at, code_sha,
                    raw_artifact_hash_sha256, normalized_artifact_hash_sha256,
                    retention_status, capabilities_json, receipt_hash_sha256, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    capability_receipt_id,
                    provider,
                    feed,
                    entitlement,
                    requested_at.isoformat(),
                    request_start.isoformat(),
                    request_end.isoformat(),
                    fetched_at.isoformat(),
                    code_sha or self.code_sha,
                    raw_artifact_hash_sha256,
                    normalized_artifact_hash_sha256,
                    retention_status,
                    _stable_json(capabilities),
                    receipt_hash_sha256,
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return capability_receipt_id

    def record_legacy_policy_classification(
        self,
        *,
        classification_id: str,
        source_db_hash_sha256: str,
        source_code_sha: str,
        classifier_version: str,
        generated_at: datetime,
        inferred_policy: str,
        membership_hash_sha256: str,
        payload: dict[str, Any],
    ) -> str:
        self.initialize()
        _require_utc(generated_at, "generated_at")
        with self._sqlite_store._connect() as connection:
            connection.execute(
                """
                INSERT OR IGNORE INTO legacy_policy_classifications (
                    classification_id, source_db_hash_sha256, source_code_sha,
                    classifier_version, generated_at, inferred_policy,
                    membership_hash_sha256, payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    classification_id,
                    source_db_hash_sha256,
                    source_code_sha,
                    classifier_version,
                    generated_at.isoformat(),
                    inferred_policy,
                    membership_hash_sha256,
                    _stable_json(payload),
                    datetime.now(timezone.utc).isoformat(),
                ),
            )
        return classification_id

    def _load_manifest_by_identity(self, identity: str) -> IntradayArtifactManifest | None:
        if not self.db_path.exists():
            return None
        try:
            with self._sqlite_store.connect_read_only() as connection:
                row = connection.execute(
                    "SELECT payload_json FROM intraday_artifact_manifests "
                    "WHERE artifact_identity = ?",
                    (identity,),
                ).fetchone()
        except sqlite3.Error:
            return None
        return (
            IntradayArtifactManifest.from_json(str(row[0])) if row is not None else None
        )

    def _artifact_path(
        self,
        provider: str,
        feed: str,
        artifact_kind: str,
        market_date: str,
        symbol: str,
        representation: str,
        content_hash: str,
    ) -> Path:
        components = (
            provider,
            feed,
            artifact_kind,
            market_date,
            symbol,
        )
        directory = self.evidence_root.joinpath(
            *(_safe_component(component) for component in components)
        )
        return directory / f"{representation}-{content_hash}.bin.gz"

    def _ensure_artifact_files(
        self,
        manifest: IntradayArtifactManifest,
        raw_bytes: bytes,
        normalized_bytes: bytes,
    ) -> None:
        self._atomic_write_gzip(Path(manifest.raw_artifact_path), raw_bytes)
        self._atomic_write_gzip(Path(manifest.normalized_artifact_path), normalized_bytes)

    @staticmethod
    def _atomic_write_gzip(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            return
        compressed = gzip.compress(content, mtime=0)
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            with temporary.open("wb") as handle:
                handle.write(compressed)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if temporary.exists():
                temporary.unlink()


def _require_utc(value: datetime, field_name: str) -> None:
    offset = value.utcoffset()
    if value.tzinfo is None or offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{field_name} must be timezone-aware UTC")


def _iso_or_none(value: datetime | None) -> str | None:
    if value is None:
        return None
    _require_utc(value, "timestamp")
    return value.isoformat()


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def _stable_hash(value: Any) -> str:
    return _sha256(_stable_json(value).encode("utf-8"))


def _safe_component(value: str) -> str:
    cleaned = _SAFE_COMPONENT.sub("_", value.strip())
    return cleaned or "unknown"


__all__ = [
    "EvidenceStoreError",
    "IntradayEvidenceStore",
    "RetentionNotPermittedError",
    "SourceConflictError",
]
