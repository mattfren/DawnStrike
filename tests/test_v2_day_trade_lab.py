from __future__ import annotations

import ast
import csv
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.v2.day_trade_lab import (
    build_corpus,
    build_sessions,
    compare,
    compare_corpus,
    corpus_plan,
    corpus_report,
    demo,
    evaluate_refinements,
    fetch_corpus,
    generate_refinements,
    import_data,
    init,
    report,
    robustness,
    robustness_report,
    run,
    run_corpus,
    split_test,
    stress_slippage,
    verify,
)
from intraday_scanner.v2.day_trade_lab.core import REQUIRED_STRATEGIES


def _write_seeded_corpus_fetch(output_root: Path, interval: str) -> None:
    corpus_root = output_root / "corpus"
    normalized = (
        corpus_root
        / "normalized"
        / "per_provider"
        / "alpaca_market_data"
        / interval
        / "seed.csv"
    )
    normalized.parent.mkdir(parents=True, exist_ok=True)
    step = 1 if interval == "1min" else 5
    rows: list[dict[str, object]] = []
    for symbol_index, symbol in enumerate(["QQQ", "SPY"]):
        for day_index, day in enumerate(["2026-06-30", "2026-07-01"]):
            base = 100 + symbol_index * 10 + day_index
            session_day = datetime.fromisoformat(day).replace(tzinfo=timezone.utc)
            for minute_offset in [13 * 60, 13 * 60 + 10, 13 * 60 + 20]:
                ts = session_day + timedelta(minutes=minute_offset)
                rows.append(
                    {
                        "symbol": symbol,
                        "timestamp": ts.isoformat(),
                        "open": base - 0.3,
                        "high": base,
                        "low": base - 0.6,
                        "close": base - 0.1,
                        "volume": 1000,
                    }
                )
            for index in range(48):
                ts = session_day + timedelta(minutes=13 * 60 + 30 + index * step)
                open_ = base + index * 0.08
                close = open_ + (0.15 if index > 5 else 0.02)
                rows.append(
                    {
                        "symbol": symbol,
                        "timestamp": ts.isoformat(),
                        "open": round(open_, 4),
                        "high": round(close + 0.08, 4),
                        "low": round(open_ - 0.08, 4),
                        "close": round(close, 4),
                        "volume": 2000 + index,
                    }
                )
    with normalized.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["symbol", "timestamp", "open", "high", "low", "close", "volume"],
        )
        writer.writeheader()
        writer.writerows(rows)
    manifest = {
        "schema_version": "v2.day_trade_lab.provider_fetch_manifest.v1",
        "status": "passed",
        "interval": interval,
        "provider_ids": ["alpaca_market_data"],
        "rows": [
            {
                "schema_version": "v2.day_trade_lab.corpus_provider_request.v1",
                "status": "passed",
                "provider_id": "alpaca_market_data",
                "provider_name": "Alpaca Market Data",
                "source_label": "broker_or_vendor_intraday",
                "source_trust_level": "provider_backed",
                "symbol": "SEEDED",
                "interval": interval,
                "requested_start": "2026-06-01",
                "requested_end": "2026-07-01",
                "returned_bars": len(rows),
                "accepted_bars": len(rows),
                "rejected_bars": 0,
                "raw_artifact_path": "seeded-test-raw.json",
                "raw_hash": "seeded-test-raw",
                "normalized_artifact_path": normalized.as_posix(),
                "normalized_hash": "seeded-test-normalized",
                "warnings": [],
                "errors": [],
                "research_only": True,
                "live_trading_enabled": False,
            }
        ],
        "warnings": [],
        "research_only": True,
        "live_trading_enabled": False,
    }
    manifest_dir = corpus_root / "manifests"
    reports_dir = corpus_root / "reports"
    manifest_dir.mkdir(parents=True, exist_ok=True)
    reports_dir.mkdir(parents=True, exist_ok=True)
    (manifest_dir / f"provider_fetch_manifest_{interval}.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    (reports_dir / f"provider_fetch_summary_{interval}.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    (reports_dir / "provider_fetch_summary.md").write_text(
        "# Provider Fetch Summary\n\nSeeded provider-backed test corpus.\n",
        encoding="utf-8",
        newline="\n",
    )


def _write_minimal_x2_robustness_pages(repo_root: Path) -> None:
    pages = repo_root / "data/v2_command_center_x2/pages"
    pages.mkdir(parents=True, exist_ok=True)
    boundary = (
        "historical day-trade backtest only - not validated. "
        "zero overnight holds. provider/data limitations."
    )
    for name in (
        "day_trade_robustness.html",
        "day_trade_slippage_stress.html",
        "day_trade_oos.html",
        "day_trade_refinements.html",
    ):
        (pages / name).write_text(boundary, encoding="utf-8", newline="\n")


def test_day_trade_lab_demo_proves_same_session_intraday_trades(tmp_path: Path) -> None:
    output_root = tmp_path / "day_trade_lab"

    result = demo(output_root=output_root, repo_root=tmp_path)

    assert result["status"] == "passed"
    assert result["source_mode"] == "fixture_demo_intraday"
    assert result["same_session_trade_check"] is True
    assert result["one_minute_trade_count"] > 0
    assert result["five_minute_trade_count"] > 0
    assert result["missing_strategies"] == []
    assert (output_root / "reports/demo_proof.json").exists()
    assert (output_root / "demo/trades/day_trades_1min.csv").exists()
    assert not (output_root / "trades/day_trades_1min.csv").exists()


def test_day_trade_lab_rejects_daily_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="1min or 5min"):
        import_data(output_root=tmp_path / "day_trade_lab", repo_root=tmp_path, interval="1d")


def test_day_trade_corpus_workflow_is_intraday_safe_and_metric_complete(tmp_path: Path) -> None:
    repo_root = tmp_path
    output_root = tmp_path / "data/v2_day_trade_lab"
    original_strategy_catalog = tuple(REQUIRED_STRATEGIES)

    plan = corpus_plan(
        months=1,
        intervals="1min,5min",
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    _write_seeded_corpus_fetch(output_root, "1min")
    _write_seeded_corpus_fetch(output_root, "5min")

    quality = build_corpus(
        months=1,
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    one = run_corpus(
        months=1,
        interval="1min",
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    five = run_corpus(
        months=1,
        interval="5min",
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    comparison = compare_corpus(
        months=1,
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    summary = corpus_report(
        months=1,
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    qa = verify(output_root=output_root, repo_root=repo_root)
    robust = robustness(months=1, asof="2026-07-02", output_root=output_root, repo_root=repo_root)
    stress = stress_slippage(
        months=1,
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    oos = split_test(months=1, asof="2026-07-02", output_root=output_root, repo_root=repo_root)
    candidates = generate_refinements(
        months=1,
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    evaluation = evaluate_refinements(
        months=1,
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    _write_minimal_x2_robustness_pages(repo_root)
    robust_report = robustness_report(
        months=1,
        asof="2026-07-02",
        output_root=output_root,
        repo_root=repo_root,
    )
    robust_qa = verify(output_root=output_root, repo_root=repo_root)

    assert plan["intervals"] == ["1min", "5min"]
    assert "range_request_provider_calls" in plan["estimated_provider_calls"]
    assert plan["universe_priority"][:2] == ["QQQ", "SPY"]
    assert quality["canonical_duplicate_timestamp_count"] == 0
    assert quality["partial_session_count"] > 0
    assert quality["missing_session_count"] > 0
    assert one["overnight_hold_count"] == 0
    assert five["overnight_hold_count"] == 0
    assert one["skip_count"] > 0
    assert comparison["comparison_rows"] == len(REQUIRED_STRATEGIES) * 2
    best = summary["best_day_trade_strategy"]
    assert best["average_r"] == best["expectancy"]
    assert "max_hold_minutes" in best
    assert best["time_of_day_breakdown"]
    assert summary["evidence_mode"] == "historical_daytrade_backtest"
    assert summary["overnight_hold_count"] == 0
    assert summary["strategy_validation"] == "not_validated"
    assert qa["status"] == "passed"
    assert qa["artifact_mode"] == "corpus"
    assert qa["checks"]["corpus_no_mock_provider_rows"] is True
    assert (output_root / "reports/corpus_no_trade_days.csv").exists()
    assert (output_root / "reports/corpus_skip_reasons.csv").exists()
    sync = json.loads(
        (repo_root / "data/v2_learning_foundry/reports/day_trade_corpus_sync.json").read_text(
            encoding="utf-8"
        )
    )
    assert sync["promotion_allowed"] is False
    assert sync["commitbridge_commits"] == 0
    assert tuple(REQUIRED_STRATEGIES) == original_strategy_catalog
    assert robust["overnight_hold_count"] == 0
    assert robust["slice_counts"]["by_symbol"] > 0
    assert robust["slice_counts"]["by_time"] > 0
    assert robust["slice_counts"]["by_month"] > 0
    assert robust["slice_counts"]["by_weekday"] > 0
    assert robust["slice_counts"]["by_interval"] > 0
    assert (output_root / "robustness/by_symbol/by_symbol.csv").exists()
    assert (output_root / "robustness/by_time/by_time.csv").exists()
    stress_rows = stress["stress_rows"]
    current_by_key = {
        (row["strategy_id"], row["interval"]): row
        for row in stress_rows
        if row["stress_name"] == "current_slippage"
    }
    assert any(
        row["total_return_pct"]
        <= current_by_key[(row["strategy_id"], row["interval"])]["total_return_pct"]
        for row in stress_rows
        if row["stress_name"] in {"slippage_3x", "fixed_spread_estimate"}
        and (row["strategy_id"], row["interval"]) in current_by_key
    )
    assert stress["strategy_validation"] == "not_validated"
    assert oos["rows"]
    assert {row["split_name"] for row in oos["rows"]} >= {
        "70_30_time",
        "50_50_time",
        "odd_even_sessions",
    }
    fragility = json.loads(
        (output_root / "robustness/reports/fragility_report.json").read_text(encoding="utf-8")
    )
    assert any(row["reason"] == "low_sample_size" for row in fragility["rows"])
    assert candidates["candidate_count"] == 8
    assert all(row["status"] == "shadow_refinement" for row in candidates["candidates"])
    assert all(row["not_validated"] is True for row in candidates["candidates"])
    assert all(row["no_live_trading"] is True for row in candidates["candidates"])
    assert all(row["promotion_allowed"] is False for row in candidates["candidates"])
    assert evaluation["candidate_count"] == candidates["candidate_count"]
    assert evaluation["champions_changed"] is False
    assert all(row["status"] == "shadow_refinement" for row in evaluation["rows"])
    assert robust_report["strategy_validation"] == "not_validated"
    assert robust_report["promotion_allowed"] is False
    assert robust_report["paperops_mutation"] is False
    assert robust_report["commitbridge_commits"] == 0
    assert robust_report["champions_changed"] is False
    assert all(row["status"] == "passed" for row in robust_report["red_team_findings"])
    assert robust_qa["status"] == "passed"
    assert robust_qa["artifact_mode"] == "robustness"
    assert robust_qa["checks"]["robustness_no_validation_or_promotion"] is True
    assert robust_qa["checks"]["robustness_no_mutations"] is True
    robustness_sync = json.loads(
        (repo_root / "data/v2_learning_foundry/reports/day_trade_robustness_sync.json").read_text(
            encoding="utf-8"
        )
    )
    assert robustness_sync["promotion_allowed"] is False
    assert robustness_sync["paperops_mutation"] is False
    assert robustness_sync["commitbridge_commits"] == 0


def test_day_trade_corpus_rejects_daily_interval(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="corpus only accepts"):
        fetch_corpus(output_root=tmp_path / "day_trade_lab", repo_root=tmp_path, interval="1d")


def test_day_trade_lab_real_limited_workflow_reports_truth(tmp_path: Path) -> None:
    repo_root = tmp_path
    output_root = tmp_path / "data/v2_day_trade_lab"
    real_root = tmp_path / "data/v2_real_intraday"
    normalized = real_root / "normalized/fixture_real_intraday.csv"
    normalized.parent.mkdir(parents=True, exist_ok=True)
    normalized.write_text(
        "\n".join(
            [
                "symbol,timestamp,open,high,low,close,volume",
                "QQQ,2026-06-29T13:30:00+00:00,100,101,99,100.6,1000",
                "QQQ,2026-06-29T13:31:00+00:00,100.6,101.6,100.5,101.3,1000",
                "QQQ,2026-06-29T13:32:00+00:00,101.3,102.2,101.1,101.8,1000",
                "QQQ,2026-06-29T14:00:00+00:00,101.8,103.0,101.4,102.8,1000",
                "QQQ,2026-06-29T15:00:00+00:00,102.8,103.2,102.0,102.4,1000",
                "QQQ,2026-06-29T19:59:00+00:00,102.4,103.4,102.2,103.0,1000",
            ]
        )
        + "\n",
        encoding="utf-8",
        newline="\n",
    )
    manifest = real_root / "manifests/latest_import.json"
    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(
        json.dumps(
            {
                "normalized_artifact": "data/v2_real_intraday/normalized/fixture_real_intraday.csv",
                "source_label": "broker_or_vendor_intraday",
                "source_name": "fixture",
            }
        ),
        encoding="utf-8",
    )

    init(output_root=output_root, repo_root=repo_root)
    one = import_data(output_root=output_root, repo_root=repo_root, interval="1min")
    five = import_data(output_root=output_root, repo_root=repo_root, interval="5min")
    sessions = build_sessions(output_root=output_root, repo_root=repo_root)
    run_one = run(output_root=output_root, repo_root=repo_root, interval="1min")
    run_five = run(output_root=output_root, repo_root=repo_root, interval="5min")
    comparison = compare(output_root=output_root, repo_root=repo_root)
    summary = report(output_root=output_root, repo_root=repo_root)
    qa = verify(output_root=output_root, repo_root=repo_root)

    assert one["source_mode"] == "real_intraday_limited"
    assert five["source_mode"] == "real_intraday_limited"
    assert one["accepted_session_count"] == 1
    assert sessions["session_count"] >= 1
    assert run_one["overnight_trade_count"] == 0
    assert run_five["overnight_trade_count"] == 0
    assert comparison["comparison_rows"] == len(REQUIRED_STRATEGIES) * 2
    assert summary["final_status"] == "COMPLETE_DAY_TRADE_LAB_WITH_DATA_LIMITATIONS"
    assert summary["real_intraday_session_count"] == 1
    assert qa["status"] == "passed"
    assert (repo_root / "docs/audit/omega_day_trade_lab_build_state.json").exists()
    assert (repo_root / "data/v2_learning_foundry/reports/day_trade_lab_sync.json").exists()
    assert (repo_root / "data/v2_market_masters/reports/day_trade_lab_sync.json").exists()


def test_day_trade_lab_package_keeps_research_only_surface() -> None:
    forbidden_roots = {
        "app",
        "sqlite3",
        "streamlit",
        "socket",
        "urllib",
        "requests",
        "httpx",
    }
    forbidden_calls = {"connect", "urlopen", "request"}

    for path in Path("intraday_scanner/v2/day_trade_lab").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_roots, path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_roots, path
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path
