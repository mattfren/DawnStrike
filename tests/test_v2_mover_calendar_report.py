from __future__ import annotations

from pathlib import Path

import pytest

from intraday_scanner.v2.mover_pattern_lab.calendar_report import (
    render_strategy_calendar_report,
    write_strategy_calendar_report,
)


def _row(**overrides: object) -> dict[str, object]:
    row: dict[str, object] = {
        "market_date": "2026-07-17",
        "strategy_id": "mover_opening_drive_rvol_v1",
        "strategy_version": "v1.0",
        "evidence_mode": "forward_observation",
        "status": "complete",
        "paper_book_return_pct": 0.2468,
        "pnl": 2.468,
        "capital_deployed": 1_000.0,
        "decision_count": 7,
        "signal_count": 1,
        "closed_trade_count": 1,
        "pending_trade_count": 0,
        "not_entered_count": 0,
        "symbols": ["ABCD"],
        "return_semantics": "after-cost paper P&L / deployed capital",
        "learning_eligible": True,
    }
    row.update(overrides)
    return row


def test_report_is_clickable_self_contained_and_separates_evidence_modes() -> None:
    replay = _row(
        evidence_mode="historical_replay",
        paper_book_return_pct=-1.234,
        pnl=-12.34,
        learning_eligible=False,
    )
    html = render_strategy_calendar_report(
        {
            "schema_version": "mover-pattern-lab.v1.analysis",
            "strategy_daily_calendar": [_row(), replay],
        }
    )

    assert "<details class=\"day-card" in html
    assert "<summary aria-label=" in html
    assert "Forward observation" in html
    assert "Historical replay" in html
    assert "+0.25%" in html
    assert "-1.23%" in html
    assert "Opening Drive + Same-Clock RVOL" in html
    assert "No broker execution" in html
    assert "<script" not in html.lower()
    assert " src=" not in html.lower()
    assert "https://" not in html.lower()


@pytest.mark.parametrize("status", ["not_evaluated", "skipped", "incomplete"])
def test_missing_or_skipped_truth_is_an_em_dash_never_zero(status: str) -> None:
    html = render_strategy_calendar_report(
        {
            "strategy_daily_calendar": [
                _row(
                    status=status,
                    paper_book_return_pct=0.0,
                    pnl=0.0,
                    capital_deployed=0.0,
                    learning_eligible=False,
                )
            ]
        }
    )

    assert "Paper book return: not evaluated" in html
    assert "Not evaluated" in html or "Incomplete" in html
    assert "0.00%" not in html
    assert "$0.00 / $0.00" not in html
    assert "&mdash;" in html or "—" in html


def test_evaluated_cash_day_may_truthfully_show_zero() -> None:
    html = render_strategy_calendar_report(
        {
            "strategy_daily_calendar": [
                _row(
                    status="no_setup",
                    paper_book_return_pct=0.0,
                    pnl=0.0,
                    capital_deployed=0.0,
                    signal_count=0,
                    closed_trade_count=0,
                    learning_eligible=False,
                )
            ]
        }
    )

    assert "0.00%" in html
    assert "No setup" in html


def test_all_dynamic_text_is_html_escaped() -> None:
    html = render_strategy_calendar_report(
        {
            "schema_version": '<img src=x onerror="bad()">',
            "strategy_daily_calendar": [
                _row(
                    strategy_id="<script>alert(1)</script>",
                    symbols=["<svg/onload=bad()>", "SAFE"],
                    return_semantics="<b>not markup</b>",
                )
            ],
        },
        title='<img src=x onerror="bad()">',
    )

    assert "<script>alert" not in html
    assert "<svg/onload" not in html
    assert "<b>not markup</b>" not in html
    assert "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert "&lt;svg/onload=bad()&gt;" in html
    assert "&lt;b&gt;not markup&lt;/b&gt;" in html
    assert "&lt;img src=x onerror=&quot;bad()&quot;&gt;" in html


def test_empty_calendar_does_not_fabricate_a_zero_return() -> None:
    html = render_strategy_calendar_report({"strategy_daily_calendar": []})

    assert "No retained strategy calendar" in html
    assert "No return is shown" in html
    assert "0.00%" not in html


def test_write_helper_is_deterministic_utf8(tmp_path: Path) -> None:
    payload = {"strategy_daily_calendar": [_row()]}
    output = tmp_path / "nested" / "strategy-calendar.html"

    first_path = write_strategy_calendar_report(payload, output)
    first = output.read_bytes()
    second_path = write_strategy_calendar_report(payload, output)
    second = output.read_bytes()

    assert first_path == output.resolve()
    assert second_path == output.resolve()
    assert first == second
    assert first.startswith(b"<!doctype html>\n")
    assert "Dawnstrike Strategy Calendar" in first.decode("utf-8")


def test_duplicate_strategy_day_is_rejected() -> None:
    row = _row()
    with pytest.raises(ValueError, match="duplicate strategy calendar row"):
        render_strategy_calendar_report({"strategy_daily_calendar": [row, row]})


def test_invalid_date_and_negative_counts_fail_closed() -> None:
    with pytest.raises(ValueError, match="invalid market_date"):
        render_strategy_calendar_report(
            {"strategy_daily_calendar": [_row(market_date="not-a-date")]}
        )
    with pytest.raises(ValueError, match="negative count"):
        render_strategy_calendar_report(
            {"strategy_daily_calendar": [_row(signal_count=-1)]}
        )
