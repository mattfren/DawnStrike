"""Canonical SQLite projections for durable validation evidence."""

from __future__ import annotations

from intraday_scanner.storage.opportunity_validation_contracts import (
    LockedOOSSessionBinding,
    ValidationPersistenceReceipt,
)
from intraday_scanner.v2.opportunity.validation_audit import (
    ChronologicalValidationPreparationReceipt,
)
from intraday_scanner.v2.opportunity.validation_contracts import HoldoutAccessEvidence
from intraday_scanner.v2.opportunity.validation_metric_report import (
    ValidationTradingMetricReport,
)
from intraday_scanner.v2.opportunity.validation_robustness_report import (
    ValidationRobustnessReport,
)


def validation_receipt_row(
    receipt: ValidationPersistenceReceipt,
    preparation: ChronologicalValidationPreparationReceipt,
    metric_report: ValidationTradingMetricReport,
    robustness_report: ValidationRobustnessReport,
    holdout_access_evidence: HoldoutAccessEvidence,
) -> tuple[object, ...]:
    return (
        receipt.validation_receipt_id,
        receipt.content_hash(),
        receipt.semantic_lock_key,
        receipt.lock_authority_key,
        receipt.holdout_inventory_key,
        receipt.status.value,
        int(receipt.fresh_lock_eligible),
        receipt.preparation_id,
        receipt.preparation_content_hash_sha256,
        preparation.schema_version,
        preparation.to_json(),
        receipt.metric_report_id,
        receipt.metric_report_content_hash_sha256,
        metric_report.schema_version,
        metric_report.to_json(),
        receipt.robustness_report_id,
        receipt.robustness_report_content_hash_sha256,
        robustness_report.schema_version,
        robustness_report.to_json(),
        receipt.holdout_access_evidence_id,
        receipt.holdout_access_content_hash_sha256,
        holdout_access_evidence.schema_version,
        holdout_access_evidence.to_json(),
        receipt.corpus_id,
        receipt.split_plan_id,
        receipt.split_policy_id,
        receipt.split_policy_content_hash_sha256,
        receipt.split_policy_declared_at.isoformat(),
        receipt.code_identity,
        receipt.code_content_hash_sha256,
        receipt.strategy_id,
        receipt.strategy_version,
        receipt.confirmatory_unit_id,
        receipt.confirmatory_unit_content_hash_sha256,
        receipt.corpus_policy_id,
        receipt.corpus_policy_content_hash_sha256,
        receipt.metric_policy_id,
        receipt.metric_policy_content_hash_sha256,
        receipt.robustness_policy_id,
        receipt.robustness_policy_content_hash_sha256,
        len(receipt.oos_sessions),
        receipt.oos_session_inventory_hash_sha256,
        receipt.result_set_hash_sha256,
        receipt.persisted_at.isoformat(),
        receipt.lifecycle_mutation_count,
        int(receipt.take_authorization),
        int(receipt.research_only),
        int(receipt.promotion_eligible),
        receipt.database_schema_version,
        receipt.schema_version,
        receipt.to_json(),
    )


def validation_session_row(
    receipt_id: str,
    session: LockedOOSSessionBinding,
) -> tuple[object, ...]:
    return (
        receipt_id,
        session.session_ordinal,
        session.session_source_id,
        session.session_content_hash_sha256,
        session.exchange_session_id,
        session.session_open_at.isoformat(),
        session.session_close_at.isoformat(),
        session.role,
    )


__all__ = ["validation_receipt_row", "validation_session_row"]
