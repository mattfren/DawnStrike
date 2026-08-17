"""Offline production entrypoint for retained opportunity-research evidence."""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from typing import Any

from intraday_scanner.services.opportunity_catalyst_adapter import (
    load_retained_catalyst_adapter,
)
from intraday_scanner.storage.test_isolation import ACTIVE_DATABASE
from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.data_truth import (
    DataTruthManifest,
    load_datatruth_snapshot,
    verify_datatruth_snapshot,
)
from intraday_scanner.v2.opportunity.capabilities import (
    CapabilityState,
    build_provider_capability_receipt,
)
from intraday_scanner.v2.opportunity.catalyst import InjectedCatalystAdapter
from intraday_scanner.v2.opportunity.pipeline import build_pipeline_risk_policy
from intraday_scanner.v2.opportunity.producer import (
    CurrentOpportunityAdapter,
    EvidenceCacheIdentity,
    HistoricalOpportunityAdapter,
    OpportunityAdapterRequest,
    OpportunityProducerError,
    OpportunityResearchProducer,
    ProducerFailureCode,
    ProducerFailureReceipt,
    ProducerReceipt,
    StageStatus,
    StageTelemetry,
)
from intraday_scanner.v2.opportunity.registry import build_default_registry
from intraday_scanner.v2.opportunity.universe import (
    SafetyStatus,
    SecurityType,
    UniverseMemberFact,
    UniversePolicy,
    build_universe_snapshot,
)

LOCAL_UNIVERSE_SCHEMA = "dawnstrike.opportunity.local_universe_evidence.v1"
LOCAL_V5_SCHEMA = "dawnstrike.opportunity.local_alphaops_v5_candidates.v1"


class OpportunityResearchMode(str, Enum):
    CURRENT = "current"
    HISTORICAL = "historical"


class LocalResearchStatus(str, Enum):
    DISABLED = "DISABLED"
    COMPLETE = "COMPLETE"
    FAILED = "FAILED"


@dataclass(frozen=True)
class LocalOpportunityResearchReport:
    status: LocalResearchStatus
    mode: OpportunityResearchMode
    producer_receipt: ProducerReceipt | None = None
    failure_receipt: ProducerFailureReceipt | None = None
    research_only: bool = True
    broker_execution_enabled: bool = False
    network_enabled: bool = False
    promotion_authority: bool = False

    def __post_init__(self) -> None:
        if self.status is LocalResearchStatus.COMPLETE and self.producer_receipt is None:
            raise ValueError("complete local research report requires a producer receipt")
        if self.status is LocalResearchStatus.FAILED and self.failure_receipt is None:
            raise ValueError("failed local research report requires a failure receipt")
        if self.status is LocalResearchStatus.DISABLED and (
            self.producer_receipt is not None or self.failure_receipt is not None
        ):
            raise ValueError("disabled local research report must remain a no-op")
        if (
            not self.research_only
            or self.broker_execution_enabled
            or self.network_enabled
            or self.promotion_authority
        ):
            raise ValueError("local opportunity research cannot hold execution authority")

    def deterministic_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": self.status.value,
            "mode": self.mode.value,
            "research_only": self.research_only,
            "broker_execution_enabled": self.broker_execution_enabled,
            "network_enabled": self.network_enabled,
            "promotion_authority": self.promotion_authority,
        }
        if self.failure_receipt is not None:
            payload["failure"] = self.failure_receipt.deterministic_payload()
        if self.producer_receipt is not None:
            payload.update(
                {
                    "run_id": self.producer_receipt.pipeline_result.run_id,
                    "decision_count": len(self.producer_receipt.pipeline_result.decisions),
                    "telemetry": tuple(
                        item.deterministic_payload()
                        for item in self.producer_receipt.telemetry
                    ),
                }
            )
        return payload


class LocalOpportunityResearchEntrypoint:
    """One reusable offline mount whose producer owns the evidence cache."""

    def __init__(self, *, enabled: bool = False) -> None:
        self.enabled = enabled
        self.producer = OpportunityResearchProducer(enabled=enabled)

    def run(
        self,
        *,
        mode: OpportunityResearchMode,
        data_truth_root: Path | None,
        snapshot_id: str | None,
        database_path: Path | None,
        decision_at: datetime | None,
        recorded_at: datetime | None,
        universe_evidence_path: Path | None = None,
        catalyst_database_path: Path | None = None,
        alphaops_v5_candidates_path: Path | None = None,
    ) -> LocalOpportunityResearchReport:
        if not self.enabled:
            return LocalOpportunityResearchReport(
                status=LocalResearchStatus.DISABLED,
                mode=mode,
            )
        try:
            if data_truth_root is None or database_path is None:
                raise ValueError("explicit retained input and research database are required")
            if not snapshot_id or decision_at is None or recorded_at is None:
                raise ValueError("snapshot and causal run times are required")
            if not data_truth_root.is_absolute() or not database_path.is_absolute():
                raise ValueError("retained input and research database paths must be absolute")
            identity, manifest = _build_cache_identity(
                data_truth_root=data_truth_root,
                snapshot_id=snapshot_id,
                decision_at=decision_at,
                universe_evidence_path=universe_evidence_path,
                catalyst_database_path=catalyst_database_path,
                alphaops_v5_candidates_path=alphaops_v5_candidates_path,
            )
        except Exception as exc:
            return LocalOpportunityResearchReport(
                status=LocalResearchStatus.FAILED,
                mode=mode,
                failure_receipt=_input_failure(exc),
            )

        def load_request() -> OpportunityAdapterRequest:
            dataset, loaded_manifest = load_datatruth_snapshot(snapshot_id, data_truth_root)
            if loaded_manifest.to_json() != manifest.to_json():
                raise ValueError("retained manifest changed during request construction")
            universe_payload = _load_optional_contract(
                universe_evidence_path,
                schema_version=LOCAL_UNIVERSE_SCHEMA,
            )
            v5_payload = _load_optional_contract(
                alphaops_v5_candidates_path,
                schema_version=LOCAL_V5_SCHEMA,
            )
            catalyst = (
                load_retained_catalyst_adapter(
                    catalyst_database_path,
                    decision_at=decision_at,
                    symbols=dataset.symbols,
                )
                if catalyst_database_path is not None
                else InjectedCatalystAdapter({})
            )
            return _build_request(
                dataset,
                manifest=loaded_manifest,
                decision_at=decision_at,
                universe_payload=universe_payload,
                v5_payload=v5_payload,
                catalyst_adapter=catalyst,
            )

        adapter = (
            CurrentOpportunityAdapter()
            if mode is OpportunityResearchMode.CURRENT
            else HistoricalOpportunityAdapter()
        )
        try:
            receipt = self.producer.run_cached(
                identity,
                load_request,
                database_path=database_path,
                recorded_at=recorded_at,
                adapter=adapter,
            )
        except OpportunityProducerError as exc:
            return LocalOpportunityResearchReport(
                status=LocalResearchStatus.FAILED,
                mode=mode,
                failure_receipt=exc.receipt,
            )
        return LocalOpportunityResearchReport(
            status=LocalResearchStatus.COMPLETE,
            mode=mode,
            producer_receipt=receipt,
        )


def run_local_opportunity_research(
    *,
    enabled: bool = False,
    mode: OpportunityResearchMode = OpportunityResearchMode.CURRENT,
    data_truth_root: Path | None = None,
    snapshot_id: str | None = None,
    database_path: Path | None = None,
    decision_at: datetime | None = None,
    recorded_at: datetime | None = None,
    universe_evidence_path: Path | None = None,
    catalyst_database_path: Path | None = None,
    alphaops_v5_candidates_path: Path | None = None,
) -> LocalOpportunityResearchReport:
    """Run one explicitly enabled local-only opportunity research cycle."""

    return LocalOpportunityResearchEntrypoint(enabled=enabled).run(
        mode=mode,
        data_truth_root=data_truth_root,
        snapshot_id=snapshot_id,
        database_path=database_path,
        decision_at=decision_at,
        recorded_at=recorded_at,
        universe_evidence_path=universe_evidence_path,
        catalyst_database_path=catalyst_database_path,
        alphaops_v5_candidates_path=alphaops_v5_candidates_path,
    )


def _build_cache_identity(
    *,
    data_truth_root: Path,
    snapshot_id: str,
    decision_at: datetime,
    universe_evidence_path: Path | None,
    catalyst_database_path: Path | None,
    alphaops_v5_candidates_path: Path | None,
) -> tuple[EvidenceCacheIdentity, DataTruthManifest]:
    _require_aware(decision_at, "decision_at")
    manifest = verify_datatruth_snapshot(snapshot_id, data_truth_root)
    manifest_observed_at = _parse_time(manifest.created_at, "manifest.created_at")
    if manifest_observed_at > decision_at:
        raise ValueError("retained manifest was unavailable at the decision cutoff")
    observed_times = [manifest_observed_at]
    for path, schema_version in (
        (universe_evidence_path, LOCAL_UNIVERSE_SCHEMA),
        (alphaops_v5_candidates_path, LOCAL_V5_SCHEMA),
    ):
        payload = _load_optional_contract(path, schema_version=schema_version)
        if payload is None:
            continue
        observed_at = _parse_time(payload.get("observed_at"), "local evidence observed_at")
        if observed_at > decision_at:
            raise ValueError("local evidence was unavailable at the decision cutoff")
        observed_times.append(observed_at)
    if catalyst_database_path is not None:
        _require_non_active_database(catalyst_database_path)
        observed_times.append(decision_at)
    available_at = max(observed_times)
    component_hashes = [
        manifest.normalized_artifact_hash,
        manifest.manifest_payload_hash or hashlib.sha256(manifest.to_json().encode()).hexdigest(),
        _optional_file_hash(universe_evidence_path),
        _optional_file_hash(catalyst_database_path),
        _optional_file_hash(alphaops_v5_candidates_path),
    ]
    aggregate = hashlib.sha256()
    for value in component_hashes:
        aggregate.update(value.encode("ascii"))
        aggregate.update(b"\n")
    return (
        EvidenceCacheIdentity(
            provider_identity="local-retained-opportunity-evidence-v1",
            source_identity=f"datatruth:{snapshot_id}",
            payload_hash_sha256=aggregate.hexdigest(),
            observed_at=manifest_observed_at,
            available_at=available_at,
            decision_cutoff=decision_at,
        ),
        manifest,
    )


def _build_request(
    dataset: MarketDataset,
    *,
    manifest: DataTruthManifest,
    decision_at: datetime,
    universe_payload: dict[str, object] | None,
    v5_payload: dict[str, object] | None,
    catalyst_adapter: InjectedCatalystAdapter,
) -> OpportunityAdapterRequest:
    manifest_observed_at = _parse_time(manifest.created_at, "manifest.created_at")
    coverage = tuple(
        bar.timestamp for bars in dataset.bars_by_symbol.values() for bar in bars
    )
    if not coverage:
        raise ValueError("retained dataset contains no bars")
    coverage_start = min(coverage)
    coverage_end = max(coverage)
    if coverage_end > decision_at or manifest_observed_at > decision_at:
        raise ValueError("retained dataset is not causal at the decision cutoff")
    member_payloads, member_observed_at = _member_payloads(
        universe_payload,
        decision_at=decision_at,
    )
    observed_at = max(manifest_observed_at, member_observed_at or manifest_observed_at)
    has_halts = any("halt_status" in item for item in member_payloads.values())
    has_actions = any("corporate_action_status" in item for item in member_payloads.values())
    source_hash = hashlib.sha256(
        (manifest.manifest_payload_hash or manifest.to_json()).encode("utf-8")
    ).hexdigest()
    capability = build_provider_capability_receipt(
        provider=manifest.provider_name or manifest.provider_id,
        feed=f"retained-{manifest.timeframe}",
        entitlement_identity="local-retained-research-only",
        decision_at=decision_at,
        observed_at=observed_at,
        bars=CapabilityState.AVAILABLE,
        trades=CapabilityState.UNSUPPORTED,
        quotes=CapabilityState.UNSUPPORTED,
        consolidated_nbbo=CapabilityState.UNSUPPORTED,
        aggressor_classification=CapabilityState.UNSUPPORTED,
        corporate_actions=(
            CapabilityState.AVAILABLE if has_actions else CapabilityState.UNSUPPORTED
        ),
        halts=CapabilityState.AVAILABLE if has_halts else CapabilityState.UNSUPPORTED,
        historical_coverage=CapabilityState.AVAILABLE,
        coverage_start=coverage_start,
        coverage_end=coverage_end,
        source_identity=f"datatruth-{source_hash}",
        method="verified immutable local DataTruth snapshot",
        limitations=(
            "local retained OHLCV only",
            "no provider or network access",
            "quotes trades and consolidated NBBO unsupported",
        ),
    )
    facts = tuple(
        _member_fact(
            symbol,
            dataset=dataset,
            payload=member_payloads.get(symbol, {}),
            observed_at=observed_at,
            capability_receipt_id=capability.capability_receipt_id,
        )
        for symbol in dataset.symbols
    )
    universe = build_universe_snapshot(
        dataset,
        decision_at=decision_at,
        as_of=observed_at,
        policy=UniversePolicy(
            policy_id="local-retained-opportunity-research",
            version="1.0.0",
        ),
        member_facts=facts,
        capability_receipts=(capability,),
        source_identity=f"datatruth:{manifest.snapshot_id}:{source_hash}",
        limitations=(
            "bounded retained local snapshot",
            "unknown metadata remains unavailable",
        ),
    )
    candidates = _v5_candidates(v5_payload)

    def risk_factory(_prepared: object):
        return (
            {},
            build_pipeline_risk_policy(
                policy_version="local-retained-no-execution-risk-v1",
                account_identity="research-only-no-account",
                risk_cap_identity="no-live-risk-authority",
                concentration_identity="no-live-portfolio-authority",
                minimum_after_cost_reward_risk=Decimal("1.5"),
            ),
        )

    return OpportunityAdapterRequest(
        dataset=dataset,
        universe_snapshot=universe,
        risk_factory=risk_factory,
        registry=build_default_registry(alphaops_v5_candidates=candidates),
        catalyst_adapter=catalyst_adapter,
    )


def _member_fact(
    symbol: str,
    *,
    dataset: MarketDataset,
    payload: Mapping[str, object],
    observed_at: datetime,
    capability_receipt_id: str,
) -> UniverseMemberFact:
    bars = dataset.bars_by_symbol[symbol]
    latest = bars[-1]
    average_dollar_volume = sum(
        (Decimal(str(bar.close)) * bar.volume for bar in bars),
        Decimal("0"),
    ) / Decimal(len(bars))
    return UniverseMemberFact(
        symbol=symbol,
        security_type=_enum_value(SecurityType, payload.get("security_type"), SecurityType.UNKNOWN),
        venue=_optional_text(payload.get("venue")),
        first_seen_at=_optional_time(payload.get("first_seen_at")),
        observed_at=observed_at,
        data_availability=CapabilityState.AVAILABLE,
        halt_status=_enum_value(SafetyStatus, payload.get("halt_status"), SafetyStatus.UNKNOWN),
        corporate_action_status=_enum_value(
            SafetyStatus,
            payload.get("corporate_action_status"),
            SafetyStatus.UNKNOWN,
        ),
        observed_price=Decimal(str(latest.close)),
        average_daily_dollar_volume=average_dollar_volume,
        provider_receipt_ids=(capability_receipt_id,),
        limitations=("local retained metadata only",),
    )


def _member_payloads(
    payload: dict[str, object] | None,
    *,
    decision_at: datetime,
) -> tuple[dict[str, dict[str, object]], datetime | None]:
    if payload is None:
        return {}, None
    observed_at = _parse_time(payload.get("observed_at"), "universe observed_at")
    if observed_at > decision_at:
        raise ValueError("universe evidence was unavailable at the decision cutoff")
    raw_members = payload.get("members")
    if not isinstance(raw_members, list):
        raise ValueError("universe evidence members must be an array")
    members: dict[str, dict[str, object]] = {}
    for item in raw_members:
        if not isinstance(item, dict):
            raise ValueError("universe member evidence must be an object")
        symbol = str(item.get("symbol") or "").strip().upper()
        if not symbol or symbol in members:
            raise ValueError("universe member symbols must be nonblank and unique")
        members[symbol] = item
    return members, observed_at


def _v5_candidates(payload: dict[str, object] | None) -> dict[str, dict[str, Any]]:
    if payload is None:
        return {}
    raw = payload.get("candidates")
    if not isinstance(raw, dict):
        raise ValueError("AlphaOps V5 candidates must be an object")
    candidates: dict[str, dict[str, Any]] = {}
    for symbol, item in raw.items():
        normalized = str(symbol).strip().upper()
        if not normalized or not isinstance(item, dict):
            raise ValueError("AlphaOps V5 candidate entries must be symbol objects")
        candidates[normalized] = dict(item)
    return candidates


def _load_optional_contract(
    path: Path | None,
    *,
    schema_version: str,
) -> dict[str, object] | None:
    if path is None:
        return None
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or payload.get("schema_version") != schema_version:
        raise ValueError("unsupported local opportunity evidence contract")
    return payload


def _optional_file_hash(path: Path | None) -> str:
    if path is None:
        return "0" * 64
    if not path.is_absolute() or not path.is_file():
        raise ValueError("optional retained evidence path must be an absolute file")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _require_non_active_database(path: Path) -> None:
    resolved = path.resolve(strict=False)
    if os.path.normcase(str(resolved)) == os.path.normcase(str(ACTIVE_DATABASE)):
        raise ValueError("active database is forbidden for local opportunity research")


def _input_failure(exc: Exception) -> ProducerFailureReceipt:
    exception_type = type(exc).__name__
    if not exception_type.isidentifier():
        exception_type = "Exception"
    telemetry = (
        StageTelemetry(
            stage_name="retained_input_evidence",
            status=StageStatus.FAILED,
            failure_code=ProducerFailureCode.INPUT_EVIDENCE_FAILED,
            input_count=1,
            output_count=0,
            duration_ms=0.0,
        ),
    )
    return ProducerFailureReceipt(
        failure_code=ProducerFailureCode.INPUT_EVIDENCE_FAILED,
        failed_stage="retained_input_evidence",
        exception_type=exception_type,
        telemetry=telemetry,
    )


def _parse_time(value: object, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field_name} must be an ISO timestamp") from exc
    _require_aware(parsed, field_name)
    return parsed


def _optional_time(value: object) -> datetime | None:
    return None if value in {None, ""} else _parse_time(value, "optional timestamp")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _optional_text(value: object) -> str | None:
    text = str(value or "").strip()
    return text or None


def _enum_value(enum_type: type[Enum], value: object, default: Enum):
    if value in {None, ""}:
        return default
    return enum_type(str(value))


__all__ = [
    "LOCAL_UNIVERSE_SCHEMA",
    "LOCAL_V5_SCHEMA",
    "LocalOpportunityResearchEntrypoint",
    "LocalOpportunityResearchReport",
    "LocalResearchStatus",
    "OpportunityResearchMode",
    "run_local_opportunity_research",
]
