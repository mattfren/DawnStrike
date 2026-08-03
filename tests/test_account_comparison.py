from __future__ import annotations

from intraday_scanner.performance.account_comparison import (
    build_account_comparison,
    public_account_comparison,
)


def _ledger(*, version: str, account_id: str, return_pct: float) -> dict[str, object]:
    return {
        "account_id": account_id,
        "strategy_id": "alphaops_v5" if version == "v5" else "alphaops_v6",
        "strategy_version": f"dawnstrike-alphaops-{version}",
        "execution_policy_version": f"{version}-policy",
        "cost_model_version": f"{version}-cost",
        "market_date": "2026-08-03",
        "status": "COMPLETE",
        "net_return_pct": return_pct,
        "source_hash_sha256": "a" * 64,
        "beginning_equity_cents": 100_000,
        "ending_equity_cents": int(100_000 * (1.0 + return_pct / 100.0)),
        "realized_net_pnl_cents": int(1_000 * return_pct),
        "trade_count": 1,
    }


def _benchmark(symbol: str, return_pct: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "market_date": "2026-08-03",
        "return_close": return_pct,
        "payload_json": {"source_bar_hash_sha256": "b" * 64},
    }


def test_account_comparison_never_converts_v6_signal_outcomes_into_account_returns() -> None:
    report = build_account_comparison(
        v5_ledger=[_ledger(version="v5", account_id="v5-account", return_pct=1.0)],
        v6_ledger=[],
        benchmark_rows=[_benchmark("SPY", 0.3), _benchmark("IWM", 0.4)],
        calculated_at="2026-08-03T21:00:00+00:00",
    )

    assert report["status"] == "WAITING_FOR_AUTHORITATIVE_V6_ACCOUNT_LEDGER"
    assert "missing_authoritative_v6_account_ledger" in report["promotion_blockers"]
    assert report["series_metrics"]["v6"]["compounded_net_return_pct"] is None
    assert report["series_metrics"]["SPY"]["compounded_net_return_pct"] is None
    assert report["promotion_eligible"] is False


def test_complete_account_comparison_requires_aligned_ledger_and_sourced_benchmarks() -> None:
    report = build_account_comparison(
        v5_ledger=[_ledger(version="v5", account_id="v5-account", return_pct=1.0)],
        v6_ledger=[_ledger(version="v6", account_id="v6-account", return_pct=1.5)],
        benchmark_rows=[_benchmark("SPY", 0.3), _benchmark("IWM", 0.4)],
        calculated_at="2026-08-03T21:00:00+00:00",
    )

    assert report["status"] == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
    assert report["alignment"]["coverage_pct"] == 100.0
    assert report["series_metrics"]["v5"]["compounded_net_return_pct"] == 1.0
    assert report["series_metrics"]["v6"]["compounded_net_return_pct"] == 1.5
    assert report["series_metrics"]["cash"]["compounded_net_return_pct"] == 0.0
    assert report["series_metrics"]["SPY"]["compounded_net_return_pct"] == 0.3
    public = public_account_comparison(report)
    assert public is not None
    assert public["status"] == "COMPLETE_ACCOUNT_LEVEL_COMPARISON"
    assert "comparison_id" not in public


def test_benchmark_without_lineage_is_not_publishable() -> None:
    report = build_account_comparison(
        v5_ledger=[_ledger(version="v5", account_id="v5-account", return_pct=1.0)],
        v6_ledger=[_ledger(version="v6", account_id="v6-account", return_pct=1.5)],
        benchmark_rows=[
            {"symbol": "SPY", "market_date": "2026-08-03", "return_close": 0.3},
            _benchmark("IWM", 0.4),
        ],
        calculated_at="2026-08-03T21:00:00+00:00",
    )

    assert report["status"] == "NOT_PUBLISHABLE_INCOMPLETE_ACCOUNT_OR_BENCHMARK_TRUTH"
    assert "missing_sourced_spy_benchmark" in report["promotion_blockers"]
