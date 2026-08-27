"""Canonical, source-linked performance truth for Dawnstrike."""

from intraday_scanner.performance.contracts import (
    Cohort,
    EvidenceState,
    PerformanceCohort,
    PerformanceRow,
    ReturnMethodology,
)
from intraday_scanner.performance.service import CanonicalPerformanceService
from intraday_scanner.performance.strategy_miss_attribution import (
    AttributionState,
    Eligibility,
    StrategyMissAttributionReport,
    StrategyMissAttributionRow,
    StrategyMissAttributionSummary,
    attribute_strategy_misses,
    from_portfolio_rows,
    load_portfolio_performance_rows_readonly,
    load_strategy_decision_receipts_readonly,
    summarize_strategy_misses,
)

__all__ = [
    "CanonicalPerformanceService",
    "Cohort",
    "EvidenceState",
    "PerformanceCohort",
    "PerformanceRow",
    "ReturnMethodology",
    "AttributionState",
    "Eligibility",
    "StrategyMissAttributionReport",
    "StrategyMissAttributionRow",
    "StrategyMissAttributionSummary",
    "attribute_strategy_misses",
    "from_portfolio_rows",
    "load_portfolio_performance_rows_readonly",
    "load_strategy_decision_receipts_readonly",
    "summarize_strategy_misses",
]
