"""Append-only SQLite adapter for validation evidence and locked-OOS use."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.opportunity_validation_contracts import (
    ValidationPersistenceReceipt,
    ValidationPersistenceReplay,
    ValidationPersistenceStatus,
    build_validation_persistence_receipt,
    build_validation_persistence_replay,
)
from intraday_scanner.storage.opportunity_validation_errors import (
    OpportunityValidationConflictError,
    OpportunityValidationIntegrityError,
    OpportunityValidationReadOnlyError,
    OpportunityValidationStoreError,
)
from intraday_scanner.storage.opportunity_validation_rows import (
    validation_receipt_row,
    validation_session_row,
)
from intraday_scanner.storage.opportunity_validation_schema import (
    RECEIPT_INSERT_ORDER,
    SESSION_INSERT_ORDER,
    validate_validation_schema,
)
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.v2.opportunity.miss_contracts import (
    require_identity,
    require_sanitized,
)
from intraday_scanner.v2.opportunity.validation_audit import (
    ChronologicalValidationPreparationReceipt,
)
from intraday_scanner.v2.opportunity.validation_contracts import HoldoutAccessEvidence
from intraday_scanner.v2.opportunity.validation_metric_report import (
    ValidationTradingMetricReport,
)
from intraday_scanner.v2.opportunity.validation_robustness_report import (
    ValidationRobustnessReport,
)


class OpportunityValidationStore:
    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self._db_path = Path(db_path)
        self._read_only = read_only

    def initialize(self) -> None:
        if self._read_only:
            raise OpportunityValidationReadOnlyError(
                "read-only validation store cannot initialize"
            )
        connection = self._connect_writable(require_existing=False)
        try:
            run_migrations(connection)
            connection.execute("PRAGMA foreign_keys=ON")
            validate_validation_schema(connection)
            connection.commit()
        except OpportunityValidationStoreError:
            connection.rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise OpportunityValidationIntegrityError(
                f"could not initialize validation store: {exc}"
            ) from exc
        finally:
            connection.close()

    def append(
        self,
        preparation: ChronologicalValidationPreparationReceipt,
        metric_report: ValidationTradingMetricReport,
        robustness_report: ValidationRobustnessReport,
        holdout_access_evidence: HoldoutAccessEvidence,
        *,
        code_identity: str,
        code_content_hash_sha256: str,
        persisted_at,
        consume_locked_oos: bool = False,
        status: ValidationPersistenceStatus | None = None,
    ) -> ValidationPersistenceReceipt:
        if self._read_only:
            raise OpportunityValidationReadOnlyError(
                "read-only validation store cannot append"
            )
        if status is not None and consume_locked_oos:
            raise OpportunityValidationConflictError(
                "choose either explicit validation status or locked-OOS consumption"
            )
        requested_status = status or (
            ValidationPersistenceStatus.LOCKED_OOS_CONSUMED
            if consume_locked_oos
            else ValidationPersistenceStatus.RESEARCH_EVIDENCE
        )
        try:
            candidate = build_validation_persistence_receipt(
                preparation,
                metric_report,
                robustness_report,
                holdout_access_evidence,
                code_identity=code_identity,
                code_content_hash_sha256=code_content_hash_sha256,
                persisted_at=persisted_at,
                status=requested_status,
            )
        except (TypeError, ValueError) as exc:
            raise OpportunityValidationConflictError(
                f"invalid validation append request: {exc}"
            ) from exc
        connection = self._connect_writable(require_existing=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            validate_validation_schema(connection)
            existing = connection.execute(
                "SELECT validation_receipt_id FROM opportunity_validation_receipts "
                "WHERE validation_receipt_id=?",
                (candidate.validation_receipt_id,),
            ).fetchone()
            if existing is not None:
                if _stored_projection_matches(
                    connection,
                    candidate,
                    preparation,
                    metric_report,
                    robustness_report,
                    holdout_access_evidence,
                ):
                    connection.rollback()
                    return candidate
                replay = _verify_stored_validation(
                    connection, candidate.validation_receipt_id
                )
                if (
                    replay.persistence_receipt != candidate
                    or replay.preparation != preparation
                    or replay.metric_report != metric_report
                    or replay.robustness_report != robustness_report
                    or replay.holdout_access_evidence != holdout_access_evidence
                ):
                    raise OpportunityValidationConflictError(
                        "stored validation identity conflicts with requested content"
                    )
                connection.rollback()
                return replay.persistence_receipt
            if requested_status is ValidationPersistenceStatus.LOCKED_OOS_CONSUMED:
                consumed = connection.execute(
                    "SELECT validation_receipt_id FROM opportunity_validation_receipts "
                    "WHERE status='locked_oos_consumed' AND ("
                    "semantic_lock_key=? OR lock_authority_key=? "
                    "OR holdout_inventory_key=?)",
                    (
                        candidate.semantic_lock_key,
                        candidate.lock_authority_key,
                        candidate.holdout_inventory_key,
                    ),
                ).fetchone()
                if consumed is not None:
                    _verify_consumed_lock_receipt(
                        connection,
                        str(consumed[0]),
                    )
                    raise OpportunityValidationConflictError(
                        "locked OOS semantic key was already consumed"
                    )
            self._insert_receipt(
                connection,
                candidate,
                preparation,
                metric_report,
                robustness_report,
                holdout_access_evidence,
            )
            self._insert_sessions(connection, candidate)
            replay = _verify_stored_validation(
                connection, candidate.validation_receipt_id
            )
            if (
                replay.persistence_receipt != candidate
                or replay.preparation != preparation
                or replay.metric_report != metric_report
                or replay.robustness_report != robustness_report
                or replay.holdout_access_evidence != holdout_access_evidence
            ):
                raise OpportunityValidationIntegrityError(
                    "post-insert validation verification failed"
                )
            connection.commit()
            return candidate
        except OpportunityValidationStoreError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OpportunityValidationConflictError(
                f"validation append violates immutable constraints: {exc}"
            ) from exc
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise OpportunityValidationIntegrityError(
                f"could not append validation evidence: {exc}"
            ) from exc
        finally:
            connection.close()

    def append_evidence(self, *args, **kwargs) -> ValidationPersistenceReceipt:
        return self.append(*args, **kwargs)

    def consume_locked_oos(self, *args, **kwargs) -> ValidationPersistenceReceipt:
        kwargs["consume_locked_oos"] = True
        return self.append(*args, **kwargs)

    def load_receipt(
        self, validation_receipt_id: str
    ) -> ValidationPersistenceReceipt | None:
        replay = self.replay(validation_receipt_id)
        return replay.persistence_receipt if replay is not None else None

    def load_evidence(
        self, validation_receipt_id: str
    ) -> ValidationPersistenceReplay | None:
        return self.replay(validation_receipt_id)

    def replay(
        self, validation_receipt_id: str
    ) -> ValidationPersistenceReplay | None:
        _validate_lookup_identity(validation_receipt_id)
        connection = self._connect_read()
        try:
            connection.execute("BEGIN")
            validate_validation_schema(connection)
            row = connection.execute(
                "SELECT 1 FROM opportunity_validation_receipts "
                "WHERE validation_receipt_id=?",
                (validation_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            return _verify_stored_validation(connection, validation_receipt_id)
        except OpportunityValidationStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityValidationIntegrityError(
                f"could not replay validation evidence: {exc}"
            ) from exc
        finally:
            connection.rollback()
            connection.close()

    def _connect_writable(self, *, require_existing: bool) -> sqlite3.Connection:
        if require_existing and not self._db_path.is_file():
            raise OpportunityValidationIntegrityError("validation database is absent")
        try:
            connection = sqlite3.connect(self._db_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.Error as exc:
            raise OpportunityValidationIntegrityError(
                "could not open validation database"
            ) from exc

    def _connect_read(self) -> sqlite3.Connection:
        try:
            connection = connect_read_only(self._db_path, row_factory=sqlite3.Row)
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except (StorageError, sqlite3.Error) as exc:
            raise OpportunityValidationIntegrityError(
                f"could not open read-only validation database: {exc}"
            ) from exc

    def _insert_receipt(
        self,
        connection: sqlite3.Connection,
        receipt: ValidationPersistenceReceipt,
        preparation: ChronologicalValidationPreparationReceipt,
        metric_report: ValidationTradingMetricReport,
        robustness_report: ValidationRobustnessReport,
        holdout_access_evidence: HoldoutAccessEvidence,
    ) -> None:
        values = validation_receipt_row(
            receipt,
            preparation,
            metric_report,
            robustness_report,
            holdout_access_evidence,
        )
        connection.execute(
            f"INSERT INTO opportunity_validation_receipts "  # nosec B608 -- fixed table and immutable module-owned columns; values remain bound
            f"({','.join(RECEIPT_INSERT_ORDER)}) VALUES "
            f"({','.join('?' for _ in RECEIPT_INSERT_ORDER)})",
            values,
        )

    def _insert_sessions(
        self,
        connection: sqlite3.Connection,
        receipt: ValidationPersistenceReceipt,
    ) -> None:
        sql = (
            "INSERT INTO opportunity_validation_oos_sessions "
            f"({','.join(SESSION_INSERT_ORDER)}) VALUES "  # nosec B608 -- immutable module-owned columns; values remain bound
            f"({','.join('?' for _ in SESSION_INSERT_ORDER)})"
        )
        for session in receipt.oos_sessions:
            connection.execute(
                sql,
                validation_session_row(receipt.validation_receipt_id, session),
            )


def _verify_stored_validation(
    connection: sqlite3.Connection,
    validation_receipt_id: str,
) -> ValidationPersistenceReplay:
    row = connection.execute(
        "SELECT * FROM opportunity_validation_receipts "
        "WHERE validation_receipt_id=?",
        (validation_receipt_id,),
    ).fetchone()
    if row is None:
        raise OpportunityValidationIntegrityError(
            "persisted validation receipt is missing"
        )
    try:
        receipt = ValidationPersistenceReceipt.from_json(str(row["receipt_json"]))
        preparation = ChronologicalValidationPreparationReceipt.from_json(
            str(row["preparation_json"])
        )
        metric_report = ValidationTradingMetricReport.from_json(
            str(row["metric_report_json"])
        )
        robustness_report = ValidationRobustnessReport.from_json(
            str(row["robustness_report_json"])
        )
        holdout = HoldoutAccessEvidence.from_json(str(row["holdout_access_json"]))
        expected = build_validation_persistence_receipt(
            preparation,
            metric_report,
            robustness_report,
            holdout,
            code_identity=str(row["code_identity"]),
            code_content_hash_sha256=str(row["code_content_hash_sha256"]),
            persisted_at=receipt.persisted_at,
            status=receipt.status,
        )
    except (TypeError, ValueError) as exc:
        raise OpportunityValidationIntegrityError(
            "persisted validation JSON is invalid"
        ) from exc
    if receipt != expected:
        raise OpportunityValidationIntegrityError(
            "persisted validation receipt does not recompute"
        )
    expected_row = validation_receipt_row(
        receipt,
        preparation,
        metric_report,
        robustness_report,
        holdout,
    )
    if tuple(row[name] for name in RECEIPT_INSERT_ORDER) != expected_row:
        raise OpportunityValidationIntegrityError(
            "persisted validation receipt projection does not reconcile"
        )
    stored_sessions = connection.execute(
        "SELECT * FROM opportunity_validation_oos_sessions "
        "WHERE validation_receipt_id=? ORDER BY session_ordinal",
        (validation_receipt_id,),
    ).fetchall()
    if len(stored_sessions) != len(receipt.oos_sessions):
        raise OpportunityValidationIntegrityError(
            "persisted locked OOS inventory count is invalid"
        )
    for stored, session in zip(stored_sessions, receipt.oos_sessions, strict=True):
        if tuple(stored[name] for name in SESSION_INSERT_ORDER) != validation_session_row(
            receipt.validation_receipt_id, session
        ):
            raise OpportunityValidationIntegrityError(
                "persisted locked OOS inventory does not reconcile"
            )
    try:
        return build_validation_persistence_replay(
            receipt,
            preparation,
            metric_report,
            robustness_report,
            holdout,
        )
    except (TypeError, ValueError) as exc:
        raise OpportunityValidationIntegrityError(
            "persisted validation replay is invalid"
        ) from exc


def _stored_projection_matches(
    connection: sqlite3.Connection,
    receipt: ValidationPersistenceReceipt,
    preparation: ChronologicalValidationPreparationReceipt,
    metric_report: ValidationTradingMetricReport,
    robustness_report: ValidationRobustnessReport,
    holdout_access_evidence: HoldoutAccessEvidence,
) -> bool:
    row = connection.execute(
        "SELECT * FROM opportunity_validation_receipts "
        "WHERE validation_receipt_id=?",
        (receipt.validation_receipt_id,),
    ).fetchone()
    if row is None:
        return False
    expected_row = validation_receipt_row(
        receipt,
        preparation,
        metric_report,
        robustness_report,
        holdout_access_evidence,
    )
    if tuple(row[name] for name in RECEIPT_INSERT_ORDER) != expected_row:
        return False
    sessions = connection.execute(
        "SELECT * FROM opportunity_validation_oos_sessions "
        "WHERE validation_receipt_id=? ORDER BY session_ordinal",
        (receipt.validation_receipt_id,),
    ).fetchall()
    expected_sessions = tuple(
        validation_session_row(receipt.validation_receipt_id, item)
        for item in receipt.oos_sessions
    )
    return tuple(
        tuple(row[name] for name in SESSION_INSERT_ORDER) for row in sessions
    ) == expected_sessions


def _verify_consumed_lock_receipt(
    connection: sqlite3.Connection,
    validation_receipt_id: str,
) -> None:
    row = connection.execute(
        "SELECT validation_receipt_id,receipt_content_hash_sha256,"
        "semantic_lock_key,lock_authority_key,holdout_inventory_key,"
        "status,receipt_json "
        "FROM opportunity_validation_receipts WHERE validation_receipt_id=?",
        (validation_receipt_id,),
    ).fetchone()
    if row is None:
        raise OpportunityValidationIntegrityError(
            "consumed locked OOS receipt is missing"
        )
    try:
        receipt = ValidationPersistenceReceipt.from_json(str(row["receipt_json"]))
    except (TypeError, ValueError) as exc:
        raise OpportunityValidationIntegrityError(
            "consumed locked OOS receipt JSON is invalid"
        ) from exc
    if (
        receipt.validation_receipt_id != row["validation_receipt_id"]
        or receipt.content_hash() != row["receipt_content_hash_sha256"]
        or receipt.semantic_lock_key != row["semantic_lock_key"]
        or receipt.lock_authority_key != row["lock_authority_key"]
        or receipt.holdout_inventory_key != row["holdout_inventory_key"]
        or receipt.status is not ValidationPersistenceStatus.LOCKED_OOS_CONSUMED
        or row["status"] != ValidationPersistenceStatus.LOCKED_OOS_CONSUMED.value
    ):
        raise OpportunityValidationIntegrityError(
            "consumed locked OOS receipt does not reconcile"
        )


def _validate_lookup_identity(value: str) -> None:
    try:
        require_identity(value, "validation_receipt_id")
        require_sanitized(value, "validation_receipt_id")
    except (TypeError, ValueError) as exc:
        raise OpportunityValidationIntegrityError(
            "invalid validation receipt lookup identity"
        ) from exc


__all__ = [
    "OpportunityValidationConflictError",
    "OpportunityValidationIntegrityError",
    "OpportunityValidationReadOnlyError",
    "OpportunityValidationStore",
    "OpportunityValidationStoreError",
    "ValidationPersistenceReceipt",
    "ValidationPersistenceReplay",
    "ValidationPersistenceStatus",
]
