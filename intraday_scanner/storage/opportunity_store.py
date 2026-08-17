"""Append-only persistence contracts for canonical opportunity pipeline runs."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import Any

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.models import (
    OpportunityContract,
    stable_identity,
)
from intraday_scanner.v2.opportunity.pipeline import PipelineResult

LEGACY_OPPORTUNITY_DATABASE_SCHEMA_VERSION = 27
OPPORTUNITY_DATABASE_SCHEMA_VERSION = 28
PREVIOUS_STORAGE_SCHEMA_VERSION = 29
CURRENT_STORAGE_SCHEMA_VERSION = 30
_RECEIPT_SCHEMA_VERSION_V1 = "v2.opportunity.persistence_receipt.v1"
_RECEIPT_SCHEMA_VERSION_V2 = "v2.opportunity.persistence_receipt.v2"
_SUPPORTED_RECEIPT_SCHEMA_BY_DATABASE_VERSION = {
    LEGACY_OPPORTUNITY_DATABASE_SCHEMA_VERSION: _RECEIPT_SCHEMA_VERSION_V1,
    OPPORTUNITY_DATABASE_SCHEMA_VERSION: _RECEIPT_SCHEMA_VERSION_V2,
}
_FAMILY_COUNT_SCHEMA_VERSION = "v2.opportunity.artifact_family_count.v1"
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_IDENTITY_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}")


class OpportunityArtifactFamily(str, Enum):
    """Canonical order and names for every persisted opportunity artifact family."""

    UNIVERSE_SNAPSHOT = "universe_snapshot"
    PREPARED_PIPELINE = "prepared_pipeline"
    STRATEGY_EXPECTANCY_BINDING = "strategy_expectancy_binding"
    CHEAP_FEATURE_SNAPSHOT = "cheap_feature_snapshot"
    RICH_FEATURE_SNAPSHOT = "rich_feature_snapshot"
    BENCHMARK_FEATURE_SNAPSHOT = "benchmark_feature_snapshot"
    OPPORTUNITY_CANDIDATE = "opportunity_candidate"
    MARKET_REGIME = "market_regime"
    SECURITY_REGIME = "security_regime"
    STRATEGY_EVALUATION = "strategy_evaluation"
    RANKED_OPPORTUNITY = "ranked_opportunity"
    PIPELINE_RISK_POLICY = "pipeline_risk_policy"
    EXECUTION_RISK_EVIDENCE = "execution_risk_evidence"
    DECISION_RUN_CONTEXT = "decision_run_context"
    TRADE_DECISION = "trade_decision"
    DECISION_TRACE = "decision_trace"


CANONICAL_OPPORTUNITY_ARTIFACT_FAMILIES = tuple(OpportunityArtifactFamily)

_RUN_COLUMNS = frozenset(
    {
        "run_id",
        "result_content_hash_sha256",
        "preparation_id",
        "preparation_content_hash_sha256",
        "decision_context_id",
        "decision_context_content_hash_sha256",
        "dataset_id",
        "dataset_content_id",
        "universe_snapshot_id",
        "universe_snapshot_content_hash_sha256",
        "decision_at",
        "result_schema_version",
        "result_json",
        "artifact_count",
        "artifact_inventory_hash_sha256",
        "receipt_id",
        "receipt_json",
        "research_only",
        "first_recorded_at",
    }
)
_ARTIFACT_COLUMNS = frozenset(
    {
        "run_id",
        "inventory_ordinal",
        "artifact_family",
        "family_ordinal",
        "artifact_id",
        "evaluation_id",
        "decision_id",
        "artifact_schema_version",
        "payload_json",
        "content_hash_sha256",
        "first_recorded_at",
    }
)
_REQUIRED_TRIGGERS = frozenset(
    {
        "opportunity_pipeline_runs_no_update",
        "opportunity_pipeline_runs_no_delete",
        "opportunity_run_artifacts_no_update",
        "opportunity_run_artifacts_no_delete",
    }
)
_EXPECTED_SCHEMA_SQL = {
    "opportunity_pipeline_runs": """
        CREATE TABLE opportunity_pipeline_runs (
            run_id TEXT PRIMARY KEY,
            result_content_hash_sha256 TEXT NOT NULL,
            preparation_id TEXT NOT NULL,
            preparation_content_hash_sha256 TEXT NOT NULL,
            decision_context_id TEXT,
            decision_context_content_hash_sha256 TEXT,
            dataset_id TEXT NOT NULL,
            dataset_content_id TEXT NOT NULL,
            universe_snapshot_id TEXT NOT NULL,
            universe_snapshot_content_hash_sha256 TEXT NOT NULL,
            decision_at TEXT NOT NULL,
            result_schema_version TEXT NOT NULL,
            result_json TEXT NOT NULL,
            artifact_count INTEGER NOT NULL CHECK (artifact_count >= 0),
            artifact_inventory_hash_sha256 TEXT NOT NULL,
            receipt_id TEXT NOT NULL UNIQUE,
            receipt_json TEXT NOT NULL,
            research_only INTEGER NOT NULL CHECK (research_only = 1),
            first_recorded_at TEXT NOT NULL,
            CHECK (
                (decision_context_id IS NULL AND decision_context_content_hash_sha256 IS NULL)
                OR
                (decision_context_id IS NOT NULL
                 AND decision_context_content_hash_sha256 IS NOT NULL)
            )
        )
    """,
    "opportunity_run_artifacts": """
        CREATE TABLE opportunity_run_artifacts (
            run_id TEXT NOT NULL,
            inventory_ordinal INTEGER NOT NULL CHECK (inventory_ordinal >= 0),
            artifact_family TEXT NOT NULL CHECK (artifact_family IN (
                'universe_snapshot',
                'prepared_pipeline',
                'strategy_expectancy_binding',
                'cheap_feature_snapshot',
                'rich_feature_snapshot',
                'benchmark_feature_snapshot',
                'opportunity_candidate',
                'market_regime',
                'security_regime',
                'strategy_evaluation',
                'ranked_opportunity',
                'pipeline_risk_policy',
                'execution_risk_evidence',
                'decision_run_context',
                'trade_decision',
                'decision_trace'
            )),
            family_ordinal INTEGER NOT NULL CHECK (family_ordinal >= 0),
            artifact_id TEXT NOT NULL,
            evaluation_id TEXT,
            decision_id TEXT,
            artifact_schema_version TEXT NOT NULL,
            payload_json TEXT NOT NULL,
            content_hash_sha256 TEXT NOT NULL,
            first_recorded_at TEXT NOT NULL,
            PRIMARY KEY (run_id, inventory_ordinal),
            UNIQUE (run_id, artifact_family, family_ordinal),
            UNIQUE (run_id, artifact_family, artifact_id),
            FOREIGN KEY (run_id) REFERENCES opportunity_pipeline_runs(run_id)
        )
    """,
    "idx_opportunity_pipeline_runs_decision": """
        CREATE INDEX idx_opportunity_pipeline_runs_decision
        ON opportunity_pipeline_runs(decision_at, run_id)
    """,
    "idx_opportunity_pipeline_runs_dataset": """
        CREATE INDEX idx_opportunity_pipeline_runs_dataset
        ON opportunity_pipeline_runs(dataset_id, dataset_content_id)
    """,
    "idx_opportunity_pipeline_runs_universe": """
        CREATE INDEX idx_opportunity_pipeline_runs_universe
        ON opportunity_pipeline_runs(universe_snapshot_id,
                                     universe_snapshot_content_hash_sha256)
    """,
    "idx_opportunity_artifacts_identity": """
        CREATE INDEX idx_opportunity_artifacts_identity
        ON opportunity_run_artifacts(artifact_family, artifact_id,
                                     content_hash_sha256)
    """,
    "idx_opportunity_artifacts_evaluation": """
        CREATE INDEX idx_opportunity_artifacts_evaluation
        ON opportunity_run_artifacts(run_id, evaluation_id, artifact_family,
                                     family_ordinal)
    """,
    "idx_opportunity_artifacts_decision": """
        CREATE INDEX idx_opportunity_artifacts_decision
        ON opportunity_run_artifacts(run_id, decision_id, artifact_family,
                                     family_ordinal)
    """,
    "opportunity_pipeline_runs_no_update": """
        CREATE TRIGGER opportunity_pipeline_runs_no_update
        BEFORE UPDATE ON opportunity_pipeline_runs
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_pipeline_runs is append-only');
        END
    """,
    "opportunity_pipeline_runs_no_delete": """
        CREATE TRIGGER opportunity_pipeline_runs_no_delete
        BEFORE DELETE ON opportunity_pipeline_runs
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_pipeline_runs is append-only');
        END
    """,
    "opportunity_run_artifacts_no_update": """
        CREATE TRIGGER opportunity_run_artifacts_no_update
        BEFORE UPDATE ON opportunity_run_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_run_artifacts is append-only');
        END
    """,
    "opportunity_run_artifacts_no_delete": """
        CREATE TRIGGER opportunity_run_artifacts_no_delete
        BEFORE DELETE ON opportunity_run_artifacts
        BEGIN
            SELECT RAISE(ABORT, 'opportunity_run_artifacts is append-only');
        END
    """,
}
_EXPECTED_INDEX_COLUMNS = {
    "idx_opportunity_pipeline_runs_decision": ("decision_at", "run_id"),
    "idx_opportunity_pipeline_runs_dataset": ("dataset_id", "dataset_content_id"),
    "idx_opportunity_pipeline_runs_universe": (
        "universe_snapshot_id",
        "universe_snapshot_content_hash_sha256",
    ),
    "idx_opportunity_artifacts_identity": (
        "artifact_family",
        "artifact_id",
        "content_hash_sha256",
    ),
    "idx_opportunity_artifacts_evaluation": (
        "run_id",
        "evaluation_id",
        "artifact_family",
        "family_ordinal",
    ),
    "idx_opportunity_artifacts_decision": (
        "run_id",
        "decision_id",
        "artifact_family",
        "family_ordinal",
    ),
}


@dataclass(frozen=True)
class OpportunityArtifactFamilyCount(OpportunityContract):
    """Exact persisted row count for one canonical artifact family."""

    family: OpportunityArtifactFamily
    count: int
    schema_version: str = _FAMILY_COUNT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        if self.count < 0:
            raise ValueError("opportunity artifact family count cannot be negative")
        if self.schema_version != _FAMILY_COUNT_SCHEMA_VERSION:
            raise ValueError("unsupported opportunity artifact family count schema")


@dataclass(frozen=True)
class OpportunityPersistenceReceipt(OpportunityContract):
    """Immutable content-bound receipt for one persisted opportunity run."""

    receipt_id: str
    run_id: str
    run_content_hash_sha256: str
    preparation_id: str
    preparation_content_hash_sha256: str
    decision_context_id: str | None
    decision_context_content_hash_sha256: str | None
    decision_at: datetime
    family_counts: tuple[OpportunityArtifactFamilyCount, ...]
    artifact_count: int
    artifact_inventory_hash_sha256: str
    recorded_at: datetime
    database_schema_version: int = OPPORTUNITY_DATABASE_SCHEMA_VERSION
    research_only: bool = True
    schema_version: str = _RECEIPT_SCHEMA_VERSION_V2

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        for value, field_name in (
            (self.receipt_id, "receipt_id"),
            (self.run_id, "run_id"),
            (self.preparation_id, "preparation_id"),
        ):
            _require_identity(value, field_name)
        for value, field_name in (
            (self.run_content_hash_sha256, "run_content_hash_sha256"),
            (
                self.preparation_content_hash_sha256,
                "preparation_content_hash_sha256",
            ),
            (
                self.artifact_inventory_hash_sha256,
                "artifact_inventory_hash_sha256",
            ),
        ):
            _require_hash(value, field_name)
        if (self.decision_context_id is None) is not (
            self.decision_context_content_hash_sha256 is None
        ):
            raise ValueError("decision context identity and hash must be paired")
        if self.decision_context_id is not None:
            _require_identity(self.decision_context_id, "decision_context_id")
            _require_hash(
                self.decision_context_content_hash_sha256,
                "decision_context_content_hash_sha256",
            )
        expected_families = CANONICAL_OPPORTUNITY_ARTIFACT_FAMILIES
        actual_families = tuple(item.family for item in self.family_counts)
        if actual_families != expected_families:
            raise ValueError("opportunity artifact family counts must use canonical order")
        if self.artifact_count < 0:
            raise ValueError("artifact_count cannot be negative")
        if self.artifact_count != sum(item.count for item in self.family_counts):
            raise ValueError("artifact_count does not match family counts")
        if self.recorded_at.utcoffset() != timedelta(0):
            raise ValueError("recorded_at must use UTC")
        if self.recorded_at < self.decision_at:
            raise ValueError("recorded_at cannot precede the pipeline decision time")
        expected_receipt_schema = _SUPPORTED_RECEIPT_SCHEMA_BY_DATABASE_VERSION.get(
            self.database_schema_version
        )
        if expected_receipt_schema is None:
            raise ValueError("unsupported opportunity database schema version")
        if not self.research_only:
            raise ValueError("opportunity persistence receipt must remain research_only")
        if self.schema_version != expected_receipt_schema:
            raise ValueError(
                "opportunity persistence receipt schema/database pair is unsupported"
            )
        expected_id = stable_identity(
            "opportunity-persistence-receipt",
            {
                name: value
                for name, value in self.__dict__.items()
                if name != "receipt_id"
            },
        )
        if self.receipt_id != expected_id:
            raise ValueError("opportunity persistence receipt identity does not match content")


class OpportunityStoreError(StorageError):
    """Base failure raised by the dedicated opportunity persistence adapter."""


class OpportunityPersistenceConflictError(OpportunityStoreError):
    """A stored run identity conflicts with different canonical content."""


class OpportunityPersistenceIntegrityError(OpportunityStoreError):
    """Stored opportunity data or its schema fails exact verification."""


class OpportunityStoreReadOnlyError(OpportunityStoreError):
    """A write operation was attempted through a read-only opportunity store."""


@dataclass(frozen=True)
class _ArtifactInventoryItem:
    inventory_ordinal: int
    family: OpportunityArtifactFamily
    family_ordinal: int
    artifact_id: str
    evaluation_id: str | None
    decision_id: str | None
    schema_version: str
    payload_json: str
    content_hash_sha256: str

    def identity_tuple(self) -> tuple[int, str, int, str, str | None, str | None, str, str]:
        return (
            self.inventory_ordinal,
            self.family.value,
            self.family_ordinal,
            self.artifact_id,
            self.evaluation_id,
            self.decision_id,
            self.schema_version,
            self.content_hash_sha256,
        )


class OpportunityStore:
    """Narrow append-only store for accepted opportunity pipeline results."""

    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self.db_path = Path(db_path)
        self.read_only = read_only

    def initialize(self) -> None:
        """Explicitly create/upgrade a writable database to schema 30."""

        if self.read_only:
            raise OpportunityStoreReadOnlyError("read-only opportunity store cannot initialize")
        try:
            from intraday_scanner.storage.migrations import run_migrations

            self.db_path.parent.mkdir(parents=True, exist_ok=True)
            with self._connect_writable(require_existing=False) as connection:
                version = run_migrations(connection)
                if version != CURRENT_STORAGE_SCHEMA_VERSION:
                    raise OpportunityPersistenceIntegrityError(
                        f"opportunity store requires schema 30, found {version}"
                    )
                _validate_schema(connection)
        except OpportunityStoreError:
            raise
        except sqlite3.Error as exc:
            raise OpportunityStoreError(
                f"could not initialize opportunity store: {exc}"
            ) from exc

    def append_run(
        self,
        result: PipelineResult,
        *,
        recorded_at: datetime,
    ) -> OpportunityPersistenceReceipt:
        """Append one canonical result atomically or return its original receipt."""

        if self.read_only:
            raise OpportunityStoreReadOnlyError("read-only opportunity store cannot append")
        if not isinstance(result, PipelineResult):
            raise TypeError("result must be PipelineResult")
        _validate_recorded_at(recorded_at, result.decision_at)
        result_json = result.to_json()
        inventory = _build_artifact_inventory(result)
        inventory_hash = _artifact_inventory_hash(inventory)
        connection = self._connect_writable(require_existing=True)
        try:
            _validate_schema(connection, require_current=True)
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM opportunity_pipeline_runs WHERE run_id = ?",
                (result.run_id,),
            ).fetchone()
            if existing is not None:
                receipt = _verify_stored_run(
                    connection,
                    result.run_id,
                    expected_result=result,
                    expected_result_json=result_json,
                    conflict_on_difference=True,
                )[1]
                connection.rollback()
                return receipt

            receipt = _build_persistence_receipt(
                result,
                inventory=inventory,
                inventory_hash=inventory_hash,
                recorded_at=recorded_at,
                database_schema_version=OPPORTUNITY_DATABASE_SCHEMA_VERSION,
            )
            self._insert_run(
                connection,
                result=result,
                result_json=result_json,
                inventory=inventory,
                inventory_hash=inventory_hash,
                receipt=receipt,
            )
            self._insert_artifacts(
                connection,
                run_id=result.run_id,
                inventory=inventory,
                recorded_at=recorded_at,
            )
            stored_count = int(
                connection.execute(
                    "SELECT COUNT(*) FROM opportunity_run_artifacts WHERE run_id = ?",
                    (result.run_id,),
                ).fetchone()[0]
            )
            if stored_count != len(inventory):
                raise OpportunityPersistenceIntegrityError(
                    "persisted opportunity artifact count does not reconcile"
                )
            connection.commit()
            return receipt
        except OpportunityStoreError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OpportunityPersistenceConflictError(
                f"opportunity run conflicts with persisted content: {exc}"
            ) from exc
        except sqlite3.Error as exc:
            connection.rollback()
            raise OpportunityStoreError(f"could not append opportunity run: {exc}") from exc
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def load_run(self, run_id: str) -> PipelineResult | None:
        """Load and independently verify one byte-equivalent pipeline result."""

        _require_identity(run_id, "run_id")
        connection = self._connect_read()
        try:
            _validate_schema(connection)
            row = connection.execute(
                "SELECT 1 FROM opportunity_pipeline_runs WHERE run_id = ?",
                (run_id,),
            ).fetchone()
            if row is None:
                return None
            result, _receipt = _verify_stored_run(connection, run_id)
            return result
        except OpportunityStoreError:
            raise
        except sqlite3.Error as exc:
            raise OpportunityPersistenceIntegrityError(
                f"could not load opportunity run: {exc}"
            ) from exc
        finally:
            connection.close()

    def _connect_writable(
        self,
        *,
        require_existing: bool,
    ) -> sqlite3.Connection:
        if require_existing and not self.db_path.is_file():
            raise OpportunityPersistenceIntegrityError(
                "opportunity database is absent; call initialize explicitly"
            )
        connection = sqlite3.connect(self.db_path)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _connect_read(self) -> sqlite3.Connection:
        try:
            if self.read_only:
                connection = connect_read_only(self.db_path, row_factory=sqlite3.Row)
            else:
                if not self.db_path.is_file():
                    raise OpportunityPersistenceIntegrityError(
                        "opportunity database is absent; call initialize explicitly"
                    )
                connection = sqlite3.connect(self.db_path)
                connection.row_factory = sqlite3.Row
        except OpportunityStoreError:
            raise
        except StorageError as exc:
            raise OpportunityPersistenceIntegrityError(
                f"could not open read-only opportunity database: {exc}"
            ) from exc
        connection.execute("PRAGMA foreign_keys = ON")
        return connection

    def _insert_run(
        self,
        connection: sqlite3.Connection,
        *,
        result: PipelineResult,
        result_json: str,
        inventory: tuple[_ArtifactInventoryItem, ...],
        inventory_hash: str,
        receipt: OpportunityPersistenceReceipt,
    ) -> None:
        context = result.decision_context
        connection.execute(
            """
            INSERT INTO opportunity_pipeline_runs (
                run_id, result_content_hash_sha256,
                preparation_id, preparation_content_hash_sha256,
                decision_context_id, decision_context_content_hash_sha256,
                dataset_id, dataset_content_id,
                universe_snapshot_id, universe_snapshot_content_hash_sha256,
                decision_at, result_schema_version, result_json,
                artifact_count, artifact_inventory_hash_sha256,
                receipt_id, receipt_json, research_only, first_recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                result.run_id,
                result.content_hash(),
                result.preparation.preparation_id,
                result.preparation.content_hash(),
                context.decision_run_id if context is not None else None,
                context.content_hash() if context is not None else None,
                result.dataset_id,
                result.dataset_content_id,
                result.universe_snapshot_id,
                result.universe_snapshot_content_hash,
                result.decision_at.isoformat(),
                result.schema_version,
                result_json,
                len(inventory),
                inventory_hash,
                receipt.receipt_id,
                receipt.to_json(),
                int(result.research_only),
                receipt.recorded_at.isoformat(),
            ),
        )

    def _insert_artifacts(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        inventory: tuple[_ArtifactInventoryItem, ...],
        recorded_at: datetime,
    ) -> None:
        for item in inventory:
            connection.execute(
                """
                INSERT INTO opportunity_run_artifacts (
                    run_id, inventory_ordinal, artifact_family, family_ordinal,
                    artifact_id, evaluation_id, decision_id,
                    artifact_schema_version, payload_json, content_hash_sha256,
                    first_recorded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    run_id,
                    item.inventory_ordinal,
                    item.family.value,
                    item.family_ordinal,
                    item.artifact_id,
                    item.evaluation_id,
                    item.decision_id,
                    item.schema_version,
                    item.payload_json,
                    item.content_hash_sha256,
                    recorded_at.isoformat(),
                ),
            )


def _build_artifact_inventory(
    result: PipelineResult,
) -> tuple[_ArtifactInventoryItem, ...]:
    groups: tuple[
        tuple[
            OpportunityArtifactFamily,
            tuple[tuple[OpportunityContract, str, str | None, str | None], ...],
        ],
        ...,
    ] = (
        (
            OpportunityArtifactFamily.UNIVERSE_SNAPSHOT,
            ((result.preparation.universe_snapshot, result.universe_snapshot_id, None, None),),
        ),
        (
            OpportunityArtifactFamily.PREPARED_PIPELINE,
            ((result.preparation, result.preparation.preparation_id, None, None),),
        ),
        (
            OpportunityArtifactFamily.STRATEGY_EXPECTANCY_BINDING,
            tuple(
                (item, item.binding_id, None, None)
                for item in result.preparation.expectancy_bindings
            ),
        ),
        (
            OpportunityArtifactFamily.CHEAP_FEATURE_SNAPSHOT,
            tuple((item, item.snapshot_id, None, None) for item in result.cheap_snapshots),
        ),
        (
            OpportunityArtifactFamily.RICH_FEATURE_SNAPSHOT,
            tuple((item, item.snapshot_id, None, None) for item in result.rich_snapshots),
        ),
        (
            OpportunityArtifactFamily.BENCHMARK_FEATURE_SNAPSHOT,
            (
                ((result.benchmark_snapshot, result.benchmark_snapshot.snapshot_id, None, None),)
                if result.benchmark_snapshot is not None
                else ()
            ),
        ),
        (
            OpportunityArtifactFamily.OPPORTUNITY_CANDIDATE,
            tuple((item, item.candidate_id, None, None) for item in result.candidates),
        ),
        (
            OpportunityArtifactFamily.MARKET_REGIME,
            ((result.market_regime, result.market_regime.regime_id, None, None),),
        ),
        (
            OpportunityArtifactFamily.SECURITY_REGIME,
            tuple((item, item.regime_id, None, None) for item in result.security_regimes),
        ),
        (
            OpportunityArtifactFamily.STRATEGY_EVALUATION,
            tuple(
                (item, item.evaluation_id, item.evaluation_id, None)
                for item in result.evaluations
            ),
        ),
        (
            OpportunityArtifactFamily.RANKED_OPPORTUNITY,
            tuple(
                (item, item.ranked_id, item.evaluation_id, None)
                for item in result.ranked_opportunities
            ),
        ),
        (
            OpportunityArtifactFamily.PIPELINE_RISK_POLICY,
            ((result.risk_policy, result.risk_policy.risk_policy_id, None, None),),
        ),
        (
            OpportunityArtifactFamily.EXECUTION_RISK_EVIDENCE,
            tuple(
                (item, item.execution_risk_evidence_id, item.evaluation_id, None)
                for item in result.risk_evidence
            ),
        ),
        (
            OpportunityArtifactFamily.DECISION_RUN_CONTEXT,
            (
                ((result.decision_context, result.decision_context.decision_run_id, None, None),)
                if result.decision_context is not None
                else ()
            ),
        ),
        (
            OpportunityArtifactFamily.TRADE_DECISION,
            tuple(
                (item, item.decision_id, item.evaluation_id, item.decision_id)
                for item in result.decisions
            ),
        ),
        (
            OpportunityArtifactFamily.DECISION_TRACE,
            tuple(
                (item, item.trace_id, item.evaluation_id, item.final_decision_id)
                for item in result.traces
            ),
        ),
    )
    inventory: list[_ArtifactInventoryItem] = []
    for family, artifacts in groups:
        for family_ordinal, (artifact, artifact_id, evaluation_id, decision_id) in enumerate(
            artifacts
        ):
            schema_version = getattr(artifact, "schema_version", None)
            if not isinstance(schema_version, str) or not schema_version.strip():
                raise ValueError("opportunity artifact requires a schema version")
            _require_identity(artifact_id, "artifact_id")
            if evaluation_id is not None:
                _require_identity(evaluation_id, "evaluation_id")
            if decision_id is not None:
                _require_identity(decision_id, "decision_id")
            inventory.append(
                _ArtifactInventoryItem(
                    inventory_ordinal=len(inventory),
                    family=family,
                    family_ordinal=family_ordinal,
                    artifact_id=artifact_id,
                    evaluation_id=evaluation_id,
                    decision_id=decision_id,
                    schema_version=schema_version,
                    payload_json=artifact.to_json(),
                    content_hash_sha256=artifact.content_hash(),
                )
            )
    return tuple(inventory)


def _artifact_inventory_hash(inventory: tuple[_ArtifactInventoryItem, ...]) -> str:
    canonical = contract_to_json(tuple(item.identity_tuple() for item in inventory))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _family_counts(
    inventory: tuple[_ArtifactInventoryItem, ...],
) -> tuple[OpportunityArtifactFamilyCount, ...]:
    return tuple(
        OpportunityArtifactFamilyCount(
            family=family,
            count=sum(item.family is family for item in inventory),
        )
        for family in CANONICAL_OPPORTUNITY_ARTIFACT_FAMILIES
    )


def _build_persistence_receipt(
    result: PipelineResult,
    *,
    inventory: tuple[_ArtifactInventoryItem, ...],
    inventory_hash: str,
    recorded_at: datetime,
    database_schema_version: int = OPPORTUNITY_DATABASE_SCHEMA_VERSION,
) -> OpportunityPersistenceReceipt:
    context = result.decision_context
    values: dict[str, Any] = {
        "run_id": result.run_id,
        "run_content_hash_sha256": result.content_hash(),
        "preparation_id": result.preparation.preparation_id,
        "preparation_content_hash_sha256": result.preparation.content_hash(),
        "decision_context_id": context.decision_run_id if context is not None else None,
        "decision_context_content_hash_sha256": (
            context.content_hash() if context is not None else None
        ),
        "decision_at": result.decision_at,
        "family_counts": _family_counts(inventory),
        "artifact_count": len(inventory),
        "artifact_inventory_hash_sha256": inventory_hash,
        "recorded_at": recorded_at,
        "database_schema_version": database_schema_version,
        "research_only": True,
        "schema_version": _SUPPORTED_RECEIPT_SCHEMA_BY_DATABASE_VERSION[
            database_schema_version
        ],
    }
    return OpportunityPersistenceReceipt(
        receipt_id=stable_identity("opportunity-persistence-receipt", values),
        **values,
    )


def _verify_stored_run(
    connection: sqlite3.Connection,
    run_id: str,
    *,
    expected_result: PipelineResult | None = None,
    expected_result_json: str | None = None,
    conflict_on_difference: bool = False,
) -> tuple[PipelineResult, OpportunityPersistenceReceipt]:
    row = connection.execute(
        "SELECT * FROM opportunity_pipeline_runs WHERE run_id = ?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise OpportunityPersistenceIntegrityError("persisted opportunity run is missing")
    try:
        result = PipelineResult.from_json(str(row["result_json"]))
    except (TypeError, ValueError) as exc:
        error_type = (
            OpportunityPersistenceConflictError
            if conflict_on_difference
            else OpportunityPersistenceIntegrityError
        )
        raise error_type(
            "persisted opportunity result JSON is invalid"
        ) from exc
    canonical_result_json = result.to_json()
    result_columns: tuple[tuple[str, object], ...] = (
        ("run_id", result.run_id),
        ("result_content_hash_sha256", result.content_hash()),
        ("preparation_id", result.preparation.preparation_id),
        ("preparation_content_hash_sha256", result.preparation.content_hash()),
        (
            "decision_context_id",
            result.decision_context.decision_run_id
            if result.decision_context is not None
            else None,
        ),
        (
            "decision_context_content_hash_sha256",
            result.decision_context.content_hash()
            if result.decision_context is not None
            else None,
        ),
        ("dataset_id", result.dataset_id),
        ("dataset_content_id", result.dataset_content_id),
        ("universe_snapshot_id", result.universe_snapshot_id),
        (
            "universe_snapshot_content_hash_sha256",
            result.universe_snapshot_content_hash,
        ),
        ("decision_at", result.decision_at.isoformat()),
        ("result_schema_version", result.schema_version),
        ("research_only", int(result.research_only)),
    )
    difference = canonical_result_json != str(row["result_json"]) or any(
        row[name] != value for name, value in result_columns
    )
    if expected_result is not None:
        difference = difference or result != expected_result
    if expected_result_json is not None:
        difference = difference or str(row["result_json"]) != expected_result_json
    if difference:
        error_type = (
            OpportunityPersistenceConflictError
            if conflict_on_difference
            else OpportunityPersistenceIntegrityError
        )
        raise error_type("persisted opportunity run metadata or content does not match")

    inventory = _build_artifact_inventory(result)
    inventory_hash = _artifact_inventory_hash(inventory)
    stored_artifacts = connection.execute(
        """
        SELECT * FROM opportunity_run_artifacts
        WHERE run_id = ?
        ORDER BY inventory_ordinal ASC
        """,
        (run_id,),
    ).fetchall()
    first_recorded_at = str(row["first_recorded_at"])
    artifact_difference = len(stored_artifacts) != len(inventory)
    if not artifact_difference:
        for stored, expected in zip(stored_artifacts, inventory, strict=True):
            stored_values = (
                stored["run_id"],
                stored["inventory_ordinal"],
                stored["artifact_family"],
                stored["family_ordinal"],
                stored["artifact_id"],
                stored["evaluation_id"],
                stored["decision_id"],
                stored["artifact_schema_version"],
                stored["payload_json"],
                stored["content_hash_sha256"],
                stored["first_recorded_at"],
            )
            expected_values = (
                run_id,
                expected.inventory_ordinal,
                expected.family.value,
                expected.family_ordinal,
                expected.artifact_id,
                expected.evaluation_id,
                expected.decision_id,
                expected.schema_version,
                expected.payload_json,
                expected.content_hash_sha256,
                first_recorded_at,
            )
            if stored_values != expected_values:
                artifact_difference = True
                break
    artifact_difference = artifact_difference or (
        int(row["artifact_count"]) != len(inventory)
        or str(row["artifact_inventory_hash_sha256"]) != inventory_hash
    )
    if artifact_difference:
        error_type = (
            OpportunityPersistenceConflictError
            if conflict_on_difference
            else OpportunityPersistenceIntegrityError
        )
        raise error_type("persisted opportunity artifact inventory does not match")

    try:
        receipt = OpportunityPersistenceReceipt.from_json(str(row["receipt_json"]))
        recorded_at = datetime.fromisoformat(first_recorded_at)
        expected_receipt = _build_persistence_receipt(
            result,
            inventory=inventory,
            inventory_hash=inventory_hash,
            recorded_at=recorded_at,
            database_schema_version=receipt.database_schema_version,
        )
    except (TypeError, ValueError) as exc:
        error_type = (
            OpportunityPersistenceConflictError
            if conflict_on_difference
            else OpportunityPersistenceIntegrityError
        )
        raise error_type(
            "persisted opportunity receipt is invalid"
        ) from exc
    if (
        receipt != expected_receipt
        or receipt.to_json() != str(row["receipt_json"])
        or receipt.receipt_id != row["receipt_id"]
    ):
        error_type = (
            OpportunityPersistenceConflictError
            if conflict_on_difference
            else OpportunityPersistenceIntegrityError
        )
        raise error_type("persisted opportunity receipt does not reconcile")
    return result, receipt


def _validate_schema(
    connection: sqlite3.Connection,
    *,
    require_current: bool = False,
) -> int:
    try:
        row = connection.execute(
            "SELECT version FROM schema_version ORDER BY version DESC LIMIT 1"
        ).fetchone()
    except sqlite3.Error as exc:
        raise OpportunityPersistenceIntegrityError(
            "opportunity database schema is absent; call initialize explicitly"
        ) from exc
    version = int(row[0]) if row is not None else 0
    supported_versions = {
        LEGACY_OPPORTUNITY_DATABASE_SCHEMA_VERSION,
        OPPORTUNITY_DATABASE_SCHEMA_VERSION,
        PREVIOUS_STORAGE_SCHEMA_VERSION,
        CURRENT_STORAGE_SCHEMA_VERSION,
    }
    if version not in supported_versions or (
        require_current and version != CURRENT_STORAGE_SCHEMA_VERSION
    ):
        raise OpportunityPersistenceIntegrityError(
            "opportunity store requires schema 27, 28, 29, or 30"
            f"{' (30 for writes)' if require_current else ''}, found {version}"
        )
    expected_columns = {
        "opportunity_pipeline_runs": _RUN_COLUMNS,
        "opportunity_run_artifacts": _ARTIFACT_COLUMNS,
    }
    for table, expected in expected_columns.items():
        columns = {
            str(item[1])
            for item in connection.execute(f"PRAGMA table_info({table})").fetchall()
        }
        if columns != expected:
            raise OpportunityPersistenceIntegrityError(
                f"opportunity store table {table} has an invalid schema"
            )
    schema_objects = {
        str(item[0]): str(item[1])
        for item in connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE sql IS NOT NULL"
        ).fetchall()
    }
    for name, expected_sql in _EXPECTED_SCHEMA_SQL.items():
        actual_sql = schema_objects.get(name)
        if actual_sql is None or _sql_fingerprint(actual_sql) != _sql_fingerprint(
            expected_sql
        ):
            raise OpportunityPersistenceIntegrityError(
                f"opportunity store schema object {name} is missing or invalid"
            )
    governed_objects = {
        str(item[0])
        for item in connection.execute(
            """
            SELECT name FROM sqlite_master
            WHERE type IN ('index', 'trigger')
              AND sql IS NOT NULL
              AND tbl_name IN ('opportunity_pipeline_runs', 'opportunity_run_artifacts')
            """
        ).fetchall()
    }
    expected_governed_objects = set(_EXPECTED_INDEX_COLUMNS) | set(_REQUIRED_TRIGGERS)
    if governed_objects != expected_governed_objects:
        raise OpportunityPersistenceIntegrityError(
            "opportunity store indexes or append-only triggers are not canonical"
        )
    _validate_key_and_index_structures(connection)
    foreign_keys = int(connection.execute("PRAGMA foreign_keys").fetchone()[0])
    if foreign_keys != 1:
        raise OpportunityPersistenceIntegrityError(
            "opportunity store requires foreign key enforcement"
        )
    return version


def _validate_key_and_index_structures(connection: sqlite3.Connection) -> None:
    expected_primary_keys = {
        "opportunity_pipeline_runs": ("run_id",),
        "opportunity_run_artifacts": ("run_id", "inventory_ordinal"),
    }
    expected_unique_keys = {
        "opportunity_pipeline_runs": {
            ("run_id",),
            ("receipt_id",),
        },
        "opportunity_run_artifacts": {
            ("run_id", "inventory_ordinal"),
            ("run_id", "artifact_family", "family_ordinal"),
            ("run_id", "artifact_family", "artifact_id"),
        },
    }
    for table, expected_primary_key in expected_primary_keys.items():
        table_info = connection.execute(f"PRAGMA table_info({table})").fetchall()
        primary_key = tuple(
            str(item[1])
            for item in sorted(
                (item for item in table_info if int(item[5]) > 0),
                key=lambda item: int(item[5]),
            )
        )
        if primary_key != expected_primary_key:
            raise OpportunityPersistenceIntegrityError(
                f"opportunity store primary key for {table} is invalid"
            )
        unique_keys: set[tuple[str, ...]] = set()
        for index in connection.execute(f"PRAGMA index_list({table})").fetchall():
            index_name = str(index[1])
            if int(index[2]) == 1:
                unique_keys.add(
                    tuple(
                        str(item[2])
                        for item in connection.execute(
                            f"PRAGMA index_info({index_name})"
                        ).fetchall()
                    )
                )
        if unique_keys != expected_unique_keys[table]:
            raise OpportunityPersistenceIntegrityError(
                f"opportunity store unique constraints for {table} are invalid"
            )
    for index_name, expected_columns in _EXPECTED_INDEX_COLUMNS.items():
        columns = tuple(
            str(item[2])
            for item in connection.execute(f"PRAGMA index_info({index_name})").fetchall()
        )
        if columns != expected_columns:
            raise OpportunityPersistenceIntegrityError(
                f"opportunity store index {index_name} has invalid columns"
            )
    foreign_keys = connection.execute(
        "PRAGMA foreign_key_list(opportunity_run_artifacts)"
    ).fetchall()
    actual_foreign_keys = {
        (
            str(item[2]),
            str(item[3]),
            str(item[4]),
            str(item[5]),
            str(item[6]),
            str(item[7]),
        )
        for item in foreign_keys
    }
    if actual_foreign_keys != {
        (
            "opportunity_pipeline_runs",
            "run_id",
            "run_id",
            "NO ACTION",
            "NO ACTION",
            "NONE",
        )
    }:
        raise OpportunityPersistenceIntegrityError(
            "opportunity artifact foreign key is missing or invalid"
        )
    if connection.execute(
        "PRAGMA foreign_key_list(opportunity_pipeline_runs)"
    ).fetchall():
        raise OpportunityPersistenceIntegrityError(
            "opportunity pipeline run table has unexpected foreign keys"
        )


def _sql_fingerprint(value: str) -> str:
    normalized: list[str] = []
    quote: str | None = None
    index = 0
    while index < len(value):
        character = value[index]
        if quote is not None:
            normalized.append(character)
            if character == quote:
                if index + 1 < len(value) and value[index + 1] == quote:
                    normalized.append(value[index + 1])
                    index += 1
                else:
                    quote = None
        elif character in {"'", '"', "`"}:
            quote = character
            normalized.append(character)
        elif character.isspace():
            pass
        else:
            normalized.append(character.lower())
        index += 1
    return "".join(normalized).removesuffix(";")


def _validate_recorded_at(recorded_at: datetime, decision_at: datetime) -> None:
    if not isinstance(recorded_at, datetime):
        raise TypeError("recorded_at must be datetime")
    if recorded_at.tzinfo is None or recorded_at.utcoffset() is None:
        raise ValueError("recorded_at must be timezone-aware UTC")
    if recorded_at.utcoffset() != timedelta(0):
        raise ValueError("recorded_at must use UTC")
    if recorded_at < decision_at:
        raise ValueError("recorded_at cannot precede the pipeline decision time")


def _require_identity(value: str, field_name: str) -> None:
    if not _IDENTITY_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a sanitized nonblank identity")


def _require_hash(value: str | None, field_name: str) -> None:
    if value is None or not _HASH_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
