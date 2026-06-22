from __future__ import annotations

from pathlib import Path

from intraday_scanner.dashboard.components import (
    calendar_day_card,
    display_pick_from_raw,
    evidence_status_card,
    main_pick_card,
    no_pick_message,
    outcome_needed_panel,
    risk_summary_panel,
    simple_avoid_table,
    status_banner,
    top_three_cards,
)
from intraday_scanner.dashboard.data_loader import _attach_display_ready, load_sqlite
from intraday_scanner.dashboard.display_text import (
    evidence_status,
    no_trade_reason,
    source_label,
    translate_label,
)


def _raw_pick(ticker: str, rank: int, *, score: float = 80.0) -> dict[str, object]:
    return {
        "rank": rank,
        "ticker": ticker,
        "company": f"{ticker} Inc",
        "setup_key": "LOW_CONFIDENCE",
        "confidence_bucket": "INSUFFICIENT_SAMPLE",
        "risk_flags": "url_table_unverified;no_previous_close",
        "source": "web_url",
        "data_source_kind": "web_url",
        "alpha_score": score,
        "gap_pct": 42.5,
        "premarket_price": 5.25,
        "breakout_trigger": 5.5,
        "invalidation_level": 4.8,
        "first_target": 6.2,
        "api_key": "SECRET_TOKEN",
        "telegram_chat_id": "SECRET_CHAT",
    }


def test_display_text_translates_operator_labels() -> None:
    assert translate_label("LOW_CONFIDENCE") == "Low confidence"
    assert translate_label("INSUFFICIENT_SAMPLE") == "Not enough history yet"
    assert translate_label("NO_EDGE") == "No clear edge"
    assert translate_label("no_previous_close") == "Previous close missing"
    assert no_trade_reason("Clean") == (
        "No hard risk flags, but confidence was not high enough."
    )
    assert source_label("web_url") == "Unverified free web data"


def test_today_display_text_hides_raw_technical_labels_and_secrets() -> None:
    pick = display_pick_from_raw(_raw_pick("NOVA", 1))
    html = "".join(
        [
            status_banner(
                {
                    "variant": "green",
                    "title": "Clean Watchlist Found",
                    "explanation": "Watch the levels manually. No orders are placed.",
                }
            ),
            main_pick_card(pick),
            top_three_cards([pick]),
            risk_summary_panel(
                {
                    "avoid_count": 1,
                    "top_avoid_reason": "Clean",
                    "data_warning_count": 0,
                    "missing_outcome_count": 0,
                }
            ),
        ]
    )

    assert "LOW_CONFIDENCE" not in html
    assert "INSUFFICIENT_SAMPLE" not in html
    assert "No-Trade Reason: Clean" not in html
    assert "Broker Watch" not in html
    assert "SECRET_TOKEN" not in html
    assert "SECRET_CHAT" not in html
    assert "Low confidence" in html
    assert "Not enough history yet" in html
    assert "Watch Level" in html
    assert "Exit Line" in html


def test_display_ready_payload_handles_picks_no_trade_no_data_and_missing_outcomes() -> None:
    picks_payload = {
        "summary": {"created_at": "2026-06-21T13:30:00+00:00"},
        "top_explosive": [_raw_pick("NOVA", 1), _raw_pick("RIFT", 2), _raw_pick("MOON", 3)],
        "ranked_candidates": [],
        "avoid_list": [_raw_pick("HALT", 1, score=0)],
    }
    _attach_display_ready(picks_payload)
    assert picks_payload["latest_status"]["kind"] == "clean_watchlist"
    assert picks_payload["main_pick"]["ticker"] == "NOVA"
    assert len(picks_payload["top_three"]) == 3

    no_trade_payload = {
        "summary": {"created_at": "2026-06-21T13:30:00+00:00"},
        "alpha_signals": [{"ticker": "SKIP", "no_trade_reason": "LOW_CONFIDENCE"}],
    }
    _attach_display_ready(no_trade_payload)
    assert no_trade_payload["latest_status"]["kind"] == "no_clean_edge"
    assert "Low confidence" in no_trade_payload["latest_status"]["explanation"]

    empty_payload: dict[str, object] = {}
    _attach_display_ready(empty_payload)
    assert empty_payload["latest_status"]["kind"] == "no_clean_edge"
    assert "confidence was not high enough" in empty_payload["latest_status"]["explanation"]

    missing_payload = {
        "calendar_missing_outcomes": [
            {
                "ticker": "NOVA",
                "expected_path": r"data\inbox\outcomes\outcomes_2026-06-21.csv",
            }
        ]
    }
    _attach_display_ready(missing_payload)
    assert missing_payload["latest_status"]["kind"] == "outcome_needed"
    assert "Outcome Needed" in outcome_needed_panel(missing_payload["missing_outcomes"])


def test_top_three_and_avoid_tables_stay_compact() -> None:
    picks = [display_pick_from_raw(_raw_pick(f"PICK{i}", i)) for i in range(1, 6)]
    html = top_three_cards(picks)
    avoid = [
        {"ticker": f"BAD{i}", "why_avoid": "Halt status not checked", "gap_pct": i}
        for i in range(10)
    ]

    assert html.count("ds-watch-card") == 3
    assert "PICK4" not in html
    assert len(simple_avoid_table(avoid)) == 5
    assert simple_avoid_table(avoid)[0]["Why avoid?"] == "Halt status not checked"


def test_calendar_day_and_performance_evidence_are_plain_english() -> None:
    pending = calendar_day_card(
        {
            "date": "2026-06-21",
            "status_label": "Picks pending",
            "top_pick": "NOVA",
            "top3_return_label": "Pending",
            "missing_outcome_count": 1,
        }
    )
    evidence = evidence_status_card({"real_days": 5, "audited_days": 19})

    assert "Picks pending" in pending
    assert "Outcome Needed" in pending
    assert evidence_status(19) == "Not enough real days yet"
    assert "Not enough real days yet" in evidence


def test_loader_handles_empty_database(tmp_path: Path) -> None:
    db_path = tmp_path / "empty.sqlite"
    data = load_sqlite(db_path)

    assert data["latest_status"]["kind"] == "no_clean_edge"
    assert data["top_three"] == []
    assert data["system_health"]["status"] == "Ready"


def test_loader_handles_active_shadow_real_database_if_present() -> None:
    db_path = Path("data/shadow_real.sqlite")
    if not db_path.exists():
        return

    data = load_sqlite(db_path)

    assert "latest_status" in data
    assert "main_pick" in data
    assert "performance_summary" in data
    assert "system_health" in data


def test_simple_dashboard_code_does_not_add_order_execution() -> None:
    checked_paths = [
        Path("intraday_scanner/dashboard/components.py"),
        Path("intraday_scanner/dashboard/data_loader.py"),
        Path("app.py"),
    ]
    forbidden = [
        "TradingClient",
        "submit_order",
        "place_order",
        "create_order",
        "market_order",
        "limit_order",
        "execute_trade",
    ]
    combined = "\n".join(path.read_text(encoding="utf-8") for path in checked_paths)

    assert not any(term in combined for term in forbidden)
    assert "No orders placed" in combined
    assert no_pick_message("Clean") == (
        "No hard risk flags, but confidence was not high enough."
    )
