from __future__ import annotations

import ast
import hashlib
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
from test_opportunity_pipeline import (
    NOW,
    _execution_risk_for,
    _finalized_two_strategy_pipeline,
    _pipeline_dataset,
    _pipeline_risk_policy,
    _pipeline_universe,
    _two_candidate_dataset,
)

from intraday_scanner.storage.migrations import MIGRATIONS, run_migrations, set_schema_version
from intraday_scanner.storage.opportunity_store import (
    CANONICAL_OPPORTUNITY_ARTIFACT_FAMILIES,
    OpportunityArtifactFamily,
    OpportunityPersistenceConflictError,
    OpportunityPersistenceIntegrityError,
    OpportunityPersistenceReceipt,
    OpportunityStore,
    OpportunityStoreReadOnlyError,
    _artifact_inventory_hash,
    _build_artifact_inventory,
    _build_persistence_receipt,
)
from intraday_scanner.v2.opportunity.expectancy import build_expectancy_evidence
from intraday_scanner.v2.opportunity.models import EvaluationStatus
from intraday_scanner.v2.opportunity.pipeline import (
    PipelineResult,
    build_strategy_expectancy_binding,
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.registry import StrategyRegistry, build_default_registry

RECORDED_AT = datetime(2026, 8, 11, 15, 0, tzinfo=timezone.utc)


@pytest.fixture
def pipeline_result() -> PipelineResult:
    return _finalized_two_strategy_pipeline()[2]


def _initialized_store(path: Path) -> OpportunityStore:
    store = OpportunityStore(path)
    store.initialize()
    return store


def _row_counts(path: Path) -> tuple[int, int]:
    with sqlite3.connect(path) as connection:
        run_count = int(
            connection.execute("SELECT COUNT(*) FROM opportunity_pipeline_runs").fetchone()[0]
        )
        artifact_count = int(
            connection.execute("SELECT COUNT(*) FROM opportunity_run_artifacts").fetchone()[0]
        )
    return run_count, artifact_count


def _initialize_schema_through(path: Path, target_version: int) -> None:
    with sqlite3.connect(path) as connection:
        for version, migration in MIGRATIONS:
            if version > target_version:
                break
            migration(connection)
            set_schema_version(connection, version)
        connection.commit()


def _pipeline_with_expectancy_cohort(cohort_id: str) -> PipelineResult:
    dataset = _two_candidate_dataset()
    registry = build_default_registry()
    momentum = registry.get("DS-MOM-001")
    disabled = registry.get("DS-OF-001")
    expectancy = build_expectancy_evidence(
        (Decimal("1"),) * 120 + (Decimal("-1"),) * 80,
        cohort_id=cohort_id,
        min_sample_size=100,
    )
    binding = build_strategy_expectancy_binding(
        decision_at=NOW,
        strategy_definition=momentum,
        evidence=expectancy,
        observed_at=NOW,
        source_identity="bounded-persistence-expectancy-fixture",
        method="fixture cohort calculation",
    )
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(
            dataset,
            requested_symbols=("ABC", "DEF"),
        ),
        registry=StrategyRegistry((momentum, disabled)),
        expectancy_bindings=(binding,),
        sector_by_symbol={"ABC": "technology", "DEF": "industrials"},
        correlation_cluster_by_symbol={"ABC": "cluster-a", "DEF": "cluster-b"},
    )
    risks = {
        item.evaluation_id: _execution_risk_for(item)
        for item in prepared.evaluations
        if item.status is EvaluationStatus.ELIGIBLE
    }
    return run_opportunity_pipeline(
        prepared,
        risk_by_evaluation=risks,
        risk_policy=_pipeline_risk_policy(),
    )


def _drop_artifact_trigger(connection: sqlite3.Connection, operation: str) -> None:
    connection.execute(f"DROP TRIGGER opportunity_run_artifacts_no_{operation}")


def _restore_artifact_trigger(connection: sqlite3.Connection, operation: str) -> None:
    connection.execute(
        f"""
        CREATE TRIGGER opportunity_run_artifacts_no_{operation}
        BEFORE {operation.upper()} ON opportunity_run_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_run_artifacts is append-only');
        END
        """
    )


def _restore_run_update_trigger(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        CREATE TRIGGER opportunity_pipeline_runs_no_update
        BEFORE UPDATE ON opportunity_pipeline_runs
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_pipeline_runs is append-only');
        END
        """
    )


def test_constructor_and_operations_do_not_implicitly_create_or_migrate(
    tmp_path: Path,
    pipeline_result: PipelineResult,
) -> None:
    absent = tmp_path / "absent.sqlite"
    store = OpportunityStore(absent)
    assert not absent.exists()
    with pytest.raises(OpportunityPersistenceIntegrityError, match="initialize"):
        store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    assert not absent.exists()
    with pytest.raises(OpportunityPersistenceIntegrityError, match="initialize"):
        store.load_run(pipeline_result.run_id)
    assert not absent.exists()

    stale = tmp_path / "stale.sqlite"
    with sqlite3.connect(stale) as connection:
        connection.execute(
            "CREATE TABLE schema_version (version INTEGER NOT NULL, applied_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO schema_version VALUES (26, ?)", (RECORDED_AT.isoformat(),))
        connection.commit()
    before_hash = hashlib.sha256(stale.read_bytes()).hexdigest()
    stale_store = OpportunityStore(stale)
    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema 27"):
        stale_store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema 27"):
        stale_store.load_run(pipeline_result.run_id)
    assert hashlib.sha256(stale.read_bytes()).hexdigest() == before_hash


def test_append_load_is_byte_equivalent_and_receipt_round_trips(
    tmp_path: Path,
    pipeline_result: PipelineResult,
) -> None:
    store = _initialized_store(tmp_path / "opportunity.sqlite")
    receipt = store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    loaded = store.load_run(pipeline_result.run_id)

    assert loaded == pipeline_result
    assert loaded is not None
    assert loaded.to_json() == pipeline_result.to_json()
    assert OpportunityPersistenceReceipt.from_json(receipt.to_json()) == receipt
    assert receipt.run_content_hash_sha256 == pipeline_result.content_hash()
    assert receipt.artifact_count == 31
    assert tuple(item.family for item in receipt.family_counts) == (
        CANONICAL_OPPORTUNITY_ARTIFACT_FAMILIES
    )
    assert receipt.recorded_at == RECORDED_AT
    assert store.load_run("opportunity-run:000000000000000000000000") is None


def test_27_to_30_preserves_v1_receipt_and_new_run_uses_v2_28(
    tmp_path: Path,
    pipeline_result: PipelineResult,
) -> None:
    database = tmp_path / "schema-27-to-29.sqlite"
    _initialize_schema_through(database, 27)
    store = OpportunityStore(database)
    inventory = _build_artifact_inventory(pipeline_result)
    inventory_hash = _artifact_inventory_hash(inventory)
    legacy_receipt = _build_persistence_receipt(
        pipeline_result,
        inventory=inventory,
        inventory_hash=inventory_hash,
        recorded_at=RECORDED_AT,
        database_schema_version=27,
    )
    result_json = pipeline_result.to_json()
    receipt_json = legacy_receipt.to_json()
    with store._connect_writable(require_existing=True) as connection:
        store._insert_run(
            connection,
            result=pipeline_result,
            result_json=result_json,
            inventory=inventory,
            inventory_hash=inventory_hash,
            receipt=legacy_receipt,
        )
        store._insert_artifacts(
            connection,
            run_id=pipeline_result.run_id,
            inventory=inventory,
            recorded_at=RECORDED_AT,
        )
        connection.commit()

    with sqlite3.connect(database) as connection:
        assert run_migrations(connection) == 30
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)

    assert store.load_run(pipeline_result.run_id) == pipeline_result
    replayed = store.append_run(
        pipeline_result,
        recorded_at=RECORDED_AT + timedelta(hours=1),
    )
    assert replayed == legacy_receipt
    assert replayed.schema_version == "v2.opportunity.persistence_receipt.v1"
    assert replayed.database_schema_version == 27
    with sqlite3.connect(database) as connection:
        stored = connection.execute(
            "SELECT result_json, receipt_json FROM opportunity_pipeline_runs WHERE run_id = ?",
            (pipeline_result.run_id,),
        ).fetchone()
    assert stored == (result_json, receipt_json)

    new_result = _pipeline_with_expectancy_cohort("post-schema-29")
    new_receipt = store.append_run(
        new_result,
        recorded_at=RECORDED_AT + timedelta(hours=2),
    )
    assert new_receipt.schema_version == "v2.opportunity.persistence_receipt.v2"
    assert new_receipt.database_schema_version == 28
    assert store.load_run(new_result.run_id) == new_result


def test_receipt_direct_contract_rejects_count_time_and_identity_tamper(
    tmp_path: Path,
    pipeline_result: PipelineResult,
) -> None:
    receipt = _initialized_store(tmp_path / "receipt.sqlite").append_run(
        pipeline_result,
        recorded_at=RECORDED_AT,
    )
    with pytest.raises(ValueError, match="identity"):
        replace(receipt, receipt_id="opportunity-persistence-receipt:000000000000000000000000")
    with pytest.raises(ValueError, match="canonical order"):
        replace(receipt, family_counts=tuple(reversed(receipt.family_counts)))
    with pytest.raises(ValueError, match="does not match family counts"):
        replace(receipt, artifact_count=receipt.artifact_count + 1)
    with pytest.raises(ValueError, match="UTC"):
        replace(
            receipt,
            recorded_at=RECORDED_AT.astimezone(ZoneInfo("America/New_York")),
        )
    with pytest.raises(ValueError, match="cannot precede"):
        replace(
            receipt,
            recorded_at=(pipeline_result.decision_at - timedelta(seconds=1)).astimezone(
                timezone.utc
            ),
        )
    payload = receipt.to_dict()
    payload["family_counts"] = payload["family_counts"][:-1]
    with pytest.raises(ValueError, match="canonical order"):
        OpportunityPersistenceReceipt.from_dict(payload)


def test_identical_append_is_write_free_and_preserves_first_receipt_time(
    tmp_path: Path,
    pipeline_result: PipelineResult,
) -> None:
    database = tmp_path / "idempotent.sqlite"
    store = _initialized_store(database)
    first = store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    counts = _row_counts(database)
    second = store.append_run(
        pipeline_result,
        recorded_at=RECORDED_AT + timedelta(hours=3),
    )

    assert second == first
    assert second.recorded_at == RECORDED_AT
    assert _row_counts(database) == counts


@pytest.mark.parametrize("target", ["run", "artifact"])
def test_same_run_or_artifact_content_conflict_leaves_rows_unchanged(
    tmp_path: Path,
    pipeline_result: PipelineResult,
    target: str,
) -> None:
    database = tmp_path / f"conflict-{target}.sqlite"
    store = _initialized_store(database)
    store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    with sqlite3.connect(database) as connection:
        if target == "run":
            connection.execute("DROP TRIGGER opportunity_pipeline_runs_no_update")
            connection.execute(
                """
                UPDATE opportunity_pipeline_runs
                SET result_json = '{}', result_content_hash_sha256 = ?
                """,
                ("0" * 64,),
            )
            _restore_run_update_trigger(connection)
        else:
            _drop_artifact_trigger(connection, "update")
            connection.execute(
                """
                UPDATE opportunity_run_artifacts SET payload_json = '{}'
                WHERE run_id = ? AND inventory_ordinal = 0
                """,
                (pipeline_result.run_id,),
            )
            _restore_artifact_trigger(connection, "update")
        connection.commit()
    before = _row_counts(database)
    with pytest.raises(OpportunityPersistenceConflictError):
        store.append_run(pipeline_result, recorded_at=RECORDED_AT + timedelta(minutes=1))
    assert _row_counts(database) == before


def test_distinct_runs_may_share_evaluation_id_with_different_content_hash(
    tmp_path: Path,
) -> None:
    first = _pipeline_with_expectancy_cohort("persistence-cohort-a")
    second = _pipeline_with_expectancy_cohort("persistence-cohort-b")
    assert first.run_id != second.run_id
    first_by_id = {item.evaluation_id: item.content_hash() for item in first.evaluations}
    second_by_id = {item.evaluation_id: item.content_hash() for item in second.evaluations}
    shared_different = {
        evaluation_id
        for evaluation_id in first_by_id.keys() & second_by_id.keys()
        if first_by_id[evaluation_id] != second_by_id[evaluation_id]
    }
    assert shared_different

    database = tmp_path / "cross-run-identity.sqlite"
    store = _initialized_store(database)
    store.append_run(first, recorded_at=RECORDED_AT)
    store.append_run(second, recorded_at=RECORDED_AT + timedelta(minutes=1))
    assert store.load_run(first.run_id) == first
    assert store.load_run(second.run_id) == second
    with sqlite3.connect(database) as connection:
        for evaluation_id in shared_different:
            hashes = connection.execute(
                """
                SELECT DISTINCT content_hash_sha256
                FROM opportunity_run_artifacts
                WHERE artifact_family = 'strategy_evaluation' AND artifact_id = ?
                """,
                (evaluation_id,),
            ).fetchall()
            assert len(hashes) == 2


def test_injected_artifact_failure_rolls_back_parent_and_partial_rows(
    tmp_path: Path,
    pipeline_result: PipelineResult,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = tmp_path / "rollback.sqlite"
    store = _initialized_store(database)
    original = OpportunityStore._insert_artifacts

    def fail_after_first(
        self: OpportunityStore,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        inventory: tuple[object, ...],
        recorded_at: datetime,
    ) -> None:
        original(
            self,
            connection,
            run_id=run_id,
            inventory=inventory[:1],  # type: ignore[arg-type]
            recorded_at=recorded_at,
        )
        raise RuntimeError("injected persistence failure")

    monkeypatch.setattr(OpportunityStore, "_insert_artifacts", fail_after_first)
    with pytest.raises(RuntimeError, match="injected persistence failure"):
        store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    assert _row_counts(database) == (0, 0)
    with sqlite3.connect(database) as connection:
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)


def test_database_triggers_reject_update_and_delete_on_both_tables(
    tmp_path: Path,
    pipeline_result: PipelineResult,
) -> None:
    database = tmp_path / "triggers.sqlite"
    store = _initialized_store(database)
    store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    statements = (
        "UPDATE opportunity_pipeline_runs SET dataset_id = 'changed'",
        "DELETE FROM opportunity_pipeline_runs",
        "UPDATE opportunity_run_artifacts SET payload_json = '{}'",
        "DELETE FROM opportunity_run_artifacts",
    )
    for statement in statements:
        with sqlite3.connect(database) as connection:
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                connection.execute(statement)
    assert _row_counts(database) == (1, 31)


@pytest.mark.parametrize(
    ("trigger_name", "replacement_sql"),
    (
        (
            "opportunity_pipeline_runs_no_update",
            """
            CREATE TRIGGER opportunity_pipeline_runs_no_update
            AFTER INSERT ON opportunity_pipeline_runs
            BEGIN
                SELECT 1;
            END
            """,
        ),
        (
            "opportunity_pipeline_runs_no_delete",
            """
            CREATE TRIGGER opportunity_pipeline_runs_no_delete
            BEFORE UPDATE ON opportunity_pipeline_runs
            BEGIN
                SELECT RAISE(ABORT, 'opportunity_pipeline_runs is append-only');
            END
            """,
        ),
        (
            "opportunity_run_artifacts_no_update",
            """
            CREATE TRIGGER opportunity_run_artifacts_no_update
            BEFORE UPDATE ON opportunity_pipeline_runs
            BEGIN
                SELECT RAISE(ABORT, 'opportunity_run_artifacts is append-only');
            END
            """,
        ),
        (
            "opportunity_run_artifacts_no_delete",
            """
            CREATE TRIGGER opportunity_run_artifacts_no_delete
            BEFORE DELETE ON opportunity_run_artifacts
            BEGIN
                SELECT 1;
            END
            """,
        ),
    ),
)
def test_same_named_forged_append_only_trigger_fails_every_store_entrypoint(
    tmp_path: Path,
    pipeline_result: PipelineResult,
    trigger_name: str,
    replacement_sql: str,
) -> None:
    database = tmp_path / f"forged-{trigger_name}.sqlite"
    store = _initialized_store(database)
    store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(replacement_sql)
        connection.commit()

    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema object"):
        store.initialize()
    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema object"):
        store.load_run(pipeline_result.run_id)
    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema object"):
        store.append_run(
            pipeline_result,
            recorded_at=RECORDED_AT + timedelta(minutes=1),
        )


@pytest.mark.parametrize(
    ("original_literal", "altered_literal"),
    (
        ("'universe_snapshot'", "'UNIVERSE_SNAPSHOT'"),
        ("'decision_trace'", "'decision trace'"),
    ),
)
def test_case_or_whitespace_changed_family_check_literal_fails_every_entrypoint(
    tmp_path: Path,
    pipeline_result: PipelineResult,
    original_literal: str,
    altered_literal: str,
) -> None:
    database = tmp_path / f"altered-family-check-{altered_literal[1:-1]}.sqlite"
    store = _initialized_store(database)
    with sqlite3.connect(database) as connection:
        objects = connection.execute(
            """
            SELECT type, sql FROM sqlite_master
            WHERE tbl_name = 'opportunity_run_artifacts' AND sql IS NOT NULL
            ORDER BY CASE type WHEN 'table' THEN 0 WHEN 'index' THEN 1 ELSE 2 END,
                     name
            """
        ).fetchall()
        table_sql = str(objects[0][1])
        assert original_literal in table_sql
        connection.execute("DROP TABLE opportunity_run_artifacts")
        connection.execute(table_sql.replace(original_literal, altered_literal, 1))
        for _object_type, object_sql in objects[1:]:
            connection.execute(str(object_sql))
        connection.commit()

    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema object"):
        store.initialize()
    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema object"):
        store.load_run("opportunity-run:000000000000000000000000")
    with pytest.raises(OpportunityPersistenceIntegrityError, match="schema object"):
        store.append_run(pipeline_result, recorded_at=RECORDED_AT)


@pytest.mark.parametrize("tamper", ["missing", "extra", "reordered", "content"])
def test_load_rejects_missing_extra_reordered_or_tampered_artifacts(
    tmp_path: Path,
    pipeline_result: PipelineResult,
    tamper: str,
) -> None:
    database = tmp_path / f"tamper-{tamper}.sqlite"
    store = _initialized_store(database)
    store.append_run(pipeline_result, recorded_at=RECORDED_AT)
    with sqlite3.connect(database) as connection:
        if tamper == "missing":
            _drop_artifact_trigger(connection, "delete")
            connection.execute(
                "DELETE FROM opportunity_run_artifacts WHERE run_id = ? AND inventory_ordinal = 0",
                (pipeline_result.run_id,),
            )
            _restore_artifact_trigger(connection, "delete")
        elif tamper == "extra":
            connection.execute(
                """
                INSERT INTO opportunity_run_artifacts (
                    run_id, inventory_ordinal, artifact_family, family_ordinal,
                    artifact_id, evaluation_id, decision_id,
                    artifact_schema_version, payload_json, content_hash_sha256,
                    first_recorded_at
                )
                SELECT run_id, 9999, artifact_family, 9999,
                       'tampered-extra-artifact', NULL, NULL,
                       artifact_schema_version, '{}', ?, first_recorded_at
                FROM opportunity_run_artifacts
                WHERE run_id = ? AND inventory_ordinal = 0
                """,
                ("0" * 64, pipeline_result.run_id),
            )
        elif tamper == "reordered":
            _drop_artifact_trigger(connection, "update")
            connection.execute(
                """
                UPDATE opportunity_run_artifacts SET family_ordinal = 9999
                WHERE run_id = ? AND inventory_ordinal = 0
                """,
                (pipeline_result.run_id,),
            )
            _restore_artifact_trigger(connection, "update")
        else:
            _drop_artifact_trigger(connection, "update")
            connection.execute(
                """
                UPDATE opportunity_run_artifacts SET payload_json = '{}'
                WHERE run_id = ? AND inventory_ordinal = 0
                """,
                (pipeline_result.run_id,),
            )
            _restore_artifact_trigger(connection, "update")
        connection.commit()
    with pytest.raises(OpportunityPersistenceIntegrityError, match="artifact inventory"):
        store.load_run(pipeline_result.run_id)


def test_read_only_load_succeeds_and_all_write_paths_reject(
    tmp_path: Path,
    pipeline_result: PipelineResult,
) -> None:
    database = tmp_path / "read-only.sqlite"
    writable = _initialized_store(database)
    writable.append_run(pipeline_result, recorded_at=RECORDED_AT)
    read_only = OpportunityStore(database, read_only=True)

    assert read_only.load_run(pipeline_result.run_id) == pipeline_result
    with pytest.raises(OpportunityStoreReadOnlyError, match="initialize"):
        read_only.initialize()
    with pytest.raises(OpportunityStoreReadOnlyError, match="append"):
        read_only.append_run(pipeline_result, recorded_at=RECORDED_AT)


def test_empty_universe_run_persists_with_zero_optional_family_counts(
    tmp_path: Path,
) -> None:
    dataset = _pipeline_dataset()
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(dataset, requested_symbols=()),
        registry=StrategyRegistry(()),
    )
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    assert result.evaluations == ()
    assert result.decision_context is None
    store = _initialized_store(tmp_path / "empty.sqlite")
    receipt = store.append_run(result, recorded_at=RECORDED_AT)
    counts = {item.family: item.count for item in receipt.family_counts}

    for family in (
        OpportunityArtifactFamily.OPPORTUNITY_CANDIDATE,
        OpportunityArtifactFamily.SECURITY_REGIME,
        OpportunityArtifactFamily.STRATEGY_EVALUATION,
        OpportunityArtifactFamily.RANKED_OPPORTUNITY,
        OpportunityArtifactFamily.EXECUTION_RISK_EVIDENCE,
        OpportunityArtifactFamily.DECISION_RUN_CONTEXT,
        OpportunityArtifactFamily.TRADE_DECISION,
        OpportunityArtifactFamily.DECISION_TRACE,
    ):
        assert counts[family] == 0
    assert counts[OpportunityArtifactFamily.UNIVERSE_SNAPSHOT] == 1
    assert counts[OpportunityArtifactFamily.PREPARED_PIPELINE] == 1
    assert counts[OpportunityArtifactFamily.MARKET_REGIME] == 1
    assert counts[OpportunityArtifactFamily.PIPELINE_RISK_POLICY] == 1
    loaded = store.load_run(result.run_id)
    assert loaded == result
    assert loaded is not None and loaded.to_json() == result.to_json()


def test_persistence_module_has_no_forbidden_runtime_or_future_label_imports() -> None:
    source_path = (
        Path(__file__).parents[1]
        / "intraday_scanner"
        / "storage"
        / "opportunity_store.py"
    )
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    imported = {
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    }
    imported.update(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    forbidden = (
        "intraday_scanner.v2.contracts.outcomes",
        "intraday_scanner.v2.opportunity.backtest",
        "streamlit",
        "requests",
        "intraday_scanner.broker",
        "intraday_scanner.network",
    )
    assert not any(name.startswith(forbidden) for name in imported)
    assert "SQLiteScanStore" not in source_path.read_text(encoding="utf-8")
