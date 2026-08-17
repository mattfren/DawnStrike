"""Append-only SQLite adapter for stored missed-opportunity revisions."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.opportunity_miss_errors import (
    OpportunityMissConflictError,
    OpportunityMissIntegrityError,
    OpportunityMissReadOnlyError,
    OpportunityMissStaleParentError,
    OpportunityMissStoreError,
)
from intraday_scanner.storage.opportunity_miss_inventory import (
    build_miss_inventory,
    build_miss_receipt,
    miss_inventory_hash,
)
from intraday_scanner.storage.opportunity_miss_schema import (
    BINDING_INSERT_ORDER,
    RECEIPT_INSERT_ORDER,
    RECORD_INSERT_ORDER,
    validate_miss_schema,
)
from intraday_scanner.storage.opportunity_outcome_errors import (
    OpportunityOutcomeStoreError,
)
from intraday_scanner.storage.opportunity_outcome_store import (
    _audit_run_chain as _audit_outcome_chain,
)
from intraday_scanner.storage.opportunity_outcome_store import (
    _load_chain_prefix as _load_outcome_prefix,
)
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.miss_contracts import (
    require_identity,
)
from intraday_scanner.v2.opportunity.miss_persistence import (
    CurrentMissReplay,
    HistoricalMissReplay,
    MissPersistenceKind,
    OpportunityMissPersistenceReceipt,
    validate_miss_receipt_batch,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import (
    MissReconciliationBatch,
)
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcome_persistence import CurrentOutcomeReplay


class OpportunityMissStore:
    """Dedicated inert adapter for immutable miss analysis chains."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self._db_path = Path(db_path)
        self._read_only = read_only

    def initialize(self) -> None:
        """Explicitly create/migrate a writable disposable database to schema 30."""

        if self._read_only:
            raise OpportunityMissReadOnlyError("read-only miss store cannot initialize")
        connection = self._connect_writable(require_existing=False)
        try:
            run_migrations(connection)
            connection.execute("PRAGMA foreign_keys = ON")
            validate_miss_schema(connection)
            connection.commit()
        except OpportunityMissStoreError:
            connection.rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise OpportunityMissIntegrityError(
                f"could not initialize miss store: {exc}"
            ) from exc
        finally:
            connection.close()

    def append_batch(
        self,
        batch: MissReconciliationBatch,
        *,
        persisted_at: datetime,
        supersedes_miss_receipt_id: str | None = None,
    ) -> OpportunityMissPersistenceReceipt:
        """Append an initial/correction revision or return an exact idempotent receipt."""

        if self._read_only:
            raise OpportunityMissReadOnlyError("read-only miss store cannot append")
        _validate_persisted_at(persisted_at, batch.recorded_at)
        if supersedes_miss_receipt_id is not None:
            require_identity(supersedes_miss_receipt_id, "supersedes_miss_receipt_id")
        inventory = build_miss_inventory(batch)
        candidate = build_miss_receipt(
            batch,
            persisted_at=persisted_at,
            inventory=inventory,
            predecessor=None,
        )
        connection = self._connect_writable(require_existing=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            validate_miss_schema(connection)
            existing = connection.execute(
                "SELECT miss_receipt_id, analysis_key FROM opportunity_miss_receipts "
                "WHERE batch_id = ?",
                (batch.batch_id,),
            ).fetchone()
            if existing is not None:
                chain = _audit_analysis_chain(connection, str(existing["analysis_key"]))
                receipt, stored_batch = _chain_item(
                    chain,
                    str(existing["miss_receipt_id"]),
                )
                if (
                    stored_batch != batch
                    or receipt.analysis_key != candidate.analysis_key
                    or receipt.supersedes_miss_receipt_id
                    != supersedes_miss_receipt_id
                    or persisted_at < receipt.persisted_at
                ):
                    raise OpportunityMissConflictError(
                        "stored miss batch identity conflicts with requested append"
                    )
                connection.rollback()
                return receipt

            chain = _audit_analysis_chain(connection, candidate.analysis_key)
            predecessor: OpportunityMissPersistenceReceipt | None = None
            predecessor_batch: MissReconciliationBatch | None = None
            if chain:
                predecessor, predecessor_batch = chain[-1]
                if supersedes_miss_receipt_id != predecessor.miss_receipt_id:
                    raise OpportunityMissConflictError(
                        "miss correction must explicitly supersede the current head"
                    )
                if (
                    persisted_at <= predecessor.persisted_at
                    or batch.recorded_at <= predecessor_batch.recorded_at
                ):
                    raise OpportunityMissConflictError(
                        "miss correction chronology must strictly advance"
                    )
            elif supersedes_miss_receipt_id is not None:
                raise OpportunityMissConflictError(
                    "initial miss append cannot declare a predecessor"
                )
            receipt = build_miss_receipt(
                batch,
                persisted_at=persisted_at,
                inventory=inventory,
                predecessor=predecessor,
            )
            if receipt.analysis_key != candidate.analysis_key:
                raise OpportunityMissConflictError(
                    "miss correction changed its stable analysis scope"
                )
            _verify_current_parents(connection, batch)
            self._insert_receipt(connection, receipt, batch)
            self._insert_records(connection, receipt, batch)
            self._insert_bindings(connection, receipt, batch)
            verified = _audit_analysis_chain(connection, receipt.analysis_key)
            if not verified or verified[-1] != (receipt, batch):
                raise OpportunityMissIntegrityError(
                    "post-insert miss chain verification failed"
                )
            _verify_current_parents(connection, batch)
            connection.commit()
            return receipt
        except OpportunityMissStoreError:
            connection.rollback()
            raise
        except OpportunityOutcomeStoreError as exc:
            connection.rollback()
            raise OpportunityMissIntegrityError(
                f"stored miss parent outcome is invalid: {exc}"
            ) from exc
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OpportunityMissConflictError(
                f"miss append violates immutable storage constraints: {exc}"
            ) from exc
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise OpportunityMissIntegrityError(f"could not append miss batch: {exc}") from exc
        finally:
            connection.close()

    def load_receipt(
        self,
        miss_receipt_id: str,
    ) -> OpportunityMissPersistenceReceipt | None:
        require_identity(miss_receipt_id, "miss_receipt_id")
        connection = self._connect_read()
        try:
            validate_miss_schema(connection)
            row = connection.execute(
                "SELECT analysis_key FROM opportunity_miss_receipts "
                "WHERE miss_receipt_id = ?",
                (miss_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            chain = _audit_analysis_chain(connection, str(row["analysis_key"]))
            return _chain_item(chain, miss_receipt_id)[0]
        except OpportunityMissStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityMissIntegrityError(f"could not load miss receipt: {exc}") from exc
        finally:
            connection.close()

    def load_batch(self, miss_receipt_id: str) -> MissReconciliationBatch | None:
        require_identity(miss_receipt_id, "miss_receipt_id")
        connection = self._connect_read()
        try:
            validate_miss_schema(connection)
            row = connection.execute(
                "SELECT analysis_key FROM opportunity_miss_receipts "
                "WHERE miss_receipt_id = ?",
                (miss_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            chain = _audit_analysis_chain(connection, str(row["analysis_key"]))
            return _chain_item(chain, miss_receipt_id)[1]
        except OpportunityMissStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityMissIntegrityError(f"could not load miss batch: {exc}") from exc
        finally:
            connection.close()

    def replay_historical(self, miss_receipt_id: str) -> HistoricalMissReplay | None:
        require_identity(miss_receipt_id, "miss_receipt_id")
        connection = self._connect_read()
        try:
            validate_miss_schema(connection)
            row = connection.execute(
                "SELECT analysis_key FROM opportunity_miss_receipts "
                "WHERE miss_receipt_id = ?",
                (miss_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            chain = _audit_analysis_chain(connection, str(row["analysis_key"]))
            index = _chain_index(chain, miss_receipt_id)
            prefix = chain[: index + 1]
            receipt, batch = prefix[-1]
            values: dict[str, Any] = {
                "miss_persistence_receipt": receipt,
                "miss_batch": batch,
                "chain_prefix_receipts": tuple(item[0] for item in prefix),
                "chain_prefix_batches": tuple(item[1] for item in prefix),
                "parent_outcome_replays": batch.session_replay.current_outcome_replays,
                "research_only": True,
                "promotion_eligible": False,
                "schema_version": "v2.opportunity.historical_miss_replay.v1",
            }
            return HistoricalMissReplay(
                replay_id=stable_identity("historical-opportunity-miss-replay", values),
                **values,
            )
        except OpportunityMissStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityMissIntegrityError(
                f"could not replay historical miss analysis: {exc}"
            ) from exc
        finally:
            connection.close()

    def replay_current(self, analysis_key: str) -> CurrentMissReplay | None:
        require_identity(analysis_key, "analysis_key")
        connection = self._connect_read()
        try:
            validate_miss_schema(connection)
            chain = _audit_analysis_chain(connection, analysis_key)
            if not chain:
                return None
            receipt, batch = chain[-1]
            current_parents = _verify_current_parents(connection, batch)
            values: dict[str, Any] = {
                "miss_persistence_receipt": receipt,
                "miss_batch": batch,
                "full_chain_receipts": tuple(item[0] for item in chain),
                "full_chain_batches": tuple(item[1] for item in chain),
                "current_parent_outcome_replays": current_parents,
                "research_only": True,
                "promotion_eligible": False,
                "schema_version": "v2.opportunity.current_miss_replay.v1",
            }
            return CurrentMissReplay(
                replay_id=stable_identity("current-opportunity-miss-replay", values),
                **values,
            )
        except OpportunityMissStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityMissIntegrityError(
                f"could not replay current miss analysis: {exc}"
            ) from exc
        finally:
            connection.close()

    def _connect_writable(self, *, require_existing: bool) -> sqlite3.Connection:
        if require_existing and not self._db_path.is_file():
            raise OpportunityMissIntegrityError(
                "miss database is absent; call initialize explicitly"
            )
        try:
            connection = sqlite3.connect(self._db_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except sqlite3.Error as exc:
            raise OpportunityMissIntegrityError(
                f"could not open writable miss database: {exc}"
            ) from exc

    def _connect_read(self) -> sqlite3.Connection:
        try:
            connection = connect_read_only(self._db_path, row_factory=sqlite3.Row)
            connection.execute("PRAGMA foreign_keys = ON")
            return connection
        except (StorageError, sqlite3.Error) as exc:
            raise OpportunityMissIntegrityError(
                f"could not open read-only miss database: {exc}"
            ) from exc

    def _insert_receipt(
        self,
        connection: sqlite3.Connection,
        receipt: OpportunityMissPersistenceReceipt,
        batch: MissReconciliationBatch,
    ) -> None:
        values = _receipt_row(receipt, batch)
        placeholders = ",".join("?" for _ in RECEIPT_INSERT_ORDER)
        connection.execute(
            f"INSERT INTO opportunity_miss_receipts "  # nosec B608 -- fixed table and immutable module-owned columns; values remain bound
            f"({','.join(RECEIPT_INSERT_ORDER)}) VALUES ({placeholders})",
            values,
        )

    def _insert_records(
        self,
        connection: sqlite3.Connection,
        receipt: OpportunityMissPersistenceReceipt,
        batch: MissReconciliationBatch,
    ) -> None:
        placeholders = ",".join("?" for _ in RECORD_INSERT_ORDER)
        sql = (
            f"INSERT INTO opportunity_miss_records "  # nosec B608 -- fixed table and immutable module-owned columns; values remain bound
            f"({','.join(RECORD_INSERT_ORDER)}) VALUES ({placeholders})"
        )
        first_times = _first_record_times(connection, receipt.analysis_key)
        for ordinal, record in enumerate(batch.records):
            key = record.opportunity.session_opportunity_key
            connection.execute(
                sql,
                _record_row(
                    receipt,
                    ordinal,
                    record,
                    first_persisted_at=first_times.get(key, receipt.persisted_at),
                ),
            )

    def _insert_bindings(
        self,
        connection: sqlite3.Connection,
        receipt: OpportunityMissPersistenceReceipt,
        batch: MissReconciliationBatch,
    ) -> None:
        placeholders = ",".join("?" for _ in BINDING_INSERT_ORDER)
        sql = (
            f"INSERT INTO opportunity_miss_run_bindings "  # nosec B608 -- fixed table and immutable module-owned columns; values remain bound
            f"({','.join(BINDING_INSERT_ORDER)}) VALUES ({placeholders})"
        )
        for ordinal, binding in enumerate(batch.session_replay.run_inventory.bindings):
            connection.execute(sql, _binding_row(receipt, ordinal, binding))


def _verify_stored_receipt(
    connection: sqlite3.Connection,
    miss_receipt_id: str,
) -> tuple[OpportunityMissPersistenceReceipt, MissReconciliationBatch]:
    row = connection.execute(
        "SELECT * FROM opportunity_miss_receipts WHERE miss_receipt_id = ?",
        (miss_receipt_id,),
    ).fetchone()
    if row is None:
        raise OpportunityMissIntegrityError("persisted miss receipt is missing")
    try:
        receipt = OpportunityMissPersistenceReceipt.from_json(str(row["receipt_json"]))
        batch = MissReconciliationBatch.from_json(str(row["batch_json"]))
        validate_miss_receipt_batch(receipt, batch)
    except (TypeError, ValueError) as exc:
        raise OpportunityMissIntegrityError(
            "persisted miss receipt or batch JSON is invalid"
        ) from exc
    predecessor: OpportunityMissPersistenceReceipt | None = None
    if receipt.supersedes_miss_receipt_id is not None:
        predecessor_row = connection.execute(
            "SELECT receipt_json FROM opportunity_miss_receipts WHERE miss_receipt_id = ?",
            (receipt.supersedes_miss_receipt_id,),
        ).fetchone()
        if predecessor_row is None:
            raise OpportunityMissIntegrityError("persisted miss predecessor is missing")
        try:
            predecessor = OpportunityMissPersistenceReceipt.from_json(
                str(predecessor_row["receipt_json"])
            )
        except (TypeError, ValueError) as exc:
            raise OpportunityMissIntegrityError(
                "persisted miss predecessor JSON is invalid"
            ) from exc
    inventory = build_miss_inventory(batch)
    try:
        expected_receipt = build_miss_receipt(
            batch,
            persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
            inventory=inventory,
            predecessor=predecessor,
        )
    except (TypeError, ValueError) as exc:
        raise OpportunityMissIntegrityError(
            "persisted miss receipt reconstruction is invalid"
        ) from exc
    if receipt != expected_receipt or tuple(row[name] for name in RECEIPT_INSERT_ORDER) != (
        _receipt_row(receipt, batch)
    ):
        raise OpportunityMissIntegrityError(
            "persisted miss receipt metadata or content does not reconcile"
        )
    stored_records = connection.execute(
        "SELECT * FROM opportunity_miss_records WHERE miss_receipt_id = ? "
        "ORDER BY record_ordinal",
        (miss_receipt_id,),
    ).fetchall()
    if len(stored_records) != len(batch.records):
        raise OpportunityMissIntegrityError("persisted miss record count does not reconcile")
    first_times = _first_record_times(connection, receipt.analysis_key)
    for ordinal, (stored, record) in enumerate(zip(stored_records, batch.records, strict=True)):
        key = record.opportunity.session_opportunity_key
        if tuple(stored[name] for name in RECORD_INSERT_ORDER) != _record_row(
            receipt,
            ordinal,
            record,
            first_persisted_at=first_times[key],
        ):
            raise OpportunityMissIntegrityError(
                "persisted miss record inventory does not reconcile"
            )
    bindings = batch.session_replay.run_inventory.bindings
    stored_bindings = connection.execute(
        "SELECT * FROM opportunity_miss_run_bindings WHERE miss_receipt_id = ? "
        "ORDER BY binding_ordinal",
        (miss_receipt_id,),
    ).fetchall()
    if len(stored_bindings) != len(bindings):
        raise OpportunityMissIntegrityError(
            "persisted miss run binding count does not reconcile"
        )
    for ordinal, (stored, binding) in enumerate(
        zip(stored_bindings, bindings, strict=True)
    ):
        if tuple(stored[name] for name in BINDING_INSERT_ORDER) != _binding_row(
            receipt, ordinal, binding
        ):
            raise OpportunityMissIntegrityError(
                "persisted miss run binding inventory does not reconcile"
            )
    if receipt.artifact_inventory_hash_sha256 != miss_inventory_hash(inventory):
        raise OpportunityMissIntegrityError("persisted miss inventory hash is invalid")
    _verify_historical_parents(connection, batch)
    return receipt, batch


def _audit_analysis_chain(
    connection: sqlite3.Connection,
    analysis_key: str,
) -> tuple[tuple[OpportunityMissPersistenceReceipt, MissReconciliationBatch], ...]:
    rows = connection.execute(
        "SELECT miss_receipt_id, supersedes_miss_receipt_id "
        "FROM opportunity_miss_receipts WHERE analysis_key = ?",
        (analysis_key,),
    ).fetchall()
    if not rows:
        return ()
    ids = {str(row["miss_receipt_id"]) for row in rows}
    roots = [
        str(row["miss_receipt_id"])
        for row in rows
        if row["supersedes_miss_receipt_id"] is None
    ]
    if len(roots) != 1:
        raise OpportunityMissIntegrityError(
            "nonempty miss history requires exactly one root"
        )
    successors: dict[str, str] = {}
    for row in rows:
        predecessor = row["supersedes_miss_receipt_id"]
        if predecessor is None:
            continue
        predecessor_id = str(predecessor)
        if predecessor_id not in ids or predecessor_id in successors:
            raise OpportunityMissIntegrityError("miss history contains an orphan or fork")
        successors[predecessor_id] = str(row["miss_receipt_id"])
    ordered: list[str] = []
    current: str | None = roots[0]
    while current is not None:
        if current in ordered:
            raise OpportunityMissIntegrityError("miss history contains a cycle")
        ordered.append(current)
        current = successors.get(current)
    if set(ordered) != ids:
        raise OpportunityMissIntegrityError("miss history contains a disconnected component")
    chain = tuple(_verify_stored_receipt(connection, item) for item in ordered)
    for index, (receipt, batch) in enumerate(chain):
        if index == 0:
            if receipt.receipt_kind is not MissPersistenceKind.INITIAL:
                raise OpportunityMissIntegrityError("miss history root is not initial")
            continue
        previous_receipt, previous_batch = chain[index - 1]
        if (
            receipt.receipt_kind is not MissPersistenceKind.CORRECTION
            or receipt.supersedes_miss_receipt_id != previous_receipt.miss_receipt_id
            or receipt.supersedes_miss_receipt_content_hash_sha256
            != previous_receipt.content_hash()
            or receipt.persisted_at <= previous_receipt.persisted_at
            or batch.recorded_at <= previous_batch.recorded_at
        ):
            raise OpportunityMissIntegrityError(
                "persisted miss correction lineage or chronology is invalid"
            )
    return chain


def _verify_historical_parents(
    connection: sqlite3.Connection,
    batch: MissReconciliationBatch,
) -> None:
    for embedded in batch.session_replay.current_outcome_replays:
        try:
            _audit_outcome_chain(connection, embedded.pipeline_result.run_id)
            prefix = _load_outcome_prefix(
                connection,
                embedded.outcome_persistence_receipt.outcome_receipt_id,
            )
            expected = _build_current_outcome_replay(prefix)
        except OpportunityOutcomeStoreError as exc:
            raise OpportunityMissIntegrityError(
                f"persisted historical outcome parent is invalid: {exc}"
            ) from exc
        if embedded != expected:
            raise OpportunityMissIntegrityError(
                "persisted miss batch does not match its historical outcome parent"
            )


def _verify_current_parents(
    connection: sqlite3.Connection,
    batch: MissReconciliationBatch,
) -> tuple[CurrentOutcomeReplay, ...]:
    current: list[CurrentOutcomeReplay] = []
    for embedded in batch.session_replay.current_outcome_replays:
        try:
            prefix = _audit_outcome_chain(connection, embedded.pipeline_result.run_id)
        except OpportunityOutcomeStoreError as exc:
            raise OpportunityMissIntegrityError(
                f"stored current outcome parent is invalid: {exc}"
            ) from exc
        if not prefix:
            raise OpportunityMissIntegrityError("stored current outcome parent is missing")
        expected = _build_current_outcome_replay(prefix)
        if embedded != expected:
            raise OpportunityMissStaleParentError(
                "miss batch outcome parent is not the current stored head"
            )
        current.append(expected)
    return tuple(current)


def _build_current_outcome_replay(prefix) -> CurrentOutcomeReplay:
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
        replay_id=stable_identity("current-opportunity-outcome-replay", values),
        **values,
    )


def _receipt_row(
    receipt: OpportunityMissPersistenceReceipt,
    batch: MissReconciliationBatch,
) -> tuple[object, ...]:
    return (
        receipt.miss_receipt_id,
        receipt.content_hash(),
        receipt.receipt_kind.value,
        receipt.analysis_key,
        receipt.batch_id,
        receipt.batch_content_hash_sha256,
        receipt.batch_schema_version,
        batch.to_json(),
        receipt.exchange_session_id,
        receipt.session_open_at.isoformat(),
        receipt.session_close_at.isoformat(),
        receipt.membership_as_of_at.isoformat(),
        receipt.requested_query_start_at.isoformat(),
        receipt.requested_through_at.isoformat(),
        contract_to_json(receipt.requested_symbols),
        len(receipt.requested_symbols),
        int(receipt.empty_eligible_universe),
        receipt.authority_claim.value,
        receipt.source_scope_status.value,
        receipt.inventory_status.value,
        receipt.qualification_policy_id,
        receipt.qualification_policy_content_hash_sha256,
        receipt.qualification_batch_id,
        receipt.qualification_batch_content_hash_sha256,
        receipt.session_replay_id,
        receipt.session_replay_content_hash_sha256,
        receipt.session_disposition.value,
        receipt.batch_recorded_at.isoformat(),
        receipt.persisted_at.isoformat(),
        receipt.supersedes_miss_receipt_id,
        receipt.supersedes_miss_receipt_content_hash_sha256,
        receipt.record_count,
        receipt.run_binding_count,
        receipt.artifact_count,
        receipt.artifact_inventory_hash_sha256,
        receipt.schema_version,
        receipt.to_json(),
        int(receipt.research_only),
        int(receipt.promotion_eligible),
        receipt.database_schema_version,
    )


def _record_row(
    receipt,
    ordinal,
    record,
    *,
    first_persisted_at: datetime,
) -> tuple[object, ...]:
    return (
        receipt.miss_receipt_id,
        ordinal,
        receipt.analysis_key,
        record.opportunity.session_opportunity_key,
        record.opportunity.symbol,
        record.opportunity.direction.value,
        record.opportunity.horizon_id,
        record.opportunity_id,
        record.opportunity_content_hash_sha256,
        record.miss_record_id,
        record.content_hash(),
        record.schema_version,
        record.to_json(),
        record.disposition.value,
        record.category.value if record.category is not None else None,
        first_persisted_at.isoformat(),
    )


def _binding_row(receipt, ordinal, binding) -> tuple[object, ...]:
    return (
        receipt.miss_receipt_id,
        ordinal,
        binding.binding_id,
        binding.content_hash(),
        binding.schema_version,
        binding.to_json(),
        binding.run_id,
        binding.run_content_hash_sha256,
        binding.run_persistence_receipt_id,
        binding.run_persistence_receipt_content_hash_sha256,
        binding.outcome_replay_id,
        binding.outcome_replay_content_hash_sha256,
        binding.outcome_head_receipt_id,
        binding.outcome_head_receipt_content_hash_sha256,
        binding.decision_at.isoformat(),
    )


def _first_record_times(
    connection: sqlite3.Connection,
    analysis_key: str,
) -> dict[str, datetime]:
    rows = connection.execute(
        "SELECT record.session_opportunity_key, MIN(parent.persisted_at) AS first_at "
        "FROM opportunity_miss_records AS record "
        "JOIN opportunity_miss_receipts AS parent "
        "ON parent.miss_receipt_id = record.miss_receipt_id "
        "WHERE parent.analysis_key = ? GROUP BY record.session_opportunity_key",
        (analysis_key,),
    ).fetchall()
    try:
        return {
            str(row["session_opportunity_key"]): datetime.fromisoformat(str(row["first_at"]))
            for row in rows
        }
    except (TypeError, ValueError) as exc:
        raise OpportunityMissIntegrityError(
            "persisted miss record chronology is malformed"
        ) from exc


def _chain_index(chain, miss_receipt_id: str) -> int:
    for index, (receipt, _batch) in enumerate(chain):
        if receipt.miss_receipt_id == miss_receipt_id:
            return index
    raise OpportunityMissIntegrityError("requested miss receipt is outside its analysis chain")


def _chain_item(chain, miss_receipt_id: str):
    return chain[_chain_index(chain, miss_receipt_id)]


def _validate_persisted_at(persisted_at: datetime, recorded_at: datetime) -> None:
    offset = persisted_at.utcoffset()
    if persisted_at.tzinfo is None or offset is None:
        raise ValueError("persisted_at must be timezone-aware")
    if offset.total_seconds() != 0:
        raise ValueError("persisted_at must be UTC")
    if persisted_at < recorded_at:
        raise ValueError("persisted_at cannot precede miss batch recorded_at")


__all__ = [
    "OpportunityMissConflictError",
    "OpportunityMissIntegrityError",
    "OpportunityMissReadOnlyError",
    "OpportunityMissStaleParentError",
    "OpportunityMissStore",
    "OpportunityMissStoreError",
]
