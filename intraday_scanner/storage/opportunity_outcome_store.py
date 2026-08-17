"""Append-only SQLite adapter for downstream opportunity outcome batches."""

from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.opportunity_outcome_errors import (
    OpportunityOutcomeConflictError,
    OpportunityOutcomeIntegrityError,
    OpportunityOutcomeReadOnlyError,
    OpportunityOutcomeStoreError,
)
from intraday_scanner.storage.opportunity_outcome_inventory import (
    build_outcome_inventory as _build_outcome_inventory,
)
from intraday_scanner.storage.opportunity_outcome_inventory import (
    build_outcome_receipt as _build_outcome_receipt,
)
from intraday_scanner.storage.opportunity_outcome_inventory import (
    outcome_inventory_hash as _outcome_inventory_hash,
)
from intraday_scanner.storage.opportunity_outcome_schema import (
    CURRENT_STORAGE_SCHEMA_VERSION,
)
from intraday_scanner.storage.opportunity_outcome_schema import (
    RECEIPT_INSERT_ORDER as _RECEIPT_INSERT_ORDER,
)
from intraday_scanner.storage.opportunity_outcome_schema import (
    RECORD_INSERT_ORDER as _RECORD_INSERT_ORDER,
)
from intraday_scanner.storage.opportunity_outcome_schema import (
    validate_outcome_schema as _validate_outcome_schema,
)
from intraday_scanner.storage.opportunity_store import (
    OpportunityPersistenceConflictError,
    OpportunityPersistenceReceipt,
    OpportunityStoreError,
    _verify_stored_run,
)
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import _require_identity
from intraday_scanner.v2.opportunity.outcome_persistence import (
    CurrentOutcomeReplay,
    HistoricalOutcomeReplay,
    OpportunityOutcomePersistenceReceipt,
    OutcomePersistenceKind,
)
from intraday_scanner.v2.opportunity.outcome_records import OutcomeRecord
from intraday_scanner.v2.opportunity.outcome_replay import OutcomeLabelBatch
from intraday_scanner.v2.opportunity.pipeline import PipelineResult


class OpportunityOutcomeStore:
    """Dedicated, explicit, append-only store for retrospective outcomes."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.db_path = Path(db_path)
        self.read_only = read_only

    def initialize(self) -> None:
        """Explicitly create or migrate a writable database to schema 30."""

        if self.read_only:
            raise OpportunityOutcomeReadOnlyError(
                "read-only outcome store cannot initialize"
            )
        try:
            from intraday_scanner.storage.migrations import run_migrations

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect_writable(require_existing=False) as connection:
                version = run_migrations(connection)
                if version != CURRENT_STORAGE_SCHEMA_VERSION:
                    raise OpportunityOutcomeIntegrityError(
                        f"outcome store requires schema 30, found {version}"
                    )
                _validate_outcome_schema(connection)
        except OpportunityOutcomeStoreError:
            raise
        except (TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"outcome database schema is invalid: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            raise OpportunityOutcomeStoreError(
                f"could not initialize outcome store: {exc}"
            ) from exc

    def append_batch(
        self,
        batch: OutcomeLabelBatch,
        *,
        persisted_at: datetime,
        supersedes_outcome_receipt_id: str | None = None,
    ) -> OpportunityOutcomePersistenceReceipt:
        """Append one initial or correcting batch atomically."""

        if self.read_only:
            raise OpportunityOutcomeReadOnlyError("read-only outcome store cannot append")
        if not isinstance(batch, OutcomeLabelBatch):
            raise TypeError("batch must be OutcomeLabelBatch")
        _validate_persisted_at(persisted_at, batch.recorded_at)
        if supersedes_outcome_receipt_id is not None:
            _require_identity(
                supersedes_outcome_receipt_id,
                "supersedes_outcome_receipt_id",
            )
        batch_json = batch.to_json()
        connection = self._connect_writable(require_existing=True)
        try:
            _validate_outcome_schema(connection, require_current=True)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT outcome_receipt_id FROM opportunity_outcome_receipts "
                "WHERE batch_id = ?",
                (batch.batch_id,),
            ).fetchone()
            if existing is not None:
                _audit_run_chain(connection, batch.pipeline_result.run_id)
                receipt, stored_batch = _verify_stored_receipt(
                    connection,
                    str(existing["outcome_receipt_id"]),
                )
                if (
                    stored_batch.to_json() != batch_json
                    or receipt.supersedes_outcome_receipt_id
                    != supersedes_outcome_receipt_id
                ):
                    raise OpportunityOutcomeConflictError(
                        "outcome batch identity conflicts with stored content or lineage"
                    )
                connection.rollback()
                return receipt

            _stored_result, run_receipt = _verify_parent_run(
                connection,
                batch.pipeline_result.run_id,
                expected_result=batch.pipeline_result,
                expected_result_json=batch.pipeline_result.to_json(),
                conflict_on_difference=True,
            )
            if run_receipt != batch.persistence_receipt:
                raise OpportunityOutcomeConflictError(
                    "outcome batch run receipt does not match stored run receipt"
                )
            existing_chain = _audit_run_chain(
                connection,
                batch.pipeline_result.run_id,
            )
            predecessor_receipt: OpportunityOutcomePersistenceReceipt | None = None
            predecessor_batch: OutcomeLabelBatch | None = None
            if not existing_chain:
                if supersedes_outcome_receipt_id is not None:
                    raise OpportunityOutcomeConflictError(
                        "initial outcome batch cannot name a predecessor"
                    )
            else:
                head_id = existing_chain[-1][0].outcome_receipt_id
                if supersedes_outcome_receipt_id != head_id:
                    raise OpportunityOutcomeConflictError(
                        "correction must supersede the unique current receipt head"
                    )
                predecessor_receipt, predecessor_batch = _verify_stored_receipt(
                    connection,
                    head_id,
                )
                if persisted_at <= predecessor_receipt.persisted_at:
                    raise ValueError(
                        "correction persisted_at must follow predecessor persisted_at"
                    )
                _validate_correction_batches(predecessor_batch, batch)

            predecessor_by_pair = {
                (item.evaluation_id, item.horizon_id): item
                for item in (predecessor_batch.outcomes if predecessor_batch else ())
            }
            inventory = _build_outcome_inventory(
                batch,
                predecessor_receipt=predecessor_receipt,
                predecessor_by_pair=predecessor_by_pair,
            )
            inventory_hash = _outcome_inventory_hash(inventory)
            receipt = _build_outcome_receipt(
                batch,
                persisted_at=persisted_at,
                inventory=inventory,
                inventory_hash=inventory_hash,
                predecessor=predecessor_receipt,
            )
            self._insert_receipt(
                connection,
                batch=batch,
                batch_json=batch_json,
                receipt=receipt,
            )
            self._insert_records(
                connection,
                batch=batch,
                receipt=receipt,
                predecessor_by_pair=predecessor_by_pair,
            )
            stored_receipt, stored_batch = _verify_stored_receipt(
                connection,
                receipt.outcome_receipt_id,
            )
            if stored_receipt != receipt or stored_batch != batch:
                raise OpportunityOutcomeIntegrityError(
                    "inserted outcome receipt does not reconcile"
                )
            committed_chain = _audit_run_chain(connection, receipt.run_id)
            if not committed_chain or committed_chain[-1] != (receipt, batch):
                raise OpportunityOutcomeIntegrityError(
                    "inserted outcome chain head does not reconcile"
                )
            connection.commit()
            return receipt
        except OpportunityOutcomeStoreError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OpportunityOutcomeConflictError(
                f"outcome batch conflicts with immutable history: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise OpportunityOutcomeStoreError(
                f"could not append outcome batch: {exc}"
            ) from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_receipt(
        self,
        outcome_receipt_id: str,
    ) -> OpportunityOutcomePersistenceReceipt | None:
        """Load and verify one historical receipt and its chain prefix."""

        _require_identity(outcome_receipt_id, "outcome_receipt_id")
        connection = self._connect_read()
        try:
            _validate_outcome_schema(connection)
            row = connection.execute(
                "SELECT run_id FROM opportunity_outcome_receipts "
                "WHERE outcome_receipt_id = ?",
                (outcome_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            _audit_run_chain(connection, str(row["run_id"]))
            prefix = _load_chain_prefix(connection, outcome_receipt_id)
            return prefix[-1][0]
        except OpportunityOutcomeStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"could not load outcome receipt: {exc}"
            ) from exc
        finally:
            connection.close()

    def load_batch(self, outcome_receipt_id: str) -> OutcomeLabelBatch | None:
        """Load one byte-equivalent historical batch after prefix verification."""

        _require_identity(outcome_receipt_id, "outcome_receipt_id")
        connection = self._connect_read()
        try:
            _validate_outcome_schema(connection)
            row = connection.execute(
                "SELECT run_id FROM opportunity_outcome_receipts "
                "WHERE outcome_receipt_id = ?",
                (outcome_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            _audit_run_chain(connection, str(row["run_id"]))
            return _load_chain_prefix(connection, outcome_receipt_id)[-1][1]
        except OpportunityOutcomeStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"could not load outcome batch: {exc}"
            ) from exc
        finally:
            connection.close()

    def load_current_receipt(
        self,
        run_id: str,
    ) -> OpportunityOutcomePersistenceReceipt | None:
        """Load the unique derived chain head for a run."""

        _require_identity(run_id, "run_id")
        connection = self._connect_read()
        try:
            _validate_outcome_schema(connection)
            chain = _audit_run_chain(connection, run_id)
            if not chain:
                return None
            return chain[-1][0]
        except OpportunityOutcomeStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"could not load current outcome receipt: {exc}"
            ) from exc
        finally:
            connection.close()

    def load_current_outcomes(self, run_id: str) -> tuple[OutcomeRecord, ...]:
        """Return the unique head batch's exact evaluation-horizon outcomes."""

        _require_identity(run_id, "run_id")
        connection = self._connect_read()
        try:
            _validate_outcome_schema(connection)
            chain = _audit_run_chain(connection, run_id)
            if not chain:
                return ()
            return chain[-1][1].outcomes
        except OpportunityOutcomeStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"could not load current outcomes: {exc}"
            ) from exc
        finally:
            connection.close()

    def replay_historical(self, outcome_receipt_id: str) -> HistoricalOutcomeReplay | None:
        """Purely reconstruct one receipt with only its verified chain prefix."""

        _require_identity(outcome_receipt_id, "outcome_receipt_id")
        connection = self._connect_read()
        try:
            _validate_outcome_schema(connection)
            row = connection.execute(
                "SELECT run_id FROM opportunity_outcome_receipts "
                "WHERE outcome_receipt_id = ?",
                (outcome_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            _audit_run_chain(connection, str(row["run_id"]))
            prefix = _load_chain_prefix(connection, outcome_receipt_id)
            receipt, batch = prefix[-1]
            values: dict[str, Any] = {
                "pipeline_result": batch.pipeline_result,
                "run_persistence_receipt": batch.persistence_receipt,
                "outcome_persistence_receipt": receipt,
                "outcome_batch": batch,
                "chain_prefix": tuple(item[0] for item in prefix),
                "research_only": True,
                "schema_version": "v2.opportunity.historical_outcome_replay.v1",
            }
            return HistoricalOutcomeReplay(
                replay_id=stable_identity(
                    "historical-opportunity-outcome-replay",
                    values,
                ),
                **values,
            )
        except OpportunityOutcomeStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"could not replay historical outcomes: {exc}"
            ) from exc
        finally:
            connection.close()

    def replay_current(self, run_id: str) -> CurrentOutcomeReplay | None:
        """Purely reconstruct the unique current head and its full stored chain."""

        _require_identity(run_id, "run_id")
        connection = self._connect_read()
        try:
            _validate_outcome_schema(connection)
            prefix = _audit_run_chain(connection, run_id)
            if not prefix:
                return None
            receipt, batch = prefix[-1]
            values: dict[str, Any] = {
                "pipeline_result": batch.pipeline_result,
                "run_persistence_receipt": batch.persistence_receipt,
                "outcome_persistence_receipt": receipt,
                "outcome_batch": batch,
                "full_chain": tuple(item[0] for item in prefix),
                "research_only": True,
                "schema_version": "v2.opportunity.current_outcome_replay.v1",
            }
            return CurrentOutcomeReplay(
                replay_id=stable_identity(
                    "current-opportunity-outcome-replay",
                    values,
                ),
                **values,
            )
        except OpportunityOutcomeStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"could not replay current outcomes: {exc}"
            ) from exc
        finally:
            connection.close()

    def _connect_writable(self, *, require_existing: bool) -> sqlite3.Connection:
        if require_existing and not self.db_path.is_file():
            raise OpportunityOutcomeIntegrityError(
                "outcome database is absent; call initialize explicitly"
            )
        try:
            connection = sqlite3.connect(self.db_path)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as exc:
            raise OpportunityOutcomeStoreError(
                f"could not open writable outcome database: {exc}"
            ) from exc

    def _connect_read(self) -> sqlite3.Connection:
        try:
            if self.read_only:
                connection = connect_read_only(self.db_path, row_factory=sqlite3.Row)
            else:
                if not self.db_path.is_file():
                    raise OpportunityOutcomeIntegrityError(
                        "outcome database is absent; call initialize explicitly"
                    )
                connection = sqlite3.connect(self.db_path)
                connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except OpportunityOutcomeStoreError:
            raise
        except (StorageError, sqlite3.Error) as exc:
            raise OpportunityOutcomeIntegrityError(
                f"could not open read-only outcome database: {exc}"
            ) from exc

    def _insert_receipt(
        self,
        connection: sqlite3.Connection,
        *,
        batch: OutcomeLabelBatch,
        batch_json: str,
        receipt: OpportunityOutcomePersistenceReceipt,
    ) -> None:
        connection.execute(
            """
            INSERT INTO opportunity_outcome_receipts (
                outcome_receipt_id, receipt_content_hash_sha256,
                receipt_kind, batch_id, batch_content_hash_sha256,
                batch_schema_version, batch_json, run_id,
                run_content_hash_sha256, run_persistence_receipt_id,
                run_persistence_receipt_content_hash_sha256,
                source_dataset_id, source_dataset_content_hash_sha256,
                policy_id, policy_content_hash_sha256, decision_at,
                batch_recorded_at, persisted_at,
                supersedes_outcome_receipt_id,
                supersedes_outcome_receipt_content_hash_sha256,
                record_count, artifact_count, artifact_inventory_hash_sha256,
                receipt_schema_version, receipt_json, research_only,
                database_schema_version
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                receipt.outcome_receipt_id,
                receipt.content_hash(),
                receipt.receipt_kind.value,
                receipt.batch_id,
                receipt.batch_content_hash_sha256,
                receipt.batch_schema_version,
                batch_json,
                receipt.run_id,
                receipt.run_content_hash_sha256,
                receipt.run_persistence_receipt_id,
                receipt.run_persistence_receipt_content_hash_sha256,
                receipt.source_dataset_id,
                receipt.source_dataset_content_hash_sha256,
                receipt.policy_id,
                receipt.policy_content_hash_sha256,
                receipt.decision_at.isoformat(),
                receipt.batch_recorded_at.isoformat(),
                receipt.persisted_at.isoformat(),
                receipt.supersedes_outcome_receipt_id,
                receipt.supersedes_outcome_receipt_content_hash_sha256,
                receipt.record_count,
                receipt.artifact_count,
                receipt.artifact_inventory_hash_sha256,
                receipt.schema_version,
                receipt.to_json(),
                int(receipt.research_only),
                receipt.database_schema_version,
            ),
        )

    def _insert_records(
        self,
        connection: sqlite3.Connection,
        *,
        batch: OutcomeLabelBatch,
        receipt: OpportunityOutcomePersistenceReceipt,
        predecessor_by_pair: dict[tuple[str, str], OutcomeRecord],
    ) -> None:
        rows = []
        for ordinal, record in enumerate(batch.outcomes):
            predecessor = predecessor_by_pair.get(
                (record.evaluation_id, record.horizon_id)
            )
            rows.append(
                (
                    receipt.outcome_receipt_id,
                    ordinal,
                    receipt.run_id,
                    record.evaluation_id,
                    record.horizon_id,
                    record.decision_id,
                    record.outcome_id,
                    record.content_hash(),
                    record.schema_version,
                    record.to_json(),
                    record.completeness.value,
                    record.entry_status.value,
                    record.path_status.value,
                    receipt.supersedes_outcome_receipt_id if predecessor else None,
                    predecessor.outcome_id if predecessor else None,
                    predecessor.content_hash() if predecessor else None,
                    receipt.persisted_at.isoformat(),
                )
            )
        connection.executemany(
            """
            INSERT INTO opportunity_outcome_records (
                outcome_receipt_id, record_ordinal, run_id, evaluation_id,
                horizon_id, decision_id, outcome_id,
                outcome_content_hash_sha256, outcome_schema_version,
                outcome_json, completeness, entry_status, path_status,
                supersedes_outcome_receipt_id, supersedes_outcome_id,
                supersedes_outcome_content_hash_sha256, first_persisted_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )


def _validate_correction_batches(
    predecessor: OutcomeLabelBatch,
    correction: OutcomeLabelBatch,
) -> None:
    if predecessor.pipeline_result != correction.pipeline_result:
        raise OpportunityOutcomeConflictError(
            "outcome correction must remain bound to the exact pipeline result"
        )
    prior = {
        (item.evaluation_id, item.horizon_id): item for item in predecessor.outcomes
    }
    current = {
        (item.evaluation_id, item.horizon_id): item for item in correction.outcomes
    }
    if not set(prior).issubset(current):
        raise OpportunityOutcomeConflictError(
            "outcome correction cannot drop evaluation-horizon pairs"
        )
    for pair, old_record in prior.items():
        new_record = current[pair]
        if (
            new_record.outcome_id == old_record.outcome_id
            or new_record.content_hash() == old_record.content_hash()
        ):
            raise OpportunityOutcomeConflictError(
                "outcome correction must change every overlapping outcome"
            )


def _verify_stored_receipt(
    connection: sqlite3.Connection,
    outcome_receipt_id: str,
) -> tuple[OpportunityOutcomePersistenceReceipt, OutcomeLabelBatch]:
    row = connection.execute(
        "SELECT * FROM opportunity_outcome_receipts WHERE outcome_receipt_id = ?",
        (outcome_receipt_id,),
    ).fetchone()
    if row is None:
        raise OpportunityOutcomeIntegrityError("persisted outcome receipt is missing")
    try:
        receipt = OpportunityOutcomePersistenceReceipt.from_json(
            str(row["receipt_json"])
        )
        batch = OutcomeLabelBatch.from_json(str(row["batch_json"]))
    except (TypeError, ValueError) as exc:
        raise OpportunityOutcomeIntegrityError(
            "persisted outcome receipt or batch JSON is invalid"
        ) from exc
    _stored_result, run_receipt = _verify_parent_run(
        connection,
        receipt.run_id,
        expected_result=batch.pipeline_result,
        expected_result_json=batch.pipeline_result.to_json(),
    )
    if run_receipt != batch.persistence_receipt:
        raise OpportunityOutcomeIntegrityError(
            "persisted outcome batch does not match stored run receipt"
        )
    predecessor_receipt: OpportunityOutcomePersistenceReceipt | None = None
    predecessor_by_pair: dict[tuple[str, str], OutcomeRecord] = {}
    if receipt.supersedes_outcome_receipt_id is not None:
        predecessor_row = connection.execute(
            "SELECT receipt_json, receipt_content_hash_sha256, run_id "
            "FROM opportunity_outcome_receipts WHERE outcome_receipt_id = ?",
            (receipt.supersedes_outcome_receipt_id,),
        ).fetchone()
        if predecessor_row is None:
            raise OpportunityOutcomeIntegrityError(
                "persisted outcome predecessor receipt is missing"
            )
        try:
            predecessor_receipt = OpportunityOutcomePersistenceReceipt.from_json(
                str(predecessor_row["receipt_json"])
            )
        except (TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                "persisted outcome predecessor receipt JSON is invalid"
            ) from exc
        if (
            predecessor_receipt.content_hash()
            != receipt.supersedes_outcome_receipt_content_hash_sha256
            or predecessor_row["receipt_content_hash_sha256"]
            != predecessor_receipt.content_hash()
            or predecessor_row["run_id"] != receipt.run_id
        ):
            raise OpportunityOutcomeIntegrityError(
                "persisted outcome predecessor receipt binding is invalid"
            )
        predecessor_records = connection.execute(
            "SELECT outcome_json FROM opportunity_outcome_records "
            "WHERE outcome_receipt_id = ? ORDER BY record_ordinal",
            (predecessor_receipt.outcome_receipt_id,),
        ).fetchall()
        try:
            predecessor_by_pair = {
                (item.evaluation_id, item.horizon_id): item
                for item in (
                    OutcomeRecord.from_json(str(record["outcome_json"]))
                    for record in predecessor_records
                )
            }
        except (TypeError, ValueError) as exc:
            raise OpportunityOutcomeIntegrityError(
                "persisted outcome predecessor record JSON is invalid"
            ) from exc
    try:
        inventory = _build_outcome_inventory(
            batch,
            predecessor_receipt=predecessor_receipt,
            predecessor_by_pair=predecessor_by_pair,
        )
        expected_receipt = _build_outcome_receipt(
            batch,
            persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
            inventory=inventory,
            inventory_hash=_outcome_inventory_hash(inventory),
            predecessor=predecessor_receipt,
        )
    except (TypeError, ValueError) as exc:
        raise OpportunityOutcomeIntegrityError(
            "persisted outcome receipt reconstruction is invalid"
        ) from exc
    expected_row = (
        receipt.outcome_receipt_id,
        receipt.content_hash(),
        receipt.receipt_kind.value,
        receipt.batch_id,
        receipt.batch_content_hash_sha256,
        receipt.batch_schema_version,
        batch.to_json(),
        receipt.run_id,
        receipt.run_content_hash_sha256,
        receipt.run_persistence_receipt_id,
        receipt.run_persistence_receipt_content_hash_sha256,
        receipt.source_dataset_id,
        receipt.source_dataset_content_hash_sha256,
        receipt.policy_id,
        receipt.policy_content_hash_sha256,
        receipt.decision_at.isoformat(),
        receipt.batch_recorded_at.isoformat(),
        receipt.persisted_at.isoformat(),
        receipt.supersedes_outcome_receipt_id,
        receipt.supersedes_outcome_receipt_content_hash_sha256,
        receipt.record_count,
        receipt.artifact_count,
        receipt.artifact_inventory_hash_sha256,
        receipt.schema_version,
        receipt.to_json(),
        int(receipt.research_only),
        receipt.database_schema_version,
    )
    actual_row = tuple(row[name] for name in _RECEIPT_INSERT_ORDER)
    if receipt != expected_receipt or actual_row != expected_row:
        raise OpportunityOutcomeIntegrityError(
            "persisted outcome receipt metadata or content does not reconcile"
        )
    stored_records = connection.execute(
        "SELECT * FROM opportunity_outcome_records WHERE outcome_receipt_id = ? "
        "ORDER BY record_ordinal",
        (outcome_receipt_id,),
    ).fetchall()
    if len(stored_records) != len(batch.outcomes):
        raise OpportunityOutcomeIntegrityError(
            "persisted outcome record count does not reconcile"
        )
    for ordinal, (stored, record) in enumerate(
        zip(stored_records, batch.outcomes, strict=True)
    ):
        predecessor = predecessor_by_pair.get(
            (record.evaluation_id, record.horizon_id)
        )
        expected = (
            outcome_receipt_id,
            ordinal,
            receipt.run_id,
            record.evaluation_id,
            record.horizon_id,
            record.decision_id,
            record.outcome_id,
            record.content_hash(),
            record.schema_version,
            record.to_json(),
            record.completeness.value,
            record.entry_status.value,
            record.path_status.value,
            receipt.supersedes_outcome_receipt_id if predecessor else None,
            predecessor.outcome_id if predecessor else None,
            predecessor.content_hash() if predecessor else None,
            receipt.persisted_at.isoformat(),
        )
        actual = tuple(stored[name] for name in _RECORD_INSERT_ORDER)
        if actual != expected:
            raise OpportunityOutcomeIntegrityError(
                "persisted outcome record inventory does not reconcile"
            )
    return receipt, batch


def _verify_parent_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    expected_result: PipelineResult | None = None,
    expected_result_json: str | None = None,
    conflict_on_difference: bool = False,
) -> tuple[PipelineResult, OpportunityPersistenceReceipt]:
    try:
        return _verify_stored_run(
            connection,
            run_id,
            expected_result=expected_result,
            expected_result_json=expected_result_json,
            conflict_on_difference=conflict_on_difference,
        )
    except OpportunityPersistenceConflictError as exc:
        raise OpportunityOutcomeConflictError(
            f"stored parent opportunity run conflicts: {exc}"
        ) from exc
    except OpportunityStoreError as exc:
        raise OpportunityOutcomeIntegrityError(
            f"stored parent opportunity run is invalid: {exc}"
        ) from exc


def _load_chain_prefix(
    connection: sqlite3.Connection,
    outcome_receipt_id: str,
) -> tuple[tuple[OpportunityOutcomePersistenceReceipt, OutcomeLabelBatch], ...]:
    reversed_ids: list[str] = []
    seen: set[str] = set()
    current_id: str | None = outcome_receipt_id
    while current_id is not None:
        if current_id in seen:
            raise OpportunityOutcomeIntegrityError("outcome receipt chain contains a cycle")
        seen.add(current_id)
        row = connection.execute(
            "SELECT supersedes_outcome_receipt_id FROM opportunity_outcome_receipts "
            "WHERE outcome_receipt_id = ?",
            (current_id,),
        ).fetchone()
        if row is None:
            raise OpportunityOutcomeIntegrityError("outcome receipt chain contains an orphan")
        reversed_ids.append(current_id)
        current_id = row["supersedes_outcome_receipt_id"]
    ids = tuple(reversed(reversed_ids))
    prefix = tuple(_verify_stored_receipt(connection, item) for item in ids)
    for index, (receipt, batch) in enumerate(prefix):
        if index == 0:
            if receipt.receipt_kind is not OutcomePersistenceKind.INITIAL:
                raise OpportunityOutcomeIntegrityError(
                    "outcome receipt chain does not begin with an initial receipt"
                )
            continue
        previous_receipt, previous_batch = prefix[index - 1]
        if (
            receipt.receipt_kind is not OutcomePersistenceKind.CORRECTION
            or receipt.supersedes_outcome_receipt_id
            != previous_receipt.outcome_receipt_id
            or receipt.supersedes_outcome_receipt_content_hash_sha256
            != previous_receipt.content_hash()
            or receipt.persisted_at <= previous_receipt.persisted_at
        ):
            raise OpportunityOutcomeIntegrityError(
                "outcome receipt chain chronology or lineage is invalid"
            )
        try:
            _validate_correction_batches(previous_batch, batch)
        except OpportunityOutcomeConflictError as exc:
            raise OpportunityOutcomeIntegrityError(
                "persisted outcome correction chain is invalid"
            ) from exc
    return prefix


def _head_rows(connection: sqlite3.Connection, run_id: str) -> list[sqlite3.Row]:
    return list(
        connection.execute(
            """
            SELECT parent.outcome_receipt_id
            FROM opportunity_outcome_receipts AS parent
            LEFT JOIN opportunity_outcome_receipts AS child
              ON child.supersedes_outcome_receipt_id = parent.outcome_receipt_id
            WHERE parent.run_id = ? AND child.outcome_receipt_id IS NULL
            ORDER BY parent.persisted_at, parent.outcome_receipt_id
            """,
            (run_id,),
        ).fetchall()
    )


def _audit_run_chain(
    connection: sqlite3.Connection,
    run_id: str,
) -> tuple[tuple[OpportunityOutcomePersistenceReceipt, OutcomeLabelBatch], ...]:
    rows = connection.execute(
        "SELECT outcome_receipt_id, supersedes_outcome_receipt_id "
        "FROM opportunity_outcome_receipts WHERE run_id = ?",
        (run_id,),
    ).fetchall()
    if not rows:
        return ()
    ids = {str(row["outcome_receipt_id"]) for row in rows}
    roots = [
        str(row["outcome_receipt_id"])
        for row in rows
        if row["supersedes_outcome_receipt_id"] is None
    ]
    if len(roots) != 1:
        raise OpportunityOutcomeIntegrityError(
            "nonempty outcome receipt history requires exactly one root"
        )
    successors: dict[str, str] = {}
    for row in rows:
        predecessor = row["supersedes_outcome_receipt_id"]
        if predecessor is None:
            continue
        predecessor_id = str(predecessor)
        if predecessor_id not in ids or predecessor_id in successors:
            raise OpportunityOutcomeIntegrityError(
                "outcome receipt history contains an orphan or fork"
            )
        successors[predecessor_id] = str(row["outcome_receipt_id"])
    ordered_ids: list[str] = []
    seen: set[str] = set()
    current: str | None = roots[0]
    while current is not None:
        if current in seen:
            raise OpportunityOutcomeIntegrityError(
                "outcome receipt history contains a cycle"
            )
        seen.add(current)
        ordered_ids.append(current)
        current = successors.get(current)
    if seen != ids:
        raise OpportunityOutcomeIntegrityError(
            "outcome receipt history contains a disconnected component"
        )
    heads = _head_rows(connection, run_id)
    if len(heads) != 1 or str(heads[0]["outcome_receipt_id"]) != ordered_ids[-1]:
        raise OpportunityOutcomeIntegrityError(
            "nonempty outcome receipt history requires exactly one head"
        )
    return _load_chain_prefix(connection, ordered_ids[-1])


def _validate_persisted_at(persisted_at: datetime, batch_recorded_at: datetime) -> None:
    if not isinstance(persisted_at, datetime):
        raise TypeError("persisted_at must be datetime")
    if persisted_at.tzinfo is None or persisted_at.utcoffset() != timedelta(0):
        raise ValueError("persisted_at must use timezone-aware UTC")
    if persisted_at < batch_recorded_at:
        raise ValueError("persisted_at cannot precede batch recorded_at")
