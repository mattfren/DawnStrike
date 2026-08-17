from __future__ import annotations

import ast
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import replace
from datetime import timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.opportunity_outcome_inventory import (
    build_outcome_inventory,
    build_outcome_receipt,
    outcome_inventory_hash,
)
from intraday_scanner.storage.opportunity_outcome_schema import (
    expected_outcome_schema_sql,
)
from intraday_scanner.storage.opportunity_outcome_store import (
    OpportunityOutcomeConflictError,
    OpportunityOutcomeIntegrityError,
    OpportunityOutcomeReadOnlyError,
    OpportunityOutcomeStore,
)
from intraday_scanner.storage.opportunity_store import (
    OpportunityStore,
    _artifact_inventory_hash,
    _build_artifact_inventory,
)
from intraday_scanner.v2.opportunity.models import stable_identity
from intraday_scanner.v2.opportunity.outcome_persistence import (
    CurrentOutcomeReplay,
    HistoricalOutcomeReplay,
    OpportunityOutcomePersistenceReceipt,
    OutcomeArtifactFamily,
    OutcomeArtifactFamilyCount,
    OutcomePersistenceKind,
)
from intraday_scanner.v2.opportunity.outcomes import (
    OutcomeHorizonKind,
    build_outcome_horizon,
    build_outcome_label_policy,
    build_outcome_observation_dataset,
    label_pipeline_outcomes,
)
from intraday_scanner.v2.opportunity.pipeline import (
    _pipeline_run_values,
    prepare_opportunity_pipeline,
    run_opportunity_pipeline,
)
from intraday_scanner.v2.opportunity.registry import StrategyRegistry
from tests.test_opportunity_outcomes import _batch, _receipt
from tests.test_opportunity_persistence import _initialize_schema_through
from tests.test_opportunity_pipeline import (
    _pipeline_dataset,
    _pipeline_risk_policy,
    _pipeline_universe,
)


def _initialized_batch_store(path: Path):
    batch = _batch()
    run_store = OpportunityStore(path)
    run_store.initialize()
    receipt = run_store.append_run(
        batch.pipeline_result,
        recorded_at=batch.persistence_receipt.recorded_at,
    )
    assert receipt == batch.persistence_receipt
    return batch, OpportunityOutcomeStore(path)


@pytest.fixture(scope="module")
def persisted_replay(tmp_path_factory: pytest.TempPathFactory):
    batch, store = _initialized_batch_store(
        tmp_path_factory.mktemp("outcome-replay") / "projection.sqlite"
    )
    receipt = store.append_batch(
        batch,
        persisted_at=batch.recorded_at + timedelta(seconds=1),
    )
    replay = store.replay_historical(receipt.outcome_receipt_id)
    assert replay is not None
    return receipt, replay, store


def test_28_to_30_preserves_history_and_keeps_governed_receipt_generations(
    tmp_path: Path,
) -> None:
    initial = _batch(missing_symbol="ABC")
    database = tmp_path / "schema-28-to-29.sqlite"
    _initialize_schema_through(database, 28)
    run_store = OpportunityStore(database)
    run_inventory = _build_artifact_inventory(initial.pipeline_result)
    run_inventory_hash = _artifact_inventory_hash(run_inventory)
    with run_store._connect_writable(require_existing=True) as connection:
        run_store._insert_run(
            connection,
            result=initial.pipeline_result,
            result_json=initial.pipeline_result.to_json(),
            inventory=run_inventory,
            inventory_hash=run_inventory_hash,
            receipt=initial.persistence_receipt,
        )
        run_store._insert_artifacts(
            connection,
            run_id=initial.pipeline_result.run_id,
            inventory=run_inventory,
            recorded_at=initial.persistence_receipt.recorded_at,
        )
        connection.commit()

    persisted_at = initial.recorded_at + timedelta(seconds=1)
    outcome_inventory = build_outcome_inventory(
        initial,
        predecessor_receipt=None,
        predecessor_by_pair={},
    )
    outcome_receipt = build_outcome_receipt(
        initial,
        persisted_at=persisted_at,
        inventory=outcome_inventory,
        inventory_hash=outcome_inventory_hash(outcome_inventory),
        predecessor=None,
    )
    outcome_store = OpportunityOutcomeStore(database)
    with outcome_store._connect_writable(require_existing=True) as connection:
        outcome_store._insert_receipt(
            connection,
            batch=initial,
            batch_json=initial.to_json(),
            receipt=outcome_receipt,
        )
        outcome_store._insert_records(
            connection,
            batch=initial,
            receipt=outcome_receipt,
            predecessor_by_pair={},
        )
        connection.commit()

    with sqlite3.connect(database) as connection:
        run_before = connection.execute(
            "SELECT result_json, receipt_json, first_recorded_at "
            "FROM opportunity_pipeline_runs WHERE run_id = ?",
            (initial.pipeline_result.run_id,),
        ).fetchone()
        outcome_before = connection.execute(
            "SELECT batch_json, receipt_json, persisted_at "
            "FROM opportunity_outcome_receipts WHERE outcome_receipt_id = ?",
            (outcome_receipt.outcome_receipt_id,),
        ).fetchone()
        assert run_migrations(connection) == 30
        assert run_migrations(connection) == 30
        assert connection.execute("PRAGMA quick_check").fetchone() == ("ok",)
        run_after = connection.execute(
            "SELECT result_json, receipt_json, first_recorded_at "
            "FROM opportunity_pipeline_runs WHERE run_id = ?",
            (initial.pipeline_result.run_id,),
        ).fetchone()
        outcome_after = connection.execute(
            "SELECT batch_json, receipt_json, persisted_at "
            "FROM opportunity_outcome_receipts WHERE outcome_receipt_id = ?",
            (outcome_receipt.outcome_receipt_id,),
        ).fetchone()
    assert run_after == run_before
    assert outcome_after == outcome_before
    assert run_store.load_run(initial.pipeline_result.run_id) == initial.pipeline_result
    assert outcome_store.load_receipt(outcome_receipt.outcome_receipt_id) == outcome_receipt
    assert run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at + timedelta(hours=1),
    ) == initial.persistence_receipt
    assert outcome_store.append_batch(
        initial,
        persisted_at=persisted_at + timedelta(hours=1),
    ) == outcome_receipt

    new_initial = _batch_with_distinct_run(
        _batch(missing_symbol="ABC"),
        "post_schema_29",
    )
    new_correction = _batch_with_distinct_run(_batch(), "post_schema_29")
    new_run_receipt = run_store.append_run(
        new_initial.pipeline_result,
        recorded_at=new_initial.persistence_receipt.recorded_at,
    )
    assert new_run_receipt.schema_version == "v2.opportunity.persistence_receipt.v2"
    assert new_run_receipt.database_schema_version == 28
    new_outcome_receipt = outcome_store.append_batch(
        new_initial,
        persisted_at=new_initial.recorded_at + timedelta(seconds=1),
    )
    corrected_outcome_receipt = outcome_store.append_batch(
        new_correction,
        persisted_at=new_correction.recorded_at + timedelta(seconds=2),
        supersedes_outcome_receipt_id=new_outcome_receipt.outcome_receipt_id,
    )
    for receipt in (new_outcome_receipt, corrected_outcome_receipt):
        assert receipt.schema_version == "v2.opportunity.outcome_persistence_receipt.v1"
        assert receipt.database_schema_version == 28


def _rehash_receipt(
    receipt: OpportunityOutcomePersistenceReceipt,
    **changes: object,
) -> OpportunityOutcomePersistenceReceipt:
    values = {**receipt.__dict__, **changes}
    values.pop("outcome_receipt_id")
    return replace(
        receipt,
        outcome_receipt_id=stable_identity(
            "opportunity-outcome-persistence-receipt",
            values,
        ),
        **changes,
    )


def _rehash_historical_replay(
    replay: HistoricalOutcomeReplay,
    **changes: object,
) -> HistoricalOutcomeReplay:
    values = {**replay.__dict__, **changes}
    values.pop("replay_id")
    return replace(
        replay,
        replay_id=stable_identity(
            "historical-opportunity-outcome-replay",
            values,
        ),
        **changes,
    )


def _multi_horizon_batch(batch):
    full = batch.horizons[0]
    early = build_outcome_horizon(
        decision_at=batch.pipeline_result.decision_at,
        exchange_session_id=full.exchange_session_id,
        session_open_at=full.session_open_at,
        session_close_at=full.session_close_at,
        kind=OutcomeHorizonKind.ELAPSED_SECONDS,
        elapsed_seconds=121,
    )
    return label_pipeline_outcomes(
        pipeline_result=batch.pipeline_result,
        persistence_receipt=batch.persistence_receipt,
        source_dataset=batch.source_dataset,
        policy=batch.policy,
        horizons=(early, full),
        recorded_at=batch.recorded_at,
        limitations=batch.limitations,
    )


def _batch_with_distinct_run(batch, limitation: str):
    result = batch.pipeline_result
    limitations = (*result.limitations, limitation)
    result_values = _pipeline_run_values(
        preparation=result.preparation,
        risk_policy=result.risk_policy,
        gate_config=result.gate_config,
        risk_evidence=result.risk_evidence,
        decision_context=result.decision_context,
        decisions=result.decisions,
        traces=result.traces,
        limitations=limitations,
    )
    distinct_result = replace(
        result,
        run_id=stable_identity("opportunity-run", result_values),
        limitations=limitations,
    )
    receipt = _receipt(
        distinct_result,
        batch.persistence_receipt.recorded_at,
    )
    return label_pipeline_outcomes(
        pipeline_result=distinct_result,
        persistence_receipt=receipt,
        source_dataset=batch.source_dataset,
        policy=batch.policy,
        horizons=batch.horizons,
        recorded_at=batch.recorded_at,
        limitations=batch.limitations,
    )


def _assert_schema_rejected_on_all_public_paths(
    store: OpportunityOutcomeStore,
    batch,
) -> None:
    with pytest.raises(OpportunityOutcomeIntegrityError):
        store.initialize()
    with pytest.raises(OpportunityOutcomeIntegrityError):
        store.load_receipt("outcome-receipt:000000000000000000000000")
    with pytest.raises(OpportunityOutcomeIntegrityError):
        store.replay_current(batch.pipeline_result.run_id)
    with pytest.raises(OpportunityOutcomeIntegrityError):
        store.append_batch(
            batch,
            persisted_at=batch.recorded_at + timedelta(seconds=1),
        )


def test_initial_append_load_and_historical_replay_are_byte_equivalent(
    tmp_path: Path,
) -> None:
    batch, store = _initialized_batch_store(tmp_path / "outcomes.sqlite")
    persisted_at = batch.recorded_at + timedelta(seconds=1)
    receipt = store.append_batch(batch, persisted_at=persisted_at)

    assert store.load_receipt(receipt.outcome_receipt_id) == receipt
    loaded = store.load_batch(receipt.outcome_receipt_id)
    assert loaded == batch
    assert loaded is not None and loaded.to_json() == batch.to_json()
    assert store.load_current_receipt(batch.pipeline_result.run_id) == receipt
    assert store.load_current_outcomes(batch.pipeline_result.run_id) == batch.outcomes
    replay = store.replay_historical(receipt.outcome_receipt_id)
    assert replay is not None
    assert replay.outcome_batch.to_json() == batch.to_json()
    assert HistoricalOutcomeReplay.from_json(replay.to_json()) == replay

    repeated = store.append_batch(
        batch,
        persisted_at=persisted_at + timedelta(hours=1),
    )
    assert repeated == receipt
    assert repeated.persisted_at == persisted_at


def test_receipt_requires_exact_family_allocation_and_chronology(tmp_path: Path) -> None:
    batch, store = _initialized_batch_store(tmp_path / "receipt-contract.sqlite")
    receipt = store.append_batch(
        batch,
        persisted_at=batch.recorded_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="family allocation"):
        replace(
            receipt,
            family_counts=(
                OutcomeArtifactFamilyCount(
                    family=OutcomeArtifactFamily.OUTCOME_LABEL_BATCH,
                    count=0,
                ),
                OutcomeArtifactFamilyCount(
                    family=OutcomeArtifactFamily.OUTCOME_RECORD,
                    count=receipt.record_count + 1,
                ),
            ),
        )
    with pytest.raises(ValueError, match="decision_at"):
        replace(
            receipt,
            batch_recorded_at=receipt.decision_at.astimezone(
                receipt.batch_recorded_at.tzinfo
            )
            - timedelta(microseconds=1),
        )
    with pytest.raises(ValueError, match="batch schema"):
        _rehash_receipt(receipt, batch_schema_version="future-batch-schema")


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("batch_id", "outcome-label-batch:000000000000000000000000"),
        ("batch_content_hash_sha256", "0" * 64),
        ("run_id", "opportunity-run:000000000000000000000000"),
        ("run_content_hash_sha256", "1" * 64),
        ("run_persistence_receipt_id", "receipt:000000000000000000000000"),
        ("run_persistence_receipt_content_hash_sha256", "2" * 64),
        ("source_dataset_id", "outcome-source-dataset:000000000000000000000000"),
        ("source_dataset_content_hash_sha256", "3" * 64),
        ("policy_id", "outcome-label-policy:000000000000000000000000"),
        ("policy_content_hash_sha256", "4" * 64),
    ),
)
def test_replay_rejects_consistently_rehashed_receipt_projection_tamper(
    persisted_replay,
    field: str,
    value: object,
) -> None:
    receipt, replay, _store = persisted_replay
    forged_receipt = _rehash_receipt(receipt, **{field: value})
    with pytest.raises(ValueError, match="does not match embedded replay objects"):
        _rehash_historical_replay(
            replay,
            outcome_persistence_receipt=forged_receipt,
            chain_prefix=(forged_receipt,),
        )


def test_replay_rejects_valid_but_wrong_record_count_projection(
    persisted_replay,
) -> None:
    receipt, replay, _store = persisted_replay
    for field, value in (
        ("decision_at", receipt.decision_at + timedelta(microseconds=1)),
        (
            "batch_recorded_at",
            receipt.batch_recorded_at + timedelta(microseconds=1),
        ),
    ):
        forged_time = _rehash_receipt(receipt, **{field: value})
        with pytest.raises(ValueError, match="does not match embedded replay objects"):
            _rehash_historical_replay(
                replay,
                outcome_persistence_receipt=forged_time,
                chain_prefix=(forged_time,),
            )
    forged_count = receipt.record_count + 1
    forged_receipt = _rehash_receipt(
        receipt,
        record_count=forged_count,
        artifact_count=forged_count + 1,
        family_counts=(
            OutcomeArtifactFamilyCount(
                family=OutcomeArtifactFamily.OUTCOME_LABEL_BATCH,
                count=1,
            ),
            OutcomeArtifactFamilyCount(
                family=OutcomeArtifactFamily.OUTCOME_RECORD,
                count=forged_count,
            ),
        ),
    )
    with pytest.raises(ValueError, match="does not match embedded replay objects"):
        _rehash_historical_replay(
            replay,
            outcome_persistence_receipt=forged_receipt,
            chain_prefix=(forged_receipt,),
        )
    with pytest.raises(ValueError, match="research_only"):
        _rehash_receipt(receipt, research_only=False)


def test_correction_chain_preserves_historical_and_advances_current(
    tmp_path: Path,
) -> None:
    initial = _batch(missing_symbol="ABC")
    correction = _batch()
    database = tmp_path / "correction.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    assert run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at,
    ) == initial.persistence_receipt
    store = OpportunityOutcomeStore(database)
    first = store.append_batch(
        initial,
        persisted_at=initial.recorded_at + timedelta(seconds=1),
    )
    historical_before = store.replay_historical(first.outcome_receipt_id)
    assert historical_before is not None

    second = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=2),
        supersedes_outcome_receipt_id=first.outcome_receipt_id,
    )
    assert second.receipt_kind is OutcomePersistenceKind.CORRECTION
    assert second.supersedes_outcome_receipt_content_hash_sha256 == first.content_hash()
    assert store.load_current_receipt(initial.pipeline_result.run_id) == second
    assert store.load_current_outcomes(initial.pipeline_result.run_id) == correction.outcomes
    assert store.replay_historical(first.outcome_receipt_id) == historical_before
    current = store.replay_current(initial.pipeline_result.run_id)
    assert current is not None
    assert current.full_chain == (first, second)
    assert current.outcome_batch == correction
    assert type(current).from_json(current.to_json()) == current
    forged_second = _rehash_receipt(second, batch_content_hash_sha256="9" * 64)
    forged_values = {
        **current.__dict__,
        "outcome_persistence_receipt": forged_second,
        "full_chain": (first, forged_second),
    }
    forged_values.pop("replay_id")
    with pytest.raises(ValueError, match="does not match embedded replay objects"):
        replace(
            current,
            replay_id=stable_identity(
                "current-opportunity-outcome-replay",
                forged_values,
            ),
            outcome_persistence_receipt=forged_second,
            full_chain=(first, forged_second),
        )
    payload = current.to_dict()
    payload["outcome_persistence_receipt"] = forged_second.to_dict()
    payload["full_chain"][-1] = forged_second.to_dict()
    payload["replay_id"] = stable_identity(
        "current-opportunity-outcome-replay",
        {name: value for name, value in payload.items() if name != "replay_id"},
    )
    with pytest.raises(ValueError, match="does not match embedded replay objects"):
        CurrentOutcomeReplay.from_dict(payload)

    repeated = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(hours=1),
        supersedes_outcome_receipt_id=first.outcome_receipt_id,
    )
    assert repeated == second
    assert repeated.persisted_at == second.persisted_at


def test_correction_rejects_stale_head_and_unchanged_overlap(tmp_path: Path) -> None:
    initial = _batch(missing_symbol="ABC")
    correction = _batch()
    database = tmp_path / "correction-conflicts.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at,
    )
    store = OpportunityOutcomeStore(database)
    first = store.append_batch(
        initial,
        persisted_at=initial.recorded_at + timedelta(seconds=1),
    )
    second = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=2),
        supersedes_outcome_receipt_id=first.outcome_receipt_id,
    )
    third_candidate = _batch(missing_symbol="DEF")
    with pytest.raises(OpportunityOutcomeConflictError, match="current receipt head"):
        store.append_batch(
            third_candidate,
            persisted_at=third_candidate.recorded_at + timedelta(seconds=3),
            supersedes_outcome_receipt_id=first.outcome_receipt_id,
        )

    values = {**correction.__dict__, "limitations": (*correction.limitations, "correction_note")}
    values.pop("batch_id")
    unchanged_outcomes = replace(
        correction,
        batch_id=stable_identity("outcome-label-batch", values),
        limitations=(*correction.limitations, "correction_note"),
    )
    with pytest.raises(
        OpportunityOutcomeConflictError,
        match="change every overlapping outcome",
    ):
        store.append_batch(
            unchanged_outcomes,
            persisted_at=unchanged_outcomes.recorded_at + timedelta(seconds=3),
            supersedes_outcome_receipt_id=second.outcome_receipt_id,
        )


def test_correction_chronology_pair_superset_added_roots_and_three_revision_leaves(
    tmp_path: Path,
) -> None:
    initial = _batch(missing_symbol="ABC")
    correction = _multi_horizon_batch(_batch())
    third_batch = _multi_horizon_batch(_batch(missing_symbol="DEF"))
    database = tmp_path / "three-revisions.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at,
    )
    store = OpportunityOutcomeStore(database)
    first = store.append_batch(
        initial,
        persisted_at=initial.recorded_at + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="must follow predecessor"):
        store.append_batch(
            correction,
            persisted_at=first.persisted_at,
            supersedes_outcome_receipt_id=first.outcome_receipt_id,
        )
    with pytest.raises(ValueError, match="must follow predecessor"):
        store.append_batch(
            correction,
            persisted_at=first.persisted_at - timedelta(microseconds=1),
            supersedes_outcome_receipt_id=first.outcome_receipt_id,
        )
    second = store.append_batch(
        correction,
        persisted_at=first.persisted_at + timedelta(seconds=1),
        supersedes_outcome_receipt_id=first.outcome_receipt_id,
    )
    early_horizon_id = correction.horizons[0].horizon_id
    full_horizon_id = correction.horizons[1].horizon_id
    with sqlite3.connect(database) as connection:
        lineage = connection.execute(
            "SELECT horizon_id, supersedes_outcome_receipt_id "
            "FROM opportunity_outcome_records WHERE outcome_receipt_id = ?",
            (second.outcome_receipt_id,),
        ).fetchall()
    assert all(
        predecessor is None
        for horizon_id, predecessor in lineage
        if horizon_id == early_horizon_id
    )
    assert all(
        predecessor == first.outcome_receipt_id
        for horizon_id, predecessor in lineage
        if horizon_id == full_horizon_id
    )
    third = store.append_batch(
        third_batch,
        persisted_at=second.persisted_at + timedelta(seconds=1),
        supersedes_outcome_receipt_id=second.outcome_receipt_id,
    )
    current = store.replay_current(initial.pipeline_result.run_id)
    assert current is not None
    assert current.full_chain == (first, second, third)
    assert current.outcome_batch == third_batch
    with sqlite3.connect(database) as connection:
        leaves = int(
            connection.execute(
                """
                SELECT COUNT(*)
                FROM opportunity_outcome_records AS parent
                LEFT JOIN opportunity_outcome_records AS child
                  ON child.supersedes_outcome_receipt_id = parent.outcome_receipt_id
                 AND child.supersedes_outcome_id = parent.outcome_id
                 AND child.supersedes_outcome_content_hash_sha256 =
                     parent.outcome_content_hash_sha256
                WHERE parent.run_id = ? AND child.outcome_id IS NULL
                """,
                (initial.pipeline_result.run_id,),
            ).fetchone()[0]
        )
    assert leaves == len(third_batch.outcomes)

    dropped_pairs = _batch()
    with pytest.raises(OpportunityOutcomeConflictError, match="cannot drop"):
        store.append_batch(
            dropped_pairs,
            persisted_at=third.persisted_at + timedelta(seconds=1),
            supersedes_outcome_receipt_id=third.outcome_receipt_id,
        )


def test_cross_run_predecessor_is_rejected(tmp_path: Path) -> None:
    initial = _batch(missing_symbol="ABC")
    correction = _batch()
    other = _batch_with_distinct_run(correction, "distinct_run_fixture")
    database = tmp_path / "cross-run.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at,
    )
    run_store.append_run(
        other.pipeline_result,
        recorded_at=other.persistence_receipt.recorded_at,
    )
    store = OpportunityOutcomeStore(database)
    first = store.append_batch(
        initial,
        persisted_at=initial.recorded_at + timedelta(seconds=1),
    )
    other_receipt = store.append_batch(
        other,
        persisted_at=other.recorded_at + timedelta(seconds=1),
    )
    assert other_receipt.run_id != first.run_id
    with pytest.raises(OpportunityOutcomeConflictError, match="current receipt head"):
        store.append_batch(
            correction,
            persisted_at=correction.recorded_at + timedelta(seconds=2),
            supersedes_outcome_receipt_id=other_receipt.outcome_receipt_id,
        )


def test_replay_chain_direct_contract_rejects_omission_reorder_duplicate_and_cross_run(
    tmp_path: Path,
) -> None:
    initial = _batch(missing_symbol="ABC")
    correction = _batch()
    database = tmp_path / "chain-contract.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at,
    )
    store = OpportunityOutcomeStore(database)
    first = store.append_batch(
        initial,
        persisted_at=initial.recorded_at + timedelta(seconds=1),
    )
    second = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=2),
        supersedes_outcome_receipt_id=first.outcome_receipt_id,
    )
    replay = store.replay_historical(second.outcome_receipt_id)
    assert replay is not None
    with pytest.raises(ValueError, match="begin with an initial"):
        _rehash_historical_replay(replay, chain_prefix=(second,))
    with pytest.raises(ValueError, match="end at its requested"):
        _rehash_historical_replay(replay, chain_prefix=(second, first))
    with pytest.raises(ValueError, match="duplicate"):
        _rehash_historical_replay(replay, chain_prefix=(first, first, second))
    cross_run_first = _rehash_receipt(
        first,
        run_id="opportunity-run:000000000000000000000000",
    )
    with pytest.raises(ValueError, match="cross-run"):
        _rehash_historical_replay(
            replay,
            chain_prefix=(cross_run_first, second),
        )


def test_identical_correction_reappend_audits_corrupted_predecessor(tmp_path: Path) -> None:
    initial = _batch(missing_symbol="ABC")
    correction = _batch()
    database = tmp_path / "idempotent-prefix-audit.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at,
    )
    store = OpportunityOutcomeStore(database)
    first = store.append_batch(
        initial,
        persisted_at=initial.recorded_at + timedelta(seconds=1),
    )
    store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=2),
        supersedes_outcome_receipt_id=first.outcome_receipt_id,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("DROP TRIGGER opportunity_outcome_receipts_no_update")
        connection.execute(
            "UPDATE opportunity_outcome_receipts SET receipt_json = '{}' "
            "WHERE outcome_receipt_id = ?",
            (first.outcome_receipt_id,),
        )
        connection.execute(
            """
            CREATE TRIGGER opportunity_outcome_receipts_no_update
            BEFORE UPDATE ON opportunity_outcome_receipts
            BEGIN
                SELECT RAISE(ABORT, 'opportunity_outcome_receipts is append-only');
            END
            """
        )
        connection.commit()
    with pytest.raises(OpportunityOutcomeIntegrityError, match="receipt or batch JSON"):
        store.append_batch(
            correction,
            persisted_at=correction.recorded_at + timedelta(hours=1),
            supersedes_outcome_receipt_id=first.outcome_receipt_id,
        )


def test_no_head_cycle_is_never_treated_as_empty_history(tmp_path: Path) -> None:
    initial = _batch(missing_symbol="ABC")
    correction = _batch()
    database = tmp_path / "cycle.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    run_store.append_run(
        initial.pipeline_result,
        recorded_at=initial.persistence_receipt.recorded_at,
    )
    store = OpportunityOutcomeStore(database)
    first = store.append_batch(
        initial,
        persisted_at=initial.recorded_at + timedelta(seconds=1),
    )
    second = store.append_batch(
        correction,
        persisted_at=correction.recorded_at + timedelta(seconds=2),
        supersedes_outcome_receipt_id=first.outcome_receipt_id,
    )
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TRIGGER opportunity_outcome_receipts_no_update")
        connection.execute(
            "UPDATE opportunity_outcome_receipts SET receipt_kind = 'correction', "
            "supersedes_outcome_receipt_id = ?, "
            "supersedes_outcome_receipt_content_hash_sha256 = ? "
            "WHERE outcome_receipt_id = ?",
            (second.outcome_receipt_id, second.content_hash(), first.outcome_receipt_id),
        )
        connection.execute(
            """
            CREATE TRIGGER opportunity_outcome_receipts_no_update
            BEFORE UPDATE ON opportunity_outcome_receipts
            BEGIN
                SELECT RAISE(ABORT, 'opportunity_outcome_receipts is append-only');
            END
            """
        )
        connection.commit()
    with pytest.raises(OpportunityOutcomeIntegrityError, match="exactly one root"):
        store.load_current_receipt(initial.pipeline_result.run_id)
    with pytest.raises(OpportunityOutcomeIntegrityError, match="exactly one root"):
        store.append_batch(
            _batch(missing_symbol="DEF"),
            persisted_at=correction.recorded_at + timedelta(seconds=3),
            supersedes_outcome_receipt_id=second.outcome_receipt_id,
        )


@pytest.mark.parametrize(
    ("trigger_name", "table"),
    (
        ("opportunity_outcome_receipts_no_update", "opportunity_outcome_receipts"),
        ("opportunity_outcome_receipts_no_delete", "opportunity_outcome_receipts"),
        ("opportunity_outcome_records_no_update", "opportunity_outcome_records"),
        ("opportunity_outcome_records_no_delete", "opportunity_outcome_records"),
    ),
)
def test_same_named_forged_append_only_trigger_fails_closed(
    tmp_path: Path,
    persisted_replay,
    trigger_name: str,
    table: str,
) -> None:
    database = tmp_path / f"forged-{trigger_name}.sqlite"
    store = OpportunityOutcomeStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute(f"DROP TRIGGER {trigger_name}")
        connection.execute(
            f"CREATE TRIGGER {trigger_name} AFTER INSERT ON {table} BEGIN SELECT 1; END"
        )
        connection.commit()
    _receipt_value, replay, _source_store = persisted_replay
    _assert_schema_rejected_on_all_public_paths(store, replay.outcome_batch)


def test_same_named_wrong_index_and_altered_table_ddl_fail_closed(
    tmp_path: Path,
    persisted_replay,
) -> None:
    _receipt_value, replay, _source_store = persisted_replay
    wrong_index = tmp_path / "wrong-index.sqlite"
    index_store = OpportunityOutcomeStore(wrong_index)
    index_store.initialize()
    with sqlite3.connect(wrong_index) as connection:
        connection.execute("DROP INDEX idx_opportunity_outcome_records_status")
        connection.execute(
            "CREATE INDEX idx_opportunity_outcome_records_status "
            "ON opportunity_outcome_records(run_id)"
        )
        connection.commit()
    _assert_schema_rejected_on_all_public_paths(index_store, replay.outcome_batch)

    altered_table = tmp_path / "altered-table.sqlite"
    table_store = OpportunityOutcomeStore(altered_table)
    table_store.initialize()
    expected = expected_outcome_schema_sql()
    record_objects = tuple(
        sql
        for name, sql in expected.items()
        if name == "opportunity_outcome_records"
        or " ON opportunity_outcome_records" in sql
    )
    with sqlite3.connect(altered_table) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute("DROP TABLE opportunity_outcome_records")
        altered = record_objects[0].replace(
            "'v2.opportunity.outcome_record.v3'",
            "'V2.opportunity.outcome_record.v3'",
        )
        connection.execute(altered)
        for sql in record_objects[1:]:
            connection.execute(sql)
        connection.commit()
    _assert_schema_rejected_on_all_public_paths(table_store, replay.outcome_batch)


def test_orphan_outcome_row_fails_scoped_foreign_key_check(
    tmp_path: Path,
    persisted_replay,
) -> None:
    database = tmp_path / "orphan.sqlite"
    store = OpportunityOutcomeStore(database)
    store.initialize()
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = OFF")
        connection.execute(
            """
            INSERT INTO opportunity_outcome_records (
                outcome_receipt_id, record_ordinal, run_id, evaluation_id,
                horizon_id, decision_id, outcome_id,
                outcome_content_hash_sha256, outcome_schema_version,
                outcome_json, completeness, entry_status, path_status,
                supersedes_outcome_receipt_id, supersedes_outcome_id,
                supersedes_outcome_content_hash_sha256, first_persisted_at
            ) VALUES (?, 0, ?, ?, ?, ?, ?, ?, ?, '{}', 'unavailable',
                      'unsupported', 'unsupported_evidence', NULL, NULL, NULL, ?)
            """,
            (
                "outcome-receipt:000000000000000000000000",
                "opportunity-run:000000000000000000000000",
                "evaluation:000000000000000000000000",
                "horizon:000000000000000000000000",
                "decision:000000000000000000000000",
                "outcome:000000000000000000000000",
                "0" * 64,
                "v2.opportunity.outcome_record.v3",
                "2026-08-11T15:00:00+00:00",
            ),
        )
        connection.commit()
    _receipt_value, replay, _source_store = persisted_replay
    _assert_schema_rejected_on_all_public_paths(store, replay.outcome_batch)


@pytest.mark.parametrize(
    ("table", "operation"),
    (
        ("opportunity_outcome_receipts", "UPDATE"),
        ("opportunity_outcome_receipts", "DELETE"),
        ("opportunity_outcome_records", "UPDATE"),
        ("opportunity_outcome_records", "DELETE"),
    ),
)
def test_all_append_only_guards_abort_real_mutations(
    tmp_path: Path,
    persisted_replay,
    table: str,
    operation: str,
) -> None:
    _receipt_value, _replay, source_store = persisted_replay
    database = tmp_path / f"guard-{table}-{operation}.sqlite"
    shutil.copy2(source_store.db_path, database)
    statement = (
        f"UPDATE {table} SET research_only = research_only"
        if operation == "UPDATE" and table == "opportunity_outcome_receipts"
        else f"UPDATE {table} SET record_ordinal = record_ordinal"
        if operation == "UPDATE"
        else f"DELETE FROM {table}"
    )
    with sqlite3.connect(database) as connection:
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute(statement)


def test_constructor_read_only_and_empty_batch_semantics(tmp_path: Path) -> None:
    absent = tmp_path / "absent.sqlite"
    store = OpportunityOutcomeStore(absent)
    assert not absent.exists()
    with pytest.raises(OpportunityOutcomeIntegrityError, match="initialize"):
        store.load_receipt("outcome-receipt:000000000000000000000000")
    assert not absent.exists()

    dataset = _pipeline_dataset()
    prepared = prepare_opportunity_pipeline(
        dataset,
        universe_snapshot=_pipeline_universe(dataset),
        registry=StrategyRegistry(()),
    )
    result = run_opportunity_pipeline(
        prepared,
        risk_by_evaluation={},
        risk_policy=_pipeline_risk_policy(),
    )
    assert result.evaluations == ()
    recorded_at = result.decision_at.astimezone(timezone.utc) + timedelta(seconds=1)
    source = build_outcome_observation_dataset(
        decision_at=result.decision_at,
        frozen_at=recorded_at,
        series=(),
    )
    empty_batch = label_pipeline_outcomes(
        pipeline_result=result,
        persistence_receipt=_receipt(result, result.decision_at.astimezone(timezone.utc)),
        source_dataset=source,
        policy=build_outcome_label_policy(
            policy_version="wp003-c-empty",
            expected_bar_interval_seconds=60,
        ),
        horizons=(),
        recorded_at=recorded_at,
    )
    database = tmp_path / "empty.sqlite"
    run_store = OpportunityStore(database)
    run_store.initialize()
    run_store.append_run(
        result,
        recorded_at=empty_batch.persistence_receipt.recorded_at,
    )
    writable = OpportunityOutcomeStore(database)
    receipt = writable.append_batch(
        empty_batch,
        persisted_at=recorded_at + timedelta(seconds=1),
    )
    assert receipt.record_count == 0
    assert receipt.artifact_count == 1
    assert writable.load_current_outcomes(result.run_id) == ()
    correction_values = {
        **empty_batch.__dict__,
        "limitations": (*empty_batch.limitations, "empty_correction"),
    }
    correction_values.pop("batch_id")
    empty_correction = replace(
        empty_batch,
        batch_id=stable_identity("outcome-label-batch", correction_values),
        limitations=(*empty_batch.limitations, "empty_correction"),
    )
    corrected = writable.append_batch(
        empty_correction,
        persisted_at=receipt.persisted_at + timedelta(seconds=1),
        supersedes_outcome_receipt_id=receipt.outcome_receipt_id,
    )
    current = writable.replay_current(result.run_id)
    assert current is not None
    assert current.full_chain == (receipt, corrected)
    assert current.outcome_batch == empty_correction
    assert writable.append_batch(
        empty_correction,
        persisted_at=corrected.persisted_at + timedelta(hours=1),
        supersedes_outcome_receipt_id=receipt.outcome_receipt_id,
    ) == corrected
    read_only = OpportunityOutcomeStore(database, read_only=True)
    assert read_only.load_batch(receipt.outcome_receipt_id) == empty_batch
    with pytest.raises(OpportunityOutcomeReadOnlyError, match="initialize"):
        read_only.initialize()
    with pytest.raises(OpportunityOutcomeReadOnlyError, match="append"):
        read_only.append_batch(
            empty_batch,
            persisted_at=recorded_at + timedelta(seconds=2),
        )


def test_schema_27_and_missing_or_corrupt_parent_use_typed_outcome_errors(
    tmp_path: Path,
    persisted_replay,
) -> None:
    stale = tmp_path / "schema-27.sqlite"
    _initialize_schema_through(stale, 27)
    stale_store = OpportunityOutcomeStore(stale, read_only=True)
    with pytest.raises(OpportunityOutcomeIntegrityError, match="schema 28"):
        stale_store.load_receipt("outcome-receipt:000000000000000000000000")

    _receipt_value, replay, source_store = persisted_replay
    malformed = tmp_path / "malformed-schema-version.sqlite"
    shutil.copy2(source_store.db_path, malformed)
    with sqlite3.connect(malformed) as connection:
        connection.execute("UPDATE schema_version SET version = 'not-an-integer'")
        connection.commit()
    malformed_store = OpportunityOutcomeStore(malformed)
    with pytest.raises(OpportunityOutcomeIntegrityError, match="schema"):
        malformed_store.initialize()
    with pytest.raises(OpportunityOutcomeIntegrityError, match="schema version"):
        malformed_store.load_receipt(
            replay.outcome_persistence_receipt.outcome_receipt_id
        )

    missing_parent = tmp_path / "missing-parent.sqlite"
    OpportunityOutcomeStore(missing_parent).initialize()
    with pytest.raises(
        OpportunityOutcomeIntegrityError,
        match="stored parent opportunity run is invalid",
    ):
        OpportunityOutcomeStore(missing_parent).append_batch(
            replay.outcome_batch,
            persisted_at=replay.outcome_batch.recorded_at + timedelta(seconds=1),
        )

    corrupt_parent = tmp_path / "corrupt-parent.sqlite"
    shutil.copy2(source_store.db_path, corrupt_parent)
    with sqlite3.connect(corrupt_parent) as connection:
        connection.execute("DROP TRIGGER opportunity_pipeline_runs_no_update")
        connection.execute(
            "UPDATE opportunity_pipeline_runs SET receipt_json = '{}' "
            "WHERE run_id = ?",
            (replay.pipeline_result.run_id,),
        )
        connection.execute(
            """
            CREATE TRIGGER opportunity_pipeline_runs_no_update
            BEFORE UPDATE ON opportunity_pipeline_runs
            BEGIN
                SELECT RAISE(ABORT, 'opportunity_pipeline_runs is append-only');
            END
            """
        )
        connection.commit()
    with pytest.raises(
        OpportunityOutcomeIntegrityError,
        match="stored parent opportunity run is invalid",
    ):
        OpportunityOutcomeStore(corrupt_parent).load_batch(
            replay.outcome_persistence_receipt.outcome_receipt_id
        )


def test_insert_failure_rolls_back_parent_and_records(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    batch, store = _initialized_batch_store(tmp_path / "rollback.sqlite")

    def fail_records(*_args, **_kwargs):
        raise RuntimeError("fixture insertion failure")

    monkeypatch.setattr(store, "_insert_records", fail_records)
    with pytest.raises(RuntimeError, match="fixture insertion failure"):
        store.append_batch(
            batch,
            persisted_at=batch.recorded_at + timedelta(seconds=1),
        )
    with sqlite3.connect(store.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_outcome_receipts"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM opportunity_outcome_records"
        ).fetchone() == (0,)


@pytest.mark.parametrize(
    "attack",
    (
        "missing_record",
        "extra_record",
        "reordered_records",
        "record_payload",
        "record_hash",
        "record_status",
        "record_lineage",
        "receipt_inventory",
        "receipt_timestamp",
    ),
)
def test_raw_stored_projection_and_inventory_tamper_fails_load_and_replay(
    tmp_path: Path,
    persisted_replay,
    attack: str,
) -> None:
    receipt, replay, source_store = persisted_replay
    database = tmp_path / f"raw-{attack}.sqlite"
    shutil.copy2(source_store.db_path, database)
    with sqlite3.connect(database) as connection:
        if attack in {
            "missing_record",
            "extra_record",
            "reordered_records",
            "record_payload",
            "record_hash",
            "record_status",
            "record_lineage",
        }:
            connection.execute("DROP TRIGGER opportunity_outcome_records_no_update")
            connection.execute("DROP TRIGGER opportunity_outcome_records_no_delete")
        if attack == "missing_record":
            connection.execute(
                "DELETE FROM opportunity_outcome_records "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 0",
                (receipt.outcome_receipt_id,),
            )
        elif attack == "extra_record":
            connection.execute(
                """
                INSERT INTO opportunity_outcome_records
                SELECT outcome_receipt_id, 99, run_id, evaluation_id || '-extra',
                       horizon_id, decision_id, outcome_id || '-extra',
                       outcome_content_hash_sha256, outcome_schema_version,
                       outcome_json, completeness, entry_status, path_status,
                       NULL, NULL, NULL, first_persisted_at
                FROM opportunity_outcome_records
                WHERE outcome_receipt_id = ? AND record_ordinal = 0
                """,
                (receipt.outcome_receipt_id,),
            )
        elif attack == "reordered_records":
            connection.execute(
                "UPDATE opportunity_outcome_records SET record_ordinal = 99 "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 0",
                (receipt.outcome_receipt_id,),
            )
            connection.execute(
                "UPDATE opportunity_outcome_records SET record_ordinal = 0 "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 1",
                (receipt.outcome_receipt_id,),
            )
            connection.execute(
                "UPDATE opportunity_outcome_records SET record_ordinal = 1 "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 99",
                (receipt.outcome_receipt_id,),
            )
        elif attack == "record_payload":
            connection.execute(
                "UPDATE opportunity_outcome_records SET outcome_json = '{}' "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 0",
                (receipt.outcome_receipt_id,),
            )
        elif attack == "record_hash":
            connection.execute(
                "UPDATE opportunity_outcome_records "
                "SET outcome_content_hash_sha256 = ? "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 0",
                ("0" * 64, receipt.outcome_receipt_id),
            )
        elif attack == "record_status":
            connection.execute(
                "UPDATE opportunity_outcome_records SET completeness = 'unavailable' "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 0",
                (receipt.outcome_receipt_id,),
            )
        elif attack == "record_lineage":
            connection.execute("PRAGMA foreign_keys = OFF")
            connection.execute(
                "UPDATE opportunity_outcome_records "
                "SET supersedes_outcome_receipt_id = ?, supersedes_outcome_id = ?, "
                "supersedes_outcome_content_hash_sha256 = ? "
                "WHERE outcome_receipt_id = ? AND record_ordinal = 0",
                (
                    "outcome-receipt:000000000000000000000000",
                    "outcome:000000000000000000000000",
                    "0" * 64,
                    receipt.outcome_receipt_id,
                ),
            )
        else:
            connection.execute("DROP TRIGGER opportunity_outcome_receipts_no_update")
            if attack == "receipt_inventory":
                connection.execute(
                    "UPDATE opportunity_outcome_receipts "
                    "SET record_count = record_count + 1, "
                    "artifact_count = artifact_count + 1, "
                    "artifact_inventory_hash_sha256 = ? "
                    "WHERE outcome_receipt_id = ?",
                    ("0" * 64, receipt.outcome_receipt_id),
                )
            else:
                connection.execute(
                    "UPDATE opportunity_outcome_receipts SET persisted_at = 'not-a-time' "
                    "WHERE outcome_receipt_id = ?",
                    (receipt.outcome_receipt_id,),
                )
        if attack in {
            "missing_record",
            "extra_record",
            "reordered_records",
            "record_payload",
            "record_hash",
            "record_status",
            "record_lineage",
        }:
            connection.executescript(
                """
                CREATE TRIGGER opportunity_outcome_records_no_update
                BEFORE UPDATE ON opportunity_outcome_records
                BEGIN
                    SELECT RAISE(ABORT, 'opportunity_outcome_records is append-only');
                END;
                CREATE TRIGGER opportunity_outcome_records_no_delete
                BEFORE DELETE ON opportunity_outcome_records
                BEGIN
                    SELECT RAISE(ABORT, 'opportunity_outcome_records is append-only');
                END;
                """
            )
        else:
            connection.execute(
                """
                CREATE TRIGGER opportunity_outcome_receipts_no_update
                BEFORE UPDATE ON opportunity_outcome_receipts
                BEGIN
                    SELECT RAISE(ABORT, 'opportunity_outcome_receipts is append-only');
                END
                """
            )
        connection.commit()
    tampered = OpportunityOutcomeStore(database)
    with pytest.raises(OpportunityOutcomeIntegrityError):
        tampered.load_batch(receipt.outcome_receipt_id)
    with pytest.raises(OpportunityOutcomeIntegrityError):
        tampered.replay_current(replay.pipeline_result.run_id)


def test_core_and_storage_root_imports_do_not_load_outcome_modules() -> None:
    script = """
import sys
import intraday_scanner.v2.opportunity
import intraday_scanner.v2.opportunity.models
import intraday_scanner.v2.opportunity.pipeline
import intraday_scanner.storage
import intraday_scanner.storage.opportunity_store
assert not any(
    name.startswith('intraday_scanner.v2.opportunity.outcome')
    for name in sys.modules
)
assert 'intraday_scanner.storage.opportunity_outcome_store' not in sys.modules
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr


def test_outcome_storage_ast_import_boundary_is_downstream_only() -> None:
    repository = Path(__file__).resolve().parents[1]
    files = (
        repository / "intraday_scanner/v2/opportunity/outcome_persistence.py",
        repository / "intraday_scanner/storage/opportunity_outcome_errors.py",
        repository / "intraday_scanner/storage/opportunity_outcome_inventory.py",
        repository / "intraday_scanner/storage/opportunity_outcome_schema.py",
        repository / "intraday_scanner/storage/opportunity_outcome_store.py",
    )
    forbidden = (
        "alpha.path_replay",
        "backtest",
        "app",
        "broker",
        "network",
        "scheduler",
        "streamlit",
        "ui",
    )
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.append(node.module)
        assert not any(
            token in imported for imported in imports for token in forbidden
        ), (path, imports)
    for root in (
        repository / "intraday_scanner/storage/__init__.py",
        repository / "intraday_scanner/v2/opportunity/__init__.py",
    ):
        text = root.read_text(encoding="utf-8")
        assert "opportunity_outcome_store" not in text
        assert "outcome_persistence" not in text
