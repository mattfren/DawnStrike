from __future__ import annotations

import ast
import shutil
import sqlite3
import subprocess
import sys
from collections import Counter
from dataclasses import fields
from datetime import timedelta
from pathlib import Path
from time import monotonic

import pytest

import intraday_scanner.storage.opportunity_metric_store as metric_store_module
from intraday_scanner.storage.opportunity_metric_inventory import build_metric_receipt
from intraday_scanner.storage.opportunity_metric_schema import validate_metric_schema
from intraday_scanner.storage.opportunity_metric_store import (
    OpportunityMetricConflictError,
    OpportunityMetricIntegrityError,
    OpportunityMetricReadOnlyError,
    OpportunityMetricStaleParentError,
    OpportunityMetricStore,
)
from intraday_scanner.storage.opportunity_metric_verification import (
    _MetricVerificationContext,
)
from intraday_scanner.storage.opportunity_miss_store import OpportunityMissStore
from intraday_scanner.v2.opportunity.miss_metric_persistence import (
    CurrentMultiMetricReplay,
    CurrentSessionMetricReplay,
    HistoricalMetricReplay,
    MetricArtifactFamily,
    MetricSessionReportBinding,
    OpportunityMetricPersistenceReceipt,
    validate_binding_set,
)
from intraday_scanner.v2.opportunity.miss_metric_reconciliation import (
    reconcile_discovery_metrics,
    reconcile_session_discovery_metrics,
)
from intraday_scanner.v2.opportunity.models import stable_identity
from tests.test_opportunity_discovery_metrics import _metric_policy
from tests.test_opportunity_miss_persistence import (
    _batch,
    _corrected_batch,
    _persist_parents,
)


@pytest.fixture(scope="module")
def metric_template(tmp_path_factory: pytest.TempPathFactory):
    database = tmp_path_factory.mktemp("metric-persistence") / "template.sqlite"
    miss_store = OpportunityMissStore(database)
    miss_store.initialize()
    batch = _batch()
    _persist_parents(database, batch)
    miss_receipt = miss_store.append_batch(
        batch,
        persisted_at=batch.recorded_at + timedelta(seconds=1),
    )
    current_miss = miss_store.replay_current(miss_receipt.analysis_key)
    assert current_miss is not None
    policy = _metric_policy()
    session_report = reconcile_session_discovery_metrics(batch, policy=policy)
    metric_store = OpportunityMetricStore(database)
    session_receipt = metric_store.append_session(
        session_report,
        current_miss_replay=current_miss,
        persisted_at=session_report.recorded_at + timedelta(seconds=2),
    )
    current_session = metric_store.replay_current(session_receipt.scope_key)
    assert isinstance(current_session, CurrentSessionMetricReplay)
    multi_report = reconcile_discovery_metrics((batch,), policy=policy)
    multi_receipt = metric_store.append_multi(
        multi_report,
        current_session_metric_replays=(current_session,),
        persisted_at=multi_report.recorded_at + timedelta(seconds=3),
    )
    current_multi = metric_store.replay_current(multi_receipt.scope_key)
    assert isinstance(current_multi, CurrentMultiMetricReplay)
    return (
        database,
        batch,
        current_miss,
        session_report,
        session_receipt,
        current_session,
        multi_report,
        multi_receipt,
        current_multi,
    )


def _copy_metric_template(metric_template, tmp_path: Path):
    database = tmp_path / "metric.sqlite"
    shutil.copy2(metric_template[0], database)
    return database, OpportunityMetricStore(database)


@pytest.fixture(scope="module")
def metric_miss_correction_template(metric_template, tmp_path_factory):
    database = tmp_path_factory.mktemp("metric-miss-correction") / "template.sqlite"
    shutil.copy2(metric_template[0], database)
    initial_batch = metric_template[1]
    correction_batch = _corrected_batch(database, initial_batch)
    miss_store = OpportunityMissStore(database)
    first_miss = metric_template[2].miss_persistence_receipt
    second_miss = miss_store.append_batch(
        correction_batch,
        persisted_at=correction_batch.recorded_at + timedelta(seconds=3),
        supersedes_miss_receipt_id=first_miss.miss_receipt_id,
    )
    current_miss = miss_store.replay_current(second_miss.analysis_key)
    assert current_miss is not None
    return database, correction_batch, current_miss


@pytest.fixture(scope="module")
def metric_session_correction_template(
    metric_template, metric_miss_correction_template, tmp_path_factory
):
    database = tmp_path_factory.mktemp("metric-session-correction") / "template.sqlite"
    shutil.copy2(metric_miss_correction_template[0], database)
    correction_batch = metric_miss_correction_template[1]
    current_miss = metric_miss_correction_template[2]
    corrected_session_report = reconcile_session_discovery_metrics(
        correction_batch,
        policy=_metric_policy(),
    )
    metric_store = OpportunityMetricStore(database)
    second_session = metric_store.append_session(
        corrected_session_report,
        current_miss_replay=current_miss,
        persisted_at=corrected_session_report.recorded_at + timedelta(seconds=4),
        supersedes_metric_receipt_id=metric_template[4].metric_receipt_id,
    )
    current_session = metric_store.replay_current(second_session.scope_key)
    assert isinstance(current_session, CurrentSessionMetricReplay)
    corrected_multi_report = reconcile_discovery_metrics(
        (correction_batch,),
        policy=_metric_policy(),
    )
    return (
        database,
        correction_batch,
        current_miss,
        corrected_session_report,
        second_session,
        current_session,
        corrected_multi_report,
    )


def _copy_database(source: Path, target: Path) -> OpportunityMetricStore:
    shutil.copy2(source, target)
    return OpportunityMetricStore(target)


def _guard_sql(connection: sqlite3.Connection, trigger_name: str) -> str:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",
        (trigger_name,),
    ).fetchone()
    assert row is not None and row[0]
    return str(row[0])


def _apply_guarded_raw_mutation(
    database: Path,
    *,
    trigger_names: tuple[str, ...],
    mutate,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        trigger_sql = {
            name: _guard_sql(connection, name) for name in trigger_names
        }
        for name in trigger_names:
            connection.execute(f"DROP TRIGGER {name}")
        mutate(connection)
        for sql in trigger_sql.values():
            connection.execute(sql)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        validate_metric_schema(connection)


def _assert_ddl_rejects_without_mutation(
    database: Path,
    *,
    trigger_name: str,
    mutate,
) -> None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        trigger_sql = _guard_sql(connection, trigger_name)
        before = {
            table: tuple(
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            )
            for table in (
                "opportunity_metric_receipts",
                "opportunity_metric_session_bindings",
            )
        }
        try:
            connection.execute(f"DROP TRIGGER {trigger_name}")
            with pytest.raises(sqlite3.IntegrityError):
                mutate(connection)
        finally:
            connection.rollback()
            exists = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type='trigger' AND name=?",
                (trigger_name,),
            ).fetchone()
            if exists is None:
                connection.execute(trigger_sql)
            connection.commit()
        after = {
            table: tuple(
                tuple(row)
                for row in connection.execute(
                    f"SELECT * FROM {table} ORDER BY rowid"
                ).fetchall()
            )
            for table in before
        }
        assert after == before
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        validate_metric_schema(connection)


def _metric_row_counts(database: Path) -> tuple[int, int]:
    with sqlite3.connect(database) as connection:
        return (
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunity_metric_receipts"
                ).fetchone()[0]
            ),
            int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunity_metric_session_bindings"
                ).fetchone()[0]
            ),
        )


def _insert_mapping(connection: sqlite3.Connection, table: str, values) -> None:
    names = tuple(values.keys())
    connection.execute(
        f"INSERT INTO {table} ({','.join(names)}) "
        f"VALUES ({','.join('?' for _ in names)})",
        tuple(values[name] for name in names),
    )


def _reidentify_binding(binding: MetricSessionReportBinding, **changes):
    values = {item.name: getattr(binding, item.name) for item in fields(binding)}
    values.update(changes)
    values["binding_id"] = stable_identity(
        "metric-session-report-binding",
        {key: value for key, value in values.items() if key != "binding_id"},
    )
    return MetricSessionReportBinding(**values)


def _reidentify_replay(replay, namespace: str, **changes):
    values = {item.name: getattr(replay, item.name) for item in fields(replay)}
    values.update(changes)
    values["replay_id"] = stable_identity(
        namespace,
        {key: value for key, value in values.items() if key != "replay_id"},
    )
    return type(replay)(**values)


def _reidentify_contract(contract, identity_field: str, namespace: str, **changes):
    values = {item.name: getattr(contract, item.name) for item in fields(contract)}
    values.update(changes)
    values[identity_field] = stable_identity(
        namespace,
        {key: value for key, value in values.items() if key != identity_field},
    )
    return type(contract)(**values)


def test_session_and_multi_round_trip_and_exact_family_inventory(metric_template):
    (
        _database,
        _batch_body,
        _miss,
        session_report,
        session_receipt,
        current_session,
        multi_report,
        multi_receipt,
        current_multi,
    ) = metric_template
    assert tuple(item.family for item in session_receipt.family_counts) == tuple(
        MetricArtifactFamily
    )
    assert tuple(item.count for item in session_receipt.family_counts) == (1, 0, 0)
    assert tuple(item.count for item in multi_receipt.family_counts) == (0, 1, 1)
    assert session_receipt.metric_value_count == multi_receipt.metric_value_count == 9
    assert OpportunityMetricPersistenceReceipt.from_json(session_receipt.to_json()) == (
        session_receipt
    )
    assert CurrentSessionMetricReplay.from_json(current_session.to_json()) == current_session
    assert CurrentMultiMetricReplay.from_json(current_multi.to_json()) == current_multi
    assert current_session.metric_report == session_report
    assert current_multi.metric_report == multi_report
    binding = current_multi.session_bindings[0]
    assert binding.parent_report_id == multi_report.report_id
    assert binding.parent_metric_scope_key == multi_receipt.scope_key
    assert binding.child_metric_scope_key == session_receipt.scope_key


def test_load_historical_and_idempotent_retry(metric_template, tmp_path: Path):
    database, store = _copy_metric_template(metric_template, tmp_path)
    session_report = metric_template[3]
    session_receipt = metric_template[4]
    current_miss = metric_template[2]
    assert store.load_receipt(session_receipt.metric_receipt_id) == session_receipt
    assert store.load_report(session_receipt.metric_receipt_id) == session_report
    historical = store.replay_historical(session_receipt.metric_receipt_id)
    assert isinstance(historical, HistoricalMetricReplay)
    assert HistoricalMetricReplay.from_json(historical.to_json()) == historical
    retried = store.append_session(
        session_report,
        current_miss_replay=current_miss,
        persisted_at=session_receipt.persisted_at + timedelta(seconds=10),
    )
    assert retried == session_receipt
    with pytest.raises(OpportunityMetricConflictError):
        store.append_session(
            session_report,
            current_miss_replay=current_miss,
            persisted_at=session_receipt.persisted_at - timedelta(microseconds=1),
        )
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_metric_receipts"
        ).fetchone()[0] == 2


def test_session_historical_retry_after_miss_advance_is_no_write_and_current_stale(
    metric_template, metric_miss_correction_template, tmp_path: Path
):
    database = tmp_path / "metric.sqlite"
    store = _copy_database(metric_miss_correction_template[0], database)
    first = metric_template[4]
    before = _metric_row_counts(database)
    for requested_at in (first.persisted_at, first.persisted_at + timedelta(seconds=30)):
        assert store.append_session(
            metric_template[3],
            current_miss_replay=metric_template[2],
            persisted_at=requested_at,
        ) == first
        assert _metric_row_counts(database) == before
    with pytest.raises(OpportunityMetricConflictError):
        store.append_session(
            metric_template[3],
            current_miss_replay=metric_template[2],
            persisted_at=first.persisted_at - timedelta(microseconds=1),
        )
    historical = store.replay_historical(first.metric_receipt_id)
    assert historical is not None
    assert historical.metric_persistence_receipt == first
    with pytest.raises(OpportunityMetricStaleParentError):
        store.replay_current(first.scope_key)


def test_existing_session_rejects_different_parent_lineage(metric_template, tmp_path: Path):
    _database, store = _copy_metric_template(metric_template, tmp_path)
    current = metric_template[2]
    forged_receipt = _reidentify_contract(
        current.miss_persistence_receipt,
        "miss_receipt_id",
        "opportunity-miss-persistence-receipt",
        persisted_at=current.miss_persistence_receipt.persisted_at + timedelta(seconds=1),
    )
    forged = _reidentify_replay(
        current,
        "current-opportunity-miss-replay",
        miss_persistence_receipt=forged_receipt,
        full_chain_receipts=(forged_receipt,),
    )
    with pytest.raises(OpportunityMetricConflictError):
        store.append_session(
            metric_template[3],
            current_miss_replay=forged,
            persisted_at=metric_template[4].persisted_at + timedelta(seconds=1),
        )


def test_existing_multi_rejects_different_child_binding_lineage(
    metric_template, tmp_path: Path
):
    _database, store = _copy_metric_template(metric_template, tmp_path)
    child = metric_template[5]
    forged_receipt = _reidentify_contract(
        child.metric_persistence_receipt,
        "metric_receipt_id",
        "opportunity-metric-persistence-receipt",
        persisted_at=child.metric_persistence_receipt.persisted_at + timedelta(seconds=1),
    )
    forged = _reidentify_replay(
        child,
        "current-session-opportunity-metric-replay",
        metric_persistence_receipt=forged_receipt,
        full_chain_receipts=(forged_receipt,),
    )
    with pytest.raises(OpportunityMetricConflictError):
        store.append_multi(
            metric_template[6],
            current_session_metric_replays=(forged,),
            persisted_at=metric_template[7].persisted_at + timedelta(seconds=1),
        )
    with pytest.raises(OpportunityMetricConflictError):
        store.append_session(
            metric_template[6],  # type: ignore[arg-type]
            current_miss_replay=metric_template[2],
            persisted_at=metric_template[7].persisted_at + timedelta(seconds=1),
        )


def test_empty_multi_report_round_trip(tmp_path: Path):
    database = tmp_path / "empty.sqlite"
    store = OpportunityMetricStore(database)
    store.initialize()
    report = reconcile_discovery_metrics((), policy=_metric_policy())
    receipt = store.append_multi(
        report,
        current_session_metric_replays=(),
        persisted_at=metric_template_time(report),
    )
    assert tuple(item.count for item in receipt.family_counts) == (0, 1, 0)
    assert receipt.session_binding_count == 0
    current = store.replay_current(receipt.scope_key)
    assert isinstance(current, CurrentMultiMetricReplay)
    assert current.session_bindings == current.current_child_metric_replays == ()


def test_session_and_multi_correction_stale_and_historical_stability(
    metric_template,
    metric_session_correction_template,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    database = tmp_path / "metric.sqlite"
    store = _copy_database(metric_session_correction_template[0], database)
    first_session = metric_template[4]
    first_multi = metric_template[7]
    second_session = metric_session_correction_template[4]
    current_session = metric_session_correction_template[5]
    corrected_multi_report = metric_session_correction_template[6]
    historical = store.replay_historical(first_session.metric_receipt_id)
    assert historical is not None
    assert historical.metric_persistence_receipt == first_session
    # Exact retry remains idempotent even though its upstream miss head advanced.
    assert store.append_session(
        metric_template[3],
        current_miss_replay=metric_template[2],
        persisted_at=first_session.persisted_at + timedelta(seconds=20),
    ) == first_session
    current_session = store.replay_current(second_session.scope_key)
    assert isinstance(current_session, CurrentSessionMetricReplay)
    assert len(current_session.full_chain_receipts) == 2
    before_multi_retry = _metric_row_counts(database)
    for requested_at in (
        first_multi.persisted_at,
        first_multi.persisted_at + timedelta(seconds=30),
    ):
        assert store.append_multi(
            metric_template[6],
            current_session_metric_replays=(metric_template[5],),
            persisted_at=requested_at,
        ) == first_multi
        assert _metric_row_counts(database) == before_multi_retry
    with pytest.raises(OpportunityMetricConflictError):
        store.append_multi(
            metric_template[6],
            current_session_metric_replays=(metric_template[5],),
            persisted_at=first_multi.persisted_at - timedelta(microseconds=1),
        )
    with pytest.raises(OpportunityMetricConflictError):
        store.append_multi(
            metric_template[6],
            current_session_metric_replays=(current_session,),
            persisted_at=first_multi.persisted_at + timedelta(seconds=30),
        )
    with pytest.raises(OpportunityMetricConflictError):
        store.append_multi(
            metric_template[6],
            current_session_metric_replays=(metric_template[5],),
            persisted_at=first_multi.persisted_at + timedelta(seconds=30),
            supersedes_metric_receipt_id=first_multi.metric_receipt_id,
        )
    historical_multi = store.replay_historical(first_multi.metric_receipt_id)
    assert historical_multi is not None
    assert historical_multi.metric_persistence_receipt == first_multi
    with pytest.raises(OpportunityMetricStaleParentError):
        store.replay_current(first_multi.scope_key)
    second_multi = store.append_multi(
        corrected_multi_report,
        current_session_metric_replays=(current_session,),
        persisted_at=corrected_multi_report.recorded_at + timedelta(seconds=5),
        supersedes_metric_receipt_id=first_multi.metric_receipt_id,
    )
    counts: Counter[str] = Counter()

    def count(name, function):
        def wrapped(*args, **kwargs):
            counts[name] += 1
            return function(*args, **kwargs)

        return wrapped

    monkeypatch.setattr(
        metric_store_module,
        "_compute_metric_chain",
        count("metric_scope", metric_store_module._compute_metric_chain),
    )
    monkeypatch.setattr(
        metric_store_module,
        "_compute_stored_receipt",
        count("metric_receipt", metric_store_module._compute_stored_receipt),
    )
    monkeypatch.setattr(
        metric_store_module,
        "_audit_analysis_chain",
        count("miss_analysis", metric_store_module._audit_analysis_chain),
    )
    monkeypatch.setattr(
        metric_store_module,
        "_verify_current_parents",
        count("current_miss_parent", metric_store_module._verify_current_parents),
    )
    started_at = monotonic()
    current_multi = store.replay_current(second_multi.scope_key)
    replay_elapsed = monotonic() - started_at
    assert isinstance(current_multi, CurrentMultiMetricReplay)
    assert len(current_multi.full_chain_receipts) == 2
    assert current_multi.current_child_metric_replays == (current_session,)
    assert counts == {
        "metric_scope": 2,
        "metric_receipt": 4,
        "miss_analysis": 1,
        "current_miss_parent": 1,
    }
    assert replay_elapsed <= 600
    assert CurrentMultiMetricReplay.from_json(current_multi.to_json()) == current_multi


def metric_template_time(report):
    # Empty multi reports intentionally have no fact-recording timestamp.
    from datetime import datetime, timezone

    return datetime(2026, 8, 12, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("field", "replacement"),
    (
        ("parent_metric_scope_key", "forged-parent-scope"),
        ("child_metric_scope_key", "forged-child-scope"),
        ("child_session_report_id", "forged-child-report"),
        ("child_session_report_content_hash_sha256", "0" * 64),
        ("child_miss_receipt_id", "forged-child-miss"),
        ("child_miss_receipt_content_hash_sha256", "1" * 64),
        ("session_ordinal", 1),
        ("exchange_session_id", "forged-session"),
    ),
)
def test_binding_consistent_rehash_tamper_rejected_by_public_parent(
    metric_template, field: str, replacement
):
    replay = metric_template[8]
    forged = _reidentify_binding(replay.session_bindings[0], **{field: replacement})
    with pytest.raises(ValueError):
        _reidentify_replay(
            replay,
            "current-multi-opportunity-metric-replay",
            session_bindings=(forged,),
        )


def test_binding_rejects_unsanitized_session(metric_template):
    binding = metric_template[8].session_bindings[0]
    with pytest.raises(ValueError):
        _reidentify_binding(binding, exchange_session_id="session/api_key=secret")


def test_append_public_shape_errors_are_typed(metric_template, tmp_path: Path):
    _database, store = _copy_metric_template(metric_template, tmp_path)
    with pytest.raises(OpportunityMetricConflictError):
        store.append_multi(
            metric_template[6],
            current_session_metric_replays=(),
            persisted_at=metric_template[7].persisted_at + timedelta(seconds=1),
        )


def test_multi_binding_rejects_non_session_child(metric_template):
    replay = metric_template[8]
    with pytest.raises(ValueError, match="SESSION replay"):
        validate_binding_set(
            replay.metric_report,
            replay.metric_persistence_receipt.scope_key,
            replay.session_bindings,
            (replay,),  # type: ignore[arg-type]
        )


def test_verification_context_is_connection_local_and_cycle_safe(tmp_path: Path):
    database = tmp_path / "context.sqlite"
    OpportunityMetricStore(database).initialize()
    first = sqlite3.connect(database)
    second = sqlite3.connect(database)
    try:
        context = _MetricVerificationContext(first)
        with pytest.raises(OpportunityMetricIntegrityError, match="crossed"):
            context.assert_connection(second)
        context.enter("metric_chain", "cycle-scope")
        with pytest.raises(OpportunityMetricIntegrityError, match="cycle"):
            metric_store_module._audit_metric_chain(
                first,
                "cycle-scope",
                context,
            )
        assert "cycle-scope" not in context.metric_chains
    finally:
        first.close()
        second.close()


def test_public_json_rejects_unknown_duplicate_and_float(metric_template):
    replay = metric_template[8]
    raw = replay.to_json()
    with pytest.raises(ValueError):
        CurrentMultiMetricReplay.from_json(raw[:-1] + ',"unknown":1}')
    duplicate = raw.replace('"research_only":true', '"research_only":true,"research_only":true', 1)
    with pytest.raises(ValueError):
        CurrentMultiMetricReplay.from_json(duplicate)
    floated = raw.replace('"session_ordinal":0', '"session_ordinal":0.0', 1)
    with pytest.raises((TypeError, ValueError)):
        CurrentMultiMetricReplay.from_json(floated)


def test_direct_and_json_replay_reject_nonincreasing_report_chronology(metric_template):
    initial = metric_template[4]
    report = metric_template[3]
    parent = metric_template[2]
    correction = build_metric_receipt(
        report,
        persisted_at=initial.persisted_at + timedelta(seconds=1),
        predecessor=initial,
        parent_miss=parent,
        children=(),
        bindings=(),
    )
    current_values = {
        "metric_persistence_receipt": correction,
        "metric_report": report,
        "full_chain_receipts": (initial, correction),
        "full_chain_reports": (report, report),
        "current_miss_replay": parent,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.current_session_metric_replay.v1",
    }
    with pytest.raises(ValueError, match="report chronology"):
        CurrentSessionMetricReplay(
            replay_id=stable_identity(
                "current-session-opportunity-metric-replay", current_values
            ),
            **current_values,
        )
    historical = OpportunityMetricStore(metric_template[0]).replay_historical(
        initial.metric_receipt_id
    )
    assert historical is not None and historical.historical_miss_replay is not None
    historical_values = {
        "metric_persistence_receipt": correction,
        "metric_report": report,
        "chain_prefix_receipts": (initial, correction),
        "chain_prefix_reports": (report, report),
        "historical_miss_replay": historical.historical_miss_replay,
        "session_bindings": (),
        "historical_child_metric_replays": (),
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.historical_metric_replay.v1",
    }
    with pytest.raises(ValueError, match="report chronology"):
        HistoricalMetricReplay(
            replay_id=stable_identity(
                "historical-opportunity-metric-replay", historical_values
            ),
            **historical_values,
        )
    for contract, namespace, values in (
        (
            CurrentSessionMetricReplay,
            "current-session-opportunity-metric-replay",
            current_values,
        ),
        (
            HistoricalMetricReplay,
            "historical-opportunity-metric-replay",
            historical_values,
        ),
    ):
        payload = {
            "replay_id": stable_identity(namespace, values),
            **{
                key: (
                    tuple(item.to_dict() for item in value)
                    if isinstance(value, tuple)
                    else value.to_dict()
                    if hasattr(value, "to_dict")
                    else value
                )
                for key, value in values.items()
            },
        }
        from intraday_scanner.v2.contracts import contract_to_json

        with pytest.raises(ValueError, match="report chronology"):
            contract.from_json(contract_to_json(payload))


@pytest.mark.parametrize(
    ("method", "value"),
    (
        ("load_receipt", ""),
        ("load_report", "../secret"),
        ("replay_historical", "metric-receipt/api_key=secret"),
        ("replay_current", "scope/C:/Users/private"),
    ),
)
def test_read_lookup_ids_reject_before_connect_with_typed_error(
    tmp_path: Path, method: str, value: str
):
    database = tmp_path / "must-not-exist.sqlite"
    store = OpportunityMetricStore(database)
    with pytest.raises(OpportunityMetricIntegrityError, match="invalid metric lookup"):
        getattr(store, method)(value)
    assert not database.exists()


def test_schema_guards_are_exact_and_all_mutations_abort(metric_template, tmp_path: Path):
    database, _store = _copy_metric_template(metric_template, tmp_path)
    cases = (
        ("opportunity_metric_receipts", "UPDATE", "persisted_at=persisted_at"),
        ("opportunity_metric_receipts", "DELETE", ""),
        (
            "opportunity_metric_session_bindings",
            "UPDATE",
            "exchange_session_id=exchange_session_id",
        ),
        ("opportunity_metric_session_bindings", "DELETE", ""),
    )
    for table, operation, assignment in cases:
        with sqlite3.connect(database) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            sql = (
                f"UPDATE {table} SET {assignment}"
                if operation == "UPDATE"
                else f"DELETE FROM {table}"
            )
            with pytest.raises(sqlite3.IntegrityError):
                connection.execute(sql)
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        validate_metric_schema(connection)


@pytest.mark.parametrize("entrypoint", ("initialize", "load", "append"))
def test_same_named_forged_trigger_fails_all_entrypoints(
    metric_template, tmp_path: Path, entrypoint: str
):
    database, store = _copy_metric_template(metric_template, tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER opportunity_metric_receipts_no_update")
        connection.execute(
            "CREATE TRIGGER opportunity_metric_receipts_no_update "
            "AFTER INSERT ON opportunity_metric_receipts BEGIN SELECT 1; END"
        )
    with pytest.raises(OpportunityMetricIntegrityError):
        if entrypoint == "initialize":
            store.initialize()
        elif entrypoint == "load":
            store.load_receipt(metric_template[4].metric_receipt_id)
        else:
            store.append_session(
                metric_template[3],
                current_miss_replay=metric_template[2],
                persisted_at=metric_template[4].persisted_at + timedelta(seconds=1),
            )


@pytest.mark.parametrize(
    ("case", "entrypoint"),
    (
        ("receipt_hash", "load_receipt"),
        ("receipt_projection", "load_report"),
        ("receipt_json", "historical"),
        ("report_json", "current"),
        ("report_hash", "load_receipt"),
        ("inventory_hash", "historical"),
        ("counts", "current"),
        ("missing_binding", "load_report"),
        ("binding_ordinal", "historical"),
        ("binding_json", "current"),
        ("binding_hash", "load_receipt"),
    ),
)
def test_raw_corruption_fails_after_canonical_guard_restore(
    metric_template, tmp_path: Path, case: str, entrypoint: str
):
    database, store = _copy_metric_template(metric_template, tmp_path)
    receipt_id = metric_template[7].metric_receipt_id

    def mutate(connection):
        receipt_mutations = {
            "receipt_hash": (
                "UPDATE opportunity_metric_receipts "
                "SET receipt_content_hash_sha256=? WHERE metric_receipt_id=?",
                ("0" * 64, receipt_id),
            ),
            "receipt_projection": (
                "UPDATE opportunity_metric_receipts "
                "SET metric_policy_id=? WHERE metric_receipt_id=?",
                ("forged-metric-policy", receipt_id),
            ),
            "receipt_json": (
                "UPDATE opportunity_metric_receipts SET receipt_json=? "
                "WHERE metric_receipt_id=?",
                ('{"forged":true}', receipt_id),
            ),
            "report_json": (
                "UPDATE opportunity_metric_receipts SET report_json=? "
                "WHERE metric_receipt_id=?",
                ('{"forged":true}', receipt_id),
            ),
            "report_hash": (
                "UPDATE opportunity_metric_receipts "
                "SET report_content_hash_sha256=? WHERE metric_receipt_id=?",
                ("1" * 64, receipt_id),
            ),
            "inventory_hash": (
                "UPDATE opportunity_metric_receipts "
                "SET artifact_inventory_hash_sha256=? WHERE metric_receipt_id=?",
                ("2" * 64, receipt_id),
            ),
            "counts": (
                "UPDATE opportunity_metric_receipts "
                "SET session_binding_count=2,artifact_count=3 "
                "WHERE metric_receipt_id=?",
                (receipt_id,),
            ),
        }
        if case in receipt_mutations:
            connection.execute(*receipt_mutations[case])
            return
        if case == "missing_binding":
            connection.execute(
                "DELETE FROM opportunity_metric_session_bindings "
                "WHERE metric_receipt_id=?",
                (receipt_id,),
            )
        elif case == "binding_ordinal":
            connection.execute(
                "UPDATE opportunity_metric_session_bindings SET session_ordinal=1 "
                "WHERE metric_receipt_id=?",
                (receipt_id,),
            )
        elif case == "binding_json":
            connection.execute(
                "UPDATE opportunity_metric_session_bindings SET binding_json=? "
                "WHERE metric_receipt_id=?",
                ('{"forged":true}', receipt_id),
            )
        else:
            assert case == "binding_hash"
            connection.execute(
                "UPDATE opportunity_metric_session_bindings "
                "SET binding_content_hash_sha256=? WHERE metric_receipt_id=?",
                ("3" * 64, receipt_id),
            )

    _apply_guarded_raw_mutation(
        database,
        trigger_names=(
            "opportunity_metric_receipts_no_update",
            "opportunity_metric_session_bindings_no_update",
            "opportunity_metric_session_bindings_no_delete",
        ),
        mutate=mutate,
    )
    with pytest.raises(OpportunityMetricIntegrityError):
        if entrypoint == "load_receipt":
            store.load_receipt(receipt_id)
        elif entrypoint == "load_report":
            store.load_report(receipt_id)
        elif entrypoint == "historical":
            store.replay_historical(receipt_id)
        else:
            assert entrypoint == "current"
            store.replay_current(metric_template[7].scope_key)


@pytest.mark.parametrize(
    "field",
    (
        "child_metric_scope_key",
        "child_session_report_id",
        "child_session_report_content_hash_sha256",
        "child_miss_receipt_id",
        "child_miss_receipt_content_hash_sha256",
    ),
)
def test_binding_parent_projection_drift_is_rejected_by_exact_fk(
    metric_template, tmp_path: Path, field: str
):
    database, _store = _copy_metric_template(metric_template, tmp_path)
    receipt_id = metric_template[7].metric_receipt_id
    replacement = "0" * 64 if field.endswith("hash_sha256") else "forged-identity"

    def mutate(connection):
        connection.execute(
            f"UPDATE opportunity_metric_session_bindings SET {field}=? "
            "WHERE metric_receipt_id=?",
            (replacement, receipt_id),
        )

    _assert_ddl_rejects_without_mutation(
        database,
        trigger_name="opportunity_metric_session_bindings_no_update",
        mutate=mutate,
    )


@pytest.mark.parametrize("case", ("orphan", "fork", "disconnected", "extra_binding"))
def test_chain_and_binding_corruption_not_representable_under_canonical_ddl(
    metric_template, metric_session_correction_template, tmp_path: Path, case: str
):
    database = tmp_path / "metric.sqlite"
    _copy_database(metric_session_correction_template[0], database)
    second = metric_session_correction_template[4]

    def mutate(connection):
        if case == "extra_binding":
            binding = dict(
                connection.execute(
                    "SELECT * FROM opportunity_metric_session_bindings "
                    "WHERE metric_receipt_id=?",
                    (metric_template[7].metric_receipt_id,),
                ).fetchone()
            )
            binding["session_ordinal"] = 1
            _insert_mapping(connection, "opportunity_metric_session_bindings", binding)
        elif case == "fork":
            row = dict(
                connection.execute(
                    "SELECT * FROM opportunity_metric_receipts WHERE metric_receipt_id=?",
                    (second.metric_receipt_id,),
                ).fetchone()
            )
            row["metric_receipt_id"] = "metric-receipt:forged-fork"
            row["receipt_content_hash_sha256"] = "a" * 64
            row["report_id"] = "metric-report:forged-fork"
            _insert_mapping(connection, "opportunity_metric_receipts", row)
        else:
            if case == "orphan":
                sql = (
                    "UPDATE opportunity_metric_receipts "
                    "SET supersedes_metric_receipt_id=?,"
                    "supersedes_metric_receipt_content_hash_sha256=? "
                    "WHERE metric_receipt_id=?"
                )
                values = ("metric-receipt:missing", "b" * 64, second.metric_receipt_id)
            else:
                sql = (
                    "UPDATE opportunity_metric_receipts SET receipt_kind='initial',"
                    "supersedes_metric_receipt_id=NULL,"
                    "supersedes_metric_receipt_content_hash_sha256=NULL "
                    "WHERE metric_receipt_id=?"
                )
                values = (second.metric_receipt_id,)
            connection.execute(sql, values)

    _assert_ddl_rejects_without_mutation(
        database,
        trigger_name=(
            "opportunity_metric_session_bindings_no_update"
            if case == "extra_binding"
            else "opportunity_metric_receipts_no_update"
        ),
        mutate=mutate,
    )


def test_two_revision_cycle_rejected_by_chain_audit_after_schema_acceptance(
    metric_template, metric_session_correction_template, tmp_path: Path
):
    database = tmp_path / "metric.sqlite"
    store = _copy_database(metric_session_correction_template[0], database)
    first = metric_template[4]
    second = metric_session_correction_template[4]

    def mutate(connection):
        connection.execute(
            "UPDATE opportunity_metric_receipts SET receipt_kind='correction',"
            "supersedes_metric_receipt_id=?,"
            "supersedes_metric_receipt_content_hash_sha256=? "
            "WHERE metric_receipt_id=?",
            (second.metric_receipt_id, second.content_hash(), first.metric_receipt_id),
        )

    _apply_guarded_raw_mutation(
        database,
        trigger_names=("opportunity_metric_receipts_no_update",),
        mutate=mutate,
    )
    for entrypoint in ("load", "historical", "current"):
        with pytest.raises(OpportunityMetricIntegrityError, match="exactly one root"):
            if entrypoint == "load":
                store.load_receipt(first.metric_receipt_id)
            elif entrypoint == "historical":
                store.replay_historical(first.metric_receipt_id)
            else:
                store.replay_current(first.scope_key)


def test_multi_partial_binding_insert_rolls_back_entire_transaction(
    metric_template, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    database, store = _copy_metric_template(metric_template, tmp_path)
    multi_receipt = metric_template[7]

    def remove_multi(connection):
        connection.execute(
            "DELETE FROM opportunity_metric_session_bindings WHERE metric_receipt_id=?",
            (multi_receipt.metric_receipt_id,),
        )
        connection.execute(
            "DELETE FROM opportunity_metric_receipts WHERE metric_receipt_id=?",
            (multi_receipt.metric_receipt_id,),
        )

    _apply_guarded_raw_mutation(
        database,
        trigger_names=(
            "opportunity_metric_receipts_no_delete",
            "opportunity_metric_session_bindings_no_delete",
        ),
        mutate=remove_multi,
    )
    before = _metric_row_counts(database)

    def fail_binding_insert(_connection, _receipt, _bindings):
        raise sqlite3.IntegrityError("fixture binding insertion failure")

    monkeypatch.setattr(store, "_insert_bindings", fail_binding_insert)
    with pytest.raises(OpportunityMetricConflictError, match="immutable constraints"):
        store.append_multi(
            metric_template[6],
            current_session_metric_replays=(metric_template[5],),
            persisted_at=multi_receipt.persisted_at,
        )
    assert _metric_row_counts(database) == before
    assert store.load_receipt(multi_receipt.metric_receipt_id) is None


def test_read_only_inert_absent_and_query_only(metric_template, tmp_path: Path):
    absent = tmp_path / "absent.sqlite"
    read_only = OpportunityMetricStore(absent, read_only=True)
    assert not absent.exists()
    with pytest.raises(OpportunityMetricReadOnlyError):
        read_only.initialize()
    with pytest.raises(OpportunityMetricIntegrityError):
        read_only.load_receipt("missing")
    database, _ = _copy_metric_template(metric_template, tmp_path)
    store = OpportunityMetricStore(database, read_only=True)
    connection = store._connect_read()
    try:
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("CREATE TABLE forbidden(value TEXT)")
    finally:
        connection.close()
    sidecar = Path(f"{database}-wal")
    sidecar.touch()
    with pytest.raises(OpportunityMetricIntegrityError, match="sidecar"):
        store.load_receipt(metric_template[4].metric_receipt_id)


def test_import_firewall_and_no_root_export():
    code = (
        "import sys; import intraday_scanner.v2.opportunity; "
        "import intraday_scanner.v2.opportunity.pipeline; "
        "assert not any('miss_metric_persistence' in k or "
        "'opportunity_metric_store' in k for k in sys.modules)"
    )
    subprocess.run([sys.executable, "-c", code], check=True)
    root = Path("intraday_scanner")
    forbidden = ("app", "broker", "network", "scheduler", "ui", "runtime")
    for path in tuple(root.glob("v2/opportunity/miss_metric_persistence.py")) + tuple(
        root.glob("storage/opportunity_metric*.py")
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = (
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert not any(any(part in item for part in forbidden) for item in imports)
