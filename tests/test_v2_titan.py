from __future__ import annotations

import json
from datetime import date
from pathlib import Path

from intraday_scanner.public_data.nasdaq_historical_fetcher import (
    fetch_nasdaq_historical_daily_dataset,
)

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.decision_engine import build_decision_engine
from intraday_scanner.v2.quality import score_titan_quality
from intraday_scanner.v2.riskhub import build_risk_report


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, payload: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(payload, encoding="utf-8")


def _seed_titan_fixture(tmp_path: Path) -> tuple[Path, Path, Path, Path]:
    titan = tmp_path / "v2_titan"
    alpha = tmp_path / "v2_alpha_lab"
    datatruth = tmp_path / "v2_data_truth"
    paper = tmp_path / "v2_paper_ops"
    _write_json(
        alpha / "scans" / "latest_scan.json",
        {
            "cards": [
                {
                    "data_snapshot_id": "public:snapshot",
                    "direction": "long",
                    "entry_trigger": "Signal at close 100.00; default execution is next bar open.",
                    "evidence": ["fixture"],
                    "historical_summary": "10 trades, return 5.00%, max drawdown -3.00%.",
                    "invalidation": "stop hit",
                    "research_only": True,
                    "reward": 15.0,
                    "reward_risk": 3.0,
                    "risk_per_share": 5.0,
                    "run_manifest_id": "alpha-run",
                    "setup_score": 80,
                    "status": "candidate",
                    "stop": 95.0,
                    "strategy_id": "test_strategy",
                    "strategy_version": "v1",
                    "symbol": "TST",
                    "target": 115.0,
                    "timestamp": "2026-06-29T13:30:00+00:00",
                    "warnings": [],
                }
            ],
            "no_setup": [
                {
                    "strategy_id": "test_strategy",
                    "strategy_version": "v1",
                    "symbol": "ALT",
                    "timestamp": "2026-06-29T13:30:00+00:00",
                    "warnings": ["no_current_setup"],
                }
            ],
            "warnings": [],
        },
    )
    _write_json(
        alpha / "reports" / "strategy_comparison.json",
        [
            {
                "rank_by_return": 1,
                "strategy_id": "test_strategy",
                "total_return_pct": 0.05,
            }
        ],
    )
    _write_json(
        alpha / "paper" / "strategy_pnl.json",
        [{"strategy_id": "test_strategy", "trade_count": 2, "net_pnl": 10.0}],
    )
    _write_text(alpha / "reports" / "alpha_lab_summary.md", "# Alpha\n")
    _write_json(
        datatruth / "reconciliation" / "latest_reconciliation.json",
        {"status": "single_provider_unreconciled"},
    )
    _write_json(
        datatruth / "manifests" / "latest.json",
        {"accepted_end": "2026-06-26", "snapshot_id": "snapshot-20260626"},
    )
    _write_text(datatruth / "reports" / "data_truth_summary.md", "# DataTruth\nsingle_provider\n")
    _write_json(
        paper / "reports" / "strategy_evidence_scores.json",
        {
            "scores": [
                {
                    "blockers": "needs more forward evidence",
                    "evidence_status": "watch",
                    "forward_closed_trades": 0,
                    "forward_days": 1,
                    "overall_score": 21,
                    "replay_closed_trades": 2,
                    "replay_days": 2,
                    "strategy_id": "test_strategy",
                }
            ]
        },
    )
    _write_json(
        paper / "reports" / "forward_readiness.json",
        {
            "calendar_truth_status": "failed",
            "ledger_rebuild_status": "mismatch",
            "status": "blocked",
            "warnings": ["ledger rebuild did not match stored state/calendar"],
        },
    )
    _write_json(paper / "state" / "pending_orders.json", [])
    _write_json(paper / "state" / "open_positions.json", [])
    _write_text(paper / "reports" / "paper_ops_summary.md", "# PaperOps\n")
    _write_text(paper / "calendar" / "calendar_summary.md", "# Calendar\n")
    _write_text(paper / "reports" / "strategy_evidence_summary.md", "# Evidence\n")
    return titan, alpha, datatruth, paper


def test_decision_engine_blocks_untrusted_candidates(tmp_path: Path) -> None:
    titan, alpha, datatruth, paper = _seed_titan_fixture(tmp_path)

    result = build_decision_engine(
        run_date=date(2026, 6, 29),
        output_root=titan,
        alpha_root=alpha,
        data_truth_root=datatruth,
        paper_ops_root=paper,
    )
    cards = json.loads((titan / "decision_engine" / "decision_cards.json").read_text())

    assert result.status == "passed_with_warnings"
    assert result.blocked_count == 1
    assert cards[0]["status"] == "blocked"
    assert "paper_ops_readiness_blocked" in cards[0]["warnings"]
    assert "single_provider_data_not_reconciled" in cards[0]["warnings"]
    assert "candidate_bar_after_datatruth_accepted_end" in cards[0]["warnings"]
    assert cards[0]["max_loss_estimate"] > 0


def test_riskhub_kill_switch_follows_blocked_readiness(tmp_path: Path) -> None:
    titan, alpha, datatruth, paper = _seed_titan_fixture(tmp_path)
    build_decision_engine(
        run_date=date(2026, 6, 29),
        output_root=titan,
        alpha_root=alpha,
        data_truth_root=datatruth,
        paper_ops_root=paper,
    )

    result = build_risk_report(run_date=date(2026, 6, 29), output_root=titan, paper_ops_root=paper)

    assert result.status == "blocked"
    assert result.kill_switch is True
    assert "paper_ops_readiness_blocked" in result.warnings
    assert "ledger_or_calendar_truth_failed" in result.warnings


def test_decision_engine_blocks_fragile_robustness_candidate(tmp_path: Path) -> None:
    titan, alpha, datatruth, paper = _seed_titan_fixture(tmp_path)
    _write_json(
        datatruth / "reconciliation" / "latest_reconciliation.json",
        {"status": "reconciled_with_minor_diffs"},
    )
    _write_json(
        datatruth / "manifests" / "latest.json",
        {"accepted_end": "2026-06-29", "snapshot_id": "snapshot-20260629"},
    )
    _write_json(
        paper / "reports" / "forward_readiness.json",
        {
            "calendar_truth_status": "passed",
            "ledger_rebuild_status": "passed",
            "status": "ready_with_warnings",
            "warnings": [],
        },
    )
    _write_json(
        alpha / "reports" / "robustness_summary.json",
        {
            "rows": [
                {
                    "robustness_status": "fragile",
                    "status": "experimental",
                    "strategy_id": "test_strategy",
                    "test_return_pct": -0.01,
                    "test_trade_count": 9,
                    "warnings": "negative out-of-sample return",
                }
            ]
        },
    )

    result = build_decision_engine(
        run_date=date(2026, 6, 29),
        output_root=titan,
        alpha_root=alpha,
        data_truth_root=datatruth,
        paper_ops_root=paper,
    )
    cards = json.loads((titan / "decision_engine" / "decision_cards.json").read_text())
    blocked = json.loads(
        (titan / "decision_engine" / "blocked_candidates.json").read_text()
    )

    assert result.blocked_count == 1
    assert cards[0]["status"] == "blocked"
    assert cards[0]["strategy_robustness_status"] == "fragile"
    assert cards[0]["strategy_robustness_eligible"] is False
    assert "strategy_robustness_fragile" in cards[0]["warnings"]
    assert "Alpha Lab robustness flags this strategy as fragile." in cards[0]["reasons_to_avoid"]
    assert blocked[0]["strategy_id"] == "test_strategy"


def test_riskhub_kill_switch_follows_fragile_robustness(tmp_path: Path) -> None:
    titan, alpha, datatruth, paper = _seed_titan_fixture(tmp_path)
    _write_json(
        datatruth / "reconciliation" / "latest_reconciliation.json",
        {"status": "reconciled_with_minor_diffs"},
    )
    _write_json(
        datatruth / "manifests" / "latest.json",
        {"accepted_end": "2026-06-29", "snapshot_id": "snapshot-20260629"},
    )
    _write_json(
        paper / "reports" / "forward_readiness.json",
        {
            "calendar_truth_status": "passed",
            "ledger_rebuild_status": "passed",
            "status": "ready_with_warnings",
            "warnings": [],
        },
    )
    _write_json(
        alpha / "reports" / "robustness_summary.json",
        {
            "rows": [
                {
                    "robustness_status": "fragile",
                    "status": "experimental",
                    "strategy_id": "test_strategy",
                    "warnings": "trade-order drawdown stress exceeds 20 percent",
                }
            ]
        },
    )
    build_decision_engine(
        run_date=date(2026, 6, 29),
        output_root=titan,
        alpha_root=alpha,
        data_truth_root=datatruth,
        paper_ops_root=paper,
    )

    result = build_risk_report(run_date=date(2026, 6, 29), output_root=titan, paper_ops_root=paper)
    report = json.loads((titan / "risk" / "risk_report.json").read_text())

    assert result.status == "blocked"
    assert result.kill_switch is True
    assert "candidate_blocked_by_decision_engine" in result.warnings
    assert "candidate_uses_fragile_strategy" in result.warnings
    assert report["policy"]["fragile_strategy_action"] == "kill_switch"


def test_command_center_and_quality_artifacts(tmp_path: Path) -> None:
    titan, alpha, datatruth, paper = _seed_titan_fixture(tmp_path)
    command_center = tmp_path / "v2_command_center"
    docs = tmp_path / "docs"
    build_decision_engine(
        run_date=date(2026, 6, 29),
        output_root=titan,
        alpha_root=alpha,
        data_truth_root=datatruth,
        paper_ops_root=paper,
    )
    build_risk_report(run_date=date(2026, 6, 29), output_root=titan, paper_ops_root=paper)
    _write_text(docs / "operations" / "titan_daily_runbook.md", "# Runbook\n")
    _write_text(docs / "audit" / "titan_release_summary.md", "# Release\n")

    center = build_command_center(
        output_root=command_center,
        titan_root=titan,
        alpha_root=alpha,
        data_truth_root=datatruth,
        paper_ops_root=paper,
    )
    quality = score_titan_quality(
        titan_root=titan,
        alpha_root=alpha,
        data_truth_root=datatruth,
        paper_ops_root=paper,
        command_center_root=command_center,
        docs_root=docs,
    )

    assert center.index_path.exists()
    assert (command_center / "risk.html").exists()
    assert center.qa_report_path.exists()
    qa_report = json.loads(center.qa_report_path.read_text())
    assert qa_report["status"] == "passed"
    assert qa_report["required_pages_present"] is True
    assert qa_report["script_tags_clear"] is True
    assert qa_report["absolute_local_paths_clear"] is True
    assert qa_report["research_only_banner_all_pages"] is True
    assert quality.status == "resume_required"
    assert quality.score < quality.target
    assert any("single-provider" in blocker for blocker in quality.blockers)


def test_nasdaq_historical_fetcher_skips_placeholder_volume(
    tmp_path: Path,
    monkeypatch,
) -> None:
    payload = {
        "data": {
            "tradesTable": {
                "rows": [
                    {
                        "close": "$100.00",
                        "date": "06/26/2026",
                        "high": "$101.00",
                        "low": "$99.00",
                        "open": "$100.50",
                        "volume": "9,999,999",
                    },
                    {
                        "close": "$101.00",
                        "date": "06/25/2026",
                        "high": "$102.00",
                        "low": "$98.00",
                        "open": "$99.50",
                        "volume": "1,234,567",
                    },
                ]
            }
        }
    }

    def fake_fetch_json(url: str, *, timeout_seconds: float):
        del url, timeout_seconds
        return payload

    monkeypatch.setattr(
        "intraday_scanner.public_data.nasdaq_historical_fetcher._fetch_json",
        fake_fetch_json,
    )
    result = fetch_nasdaq_historical_daily_dataset(
        symbols=("SPY",),
        cache_dir=tmp_path / "nasdaq",
        start=date(2026, 6, 25),
        end=date(2026, 6, 26),
    )

    assert result.dataset.total_bars == 1
    assert any("placeholder volume" in warning for warning in result.warnings)
