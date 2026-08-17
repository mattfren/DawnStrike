from __future__ import annotations

import sqlite3
from datetime import timezone
from pathlib import Path

import pytest
from test_alphaops_intraday_adapter import DECISION_AT, _signal
from test_opportunity_persistence import _initialize_schema_through
from test_opportunity_pipeline import (
    NOW,
    _execution_risk_for,
    _pipeline_risk_policy,
    _pipeline_universe,
    _two_candidate_dataset,
)

from intraday_scanner.storage.test_isolation import ACTIVE_DATABASE
from intraday_scanner.v2.opportunity.catalyst import (
    CatalystEvidence,
    InjectedCatalystAdapter,
)
from intraday_scanner.v2.opportunity.features import build_feature_snapshots
from intraday_scanner.v2.opportunity.models import EvaluationStatus, FeatureStage
from intraday_scanner.v2.opportunity.producer import (
    CurrentOpportunityAdapter,
    EvidenceCacheIdentity,
    EvidenceIdentityCache,
    HistoricalOpportunityAdapter,
    OpportunityAdapterRequest,
    OpportunityResearchProducer,
    ProducerFailureCode,
    StageStatus,
    StageTelemetry,
)
from intraday_scanner.v2.opportunity.registry import (
    StrategyRegistry,
    alphaops_v5_adapter_parity,
    build_alphaops_v5_adapter,
    build_default_registry,
)
from intraday_scanner.v2.strategies.alphaops_intraday import IntradayDecisionPoint


def _request(*, catalyst: InjectedCatalystAdapter | None = None) -> OpportunityAdapterRequest:
    dataset = _two_candidate_dataset()
    registry = build_default_registry()
    selected = StrategyRegistry(
        (registry.get("DS-MOM-001"), registry.get("DS-OF-001"))
    )

    def risk_factory(prepared):
        return (
            {
                item.evaluation_id: _execution_risk_for(item)
                for item in prepared.evaluations
                if item.status is EvaluationStatus.ELIGIBLE
            },
            _pipeline_risk_policy(),
        )

    return OpportunityAdapterRequest(
        dataset=dataset,
        universe_snapshot=_pipeline_universe(dataset, requested_symbols=("ABC", "DEF")),
        risk_factory=risk_factory,
        registry=selected,
        catalyst_adapter=catalyst,
    )


def test_current_historical_adapters_are_byte_equivalent_over_shared_rules() -> None:
    request = _request()
    current = CurrentOpportunityAdapter().evaluate(request)
    historical = HistoricalOpportunityAdapter().evaluate(request)

    assert current.to_json() == historical.to_json()
    assert current.run_id == historical.run_id


def test_catalyst_adapter_is_causal_and_missing_evidence_stays_unavailable() -> None:
    evidence = CatalystEvidence.from_payload(
        symbol="ABC",
        state="verified_filing",
        observed_at=NOW,
        available_at=NOW,
        source_identity="fixture-sec-feed",
        payload=b"fixture catalyst",
    )
    adapter = InjectedCatalystAdapter({"ABC": evidence})
    request = _request(catalyst=adapter)
    snapshots = build_feature_snapshots(
        request.dataset,
        decision_at=NOW,
        universe_id=request.universe_snapshot.universe_snapshot_id,
        stage=FeatureStage.RICH,
        symbols=("ABC", "DEF"),
        catalyst_adapter=adapter,
    )

    assert snapshots[0].category("catalyst_state").value == "verified_filing"
    assert snapshots[1].category("catalyst_state").value is None
    late = CatalystEvidence.from_payload(
        symbol="ABC",
        state="later_news",
        observed_at=NOW,
        available_at=NOW.replace(hour=10),
        source_identity="fixture-sec-feed",
        payload=b"later",
    )
    with pytest.raises(ValueError, match="post-cutoff catalyst"):
        InjectedCatalystAdapter({"ABC": late}).evidence_at("ABC", decision_at=NOW)


def test_evidence_cache_reuses_exact_identity_only_and_counts_provider_calls() -> None:
    cache: EvidenceIdentityCache[str] = EvidenceIdentityCache()
    calls = 0

    def load() -> str:
        nonlocal calls
        calls += 1
        return f"value-{calls}"

    base = EvidenceCacheIdentity(
        provider_identity="provider-a",
        source_identity="source-a",
        payload_hash_sha256="a" * 64,
        observed_at=NOW,
        available_at=NOW,
        decision_cutoff=NOW,
    )
    first, hit1 = cache.get_or_load(base, load)
    second, hit2 = cache.get_or_load(base, load)
    changed = EvidenceCacheIdentity(
        provider_identity="provider-a",
        source_identity="source-b",
        payload_hash_sha256="a" * 64,
        observed_at=NOW,
        available_at=NOW,
        decision_cutoff=NOW,
    )
    third, hit3 = cache.get_or_load(changed, load)

    assert (first, second, third) == ("value-1", "value-1", "value-2")
    assert (hit1, hit2, hit3, calls) == (False, True, False, 2)


def test_stage_duration_is_observational_not_decision_identity() -> None:
    first = StageTelemetry("pipeline", StageStatus.COMPLETE, None, 2, 1, 1.0)
    second = StageTelemetry("pipeline", StageStatus.COMPLETE, None, 2, 1, 999.0)
    assert first.deterministic_payload() == second.deterministic_payload()


def test_disabled_producer_rejects_active_path_and_mounts_append_replay(tmp_path: Path) -> None:
    request = _request()
    disabled = OpportunityResearchProducer()
    with pytest.raises(RuntimeError, match=ProducerFailureCode.DISABLED.value):
        disabled.run(request, database_path=tmp_path / "research.sqlite", recorded_at=NOW)

    enabled = OpportunityResearchProducer(enabled=True)
    with pytest.raises(ValueError, match=ProducerFailureCode.ACTIVE_PATH_FORBIDDEN.value):
        enabled.run(request, database_path=ACTIVE_DATABASE, recorded_at=NOW)

    database = (tmp_path / "research-schema-30.sqlite").resolve()
    _initialize_schema_through(database, 30)
    receipt = enabled.run(
        request,
        database_path=database,
        recorded_at=NOW.astimezone(timezone.utc),
    )

    assert receipt.research_only is True
    assert receipt.broker_execution_enabled is False
    assert receipt.promotion_authority is False
    assert receipt.pipeline_result.to_json() == receipt.replay.to_json()
    assert [item.stage_name for item in receipt.telemetry] == [
        "shared_opportunity_pipeline",
        "immutable_append_and_read_only_replay",
    ]
    with sqlite3.connect(database) as connection:
        row = connection.execute(
            "SELECT COUNT(*) FROM opportunity_pipeline_runs"
        ).fetchone()
        assert row is not None and row[0] == 1


def test_alphaops_v5_adapter_delegates_frozen_policy_with_byte_semantics() -> None:
    point = IntradayDecisionPoint(
        symbol="NOVA",
        decision_at=DECISION_AT,
        signal=_signal(),
        observation={
            "price": 10.05,
            "observed_at": DECISION_AT.isoformat(),
            "requested_at": DECISION_AT.isoformat(),
            "freshness_seconds": 0,
            "is_usable": True,
        },
        artifact_identity="fixture:bars:NOVA:2026-08-03",
        artifact_hash_sha256="bars-hash",
        exchange_session_id="XNYS:2026-08-03:regular",
    )
    adapter = build_alphaops_v5_adapter({"NOVA": _signal()})

    assert adapter.strategy_id == "alphaops_v5"
    assert adapter.parameters["broker_execution_enabled"] is False
    assert alphaops_v5_adapter_parity(point) is True
