"""Self-recomputing public report for bounded validation trading metrics."""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

from intraday_scanner.v2.opportunity.models import OpportunityContract, stable_identity
from intraday_scanner.v2.opportunity.outcome_contracts import (
    OutcomeContract,
    _identity_payload,
    _require_hash,
    _require_identity,
    _require_sanitized_text,
    _require_schema,
    _require_unique,
    _require_utc,
)
from intraday_scanner.v2.opportunity.validation_audit import (
    ChronologicalValidationPreparationReceipt,
)
from intraday_scanner.v2.opportunity.validation_metric_contracts import (
    ValidationMetricReportStatus,
    ValidationTradingMetricPolicy,
)
from intraday_scanner.v2.opportunity.validation_metric_math import (
    _fresh_decimal_context,
    _metric_decimal_context,
)
from intraday_scanner.v2.opportunity.validation_metric_segments import (
    _BoundValidationMetricRow,
    _derive_metric_report_projections,
    _ValidationMetricExclusion,
    _ValidationMetricScope,
)


@dataclass(frozen=True)
class ValidationTradingMetricReport(OutcomeContract):
    report_id: str
    preparation_id: str
    preparation_content_hash_sha256: str
    preparation: ChronologicalValidationPreparationReceipt
    policy_id: str
    policy_content_hash_sha256: str
    policy: ValidationTradingMetricPolicy
    recorded_at: datetime
    bound_rows: tuple[_BoundValidationMetricRow, ...]
    scopes: tuple[_ValidationMetricScope, ...]
    exclusions: tuple[_ValidationMetricExclusion, ...]
    status: ValidationMetricReportStatus
    coverage_row_count: int
    scope_count: int
    segment_count: int
    excluded_session_count: int
    limitations: tuple[str, ...]
    research_only: bool = True
    promotion_eligible: bool = False
    schema_version: str = "v2.opportunity.validation_trading_metric_report.v1"

    @classmethod
    def from_dict(cls, payload: dict[str, object]):
        with _fresh_decimal_context(precision=28):
            decoded = super().from_dict(payload)
        with _metric_decimal_context(decoded.policy):
            return replace(decoded)

    @classmethod
    def from_json(cls, payload: str):
        with _fresh_decimal_context(precision=28):
            return super().from_json(payload)

    def __post_init__(self) -> None:
        OpportunityContract.__post_init__(self)
        _require_schema(
            self.schema_version,
            "v2.opportunity.validation_trading_metric_report.v1",
        )
        _require_identity(self.report_id, "report_id")
        _require_identity(self.preparation_id, "preparation_id")
        _require_hash(self.preparation_content_hash_sha256, "preparation content hash")
        _require_identity(self.policy_id, "policy_id")
        _require_hash(self.policy_content_hash_sha256, "policy content hash")
        _require_utc(self.recorded_at, "recorded_at")
        if (
            self.preparation_id != self.preparation.preparation_id
            or self.preparation_content_hash_sha256 != self.preparation.content_hash()
        ):
            raise ValueError("validation metric preparation binding does not match content")
        if self.recorded_at < self.preparation.recorded_at:
            raise ValueError("validation metric report predates preparation")
        if (
            self.policy_id != self.policy.policy_id
            or self.policy_content_hash_sha256 != self.policy.content_hash()
        ):
            raise ValueError("validation metric policy binding does not match content")
        with _metric_decimal_context(self.policy):
            (
                expected_rows,
                expected_scopes,
                expected_exclusions,
                expected_status,
                expected_limits,
            ) = _derive_metric_report_projections(self.preparation, self.policy)
        if self.bound_rows != expected_rows:
            raise ValueError("validation metric bound rows do not recompute")
        if self.scopes != expected_scopes:
            raise ValueError("validation metric scopes do not recompute")
        if self.exclusions != expected_exclusions:
            raise ValueError("validation metric exclusions do not recompute")
        if self.status is not expected_status:
            raise ValueError("validation metric report status does not recompute")
        expected_counts = (
            len(expected_rows),
            len(expected_scopes),
            sum(len(item.segments) for item in expected_scopes),
            len(expected_exclusions),
        )
        if (
            self.coverage_row_count,
            self.scope_count,
            self.segment_count,
            self.excluded_session_count,
        ) != expected_counts:
            raise ValueError("validation metric report counts do not reconcile")
        _require_unique([item.row_id for item in self.bound_rows], "metric bound row")
        _require_unique([item.scope_id for item in self.scopes], "metric scope")
        _require_unique(
            [item.session_source_id for item in self.exclusions],
            "metric excluded session",
        )
        if self.limitations != expected_limits:
            raise ValueError("validation metric report limitations do not recompute")
        _require_unique(list(self.limitations), "validation metric report limitation")
        for limitation in self.limitations:
            _require_sanitized_text(limitation, "validation metric report limitation")
        if not self.research_only or self.promotion_eligible:
            raise ValueError("validation metric report must remain research-only")
        expected_id = stable_identity(
            "validation-trading-metric-report",
            _identity_payload(self, "report_id"),
        )
        if self.report_id != expected_id:
            raise ValueError("validation metric report identity does not match content")


def build_validation_trading_metric_report(
    preparation: ChronologicalValidationPreparationReceipt,
    *,
    policy: ValidationTradingMetricPolicy,
    recorded_at: datetime,
) -> ValidationTradingMetricReport:
    _require_utc(recorded_at, "recorded_at")
    if recorded_at < preparation.recorded_at:
        raise ValueError("validation metric report predates preparation")
    with _metric_decimal_context(policy):
        rows, scopes, exclusions, status, limitations = _derive_metric_report_projections(
            preparation, policy
        )
        values = {
            "preparation_id": preparation.preparation_id,
            "preparation_content_hash_sha256": preparation.content_hash(),
            "preparation": preparation,
            "policy_id": policy.policy_id,
            "policy_content_hash_sha256": policy.content_hash(),
            "policy": policy,
            "recorded_at": recorded_at,
            "bound_rows": rows,
            "scopes": scopes,
            "exclusions": exclusions,
            "status": status,
            "coverage_row_count": len(rows),
            "scope_count": len(scopes),
            "segment_count": sum(len(item.segments) for item in scopes),
            "excluded_session_count": len(exclusions),
            "limitations": limitations,
            "research_only": True,
            "promotion_eligible": False,
            "schema_version": "v2.opportunity.validation_trading_metric_report.v1",
        }
        return ValidationTradingMetricReport(
            report_id=stable_identity("validation-trading-metric-report", values),
            preparation_id=preparation.preparation_id,
            preparation_content_hash_sha256=preparation.content_hash(),
            preparation=preparation,
            policy_id=policy.policy_id,
            policy_content_hash_sha256=policy.content_hash(),
            policy=policy,
            recorded_at=recorded_at,
            bound_rows=rows,
            scopes=scopes,
            exclusions=exclusions,
            status=status,
            coverage_row_count=len(rows),
            scope_count=len(scopes),
            segment_count=sum(len(item.segments) for item in scopes),
            excluded_session_count=len(exclusions),
            limitations=limitations,
        )


__all__ = [
    "ValidationTradingMetricReport",
    "build_validation_trading_metric_report",
]
