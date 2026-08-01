from __future__ import annotations

from intraday_scanner.services.benchmark_service import benchmark_coverage


def test_v6_benchmark_coverage_requires_both_predeclared_source_receipts() -> None:
    coverage = benchmark_coverage(
        [
            {
                "learning_eligible": True,
                "activation_status": "ACTIVATED",
                "benchmark_symbol": "SPY",
                "benchmark_return_pct": 0.3,
                "benchmark_source_bar_hash_sha256": "a" * 64,
                "secondary_benchmark_symbol": "IWM",
                "secondary_benchmark_return_pct": None,
                "secondary_benchmark_source_bar_hash_sha256": None,
            }
        ]
    )

    assert coverage["primary_complete"] is True
    assert coverage["secondary_complete"] is False
    assert coverage["secondary_coverage_pct"] == 0.0
