"""Immutable downstream persistence and replay contracts for discovery metrics."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import Enum

from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    identity_payload,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
    require_utc,
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


class MetricPersistenceKind(str, Enum):
    INITIAL = "initial"
    CORRECTION = "correction"


class MetricReportKind(str, Enum):
    SESSION = "session"
    MULTI_SESSION = "multi_session"


class MetricArtifactFamily(str, Enum):
    SESSION_DISCOVERY_METRIC_REPORT = "session_discovery_metric_report"
    DISCOVERY_METRIC_REPORT = "discovery_metric_report"
    METRIC_SESSION_REPORT_BINDING = "metric_session_report_binding"


CANONICAL_METRIC_ARTIFACT_FAMILIES = tuple(MetricArtifactFamily)


@dataclass(frozen=True)
class MetricArtifactFamilyCount(MissContract):
    family: MetricArtifactFamily
    count: int
    schema_version: str = "v2.opportunity.metric_artifact_family_count.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.metric_artifact_family_count.v1")
        if isinstance(self.count, bool) or not isinstance(self.count, int) or self.count < 0:
            raise ValueError("metric artifact family count must be nonnegative integer")


@dataclass(frozen=True)
class MetricSessionReportBinding(MissContract):
    binding_id: str
    parent_report_id: str
    parent_report_content_hash_sha256: str
    parent_metric_scope_key: str
    session_ordinal: int
    exchange_session_id: str
    session_open_at: datetime
    session_close_at: datetime
    child_metric_receipt_id: str
    child_metric_receipt_content_hash_sha256: str
    child_metric_scope_key: str
    child_session_report_id: str
    child_session_report_content_hash_sha256: str
    child_miss_receipt_id: str
    child_miss_receipt_content_hash_sha256: str
    schema_version: str = "v2.opportunity.metric_session_report_binding.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.metric_session_report_binding.v1",
        )
        for value, name in (
            (self.binding_id, "binding_id"),
            (self.parent_report_id, "parent_report_id"),
            (self.parent_metric_scope_key, "parent_metric_scope_key"),
            (self.child_metric_receipt_id, "child_metric_receipt_id"),
            (self.child_metric_scope_key, "child_metric_scope_key"),
            (self.child_session_report_id, "child_session_report_id"),
            (self.child_miss_receipt_id, "child_miss_receipt_id"),
        ):
            require_identity(value, name)
        for digest, digest_name in (
            (self.parent_report_content_hash_sha256, "parent_report_content_hash_sha256"),
            (
                self.child_metric_receipt_content_hash_sha256,
                "child_metric_receipt_content_hash_sha256",
            ),
            (
                self.child_session_report_content_hash_sha256,
                "child_session_report_content_hash_sha256",
            ),
            (
                self.child_miss_receipt_content_hash_sha256,
                "child_miss_receipt_content_hash_sha256",
            ),
        ):
            require_hash(digest, digest_name)
        if (
            isinstance(self.session_ordinal, bool)
            or not isinstance(self.session_ordinal, int)
            or self.session_ordinal < 0
        ):
            raise ValueError("session ordinal must be a nonnegative integer")
        require_utc(self.session_open_at, "session_open_at")
        require_utc(self.session_close_at, "session_close_at")
        require_sanitized(self.exchange_session_id, "exchange_session_id")
        if self.session_open_at >= self.session_close_at:
            raise ValueError("metric session binding session is reversed or empty")
        expected = stable_identity(
            "metric-session-report-binding",
            identity_payload(self, "binding_id"),
        )
        if self.binding_id != expected:
            raise ValueError("metric session binding identity does not match content")


@dataclass(frozen=True)
class OpportunityMetricPersistenceReceipt(MissContract):
    metric_receipt_id: str
    receipt_kind: MetricPersistenceKind
    report_kind: MetricReportKind
    scope_key: str
    report_id: str
    report_content_hash_sha256: str
    report_schema_version: str
    metric_policy_id: str
    metric_policy_content_hash_sha256: str
    exchange_session_id: str | None
    session_open_at: datetime | None
    session_close_at: datetime | None
    parent_miss_receipt_id: str | None
    parent_miss_receipt_content_hash_sha256: str | None
    cohort_id: str | None
    report_recorded_at: datetime | None
    persisted_at: datetime
    supersedes_metric_receipt_id: str | None
    supersedes_metric_receipt_content_hash_sha256: str | None
    family_counts: tuple[MetricArtifactFamilyCount, ...]
    session_binding_count: int
    metric_value_count: int
    artifact_count: int
    artifact_inventory_hash_sha256: str
    database_schema_version: int = 29
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.metric_persistence_receipt.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.metric_persistence_receipt.v1")
        for value, name in (
            (self.metric_receipt_id, "metric_receipt_id"),
            (self.scope_key, "scope_key"),
            (self.report_id, "report_id"),
            (self.metric_policy_id, "metric_policy_id"),
        ):
            require_identity(value, name)
        for digest, digest_name in (
            (self.report_content_hash_sha256, "report_content_hash_sha256"),
            (
                self.metric_policy_content_hash_sha256,
                "metric_policy_content_hash_sha256",
            ),
            (self.artifact_inventory_hash_sha256, "artifact_inventory_hash_sha256"),
        ):
            require_hash(digest, digest_name)
        require_utc(self.persisted_at, "persisted_at")
        predecessor_pair = (self.supersedes_metric_receipt_id is None) is (
            self.supersedes_metric_receipt_content_hash_sha256 is None
        )
        if not predecessor_pair:
            raise ValueError("metric predecessor identity and hash must be paired")
        if self.receipt_kind is MetricPersistenceKind.INITIAL:
            if self.supersedes_metric_receipt_id is not None:
                raise ValueError("initial metric receipt cannot supersede another")
        elif self.supersedes_metric_receipt_id is None:
            raise ValueError("metric correction requires a predecessor")
        if self.supersedes_metric_receipt_id is not None:
            require_identity(self.supersedes_metric_receipt_id, "supersedes_metric_receipt_id")
            require_hash(
                self.supersedes_metric_receipt_content_hash_sha256 or "",
                "supersedes_metric_receipt_content_hash_sha256",
            )
        if tuple(item.family for item in self.family_counts) != (
            CANONICAL_METRIC_ARTIFACT_FAMILIES
        ):
            raise ValueError("metric family counts must use canonical order")
        for count, count_name in (
            (self.session_binding_count, "session_binding_count"),
            (self.metric_value_count, "metric_value_count"),
            (self.artifact_count, "artifact_count"),
        ):
            if isinstance(count, bool) or not isinstance(count, int) or count < 0:
                raise ValueError(f"{count_name} must be a nonnegative integer")
        if self.metric_value_count != 9:
            raise ValueError("metric persistence receipt requires exactly nine embedded values")
        if self.report_kind is MetricReportKind.SESSION:
            if (
                self.report_schema_version
                != "v2.opportunity.session_discovery_metric_report.v1"
                or self.exchange_session_id is None
                or self.session_open_at is None
                or self.session_close_at is None
                or self.parent_miss_receipt_id is None
                or self.parent_miss_receipt_content_hash_sha256 is None
                or self.cohort_id is not None
                or self.report_recorded_at is None
                or self.session_binding_count != 0
            ):
                raise ValueError("session metric receipt shape is invalid")
            require_utc(self.session_open_at, "session_open_at")
            require_utc(self.session_close_at, "session_close_at")
            require_utc(self.report_recorded_at, "report_recorded_at")
            if self.session_open_at >= self.session_close_at:
                raise ValueError("session metric receipt session is invalid")
            require_identity(self.parent_miss_receipt_id, "parent_miss_receipt_id")
            require_hash(
                self.parent_miss_receipt_content_hash_sha256,
                "parent_miss_receipt_content_hash_sha256",
            )
            counts = (1, 0, 0)
        else:
            if (
                self.report_schema_version != "v2.opportunity.discovery_metric_report.v1"
                or self.exchange_session_id is not None
                or self.session_open_at is not None
                or self.session_close_at is not None
                or self.parent_miss_receipt_id is not None
                or self.parent_miss_receipt_content_hash_sha256 is not None
                or self.cohort_id is None
                or (self.session_binding_count == 0) is not (self.report_recorded_at is None)
            ):
                raise ValueError("multi-session metric receipt shape is invalid")
            require_identity(self.cohort_id, "cohort_id")
            if self.report_recorded_at is not None:
                require_utc(self.report_recorded_at, "report_recorded_at")
            counts = (0, 1, self.session_binding_count)
        if tuple(item.count for item in self.family_counts) != counts:
            raise ValueError("metric family allocation does not match report kind")
        if self.artifact_count != sum(counts):
            raise ValueError("metric artifact count does not match family counts")
        if self.report_recorded_at is not None and self.persisted_at < self.report_recorded_at:
            raise ValueError("metric persistence predates report recording")
        if self.database_schema_version != 29:
            raise ValueError("metric persistence requires database schema 29")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("metric persistence receipt must remain research-only")
        expected = stable_identity(
            "opportunity-metric-persistence-receipt",
            identity_payload(self, "metric_receipt_id"),
        )
        if self.metric_receipt_id != expected:
            raise ValueError("metric persistence receipt identity does not match content")


def validate_binding_set(
    report: DiscoveryMetricReport,
    scope_key: str,
    bindings: tuple[MetricSessionReportBinding, ...],
    child_replays: tuple[CurrentSessionMetricReplay, ...],
) -> None:
    if any(not isinstance(item, CurrentSessionMetricReplay) for item in child_replays):
        raise ValueError("multi metric binding child must be a SESSION replay")
    if len(bindings) != len(report.session_reports) or len(bindings) != len(child_replays):
        raise ValueError("multi metric binding set does not match report sessions")
    require_unique(tuple(item.binding_id for item in bindings), "metric session binding")
    for ordinal, (session, binding, child) in enumerate(
        zip(report.session_reports, bindings, child_replays, strict=True)
    ):
        miss = session.session_evidence.miss_batch
        source = miss.qualification_batch.source.scope_receipt
        if (
            binding.parent_report_id != report.report_id
            or binding.parent_report_content_hash_sha256 != report.content_hash()
            or binding.parent_metric_scope_key != scope_key
            or binding.session_ordinal != ordinal
            or binding.exchange_session_id != source.exchange_session_id
            or binding.session_open_at != source.session_open_at
            or binding.session_close_at != source.session_close_at
            or binding.child_metric_receipt_id
            != child.metric_persistence_receipt.metric_receipt_id
            or binding.child_metric_receipt_content_hash_sha256
            != child.metric_persistence_receipt.content_hash()
            or binding.child_metric_scope_key
            != child.metric_persistence_receipt.scope_key
            or binding.child_session_report_id != session.report_id
            or binding.child_session_report_content_hash_sha256 != session.content_hash()
            or binding.child_miss_receipt_id
            != child.current_miss_replay.miss_persistence_receipt.miss_receipt_id
            or binding.child_miss_receipt_content_hash_sha256
            != child.current_miss_replay.miss_persistence_receipt.content_hash()
            or child.metric_report != session
        ):
            raise ValueError("metric session binding does not match exact child report replay")


@dataclass(frozen=True)
class HistoricalMetricReplay(MissContract):
    replay_id: str
    metric_persistence_receipt: OpportunityMetricPersistenceReceipt
    metric_report: SessionDiscoveryMetricReport | DiscoveryMetricReport
    chain_prefix_receipts: tuple[OpportunityMetricPersistenceReceipt, ...]
    chain_prefix_reports: tuple[SessionDiscoveryMetricReport | DiscoveryMetricReport, ...]
    historical_miss_replay: HistoricalMissReplay | None
    session_bindings: tuple[MetricSessionReportBinding, ...]
    historical_child_metric_replays: tuple[HistoricalMetricReplay, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.historical_metric_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.historical_metric_replay.v1")
        require_identity(self.replay_id, "replay_id")
        _validate_replay_chain(
            self.metric_persistence_receipt,
            self.metric_report,
            self.chain_prefix_receipts,
            self.chain_prefix_reports,
        )
        if self.metric_persistence_receipt.report_kind is MetricReportKind.SESSION:
            if (
                self.historical_miss_replay is None
                or self.session_bindings
                or self.historical_child_metric_replays
            ):
                raise ValueError("historical session metric replay parent shape is invalid")
            if (
                not isinstance(self.metric_report, SessionDiscoveryMetricReport)
                or self.metric_report.session_evidence.miss_batch
                != self.historical_miss_replay.miss_batch
                or self.metric_persistence_receipt.parent_miss_receipt_id
                != self.historical_miss_replay.miss_persistence_receipt.miss_receipt_id
                or self.metric_persistence_receipt.parent_miss_receipt_content_hash_sha256
                != self.historical_miss_replay.miss_persistence_receipt.content_hash()
                or self.metric_persistence_receipt.scope_key
                != _session_scope_key(self.metric_report, self.historical_miss_replay)
            ):
                raise ValueError("historical session metric parent does not match report")
        elif self.historical_miss_replay is not None:
            raise ValueError("historical multi metric replay cannot embed direct miss parent")
        elif not isinstance(self.metric_report, DiscoveryMetricReport):
            raise ValueError("historical multi metric replay requires multi report")
        else:
            if len(self.session_bindings) != len(self.historical_child_metric_replays):
                raise ValueError("historical metric bindings and child replays must pair")
            for ordinal, (session, binding, child) in enumerate(
                zip(
                    self.metric_report.session_reports,
                    self.session_bindings,
                    self.historical_child_metric_replays,
                    strict=True,
                )
            ):
                child_receipt = child.metric_persistence_receipt
                child_miss = child.historical_miss_replay
                if (
                    child_miss is None
                    or binding.parent_report_id != self.metric_report.report_id
                    or binding.parent_report_content_hash_sha256
                    != self.metric_report.content_hash()
                    or binding.parent_metric_scope_key
                    != self.metric_persistence_receipt.scope_key
                    or binding.session_ordinal != ordinal
                    or binding.child_metric_receipt_id != child_receipt.metric_receipt_id
                    or binding.child_metric_receipt_content_hash_sha256
                    != child_receipt.content_hash()
                    or binding.child_metric_scope_key != child_receipt.scope_key
                    or binding.child_session_report_id != session.report_id
                    or binding.child_session_report_content_hash_sha256
                    != session.content_hash()
                    or binding.child_miss_receipt_id
                    != child_miss.miss_persistence_receipt.miss_receipt_id
                    or binding.child_miss_receipt_content_hash_sha256
                    != child_miss.miss_persistence_receipt.content_hash()
                    or child.metric_report != session
                ):
                    raise ValueError("historical metric binding does not match child replay")
            if self.metric_persistence_receipt.scope_key != _multi_scope_key(
                self.metric_report,
                self.historical_child_metric_replays,
            ):
                raise ValueError("historical multi metric scope does not recompute")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("historical metric replay must remain research-only")
        expected = stable_identity(
            "historical-opportunity-metric-replay",
            identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected:
            raise ValueError("historical metric replay identity does not match content")


@dataclass(frozen=True)
class CurrentSessionMetricReplay(MissContract):
    replay_id: str
    metric_persistence_receipt: OpportunityMetricPersistenceReceipt
    metric_report: SessionDiscoveryMetricReport
    full_chain_receipts: tuple[OpportunityMetricPersistenceReceipt, ...]
    full_chain_reports: tuple[SessionDiscoveryMetricReport, ...]
    current_miss_replay: CurrentMissReplay
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.current_session_metric_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.current_session_metric_replay.v1")
        require_identity(self.replay_id, "replay_id")
        _validate_replay_chain(
            self.metric_persistence_receipt,
            self.metric_report,
            self.full_chain_receipts,
            self.full_chain_reports,
        )
        if (
            self.metric_persistence_receipt.report_kind is not MetricReportKind.SESSION
            or self.metric_report.session_evidence.miss_batch != self.current_miss_replay.miss_batch
            or self.metric_persistence_receipt.parent_miss_receipt_id
            != self.current_miss_replay.miss_persistence_receipt.miss_receipt_id
            or self.metric_persistence_receipt.parent_miss_receipt_content_hash_sha256
            != self.current_miss_replay.miss_persistence_receipt.content_hash()
            or self.metric_persistence_receipt.scope_key
            != _session_scope_key(self.metric_report, self.current_miss_replay)
        ):
            raise ValueError("current session metric replay miss parent is inconsistent")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("current session metric replay must remain research-only")
        expected = stable_identity(
            "current-session-opportunity-metric-replay",
            identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected:
            raise ValueError("current session metric replay identity does not match content")


@dataclass(frozen=True)
class CurrentMultiMetricReplay(MissContract):
    replay_id: str
    metric_persistence_receipt: OpportunityMetricPersistenceReceipt
    metric_report: DiscoveryMetricReport
    full_chain_receipts: tuple[OpportunityMetricPersistenceReceipt, ...]
    full_chain_reports: tuple[DiscoveryMetricReport, ...]
    session_bindings: tuple[MetricSessionReportBinding, ...]
    current_child_metric_replays: tuple[CurrentSessionMetricReplay, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.current_multi_metric_replay.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.current_multi_metric_replay.v1")
        require_identity(self.replay_id, "replay_id")
        _validate_replay_chain(
            self.metric_persistence_receipt,
            self.metric_report,
            self.full_chain_receipts,
            self.full_chain_reports,
        )
        if self.metric_persistence_receipt.report_kind is not MetricReportKind.MULTI_SESSION:
            raise ValueError("current multi replay requires multi-session receipt")
        validate_binding_set(
            self.metric_report,
            self.metric_persistence_receipt.scope_key,
            self.session_bindings,
            self.current_child_metric_replays,
        )
        if self.metric_persistence_receipt.scope_key != _multi_scope_key(
            self.metric_report,
            self.current_child_metric_replays,
        ):
            raise ValueError("current multi metric scope does not recompute")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("current multi metric replay must remain research-only")
        expected = stable_identity(
            "current-multi-opportunity-metric-replay",
            identity_payload(self, "replay_id"),
        )
        if self.replay_id != expected:
            raise ValueError("current multi metric replay identity does not match content")


def _validate_replay_chain(receipt, report, receipts, reports) -> None:
    if (
        not receipts
        or len(receipts) != len(reports)
        or receipts[-1] != receipt
        or reports[-1] != report
    ):
        raise ValueError("metric replay chain bodies are not exact")
    require_unique(tuple(item.metric_receipt_id for item in receipts), "metric receipt")
    for index, (item, item_report) in enumerate(zip(receipts, reports, strict=True)):
        _validate_receipt_report_projection(item, item_report)
        if item.scope_key != receipt.scope_key or item.report_kind is not receipt.report_kind:
            raise ValueError("metric replay chain crosses scope or report kind")
        if index == 0:
            if item.receipt_kind is not MetricPersistenceKind.INITIAL:
                raise ValueError("metric replay chain must begin with initial receipt")
            continue
        previous = receipts[index - 1]
        previous_report = reports[index - 1]
        if (
            item.receipt_kind is not MetricPersistenceKind.CORRECTION
            or item.supersedes_metric_receipt_id != previous.metric_receipt_id
            or item.supersedes_metric_receipt_content_hash_sha256 != previous.content_hash()
            or item.persisted_at <= previous.persisted_at
            or item_report.recorded_at is None
            or previous_report.recorded_at is None
            or item_report.recorded_at <= previous_report.recorded_at
        ):
            raise ValueError(
                "metric replay chain lineage or report chronology is invalid"
            )


def _validate_receipt_report_projection(receipt, report) -> None:
    if (
        receipt.report_id != report.report_id
        or receipt.report_content_hash_sha256 != report.content_hash()
        or receipt.report_schema_version != report.schema_version
        or receipt.report_recorded_at != report.recorded_at
    ):
        raise ValueError("metric receipt does not bind exact report body")
    if isinstance(report, SessionDiscoveryMetricReport):
        source = report.session_evidence.miss_batch.qualification_batch.source.scope_receipt
        policy = report.session_evidence.metric_policy
        if (
            receipt.report_kind is not MetricReportKind.SESSION
            or receipt.metric_policy_id != policy.metric_policy_id
            or receipt.metric_policy_content_hash_sha256 != policy.content_hash()
            or receipt.exchange_session_id != source.exchange_session_id
            or receipt.session_open_at != source.session_open_at
            or receipt.session_close_at != source.session_close_at
            or receipt.cohort_id is not None
        ):
            raise ValueError("session metric receipt projections are inconsistent")
    elif (
        receipt.report_kind is not MetricReportKind.MULTI_SESSION
        or receipt.metric_policy_id != report.metric_policy_id
        or receipt.metric_policy_content_hash_sha256
        != report.metric_policy_content_hash_sha256
        or receipt.cohort_id != report.cohort_id
        or receipt.exchange_session_id is not None
        or receipt.session_open_at is not None
        or receipt.session_close_at is not None
    ):
        raise ValueError("multi metric receipt projections are inconsistent")


def _session_scope_key(
    report: SessionDiscoveryMetricReport,
    parent: CurrentMissReplay | HistoricalMissReplay,
) -> str:
    source = report.session_evidence.miss_batch.qualification_batch.source.scope_receipt
    policy = report.session_evidence.metric_policy
    return stable_identity(
        "opportunity-session-metric-scope",
        {
            "metric_policy_id": policy.metric_policy_id,
            "metric_policy_content_hash_sha256": policy.content_hash(),
            "exchange_session_id": source.exchange_session_id,
            "session_open_at": source.session_open_at,
            "session_close_at": source.session_close_at,
            "miss_analysis_key": parent.miss_persistence_receipt.analysis_key,
            "schema_version": "v2.opportunity.session_metric_scope.v1",
        },
    )


def _multi_scope_key(
    report: DiscoveryMetricReport,
    children: tuple[CurrentSessionMetricReplay | HistoricalMetricReplay, ...],
) -> str:
    return stable_identity(
        "opportunity-multi-metric-scope",
        {
            "metric_policy_id": report.metric_policy_id,
            "metric_policy_content_hash_sha256": (
                report.metric_policy_content_hash_sha256
            ),
            "child_session_metric_scope_keys": tuple(
                child.metric_persistence_receipt.scope_key for child in children
            ),
            "schema_version": "v2.opportunity.multi_metric_scope.v1",
        },
    )


__all__ = [
    "CANONICAL_METRIC_ARTIFACT_FAMILIES",
    "CurrentMultiMetricReplay",
    "CurrentSessionMetricReplay",
    "HistoricalMetricReplay",
    "MetricArtifactFamily",
    "MetricArtifactFamilyCount",
    "MetricPersistenceKind",
    "MetricReportKind",
    "MetricSessionReportBinding",
    "OpportunityMetricPersistenceReceipt",
    "validate_binding_set",
]
