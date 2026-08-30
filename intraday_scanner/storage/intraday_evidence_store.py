"""Narrow append-only facade for retained intraday evidence."""

from __future__ import annotations

import gzip
import hashlib
import json
import os
import re
import sqlite3
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from intraday_scanner.alpha.path_replay import (
    canonical_path_contract_valid,
    canonical_path_return_eligible,
)
from intraday_scanner.errors import StorageError
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.data_truth.intraday import (
    IntradayArtifactManifest,
    IntradayCoverageReceipt,
    IntradayProviderCapabilityReceipt,
)


class EvidenceStoreError(StorageError):
    """Base error for evidence-spine persistence failures."""


class RetentionNotPermittedError(EvidenceStoreError):
    """Raised before writing bytes when the source does not permit retention."""


class SourceConflictError(EvidenceStoreError):
    """Raised when one source identity resolves to different content."""

    status = "SOURCE_CONFLICT"


_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_.-]+")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


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

    def persist_committed_fill_truth_receipt(self, receipt: dict[str, Any]) -> bool:
        """Persist one immutable committed FillTruth envelope."""

        return self._sqlite_store.persist_committed_fill_truth_receipt(receipt)

    def load_committed_fill_truth_receipt_record(
        self, receipt_id: str
    ) -> dict[str, Any] | None:
        """Load an unmerged receipt record for CommitBridge validation."""

        return self._sqlite_store.load_committed_fill_truth_receipt_record(receipt_id)

    def load_committed_fill_truth_receipt(
        self, receipt_id: str
    ) -> dict[str, Any] | None:
        """Load the canonical committed FillTruth payload."""

        return self._sqlite_store.load_committed_fill_truth_receipt(receipt_id)

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

    def record_provider_capability_receipt(
        self, receipt: IntradayProviderCapabilityReceipt
    ) -> str:
        """Persist a typed, sanitized capability receipt."""

        return self.record_capability_receipt(
            capability_receipt_id=receipt.capability_receipt_id,
            provider=receipt.provider,
            feed=receipt.feed,
            entitlement=receipt.entitlement,
            requested_at=receipt.requested_at,
            request_start=receipt.request_start,
            request_end=receipt.request_end,
            fetched_at=receipt.fetched_at,
            raw_artifact_hash_sha256=receipt.raw_artifact_hash_sha256,
            normalized_artifact_hash_sha256=receipt.normalized_artifact_hash_sha256,
            retention_status=receipt.retention_status,
            capabilities=receipt.capabilities,
            receipt_hash_sha256=receipt.receipt_hash_sha256,
            code_sha=receipt.code_sha,
        )

    def persist_path_replay(self, replay: dict[str, Any]) -> dict[str, int]:
        """Append one immutable path replay and return its insert status."""

        if not canonical_path_contract_valid(replay):
            raise EvidenceStoreError("canonical path replay contract validation failed")
        if any(
            not isinstance(replay.get(key), str)
            or not str(replay[key])
            or str(replay[key]) != str(replay[key]).strip()
            for key in ("cohort", "selection_id", "market_date")
        ):
            raise EvidenceStoreError(
                "canonical path replay envelope fields must be nonblank strings"
            )
        try:
            parsed_market_date = date.fromisoformat(str(replay["market_date"]))
        except ValueError as exc:
            raise EvidenceStoreError(
                "canonical path replay market_date must be ISO formatted"
            ) from exc
        if parsed_market_date.isoformat() != replay["market_date"]:
            raise EvidenceStoreError(
                "canonical path replay market_date must be canonical ISO"
            )
        manifest = replay.get("replay_input_manifest")
        replay_binding = manifest.get("replay_binding") if isinstance(manifest, dict) else None
        origin = replay_binding.get("origin") if isinstance(replay_binding, dict) else None
        subject = replay_binding.get("subject") if isinstance(replay_binding, dict) else None
        lineage = origin.get("lineage") if isinstance(origin, dict) else None
        if not (
            isinstance(origin, dict)
            and origin.get("kind") == "alpha_paper_selection"
            and isinstance(lineage, dict)
            and isinstance(subject, dict)
            and replay.get("selection_id") == lineage.get("selection_id")
            and replay.get("signal_id") == lineage.get("signal_id")
            and replay.get("market_date") == subject.get("market_date")
        ):
            raise EvidenceStoreError(
                "canonical path replay market_date/identity paper selection "
                "replay binding conflicts with the required envelope"
            )
        decision_at = manifest.get("decision_at") if isinstance(manifest, dict) else None
        if not isinstance(decision_at, str):
            raise EvidenceStoreError(
                "canonical path replay market_date requires a bound decision date"
            )
        try:
            parsed_decision_at = datetime.fromisoformat(decision_at)
        except ValueError as exc:
            raise EvidenceStoreError(
                "canonical path replay market_date requires a bound decision date"
            ) from exc
        decision_offset = parsed_decision_at.utcoffset()
        if not (
            parsed_decision_at.tzinfo is not None
            and decision_offset is not None
            and decision_offset.total_seconds() == 0.0
            and parsed_decision_at.isoformat() == decision_at
            and parsed_decision_at.date() == parsed_market_date
        ):
            raise EvidenceStoreError(
                "canonical path replay market_date conflicts with decision date"
            )
        signal_id = replay.get("signal_id")
        if signal_id is not None and (
            not isinstance(signal_id, str)
            or not signal_id
            or signal_id != signal_id.strip()
        ):
            raise EvidenceStoreError(
                "canonical path replay signal_id must be null or a nonblank string"
            )
        if "created_at" in replay:
            created_at = replay["created_at"]
            if not isinstance(created_at, str):
                raise EvidenceStoreError(
                    "canonical path replay created_at must be canonical UTC ISO"
                )
            try:
                parsed_created_at = datetime.fromisoformat(created_at)
            except ValueError as exc:
                raise EvidenceStoreError(
                    "canonical path replay created_at must be canonical UTC ISO"
                ) from exc
            created_at_offset = parsed_created_at.utcoffset()
            if not (
                parsed_created_at.tzinfo is not None
                and created_at_offset is not None
                and created_at_offset.total_seconds() == 0.0
                and parsed_created_at.isoformat() == created_at
            ):
                raise EvidenceStoreError(
                    "canonical path replay created_at must be canonical UTC ISO"
                )
        if any(
            type(replay.get(key)) is not bool
            for key in (
                "retrospective_research_eligible",
                "prospective_promotion_eligible",
            )
        ):
            raise EvidenceStoreError(
                "canonical path replay eligibility fields must be exact booleans"
            )
        if replay["prospective_promotion_eligible"] and not replay[
            "retrospective_research_eligible"
        ]:
            raise EvidenceStoreError(
                "canonical path replay prospective eligibility requires retrospective eligibility"
            )
        source_identity = replay.get("source_artifact_identity")
        source_hash = replay.get("source_artifact_hash_sha256")
        if not (
            isinstance(source_identity, str)
            and source_identity.strip()
            and isinstance(source_hash, str)
            and _SHA256.fullmatch(source_hash)
        ):
            raise EvidenceStoreError(
                "canonical path replay source identity and SHA256 are required"
            )
        if not (
            replay.get("artifact_identity") == source_identity
            and replay.get("artifact_hash_sha256") == source_hash
        ):
            raise EvidenceStoreError(
                "canonical path replay envelope conflicts with source identity"
            )
        if (
            replay["retrospective_research_eligible"]
            or replay["prospective_promotion_eligible"]
        ) and not canonical_path_return_eligible(replay):
            raise EvidenceStoreError(
                "canonical path replay eligibility conflicts with path truth"
            )
        self.initialize()
        policy_version = str(
            replay.get("path_replay_policy_version")
            or replay.get("policy_version")
            or ""
        )
        payload_json = _stable_json(replay)
        with self._sqlite_store._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO alpha_path_replays (
                    path_replay_id, cohort, selection_id, signal_id, market_date,
                    policy_version, artifact_identity, artifact_hash_sha256,
                    path_truth_status, conservative_policy_result, entry_at,
                    entry_price, target_touched_at, stop_touched_at, exit_at,
                    exit_price, mfe_price, mfe_at, mae_price, mae_at,
                    retrospective_research_eligible,
                    prospective_promotion_eligible, payload_json, created_at
                ) VALUES (
                    ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                    ?, ?, ?, ?
                )
                """,
                (
                    replay["path_replay_id"],
                    replay["cohort"],
                    replay["selection_id"],
                    replay.get("signal_id"),
                    replay["market_date"],
                    policy_version,
                    replay["artifact_identity"],
                    replay["artifact_hash_sha256"],
                    replay["path_truth_status"],
                    replay.get("conservative_policy_result"),
                    replay.get("entry_time"),
                    _number_or_none(replay.get("entry_price")),
                    replay.get("target_touched_at"),
                    replay.get("stop_touched_at"),
                    replay.get("exit_time"),
                    _number_or_none(replay.get("exit_price")),
                    _number_or_none(replay.get("mfe_price")),
                    replay.get("mfe_at"),
                    _number_or_none(replay.get("mae_price")),
                    replay.get("mae_at"),
                    int(bool(replay.get("retrospective_research_eligible", False))),
                    int(bool(replay.get("prospective_promotion_eligible", False))),
                    payload_json,
                    replay.get("created_at", datetime.now(timezone.utc).isoformat()),
                ),
            )
            if not cursor.rowcount:
                existing = connection.execute(
                    "SELECT payload_json FROM alpha_path_replays WHERE path_replay_id = ?",
                    (replay["path_replay_id"],),
                ).fetchone()
                if existing is None or str(existing[0]) != payload_json:
                    raise EvidenceStoreError(
                        "immutable path replay conflict for existing path_replay_id"
                    )
                return {"inserted": 0, "row_count": 1}
            row = connection.execute(
                "SELECT COUNT(*) FROM alpha_path_replays WHERE path_replay_id = ?",
                (replay["path_replay_id"],),
            ).fetchone()
        return {"inserted": int(cursor.rowcount), "row_count": int(row[0]) if row else 0}

    def persist_excursion_reconciliation(
        self, reconciliation: dict[str, Any]
    ) -> dict[str, int]:
        """Append verified excursion facts for an unchanged legacy position."""

        self.initialize()
        with self._sqlite_store._connect() as connection:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO paper_position_excursion_reconciliations (
                    reconciliation_id, position_id, path_replay_id,
                    source_bar_hash_sha256, source_quote_hash_sha256,
                    path_truth_status, mfe_price, mfe_at, mae_price, mae_at,
                    mfe_lower_bound, mfe_upper_bound, mae_lower_bound,
                    mae_upper_bound, reconciliation_receipt_hash_sha256,
                    payload_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    reconciliation["reconciliation_id"],
                    reconciliation["position_id"],
                    reconciliation["path_replay_id"],
                    reconciliation["source_bar_hash_sha256"],
                    reconciliation["source_quote_hash_sha256"],
                    reconciliation["path_truth_status"],
                    _number_or_none(reconciliation.get("mfe_price")),
                    reconciliation.get("mfe_at"),
                    _number_or_none(reconciliation.get("mae_price")),
                    reconciliation.get("mae_at"),
                    _number_or_none(reconciliation.get("mfe_lower_bound")),
                    _number_or_none(reconciliation.get("mfe_upper_bound")),
                    _number_or_none(reconciliation.get("mae_lower_bound")),
                    _number_or_none(reconciliation.get("mae_upper_bound")),
                    reconciliation["reconciliation_receipt_hash_sha256"],
                    _stable_json(reconciliation),
                    reconciliation.get("created_at", datetime.now(timezone.utc).isoformat()),
                ),
            )
            row = connection.execute(
                """
                SELECT COUNT(*)
                FROM paper_position_excursion_reconciliations
                WHERE reconciliation_id = ?
                """,
                (reconciliation["reconciliation_id"],),
            ).fetchone()
        return {"inserted": int(cursor.rowcount), "row_count": int(row[0]) if row else 0}

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


def _number_or_none(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


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
