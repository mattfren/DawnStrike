from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from intraday_scanner.v2.telegram_intel import (
    build_command_center_pages,
    draft,
    readiness,
    send,
    verify,
)

RUN_DATE = date(2026, 6, 29)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    _write_json(
        Path("data/v2_omega_sentinel/status/latest_status.json"),
        {
            "accepted_candidate_count": 0,
            "accepted_data_end_date": "2026-06-26",
            "alert_level": "yellow",
            "blocked_candidate_count": 2,
            "closes": 0,
            "commitbridge_status": "passed",
            "completed_bar_status": "passed",
            "data_truth_status": "reconciled_with_minor_diffs",
            "fill_truth_status": "passed",
            "fills": 0,
            "frozen_pick_hash": "pick-hash",
            "kill_switch_active": True,
            "next_action": "Review Command Center.",
            "open_positions": 0,
            "pending_orders": 1,
            "riskhub_status": "blocked",
            "run_date": RUN_DATE.isoformat(),
            "status": "completed",
            "warnings": ["no accepted candidates"],
            "watchlist_count": 1,
        },
    )
    _write_json(
        Path("data/v2_scheduler/status/latest_status.json"),
        {"command_name": "morning_check", "run_date": RUN_DATE.isoformat(), "status": "passed"},
    )
    _write_json(
        Path("data/v2_autonomous_runner/status/latest_status.json"),
        {"missed_runs": {"rows": []}, "status": "installed"},
    )
    _write_json(
        Path("data/v2_omega_sentinel/alerts/latest_alert.json"),
        {"alert_level": "yellow", "warnings": ["candidate blocked"]},
    )
    _write_json(
        Path("data/v2_autodata/reports/autodata_summary.json"),
        {
            "canonical_provider_id": "alpaca_market_data",
            "provider_readiness_status": "ready_public_fallback_only",
            "status": "COMPLETE",
        },
    )
    _write_json(
        Path("data/v2_autodata/reports/provider_readiness.json"),
        {
            "providers": [
                {
                    "provider_id": "alpaca_market_data",
                    "source_label": "broker_or_vendor_intraday",
                    "source_trust_level": "broker_or_vendor_readonly",
                }
            ],
            "status": "ready_public_fallback_only",
        },
    )
    _write_json(Path("data/v2_autodata/reports/fetch_pending_latest.json"), {"status": "passed"})
    _write_json(
        Path("data/v2_fill_truth/reports/filltruth_summary.json"),
        {"fills_resolved": 0, "status": "passed"},
    )
    _write_json(
        Path("data/v2_fill_truth/reports/pending_resolution_latest.json"),
        {"decisions": [], "status": "passed"},
    )
    _write_json(
        Path("data/v2_evidence_commit/reports/evidence_commit_summary.json"),
        {"blocked": 1, "commit_events": 0, "status": "passed"},
    )
    _write_json(
        Path("data/v2_evidence_commit/reconciliation/pending_divergence_latest.json"),
        {"pending_divergence_status": "resolved", "status": "passed"},
    )
    _write_json(Path("data/v2_paper_ops/state/pending_orders.json"), [])
    _write_json(Path("data/v2_paper_ops/state/open_positions.json"), [])
    _write_text(
        Path("data/v2_paper_ops/calendar/strategy_daily_returns.csv"),
        "date,strategy_id,daily_return_pct,trades_closed\n2026-06-29,s1,0,0\n",
    )
    _write_json(
        Path("data/v2_learning_foundry/lessons/2026-06-29.json"),
        {
            "market_regime": "uptrend",
            "promotion_result": "blocked",
            "strategies_decayed": ["s2"],
            "today_learned": "Evidence is still insufficient.",
            "tomorrow": "Run after-close with learning.",
        },
    )
    _write_json(
        Path("data/v2_learning_foundry/reports/promotion_review.json"),
        {"review_count": 1, "status": "blocked"},
    )
    _write_json(
        Path("data/v2_forward_evidence/frozen_picks/2026-06-29_picks.json"),
        {
            "accepted_candidates": [],
            "blocked_candidates": [
                {
                    "blocked_reason": "RiskHub kill switch active.",
                    "setup_status": "blocked_candidate",
                    "strategy_id": "s1",
                    "symbol": "QQQ",
                }
            ],
            "near_setup_candidates": [],
            "no_setup_explanations": [],
            "pick_set_hash": "pick-hash",
            "strategies_scanned": ["s1", "s2"],
            "strategy_statuses": {"s1": "watch", "s2": "quarantined"},
            "watchlist_candidates": [
                {"entry_trigger": "needs reclaim", "strategy_id": "s1", "symbol": "SPY"}
            ],
        },
    )
    _write_json(
        Path("data/v2_forward_evidence/reports/riskhub_daily.json"),
        {
            "kill_switch_active": True,
            "riskhub_status": "blocked",
            "warnings": ["candidate_blocked_by_decision_engine"],
        },
    )
    _write_json(
        Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"),
        {"rows": [{"evidence_status": "watch", "strategy_id": "s1"}], "status": "passed"},
    )
    _write_json(
        Path("data/v2_command_center/command_center_qa.json"),
        {"broken_links": [], "page_count": 1, "status": "passed"},
    )
    _write_json(
        Path("data/v2_market_masters/reports/report_latest.json"),
        {
            "build_id": "mm-build",
            "challenger_count": 8,
            "final_status": "COMPLETE_MARKET_MASTERS_WIRED",
            "methodology_count": 11,
            "primitive_count": 8,
            "promotion_result": "blocked_no_true_forward_sample",
            "source_count": 17,
            "validation_triggered": False,
        },
    )
    _write_json(Path("data/v2_market_masters/reports/verify_latest.json"), {"status": "passed"})
    _write_json(
        Path("data/v2_market_masters/evals/2026-06-29_eval.json"),
        {
            "rows": [
                {
                    "challenger_id": "mm_ts_momentum_regime_filter_v1",
                    "evaluation_status": "watch",
                }
            ],
            "status": "passed",
        },
    )
    _write_json(
        Path("data/v2_market_masters/shadow_runs/2026-06-29_shadow_results.json"),
        {"rows": [{"challenger_id": "mm_ts_momentum_regime_filter_v1"}], "shadow_count": 1},
    )
    _write_json(
        Path("data/v2_learning_foundry/candidates/market_masters_sync_2026-06-29.json"),
        {"champion_registry_changed": False, "status": "passed"},
    )


def test_readiness_missing_env_blocks_send_without_secret_values(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_CHAT_ID", raising=False)

    payload = readiness()

    assert payload["status"] == "blocked_missing_telegram_env"
    assert payload["token_present"] is False
    assert payload["chat_id_present"] is False
    assert "TELEGRAM_BOT_TOKEN" not in json.dumps(payload)


def test_no_picks_draft_is_rich_and_passes_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_artifacts(tmp_path, monkeypatch)

    payload = draft(kind="no-picks", run_date=RUN_DATE)
    text = str(payload["text"])

    assert payload["quality_score"] == 100
    assert "Why no official paper pick:" in text
    assert "RiskHub kill switch" in text
    assert "Market Masters watch:" in text
    assert "mm_ts_momentum_regime_filter_v1" in text
    assert "blocked_no_true_forward_sample" in text
    assert "data/v2_command_center/production.html" in text
    assert "live execution" in text


def test_send_dry_run_never_calls_network_and_redacts_chat_id(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_artifacts(tmp_path, monkeypatch)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123456:ABCDEF")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "987654321")
    monkeypatch.setenv("TELEGRAM_ENABLED", "false")
    called = {"count": 0}

    def fake_transport(token: str, data: dict[str, str], timeout: int) -> dict[str, object]:
        called["count"] += 1
        return {"ok": True, "token": token, "chat_id": data["chat_id"], "timeout": timeout}

    payload = send(kind="morning", run_date=RUN_DATE, transport=fake_transport)
    raw_report = Path("data/v2_telegram_intel/reports/send_latest.json").read_text()

    assert payload["send_status"] == "dry_run_or_disabled"
    assert called["count"] == 0
    assert "987654321" not in raw_report
    assert "123456:ABCDEF" not in raw_report


def test_command_center_pages_are_static_and_secret_free(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_artifacts(tmp_path, monkeypatch)
    draft(kind="no-picks", run_date=RUN_DATE)
    build_command_center_pages()

    for name in (
        "telegram_intel.html",
        "telegram_messages.html",
        "telegram_readiness.html",
        "message_quality.html",
    ):
        text = Path("data/v2_command_center", name).read_text(encoding="utf-8")
        assert "Research-only; no live execution." in text
        assert "<script" not in text.lower()
        assert "C:\\Users\\" not in text


def test_verify_passes_safety_boundaries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_artifacts(tmp_path, monkeypatch)
    draft(kind="no-picks", run_date=RUN_DATE)

    payload = verify()

    assert payload["status"] == "passed"
    assert payload["failures"] == []
