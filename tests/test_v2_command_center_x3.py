from __future__ import annotations

import re
from pathlib import Path

from intraday_scanner.v2.command_center_x3.core import (
    _strategies_body,
    build_command_center_x3,
    demo_command_center_x3,
    qa_command_center_x3,
    report_command_center_x3,
    verify_command_center_x3,
)

REPO_ROOT = Path(".")


def _primary_nav_labels(html: str) -> list[str]:
    match = re.search(r"<nav[^>]*data-primary-nav[^>]*>(.*?)</nav>", html, re.S)
    assert match is not None
    return re.findall(r">([^<]+)</a>", match.group(1))


def test_x3_builds_simplified_story_first_dashboard(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "intraday_scanner.v2.command_center_x3.core._alphaops_watchlist_payload",
        lambda _repo_root: {
            "source": "outputs/alpha_cycle/alpha_signals.json",
            "latest_as_of": "2026-07-13T09:10:00-04:00",
            "count": 1,
            "top_five": [
                {
                    "rank": 1,
                    "ticker": "TESTX",
                    "company": "Deterministic Test Company",
                    "score": 88.5,
                    "gap_pct": 21.4,
                    "gate": "NEEDS_CONFIRMATION",
                    "watch": 5.1,
                    "target": 5.7,
                    "failed_below": 4.7,
                    "reward_risk": 1.5,
                    "source_kind": "isolated_test_fixture",
                    "next": "Wait for trigger",
                }
            ],
        },
    )
    output_root = tmp_path / "command_center_x3"

    first = build_command_center_x3(repo_root=REPO_ROOT, output_root=output_root)
    second = build_command_center_x3(repo_root=REPO_ROOT, output_root=output_root)
    qa = qa_command_center_x3(repo_root=REPO_ROOT, output_root=output_root)
    report = report_command_center_x3(repo_root=REPO_ROOT, output_root=output_root)
    verify = verify_command_center_x3(repo_root=REPO_ROOT, output_root=output_root)

    assert first["build_id"] == second["build_id"]
    assert first["page_count"] == second["page_count"]
    assert first["top_level_nav_count"] == 5
    assert qa["status"] == "passed"
    assert report["final_status"] == "COMPLETE_COMMAND_CENTER_X3"
    assert report["quality_score"] == 100
    assert verify["status"] == "passed"

    home = (output_root / "pages/home.html").read_text(encoding="utf-8")
    calendar = (output_root / "pages/calendar.html").read_text(encoding="utf-8")
    strategies = (output_root / "pages/strategies.html").read_text(encoding="utf-8")
    trades = (output_root / "pages/trades.html").read_text(encoding="utf-8")
    no_picks = (output_root / "pages/no_picks.html").read_text(encoding="utf-8")
    system = (output_root / "pages/system.html").read_text(encoding="utf-8")

    assert _primary_nav_labels(home) == [
        "Top 5",
        "Calendar",
        "Paper Book",
        "Strategies",
        "System",
    ]
    assert "Evidence" not in _primary_nav_labels(home)
    assert "Learning" not in _primary_nav_labels(home)
    assert "Market Masters" not in _primary_nav_labels(home)
    assert "story-summary" in home
    assert "Today, Dawnstrike" in home
    assert "Top 5 Operator Watchlist" in home
    assert "outputs/alpha_cycle/alpha_signals.json" in home
    assert "TESTX" in home
    assert "Open Apex" not in home + calendar + strategies + trades + no_picks + system
    assert "calendar-grid" in calendar
    assert 'href="../days/' in calendar
    assert list((output_root / "days").glob("*.html"))
    assert "strategy-card" in strategies
    assert "Swing research, separated" in strategies
    assert "Paper Book with proof boundaries" in trades
    assert "trade-card" in trades
    assert "Why Dawnstrike waited" in no_picks
    assert "Advanced artifact links" in system
    assert "FillTruth" in system
    assert "CommitBridge" in system
    assert "Command Center X2" in system
    assert "data-table watchlist-table" in home
    assert "<table" not in home
    assert "<table" not in calendar
    assert "<table" not in strategies
    assert "<table" not in trades
    assert "buy button" not in (home + calendar + strategies + trades + no_picks + system).lower()
    assert "sell button" not in (home + calendar + strategies + trades + no_picks + system).lower()
    assert ">Validated<" not in home + calendar + strategies + trades + no_picks + system
    assert (REPO_ROOT / "data/v2_command_center_x2/index.html").exists()


def test_x3_demo_runs_full_cli_path(tmp_path: Path) -> None:
    output_root = tmp_path / "command_center_x3"

    result = demo_command_center_x3(repo_root=REPO_ROOT, output_root=output_root)

    assert result["status"] == "passed"
    assert result["final_status"] == "COMPLETE_COMMAND_CENTER_X3"
    assert result["quality_score"] == 100
    assert result["qa_status"] == "passed"
    assert result["verify_status"] == "passed"


def test_x3_separates_official_swing_cards_from_experimental_audit_evidence() -> None:
    html = _strategies_body(
        {
            "day_trade": {},
            "strategies": [
                {
                    "strategy_id": "ts_momentum_sma_atr",
                    "strategy_name": "Trend Momentum",
                    "role": "official_champion",
                    "daily_return_pct": "0.020000%",
                    "trade_count": 2,
                },
                {
                    "strategy_id": "experimental_ts_momentum_shadow_v2",
                    "strategy_name": "Trend Momentum - v2 shadow audit",
                    "role": "paperops_shadow",
                    "daily_return_pct": "5.000000%",
                    "trade_count": 1,
                },
            ],
        }
    )

    official_section, experimental_section = html.split(
        "<h2>Experimental / audit evidence</h2>"
    )
    assert "Trend Momentum" in official_section
    assert "v2 shadow audit" not in official_section
    assert "v2 shadow audit" in experimental_section
    assert "excluded from official strategy counts" in experimental_section
