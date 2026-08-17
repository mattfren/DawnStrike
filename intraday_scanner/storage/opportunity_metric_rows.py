"""Canonical SQLite row projections for discovery-metric artifacts."""

from __future__ import annotations

from intraday_scanner.storage.opportunity_metric_errors import (
    OpportunityMetricIntegrityError,
)


def _receipt_row(receipt, report):
    return (
        receipt.metric_receipt_id,
        receipt.content_hash(),
        receipt.receipt_kind.value,
        receipt.report_kind.value,
        receipt.scope_key,
        receipt.report_id,
        receipt.report_content_hash_sha256,
        receipt.report_schema_version,
        report.to_json(),
        receipt.metric_policy_id,
        receipt.metric_policy_content_hash_sha256,
        receipt.exchange_session_id,
        receipt.session_open_at.isoformat() if receipt.session_open_at else None,
        receipt.session_close_at.isoformat() if receipt.session_close_at else None,
        receipt.parent_miss_receipt_id,
        receipt.parent_miss_receipt_content_hash_sha256,
        receipt.cohort_id,
        receipt.report_recorded_at.isoformat() if receipt.report_recorded_at else None,
        receipt.persisted_at.isoformat(),
        receipt.supersedes_metric_receipt_id,
        receipt.supersedes_metric_receipt_content_hash_sha256,
        receipt.session_binding_count,
        receipt.metric_value_count,
        receipt.artifact_count,
        receipt.artifact_inventory_hash_sha256,
        receipt.schema_version,
        receipt.to_json(),
        int(receipt.research_only),
        int(receipt.promotion_eligible),
        receipt.database_schema_version,
    )


def _binding_row(receipt, binding):
    return (
        receipt.metric_receipt_id,
        binding.session_ordinal,
        binding.binding_id,
        binding.content_hash(),
        binding.schema_version,
        binding.to_json(),
        binding.exchange_session_id,
        binding.child_metric_receipt_id,
        binding.child_metric_receipt_content_hash_sha256,
        binding.child_metric_scope_key,
        binding.child_session_report_id,
        binding.child_session_report_content_hash_sha256,
        binding.child_miss_receipt_id,
        binding.child_miss_receipt_content_hash_sha256,
    )


def _chain_index(chain, receipt_id):
    for index, item in enumerate(chain):
        if item[0].metric_receipt_id == receipt_id:
            return index
    raise OpportunityMetricIntegrityError("metric receipt is outside its scope chain")


def _chain_item(chain, receipt_id):
    return chain[_chain_index(chain, receipt_id)]


__all__: list[str] = []
