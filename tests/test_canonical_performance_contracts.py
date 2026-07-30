from intraday_scanner.notifiers.telegram_formatter import format_canonical_daily_performance
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


def test_canonical_telegram_formatter_uses_public_daily_fields() -> None:
    message = format_canonical_daily_performance(
        {
            "performance_id": "2026-07-29:official_forward_paper:alphaops_v4:v1",
            "market_date": "2026-07-29",
            "timezone": "America/Chicago",
            "cohort": "official_forward_paper",
            "return_pct": 1.25,
            "cumulative_return_pct": 2.5,
            "benchmark_return_pct": 0.5,
            "excess_return_pct": 0.75,
            "net_pnl_cents": 125,
            "drawdown_pct": -0.4,
            "return_basis": "net_after_costs",
            "cost_status": "complete",
            "coverage": {"coverage_pct": 100.0},
            "evidence_state": "complete",
            "input_hash_sha256": "abcdef1234567890",
            "source_refs": ["source-1"],
            "generated_at": "2026-07-29T21:00:00+00:00",
        }
    )
    assert "Cohort: Official paper" in message
    assert "Daily: +1.25% | Cumulative: +2.50%" in message
    assert "Benchmark: +0.50% | Excess: +0.75%" in message
    assert "Net P&L: +$1.25" in message
    assert "Coverage: 100.0%" in message
