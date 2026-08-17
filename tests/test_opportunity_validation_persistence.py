from __future__ import annotations

import ast
import hashlib
import shutil
import sqlite3
import subprocess
import sys
from datetime import timedelta
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import CURRENT_SCHEMA_VERSION, run_migrations
from intraday_scanner.storage.opportunity_validation_contracts import (
    ValidationPersistenceReceipt,
    ValidationPersistenceStatus,
    build_validation_persistence_receipt,
)
from intraday_scanner.storage.opportunity_validation_schema import (
    expected_validation_schema_inventory,
    validate_validation_schema,
    validation_schema_inventory,
)
from intraday_scanner.storage.opportunity_validation_store import (
    OpportunityValidationConflictError,
    OpportunityValidationIntegrityError,
    OpportunityValidationReadOnlyError,
    OpportunityValidationStore,
)
from intraday_scanner.v2.opportunity.validation_audit import (
    build_chronological_validation_preparation,
)
from intraday_scanner.v2.opportunity.validation_contracts import (
    HoldoutAccessStatus,
    build_holdout_access_evidence,
    build_validation_split_policy,
)
from intraday_scanner.v2.opportunity.validation_robustness import (
    ValidationRobustnessReport,
    build_confirmatory_population,
    build_confirmatory_unit,
)
from tests import test_opportunity_validation as validation_fixtures
from tests.test_opportunity_validation_metrics import _report
from tests.test_opportunity_validation_robustness import (
    _complete_evidence,
    _robustness_report,
)
from tests.test_opportunity_validation_robustness import (
    _policy as robustness_policy,
)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _bundle(
    monkeypatch: pytest.MonkeyPatch,
    *,
    access_status: HoldoutAccessStatus = HoldoutAccessStatus.NO_DURABLE_EVIDENCE,
    retrospective: bool = False,
    locked_oos: bool = True,
):
    corpus = validation_fixtures._multi_session_corpus(monkeypatch, count=5)
    locked_count = 1 if locked_oos else 0
    split = build_validation_split_policy(
        policy_version="wp006-synthetic-lock-v1",
        declared_at=(
            corpus.sessions[-1].session_open_at
            if retrospective
            else corpus.sessions[0].session_open_at
        ),
        train_research_session_count=2 if locked_oos else 3,
        validation_session_count=2,
        locked_oos_session_count=locked_count,
        locked_oos_required=locked_oos,
    )
    artifacts = (
        ("synthetic-prior-holdout-artifact",)
        if access_status is HoldoutAccessStatus.PREVIOUSLY_EVALUATED
        else ()
    )
    artifact_hashes = (
        (_hash("synthetic prior holdout artifact"),)
        if access_status is HoldoutAccessStatus.PREVIOUSLY_EVALUATED
        else ()
    )
    access = build_holdout_access_evidence(
        status=access_status,
        observed_at=corpus.frozen_at,
        source_identity="wp006-synthetic-holdout-audit",
        source_version="v1",
        method="synthetic append-only governance fixture",
        artifact_ids=artifacts,
        artifact_content_hashes=artifact_hashes,
        reason=(
            None
            if access_status is HoldoutAccessStatus.PREVIOUSLY_EVALUATED
            else "synthetic fixture has no prior durable locked OOS receipt"
        ),
        limitations=("synthetic_software_invariant_only",),
    )
    preparation = build_chronological_validation_preparation(
        corpus,
        split_policy=split,
        holdout_access_evidence=access,
        audited_at=corpus.frozen_at + timedelta(seconds=1),
        recorded_at=corpus.frozen_at + timedelta(seconds=2),
    )
    metric = _report(preparation)
    scope = next(item for item in metric.scopes if item.kind.value == "final_validation")
    row = next(item for item in metric.bound_rows if item.row_id in scope.row_ids)
    evaluation = row.outcome.decision.evaluation
    unit = build_confirmatory_unit(
        strategy_id=evaluation.strategy_id,
        strategy_version=evaluation.strategy_version,
        direction=evaluation.direction,
    )
    population = build_confirmatory_population(metric, unit=unit)
    robustness = _robustness_report(population)
    return preparation, metric, robustness, access


@pytest.fixture(scope="module")
def valid_bundle():
    monkeypatch = pytest.MonkeyPatch()
    try:
        yield _bundle(monkeypatch)
    finally:
        monkeypatch.undo()


def _append(store: OpportunityValidationStore, bundle, *, seconds: int = 1, **kwargs):
    preparation, metric, robustness, access = bundle
    return store.append(
        preparation,
        metric,
        robustness,
        access,
        code_identity=kwargs.pop("code_identity", "git:wp006-synthetic-code"),
        code_content_hash_sha256=kwargs.pop("code_hash", _hash("wp006 code body")),
        persisted_at=robustness.recorded_at + timedelta(seconds=seconds),
        **kwargs,
    )


def test_atomic_fresh_consumption_roundtrip_and_exact_retry(
    tmp_path: Path,
    valid_bundle,
) -> None:
    database = tmp_path / "validation.sqlite"
    store = OpportunityValidationStore(database)
    store.initialize()
    receipt = _append(store, valid_bundle, consume_locked_oos=True)
    replay = store.replay(receipt.validation_receipt_id)
    assert replay is not None
    assert replay.persistence_receipt == receipt
    assert replay.preparation.to_json() == valid_bundle[0].to_json()
    assert replay.metric_report.to_json() == valid_bundle[1].to_json()
    assert replay.robustness_report.to_json() == valid_bundle[2].to_json()
    assert replay.holdout_access_evidence.to_json() == valid_bundle[3].to_json()
    assert ValidationPersistenceReceipt.from_json(receipt.to_json()) == receipt
    assert _append(store, valid_bundle, consume_locked_oos=True) == receipt
    assert receipt.status is ValidationPersistenceStatus.LOCKED_OOS_CONSUMED
    assert receipt.research_only and not receipt.promotion_eligible
    assert receipt.lifecycle_mutation_count == 0 and not receipt.take_authorization
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_validation_receipts"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_validation_oos_sessions"
        ).fetchone() == (1,)


def test_database_semantic_key_rejects_second_use_alias_and_later_retry(
    tmp_path: Path,
    valid_bundle,
) -> None:
    database = tmp_path / "one-time.sqlite"
    store = OpportunityValidationStore(database)
    store.initialize()
    _append(store, valid_bundle, consume_locked_oos=True)
    with pytest.raises(OpportunityValidationConflictError, match="already consumed"):
        _append(store, valid_bundle, seconds=2, consume_locked_oos=True)
    with pytest.raises(OpportunityValidationConflictError, match="already consumed"):
        _append(
            store,
            valid_bundle,
            seconds=3,
            code_identity="git:alias-for-same-code",
            consume_locked_oos=True,
        )
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT * FROM opportunity_validation_receipts"
        ).fetchone()
        columns = tuple(
            item[1]
            for item in connection.execute(
                "PRAGMA table_info(opportunity_validation_receipts)"
            ).fetchall()
        )
        values = dict(zip(columns, row, strict=True))
        values["validation_receipt_id"] = "raw-alias-receipt"
        placeholders = ",".join("?" for _ in columns)
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                f"INSERT INTO opportunity_validation_receipts "
                f"({','.join(columns)}) VALUES ({placeholders})",
                tuple(values[name] for name in columns),
            )
        values["validation_receipt_id"] = "raw-changed-inventory-receipt"
        values["semantic_lock_key"] = _hash("consistently rehashed lock body")
        values["holdout_inventory_key"] = _hash("changed OOS inventory")
        values["oos_session_inventory_hash_sha256"] = _hash(
            "changed OOS inventory projection"
        )
        with pytest.raises(sqlite3.IntegrityError, match="UNIQUE"):
            connection.execute(
                f"INSERT INTO opportunity_validation_receipts "
                f"({','.join(columns)}) VALUES ({placeholders})",
                tuple(values[name] for name in columns),
            )


def test_changed_result_cannot_create_a_fresh_claim(
    tmp_path: Path,
    valid_bundle,
) -> None:
    database = tmp_path / "changed-result.sqlite"
    store = OpportunityValidationStore(database)
    store.initialize()
    _append(store, valid_bundle, consume_locked_oos=True)
    preparation, metric, robustness, access = valid_bundle
    population = robustness.population
    policy = robustness_policy(population)
    arms, regimes, complexity, sentinel = _complete_evidence(population)
    changed = ValidationRobustnessReport.from_json(
        _robustness_report(
            population,
            policy=policy,
            arms=tuple(item for item in arms if item.kind.value != "negative_control"),
            regimes=regimes,
            complexity=complexity,
            sentinel=sentinel,
        ).to_json()
    )
    assert changed.report_id != robustness.report_id
    with pytest.raises(OpportunityValidationConflictError, match="already consumed"):
        _append(
            store,
            (preparation, metric, changed, access),
            consume_locked_oos=True,
        )


@pytest.mark.parametrize(
    ("access_status", "retrospective", "locked_oos", "expected"),
    (
        (HoldoutAccessStatus.NO_DURABLE_EVIDENCE, True, True, "retrospective"),
        (HoldoutAccessStatus.PREVIOUSLY_EVALUATED, False, True, "reused"),
        (HoldoutAccessStatus.UNKNOWN, False, True, "missing_evidence"),
        (HoldoutAccessStatus.NO_DURABLE_EVIDENCE, False, False, "non_predeclared"),
    ),
)
def test_invalid_retrospective_reused_missing_and_nonpredeclared_fail_closed(
    tmp_path: Path,
    access_status: HoldoutAccessStatus,
    retrospective: bool,
    locked_oos: bool,
    expected: str,
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        bundle = _bundle(
            monkeypatch,
            access_status=access_status,
            retrospective=retrospective,
            locked_oos=locked_oos,
        )
    finally:
        monkeypatch.undo()
    database = tmp_path / f"{expected}.sqlite"
    store = OpportunityValidationStore(database)
    store.initialize()
    with pytest.raises(OpportunityValidationConflictError, match=expected):
        _append(store, bundle, consume_locked_oos=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_validation_receipts"
        ).fetchone() == (0,)


def test_failed_partial_transaction_does_not_consume_lock(
    tmp_path: Path,
    valid_bundle,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback.sqlite"
    store = OpportunityValidationStore(database)
    store.initialize()

    def fail_sessions(*_args, **_kwargs):
        raise sqlite3.OperationalError("synthetic interrupted child insert")

    monkeypatch.setattr(store, "_insert_sessions", fail_sessions)
    with pytest.raises(OpportunityValidationIntegrityError, match="interrupted"):
        _append(store, valid_bundle, consume_locked_oos=True)
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_validation_receipts"
        ).fetchone() == (0,)
    assert _append(
        OpportunityValidationStore(database),
        valid_bundle,
        consume_locked_oos=True,
    ).status is ValidationPersistenceStatus.LOCKED_OOS_CONSUMED


def test_append_only_ddl_and_tamper_missing_inventory_fail_closed(
    tmp_path: Path,
    valid_bundle,
) -> None:
    database = tmp_path / "tamper.sqlite"
    store = OpportunityValidationStore(database)
    store.initialize()
    receipt = _append(store, valid_bundle, consume_locked_oos=True)
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(
                "UPDATE opportunity_validation_receipts SET code_identity='tampered'"
            )
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM opportunity_validation_oos_sessions")
    with sqlite3.connect(database) as connection:
        trigger = connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='trigger' "
            "AND name='opportunity_validation_oos_sessions_no_delete'"
        ).fetchone()[0]
        connection.execute(
            "DROP TRIGGER opportunity_validation_oos_sessions_no_delete"
        )
        connection.execute("DELETE FROM opportunity_validation_oos_sessions")
        connection.execute(trigger)
        connection.commit()
    with pytest.raises(OpportunityValidationIntegrityError, match="inventory count"):
        store.replay(receipt.validation_receipt_id)


def test_schema_fingerprint_detects_same_named_forgery(tmp_path: Path) -> None:
    database = tmp_path / "schema.sqlite"
    OpportunityValidationStore(database).initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        assert validation_schema_inventory(connection) == (
            expected_validation_schema_inventory()
        )
        connection.execute(
            "DROP INDEX idx_opportunity_validation_receipts_result"
        )
        connection.execute(
            "CREATE INDEX idx_opportunity_validation_receipts_result "
            "ON opportunity_validation_receipts(metric_report_id)"
        )
        with pytest.raises(OpportunityValidationIntegrityError, match="schema object"):
            validate_validation_schema(connection)


def test_two_disposable_migrations_converge_and_old_rows_remain(tmp_path: Path) -> None:
    source = tmp_path / "source.sqlite"
    with sqlite3.connect(source) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (29, 'fixture')")
        connection.execute("CREATE TABLE old_truth (id INTEGER PRIMARY KEY, body TEXT)")
        connection.execute("INSERT INTO old_truth VALUES (1, 'unchanged')")
        connection.commit()
    inventories = []
    for ordinal in (1, 2):
        target = tmp_path / f"migration-{ordinal}.sqlite"
        shutil.copy2(source, target)
        with sqlite3.connect(target) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            assert run_migrations(connection) == CURRENT_SCHEMA_VERSION == 30
            assert run_migrations(connection) == 30
            assert connection.execute("SELECT * FROM old_truth").fetchall() == [
                (1, "unchanged")
            ]
            inventories.append(validation_schema_inventory(connection))
    assert inventories[0] == inventories[1]


def test_read_only_replay_is_query_only_inert_and_creates_no_sidecars(
    tmp_path: Path,
    valid_bundle,
) -> None:
    absent = tmp_path / "absent.sqlite"
    read_only = OpportunityValidationStore(absent, read_only=True)
    with pytest.raises(OpportunityValidationReadOnlyError):
        read_only.initialize()
    with pytest.raises(OpportunityValidationIntegrityError, match="does not exist"):
        read_only.replay("missing-validation-receipt")
    assert not absent.exists()
    assert not tuple(tmp_path.glob("absent.sqlite-*"))

    database = tmp_path / "readonly.sqlite"
    writer = OpportunityValidationStore(database)
    writer.initialize()
    receipt = _append(writer, valid_bundle, consume_locked_oos=True)
    before = hashlib.sha256(database.read_bytes()).hexdigest()
    observer = OpportunityValidationStore(database, read_only=True)
    assert observer.replay(receipt.validation_receipt_id) is not None
    with pytest.raises(OpportunityValidationReadOnlyError):
        _append(observer, valid_bundle, consume_locked_oos=True)
    assert hashlib.sha256(database.read_bytes()).hexdigest() == before
    assert not tuple(tmp_path.glob("readonly.sqlite-*"))


def test_validation_persistence_import_firewall_is_lazy() -> None:
    root = Path(__file__).resolve().parents[1]
    forbidden_files = tuple(
        root / "intraday_scanner" / "v2" / "opportunity" / name
        for name in (
            "discovery.py",
            "features.py",
            "regimes.py",
            "registry.py",
            "ranking.py",
            "risk.py",
            "quality_gate.py",
            "pipeline.py",
        )
    ) + (root / "app.py",)
    for path in forbidden_files:
        imports = tuple(
            node.module or ""
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.ImportFrom)
        ) + tuple(
            alias.name
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
            if isinstance(node, ast.Import)
            for alias in node.names
        )
        assert not any("opportunity_validation" in item for item in imports), path
    code = """
import sys
import intraday_scanner.v2.opportunity.pipeline
import intraday_scanner.storage
import intraday_scanner.storage.opportunity_store
import intraday_scanner.storage.opportunity_outcome_store
import intraday_scanner.storage.opportunity_miss_store
import intraday_scanner.storage.opportunity_metric_store
assert not any('opportunity_validation' in name for name in sys.modules)
assert not any('validation_robustness' in name for name in sys.modules)
"""
    completed = subprocess.run(
        [sys.executable, "-c", code],
        cwd=root,
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_direct_receipt_rejects_alias_and_invalid_status_tamper(valid_bundle) -> None:
    preparation, metric, robustness, access = valid_bundle
    receipt = build_validation_persistence_receipt(
        preparation,
        metric,
        robustness,
        access,
        code_identity="git:wp006-synthetic-code",
        code_content_hash_sha256=_hash("wp006 code body"),
        persisted_at=robustness.recorded_at + timedelta(seconds=1),
        status=ValidationPersistenceStatus.LOCKED_OOS_CONSUMED,
    )
    payload = receipt.to_dict()
    payload["validation_receipt_id"] = _hash("caller-chosen-alias")
    with pytest.raises(ValueError, match="identity"):
        type(receipt).from_dict(payload)
    invalid_status = receipt.to_dict()
    invalid_status["status"] = "reused"
    with pytest.raises(ValueError, match="fresh-lock eligible"):
        type(receipt).from_dict(invalid_status)
    assert preparation.audit_receipt.fold_collection.split_plan.holdout_access_evidence == access


def test_research_only_invalid_status_can_be_recorded_without_consuming(
    tmp_path: Path,
) -> None:
    monkeypatch = pytest.MonkeyPatch()
    try:
        bundle = _bundle(monkeypatch, retrospective=True)
    finally:
        monkeypatch.undo()
    database = tmp_path / "retrospective-evidence.sqlite"
    store = OpportunityValidationStore(database)
    store.initialize()
    receipt = _append(
        store,
        bundle,
        status=ValidationPersistenceStatus.RETROSPECTIVE,
    )
    assert receipt.status is ValidationPersistenceStatus.RETROSPECTIVE
    assert not receipt.fresh_lock_eligible
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_validation_receipts "
            "WHERE status='locked_oos_consumed'"
        ).fetchone() == (0,)
