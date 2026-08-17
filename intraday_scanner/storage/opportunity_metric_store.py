"""Append-only SQLite adapter for discovery metric reports."""

from __future__ import annotations

import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, cast

from intraday_scanner.errors import StorageError
from intraday_scanner.storage.migrations import run_migrations
from intraday_scanner.storage.opportunity_metric_errors import (
    OpportunityMetricConflictError,
    OpportunityMetricIntegrityError,
    OpportunityMetricReadOnlyError,
    OpportunityMetricStaleParentError,
    OpportunityMetricStoreError,
)
from intraday_scanner.storage.opportunity_metric_inventory import (
    MetricReport,
    build_metric_bindings,
    build_metric_inventory,
    build_metric_receipt,
    metric_inventory_hash,
)
from intraday_scanner.storage.opportunity_metric_rows import (
    _binding_row,
    _chain_index,
    _chain_item,
    _receipt_row,
)
from intraday_scanner.storage.opportunity_metric_schema import (
    BINDING_INSERT_ORDER,
    RECEIPT_INSERT_ORDER,
    validate_metric_schema,
)
from intraday_scanner.storage.opportunity_metric_verification import (
    _current_shape_from_historical,
    _current_shape_from_historical_metric,
    _embedded_outcome_head_key,
    _MetricVerificationContext,
)
from intraday_scanner.storage.opportunity_miss_errors import (
    OpportunityMissStaleParentError,
    OpportunityMissStoreError,
)
from intraday_scanner.storage.opportunity_miss_store import (
    _audit_analysis_chain,
    _verify_current_parents,
)
from intraday_scanner.storage.read_only import connect_read_only
from intraday_scanner.v2.opportunity.miss_contracts import (
    require_identity,
    require_sanitized,
)
from intraday_scanner.v2.opportunity.miss_metric_persistence import (
    CurrentMultiMetricReplay,
    CurrentSessionMetricReplay,
    HistoricalMetricReplay,
    MetricReportKind,
    MetricSessionReportBinding,
    OpportunityMetricPersistenceReceipt,
)
from intraday_scanner.v2.opportunity.miss_metric_reconciliation import (
    DiscoveryMetricReport,
    SessionDiscoveryMetricReport,
)
from intraday_scanner.v2.opportunity.miss_persistence import (
    CurrentMissReplay,
    HistoricalMissReplay,
)
from intraday_scanner.v2.opportunity.models import stable_identity


class OpportunityMetricStore:
    def __init__(self, db_path: str | Path, *, read_only: bool = False):
        self._db_path = Path(db_path)
        self._read_only = read_only

    def initialize(self) -> None:
        if self._read_only:
            raise OpportunityMetricReadOnlyError("read-only metric store cannot initialize")
        connection = self._connect_writable(require_existing=False)
        try:
            run_migrations(connection)
            connection.execute("PRAGMA foreign_keys=ON")
            validate_metric_schema(connection)
            connection.commit()
        except OpportunityMetricStoreError:
            connection.rollback()
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise OpportunityMetricIntegrityError(
                f"could not initialize metric store: {exc}"
            ) from exc
        finally:
            connection.close()

    def append_session(
        self,
        report: SessionDiscoveryMetricReport,
        *,
        current_miss_replay: CurrentMissReplay,
        persisted_at: datetime,
        supersedes_metric_receipt_id: str | None = None,
    ) -> OpportunityMetricPersistenceReceipt:
        try:
            return self._append(
                report,
                persisted_at=persisted_at,
                parent_miss=current_miss_replay,
                children=(),
                bindings=(),
                supersedes_metric_receipt_id=supersedes_metric_receipt_id,
            )
        except OpportunityMetricStoreError:
            raise
        except (TypeError, ValueError) as exc:
            raise OpportunityMetricConflictError(
                f"invalid session metric append request: {exc}"
            ) from exc

    def append_multi(
        self,
        report: DiscoveryMetricReport,
        *,
        current_session_metric_replays: tuple[CurrentSessionMetricReplay, ...],
        persisted_at: datetime,
        supersedes_metric_receipt_id: str | None = None,
    ) -> OpportunityMetricPersistenceReceipt:
        try:
            bindings = build_metric_bindings(report, current_session_metric_replays)
            return self._append(
                report,
                persisted_at=persisted_at,
                parent_miss=None,
                children=current_session_metric_replays,
                bindings=bindings,
                supersedes_metric_receipt_id=supersedes_metric_receipt_id,
            )
        except OpportunityMetricStoreError:
            raise
        except (TypeError, ValueError) as exc:
            raise OpportunityMetricConflictError(
                f"invalid multi metric append request: {exc}"
            ) from exc

    def _append(
        self,
        report: MetricReport,
        *,
        persisted_at: datetime,
        parent_miss: CurrentMissReplay | None,
        children: tuple[CurrentSessionMetricReplay, ...],
        bindings: tuple[MetricSessionReportBinding, ...],
        supersedes_metric_receipt_id: str | None,
    ) -> OpportunityMetricPersistenceReceipt:
        if self._read_only:
            raise OpportunityMetricReadOnlyError("read-only metric store cannot append")
        _validate_persisted_at(persisted_at, report.recorded_at)
        candidate = build_metric_receipt(
            report,
            persisted_at=persisted_at,
            predecessor=None,
            parent_miss=parent_miss,
            children=children,
            bindings=bindings,
        )
        connection = self._connect_writable(require_existing=True)
        try:
            connection.execute("BEGIN IMMEDIATE")
            validate_metric_schema(connection)
            context = _MetricVerificationContext(connection)
            existing = connection.execute(
                "SELECT metric_receipt_id,scope_key FROM opportunity_metric_receipts "
                "WHERE report_id=?",
                (report.report_id,),
            ).fetchone()
            if existing is not None:
                chain = _audit_metric_chain(
                    connection, str(existing["scope_key"]), context
                )
                receipt, stored_report, stored_bindings = _chain_item(
                    chain, str(existing["metric_receipt_id"])
                )
                if (
                    stored_report != report
                    or receipt.scope_key != candidate.scope_key
                    or receipt.parent_miss_receipt_id
                    != candidate.parent_miss_receipt_id
                    or receipt.parent_miss_receipt_content_hash_sha256
                    != candidate.parent_miss_receipt_content_hash_sha256
                    or stored_bindings != bindings
                    or receipt.supersedes_metric_receipt_id
                    != supersedes_metric_receipt_id
                    or persisted_at < receipt.persisted_at
                ):
                    raise OpportunityMetricConflictError(
                        "stored metric report conflicts with requested content or lineage"
                    )
                connection.rollback()
                return receipt
            chain = _audit_metric_chain(connection, candidate.scope_key, context)
            predecessor = chain[-1][0] if chain else None
            predecessor_report = chain[-1][1] if chain else None
            if predecessor is None:
                if supersedes_metric_receipt_id is not None:
                    raise OpportunityMetricConflictError(
                        "initial metric report cannot declare predecessor"
                    )
            else:
                if supersedes_metric_receipt_id != predecessor.metric_receipt_id:
                    raise OpportunityMetricConflictError(
                        "metric correction must supersede exact current head"
                    )
                if (
                    persisted_at <= predecessor.persisted_at
                    or report.recorded_at is None
                    or predecessor_report is None
                    or predecessor_report.recorded_at is None
                    or report.recorded_at <= predecessor_report.recorded_at
                ):
                    raise OpportunityMetricConflictError(
                        "metric correction chronology must strictly advance"
                    )
            _verify_append_parents(
                connection, report, parent_miss, children, context
            )
            receipt = build_metric_receipt(
                report,
                persisted_at=persisted_at,
                predecessor=predecessor,
                parent_miss=parent_miss,
                children=children,
                bindings=bindings,
            )
            self._insert_receipt(connection, receipt, report)
            self._insert_bindings(connection, receipt, bindings)
            post_insert_context = _MetricVerificationContext(connection)
            verified = _audit_metric_chain(
                connection, receipt.scope_key, post_insert_context
            )
            if not verified or verified[-1] != (receipt, report, bindings):
                raise OpportunityMetricIntegrityError(
                    "post-insert metric chain verification failed"
                )
            _verify_append_parents(
                connection,
                report,
                parent_miss,
                children,
                post_insert_context,
            )
            connection.commit()
            return receipt
        except OpportunityMetricStoreError:
            connection.rollback()
            raise
        except sqlite3.IntegrityError as exc:
            connection.rollback()
            raise OpportunityMetricConflictError(
                f"metric append violates immutable constraints: {exc}"
            ) from exc
        except (sqlite3.Error, TypeError, ValueError) as exc:
            connection.rollback()
            raise OpportunityMetricIntegrityError(
                f"could not append metric report: {exc}"
            ) from exc
        finally:
            connection.close()

    def load_receipt(self, metric_receipt_id: str):
        _validate_lookup_identity(metric_receipt_id, "metric_receipt_id")
        return self._load(metric_receipt_id, report=False)

    def load_report(self, metric_receipt_id: str):
        _validate_lookup_identity(metric_receipt_id, "metric_receipt_id")
        return self._load(metric_receipt_id, report=True)

    def _load(self, metric_receipt_id: str, *, report: bool):
        connection = self._connect_read()
        try:
            connection.execute("BEGIN")
            validate_metric_schema(connection)
            context = _MetricVerificationContext(connection)
            row = connection.execute(
                "SELECT scope_key FROM opportunity_metric_receipts WHERE metric_receipt_id=?",
                (metric_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            item = _chain_item(
                _audit_metric_chain(connection, str(row["scope_key"]), context),
                metric_receipt_id,
            )
            return item[1] if report else item[0]
        except OpportunityMetricStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityMetricIntegrityError(f"could not load metric: {exc}") from exc
        finally:
            connection.rollback()
            connection.close()

    def replay_historical(self, metric_receipt_id: str) -> HistoricalMetricReplay | None:
        _validate_lookup_identity(metric_receipt_id, "metric_receipt_id")
        connection = self._connect_read()
        try:
            connection.execute("BEGIN")
            validate_metric_schema(connection)
            context = _MetricVerificationContext(connection)
            row = connection.execute(
                "SELECT scope_key FROM opportunity_metric_receipts WHERE metric_receipt_id=?",
                (metric_receipt_id,),
            ).fetchone()
            if row is None:
                return None
            chain = _audit_metric_chain(
                connection, str(row["scope_key"]), context
            )
            index = _chain_index(chain, metric_receipt_id)
            prefix = chain[: index + 1]
            receipt, report, bindings = prefix[-1]
            miss_replay = None
            child_replays: tuple[HistoricalMetricReplay, ...] = ()
            if receipt.report_kind is MetricReportKind.SESSION:
                miss_replay = _historical_miss_replay(
                    connection,
                    cast(str, receipt.parent_miss_receipt_id),
                    context,
                )
            else:
                child_replays = tuple(
                    _historical_metric_replay(
                        connection, item.child_metric_receipt_id, context
                    )
                    for item in bindings
                )
            values: dict[str, Any] = {
                "metric_persistence_receipt": receipt,
                "metric_report": report,
                "chain_prefix_receipts": tuple(item[0] for item in prefix),
                "chain_prefix_reports": tuple(item[1] for item in prefix),
                "historical_miss_replay": miss_replay,
                "session_bindings": bindings,
                "historical_child_metric_replays": child_replays,
                "research_only": True,
                "promotion_eligible": False,
                "schema_version": "v2.opportunity.historical_metric_replay.v1",
            }
            return HistoricalMetricReplay(
                replay_id=stable_identity("historical-opportunity-metric-replay", values),
                **values,
            )
        except OpportunityMetricStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityMetricIntegrityError(
                f"could not replay historical metric: {exc}"
            ) from exc
        finally:
            connection.rollback()
            connection.close()

    def replay_current(self, scope_key: str):
        _validate_lookup_identity(scope_key, "scope_key")
        connection = self._connect_read()
        try:
            connection.execute("BEGIN")
            validate_metric_schema(connection)
            context = _MetricVerificationContext(connection)
            chain = _audit_metric_chain(connection, scope_key, context)
            if not chain:
                return None
            receipt, report, bindings = chain[-1]
            if receipt.report_kind is MetricReportKind.SESSION:
                miss = _current_miss_replay(
                    connection,
                    cast(str, receipt.parent_miss_receipt_id),
                    context,
                )
                values: dict[str, Any] = {
                    "metric_persistence_receipt": receipt,
                    "metric_report": report,
                    "full_chain_receipts": tuple(item[0] for item in chain),
                    "full_chain_reports": tuple(item[1] for item in chain),
                    "current_miss_replay": miss,
                    "research_only": True,
                    "promotion_eligible": False,
                    "schema_version": "v2.opportunity.current_session_metric_replay.v1",
                }
                return CurrentSessionMetricReplay(
                    replay_id=stable_identity(
                        "current-session-opportunity-metric-replay", values
                    ),
                    **values,
                )
            children = tuple(
                _current_session_metric_replay(
                    connection,
                    item.child_metric_scope_key,
                    expected_receipt_id=item.child_metric_receipt_id,
                    expected_receipt_hash=(
                        item.child_metric_receipt_content_hash_sha256
                    ),
                    context=context,
                )
                for item in bindings
            )
            values = {
                "metric_persistence_receipt": receipt,
                "metric_report": report,
                "full_chain_receipts": tuple(item[0] for item in chain),
                "full_chain_reports": tuple(item[1] for item in chain),
                "session_bindings": bindings,
                "current_child_metric_replays": children,
                "research_only": True,
                "promotion_eligible": False,
                "schema_version": "v2.opportunity.current_multi_metric_replay.v1",
            }
            return CurrentMultiMetricReplay(
                replay_id=stable_identity("current-multi-opportunity-metric-replay", values),
                **values,
            )
        except OpportunityMetricStaleParentError:
            raise
        except OpportunityMetricStoreError:
            raise
        except (sqlite3.Error, TypeError, ValueError) as exc:
            raise OpportunityMetricIntegrityError(
                f"could not replay current metric: {exc}"
            ) from exc
        finally:
            connection.rollback()
            connection.close()

    def _connect_writable(self, *, require_existing: bool) -> sqlite3.Connection:
        if require_existing and not self._db_path.is_file():
            raise OpportunityMetricIntegrityError("metric database is absent")
        try:
            connection = sqlite3.connect(self._db_path, timeout=30)
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except sqlite3.Error as exc:
            raise OpportunityMetricIntegrityError("could not open metric database") from exc

    def _connect_read(self) -> sqlite3.Connection:
        try:
            connection = connect_read_only(self._db_path, row_factory=sqlite3.Row)
            connection.execute("PRAGMA foreign_keys=ON")
            return connection
        except (StorageError, sqlite3.Error) as exc:
            raise OpportunityMetricIntegrityError(
                f"could not open read-only metric database: {exc}"
            ) from exc

    def _insert_receipt(self, connection, receipt, report) -> None:
        values = _receipt_row(receipt, report)
        connection.execute(
            f"INSERT INTO opportunity_metric_receipts ({','.join(RECEIPT_INSERT_ORDER)}) "  # nosec B608 -- immutable module-owned column tuple; values remain bound parameters
            f"VALUES ({','.join('?' for _ in RECEIPT_INSERT_ORDER)})",
            values,
        )

    def _insert_bindings(self, connection, receipt, bindings) -> None:
        sql = (
            f"INSERT INTO opportunity_metric_session_bindings "  # nosec B608 -- fixed table and immutable module-owned columns; values remain bound
            f"({','.join(BINDING_INSERT_ORDER)}) "
            f"VALUES ({','.join('?' for _ in BINDING_INSERT_ORDER)})"
        )
        for binding in bindings:
            connection.execute(sql, _binding_row(receipt, binding))


def _verify_append_parents(connection, report, parent_miss, children, context) -> None:
    context.assert_connection(connection)
    if isinstance(report, SessionDiscoveryMetricReport):
        if parent_miss is None:
            raise OpportunityMetricConflictError("session metric requires miss parent")
        current = _current_miss_replay(
            connection,
            parent_miss.miss_persistence_receipt.miss_receipt_id,
            context,
        )
        if current != parent_miss or report.session_evidence.miss_batch != current.miss_batch:
            raise OpportunityMetricStaleParentError("session metric miss parent is stale")
    else:
        if len(children) != len(report.session_reports):
            raise OpportunityMetricConflictError("multi metric child set is not exact")
        for child, session in zip(children, report.session_reports, strict=True):
            current = _current_session_metric_replay(
                connection,
                child.metric_persistence_receipt.scope_key,
                expected_receipt_id=child.metric_persistence_receipt.metric_receipt_id,
                expected_receipt_hash=child.metric_persistence_receipt.content_hash(),
                context=context,
            )
            if current != child or child.metric_report != session:
                raise OpportunityMetricStaleParentError("multi metric child is stale")


def _verify_stored_receipt(connection, metric_receipt_id, context):
    context.assert_connection(connection)
    cached = context.metric_receipts.get(metric_receipt_id)
    if cached is not None:
        return cached
    context.enter("metric_receipt", metric_receipt_id)
    try:
        verified = _compute_stored_receipt(connection, metric_receipt_id, context)
    finally:
        context.leave("metric_receipt", metric_receipt_id)
    context.metric_receipts[metric_receipt_id] = verified
    return verified


def _compute_stored_receipt(connection, metric_receipt_id, context):
    row = connection.execute(
        "SELECT * FROM opportunity_metric_receipts WHERE metric_receipt_id=?",
        (metric_receipt_id,),
    ).fetchone()
    if row is None:
        raise OpportunityMetricIntegrityError("persisted metric receipt is missing")
    try:
        receipt = OpportunityMetricPersistenceReceipt.from_json(str(row["receipt_json"]))
        if receipt.report_kind is MetricReportKind.SESSION:
            report: MetricReport = SessionDiscoveryMetricReport.from_json(str(row["report_json"]))
        else:
            report = DiscoveryMetricReport.from_json(str(row["report_json"]))
    except (TypeError, ValueError) as exc:
        raise OpportunityMetricIntegrityError("persisted metric JSON is invalid") from exc
    stored_bindings = connection.execute(
        "SELECT * FROM opportunity_metric_session_bindings WHERE metric_receipt_id=? "
        "ORDER BY session_ordinal",
        (metric_receipt_id,),
    ).fetchall()
    try:
        bindings = tuple(
            MetricSessionReportBinding.from_json(str(item["binding_json"]))
            for item in stored_bindings
        )
    except (TypeError, ValueError) as exc:
        raise OpportunityMetricIntegrityError("persisted metric binding JSON is invalid") from exc
    predecessor = None
    if receipt.supersedes_metric_receipt_id is not None:
        predecessor_row = connection.execute(
            "SELECT receipt_json FROM opportunity_metric_receipts WHERE metric_receipt_id=?",
            (receipt.supersedes_metric_receipt_id,),
        ).fetchone()
        if predecessor_row is None:
            raise OpportunityMetricIntegrityError("persisted metric predecessor is missing")
        predecessor = OpportunityMetricPersistenceReceipt.from_json(
            str(predecessor_row["receipt_json"])
        )
    parent_miss = None
    children: tuple[CurrentSessionMetricReplay, ...] = ()
    if receipt.report_kind is MetricReportKind.SESSION:
        historical = _historical_miss_replay(
            connection, cast(str, receipt.parent_miss_receipt_id), context
        )
        parent_miss = _current_shape_from_historical(historical)
    else:
        children = tuple(
            _current_shape_from_historical_metric(
                _historical_metric_replay(
                    connection, item.child_metric_receipt_id, context
                )
            )
            for item in bindings
        )
    expected = build_metric_receipt(
        report,
        persisted_at=datetime.fromisoformat(str(row["persisted_at"])),
        predecessor=predecessor,
        parent_miss=parent_miss,
        children=children,
        bindings=bindings,
    )
    if (
        receipt != expected
        or tuple(row[name] for name in RECEIPT_INSERT_ORDER)
        != _receipt_row(receipt, report)
    ):
        raise OpportunityMetricIntegrityError("persisted metric receipt does not reconcile")
    if len(stored_bindings) != len(bindings):
        raise OpportunityMetricIntegrityError("persisted metric binding count is invalid")
    for stored, binding in zip(stored_bindings, bindings, strict=True):
        if tuple(stored[name] for name in BINDING_INSERT_ORDER) != _binding_row(receipt, binding):
            raise OpportunityMetricIntegrityError("persisted metric binding does not reconcile")
    inventory = build_metric_inventory(report, bindings)
    if receipt.artifact_inventory_hash_sha256 != metric_inventory_hash(inventory):
        raise OpportunityMetricIntegrityError("persisted metric inventory hash is invalid")
    return receipt, report, bindings


def _audit_metric_chain(connection, scope_key, context):
    context.assert_connection(connection)
    if scope_key in context.metric_chains:
        return context.metric_chains[scope_key]
    context.enter("metric_chain", scope_key)
    try:
        chain = _compute_metric_chain(connection, scope_key, context)
    finally:
        context.leave("metric_chain", scope_key)
    context.metric_chains[scope_key] = chain
    return chain


def _compute_metric_chain(connection, scope_key, context):
    rows = connection.execute(
        "SELECT metric_receipt_id,supersedes_metric_receipt_id "
        "FROM opportunity_metric_receipts WHERE scope_key=?",
        (scope_key,),
    ).fetchall()
    if not rows:
        return ()
    ids = {str(row["metric_receipt_id"]) for row in rows}
    roots = [
        str(row["metric_receipt_id"])
        for row in rows
        if row["supersedes_metric_receipt_id"] is None
    ]
    if len(roots) != 1:
        raise OpportunityMetricIntegrityError("metric chain requires exactly one root")
    successors = {}
    for row in rows:
        predecessor = row["supersedes_metric_receipt_id"]
        if predecessor is not None:
            key = str(predecessor)
            if key not in ids or key in successors:
                raise OpportunityMetricIntegrityError("metric chain has orphan or fork")
            successors[key] = str(row["metric_receipt_id"])
    ordered = []
    current: str | None = roots[0]
    while current is not None:
        if current in ordered:
            raise OpportunityMetricIntegrityError("metric chain has cycle")
        ordered.append(current)
        current = successors.get(current)
    if set(ordered) != ids:
        raise OpportunityMetricIntegrityError("metric chain is disconnected")
    chain = tuple(
        _verify_stored_receipt(connection, item, context) for item in ordered
    )
    for index, (receipt, report, _bindings) in enumerate(chain[1:], 1):
        previous_receipt, previous_report, _ = chain[index - 1]
        if (
            receipt.supersedes_metric_receipt_id != previous_receipt.metric_receipt_id
            or receipt.supersedes_metric_receipt_content_hash_sha256
            != previous_receipt.content_hash()
            or receipt.persisted_at <= previous_receipt.persisted_at
            or report.recorded_at is None
            or previous_report.recorded_at is None
            or report.recorded_at <= previous_report.recorded_at
        ):
            raise OpportunityMetricIntegrityError("metric chain chronology is invalid")
    return chain


def _historical_miss_replay(connection, miss_receipt_id, context):
    context.assert_connection(connection)
    cached = context.historical_miss_replays.get(miss_receipt_id)
    if cached is not None:
        return cached
    context.enter("historical_miss", miss_receipt_id)
    try:
        replay = _compute_historical_miss_replay(
            connection, miss_receipt_id, context
        )
    finally:
        context.leave("historical_miss", miss_receipt_id)
    context.historical_miss_replays[miss_receipt_id] = replay
    return replay


def _compute_historical_miss_replay(connection, miss_receipt_id, context):
    row = connection.execute(
        "SELECT analysis_key FROM opportunity_miss_receipts WHERE miss_receipt_id=?",
        (miss_receipt_id,),
    ).fetchone()
    if row is None:
        raise OpportunityMetricIntegrityError("metric miss parent is missing")
    chain = _metric_audit_miss_chain(
        connection, str(row["analysis_key"]), context
    )
    index = next(
        (
            i
            for i, item in enumerate(chain)
            if item[0].miss_receipt_id == miss_receipt_id
        ),
        -1,
    )
    if index < 0:
        raise OpportunityMetricIntegrityError("metric miss parent is outside chain")
    prefix = chain[: index + 1]
    receipt, batch = prefix[-1]
    values = {
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
        replay_id=stable_identity("historical-opportunity-miss-replay", values), **values
    )


def _current_miss_replay(connection, miss_receipt_id, context):
    context.assert_connection(connection)
    historical = _historical_miss_replay(connection, miss_receipt_id, context)
    chain = _metric_audit_miss_chain(
        connection, historical.miss_persistence_receipt.analysis_key, context
    )
    head = chain[-1][0]
    if head.miss_receipt_id != miss_receipt_id:
        raise OpportunityMetricStaleParentError("metric miss parent is not current")
    outcome_heads = _embedded_outcome_head_key(historical.miss_batch)
    key = (
        historical.miss_persistence_receipt.analysis_key,
        head.miss_receipt_id,
        head.content_hash(),
        outcome_heads,
    )
    cached = context.current_miss_replays.get(key)
    if cached is not None:
        return cached
    context.enter("current_miss", key)
    try:
        _metric_verify_current_miss_parents(
            connection, historical.miss_batch, key, context
        )
        current = _current_shape_from_historical(historical)
    finally:
        context.leave("current_miss", key)
    context.current_miss_replays[key] = current
    return current


def _historical_metric_replay(connection, metric_receipt_id, context):
    context.assert_connection(connection)
    cached = context.historical_metric_replays.get(metric_receipt_id)
    if cached is not None:
        return cached
    context.enter("historical_metric", metric_receipt_id)
    try:
        replay = _compute_historical_metric_replay(
            connection, metric_receipt_id, context
        )
    finally:
        context.leave("historical_metric", metric_receipt_id)
    context.historical_metric_replays[metric_receipt_id] = replay
    return replay


def _compute_historical_metric_replay(connection, metric_receipt_id, context):
    row = connection.execute(
        "SELECT scope_key FROM opportunity_metric_receipts WHERE metric_receipt_id=?",
        (metric_receipt_id,),
    ).fetchone()
    if row is None:
        raise OpportunityMetricIntegrityError("child metric receipt is missing")
    chain = _audit_metric_chain(connection, str(row["scope_key"]), context)
    index = _chain_index(chain, metric_receipt_id)
    prefix = chain[: index + 1]
    receipt, report, bindings = prefix[-1]
    miss = None
    children = ()
    if receipt.report_kind is MetricReportKind.SESSION:
        miss = _historical_miss_replay(
            connection, cast(str, receipt.parent_miss_receipt_id), context
        )
    else:
        children = tuple(
            _historical_metric_replay(
                connection, item.child_metric_receipt_id, context
            )
            for item in bindings
        )
    values = {
        "metric_persistence_receipt": receipt,
        "metric_report": report,
        "chain_prefix_receipts": tuple(item[0] for item in prefix),
        "chain_prefix_reports": tuple(item[1] for item in prefix),
        "historical_miss_replay": miss,
        "session_bindings": bindings,
        "historical_child_metric_replays": children,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.historical_metric_replay.v1",
    }
    return HistoricalMetricReplay(
        replay_id=stable_identity("historical-opportunity-metric-replay", values), **values
    )


def _current_session_metric_replay(
    connection,
    scope_key,
    *,
    expected_receipt_id=None,
    expected_receipt_hash=None,
    context,
):
    context.assert_connection(connection)
    chain = _audit_metric_chain(connection, scope_key, context)
    if not chain or chain[-1][0].report_kind is not MetricReportKind.SESSION:
        raise OpportunityMetricStaleParentError("child session metric head is missing")
    head = chain[-1][0]
    if (
        expected_receipt_id is not None
        and (
            head.metric_receipt_id != expected_receipt_id
            or head.content_hash() != expected_receipt_hash
        )
    ):
        raise OpportunityMetricStaleParentError(
            "child session metric binding is not the current exact head"
        )
    key = (scope_key, head.metric_receipt_id, head.content_hash())
    cached = context.current_session_replays.get(key)
    if cached is not None:
        return cached
    context.enter("current_session", key)
    try:
        historical = _historical_metric_replay(
            connection, head.metric_receipt_id, context
        )
        if not isinstance(historical.metric_report, SessionDiscoveryMetricReport):
            raise OpportunityMetricIntegrityError(
                "multi binding child must be a SESSION metric report"
            )
        current = _current_shape_from_historical_metric(historical)
        current_miss = _current_miss_replay(
            connection,
            current.current_miss_replay.miss_persistence_receipt.miss_receipt_id,
            context,
        )
        if current_miss != current.current_miss_replay:
            raise OpportunityMetricStaleParentError(
                "child session metric miss parent is stale"
            )
    finally:
        context.leave("current_session", key)
    context.current_session_replays[key] = current
    return current


def _validate_persisted_at(persisted_at, recorded_at):
    if persisted_at.tzinfo is None or persisted_at.utcoffset() is None:
        raise ValueError("persisted_at must be timezone-aware")
    if persisted_at.utcoffset().total_seconds() != 0:
        raise ValueError("persisted_at must be UTC")
    if recorded_at is not None and persisted_at < recorded_at:
        raise ValueError("persisted_at cannot precede metric report")


def _validate_lookup_identity(value, field_name):
    try:
        require_identity(value, field_name)
        require_sanitized(value, field_name)
    except (TypeError, ValueError) as exc:
        raise OpportunityMetricIntegrityError(
            f"invalid metric lookup {field_name}"
        ) from exc


def _metric_audit_miss_chain(connection, analysis_key, context):
    context.assert_connection(connection)
    cached = context.miss_chains.get(analysis_key)
    if cached is not None:
        return cached
    context.enter("miss_chain", analysis_key)
    try:
        chain = _audit_analysis_chain(connection, analysis_key)
    except OpportunityMissStoreError as exc:
        raise OpportunityMetricIntegrityError(
            f"metric miss parent chain is invalid: {exc}"
        ) from exc
    finally:
        context.leave("miss_chain", analysis_key)
    context.miss_chains[analysis_key] = chain
    return chain


def _metric_verify_current_miss_parents(connection, batch, key, context):
    context.assert_connection(connection)
    if key in context.current_miss_parent_results:
        return context.current_miss_parent_results[key]
    context.enter("current_miss_parent", key)
    try:
        parents = _verify_current_parents(connection, batch)
    except OpportunityMissStaleParentError as exc:
        raise OpportunityMetricStaleParentError(
            f"metric miss parent has advanced: {exc}"
        ) from exc
    except OpportunityMissStoreError as exc:
        raise OpportunityMetricIntegrityError(
            f"metric miss parent is invalid: {exc}"
        ) from exc
    finally:
        context.leave("current_miss_parent", key)
    context.current_miss_parent_results[key] = parents
    return parents


__all__ = [
    "OpportunityMetricConflictError", "OpportunityMetricIntegrityError",
    "OpportunityMetricReadOnlyError", "OpportunityMetricStaleParentError",
    "OpportunityMetricStore", "OpportunityMetricStoreError",
]
