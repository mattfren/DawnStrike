from intraday_scanner.performance.contracts import (
    Cohort,
    EvidenceState,
    PerformanceCohort,
    ReturnMethodology,
    normalize_cohort,
)


def test_directive_cohort_and_evidence_contracts_are_typed() -> None:
    assert PerformanceCohort is Cohort
    assert Cohort.ALPHAOPS_SIGNAL_RESEARCH.value == "alphaops_signal_research"
    assert (
        normalize_cohort("alphaops_research", default=Cohort.OFFICIAL_FORWARD_PAPER)
        is Cohort.ALPHAOPS_SIGNAL_RESEARCH
    )
    assert EvidenceState.MISSING.value == "missing"
    methodology = ReturnMethodology(
        calculation_version="v1",
        execution_policy_version="policy-v1",
        portfolio_return_basis="opening_equity",
        price_basis="source_close",
        fee_policy="explicit_only",
        slippage_policy="explicit_only",
        benchmark_policy="same_market_date",
    )
    assert methodology.timezone == "America/Chicago"
