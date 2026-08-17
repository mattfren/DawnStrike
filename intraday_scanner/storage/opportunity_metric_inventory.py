"""Pure scope, binding, inventory, and receipt construction for metrics."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from intraday_scanner.v2.contracts import contract_to_json
from intraday_scanner.v2.opportunity.miss_metric_persistence import (
    CANONICAL_METRIC_ARTIFACT_FAMILIES,
    CurrentSessionMetricReplay,
    MetricArtifactFamily,
    MetricArtifactFamilyCount,
    MetricPersistenceKind,
    MetricReportKind,
    MetricSessionReportBinding,
    OpportunityMetricPersistenceReceipt,
)
from intraday_scanner.v2.opportunity.miss_metric_reconciliation import (
    DiscoveryMetricReport,
    SessionDiscoveryMetricReport,
)
from intraday_scanner.v2.opportunity.miss_persistence import CurrentMissReplay
from intraday_scanner.v2.opportunity.models import stable_identity

MetricReport = SessionDiscoveryMetricReport | DiscoveryMetricReport


@dataclass(frozen=True)
class MetricInventoryItem:
    inventory_ordinal: int
    family: MetricArtifactFamily
    family_ordinal: int
    artifact_id: str
    exchange_session_id: str | None
    schema_version: str
    content_hash_sha256: str

    def identity_tuple(self) -> tuple[int, str, int, str, str | None, str, str]:
        return (
            self.inventory_ordinal,
            self.family.value,
            self.family_ordinal,
            self.artifact_id,
            self.exchange_session_id,
            self.schema_version,
            self.content_hash_sha256,
        )


def session_metric_scope_key(
    report: SessionDiscoveryMetricReport,
    parent: CurrentMissReplay,
) -> str:
    source = report.session_evidence.miss_batch.qualification_batch.source.scope_receipt
    return stable_identity(
        "opportunity-session-metric-scope",
        {
            "metric_policy_id": report.session_evidence.metric_policy.metric_policy_id,
            "metric_policy_content_hash_sha256": (
                report.session_evidence.metric_policy.content_hash()
            ),
            "exchange_session_id": source.exchange_session_id,
            "session_open_at": source.session_open_at,
            "session_close_at": source.session_close_at,
            "miss_analysis_key": parent.miss_persistence_receipt.analysis_key,
            "schema_version": "v2.opportunity.session_metric_scope.v1",
        },
    )


def multi_metric_scope_key(
    report: DiscoveryMetricReport,
    children: tuple[CurrentSessionMetricReplay, ...],
) -> str:
    return stable_identity(
        "opportunity-multi-metric-scope",
        {
            "metric_policy_id": report.metric_policy_id,
            "metric_policy_content_hash_sha256": report.metric_policy_content_hash_sha256,
            "child_session_metric_scope_keys": tuple(
                child.metric_persistence_receipt.scope_key for child in children
            ),
            "schema_version": "v2.opportunity.multi_metric_scope.v1",
        },
    )


def build_metric_bindings(
    report: DiscoveryMetricReport,
    children: tuple[CurrentSessionMetricReplay, ...],
) -> tuple[MetricSessionReportBinding, ...]:
    scope_key = multi_metric_scope_key(report, children)
    bindings = []
    for ordinal, (session, child) in enumerate(
        zip(report.session_reports, children, strict=True)
    ):
        source = session.session_evidence.miss_batch.qualification_batch.source.scope_receipt
        values: dict[str, Any] = {
            "parent_report_id": report.report_id,
            "parent_report_content_hash_sha256": report.content_hash(),
            "parent_metric_scope_key": scope_key,
            "session_ordinal": ordinal,
            "exchange_session_id": source.exchange_session_id,
            "session_open_at": source.session_open_at,
            "session_close_at": source.session_close_at,
            "child_metric_receipt_id": child.metric_persistence_receipt.metric_receipt_id,
            "child_metric_receipt_content_hash_sha256": (
                child.metric_persistence_receipt.content_hash()
            ),
            "child_metric_scope_key": child.metric_persistence_receipt.scope_key,
            "child_session_report_id": session.report_id,
            "child_session_report_content_hash_sha256": session.content_hash(),
            "child_miss_receipt_id": (
                child.current_miss_replay.miss_persistence_receipt.miss_receipt_id
            ),
            "child_miss_receipt_content_hash_sha256": (
                child.current_miss_replay.miss_persistence_receipt.content_hash()
            ),
            "schema_version": "v2.opportunity.metric_session_report_binding.v1",
        }
        bindings.append(
            MetricSessionReportBinding(
                binding_id=stable_identity("metric-session-report-binding", values),
                **values,
            )
        )
    return tuple(bindings)


def build_metric_inventory(
    report: MetricReport,
    bindings: tuple[MetricSessionReportBinding, ...],
) -> tuple[MetricInventoryItem, ...]:
    if isinstance(report, SessionDiscoveryMetricReport):
        family = MetricArtifactFamily.SESSION_DISCOVERY_METRIC_REPORT
    else:
        family = MetricArtifactFamily.DISCOVERY_METRIC_REPORT
    inventory = [
        MetricInventoryItem(
            0,
            family,
            0,
            report.report_id,
            None,
            report.schema_version,
            report.content_hash(),
        )
    ]
    inventory.extend(
        MetricInventoryItem(
            len(inventory),
            MetricArtifactFamily.METRIC_SESSION_REPORT_BINDING,
            ordinal,
            binding.binding_id,
            binding.exchange_session_id,
            binding.schema_version,
            binding.content_hash(),
        )
        for ordinal, binding in enumerate(bindings)
    )
    return tuple(inventory)


def metric_inventory_hash(inventory: tuple[MetricInventoryItem, ...]) -> str:
    payload = contract_to_json(tuple(item.identity_tuple() for item in inventory))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_metric_receipt(
    report: MetricReport,
    *,
    persisted_at: datetime,
    predecessor: OpportunityMetricPersistenceReceipt | None,
    parent_miss: CurrentMissReplay | None,
    children: tuple[CurrentSessionMetricReplay, ...],
    bindings: tuple[MetricSessionReportBinding, ...],
) -> OpportunityMetricPersistenceReceipt:
    exchange_session_id: str | None
    session_open_at: datetime | None
    session_close_at: datetime | None
    parent_id: str | None
    parent_hash: str | None
    cohort_id: str | None
    recorded_at: datetime | None
    if isinstance(report, SessionDiscoveryMetricReport):
        if parent_miss is None or children or bindings:
            raise ValueError("session metric receipt requires one direct miss parent")
        source = report.session_evidence.miss_batch.qualification_batch.source.scope_receipt
        policy = report.session_evidence.metric_policy
        report_kind = MetricReportKind.SESSION
        scope_key = session_metric_scope_key(report, parent_miss)
        exchange_session_id = source.exchange_session_id
        session_open_at = source.session_open_at
        session_close_at = source.session_close_at
        miss_receipt = parent_miss.miss_persistence_receipt
        parent_id = miss_receipt.miss_receipt_id
        parent_hash = miss_receipt.content_hash()
        cohort_id = None
        recorded_at = report.recorded_at
    else:
        if parent_miss is not None:
            raise ValueError("multi metric receipt cannot carry direct miss parent")
        policy = report.metric_policy
        report_kind = MetricReportKind.MULTI_SESSION
        scope_key = multi_metric_scope_key(report, children)
        exchange_session_id = None
        session_open_at = None
        session_close_at = None
        parent_id = None
        parent_hash = None
        cohort_id = report.cohort_id
        recorded_at = report.recorded_at
    inventory = build_metric_inventory(report, bindings)
    family_counts = tuple(
        MetricArtifactFamilyCount(
            family=family,
            count=sum(item.family is family for item in inventory),
        )
        for family in CANONICAL_METRIC_ARTIFACT_FAMILIES
    )
    values: dict[str, Any] = {
        "receipt_kind": (
            MetricPersistenceKind.CORRECTION
            if predecessor
            else MetricPersistenceKind.INITIAL
        ),
        "report_kind": report_kind,
        "scope_key": scope_key,
        "report_id": report.report_id,
        "report_content_hash_sha256": report.content_hash(),
        "report_schema_version": report.schema_version,
        "metric_policy_id": policy.metric_policy_id,
        "metric_policy_content_hash_sha256": policy.content_hash(),
        "exchange_session_id": exchange_session_id,
        "session_open_at": session_open_at,
        "session_close_at": session_close_at,
        "parent_miss_receipt_id": parent_id,
        "parent_miss_receipt_content_hash_sha256": parent_hash,
        "cohort_id": cohort_id,
        "report_recorded_at": recorded_at,
        "persisted_at": persisted_at,
        "supersedes_metric_receipt_id": predecessor.metric_receipt_id if predecessor else None,
        "supersedes_metric_receipt_content_hash_sha256": (
            predecessor.content_hash() if predecessor else None
        ),
        "family_counts": family_counts,
        "session_binding_count": len(bindings),
        "metric_value_count": 9,
        "artifact_count": len(inventory),
        "artifact_inventory_hash_sha256": metric_inventory_hash(inventory),
        "database_schema_version": 29,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.metric_persistence_receipt.v1",
    }
    return OpportunityMetricPersistenceReceipt(
        metric_receipt_id=stable_identity("opportunity-metric-persistence-receipt", values),
        **values,
    )


__all__ = [
    "MetricInventoryItem",
    "build_metric_bindings",
    "build_metric_inventory",
    "build_metric_receipt",
    "metric_inventory_hash",
    "multi_metric_scope_key",
    "session_metric_scope_key",
]
