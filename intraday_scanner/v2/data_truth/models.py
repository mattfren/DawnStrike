"""Typed DataTruth v1 manifest and reconciliation models."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass

from intraday_scanner.v2.contracts.serialization import ContractMixin


@dataclass(frozen=True)
class ExchangeSessionIdentity(ContractMixin):
    """Stable exchange-session identity carried by intraday evidence."""

    exchange: str
    session_id: str
    market_date: str
    timezone: str
    session_type: str = "regular"
    schema_version: str = "v2.exchange_session_identity.v1"


@dataclass(frozen=True)
class DataTruthManifest:
    snapshot_id: str
    created_at: str
    provider_id: str
    provider_name: str
    symbols: tuple[str, ...]
    timeframe: str
    requested_start: str
    requested_end: str
    accepted_start: str
    accepted_end: str
    bar_count: int
    accepted_bar_count: int
    rejected_bar_count: int
    skipped_incomplete_bars: int
    validation_status: str
    warnings: tuple[str, ...]
    raw_artifact_hashes: dict[str, str]
    normalized_artifact_hash: str
    source_url_or_reference: tuple[str, ...]
    snapshot_relative_path: str | None = None
    normalized_artifact_path: str | None = None
    raw_artifact_paths: tuple[str, ...] = ()
    snapshot_content_hash: str | None = None
    manifest_payload_hash: str | None = None
    artifact_schema_version: str | None = None
    code_version: str | None = None
    required_bar_date: str | None = None
    schema_version: str = "v2.data_truth_manifest.v1"
    production_required: bool = False
    request_contract: dict[str, object] | None = None
    request_contract_hash: str | None = None
    request_contract_artifact_path: str | None = None
    request_contract_artifact_hash: str | None = None

    def to_dict(self) -> dict[str, object]:
        payload = asdict(self)
        payload["symbols"] = list(self.symbols)
        payload["warnings"] = list(self.warnings)
        payload["raw_artifact_paths"] = list(self.raw_artifact_paths)
        payload["source_url_or_reference"] = list(self.source_url_or_reference)
        return payload

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ProviderDisagreement:
    symbol: str
    timestamp: str
    field_name: str
    canonical_value: float | int | None
    other_value: float | int | None
    provider_id: str
    tolerance: float
    severity: str = "warning"
    schema_version: str = "v2.provider_disagreement.v1"

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class DataTruthReconciliationReport:
    reconciliation_id: str
    created_at: str
    canonical_snapshot_id: str
    provider_count: int
    status: str
    canonical_provider_id: str
    compared_provider_ids: tuple[str, ...]
    disagreements: tuple[ProviderDisagreement, ...]
    warnings: tuple[str, ...]
    schema_version: str = "v2.data_truth_reconciliation.v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "canonical_provider_id": self.canonical_provider_id,
            "canonical_snapshot_id": self.canonical_snapshot_id,
            "compared_provider_ids": list(self.compared_provider_ids),
            "created_at": self.created_at,
            "disagreements": [item.to_dict() for item in self.disagreements],
            "provider_count": self.provider_count,
            "reconciliation_id": self.reconciliation_id,
            "schema_version": self.schema_version,
            "status": self.status,
            "warnings": list(self.warnings),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
