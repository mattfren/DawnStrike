from __future__ import annotations

import ast
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from intraday_scanner.storage.opportunity_miss_inventory import (
    build_miss_inventory,
    build_miss_receipt,
)
from intraday_scanner.storage.opportunity_miss_schema import validate_miss_schema
from intraday_scanner.storage.opportunity_miss_store import (
    OpportunityMissConflictError,
    OpportunityMissIntegrityError,
    OpportunityMissReadOnlyError,
    OpportunityMissStaleParentError,
    OpportunityMissStore,
)
from intraday_scanner.storage.opportunity_outcome_store import OpportunityOutcomeStore
from intraday_scanner.storage.opportunity_store import OpportunityStore
from intraday_scanner.v2.opportunity.capabilities import CapabilityState
from intraday_scanner.v2.opportunity.miss_persistence import (
    CurrentMissReplay,
    HistoricalMissReplay,
    OpportunityMissPersistenceReceipt,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import (
    MissReconciliationBatch,
    reconcile_missed_opportunities,
)
from intraday_scanner.v2.opportunity.miss_replay import (
    build_session_replay,
    build_session_run_inventory,
)
from tests.test_opportunity_missed import (
    FETCHED_AT,
    SESSION_CLOSE,
    SESSION_ID,
    SESSION_OPEN,
    _qualified_batch,
    _session_replay,
    _stored_replay_for_batch,
)
from tests.test_opportunity_outcomes import _batch as _outcome_batch


def _batch(*, empty_inventory: bool = False) -> MissReconciliationBatch:
    if not empty_inventory:
        replay = _session_replay(
            current_outcome_replays=(
                _stored_replay_for_batch(_outcome_batch(missing_symbol="ABC")),
            )
        )
    else:
        inventory = build_session_run_inventory(
            exchange_session_id=SESSION_ID,
            session_open_at=SESSION_OPEN,
            session_close_at=SESSION_CLOSE,
            current_outcome_replays=(),
            source_identity="fixture_session_run_inventory",
            source_version="v1",
            method="stored_current_replay_query",
            capability_state=CapabilityState.AVAILABLE,
            authoritative=True,
            scope_complete=True,
            query_started_at=SESSION_OPEN,
            query_ended_at=SESSION_CLOSE,
            observed_through_at=SESSION_CLOSE,
            fetched_at=FETCHED_AT,
        )
        replay = build_session_replay(inventory, current_outcome_replays=())
    return reconcile_missed_opportunities(_qualified_batch(), session_replay=replay)


def _persist_parents(database: Path, batch: MissReconciliationBatch) -> None:
    for parent in batch.session_replay.current_outcome_replays:
        run_receipt = OpportunityStore(database).append_run(
            parent.pipeline_result,
            recorded_at=parent.run_persistence_receipt.recorded_at,
        )
        assert run_receipt == parent.run_persistence_receipt
        outcome_receipt = OpportunityOutcomeStore(database).append_batch(
            parent.outcome_batch,
            persisted_at=parent.outcome_persistence_receipt.persisted_at,
        )
        assert outcome_receipt == parent.outcome_persistence_receipt


@pytest.fixture(scope="module")
def parent_template(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("miss-parent") / "parents.sqlite"
    store = OpportunityMissStore(database)
    store.initialize()
    batch = _batch()
    _persist_parents(database, batch)
    return database, batch


@pytest.fixture(scope="module")
def persisted_template(parent_template, tmp_path_factory: pytest.TempPathFactory):
    parent_database, batch = parent_template
    database = tmp_path_factory.mktemp("miss-persistence") / "template.sqlite"
    shutil.copy2(parent_database, database)
    store = OpportunityMissStore(database)
    receipt = store.append_batch(
        batch,
        persisted_at=batch.recorded_at + timedelta(seconds=1),
    )
    return database, batch, receipt


def _copy_template(persisted_template, tmp_path: Path):
    source, batch, receipt = persisted_template
    target = tmp_path / "copy.sqlite"
    shutil.copy2(source, target)
    return target, batch, receipt, OpportunityMissStore(target)


def _mutate_with_canonical_guard(
    database: Path,
    *,
    table: str,
    event: str,
    statement: str,
) -> None:
    trigger = f"{table}_no_{event}"
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        saved = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
            (trigger,),
        ).fetchone()
        assert saved is not None
        connection.execute(f"DROP TRIGGER {trigger}")
        connection.execute(statement)
        connection.execute(str(saved["sql"]))
        connection.commit()
        connection.execute("PRAGMA foreign_keys = ON")
        validate_miss_schema(connection)


def _assert_public_read_fails(
    store: OpportunityMissStore,
    receipt,
    path: str,
) -> None:
    operation = {
        "load": lambda: store.load_receipt(receipt.miss_receipt_id),
        "historical": lambda: store.replay_historical(receipt.miss_receipt_id),
        "current": lambda: store.replay_current(receipt.analysis_key),
    }[path]
    with pytest.raises(OpportunityMissIntegrityError):
        operation()


def test_receipt_contract_roundtrip_and_logical_query_scope() -> None:
    batch = _batch()
    inventory = build_miss_inventory(batch)
    receipt = build_miss_receipt(
        batch,
        persisted_at=batch.recorded_at + timedelta(seconds=1),
        inventory=inventory,
        predecessor=None,
    )

    assert receipt.requested_query_start_at == (
        batch.session_replay.run_inventory.source_receipt.query_started_at
    )
    assert all(
        receipt.requested_query_start_at <= item.decision_at.astimezone(
            receipt.requested_query_start_at.tzinfo
        )
        for item in batch.session_replay.run_inventory.bindings
    )
    assert OpportunityMissPersistenceReceipt.from_json(receipt.to_json()) == receipt

    old_inventory = batch.session_replay.run_inventory
    old_source = old_inventory.source_receipt

    def rebuilt(query_started_at, fetched_at):
        inventory_value = build_session_run_inventory(
            exchange_session_id=old_inventory.exchange_session_id,
            session_open_at=old_inventory.session_open_at,
            session_close_at=old_inventory.session_close_at,
            current_outcome_replays=batch.session_replay.current_outcome_replays,
            source_identity=old_source.source_identity,
            source_version=old_source.source_version,
            method=old_source.method,
            capability_state=old_source.capability_state,
            authoritative=old_source.authoritative,
            scope_complete=old_source.scope_complete,
            query_started_at=query_started_at,
            query_ended_at=old_source.query_ended_at,
            observed_through_at=old_source.observed_through_at,
            fetched_at=fetched_at,
            limitations=old_source.limitations,
        )
        replay = build_session_replay(
            inventory_value,
            current_outcome_replays=batch.session_replay.current_outcome_replays,
        )
        return reconcile_missed_opportunities(
            batch.qualification_batch,
            session_replay=replay,
        )

    later_batch = rebuilt(old_source.query_started_at, old_source.fetched_at + timedelta(seconds=1))
    later_receipt = build_miss_receipt(
        later_batch,
        persisted_at=later_batch.recorded_at + timedelta(seconds=1),
        inventory=build_miss_inventory(later_batch),
        predecessor=None,
    )
    assert later_receipt.analysis_key == receipt.analysis_key

    earlier_batch = rebuilt(
        old_source.query_started_at - timedelta(seconds=1),
        old_source.fetched_at,
    )
    earlier_receipt = build_miss_receipt(
        earlier_batch,
        persisted_at=earlier_batch.recorded_at + timedelta(seconds=1),
        inventory=build_miss_inventory(earlier_batch),
        predecessor=None,
    )
    assert earlier_receipt.analysis_key != receipt.analysis_key


def test_initial_append_load_replay_and_idempotent_first_time(persisted_template) -> None:
    database, batch, receipt = persisted_template
    store = OpportunityMissStore(database)
    assert store.load_receipt(receipt.miss_receipt_id) == receipt
    assert store.load_batch(receipt.miss_receipt_id) == batch
    historical = store.replay_historical(receipt.miss_receipt_id)
    current = store.replay_current(receipt.analysis_key)
    assert isinstance(historical, HistoricalMissReplay)
    assert isinstance(current, CurrentMissReplay)
    assert HistoricalMissReplay.from_json(historical.to_json()) == historical
    assert CurrentMissReplay.from_json(current.to_json()) == current

    replayed = store.append_batch(
        batch,
        persisted_at=receipt.persisted_at + timedelta(hours=1),
    )
    assert replayed == receipt
    assert replayed.persisted_at == receipt.persisted_at
    with pytest.raises(OpportunityMissConflictError):
        store.append_batch(
            batch,
            persisted_at=receipt.persisted_at - timedelta(microseconds=1),
        )


def test_empty_authoritative_run_inventory_is_preserved(tmp_path: Path) -> None:
    database = tmp_path / "empty.sqlite"
    store = OpportunityMissStore(database)
    store.initialize()
    batch = _batch(empty_inventory=True)
    receipt = store.append_batch(
        batch,
        persisted_at=batch.recorded_at + timedelta(seconds=1),
    )
    assert receipt.run_binding_count == 0
    assert store.load_batch(receipt.miss_receipt_id) == batch
    current = store.replay_current(receipt.analysis_key)
    assert current is not None
    assert current.current_parent_outcome_replays == ()


def test_append_rejects_absent_parents_and_rolls_back_private_insert_fault(
    parent_template,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch = _batch()
    absent_database = tmp_path / "absent-parents.sqlite"
    absent_store = OpportunityMissStore(absent_database)
    absent_store.initialize()
    with pytest.raises(OpportunityMissIntegrityError, match="parent"):
        absent_store.append_batch(
            batch,
            persisted_at=batch.recorded_at + timedelta(seconds=1),
        )

    parent_database, parent_batch = parent_template
    database = tmp_path / "rollback.sqlite"
    shutil.copy2(parent_database, database)
    store = OpportunityMissStore(database)

    def fail_insert(*_args, **_kwargs):
        raise sqlite3.OperationalError("fixture insertion fault")

    monkeypatch.setattr(store, "_insert_bindings", fail_insert)
    with pytest.raises(OpportunityMissIntegrityError, match="insertion fault"):
        store.append_batch(
            parent_batch,
            persisted_at=parent_batch.recorded_at + timedelta(seconds=1),
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_miss_receipts"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_miss_records"
        ).fetchone()[0] == 0
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_miss_run_bindings"
        ).fetchone()[0] == 0


def _corrected_batch(database: Path, initial: MissReconciliationBatch):
    old_parent = initial.session_replay.current_outcome_replays[0]
    correction = _outcome_batch()
    outcome_receipt = OpportunityOutcomeStore(database).append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=2),
        supersedes_outcome_receipt_id=(
            old_parent.outcome_persistence_receipt.outcome_receipt_id
        ),
    )
    current = OpportunityOutcomeStore(database).replay_current(
        correction.pipeline_result.run_id
    )
    assert current is not None
    assert current.outcome_persistence_receipt == outcome_receipt
    old_inventory = initial.session_replay.run_inventory
    old_source = old_inventory.source_receipt
    inventory = build_session_run_inventory(
        exchange_session_id=old_inventory.exchange_session_id,
        session_open_at=old_inventory.session_open_at,
        session_close_at=old_inventory.session_close_at,
        current_outcome_replays=(current,),
        source_identity=old_source.source_identity,
        source_version=old_source.source_version,
        method=old_source.method,
        capability_state=old_source.capability_state,
        authoritative=old_source.authoritative,
        scope_complete=old_source.scope_complete,
        query_started_at=old_source.query_started_at,
        query_ended_at=old_source.query_ended_at,
        observed_through_at=old_source.observed_through_at,
        fetched_at=max(
            old_source.fetched_at + timedelta(seconds=1),
            outcome_receipt.persisted_at,
        ),
        limitations=old_source.limitations,
    )
    replay = build_session_replay(inventory, current_outcome_replays=(current,))
    return reconcile_missed_opportunities(
        initial.qualification_batch,
        session_replay=replay,
    )


@pytest.fixture(scope="module")
def correction_template(persisted_template, tmp_path_factory: pytest.TempPathFactory):
    source, initial, first = persisted_template
    database = tmp_path_factory.mktemp("miss-correction") / "template.sqlite"
    shutil.copy2(source, database)
    correction = _corrected_batch(database, initial)
    store = OpportunityMissStore(database)
    second = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=1),
        supersedes_miss_receipt_id=first.miss_receipt_id,
    )
    return database, initial, first, correction, second


def test_correction_chain_historical_current_and_stale_head(
    persisted_template,
    tmp_path: Path,
) -> None:
    database, initial, first, store = _copy_template(persisted_template, tmp_path)
    correction = _corrected_batch(database, initial)
    with pytest.raises(OpportunityMissConflictError, match="current head"):
        store.append_batch(
            correction,
            persisted_at=correction.recorded_at + timedelta(seconds=1),
        )
    second = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=1),
        supersedes_miss_receipt_id=first.miss_receipt_id,
    )
    assert second.analysis_key == first.analysis_key
    assert second.supersedes_miss_receipt_id == first.miss_receipt_id
    assert store.replay_historical(first.miss_receipt_id).miss_batch == initial
    current = store.replay_current(first.analysis_key)
    assert current is not None
    assert current.miss_batch == correction
    assert len(current.full_chain_receipts) == 2

    assert store.append_batch(
        correction,
        persisted_at=second.persisted_at + timedelta(seconds=1),
        supersedes_miss_receipt_id=first.miss_receipt_id,
    ) == second


def test_current_replay_fails_when_outcome_parent_advances(
    persisted_template,
    tmp_path: Path,
) -> None:
    database, initial, first, store = _copy_template(persisted_template, tmp_path)
    correction = _corrected_batch(database, initial)
    with sqlite3.connect(database) as connection:
        before = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "opportunity_miss_receipts",
                "opportunity_miss_records",
                "opportunity_miss_run_bindings",
            )
        )
    replayed = store.append_batch(
        initial,
        persisted_at=first.persisted_at + timedelta(hours=1),
    )
    assert replayed == first
    assert replayed.persisted_at == first.persisted_at
    with sqlite3.connect(database) as connection:
        after = tuple(
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "opportunity_miss_receipts",
                "opportunity_miss_records",
                "opportunity_miss_run_bindings",
            )
        )
    assert after == before
    with pytest.raises(OpportunityMissStaleParentError):
        store.replay_current(first.analysis_key)
    historical = store.replay_historical(first.miss_receipt_id)
    assert historical is not None
    assert historical.miss_batch == initial
    second = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=1),
        supersedes_miss_receipt_id=first.miss_receipt_id,
    )
    assert store.replay_current(first.analysis_key).miss_persistence_receipt == second


def test_read_only_constructor_missing_schema_and_inert_constructor(tmp_path: Path) -> None:
    absent = tmp_path / "absent.sqlite"
    store = OpportunityMissStore(absent)
    assert not absent.exists()
    with pytest.raises(OpportunityMissIntegrityError):
        store.load_receipt("miss-receipt:missing")
    assert not absent.exists()
    with pytest.raises(OpportunityMissReadOnlyError):
        OpportunityMissStore(absent, read_only=True).initialize()

    stale = tmp_path / "stale.sqlite"
    with sqlite3.connect(stale) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (28, 'fixture')")
    with pytest.raises(OpportunityMissIntegrityError, match="schema 29"):
        OpportunityMissStore(stale).load_receipt("miss-receipt:missing")


def test_read_connection_is_query_only_and_rejects_sidecars(
    persisted_template,
    tmp_path: Path,
) -> None:
    database, _batch_value, receipt, store = _copy_template(persisted_template, tmp_path)
    connection = store._connect_read()
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute(
                "UPDATE opportunity_miss_receipts SET analysis_key='forged'"
            )
    finally:
        connection.close()
    sidecar = Path(f"{database}-wal")
    sidecar.touch()
    with pytest.raises(OpportunityMissIntegrityError, match="sidecar"):
        store.load_receipt(receipt.miss_receipt_id)


@pytest.mark.parametrize(
    ("object_type", "object_name"),
    (
        ("trigger", "opportunity_miss_receipts_no_update"),
        ("index", "idx_opportunity_miss_receipts_scope"),
        ("table", "opportunity_miss_records"),
    ),
)
def test_same_named_schema_forgery_fails_all_public_paths(
    persisted_template,
    tmp_path: Path,
    object_type: str,
    object_name: str,
) -> None:
    database, batch, receipt, store = _copy_template(persisted_template, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        if object_type == "trigger":
            connection.execute(f"DROP TRIGGER {object_name}")
            connection.execute(
                f"CREATE TRIGGER {object_name} AFTER INSERT ON "
                "opportunity_miss_receipts BEGIN SELECT 1; END"
            )
        elif object_type == "index":
            connection.execute(f"DROP INDEX {object_name}")
            connection.execute(
                f"CREATE INDEX {object_name} ON opportunity_miss_receipts(batch_id)"
            )
        else:
            connection.execute("ALTER TABLE opportunity_miss_records RENAME TO forged_records")
            connection.execute(
                "CREATE TABLE opportunity_miss_records "
                "(miss_receipt_id TEXT, record_ordinal INTEGER)"
            )
    for operation in (
        lambda: OpportunityMissStore(database).initialize(),
        lambda: store.load_receipt(receipt.miss_receipt_id),
        lambda: store.append_batch(
            batch,
            persisted_at=receipt.persisted_at + timedelta(seconds=1),
        ),
    ):
        with pytest.raises(OpportunityMissIntegrityError):
            operation()


@pytest.mark.parametrize(
    "statement",
    (
        "UPDATE opportunity_miss_receipts SET analysis_key='forged'",
        "DELETE FROM opportunity_miss_receipts",
        "UPDATE opportunity_miss_records SET symbol='ZZZ'",
        "DELETE FROM opportunity_miss_records",
        "UPDATE opportunity_miss_run_bindings SET run_id='forged'",
        "DELETE FROM opportunity_miss_run_bindings",
    ),
)
def test_all_append_only_guards_abort(persisted_template, tmp_path: Path, statement: str) -> None:
    database, _batch_value, _receipt, _store = _copy_template(persisted_template, tmp_path)
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(statement)


@pytest.mark.parametrize(
    ("table", "event", "statement", "path"),
    (
        (
            "opportunity_miss_receipts",
            "update",
            "UPDATE opportunity_miss_receipts SET artifact_inventory_hash_sha256='"
            + "0" * 64
            + "'",
            "load",
        ),
        (
            "opportunity_miss_receipts",
            "update",
            "UPDATE opportunity_miss_receipts SET receipt_json='{}'",
            "historical",
        ),
        (
            "opportunity_miss_receipts",
            "update",
            "UPDATE opportunity_miss_receipts SET batch_json='{}'",
            "current",
        ),
        (
            "opportunity_miss_records",
            "delete",
            "DELETE FROM opportunity_miss_records",
            "load",
        ),
        (
            "opportunity_miss_records",
            "update",
            "UPDATE opportunity_miss_records SET record_ordinal=1",
            "historical",
        ),
        (
            "opportunity_miss_records",
            "update",
            "UPDATE opportunity_miss_records SET miss_record_json='{}'",
            "current",
        ),
        (
            "opportunity_miss_records",
            "update",
            "UPDATE opportunity_miss_records SET miss_record_content_hash_sha256='"
            + "0" * 64
            + "'",
            "load",
        ),
        (
            "opportunity_miss_run_bindings",
            "delete",
            "DELETE FROM opportunity_miss_run_bindings",
            "historical",
        ),
        (
            "opportunity_miss_run_bindings",
            "update",
            "UPDATE opportunity_miss_run_bindings SET binding_ordinal=1",
            "current",
        ),
        (
            "opportunity_miss_run_bindings",
            "update",
            "UPDATE opportunity_miss_run_bindings SET binding_json='{}'",
            "load",
        ),
        (
            "opportunity_miss_run_bindings",
            "update",
            "UPDATE opportunity_miss_run_bindings SET binding_content_hash_sha256='"
            + "0" * 64
            + "'",
            "historical",
        ),
    ),
)
def test_raw_stored_payload_and_inventory_tamper_fails_after_schema_accepts(
    persisted_template,
    tmp_path: Path,
    table: str,
    event: str,
    statement: str,
    path: str,
) -> None:
    database, _batch_value, receipt, store = _copy_template(persisted_template, tmp_path)
    _mutate_with_canonical_guard(
        database,
        table=table,
        event=event,
        statement=statement,
    )
    _assert_public_read_fails(store, receipt, path)


def test_extra_record_and_binding_are_rejected_by_canonical_unique_keys(
    persisted_template,
    tmp_path: Path,
) -> None:
    database, _batch_value, receipt, _store = _copy_template(persisted_template, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        for table, ordinal_name in (
            ("opportunity_miss_records", "record_ordinal"),
            ("opportunity_miss_run_bindings", "binding_ordinal"),
        ):
            row = connection.execute(
                f"SELECT * FROM {table} WHERE miss_receipt_id=?",
                (receipt.miss_receipt_id,),
            ).fetchone()
            assert row is not None
            columns = tuple(row.keys())
            values = dict(row)
            values[ordinal_name] = 1
            with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
                connection.execute(
                    f"INSERT INTO {table} ({','.join(columns)}) VALUES "
                    f"({','.join('?' for _ in columns)})",
                    tuple(values[name] for name in columns),
                )


def test_two_revision_cycle_is_detected_after_canonical_schema_accepts(
    correction_template,
    tmp_path: Path,
) -> None:
    source, _initial, first, _correction, second = correction_template
    database = tmp_path / "cycle.sqlite"
    shutil.copy2(source, database)
    statement = (
        "UPDATE opportunity_miss_receipts SET receipt_kind='correction', "
        f"supersedes_miss_receipt_id='{second.miss_receipt_id}', "
        "supersedes_miss_receipt_content_hash_sha256='"
        f"{second.content_hash()}' WHERE miss_receipt_id='{first.miss_receipt_id}'"
    )
    _mutate_with_canonical_guard(
        database,
        table="opportunity_miss_receipts",
        event="update",
        statement=statement,
    )
    store = OpportunityMissStore(database)
    for path in ("load", "historical", "current"):
        _assert_public_read_fails(store, first, path)


def test_orphan_and_fork_are_structurally_unrepresentable(
    correction_template,
    tmp_path: Path,
) -> None:
    source, _initial, first, _correction, second = correction_template
    database = tmp_path / "structural-chain.sqlite"
    shutil.copy2(source, database)
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        saved = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='opportunity_miss_receipts_no_update'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER opportunity_miss_receipts_no_update")
        with pytest.raises(sqlite3.IntegrityError, match="FOREIGN KEY"):
            connection.execute(
                "UPDATE opportunity_miss_receipts SET "
                "supersedes_miss_receipt_id='missing', "
                "supersedes_miss_receipt_content_hash_sha256=? "
                "WHERE miss_receipt_id=?",
                ("0" * 64, second.miss_receipt_id),
            )
        connection.execute(str(saved))
        row = connection.execute(
            "SELECT * FROM opportunity_miss_receipts WHERE miss_receipt_id=?",
            (second.miss_receipt_id,),
        ).fetchone()
        columns = tuple(item[1] for item in connection.execute(
            "PRAGMA table_info(opportunity_miss_receipts)"
        ).fetchall())
        values = dict(zip(columns, row, strict=True))
        values["miss_receipt_id"] = "miss-receipt-fork"
        values["batch_id"] = "miss-batch-fork"
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                f"INSERT INTO opportunity_miss_receipts ({','.join(columns)}) VALUES "
                f"({','.join('?' for _ in columns)})",
                tuple(values[name] for name in columns),
            )
        connection.commit()
        validate_miss_schema(connection)


def test_receipt_and_replay_strict_deserialization_rejects_tamper(
    persisted_template,
) -> None:
    database, _batch_value, receipt = persisted_template
    current = OpportunityMissStore(database).replay_current(receipt.analysis_key)
    assert current is not None
    payload = receipt.to_dict()
    payload["unknown"] = "injected"
    with pytest.raises(ValueError):
        OpportunityMissPersistenceReceipt.from_dict(payload)
    duplicate = receipt.to_json().replace(
        '"receipt_kind":"initial"',
        '"receipt_kind":"initial","receipt_kind":"correction"',
    )
    with pytest.raises(ValueError):
        OpportunityMissPersistenceReceipt.from_json(duplicate)
    replay_payload = current.to_dict()
    replay_payload["full_chain_receipts"] = []
    with pytest.raises(ValueError):
        CurrentMissReplay.from_dict(replay_payload)


def test_import_firewall_and_ast_boundary() -> None:
    code = (
        "import sys; "
        "import intraday_scanner.v2.opportunity.pipeline; "
        "import intraday_scanner.storage.opportunity_store; "
        "assert not any('miss_persistence' in name or 'opportunity_miss' in name "
        "for name in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    forbidden = {"app", "broker", "network", "scheduler", "ui"}
    for path in (
        Path("intraday_scanner/v2/opportunity/miss_persistence.py"),
        *Path("intraday_scanner/storage").glob("opportunity_miss*.py"),
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = {
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        } | {
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        }
        assert not any(any(part in item.split(".") for part in forbidden) for item in imports)


def test_schema_validator_accepts_canonical_schema(persisted_template) -> None:
    database, _batch_value, _receipt = persisted_template
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        validate_miss_schema(connection)
