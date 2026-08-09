from __future__ import annotations

from types import SimpleNamespace

import pytest

from intraday_scanner.v2.backtest.intraday_metrics import (
    compare_benchmark,
    compute_intraday_metrics,
)


def test_session_metrics_do_not_annualize_one_session() -> None:
    trades = [SimpleNamespace(net_pnl=100.0), SimpleNamespace(net_pnl=-25.0)]
    equity = [SimpleNamespace(equity=100_000.0), SimpleNamespace(equity=100_075.0)]

    metrics = compute_intraday_metrics(
        trades,
        equity,
        session_returns={"2026-08-03": 0.00075},
        benchmark_returns={"2026-08-03": 0.0001},
    )

    assert metrics["annualized_return"] is None
    assert metrics["annualization_status"] == "NOT_APPLICABLE_INSUFFICIENT_SESSIONS"
    assert metrics["return_vs_benchmark"] == pytest.approx(0.00065)
    assert metrics["uncertainty"]["status"] == "DESCRIPTIVE_ONLY"


def test_benchmark_comparison_requires_overlapping_sessions() -> None:
    blocked = compare_benchmark({"2026-08-03": 0.01}, {"2026-08-04": 0.02})
    compared = compare_benchmark(
        {"2026-08-03": 0.01, "2026-08-04": 0.02},
        {"2026-08-04": 0.01},
    )

    assert blocked["status"] == "DATA_INELIGIBLE"
    assert compared["overlap_sessions"] == 1
    assert compared["status"] == "DESCRIPTIVE_ONLY"
