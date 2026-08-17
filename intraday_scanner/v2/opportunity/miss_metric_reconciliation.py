"""Pure per-session and multi-session discovery metric reconciliation."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from intraday_scanner.v2.opportunity.miss_contracts import (
    MissContract,
    QualificationClaimKind,
    QualificationSourceScopeStatus,
    QualificationStatus,
    SessionRunInventoryStatus,
    identity_payload,
    require_aware,
    require_hash,
    require_identity,
    require_sanitized,
    require_schema,
    require_unique,
)
from intraday_scanner.v2.opportunity.miss_metric_contracts import (
    DiscoveryMetricDefinition,
    DiscoveryMetricName,
    DiscoveryMetricPolicy,
    DiscoveryMetricScope,
    DiscoveryMetricStatus,
    DiscoveryMetricUnit,
    canonical_metric_definitions,
    quantize_metric_fraction,
)
from intraday_scanner.v2.opportunity.miss_metric_matching import (
    DiscoveryMetricSessionEvidence,
    _DiscoveryMetricUnitEvidence,
    build_discovery_metric_session_evidence,
)
from intraday_scanner.v2.opportunity.miss_reconciliation import (
    MissReconciliationBatch,
)
from intraday_scanner.v2.opportunity.models import stable_identity


@dataclass(frozen=True)
class _DiscoveryMetricValue:
    metric_value_id: str
    definition_id: str
    definition_content_hash_sha256: str
    definition: DiscoveryMetricDefinition
    scope: DiscoveryMetricScope
    status: DiscoveryMetricStatus
    numerator_count: int | None
    denominator_count: int | None
    value: Decimal | None
    unit: DiscoveryMetricUnit
    numerator_unit_ids: tuple[str, ...]
    denominator_unit_ids: tuple[str, ...]
    numerator_executable_trade_count: int | None
    numerator_price_move_proxy_count: int | None
    denominator_executable_trade_count: int | None
    denominator_price_move_proxy_count: int | None
    blocking_evidence_ids: tuple[str, ...]
    reason: str | None
    schema_version: str = "v2.opportunity.discovery_metric_value.v1"

    def __post_init__(self) -> None:
        require_schema(self.schema_version, "v2.opportunity.discovery_metric_value.v1")
        require_identity(self.metric_value_id, "metric_value_id")
        require_identity(self.definition_id, "definition_id")
        require_hash(
            self.definition_content_hash_sha256,
            "definition_content_hash_sha256",
        )
        if (
            self.definition_id != self.definition.definition_id
            or self.definition_content_hash_sha256 != self.definition.content_hash()
        ):
            raise ValueError("metric value does not bind exact definition")
        if self.unit is not DiscoveryMetricUnit.FRACTION:
            raise ValueError("discovery metrics require FRACTION unit")
        require_unique(self.numerator_unit_ids, "metric numerator unit")
        require_unique(self.denominator_unit_ids, "metric denominator unit")
        require_unique(self.blocking_evidence_ids, "metric blocking evidence")
        if self.numerator_unit_ids != tuple(sorted(self.numerator_unit_ids)):
            raise ValueError("metric numerator units must use canonical order")
        if self.denominator_unit_ids != tuple(sorted(self.denominator_unit_ids)):
            raise ValueError("metric denominator units must use canonical order")
        if self.blocking_evidence_ids != tuple(sorted(self.blocking_evidence_ids)):
            raise ValueError("metric blocking evidence must use canonical order")
        claim_counts = (
            self.numerator_executable_trade_count,
            self.numerator_price_move_proxy_count,
            self.denominator_executable_trade_count,
            self.denominator_price_move_proxy_count,
        )
        if self.status is DiscoveryMetricStatus.AVAILABLE:
            _require_nonnegative_int(self.numerator_count, "metric numerator")
            _require_positive_int(self.denominator_count, "metric denominator")
            assert self.numerator_count is not None
            assert self.denominator_count is not None
            if self.numerator_count > self.denominator_count:
                raise ValueError("metric numerator cannot exceed denominator")
            if len(self.numerator_unit_ids) != self.numerator_count:
                raise ValueError("metric numerator count does not match identities")
            if len(self.denominator_unit_ids) != self.denominator_count:
                raise ValueError("metric denominator count does not match identities")
            if not set(self.numerator_unit_ids).issubset(self.denominator_unit_ids):
                raise ValueError("metric numerator identities must be denominator subset")
            if self.value != quantize_metric_fraction(
                self.numerator_count,
                self.denominator_count,
            ):
                raise ValueError("metric Decimal value does not recompute")
            if self.reason is not None or self.blocking_evidence_ids:
                raise ValueError("available metric cannot carry blocker reason")
            for count in claim_counts:
                _require_nonnegative_int(count, "metric claim count")
        elif self.status is DiscoveryMetricStatus.INSUFFICIENT:
            if (
                self.numerator_count != 0
                or self.denominator_count != 0
                or self.value is not None
                or self.numerator_unit_ids
                or self.denominator_unit_ids
                or self.blocking_evidence_ids
                or claim_counts != (0, 0, 0, 0)
            ):
                raise ValueError("insufficient metric requires known empty populations")
            if self.reason is None:
                raise ValueError("insufficient metric requires reason")
        else:
            if (
                self.numerator_count is not None
                or self.denominator_count is not None
                or self.value is not None
                or self.numerator_unit_ids
                or self.denominator_unit_ids
                or any(item is not None for item in claim_counts)
            ):
                raise ValueError("unavailable metric cannot carry population claims")
            if self.reason is None or not self.blocking_evidence_ids:
                raise ValueError("unavailable metric requires causal blockers")
        if self.reason is not None:
            require_sanitized(self.reason, "metric reason")
        expected = stable_identity("discovery-metric-value", _metric_value_payload(self))
        if self.metric_value_id != expected:
            raise ValueError("metric value identity does not match content")


@dataclass(frozen=True)
class SessionDiscoveryMetricReport(MissContract):
    report_id: str
    session_evidence_id: str
    session_evidence_content_hash_sha256: str
    session_evidence: DiscoveryMetricSessionEvidence
    values: tuple[_DiscoveryMetricValue, ...]
    qualified_executable_trade_count: int
    qualified_price_move_proxy_count: int
    recorded_at: datetime
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.session_discovery_metric_report.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(
            self.schema_version,
            "v2.opportunity.session_discovery_metric_report.v1",
        )
        require_identity(self.report_id, "report_id")
        require_identity(self.session_evidence_id, "session_evidence_id")
        require_hash(
            self.session_evidence_content_hash_sha256,
            "session_evidence_content_hash_sha256",
        )
        require_aware(self.recorded_at, "session metric report recorded_at")
        if (
            self.session_evidence_id != self.session_evidence.session_evidence_id
            or self.session_evidence_content_hash_sha256
            != self.session_evidence.content_hash()
        ):
            raise ValueError("session metric report does not bind exact evidence")
        expected = _resolve_session_report(self.session_evidence)
        _compare_report_fields(self, expected)
        expected_id = stable_identity(
            "session-discovery-metric-report",
            identity_payload(self, "report_id"),
        )
        if self.report_id != expected_id:
            raise ValueError("session metric report identity does not match content")


@dataclass(frozen=True)
class DiscoveryMetricReport(MissContract):
    report_id: str
    cohort_id: str
    metric_policy_id: str
    metric_policy_content_hash_sha256: str
    metric_policy: DiscoveryMetricPolicy
    session_reports: tuple[SessionDiscoveryMetricReport, ...]
    values: tuple[_DiscoveryMetricValue, ...]
    qualified_executable_trade_count: int
    qualified_price_move_proxy_count: int
    recorded_at: datetime | None
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.discovery_metric_report.v1"

    def __post_init__(self) -> None:
        super().__post_init__()
        require_schema(self.schema_version, "v2.opportunity.discovery_metric_report.v1")
        for value, name in (
            (self.report_id, "report_id"),
            (self.cohort_id, "cohort_id"),
            (self.metric_policy_id, "metric_policy_id"),
        ):
            require_identity(value, name)
        require_hash(
            self.metric_policy_content_hash_sha256,
            "metric_policy_content_hash_sha256",
        )
        if self.recorded_at is not None:
            require_aware(self.recorded_at, "metric report recorded_at")
        if (
            self.metric_policy_id != self.metric_policy.metric_policy_id
            or self.metric_policy_content_hash_sha256
            != self.metric_policy.content_hash()
        ):
            raise ValueError("metric report does not bind exact policy")
        if self.session_reports != tuple(
            sorted(self.session_reports, key=_session_report_sort_key)
        ):
            raise ValueError("metric session reports must use canonical order")
        session_ids = tuple(_session_id(item) for item in self.session_reports)
        require_unique(session_ids, "metric report session")
        expected = _resolve_multi_report(self.metric_policy, self.session_reports)
        _compare_report_fields(self, expected)
        expected_cohort_id = stable_identity(
            "discovery-metric-cohort",
            {
                "metric_policy_id": self.metric_policy_id,
                "metric_policy_content_hash_sha256": (
                    self.metric_policy_content_hash_sha256
                ),
                "sessions": tuple(
                    (
                        item.session_evidence_id,
                        item.session_evidence_content_hash_sha256,
                    )
                    for item in self.session_reports
                ),
            },
        )
        if self.cohort_id != expected_cohort_id:
            raise ValueError("metric cohort identity does not match exact sessions")
        expected_id = stable_identity(
            "discovery-metric-report",
            identity_payload(self, "report_id"),
        )
        if self.report_id != expected_id:
            raise ValueError("metric report identity does not match content")


def reconcile_session_discovery_metrics(
    miss_batch: MissReconciliationBatch,
    *,
    policy: DiscoveryMetricPolicy,
) -> SessionDiscoveryMetricReport:
    evidence = build_discovery_metric_session_evidence(miss_batch, policy=policy)
    return _build_session_report(evidence)


def reconcile_discovery_metrics(
    miss_batches: tuple[MissReconciliationBatch, ...],
    *,
    policy: DiscoveryMetricPolicy,
) -> DiscoveryMetricReport:
    evidences = tuple(
        sorted(
            (
                build_discovery_metric_session_evidence(item, policy=policy)
                for item in miss_batches
            ),
            key=_session_evidence_sort_key,
        )
    )
    session_ids = tuple(_evidence_session_id(item) for item in evidences)
    require_unique(session_ids, "metric report session")
    reports = tuple(_build_session_report(item) for item in evidences)
    resolved = _resolve_multi_report(policy, reports)
    cohort_values = {
        "metric_policy_id": policy.metric_policy_id,
        "metric_policy_content_hash_sha256": policy.content_hash(),
        "sessions": tuple(
            (item.session_evidence_id, item.session_evidence_content_hash_sha256)
            for item in reports
        ),
    }
    values: dict[str, Any] = {
        "cohort_id": stable_identity("discovery-metric-cohort", cohort_values),
        "metric_policy_id": policy.metric_policy_id,
        "metric_policy_content_hash_sha256": policy.content_hash(),
        "metric_policy": policy,
        "session_reports": reports,
        **resolved,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.discovery_metric_report.v1",
    }
    return DiscoveryMetricReport(
        report_id=stable_identity("discovery-metric-report", values),
        **values,
    )


def _build_session_report(
    evidence: DiscoveryMetricSessionEvidence,
) -> SessionDiscoveryMetricReport:
    resolved = _resolve_session_report(evidence)
    values: dict[str, Any] = {
        "session_evidence_id": evidence.session_evidence_id,
        "session_evidence_content_hash_sha256": evidence.content_hash(),
        "session_evidence": evidence,
        **resolved,
        "research_only": True,
        "promotion_eligible": False,
        "schema_version": "v2.opportunity.session_discovery_metric_report.v1",
    }
    return SessionDiscoveryMetricReport(
        report_id=stable_identity("session-discovery-metric-report", values),
        **values,
    )


def _resolve_session_report(evidence: DiscoveryMetricSessionEvidence) -> dict[str, object]:
    values = tuple(
        _calculate_metric(
            definition,
            (evidence,),
            DiscoveryMetricScope.SESSION,
        )
        for definition in canonical_metric_definitions()
    )
    executable, proxy = _qualified_claim_counts((evidence,))
    return {
        "values": values,
        "qualified_executable_trade_count": executable,
        "qualified_price_move_proxy_count": proxy,
        "recorded_at": evidence.recorded_at,
        "limitations": _report_limitations((evidence,), values, multi=False),
    }


def _resolve_multi_report(
    policy: DiscoveryMetricPolicy,
    session_reports: tuple[SessionDiscoveryMetricReport, ...],
) -> dict[str, object]:
    if any(item.session_evidence.metric_policy != policy for item in session_reports):
        raise ValueError("multi-session report contains mismatched metric policy")
    evidences = tuple(item.session_evidence for item in session_reports)
    values = tuple(
        _calculate_metric(
            definition,
            evidences,
            DiscoveryMetricScope.MULTI_SESSION,
        )
        for definition in canonical_metric_definitions()
    )
    executable, proxy = _qualified_claim_counts(evidences)
    return {
        "values": values,
        "qualified_executable_trade_count": executable,
        "qualified_price_move_proxy_count": proxy,
        "recorded_at": max((item.recorded_at for item in evidences), default=None),
        "limitations": _report_limitations(evidences, values, multi=True),
    }


def _calculate_metric(
    definition: DiscoveryMetricDefinition,
    evidences: tuple[DiscoveryMetricSessionEvidence, ...],
    scope: DiscoveryMetricScope,
) -> _DiscoveryMetricValue:
    base_blockers = _base_blockers(evidences)
    metric_blockers = _metric_specific_blockers(definition, evidences)
    blockers = tuple(sorted({*base_blockers, *metric_blockers}))
    if blockers:
        return _build_metric_value(
            definition=definition,
            scope=scope,
            status=DiscoveryMetricStatus.UNAVAILABLE,
            numerator_ids=(),
            denominator_ids=(),
            units=(),
            blocking_ids=blockers,
            reason=(
                "unmatched_prediction_truth"
                if metric_blockers and not base_blockers
                else "incomplete_metric_truth"
            ),
        )
    all_units = tuple(item for evidence in evidences for item in evidence.units)
    numerator_ids, denominator_ids = _metric_populations(
        definition.name,
        all_units,
        evidences,
    )
    if not denominator_ids:
        return _build_metric_value(
            definition=definition,
            scope=scope,
            status=DiscoveryMetricStatus.INSUFFICIENT,
            numerator_ids=(),
            denominator_ids=(),
            units=all_units,
            blocking_ids=(),
            reason=_zero_denominator_reason(definition.name, evidences),
        )
    return _build_metric_value(
        definition=definition,
        scope=scope,
        status=DiscoveryMetricStatus.AVAILABLE,
        numerator_ids=numerator_ids,
        denominator_ids=denominator_ids,
        units=all_units,
        blocking_ids=(),
        reason=None,
    )


def _metric_populations(
    name: DiscoveryMetricName,
    units: tuple[_DiscoveryMetricUnitEvidence, ...],
    evidences: tuple[DiscoveryMetricSessionEvidence, ...],
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    qualified = tuple(
        item for item in units if item.qualification_status is QualificationStatus.QUALIFIED
    )
    if name is DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL:
        denominator = qualified
        numerator = tuple(item for item in qualified if item.on_time_watch_or_take)
    elif name in {
        DiscoveryMetricName.TOP_1_RECALL,
        DiscoveryMetricName.TOP_3_RECALL,
        DiscoveryMetricName.TOP_5_RECALL,
    }:
        top_k = _top_k(name)
        denominator = qualified
        numerator = tuple(
            item
            for item in qualified
            if item.best_on_time_rank_position is not None
            and item.best_on_time_rank_position <= top_k
        )
    elif name in {
        DiscoveryMetricName.PRECISION_AT_1,
        DiscoveryMetricName.PRECISION_AT_3,
        DiscoveryMetricName.PRECISION_AT_5,
    }:
        top_k = _top_k(name)
        denominator = tuple(
            item
            for item in units
            if item.best_on_time_rank_position is not None
            and item.best_on_time_rank_position <= top_k
        )
        numerator = tuple(
            item
            for item in denominator
            if item.qualification_status is QualificationStatus.QUALIFIED
        )
    elif name is DiscoveryMetricName.FALSE_POSITIVE_RATE:
        denominator = tuple(
            item
            for item in units
            if item.qualification_status is QualificationStatus.NOT_QUALIFIED
        )
        numerator = tuple(item for item in denominator if item.on_time_watch_or_take)
    else:
        denominator_ids = tuple(
            sorted(
                evidence.session_evidence_id
                for evidence in evidences
                if not _session_has_any_watch_or_take(evidence)
            )
        )
        numerator_ids = tuple(
            sorted(
                evidence.session_evidence_id
                for evidence in evidences
                if evidence.session_evidence_id in denominator_ids
                and not any(
                    item.qualification_status is QualificationStatus.QUALIFIED
                    for item in evidence.units
                )
            )
        )
        return numerator_ids, denominator_ids
    return (
        tuple(sorted(item.unit_evidence_id for item in numerator)),
        tuple(sorted(item.unit_evidence_id for item in denominator)),
    )


def _build_metric_value(
    *,
    definition: DiscoveryMetricDefinition,
    scope: DiscoveryMetricScope,
    status: DiscoveryMetricStatus,
    numerator_ids: tuple[str, ...],
    denominator_ids: tuple[str, ...],
    units: tuple[_DiscoveryMetricUnitEvidence, ...],
    blocking_ids: tuple[str, ...],
    reason: str | None,
) -> _DiscoveryMetricValue:
    unit_map = {item.unit_evidence_id: item for item in units}
    numerator_count: int | None
    denominator_count: int | None
    value: Decimal | None
    numerator_claims: tuple[int | None, int | None]
    denominator_claims: tuple[int | None, int | None]
    if status is DiscoveryMetricStatus.AVAILABLE:
        numerator_count = len(numerator_ids)
        denominator_count = len(denominator_ids)
        assert denominator_count > 0
        value = quantize_metric_fraction(
            numerator_count,
            denominator_count,
        )
        numerator_claims = _population_claim_counts(numerator_ids, unit_map)
        denominator_claims = _population_claim_counts(denominator_ids, unit_map)
    elif status is DiscoveryMetricStatus.INSUFFICIENT:
        numerator_count = 0
        denominator_count = 0
        value = None
        numerator_claims = (0, 0)
        denominator_claims = (0, 0)
    else:
        numerator_count = None
        denominator_count = None
        value = None
        numerator_claims = (None, None)
        denominator_claims = (None, None)
    values: dict[str, Any] = {
        "definition_id": definition.definition_id,
        "definition_content_hash_sha256": definition.content_hash(),
        "definition": definition,
        "scope": scope,
        "status": status,
        "numerator_count": numerator_count,
        "denominator_count": denominator_count,
        "value": value,
        "unit": DiscoveryMetricUnit.FRACTION,
        "numerator_unit_ids": numerator_ids,
        "denominator_unit_ids": denominator_ids,
        "numerator_executable_trade_count": numerator_claims[0],
        "numerator_price_move_proxy_count": numerator_claims[1],
        "denominator_executable_trade_count": denominator_claims[0],
        "denominator_price_move_proxy_count": denominator_claims[1],
        "blocking_evidence_ids": blocking_ids,
        "reason": reason,
        "schema_version": "v2.opportunity.discovery_metric_value.v1",
    }
    return _DiscoveryMetricValue(
        metric_value_id=stable_identity("discovery-metric-value", values),
        **values,
    )


def _base_blockers(
    evidences: tuple[DiscoveryMetricSessionEvidence, ...],
) -> tuple[str, ...]:
    blockers: list[str] = []
    for evidence in evidences:
        batch = evidence.miss_batch
        if (
            batch.qualification_batch.source.scope_receipt.scope_status
            is not QualificationSourceScopeStatus.COMPLETE_MARKET
        ):
            blockers.append(batch.qualification_batch.source.scope_receipt.scope_receipt_id)
        if (
            batch.session_replay.run_inventory.status
            is not SessionRunInventoryStatus.COMPLETE_AUTHORITATIVE
        ):
            blockers.append(batch.session_replay.run_inventory.inventory_id)
        blockers.extend(
            item.assessment_id
            for item in evidence.units
            if item.qualification_status
            not in {QualificationStatus.QUALIFIED, QualificationStatus.NOT_QUALIFIED}
        )
    return tuple(sorted(set(blockers)))


def _metric_specific_blockers(
    definition: DiscoveryMetricDefinition,
    evidences: tuple[DiscoveryMetricSessionEvidence, ...],
) -> tuple[str, ...]:
    if definition.name in {
        DiscoveryMetricName.PRECISION_AT_1,
        DiscoveryMetricName.PRECISION_AT_3,
        DiscoveryMetricName.PRECISION_AT_5,
    }:
        top_k = _top_k(definition.name)
        return tuple(
            sorted(
                item.prediction_evidence_id
                for evidence in evidences
                for item in evidence.unmatched_predictions
                if item.on_time
                and item.rank_position is not None
                and item.rank_position <= top_k
            )
        )
    if definition.name is DiscoveryMetricName.FALSE_POSITIVE_RATE:
        return tuple(
            sorted(
                item.prediction_evidence_id
                for evidence in evidences
                for item in evidence.unmatched_predictions
                if item.on_time and item.decision_value is not None
            )
        )
    return ()


def _session_has_any_watch_or_take(evidence: DiscoveryMetricSessionEvidence) -> bool:
    return any(item.any_watch_or_take for item in evidence.units) or any(
        item.decision_value is not None for item in evidence.unmatched_predictions
    )


def _top_k(name: DiscoveryMetricName) -> int:
    definition = canonical_metric_definitions()[tuple(DiscoveryMetricName).index(name)]
    if definition.top_k is None:
        raise ValueError("metric has no top-K definition")
    return definition.top_k


def _population_claim_counts(
    identities: tuple[str, ...],
    unit_map: dict[str, _DiscoveryMetricUnitEvidence],
) -> tuple[int, int]:
    units = tuple(unit_map[item] for item in identities if item in unit_map)
    return (
        sum(item.claim_kind is QualificationClaimKind.EXECUTABLE_TRADE for item in units),
        sum(item.claim_kind is QualificationClaimKind.PRICE_MOVE_PROXY for item in units),
    )


def _qualified_claim_counts(
    evidences: tuple[DiscoveryMetricSessionEvidence, ...],
) -> tuple[int, int]:
    qualified = tuple(
        item
        for evidence in evidences
        for item in evidence.units
        if item.qualification_status is QualificationStatus.QUALIFIED
    )
    return (
        sum(item.claim_kind is QualificationClaimKind.EXECUTABLE_TRADE for item in qualified),
        sum(item.claim_kind is QualificationClaimKind.PRICE_MOVE_PROXY for item in qualified),
    )


def _zero_denominator_reason(
    name: DiscoveryMetricName,
    evidences: tuple[DiscoveryMetricSessionEvidence, ...],
) -> str:
    if not evidences:
        return "empty_metric_cohort"
    if name in {
        DiscoveryMetricName.DAILY_OPPORTUNITY_RECALL,
        DiscoveryMetricName.TOP_1_RECALL,
        DiscoveryMetricName.TOP_3_RECALL,
        DiscoveryMetricName.TOP_5_RECALL,
    }:
        return "no_qualified_opportunities"
    if name in {
        DiscoveryMetricName.PRECISION_AT_1,
        DiscoveryMetricName.PRECISION_AT_3,
        DiscoveryMetricName.PRECISION_AT_5,
    }:
        return "no_top_k_predictions"
    if name is DiscoveryMetricName.FALSE_POSITIVE_RATE:
        return "no_not_qualified_assessments"
    return "no_all_session_no_trade_predictions"


def _report_limitations(
    evidences: tuple[DiscoveryMetricSessionEvidence, ...],
    values: tuple[_DiscoveryMetricValue, ...],
    *,
    multi: bool,
) -> tuple[str, ...]:
    candidates = (
        *(item for evidence in evidences for item in evidence.limitations),
        *(item.reason for item in values if item.reason is not None),
        *("multi_session_micro_aggregate" for _ in range(1 if multi else 0)),
        "retrospective_research_only_not_promotion_evidence",
    )
    limitations = tuple(dict.fromkeys(candidates))
    for limitation in limitations:
        require_sanitized(limitation, "metric report limitation")
    return limitations


def _compare_report_fields(value, expected: dict[str, object]) -> None:
    for field_name, expected_value in expected.items():
        if getattr(value, field_name) != expected_value:
            raise ValueError(f"metric report {field_name} does not recompute")
    if not value.research_only or value.promotion_eligible:
        raise ValueError("metric report must remain research-only")


def _metric_value_payload(value: _DiscoveryMetricValue) -> dict[str, object]:
    return {
        key: item
        for key, item in value.__dict__.items()
        if key != "metric_value_id"
    }


def _session_id(value: SessionDiscoveryMetricReport) -> str:
    return _evidence_session_id(value.session_evidence)


def _evidence_session_id(value: DiscoveryMetricSessionEvidence) -> str:
    return value.miss_batch.qualification_batch.source.scope_receipt.exchange_session_id


def _session_evidence_sort_key(
    value: DiscoveryMetricSessionEvidence,
) -> tuple[object, ...]:
    scope = value.miss_batch.qualification_batch.source.scope_receipt
    return (
        scope.session_open_at,
        scope.exchange_session_id,
        value.miss_batch_id,
        value.miss_batch_content_hash_sha256,
    )


def _session_report_sort_key(
    value: SessionDiscoveryMetricReport,
) -> tuple[object, ...]:
    return _session_evidence_sort_key(value.session_evidence)


def _require_nonnegative_int(value: int | None, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{name} must be a nonnegative integer")


def _require_positive_int(value: int | None, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


__all__ = [
    "DiscoveryMetricReport",
    "SessionDiscoveryMetricReport",
    "reconcile_discovery_metrics",
    "reconcile_session_discovery_metrics",
]
