from __future__ import annotations

from dataclasses import FrozenInstanceError, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from itertools import product

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.opportunity import (
    CapabilityState,
    DecisionRunBinding,
    DecisionRunContext,
    EvaluationStatus,
    EvidenceKind,
    ExecutionRiskEvidence,
    LifecycleActorType,
    ProviderCapabilityReceipt,
    QualityGateConfig,
    QuoteEvidenceScope,
    RiskMetric,
    RiskNumericEvidence,
    RiskSafetyCategory,
    RiskSafetyEvidence,
    RiskUnit,
    RiskValueStatus,
    SafetyStatus,
    SecurityType,
    StrategyDirection,
    StrategyEvaluation,
    StrategyLifecycleTransition,
    StrategyValidationState,
    TradeDecision,
    TradeDecisionValue,
    UniverseEligibility,
    UniverseMemberFact,
    UniverseMembershipStatus,
    UniversePolicy,
    UniverseSnapshot,
    apply_quality_gate,
    build_decision_run_context,
    build_execution_risk_evidence,
    build_expectancy_evidence,
    build_provider_capability_receipt,
    build_risk_numeric_evidence,
    build_risk_safety_evidence,
    build_universe_snapshot,
    rank_opportunities,
    reconcile_trade_decisions,
    validate_lifecycle_transition,
)
from intraday_scanner.v2.opportunity.models import stable_identity

NOW = datetime(2026, 8, 11, 15, 0, tzinfo=UTC)
GATE_CONFIG = QualityGateConfig(minimum_after_cost_reward_risk=Decimal("2.5"))

ALLOWED_LIFECYCLE_TRANSITIONS = {
    StrategyValidationState.EXPERIMENTAL: {
        StrategyValidationState.RESEARCH_PASS,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.RESEARCH_PASS: {
        StrategyValidationState.VALIDATION_PASS,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.VALIDATION_PASS: {
        StrategyValidationState.OOS_PASS,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.OOS_PASS: {
        StrategyValidationState.PAPER_TRADING,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.PAPER_TRADING: {
        StrategyValidationState.PRODUCTION_ELIGIBLE,
        StrategyValidationState.DEGRADED,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.PRODUCTION_ELIGIBLE: {
        StrategyValidationState.DEGRADED,
        StrategyValidationState.DISABLED,
    },
    StrategyValidationState.DEGRADED: {
        StrategyValidationState.PAPER_TRADING,
        StrategyValidationState.DISABLED,
        StrategyValidationState.REJECTED,
    },
    StrategyValidationState.DISABLED: {StrategyValidationState.EXPERIMENTAL},
    StrategyValidationState.REJECTED: set(),
}

LIFECYCLE_PROMOTION_EDGES = (
    (StrategyValidationState.EXPERIMENTAL, StrategyValidationState.RESEARCH_PASS),
    (StrategyValidationState.RESEARCH_PASS, StrategyValidationState.VALIDATION_PASS),
    (StrategyValidationState.VALIDATION_PASS, StrategyValidationState.OOS_PASS),
    (StrategyValidationState.OOS_PASS, StrategyValidationState.PAPER_TRADING),
    (StrategyValidationState.PAPER_TRADING, StrategyValidationState.PRODUCTION_ELIGIBLE),
    (StrategyValidationState.DEGRADED, StrategyValidationState.PAPER_TRADING),
    (StrategyValidationState.DISABLED, StrategyValidationState.EXPERIMENTAL),
)


def _dataset(*symbols: str) -> MarketDataset:
    return MarketDataset(
        dataset_id="universe-fixture",
        source_kind="bounded_fixture_ohlcv",
        timeframe="1m",
        bars_by_symbol={
            symbol: (
                MarketBar(
                    symbol=symbol,
                    timestamp=NOW - timedelta(minutes=1),
                    open=10.0,
                    high=10.5,
                    low=9.5,
                    close=10.25,
                    volume=10_000,
                    exchange_session_id="XNYS:2026-08-11:regular",
                ),
            )
            for symbol in symbols
        },
        source_refs=("fixture:retained",),
    )


def _capability(
    *,
    provider: str = "fixture-provider",
    feed: str = "iex",
    observed_at: datetime = NOW,
    bars: CapabilityState = CapabilityState.AVAILABLE,
    consolidated_nbbo: CapabilityState = CapabilityState.UNSUPPORTED,
    aggressor: CapabilityState = CapabilityState.UNSUPPORTED,
    historical_coverage: CapabilityState = CapabilityState.AVAILABLE,
    coverage_end: datetime | None = None,
    entitlement_identity: str = "fixture-read-only",
    source_identity: str = "fixture-capability-artifact",
    method: str = "sanitized_fixture_probe",
    limitations: tuple[str, ...] = ("bounded fixture capability",),
) -> ProviderCapabilityReceipt:
    return build_provider_capability_receipt(
        provider=provider,
        feed=feed,
        entitlement_identity=entitlement_identity,
        decision_at=NOW,
        observed_at=observed_at,
        bars=bars,
        trades=CapabilityState.AVAILABLE,
        quotes=CapabilityState.AVAILABLE,
        consolidated_nbbo=consolidated_nbbo,
        aggressor_classification=aggressor,
        corporate_actions=CapabilityState.AVAILABLE,
        halts=CapabilityState.AVAILABLE,
        historical_coverage=historical_coverage,
        coverage_start=(
            NOW - timedelta(days=1)
            if historical_coverage is CapabilityState.AVAILABLE
            else None
        ),
        coverage_end=(
            observed_at if coverage_end is None else coverage_end
        )
        if historical_coverage is CapabilityState.AVAILABLE
        else None,
        source_identity=source_identity,
        method=method,
        limitations=limitations,
    )


def _fact(
    symbol: str,
    receipt: ProviderCapabilityReceipt,
    *,
    security_type: SecurityType = SecurityType.COMMON_STOCK,
    observed_at: datetime = NOW,
    data_availability: CapabilityState = CapabilityState.AVAILABLE,
    halt_status: SafetyStatus = SafetyStatus.CLEAR,
    corporate_action_status: SafetyStatus = SafetyStatus.CLEAR,
    informational_reason_codes: tuple[str, ...] = (),
    declared_exclusion_reason_codes: tuple[str, ...] = (),
) -> UniverseMemberFact:
    return UniverseMemberFact(
        symbol=symbol,
        security_type=security_type,
        venue="XNYS",
        first_seen_at=NOW - timedelta(days=30),
        observed_at=observed_at,
        data_availability=data_availability,
        halt_status=halt_status,
        corporate_action_status=corporate_action_status,
        observed_price=Decimal("10.25"),
        average_daily_dollar_volume=Decimal("2500000"),
        provider_receipt_ids=(receipt.capability_receipt_id,),
        informational_reason_codes=informational_reason_codes,
        declared_exclusion_reason_codes=declared_exclusion_reason_codes,
    )


def _policy(
    *,
    allowed: tuple[SecurityType, ...] = (SecurityType.COMMON_STOCK,),
    include_etfs: bool = False,
    include_adrs: bool = False,
) -> UniversePolicy:
    return UniversePolicy(
        policy_id="research-us-equities",
        version="1.0.0",
        allowed_security_types=allowed,
        include_etfs=include_etfs,
        include_adrs=include_adrs,
        minimum_price=Decimal("1"),
        minimum_average_daily_dollar_volume=Decimal("1000000"),
    )


def _evaluation(
    *,
    direction: StrategyDirection = StrategyDirection.LONG,
    entry: Decimal | None = Decimal("100"),
    stop: Decimal | None = Decimal("98"),
    target: Decimal | None = Decimal("106"),
) -> StrategyEvaluation:
    return StrategyEvaluation(
        evaluation_id="evaluation-risk-fixture",
        candidate_id="candidate-risk-fixture",
        feature_snapshot_id="features-risk-fixture",
        symbol="RISK",
        decision_at=NOW,
        strategy_id="DS-MOM-001",
        strategy_version="1.0.0",
        strategy_definition_hash="definition-hash",
        evaluator_id="risk-fixture-evaluator",
        evaluator_code_hash="evaluator-hash",
        lifecycle=StrategyValidationState.PRODUCTION_ELIGIBLE,
        direction=direction,
        status=EvaluationStatus.ELIGIBLE,
        reasons=("synthetic fixture evaluation",),
        entry_price=entry,
        invalidation_price=stop,
        target_price=target,
        after_cost_reward_risk=None,
        anomaly_strength=Decimal("0.8"),
        regime_fit=Decimal("0.75"),
        data_quality_score=Decimal("1"),
        liquidity_score=Decimal("0.9"),
    )


def _risk_numeric(
    metric: RiskMetric,
    value: Decimal | None,
    *,
    status: RiskValueStatus = RiskValueStatus.OBSERVED,
    capability_state: CapabilityState | None = None,
    evidence_kind: EvidenceKind = EvidenceKind.EMPIRICAL,
    observed_at: datetime = NOW,
    source_identity: str = "bounded-risk-fixture",
    reason: str = "synthetic point-in-time fixture",
) -> RiskNumericEvidence:
    capability_state = capability_state or (
        CapabilityState.AVAILABLE if value is not None else CapabilityState.UNAVAILABLE
    )
    return build_risk_numeric_evidence(
        metric=metric,
        value=value,
        status=status,
        capability_state=capability_state,
        evidence_kind=evidence_kind,
        observed_at=observed_at,
        source_identity=source_identity,
        method="direct fixture observation",
        reason=reason,
        limitations=("synthetic fixture only",),
    )


def _base_risk_metrics(
    *,
    entry: Decimal | None = Decimal("100"),
    stop: Decimal | None = Decimal("98"),
    target: Decimal | None = Decimal("106"),
    quantity: Decimal | None = Decimal("100"),
    account_equity: Decimal | None = Decimal("100000"),
    risk_fraction: Decimal | None = Decimal("0.00222"),
    concentration: Decimal | None = Decimal("0.10"),
    max_concentration: Decimal | None = Decimal("0.25"),
    quote_age: Decimal | None = Decimal("1"),
    max_quote_age: Decimal | None = Decimal("5"),
    minimum_r: Decimal | None = Decimal("2.5"),
    spread_status: RiskValueStatus = RiskValueStatus.OBSERVED,
    unavailable_reason: str = "input unavailable at decision time",
) -> tuple[RiskNumericEvidence, ...]:
    values = {
        RiskMetric.ENTRY_PRICE: entry,
        RiskMetric.STOP_PRICE: stop,
        RiskMetric.TARGET_PRICE: target,
        RiskMetric.SPREAD_BPS: Decimal("10"),
        RiskMetric.ENTRY_SLIPPAGE_BPS: Decimal("5"),
        RiskMetric.EXIT_SLIPPAGE_BPS: Decimal("5"),
        RiskMetric.ROUND_TRIP_FEE_PER_SHARE: Decimal("0.02"),
        RiskMetric.QUANTITY: quantity,
        RiskMetric.ACCOUNT_EQUITY: account_equity,
        RiskMetric.RISK_FRACTION: risk_fraction,
        RiskMetric.AGGREGATE_CONCENTRATION: concentration,
        RiskMetric.MAX_AGGREGATE_CONCENTRATION: max_concentration,
        RiskMetric.MAX_QUOTE_AGE: max_quote_age,
        RiskMetric.MIN_AFTER_COST_REWARD_RISK: minimum_r,
    }
    evidence: list[RiskNumericEvidence] = []
    for metric, value in values.items():
        status = spread_status if metric is RiskMetric.SPREAD_BPS else RiskValueStatus.OBSERVED
        kind = (
            EvidenceKind.HEURISTIC
            if status is RiskValueStatus.PROVISIONAL
            else EvidenceKind.EMPIRICAL
        )
        if value is None:
            status = RiskValueStatus.UNAVAILABLE
        source_identity = {
            RiskMetric.ACCOUNT_EQUITY: "paper-account-fixture",
            RiskMetric.RISK_FRACTION: "fixed-fractional-policy-v1",
            RiskMetric.AGGREGATE_CONCENTRATION: "aggregate-book-fixture",
            RiskMetric.MAX_AGGREGATE_CONCENTRATION: "aggregate-book-fixture",
        }.get(metric, "bounded-risk-fixture")
        evidence.append(
            _risk_numeric(
                metric,
                value,
                status=status,
                evidence_kind=kind,
                observed_at=(
                    NOW - timedelta(seconds=float(quote_age))
                    if metric is RiskMetric.SPREAD_BPS and quote_age is not None
                    else NOW
                ),
                source_identity=source_identity,
                reason=unavailable_reason if value is None else "synthetic point-in-time fixture",
            )
        )
    return tuple(evidence)


def _safety_evidence(
    category: RiskSafetyCategory,
    capability: ProviderCapabilityReceipt,
    *,
    status: SafetyStatus = SafetyStatus.CLEAR,
    observed_at: datetime = NOW,
    symbol: str = "RISK",
) -> RiskSafetyEvidence:
    return build_risk_safety_evidence(
        category=category,
        symbol=symbol,
        status=status,
        observed_at=observed_at,
        source_identity="bounded-safety-fixture",
        method="point-in-time fixture observation",
        capability_receipt_id=capability.capability_receipt_id,
        reason=None if status is SafetyStatus.CLEAR else "provider status not clear",
        limitations=("synthetic safety fixture only",),
    )


def _execution_risk(
    *,
    evaluation: StrategyEvaluation | None = None,
    capability: ProviderCapabilityReceipt | None = None,
    base_metrics: tuple[RiskNumericEvidence, ...] | None = None,
    quote_scope: QuoteEvidenceScope = QuoteEvidenceScope.NBBO,
    halt_status: SafetyStatus = SafetyStatus.CLEAR,
    action_status: SafetyStatus = SafetyStatus.CLEAR,
    halt_evidence: RiskSafetyEvidence | None = None,
    action_evidence: RiskSafetyEvidence | None = None,
    account_identity: str = "paper-account-fixture",
    risk_cap_identity: str = "fixed-fractional-policy-v1",
    concentration_identity: str = "aggregate-book-fixture",
) -> ExecutionRiskEvidence:
    evaluation = evaluation or _evaluation()
    capability = capability or _capability(
        provider="fixture-sip-provider",
        feed="sip",
        consolidated_nbbo=CapabilityState.AVAILABLE,
    )
    halt_evidence = halt_evidence or _safety_evidence(
        RiskSafetyCategory.HALT,
        capability,
        status=halt_status,
        symbol=evaluation.symbol,
    )
    action_evidence = action_evidence or _safety_evidence(
        RiskSafetyCategory.CORPORATE_ACTION,
        capability,
        status=action_status,
        symbol=evaluation.symbol,
    )
    return build_execution_risk_evidence(
        evaluation=evaluation,
        capability_receipts=(capability,),
        quote_capability_receipt_id=(
            capability.capability_receipt_id
            if quote_scope
            in {QuoteEvidenceScope.NBBO, QuoteEvidenceScope.NONCONSOLIDATED}
            else None
        ),
        quote_scope=quote_scope,
        halt_evidence=halt_evidence,
        corporate_action_evidence=action_evidence,
        account_identity=account_identity,
        risk_cap_identity=risk_cap_identity,
        concentration_identity=concentration_identity,
        base_metrics=base_metrics or _base_risk_metrics(),
        limitations=("bounded synthetic execution-risk fixture",),
    )


def _empirical_evaluation(
    *,
    lifecycle: StrategyValidationState = StrategyValidationState.PRODUCTION_ELIGIBLE,
    evaluation_id: str = "evaluation-risk-fixture",
    symbol: str = "RISK",
    strategy_version: str = "1.0.0",
) -> StrategyEvaluation:
    expectancy = build_expectancy_evidence(
        (Decimal("1"),) * 120 + (Decimal("-1"),) * 80,
        cohort_id=f"fixture:{evaluation_id}",
        min_sample_size=100,
    )
    return replace(
        _evaluation(),
        evaluation_id=evaluation_id,
        symbol=symbol,
        strategy_version=strategy_version,
        lifecycle=lifecycle,
        after_cost_reward_risk=Decimal("-99"),
        expectancy=expectancy,
    )


def _gate_one(
    evaluation: StrategyEvaluation,
    risk_evidence: ExecutionRiskEvidence | None,
    *,
    config: QualityGateConfig = GATE_CONFIG,
) -> TradeDecision:
    ranked = rank_opportunities((evaluation,))[0]
    risk_map = (
        {evaluation.evaluation_id: risk_evidence}
        if risk_evidence is not None
        else {}
    )
    context = build_decision_run_context(
        (evaluation,),
        (ranked,),
        risk_by_evaluation=risk_map,
        config=config,
    )
    return apply_quality_gate(
        ranked,
        evaluation,
        decision_context=context,
        risk_evidence=risk_evidence,
        config=config,
    )


def test_capability_receipt_is_content_bound_deterministic_and_round_trips() -> None:
    receipt = _capability()

    restored = ProviderCapabilityReceipt.from_json(receipt.to_json())

    assert restored == receipt
    assert restored.content_hash() == receipt.content_hash()
    assert receipt.to_json() == _capability().to_json()
    with pytest.raises(FrozenInstanceError):
        receipt.feed = "sip"  # type: ignore[misc]


def test_capability_truth_rejects_future_private_and_unsupported_claims() -> None:
    with pytest.raises(ValueError, match="after decision_at"):
        _capability(observed_at=NOW + timedelta(seconds=1))
    with pytest.raises(ValueError, match="IEX feed"):
        _capability(consolidated_nbbo=CapabilityState.AVAILABLE)
    with pytest.raises(ValueError, match="OHLCV"):
        _capability(
            feed="sip",
            aggressor=CapabilityState.AVAILABLE,
            method="OHLCV derived classification",
        )
    with pytest.raises(ValueError, match="private value"):
        _capability(limitations=("api_key=do-not-retain",))


@pytest.mark.parametrize(
    ("field", "value"),
    (
        ("limitation", "C:/Users/operator/private/receipt.json"),
        ("limitation", "Authorization: Bearer credential-value"),
        ("limitation", "https://feed.invalid/bars?token=credential-value"),
        ("entitlement", "token=credential-value"),
        ("source", "C:/Users/operator/private/source.json"),
    ),
)
def test_capability_receipt_redacts_private_and_secret_shapes(
    field: str,
    value: str,
) -> None:
    kwargs: dict[str, object] = {}
    if field == "limitation":
        kwargs["limitations"] = (value,)
    elif field == "entitlement":
        kwargs["entitlement_identity"] = value
    else:
        kwargs["source_identity"] = value
    with pytest.raises(ValueError, match="private|secret"):
        _capability(**kwargs)  # type: ignore[arg-type]


def test_capability_receipt_rejects_coverage_later_than_observation_and_iex_variants() -> None:
    with pytest.raises(ValueError, match="after observed_at"):
        _capability(
            observed_at=NOW - timedelta(minutes=2),
            coverage_end=NOW - timedelta(minutes=1),
        )
    with pytest.raises(ValueError, match="IEX feed"):
        _capability(
            provider="alpaca-iex-market-data",
            feed="real-time",
            consolidated_nbbo=CapabilityState.AVAILABLE,
        )


def test_universe_policy_requires_explicit_etf_and_adr_opt_in() -> None:
    with pytest.raises(ValueError, match="ETF policy"):
        _policy(allowed=(SecurityType.COMMON_STOCK, SecurityType.ETF))
    with pytest.raises(ValueError, match="ADR policy"):
        _policy(allowed=(SecurityType.COMMON_STOCK, SecurityType.ADR))

    policy = _policy(
        allowed=(SecurityType.COMMON_STOCK, SecurityType.ETF, SecurityType.ADR),
        include_etfs=True,
        include_adrs=True,
    )
    assert policy.admission_evidence_kind.value == "heuristic"
    with pytest.raises(ValueError, match="must remain heuristic"):
        replace(policy, admission_evidence_kind=EvidenceKind.EMPIRICAL)


@pytest.mark.parametrize(
    "security_type",
    (
        SecurityType.OTC,
        SecurityType.WARRANT,
        SecurityType.RIGHT,
        SecurityType.UNIT,
        SecurityType.PREFERRED,
    ),
)
def test_default_policy_excludes_unsupported_security_types(
    security_type: SecurityType,
) -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(_fact("ABC", receipt, security_type=security_type),),
        capability_receipts=(receipt,),
        requested_symbols=("ABC",),
        source_identity="bounded-fixture-source",
    )

    assert snapshot.included_count == 0
    assert snapshot.excluded_count == 1
    assert snapshot.excluded_members[0].eligibility is UniverseEligibility.INELIGIBLE
    assert snapshot.excluded_members[0].exclusion_reason_codes == (
        f"security_type_not_allowed:{security_type.value}",
    )


def test_common_stock_etf_and_adr_admission_is_policy_bound() -> None:
    receipt = _capability()
    policy = _policy(
        allowed=(SecurityType.COMMON_STOCK, SecurityType.ETF, SecurityType.ADR),
        include_etfs=True,
        include_adrs=True,
    )
    snapshot = build_universe_snapshot(
        _dataset("ABC", "ETF", "ADR"),
        decision_at=NOW,
        as_of=NOW,
        policy=policy,
        member_facts=(
            _fact("ABC", receipt),
            _fact("ETF", receipt, security_type=SecurityType.ETF),
            _fact("ADR", receipt, security_type=SecurityType.ADR),
        ),
        capability_receipts=(receipt,),
        source_identity="bounded-fixture-source",
    )

    assert snapshot.eligible_symbols == ("ABC", "ADR", "ETF")
    assert snapshot.policy_hash == policy.content_hash()


def test_informational_reason_is_retained_but_declared_exclusion_is_enforced() -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("INFO", "EXCL"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(
            _fact(
                "INFO",
                receipt,
                informational_reason_codes=("provider_symbol_normalized",),
            ),
            _fact(
                "EXCL",
                receipt,
                declared_exclusion_reason_codes=("source_identity_conflict",),
            ),
        ),
        capability_receipts=(receipt,),
        source_identity="bounded-fixture-source",
    )

    assert snapshot.eligible_symbols == ("INFO",)
    assert snapshot.included_members[0].informational_reason_codes == (
        "provider_symbol_normalized",
    )
    assert snapshot.excluded_members[0].exclusion_reason_codes == (
        "source_identity_conflict",
    )


def test_missing_metadata_remains_unknown_and_counts_reconcile() -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC", "MISSING"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(_fact("ABC", receipt),),
        capability_receipts=(receipt,),
        source_identity="bounded-fixture-source",
    )

    assert snapshot.requested_count == 2
    assert snapshot.included_count == 1
    assert snapshot.excluded_count == 1
    assert snapshot.unknown_metadata_count == 1
    missing = snapshot.excluded_members[0]
    assert missing.symbol == "MISSING"
    assert missing.security_type is SecurityType.UNKNOWN
    assert missing.data_availability is CapabilityState.UNKNOWN
    assert missing.eligibility is UniverseEligibility.UNKNOWN
    assert "membership_metadata_absent" in missing.exclusion_reason_codes


def test_member_data_availability_requires_causal_receipt_and_bar_coverage() -> None:
    receipt = _capability()
    base = _fact("ABC", receipt)
    kwargs = {
        "decision_at": NOW,
        "as_of": NOW,
        "policy": _policy(),
        "capability_receipts": (receipt,),
        "requested_symbols": ("ABC",),
        "source_identity": "bounded-fixture-source",
    }
    with pytest.raises(ValueError, match="requires a referenced receipt"):
        build_universe_snapshot(
            _dataset("ABC"),
            member_facts=(replace(base, provider_receipt_ids=()),),
            **kwargs,  # type: ignore[arg-type]
        )

    unsupported = _capability(
        provider="fixture-no-bars",
        feed="bounded",
        bars=CapabilityState.UNSUPPORTED,
        historical_coverage=CapabilityState.UNSUPPORTED,
    )
    with pytest.raises(ValueError, match="covering the dataset bar interval"):
        build_universe_snapshot(
            _dataset("ABC"),
            member_facts=(_fact("ABC", unsupported),),
            capability_receipts=(unsupported,),
            **{key: value for key, value in kwargs.items() if key != "capability_receipts"},
        )

    early_coverage = _capability(
        provider="fixture-early-coverage",
        feed="bounded",
        coverage_end=NOW - timedelta(minutes=2),
    )
    with pytest.raises(ValueError, match="covering the dataset bar interval"):
        build_universe_snapshot(
            _dataset("ABC"),
            member_facts=(_fact("ABC", early_coverage),),
            capability_receipts=(early_coverage,),
            **{key: value for key, value in kwargs.items() if key != "capability_receipts"},
        )


def test_unknown_member_availability_is_not_promoted_by_available_receipt() -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(
            _fact("ABC", receipt, data_availability=CapabilityState.UNKNOWN),
        ),
        capability_receipts=(receipt,),
        requested_symbols=("ABC",),
        source_identity="bounded-fixture-source",
    )
    assert snapshot.included_members == ()
    assert snapshot.excluded_members[0].data_availability is CapabilityState.UNKNOWN


def test_member_fact_cannot_predate_its_referenced_capability_receipt() -> None:
    receipt = _capability(observed_at=NOW)
    with pytest.raises(ValueError, match="cannot predate"):
        build_universe_snapshot(
            _dataset("ABC"),
            decision_at=NOW,
            as_of=NOW,
            policy=_policy(),
            member_facts=(
                _fact("ABC", receipt, observed_at=NOW - timedelta(seconds=1)),
            ),
            capability_receipts=(receipt,),
            requested_symbols=("ABC",),
            source_identity="bounded-fixture-source",
        )


def test_capability_receipt_must_exist_by_universe_as_of() -> None:
    receipt = _capability(observed_at=NOW)
    as_of = NOW - timedelta(minutes=1)
    with pytest.raises(ValueError, match="after universe as_of"):
        build_universe_snapshot(
            _dataset("ABC"),
            decision_at=NOW,
            as_of=as_of,
            policy=_policy(),
            member_facts=(_fact("ABC", receipt, observed_at=as_of),),
            capability_receipts=(receipt,),
            requested_symbols=("ABC",),
            source_identity="bounded-fixture-source",
        )


def test_universe_round_trip_and_count_or_identity_tamper_rejects() -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(_fact("ABC", receipt),),
        capability_receipts=(receipt,),
        source_identity="bounded-fixture-source",
    )
    assert UniverseSnapshot.from_json(snapshot.to_json()) == snapshot

    with pytest.raises(ValueError, match="included_count"):
        UniverseSnapshot.from_dict({**snapshot.to_dict(), "included_count": 2})
    with pytest.raises(ValueError, match="identity"):
        UniverseSnapshot.from_dict(
            {**snapshot.to_dict(), "source_identity": "mutated-source"}
        )


def test_universe_contract_rejects_future_member_or_unbound_receipt_reference() -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(_fact("ABC", receipt),),
        capability_receipts=(receipt,),
        source_identity="bounded-fixture-source",
    )
    member = snapshot.included_members[0]
    member_payload = {**member.to_dict(), "as_of": (NOW + timedelta(seconds=1)).isoformat()}
    member_payload["member_id"] = stable_identity(
        "universe-member",
        {key: value for key, value in member_payload.items() if key != "member_id"},
    )
    future_payload = {**snapshot.to_dict(), "included_members": [member_payload]}
    future_payload["universe_snapshot_id"] = stable_identity(
        "universe",
        {
            key: value
            for key, value in future_payload.items()
            if key != "universe_snapshot_id"
        },
    )
    with pytest.raises(ValueError, match="after snapshot as_of"):
        UniverseSnapshot.from_dict(future_payload)

    unbound_payload = {**snapshot.to_dict(), "provider_receipt_ids": []}
    unbound_payload["universe_snapshot_id"] = stable_identity(
        "universe",
        {
            key: value
            for key, value in unbound_payload.items()
            if key != "universe_snapshot_id"
        },
    )
    with pytest.raises(ValueError, match="receipt absent from snapshot"):
        UniverseSnapshot.from_dict(unbound_payload)


def test_universe_member_contract_rejects_nonfinite_values_and_blank_or_duplicate_metadata() -> (
    None
):
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(_fact("ABC", receipt),),
        capability_receipts=(receipt,),
        source_identity="bounded-fixture-source",
    )
    member = snapshot.included_members[0]
    with pytest.raises(ValueError, match="finite and positive"):
        type(member).from_dict({**member.to_dict(), "observed_price": "NaN"})
    with pytest.raises(ValueError, match="duplicate universe member limitation"):
        type(member).from_dict({**member.to_dict(), "limitations": ["x", "x"]})
    with pytest.raises(ValueError, match="cannot be blank"):
        type(member).from_dict({**member.to_dict(), "informational_reason_codes": [""]})
    with pytest.raises(ValueError, match="cannot be blank"):
        replace(
            _fact("ABC", receipt),
            declared_exclusion_reason_codes=("",),
        )


def test_duplicate_conflicting_and_future_membership_facts_reject() -> None:
    receipt = _capability()
    fact = _fact("ABC", receipt)
    kwargs = {
        "decision_at": NOW,
        "as_of": NOW,
        "policy": _policy(),
        "capability_receipts": (receipt,),
        "requested_symbols": ("ABC",),
        "source_identity": "bounded-fixture-source",
    }
    with pytest.raises(ValueError, match="duplicate or conflicting"):
        build_universe_snapshot(
            _dataset("ABC"), member_facts=(fact, fact), **kwargs  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="after universe as_of"):
        build_universe_snapshot(
            _dataset("ABC"),
            member_facts=(_fact("ABC", receipt, observed_at=NOW + timedelta(seconds=1)),),
            **kwargs,  # type: ignore[arg-type]
        )


def test_universe_builder_rejects_future_mismatched_or_normalization_duplicate_inputs() -> None:
    receipt = _capability()
    fact = _fact("ABC", receipt)
    common = {
        "decision_at": NOW,
        "as_of": NOW,
        "policy": _policy(),
        "member_facts": (fact,),
        "capability_receipts": (receipt,),
        "requested_symbols": ("ABC",),
        "source_identity": "bounded-fixture-source",
    }
    future_bar = replace(
        _dataset("ABC").bars_by_symbol["ABC"][0],
        timestamp=NOW + timedelta(seconds=1),
    )
    with pytest.raises(ValueError, match="after universe as_of"):
        build_universe_snapshot(
            replace(_dataset("ABC"), bars_by_symbol={"ABC": (future_bar,)}),
            **common,  # type: ignore[arg-type]
        )
    mismatched_bar = replace(_dataset("ABC").bars_by_symbol["ABC"][0], symbol="XYZ")
    with pytest.raises(ValueError, match="conflicts with dataset mapping"):
        build_universe_snapshot(
            replace(_dataset("ABC"), bars_by_symbol={"ABC": (mismatched_bar,)}),
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="normalized requested symbol"):
        build_universe_snapshot(
            _dataset("ABC"),
            **{**common, "requested_symbols": ("abc", "ABC")},  # type: ignore[arg-type]
        )


def test_universe_builder_rejects_unsorted_and_duplicate_dataset_timestamps() -> None:
    receipt = _capability()
    newest = _dataset("ABC").bars_by_symbol["ABC"][0]
    older = replace(newest, timestamp=NOW - timedelta(minutes=2))
    common = {
        "decision_at": NOW,
        "as_of": NOW,
        "policy": _policy(),
        "member_facts": (_fact("ABC", receipt),),
        "capability_receipts": (receipt,),
        "requested_symbols": ("ABC",),
        "source_identity": "bounded-fixture-source",
    }
    with pytest.raises(ValueError, match="strictly chronological and unique"):
        build_universe_snapshot(
            replace(_dataset("ABC"), bars_by_symbol={"ABC": (newest, older)}),
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="strictly chronological and unique"):
        build_universe_snapshot(
            replace(_dataset("ABC"), bars_by_symbol={"ABC": (newest, newest)}),
            **common,  # type: ignore[arg-type]
        )


def test_available_fact_with_empty_dataset_bars_is_not_admitted() -> None:
    receipt = _capability()
    dataset = replace(_dataset("ABC"), bars_by_symbol={"ABC": ()})
    with pytest.raises(ValueError, match="covering the dataset bar interval"):
        build_universe_snapshot(
            dataset,
            decision_at=NOW,
            as_of=NOW,
            policy=_policy(),
            member_facts=(_fact("ABC", receipt),),
            capability_receipts=(receipt,),
            requested_symbols=("ABC",),
            source_identity="bounded-fixture-source",
        )


def test_member_receipt_ids_require_canonical_sorted_order() -> None:
    first = _capability(provider="provider-a", feed="bounded")
    second = _capability(provider="provider-b", feed="bounded")
    unordered = tuple(
        sorted((first.capability_receipt_id, second.capability_receipt_id), reverse=True)
    )
    with pytest.raises(ValueError, match="canonical sorted order"):
        replace(_fact("ABC", first), provider_receipt_ids=unordered)


def test_snapshot_receipt_ids_require_canonical_sorted_order() -> None:
    first = _capability(provider="provider-a", feed="bounded")
    second = _capability(provider="provider-b", feed="bounded")
    receipt_ids = tuple(sorted((first.capability_receipt_id, second.capability_receipt_id)))
    fact = replace(_fact("ABC", first), provider_receipt_ids=receipt_ids)
    snapshot = build_universe_snapshot(
        _dataset("ABC"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(fact,),
        capability_receipts=(first, second),
        source_identity="bounded-fixture-source",
    )
    with pytest.raises(ValueError, match="canonical sorted order"):
        UniverseSnapshot.from_dict(
            {
                **snapshot.to_dict(),
                "provider_receipt_ids": list(reversed(snapshot.provider_receipt_ids)),
            }
        )


def test_explicit_empty_universe_stays_empty() -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(),
        capability_receipts=(receipt,),
        requested_symbols=(),
        source_identity="bounded-fixture-source",
    )

    assert snapshot.requested_symbols == ()
    assert snapshot.included_members == ()
    assert snapshot.excluded_members == ()
    assert snapshot.requested_count == snapshot.included_count == snapshot.excluded_count == 0


def test_benchmark_is_retained_separately_and_never_a_trade_member() -> None:
    receipt = _capability()
    snapshot = build_universe_snapshot(
        _dataset("ABC", "SPY"),
        decision_at=NOW,
        as_of=NOW,
        policy=_policy(),
        member_facts=(_fact("ABC", receipt), _fact("SPY", receipt)),
        capability_receipts=(receipt,),
        requested_symbols=("ABC", "SPY"),
        benchmark_symbol="SPY",
        source_identity="bounded-fixture-source",
    )

    assert snapshot.eligible_symbols == ("ABC",)
    assert snapshot.benchmark_member is not None
    assert snapshot.benchmark_member.symbol == "SPY"
    assert snapshot.benchmark_member.benchmark_only is True
    assert "SPY" not in snapshot.requested_symbols


def test_lifecycle_transition_returns_immutable_content_bound_receipt() -> None:
    receipt = validate_lifecycle_transition(
        StrategyValidationState.EXPERIMENTAL,
        StrategyValidationState.RESEARCH_PASS,
        strategy_id="DS-MOM-001",
        strategy_version="1.0.0",
        requested_at=NOW,
        effective_at=NOW + timedelta(minutes=1),
        actor_type=LifecycleActorType.HUMAN_REVIEWER,
        validation_evidence_ids=("validation-001",),
        run_evidence_ids=("run-001",),
        reason="manual research review",
        policy_version="lifecycle-policy-v1",
    )

    assert receipt.to_json() == type(receipt).from_json(receipt.to_json()).to_json()
    assert receipt.transition_id == type(receipt).from_json(receipt.to_json()).transition_id
    with pytest.raises(FrozenInstanceError):
        receipt.reason = "mutated"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("current", "target"),
    tuple(product(StrategyValidationState, repeat=2)),
)
def test_lifecycle_transition_matrix_matches_declared_graph(
    current: StrategyValidationState,
    target: StrategyValidationState,
) -> None:
    kwargs = {
        "strategy_id": "MATRIX-FIXTURE",
        "strategy_version": "1.0.0",
        "requested_at": NOW,
        "effective_at": NOW,
        "actor_type": LifecycleActorType.HUMAN_REVIEWER,
        "validation_evidence_ids": ("validation-matrix",),
        "run_evidence_ids": ("run-matrix",),
        "reason": "exhaustive transition matrix",
        "policy_version": "lifecycle-policy-v1",
    }
    if target in ALLOWED_LIFECYCLE_TRANSITIONS[current]:
        receipt = validate_lifecycle_transition(
            current,
            target,
            **kwargs,  # type: ignore[arg-type]
        )
        assert receipt.from_state is current
        assert receipt.to_state is target
    else:
        with pytest.raises(ValueError, match="invalid lifecycle transition"):
            validate_lifecycle_transition(
                current,
                target,
                **kwargs,  # type: ignore[arg-type]
            )


@pytest.mark.parametrize(("current", "target"), LIFECYCLE_PROMOTION_EDGES)
def test_every_lifecycle_promotion_edge_rejects_automation(
    current: StrategyValidationState,
    target: StrategyValidationState,
) -> None:
    with pytest.raises(ValueError, match="automated lifecycle promotion"):
        validate_lifecycle_transition(
            current,
            target,
            strategy_id="MATRIX-FIXTURE",
            strategy_version="1.0.0",
            requested_at=NOW,
            effective_at=NOW,
            actor_type=LifecycleActorType.AUTOMATED_SYSTEM,
            validation_evidence_ids=("validation-matrix",),
            run_evidence_ids=("run-matrix",),
            reason="automated transition must reject",
            policy_version="lifecycle-policy-v1",
        )


def test_lifecycle_transition_rejects_effective_time_before_request() -> None:
    with pytest.raises(ValueError, match="cannot precede requested_at"):
        validate_lifecycle_transition(
            StrategyValidationState.PRODUCTION_ELIGIBLE,
            StrategyValidationState.DISABLED,
            strategy_id="MATRIX-FIXTURE",
            strategy_version="1.0.0",
            requested_at=NOW,
            effective_at=NOW - timedelta(microseconds=1),
            actor_type=LifecycleActorType.AUTOMATED_SYSTEM,
            validation_evidence_ids=(),
            run_evidence_ids=(),
            reason="safety disable",
            policy_version="lifecycle-policy-v1",
        )


def test_lifecycle_transition_rejects_skips_auto_promotion_and_missing_evidence() -> None:
    common = {
        "strategy_id": "DS-MOM-001",
        "strategy_version": "1.0.0",
        "requested_at": NOW,
        "effective_at": NOW,
        "validation_evidence_ids": ("validation-001",),
        "run_evidence_ids": ("run-001",),
        "reason": "controlled transition fixture",
        "policy_version": "lifecycle-policy-v1",
    }
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        validate_lifecycle_transition(
            StrategyValidationState.EXPERIMENTAL,
            StrategyValidationState.PRODUCTION_ELIGIBLE,
            actor_type=LifecycleActorType.HUMAN_REVIEWER,
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="automated lifecycle promotion"):
        validate_lifecycle_transition(
            StrategyValidationState.EXPERIMENTAL,
            StrategyValidationState.RESEARCH_PASS,
            actor_type=LifecycleActorType.AUTOMATED_SYSTEM,
            **common,  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="requires validation and run evidence"):
        validate_lifecycle_transition(
            StrategyValidationState.EXPERIMENTAL,
            StrategyValidationState.RESEARCH_PASS,
            actor_type=LifecycleActorType.HUMAN_REVIEWER,
            **{**common, "run_evidence_ids": ()},  # type: ignore[arg-type]
        )


def test_lifecycle_contract_itself_rejects_content_bound_invalid_payloads() -> None:
    valid = validate_lifecycle_transition(
        StrategyValidationState.EXPERIMENTAL,
        StrategyValidationState.RESEARCH_PASS,
        strategy_id="DS-MOM-001",
        strategy_version="1.0.0",
        requested_at=NOW,
        effective_at=NOW,
        actor_type=LifecycleActorType.HUMAN_REVIEWER,
        validation_evidence_ids=("validation-001",),
        run_evidence_ids=("run-001",),
        reason="controlled transition fixture",
        policy_version="lifecycle-policy-v1",
    )
    skipped = {**valid.to_dict(), "to_state": StrategyValidationState.PRODUCTION_ELIGIBLE.value}
    skipped["transition_id"] = stable_identity(
        "lifecycle-transition",
        {key: value for key, value in skipped.items() if key != "transition_id"},
    )
    with pytest.raises(ValueError, match="invalid lifecycle transition"):
        StrategyLifecycleTransition.from_dict(skipped)

    automated = {**valid.to_dict(), "actor_type": LifecycleActorType.AUTOMATED_SYSTEM.value}
    automated["transition_id"] = stable_identity(
        "lifecycle-transition",
        {key: value for key, value in automated.items() if key != "transition_id"},
    )
    with pytest.raises(ValueError, match="automated lifecycle promotion"):
        StrategyLifecycleTransition.from_dict(automated)

    missing_evidence = {**valid.to_dict(), "run_evidence_ids": []}
    missing_evidence["transition_id"] = stable_identity(
        "lifecycle-transition",
        {
            key: value
            for key, value in missing_evidence.items()
            if key != "transition_id"
        },
    )
    with pytest.raises(ValueError, match="requires validation and run evidence"):
        StrategyLifecycleTransition.from_dict(missing_evidence)


def test_non_promotion_disable_transition_may_be_automated_but_never_promotes() -> None:
    receipt = validate_lifecycle_transition(
        StrategyValidationState.PRODUCTION_ELIGIBLE,
        StrategyValidationState.DISABLED,
        strategy_id="SYNTHETIC-FIXTURE",
        strategy_version="1.0.0",
        requested_at=NOW,
        effective_at=NOW,
        actor_type=LifecycleActorType.AUTOMATED_SYSTEM,
        validation_evidence_ids=(),
        run_evidence_ids=(),
        reason="safety disable",
        policy_version="lifecycle-policy-v1",
    )
    assert receipt.to_state is StrategyValidationState.DISABLED
    assert receipt.actor_type is LifecycleActorType.AUTOMATED_SYSTEM
    assert receipt.schema_version.endswith(".v1")
    assert UniverseMembershipStatus.INCLUDED.value == "included"

    with pytest.raises(ValueError, match="automated lifecycle promotion"):
        validate_lifecycle_transition(
            StrategyValidationState.DISABLED,
            StrategyValidationState.EXPERIMENTAL,
            strategy_id="SYNTHETIC-FIXTURE",
            strategy_version="1.0.0",
            requested_at=NOW,
            effective_at=NOW,
            actor_type=LifecycleActorType.AUTOMATED_SYSTEM,
            validation_evidence_ids=("validation-001",),
            run_evidence_ids=("run-001",),
            reason="automated re-enable must reject",
            policy_version="lifecycle-policy-v1",
        )


def test_risk_numeric_contract_has_explicit_units_status_lineage_and_identity() -> None:
    observed = _risk_numeric(RiskMetric.ENTRY_PRICE, Decimal("100"))

    restored = RiskNumericEvidence.from_json(observed.to_json())

    assert restored == observed
    assert restored.unit is RiskUnit.USD_PER_SHARE
    assert restored.status is RiskValueStatus.OBSERVED
    assert restored.evidence_kind is EvidenceKind.EMPIRICAL
    assert restored.reason == "synthetic point-in-time fixture"
    with pytest.raises(FrozenInstanceError):
        observed.value = Decimal("101")  # type: ignore[misc]


def test_risk_numeric_contract_rejects_status_unit_value_and_source_lies() -> None:
    observed = _risk_numeric(RiskMetric.QUANTITY, Decimal("100"))
    wrong_unit = {**observed.to_dict(), "unit": RiskUnit.USD.value}
    with pytest.raises(ValueError, match="requires unit shares"):
        RiskNumericEvidence.from_dict(wrong_unit)

    with pytest.raises(ValueError, match="available capability cannot have unavailable"):
        _risk_numeric(
            RiskMetric.QUANTITY,
            Decimal("100"),
            status=RiskValueStatus.UNAVAILABLE,
        )
    with pytest.raises(ValueError, match="requires a value"):
        _risk_numeric(
            RiskMetric.QUANTITY,
            None,
            status=RiskValueStatus.OBSERVED,
            capability_state=CapabilityState.AVAILABLE,
        )
    with pytest.raises(ValueError, match="must be heuristic"):
        _risk_numeric(
            RiskMetric.SPREAD_BPS,
            Decimal("10"),
            status=RiskValueStatus.PROVISIONAL,
            evidence_kind=EvidenceKind.EMPIRICAL,
        )
    with pytest.raises(ValueError, match="private or secret"):
        _risk_numeric(
            RiskMetric.SPREAD_BPS,
            Decimal("10"),
            source_identity="api_key=credential-value",
        )


@pytest.mark.parametrize("value", (Decimal("0"), Decimal("-1"), Decimal("1.5")))
def test_available_quantity_requires_positive_integral_shares(value: Decimal) -> None:
    with pytest.raises(ValueError, match="quantity must be (positive|integral)"):
        _risk_numeric(RiskMetric.QUANTITY, value)


@pytest.mark.parametrize("value", (Decimal("NaN"), Decimal("Infinity")))
def test_numeric_risk_evidence_rejects_nonfinite_values(value: Decimal) -> None:
    with pytest.raises(ValueError, match="must be finite"):
        _risk_numeric(RiskMetric.SPREAD_BPS, value)


def test_execution_risk_hand_calculations_are_exact_and_round_trip() -> None:
    receipt = _execution_risk()

    assert receipt.metric(RiskMetric.STOP_DISTANCE).value == Decimal("2")
    assert receipt.metric(RiskMetric.GROSS_REWARD).value == Decimal("6")
    assert receipt.metric(RiskMetric.GROSS_REWARD_RISK).value == Decimal("3")
    assert receipt.metric(RiskMetric.PER_SHARE_COST).value == Decimal("0.22")
    assert receipt.metric(RiskMetric.TOTAL_ROUND_TRIP_COST).value == Decimal("22.00")
    assert receipt.metric(RiskMetric.PLANNED_LOSS).value == Decimal("222.00")
    assert receipt.metric(RiskMetric.AFTER_COST_REWARD_RISK).value == (
        Decimal("578") / Decimal("222")
    )
    assert receipt.metric(RiskMetric.RISK_CAP).value == Decimal("222.00000")
    assert receipt.vetoes == ()
    assert ExecutionRiskEvidence.from_json(receipt.to_json()) == receipt
    assert _execution_risk().to_json() == receipt.to_json()


def test_provisional_input_propagates_heuristic_derived_lineage() -> None:
    base = list(_base_risk_metrics())
    index = next(
        index
        for index, item in enumerate(base)
        if item.metric is RiskMetric.ENTRY_SLIPPAGE_BPS
    )
    base[index] = _risk_numeric(
        RiskMetric.ENTRY_SLIPPAGE_BPS,
        Decimal("5"),
        status=RiskValueStatus.PROVISIONAL,
        evidence_kind=EvidenceKind.HEURISTIC,
        reason="bounded unvalidated slippage assumption",
    )

    receipt = _execution_risk(base_metrics=tuple(base))

    for metric in (
        RiskMetric.PER_SHARE_COST,
        RiskMetric.TOTAL_ROUND_TRIP_COST,
        RiskMetric.PLANNED_LOSS,
        RiskMetric.AFTER_COST_REWARD_RISK,
    ):
        evidence = receipt.metric(metric)
        assert evidence.status is RiskValueStatus.DERIVED
        assert evidence.evidence_kind is EvidenceKind.HEURISTIC
        assert (
            base[index].evidence_id in evidence.input_evidence_ids
            or metric is not RiskMetric.PER_SHARE_COST
        )


def test_missing_inputs_return_null_derived_values_and_fail_closed_vetoes() -> None:
    metrics = _base_risk_metrics(
        quantity=None,
        account_equity=None,
        risk_fraction=None,
        concentration=None,
        unavailable_reason="sizing and account policy unavailable",
    )

    receipt = _execution_risk(base_metrics=metrics)

    assert receipt.metric(RiskMetric.QUANTITY).value is None
    assert receipt.metric(RiskMetric.TOTAL_ROUND_TRIP_COST).value is None
    assert receipt.metric(RiskMetric.PLANNED_LOSS).value is None
    assert receipt.metric(RiskMetric.AFTER_COST_REWARD_RISK).value is None
    assert receipt.metric(RiskMetric.RISK_CAP).value is None
    assert "quantity_unavailable" in receipt.vetoes
    assert "account_equity_unavailable" in receipt.vetoes
    assert "risk_fraction_unavailable" in receipt.vetoes
    assert "planned_loss_unavailable" in receipt.vetoes
    assert "concentration_unavailable" in receipt.vetoes
    assert all(
        item.value is None and item.reason
        for item in receipt.metrics
        if item.status is RiskValueStatus.UNAVAILABLE
    )


@pytest.mark.parametrize(
    ("evaluation", "expected_veto"),
    (
        (_evaluation(entry=Decimal("100"), stop=Decimal("101")), "long_geometry_invalid"),
        (
            _evaluation(
                direction=StrategyDirection.SHORT,
                entry=Decimal("100"),
                stop=Decimal("98"),
                target=Decimal("94"),
            ),
            "short_geometry_invalid",
        ),
        (_evaluation(direction=StrategyDirection.BOTH), "direction_unknown"),
    ),
)
def test_invalid_directional_geometry_fails_closed(
    evaluation: StrategyEvaluation,
    expected_veto: str,
) -> None:
    receipt = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(
            entry=evaluation.entry_price,
            stop=evaluation.invalidation_price,
            target=evaluation.target_price,
        ),
    )

    assert expected_veto in receipt.vetoes


def test_staleness_halt_action_caps_concentration_and_minimum_r_fail_closed() -> None:
    stale = _execution_risk(base_metrics=_base_risk_metrics(quote_age=Decimal("6")))
    assert "stale_quote" in stale.vetoes

    unknown = _execution_risk(
        halt_status=SafetyStatus.UNKNOWN,
        action_status=SafetyStatus.UNKNOWN,
    )
    assert "halt_status_unknown" in unknown.vetoes
    assert "corporate_action_status_unknown" in unknown.vetoes

    over_cap = _execution_risk(
        base_metrics=_base_risk_metrics(
            risk_fraction=Decimal("0.00221"),
            concentration=Decimal("0.26"),
            minimum_r=Decimal("2.7"),
        )
    )
    assert "planned_loss_cap_breached" in over_cap.vetoes
    assert "concentration_cap_breached" in over_cap.vetoes
    assert "after_cost_reward_risk_below_minimum" in over_cap.vetoes


def test_cap_and_threshold_boundaries_are_inclusive() -> None:
    receipt = _execution_risk(
        base_metrics=_base_risk_metrics(
            risk_fraction=Decimal("0.00222"),
            concentration=Decimal("0.25"),
            minimum_r=Decimal("578") / Decimal("222"),
            quote_age=Decimal("5"),
        )
    )

    assert receipt.vetoes == ()


def test_quote_scope_requires_causal_quote_and_nbbo_capabilities() -> None:
    iex = _capability()
    with pytest.raises(ValueError, match="NBBO scope"):
        _execution_risk(capability=iex, quote_scope=QuoteEvidenceScope.NBBO)

    sip = _capability(
        provider="fixture-sip-provider",
        feed="sip",
        consolidated_nbbo=CapabilityState.AVAILABLE,
    )
    nbbo = _execution_risk(capability=sip, quote_scope=QuoteEvidenceScope.NBBO)
    assert nbbo.vetoes == ()

    unavailable_quotes = _capability(
        feed="bounded-bars",
        bars=CapabilityState.AVAILABLE,
        consolidated_nbbo=CapabilityState.UNAVAILABLE,
    )
    unavailable_payload = unavailable_quotes.to_dict()
    unavailable_payload["quotes"] = CapabilityState.UNAVAILABLE.value
    unavailable_payload["capability_receipt_id"] = stable_identity(
        "provider-capability",
        {
            key: value
            for key, value in unavailable_payload.items()
            if key != "capability_receipt_id"
        },
    )
    unavailable_quotes = ProviderCapabilityReceipt.from_dict(unavailable_payload)
    with pytest.raises(ValueError, match="available quote capability"):
        _execution_risk(capability=unavailable_quotes)


def test_provisional_and_unavailable_quote_truth_remain_visible_and_vetoed() -> None:
    provisional = _execution_risk(
        base_metrics=_base_risk_metrics(spread_status=RiskValueStatus.PROVISIONAL),
        quote_scope=QuoteEvidenceScope.PROVISIONAL,
    )
    assert "observed_quote_unavailable" in provisional.vetoes
    assert provisional.metric(RiskMetric.SPREAD_BPS).status is RiskValueStatus.PROVISIONAL

    unavailable_metrics = list(_base_risk_metrics())
    spread_index = next(
        index
        for index, item in enumerate(unavailable_metrics)
        if item.metric is RiskMetric.SPREAD_BPS
    )
    unavailable_metrics[spread_index] = _risk_numeric(
        RiskMetric.SPREAD_BPS,
        None,
        status=RiskValueStatus.UNAVAILABLE,
        reason="quote feed unsupported at decision time",
    )
    unavailable = _execution_risk(
        base_metrics=tuple(unavailable_metrics),
        quote_scope=QuoteEvidenceScope.UNAVAILABLE,
    )
    assert "spread_unavailable" in unavailable.vetoes
    assert "quote_capability_unavailable" in unavailable.vetoes


def test_future_risk_evidence_and_mismatched_evaluation_prices_reject() -> None:
    future = list(_base_risk_metrics())
    future[0] = _risk_numeric(
        future[0].metric,
        future[0].value,
        observed_at=NOW + timedelta(microseconds=1),
    )
    with pytest.raises(ValueError, match="after decision_at"):
        _execution_risk(base_metrics=tuple(future))

    with pytest.raises(ValueError, match="entry_price does not match"):
        _execution_risk(base_metrics=_base_risk_metrics(entry=Decimal("99")))


def test_direct_and_deserialized_execution_contract_recheck_formula_and_vetoes() -> None:
    receipt = _execution_risk()
    payload = receipt.to_dict()
    planned_loss_index = next(
        index
        for index, item in enumerate(receipt.metrics)
        if item.metric is RiskMetric.PLANNED_LOSS
    )
    planned_loss = dict(payload["metrics"][planned_loss_index])
    planned_loss["value"] = "221"
    planned_loss["evidence_id"] = stable_identity(
        "risk-numeric",
        {key: value for key, value in planned_loss.items() if key != "evidence_id"},
    )
    payload["metrics"][planned_loss_index] = planned_loss
    payload["execution_risk_evidence_id"] = stable_identity(
        "execution-risk",
        {
            key: value
            for key, value in payload.items()
            if key != "execution_risk_evidence_id"
        },
    )
    with pytest.raises(ValueError, match="derived planned_loss"):
        ExecutionRiskEvidence.from_dict(payload)

    veto_payload = receipt.to_dict()
    veto_payload["vetoes"] = ["fabricated_veto"]
    with pytest.raises(ValueError, match="vetoes do not match"):
        ExecutionRiskEvidence.from_dict(veto_payload)


def test_execution_risk_rejects_private_identity_and_has_no_side_effect_boundary() -> None:
    receipt = _execution_risk()
    assert receipt.research_only is True
    assert receipt.schema_version == "v2.opportunity.execution_risk_evidence.v1"

    with pytest.raises(ValueError, match="private or secret"):
        capability = _capability()
        build_execution_risk_evidence(
            evaluation=_evaluation(),
            capability_receipts=(capability,),
            quote_capability_receipt_id=capability.capability_receipt_id,
            quote_scope=QuoteEvidenceScope.NONCONSOLIDATED,
            halt_evidence=_safety_evidence(RiskSafetyCategory.HALT, capability),
            corporate_action_evidence=_safety_evidence(
                RiskSafetyCategory.CORPORATE_ACTION,
                capability,
            ),
            account_identity="C:/Users/operator/private/account.json",
            risk_cap_identity="fixed-fractional-policy-v1",
            concentration_identity="aggregate-book-fixture",
            base_metrics=_base_risk_metrics(),
        )


@pytest.mark.parametrize(
    "capability_state",
    (
        CapabilityState.UNAVAILABLE,
        CapabilityState.UNSUPPORTED,
        CapabilityState.UNKNOWN,
    ),
)
def test_numeric_missing_capability_states_remain_null_and_explicit(
    capability_state: CapabilityState,
) -> None:
    evidence = _risk_numeric(
        RiskMetric.SPREAD_BPS,
        None,
        status=RiskValueStatus.UNAVAILABLE,
        capability_state=capability_state,
        reason=f"spread capability {capability_state.value}",
    )

    assert evidence.value is None
    assert evidence.status is RiskValueStatus.UNAVAILABLE
    assert evidence.capability_state is capability_state
    assert RiskNumericEvidence.from_json(evidence.to_json()) == evidence


def test_numeric_capability_and_value_status_cannot_contradict() -> None:
    with pytest.raises(ValueError, match="non-available capability"):
        _risk_numeric(
            RiskMetric.SPREAD_BPS,
            Decimal("10"),
            capability_state=CapabilityState.UNKNOWN,
        )
    with pytest.raises(ValueError, match="available capability cannot have unavailable"):
        _risk_numeric(
            RiskMetric.SPREAD_BPS,
            None,
            status=RiskValueStatus.UNAVAILABLE,
            capability_state=CapabilityState.AVAILABLE,
        )

    available_without_reason = build_risk_numeric_evidence(
        metric=RiskMetric.SPREAD_BPS,
        value=Decimal("10"),
        status=RiskValueStatus.OBSERVED,
        capability_state=CapabilityState.AVAILABLE,
        evidence_kind=EvidenceKind.EMPIRICAL,
        observed_at=NOW,
        source_identity="bounded-risk-fixture",
        method="direct fixture observation",
        reason=None,
    )
    assert available_without_reason.reason is None


def test_derived_missing_values_preserve_strongest_capability_state() -> None:
    metrics = list(_base_risk_metrics())
    spread_index = next(
        index
        for index, item in enumerate(metrics)
        if item.metric is RiskMetric.SPREAD_BPS
    )
    quantity_index = next(
        index
        for index, item in enumerate(metrics)
        if item.metric is RiskMetric.QUANTITY
    )
    metrics[spread_index] = _risk_numeric(
        RiskMetric.SPREAD_BPS,
        None,
        status=RiskValueStatus.UNAVAILABLE,
        capability_state=CapabilityState.UNSUPPORTED,
        reason="spread is unsupported",
    )
    metrics[quantity_index] = _risk_numeric(
        RiskMetric.QUANTITY,
        None,
        status=RiskValueStatus.UNAVAILABLE,
        capability_state=CapabilityState.UNKNOWN,
        reason="sizing state is unknown",
    )

    receipt = _execution_risk(
        base_metrics=tuple(metrics),
        quote_scope=QuoteEvidenceScope.UNAVAILABLE,
    )

    assert receipt.metric(RiskMetric.PER_SHARE_COST).capability_state is (
        CapabilityState.UNSUPPORTED
    )
    assert receipt.metric(RiskMetric.PLANNED_LOSS).capability_state is (
        CapabilityState.UNSUPPORTED
    )


def test_quote_age_is_derived_from_spread_timestamp_not_caller_supplied() -> None:
    receipt = _execution_risk(
        base_metrics=_base_risk_metrics(quote_age=Decimal("3.000001"))
    )

    quote_age = receipt.metric(RiskMetric.QUOTE_AGE)
    spread = receipt.metric(RiskMetric.SPREAD_BPS)
    assert quote_age.value == Decimal("3.000001")
    assert quote_age.status is RiskValueStatus.DERIVED
    assert quote_age.input_evidence_ids == (spread.evidence_id,)
    assert quote_age.observed_at == receipt.decision_at

    payload = receipt.to_dict()
    quote_age_index = next(
        index
        for index, item in enumerate(receipt.metrics)
        if item.metric is RiskMetric.QUOTE_AGE
    )
    tampered = dict(payload["metrics"][quote_age_index])
    tampered["value"] = "2"
    tampered["evidence_id"] = stable_identity(
        "risk-numeric",
        {key: value for key, value in tampered.items() if key != "evidence_id"},
    )
    payload["metrics"][quote_age_index] = tampered
    payload["execution_risk_evidence_id"] = stable_identity(
        "execution-risk",
        {
            key: value
            for key, value in payload.items()
            if key != "execution_risk_evidence_id"
        },
    )
    with pytest.raises(ValueError, match="derived quote_age"):
        ExecutionRiskEvidence.from_dict(payload)


def test_nonconsolidated_spread_is_observed_but_cannot_satisfy_nbbo_proof() -> None:
    receipt = _execution_risk(
        capability=_capability(),
        quote_scope=QuoteEvidenceScope.NONCONSOLIDATED,
    )

    assert receipt.metric(RiskMetric.SPREAD_BPS).status is RiskValueStatus.OBSERVED
    assert "consolidated_nbbo_unavailable" in receipt.vetoes


@pytest.mark.parametrize(
    "quote_scope",
    (QuoteEvidenceScope.PROVISIONAL, QuoteEvidenceScope.UNAVAILABLE),
)
def test_nonobserved_quote_scope_rejects_unrelated_capability_receipt_identity(
    quote_scope: QuoteEvidenceScope,
) -> None:
    capability = _capability()
    metrics = list(_base_risk_metrics(spread_status=RiskValueStatus.PROVISIONAL))
    if quote_scope is QuoteEvidenceScope.UNAVAILABLE:
        spread_index = next(
            index
            for index, item in enumerate(metrics)
            if item.metric is RiskMetric.SPREAD_BPS
        )
        metrics[spread_index] = _risk_numeric(
            RiskMetric.SPREAD_BPS,
            None,
            status=RiskValueStatus.UNAVAILABLE,
            capability_state=CapabilityState.UNAVAILABLE,
            reason="quote unavailable",
        )
    with pytest.raises(ValueError, match="cannot claim a receipt"):
        build_execution_risk_evidence(
            evaluation=_evaluation(),
            capability_receipts=(capability,),
            quote_capability_receipt_id=capability.capability_receipt_id,
            quote_scope=quote_scope,
            halt_evidence=_safety_evidence(RiskSafetyCategory.HALT, capability),
            corporate_action_evidence=_safety_evidence(
                RiskSafetyCategory.CORPORATE_ACTION,
                capability,
            ),
            account_identity="paper-account-fixture",
            risk_cap_identity="fixed-fractional-policy-v1",
            concentration_identity="aggregate-book-fixture",
            base_metrics=tuple(metrics),
        )


def test_safety_evidence_is_causal_symbol_bound_and_capability_bound() -> None:
    capability = _capability(
        provider="fixture-sip-provider",
        feed="sip",
        consolidated_nbbo=CapabilityState.AVAILABLE,
    )
    future_halt = _safety_evidence(
        RiskSafetyCategory.HALT,
        capability,
        observed_at=NOW + timedelta(microseconds=1),
    )
    with pytest.raises(ValueError, match="halt safety evidence cannot be observed after"):
        _execution_risk(capability=capability, halt_evidence=future_halt)

    wrong_symbol_payload = _safety_evidence(
        RiskSafetyCategory.HALT,
        capability,
    ).to_dict()
    wrong_symbol_payload["symbol"] = "OTHER"
    wrong_symbol_payload["safety_evidence_id"] = stable_identity(
        "risk-safety",
        {
            key: value
            for key, value in wrong_symbol_payload.items()
            if key != "safety_evidence_id"
        },
    )
    wrong_symbol = RiskSafetyEvidence.from_dict(wrong_symbol_payload)
    with pytest.raises(ValueError, match="halt safety evidence symbol mismatch"):
        _execution_risk(capability=capability, halt_evidence=wrong_symbol)

    unsupported_payload = capability.to_dict()
    unsupported_payload["halts"] = CapabilityState.UNSUPPORTED.value
    unsupported_payload["capability_receipt_id"] = stable_identity(
        "provider-capability",
        {
            key: value
            for key, value in unsupported_payload.items()
            if key != "capability_receipt_id"
        },
    )
    unsupported = ProviderCapabilityReceipt.from_dict(unsupported_payload)
    unsupported_clear = _safety_evidence(RiskSafetyCategory.HALT, unsupported)
    with pytest.raises(ValueError, match="halt status requires available capability"):
        _execution_risk(capability=unsupported, halt_evidence=unsupported_clear)


def test_from_json_rechecks_future_safety_timestamp_after_identity_rebinding() -> None:
    receipt = _execution_risk()
    payload = receipt.to_dict()
    halt = dict(payload["halt_evidence"])
    halt["observed_at"] = (NOW + timedelta(seconds=1)).isoformat()
    halt["safety_evidence_id"] = stable_identity(
        "risk-safety",
        {key: value for key, value in halt.items() if key != "safety_evidence_id"},
    )
    payload["halt_evidence"] = halt
    payload["execution_risk_evidence_id"] = stable_identity(
        "execution-risk",
        {
            key: value
            for key, value in payload.items()
            if key != "execution_risk_evidence_id"
        },
    )
    with pytest.raises(ValueError, match="halt safety evidence cannot be observed after"):
        ExecutionRiskEvidence.from_dict(payload)


@pytest.mark.parametrize(
    ("identity_field", "mismatch"),
    (
        ("account_identity", "other-paper-account"),
        ("risk_cap_identity", "other-risk-policy"),
        ("concentration_identity", "other-concentration-book"),
    ),
)
def test_account_risk_and_concentration_sources_must_match_bound_identities(
    identity_field: str,
    mismatch: str,
) -> None:
    with pytest.raises(ValueError, match="source does not match bound identity"):
        _execution_risk(**{identity_field: mismatch})  # type: ignore[arg-type]


def test_numeric_contract_rejects_fake_base_and_derived_provenance_roles() -> None:
    base = _risk_numeric(
        RiskMetric.SPREAD_BPS,
        None,
        status=RiskValueStatus.UNAVAILABLE,
        capability_state=CapabilityState.UNAVAILABLE,
        reason="quote unavailable",
    ).to_dict()
    base["input_evidence_ids"] = ["fake-derived-input"]
    base["evidence_id"] = stable_identity(
        "risk-numeric",
        {key: value for key, value in base.items() if key != "evidence_id"},
    )
    with pytest.raises(ValueError, match="non-derived metric cannot claim"):
        RiskNumericEvidence.from_dict(base)

    receipt = _execution_risk()
    derived = receipt.metric(RiskMetric.QUOTE_AGE).to_dict()
    derived["status"] = RiskValueStatus.OBSERVED.value
    derived["evidence_id"] = stable_identity(
        "risk-numeric",
        {key: value for key, value in derived.items() if key != "evidence_id"},
    )
    with pytest.raises(ValueError, match="derived metric cannot claim observed"):
        RiskNumericEvidence.from_dict(derived)

    missing = _execution_risk(
        base_metrics=_base_risk_metrics(quantity=None),
    ).metric(RiskMetric.PLANNED_LOSS).to_dict()
    missing["input_evidence_ids"] = []
    missing["evidence_id"] = stable_identity(
        "risk-numeric",
        {key: value for key, value in missing.items() if key != "evidence_id"},
    )
    with pytest.raises(ValueError, match="requires causal input evidence"):
        RiskNumericEvidence.from_dict(missing)


def test_both_direction_veto_is_retained_when_geometry_is_also_missing() -> None:
    evaluation = _evaluation(
        direction=StrategyDirection.BOTH,
        entry=None,
        stop=None,
        target=None,
    )
    receipt = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(entry=None, stop=None, target=None),
    )

    assert "direction_unknown" in receipt.vetoes
    assert "entry_stop_target_unavailable" in receipt.vetoes


def test_synthetic_production_take_requires_full_risk_and_empirical_truth() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)

    decision = _gate_one(evaluation, risk)

    assert decision.decision is TradeDecisionValue.TAKE
    assert decision.risk_evidence_id == risk.execution_risk_evidence_id
    assert decision.risk_evidence_content_hash == risk.content_hash()
    assert decision.evaluation_id == evaluation.evaluation_id
    assert decision.evaluation_content_hash == evaluation.content_hash()
    assert decision.vetoes == ()
    assert evaluation.after_cost_reward_risk == Decimal("-99")
    assert "bounded synthetic execution-risk fixture" in decision.limitations
    assert TradeDecision.from_json(decision.to_json()) == decision
    assert DecisionRunContext.from_json(decision.decision_context.to_json()) == (
        decision.decision_context
    )


def test_after_cost_boundary_is_inclusive_and_policy_identity_must_match() -> None:
    evaluation = _empirical_evaluation()
    initial = _execution_risk(evaluation=evaluation)
    exact_after_cost = initial.metric(RiskMetric.AFTER_COST_REWARD_RISK).value
    assert exact_after_cost is not None
    risk = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(minimum_r=exact_after_cost),
    )
    config = QualityGateConfig(minimum_after_cost_reward_risk=exact_after_cost)

    assert _gate_one(evaluation, risk, config=config).decision is TradeDecisionValue.TAKE

    with pytest.raises(ValueError, match="minimum after-cost R does not match"):
        _gate_one(evaluation, risk, config=GATE_CONFIG)


def test_experimental_watch_retains_provisional_and_non_nbbo_execution_truth() -> None:
    evaluation = replace(
        _evaluation(),
        lifecycle=StrategyValidationState.EXPERIMENTAL,
    )
    base = list(_base_risk_metrics())
    slippage_index = next(
        index
        for index, item in enumerate(base)
        if item.metric is RiskMetric.ENTRY_SLIPPAGE_BPS
    )
    base[slippage_index] = _risk_numeric(
        RiskMetric.ENTRY_SLIPPAGE_BPS,
        Decimal("5"),
        status=RiskValueStatus.PROVISIONAL,
        evidence_kind=EvidenceKind.HEURISTIC,
        reason="unvalidated research slippage assumption",
    )
    provisional = _execution_risk(evaluation=evaluation, base_metrics=tuple(base))
    watched = _gate_one(evaluation, provisional)
    assert watched.decision is TradeDecisionValue.WATCH
    assert "production_lifecycle" in watched.vetoes
    assert "risk_provisional:entry_slippage_bps" in watched.limitations

    non_nbbo = _execution_risk(
        evaluation=evaluation,
        capability=_capability(),
        quote_scope=QuoteEvidenceScope.NONCONSOLIDATED,
    )
    non_nbbo_watch = _gate_one(evaluation, non_nbbo)
    assert non_nbbo_watch.decision is TradeDecisionValue.WATCH
    assert "consolidated_nbbo_unavailable" in non_nbbo_watch.vetoes


def test_production_missing_risk_is_insufficient_and_rank_one_can_pass() -> None:
    evaluation = _empirical_evaluation()
    missing = _gate_one(evaluation, None)
    assert missing.decision is TradeDecisionValue.INSUFFICIENT_DATA
    assert missing.risk_evidence_id is None
    assert "execution_risk_evidence_unavailable" in missing.limitations

    risk = _execution_risk(evaluation=evaluation)
    passed = _gate_one(
        evaluation,
        risk,
        config=QualityGateConfig(
            minimum_watch_score=Decimal("0.99"),
            minimum_take_score=Decimal("1"),
            minimum_after_cost_reward_risk=Decimal("2.5"),
        ),
    )
    assert passed.ranked is not None and passed.ranked.relative_rank == 1
    assert passed.decision is TradeDecisionValue.PASS


@pytest.mark.parametrize(
    "risk_case",
    (
        "stale",
        "non_nbbo",
        "halt_unknown",
        "action_unknown",
        "cap_breach",
        "concentration_breach",
        "minimum_r",
        "quote_unavailable",
        "quote_provisional",
        "slippage_provisional",
        "halt_blocked",
        "action_blocked",
    ),
)
def test_production_risk_veto_matrix_never_takes(risk_case: str) -> None:
    evaluation = _empirical_evaluation()
    config = GATE_CONFIG
    if risk_case == "stale":
        risk = _execution_risk(
            evaluation=evaluation,
            base_metrics=_base_risk_metrics(quote_age=Decimal("6")),
        )
    elif risk_case == "non_nbbo":
        risk = _execution_risk(
            evaluation=evaluation,
            capability=_capability(),
            quote_scope=QuoteEvidenceScope.NONCONSOLIDATED,
        )
    elif risk_case == "halt_unknown":
        risk = _execution_risk(evaluation=evaluation, halt_status=SafetyStatus.UNKNOWN)
    elif risk_case == "action_unknown":
        risk = _execution_risk(evaluation=evaluation, action_status=SafetyStatus.UNKNOWN)
    elif risk_case == "halt_blocked":
        risk = _execution_risk(evaluation=evaluation, halt_status=SafetyStatus.BLOCKED)
    elif risk_case == "action_blocked":
        risk = _execution_risk(evaluation=evaluation, action_status=SafetyStatus.BLOCKED)
    elif risk_case == "cap_breach":
        risk = _execution_risk(
            evaluation=evaluation,
            base_metrics=_base_risk_metrics(risk_fraction=Decimal("0.00221")),
        )
    elif risk_case == "concentration_breach":
        risk = _execution_risk(
            evaluation=evaluation,
            base_metrics=_base_risk_metrics(concentration=Decimal("0.26")),
        )
    elif risk_case == "minimum_r":
        config = QualityGateConfig(minimum_after_cost_reward_risk=Decimal("2.7"))
        risk = _execution_risk(
            evaluation=evaluation,
            base_metrics=_base_risk_metrics(minimum_r=Decimal("2.7")),
        )
    elif risk_case == "slippage_provisional":
        metrics = list(_base_risk_metrics())
        slippage_index = next(
            index
            for index, item in enumerate(metrics)
            if item.metric is RiskMetric.ENTRY_SLIPPAGE_BPS
        )
        metrics[slippage_index] = _risk_numeric(
            RiskMetric.ENTRY_SLIPPAGE_BPS,
            Decimal("5"),
            status=RiskValueStatus.PROVISIONAL,
            evidence_kind=EvidenceKind.HEURISTIC,
            reason="provisional slippage assumption",
        )
        risk = _execution_risk(evaluation=evaluation, base_metrics=tuple(metrics))
    elif risk_case == "quote_provisional":
        risk = _execution_risk(
            evaluation=evaluation,
            base_metrics=_base_risk_metrics(
                spread_status=RiskValueStatus.PROVISIONAL
            ),
            quote_scope=QuoteEvidenceScope.PROVISIONAL,
        )
    else:
        metrics = list(_base_risk_metrics())
        spread_index = next(
            index
            for index, item in enumerate(metrics)
            if item.metric is RiskMetric.SPREAD_BPS
        )
        metrics[spread_index] = _risk_numeric(
            RiskMetric.SPREAD_BPS,
            None,
            status=RiskValueStatus.UNAVAILABLE,
            capability_state=CapabilityState.UNAVAILABLE,
            reason="quote unavailable",
        )
        risk = _execution_risk(
            evaluation=evaluation,
            base_metrics=tuple(metrics),
            quote_scope=QuoteEvidenceScope.UNAVAILABLE,
        )

    decision = _gate_one(evaluation, risk, config=config)

    expected = (
        TradeDecisionValue.INSUFFICIENT_DATA
        if risk_case == "quote_unavailable"
        else TradeDecisionValue.PASS
    )
    assert decision.decision is expected
    if risk.vetoes:
        assert set(risk.vetoes).issubset(decision.vetoes)
    else:
        assert "execution_risk_empirical" in decision.vetoes


@pytest.mark.parametrize(
    ("lifecycle", "expected"),
    tuple(
        (
            lifecycle,
            TradeDecisionValue.TAKE
            if lifecycle is StrategyValidationState.PRODUCTION_ELIGIBLE
            else TradeDecisionValue.PASS
            if lifecycle
            in {StrategyValidationState.DISABLED, StrategyValidationState.REJECTED}
            else TradeDecisionValue.WATCH,
        )
        for lifecycle in StrategyValidationState
    ),
)
def test_eligible_lifecycle_gate_matrix(
    lifecycle: StrategyValidationState,
    expected: TradeDecisionValue,
) -> None:
    evaluation = _empirical_evaluation(lifecycle=lifecycle)
    risk = _execution_risk(evaluation=evaluation)

    assert _gate_one(evaluation, risk).decision is expected


def test_reconciler_emits_exactly_one_decision_for_every_evaluation_status() -> None:
    eligible = replace(
        _evaluation(),
        evaluation_id="evaluation-eligible",
        lifecycle=StrategyValidationState.EXPERIMENTAL,
    )
    rejected = replace(
        _evaluation(),
        evaluation_id="evaluation-rejected",
        symbol="REJ",
        status=EvaluationStatus.REJECTED,
        lifecycle=StrategyValidationState.REJECTED,
        reasons=("evaluator rejection fixture",),
    )
    insufficient = replace(
        _evaluation(),
        evaluation_id="evaluation-insufficient",
        symbol="INS",
        status=EvaluationStatus.INSUFFICIENT_DATA,
        reasons=("missing feature fixture",),
    )
    disabled = replace(
        _evaluation(),
        evaluation_id="evaluation-disabled",
        symbol="DIS",
        status=EvaluationStatus.DISABLED,
        lifecycle=StrategyValidationState.DISABLED,
        reasons=("disabled fixture",),
    )
    evaluations = (rejected, eligible, disabled, insufficient)
    ranked = rank_opportunities(evaluations)
    risk = _execution_risk(evaluation=eligible)

    decisions = reconcile_trade_decisions(
        evaluations,
        ranked,
        risk_by_evaluation={eligible.evaluation_id: risk},
        config=GATE_CONFIG,
    )

    assert tuple(item.evaluation_id for item in decisions) == tuple(
        item.evaluation_id for item in evaluations
    )
    assert [item.decision for item in decisions] == [
        TradeDecisionValue.PASS,
        TradeDecisionValue.WATCH,
        TradeDecisionValue.PASS,
        TradeDecisionValue.INSUFFICIENT_DATA,
    ]
    assert len({item.decision_id for item in decisions}) == len(evaluations)
    assert len({item.decision_run_id for item in decisions}) == 1
    for decision in decisions:
        if decision.evaluation.status is EvaluationStatus.ELIGIBLE:
            assert decision.ranked_id is not None
            assert decision.non_rankable_reason is None
        else:
            assert decision.ranked_id is None
            assert decision.non_rankable_reason == (
                f"evaluation_status_{decision.evaluation.status.value}"
            )


def test_reconciler_rejects_missing_extra_unknown_and_mismatched_risk_sets() -> None:
    eligible = replace(_evaluation(), evaluation_id="eligible-one")
    second = replace(
        _evaluation(),
        evaluation_id="eligible-two",
        symbol="RISK2",
        strategy_id="DS-MOM-002",
    )
    ranked = rank_opportunities((eligible, second))
    first_risk = _execution_risk(evaluation=eligible)
    with pytest.raises(ValueError, match="exactly for eligible"):
        reconcile_trade_decisions(
            (eligible, second),
            ranked,
            risk_by_evaluation={eligible.evaluation_id: first_risk},
            config=GATE_CONFIG,
        )
    with pytest.raises(ValueError, match="duplicate risk|key does not match"):
        reconcile_trade_decisions(
            (eligible, second),
            ranked,
            risk_by_evaluation={
                eligible.evaluation_id: first_risk,
                second.evaluation_id: first_risk,
            },
            config=GATE_CONFIG,
        )

    rejected = replace(
        second,
        status=EvaluationStatus.REJECTED,
        lifecycle=StrategyValidationState.REJECTED,
    )
    rejected_risk = _execution_risk(evaluation=rejected)
    with pytest.raises(ValueError, match="only eligible"):
        reconcile_trade_decisions(
            (eligible, rejected),
            rank_opportunities((eligible, rejected)),
            risk_by_evaluation={
                eligible.evaluation_id: first_risk,
                rejected.evaluation_id: rejected_risk,
            },
            config=GATE_CONFIG,
        )


def test_reconciler_rejects_rank_identity_position_status_and_version_ambiguity() -> None:
    first = replace(_evaluation(), evaluation_id="eligible-one")
    second = replace(
        _evaluation(),
        evaluation_id="eligible-two",
        symbol="RISK2",
        strategy_id="DS-MOM-002",
    )
    ranked = rank_opportunities((first, second))
    risks = {
        first.evaluation_id: _execution_risk(evaluation=first),
        second.evaluation_id: _execution_risk(evaluation=second),
    }
    with pytest.raises(ValueError, match="duplicate relative rank"):
        reconcile_trade_decisions(
            (first, second),
            (ranked[0], replace(ranked[1], relative_rank=ranked[0].relative_rank)),
            risk_by_evaluation=risks,
            config=GATE_CONFIG,
        )
    with pytest.raises(ValueError, match="unknown evaluation"):
        reconcile_trade_decisions(
            (first, second),
            (replace(ranked[0], evaluation_id="unknown"), ranked[1]),
            risk_by_evaluation=risks,
            config=GATE_CONFIG,
        )

    ambiguous = replace(
        second,
        symbol=first.symbol,
        strategy_id=first.strategy_id,
        strategy_version="2.0.0",
    )
    with pytest.raises(ValueError, match="version ambiguity"):
        reconcile_trade_decisions(
            (first, ambiguous),
            (),
            risk_by_evaluation={},
            config=GATE_CONFIG,
        )


def test_decision_context_and_ids_change_with_risk_or_gate_config() -> None:
    evaluation = _empirical_evaluation()
    ranked = rank_opportunities((evaluation,))
    first_risk = _execution_risk(evaluation=evaluation)
    changed_metrics = list(_base_risk_metrics())
    spread_index = next(
        index
        for index, item in enumerate(changed_metrics)
        if item.metric is RiskMetric.SPREAD_BPS
    )
    changed_metrics[spread_index] = _risk_numeric(
        RiskMetric.SPREAD_BPS,
        Decimal("9"),
        observed_at=NOW - timedelta(seconds=1),
    )
    second_risk = _execution_risk(
        evaluation=evaluation,
        base_metrics=tuple(changed_metrics),
    )
    first = reconcile_trade_decisions(
        (evaluation,),
        ranked,
        risk_by_evaluation={evaluation.evaluation_id: first_risk},
        config=GATE_CONFIG,
    )[0]
    second = reconcile_trade_decisions(
        (evaluation,),
        ranked,
        risk_by_evaluation={evaluation.evaluation_id: second_risk},
        config=GATE_CONFIG,
    )[0]
    assert first.decision_run_id != second.decision_run_id
    assert first.decision_id != second.decision_id

    changed_config = QualityGateConfig(
        minimum_take_score=Decimal("0.71"),
        minimum_after_cost_reward_risk=Decimal("2.5"),
    )
    third = reconcile_trade_decisions(
        (evaluation,),
        ranked,
        risk_by_evaluation={evaluation.evaluation_id: first_risk},
        config=changed_config,
    )[0]
    assert first.decision_run_id != third.decision_run_id
    assert first.decision_id != third.decision_id

    rejected = replace(
        evaluation,
        evaluation_id="ordered-rejected",
        symbol="ORDER",
        status=EvaluationStatus.REJECTED,
        lifecycle=StrategyValidationState.REJECTED,
    )
    forward = build_decision_run_context(
        (evaluation, rejected),
        ranked,
        risk_by_evaluation={evaluation.evaluation_id: first_risk},
        config=GATE_CONFIG,
    )
    reverse = build_decision_run_context(
        (rejected, evaluation),
        ranked,
        risk_by_evaluation={evaluation.evaluation_id: first_risk},
        config=GATE_CONFIG,
    )
    assert forward.decision_run_id != reverse.decision_run_id


def test_trade_decision_direct_and_from_json_recheck_all_bound_content() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    decision = _gate_one(evaluation, risk)

    cases = (
        ("decision_run_id", "opportunity-decision-run:" + "0" * 24, "decision_run_id"),
        ("evaluation_content_hash", "tampered", "metadata does not match"),
        ("risk_evidence_id", "execution-risk:tampered", "risk identity"),
    )
    for field, value, message in cases:
        payload = decision.to_dict()
        payload[field] = value
        payload["decision_id"] = stable_identity(
            "decision",
            {key: item for key, item in payload.items() if key != "decision_id"},
        )
        with pytest.raises(ValueError, match=message):
            TradeDecision.from_dict(payload)

    rank_payload = decision.to_dict()
    rank_payload["ranked"]["symbol"] = "OTHER"
    rank_payload["decision_id"] = stable_identity(
        "decision",
        {key: item for key, item in rank_payload.items() if key != "decision_id"},
    )
    with pytest.raises(ValueError, match="rank does not match"):
        TradeDecision.from_dict(rank_payload)

    assert decision.to_json() == TradeDecision.from_json(decision.to_json()).to_json()


def test_nonrankable_reason_and_take_risk_proof_cannot_be_bypassed() -> None:
    rejected = replace(
        _evaluation(),
        evaluation_id="rejected-direct",
        status=EvaluationStatus.REJECTED,
        lifecycle=StrategyValidationState.REJECTED,
    )
    rejected_decision = reconcile_trade_decisions(
        (rejected,),
        (),
        risk_by_evaluation={},
        config=GATE_CONFIG,
    )[0]
    payload = rejected_decision.to_dict()
    payload["non_rankable_reason"] = None
    payload["decision_id"] = stable_identity(
        "decision",
        {key: value for key, value in payload.items() if key != "decision_id"},
    )
    with pytest.raises(ValueError, match="non_rankable_reason"):
        TradeDecision.from_dict(payload)

    production = _empirical_evaluation()
    missing_risk = _gate_one(production, None)
    take_payload = missing_risk.to_dict()
    take_payload["decision"] = TradeDecisionValue.TAKE.value
    take_payload["decision_id"] = stable_identity(
        "decision",
        {
            key: value
            for key, value in take_payload.items()
            if key != "decision_id"
        },
    )
    with pytest.raises(ValueError, match="TAKE requires ExecutionRiskEvidence"):
        TradeDecision.from_dict(take_payload)


def test_decision_context_rejects_rebound_identity_and_embedded_hash_tampering() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    decision = _gate_one(evaluation, risk)
    context_payload = decision.decision_context.to_dict()
    context_payload["bindings"][0]["evaluation_content_hash"] = "tampered"
    with pytest.raises(ValueError, match="identity does not match content"):
        DecisionRunContext.from_dict(context_payload)

    rebound = dict(context_payload)
    rebound["decision_run_id"] = stable_identity(
        "opportunity-decision-run",
        {
            key: value
            for key, value in rebound.items()
            if key != "decision_run_id"
        },
    )
    rebound_context = DecisionRunContext.from_dict(rebound)
    decision_payload = decision.to_dict()
    decision_payload["decision_run_id"] = rebound_context.decision_run_id
    decision_payload["decision_context"] = rebound_context.to_dict()
    decision_payload["decision_id"] = stable_identity(
        "decision",
        {
            key: value
            for key, value in decision_payload.items()
            if key != "decision_id"
        },
    )
    with pytest.raises(ValueError, match="evaluation binding"):
        TradeDecision.from_dict(decision_payload)


def test_missing_risk_policy_threshold_flows_to_one_insufficient_decision() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(
        evaluation=evaluation,
        base_metrics=_base_risk_metrics(minimum_r=None),
    )
    assert "minimum_after_cost_r_unavailable" in risk.vetoes

    standalone = _gate_one(evaluation, risk)
    assert standalone.decision is TradeDecisionValue.INSUFFICIENT_DATA
    checks = {item.check_id: item for item in standalone.gate_checks}
    assert checks["risk_policy_minimum_available"].passed is None
    assert checks["execution_risk_empirical"].passed is not True

    ranked = rank_opportunities((evaluation,))
    reconciled = reconcile_trade_decisions(
        (evaluation,),
        ranked,
        risk_by_evaluation={evaluation.evaluation_id: risk},
        config=GATE_CONFIG,
    )
    assert len(reconciled) == 1
    assert reconciled[0].evaluation_id == evaluation.evaluation_id
    assert reconciled[0].decision is TradeDecisionValue.INSUFFICIENT_DATA


@pytest.mark.parametrize("mutation", ("omission", "injection", "reorder", "optional"))
def test_take_rejects_forged_canonical_gate_check_sets(mutation: str) -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    decision = _gate_one(evaluation, risk)
    assert decision.decision is TradeDecisionValue.TAKE
    payload = decision.to_dict()
    checks = list(payload["gate_checks"])
    if mutation == "omission":
        checks.pop(3)
    elif mutation == "injection":
        checks.append(
            {
                "check_id": "fabricated_pass",
                "passed": True,
                "mandatory": True,
                "reason": "fabricated",
                "schema_version": "v2.opportunity.gate_check.v1",
            }
        )
    elif mutation == "reorder":
        checks[0], checks[1] = checks[1], checks[0]
    else:
        checks[0]["mandatory"] = False
    payload["gate_checks"] = checks
    payload["decision_id"] = stable_identity(
        "decision",
        {key: value for key, value in payload.items() if key != "decision_id"},
    )

    message = "mandatory" if mutation == "optional" else "canonical schema"
    with pytest.raises(ValueError, match=message):
        TradeDecision.from_dict(payload)


def test_direct_decision_run_context_rejects_empty_binding_set() -> None:
    evaluation = _empirical_evaluation()
    risk = _execution_risk(evaluation=evaluation)
    context = _gate_one(evaluation, risk).decision_context
    payload = context.to_dict()
    payload["bindings"] = []
    payload["decision_run_id"] = stable_identity(
        "opportunity-decision-run",
        {
            key: value
            for key, value in payload.items()
            if key != "decision_run_id"
        },
    )

    with pytest.raises(ValueError, match="at least one binding"):
        DecisionRunContext.from_dict(payload)


def test_noneligible_risk_is_rejected_by_builder_and_direct_binding_contract() -> None:
    rejected = replace(
        _evaluation(),
        evaluation_id="rejected-risk-context",
        status=EvaluationStatus.REJECTED,
        lifecycle=StrategyValidationState.REJECTED,
    )
    risk = _execution_risk(evaluation=rejected)
    with pytest.raises(ValueError, match="only eligible"):
        build_decision_run_context(
            (rejected,),
            (),
            risk_by_evaluation={rejected.evaluation_id: risk},
            config=GATE_CONFIG,
        )

    decision = reconcile_trade_decisions(
        (rejected,),
        (),
        risk_by_evaluation={},
        config=GATE_CONFIG,
    )[0]
    binding_payload = decision.decision_context.bindings[0].to_dict()
    binding_payload["risk_evidence_id"] = risk.execution_risk_evidence_id
    binding_payload["risk_evidence_content_hash"] = risk.content_hash()
    with pytest.raises(ValueError, match="noneligible.*cannot carry risk"):
        DecisionRunBinding.from_dict(binding_payload)

    context_payload = decision.decision_context.to_dict()
    context_payload["bindings"][0] = binding_payload
    context_payload["decision_run_id"] = stable_identity(
        "opportunity-decision-run",
        {
            key: value
            for key, value in context_payload.items()
            if key != "decision_run_id"
        },
    )
    with pytest.raises(ValueError, match="noneligible.*cannot carry risk"):
        DecisionRunContext.from_dict(context_payload)
