"""Predeclared benchmark contracts for AlphaOps research evaluation."""

from __future__ import annotations

from typing import Any

PRIMARY_BENCHMARK = "SPY"
SECONDARY_BENCHMARK = "IWM"
BENCHMARK_POLICY_VERSION = "dawnstrike-alphaops-v6-benchmark-policy-v1"


def alphaops_v6_benchmark_policy() -> dict[str, Any]:
    """Return the frozen benchmark declaration made before evaluation."""

    return {
        "policy_version": BENCHMARK_POLICY_VERSION,
        "primary_benchmark": PRIMARY_BENCHMARK,
        "secondary_benchmark": SECONDARY_BENCHMARK,
        "selection_after_outcome": False,
        "cash_baseline": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def benchmark_coverage(outcomes: list[dict[str, Any]]) -> dict[str, Any]:
    """Measure source-backed benchmark coverage without treating unknown as flat."""

    eligible = [
        row
        for row in outcomes
        if row.get("learning_eligible") is True
        and row.get("activation_status") == "ACTIVATED"
    ]
    primary = [
        row
        for row in eligible
        if row.get("benchmark_symbol") == PRIMARY_BENCHMARK
        and _number(row.get("benchmark_return_pct")) is not None
        and row.get("benchmark_source_bar_hash_sha256")
    ]
    secondary = [
        row
        for row in eligible
        if row.get("secondary_benchmark_symbol") == SECONDARY_BENCHMARK
        and _number(row.get("secondary_benchmark_return_pct")) is not None
        and row.get("secondary_benchmark_source_bar_hash_sha256")
    ]
    denominator = len(eligible)
    return {
        "policy": alphaops_v6_benchmark_policy(),
        "eligible_outcome_count": denominator,
        "primary_coverage_pct": _pct(len(primary), denominator),
        "secondary_coverage_pct": _pct(len(secondary), denominator),
        "primary_complete": len(primary) == denominator if denominator else False,
        "secondary_complete": len(secondary) == denominator if denominator else False,
        "missing_truth_is_zero": False,
        "research_only": True,
    }


def _pct(count: int, total: int) -> float | None:
    return round(100.0 * count / total, 6) if total else None


def _number(value: object) -> float | None:
    try:
        parsed = float(str(value))
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


__all__ = [
    "BENCHMARK_POLICY_VERSION",
    "PRIMARY_BENCHMARK",
    "SECONDARY_BENCHMARK",
    "alphaops_v6_benchmark_policy",
    "benchmark_coverage",
]
