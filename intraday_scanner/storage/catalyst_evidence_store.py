"""Append-only persistence for point-in-time catalyst evidence."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


class CatalystEvidenceStore:
    """Persist catalyst facts and extractor lineage without mutating signals."""

    def __init__(self, db_path: str | Path, *, evidence_root: str | Path) -> None:
        self.db_path = Path(db_path)
        self.evidence_root = Path(evidence_root).resolve()
        self._sqlite_store = SQLiteScanStore(self.db_path)

    def initialize(self) -> None:
        self._sqlite_store.initialize()

    def store_raw_document(
        self,
        *,
        source_kind: str,
        symbol: str,
        content: bytes,
        content_hash_sha256: str | None = None,
    ) -> dict[str, str]:
        """Store source bytes outside SQLite and return only path/hash lineage."""

        digest = content_hash_sha256 or _sha256(content)
        if digest != _sha256(content):
            raise StorageError("catalyst raw content hash does not match bytes")
        directory = self.evidence_root / "catalyst" / _safe(source_kind) / _safe(symbol)
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"{digest}.bin"
        if path.exists() and _sha256(path.read_bytes()) != digest:
            raise StorageError(f"catalyst raw artifact hash conflict: {path}")
        if not path.exists():
            temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
            try:
                temporary.write_bytes(content)
                with temporary.open("r+b") as handle:
                    os.fsync(handle.fileno())
                os.replace(temporary, path)
            finally:
                if temporary.exists():
                    temporary.unlink()
        return {"path": str(path), "hash_sha256": digest}

    def persist_event(self, event: dict[str, Any]) -> dict[str, int]:
        self.initialize()
        try:
            with self._sqlite_store._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO catalyst_evidence_events (
                        event_id, symbol, source_kind, canonical_url,
                        source_content_hash_sha256, published_at, first_seen_at,
                        available_at_decision, decision_at, event_type, polarity,
                        financing_mechanism, novelty, timing, source_coverage_status,
                        promotional_status, rumor_status, squeeze_mechanics,
                        confidence_status, raw_artifact_path, raw_artifact_hash_sha256,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event["event_id"],
                        event["symbol"],
                        event["source_kind"],
                        event.get("canonical_url", ""),
                        event["source_content_hash_sha256"],
                        event.get("published_at"),
                        event["first_seen_at"],
                        int(bool(event.get("available_at_decision"))),
                        event.get("decision_at"),
                        event.get("event_type", "unclassified"),
                        event.get("polarity", "unknown"),
                        event.get("financing_mechanism", "none_or_unknown"),
                        event.get("novelty", "unknown"),
                        event.get("timing", "unknown"),
                        event.get("source_coverage_status", "unknown"),
                        event.get("promotional_status", "unknown"),
                        event.get("rumor_status", "unknown"),
                        event.get("squeeze_mechanics", "unknown"),
                        event.get("confidence_status", "unknown"),
                        event.get("raw_artifact_path"),
                        event.get("raw_artifact_hash_sha256"),
                        _json(event),
                        event.get("created_at", datetime.now(timezone.utc).isoformat()),
                    ),
                )
                row = connection.execute(
                    "SELECT COUNT(*) FROM catalyst_evidence_events WHERE event_id = ?",
                    (event["event_id"],),
                ).fetchone()
            return {"inserted": int(cursor.rowcount), "row_count": int(row[0]) if row else 0}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist catalyst evidence event: {exc}") from exc

    def persist_extraction(self, extraction: dict[str, Any]) -> dict[str, int]:
        self.initialize()
        raw_claims = extraction.get("claims")
        claims: list[Any] = raw_claims if isinstance(raw_claims, list) else []
        spans: list[str] = []
        for claim in claims:
            if not isinstance(claim, dict):
                continue
            raw_spans = claim.get("evidence_spans")
            if not isinstance(raw_spans, list):
                continue
            spans.extend(str(span) for span in raw_spans if str(span).strip())
        try:
            with self._sqlite_store._connect() as connection:
                cursor = connection.execute(
                    """
                    INSERT OR IGNORE INTO catalyst_claim_extractions (
                        extraction_id, event_id, source_content_hash_sha256,
                        prompt_version, schema_version, model, input_hash_sha256,
                        output_hash_sha256, status, evidence_spans_json,
                        payload_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        extraction["extraction_id"],
                        extraction["event_id"],
                        extraction["source_content_hash_sha256"],
                        extraction["prompt_version"],
                        extraction["schema_version"],
                        extraction.get("model", ""),
                        extraction["input_hash_sha256"],
                        extraction["output_hash_sha256"],
                        extraction.get("status", "abstain"),
                        _json(spans),
                        _json(extraction),
                        extraction.get("created_at", datetime.now(timezone.utc).isoformat()),
                    ),
                )
                row = connection.execute(
                    "SELECT COUNT(*) FROM catalyst_claim_extractions WHERE extraction_id = ?",
                    (extraction["extraction_id"],),
                ).fetchone()
            return {"inserted": int(cursor.rowcount), "row_count": int(row[0]) if row else 0}
        except sqlite3.Error as exc:
            raise StorageError(f"Could not persist catalyst claim extraction: {exc}") from exc

    def load_events(self, *, symbol: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT payload_json FROM catalyst_evidence_events"
        params: tuple[Any, ...] = ()
        if symbol:
            query += " WHERE symbol = ?"
            params = (symbol.upper(),)
        query += " ORDER BY published_at ASC, first_seen_at ASC LIMIT ?"
        params += (limit,)
        with self._sqlite_store._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [json.loads(str(row[0])) for row in rows]

    def load_extractions(
        self, *, event_id: str | None = None, limit: int = 100
    ) -> list[dict[str, Any]]:
        self.initialize()
        query = "SELECT payload_json FROM catalyst_claim_extractions"
        params: tuple[Any, ...] = ()
        if event_id:
            query += " WHERE event_id = ?"
            params = (event_id,)
        query += " ORDER BY created_at ASC LIMIT ?"
        params += (limit,)
        with self._sqlite_store._connect() as connection:
            rows = connection.execute(query, params).fetchall()
        return [json.loads(str(row[0])) for row in rows]


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str)


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _safe(value: str) -> str:
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in value
    )
    return cleaned or "unknown"


__all__ = ["CatalystEvidenceStore"]
