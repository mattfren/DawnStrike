"""SEC risk checking must spend its budget on the rows that can be traded.

``sec_risk_status`` is one of four evidence fields the alert gate hard-blocks
on: anything but CLEAR/OK/PASS/VERIFIED appends "SEC risk status is not
verified clear" and the signal can never alert. Only tickers that were actually
checked get a status.

The check is one HTTP request per ticker, so the list is capped - reasonably.
What was not reasonable is that the cap was applied as ``deduped[:20]``, an
unordered slice of the source merge. A live collection on 2026-09-05 produced
166 merged rows, of which 146 kept ``sec_risk_status='UNKNOWN'`` purely
because they landed past index 19. The slice, not the strategy, decided which
candidates could ever be alertable.

The budget now goes to the rows the strategy actually selects on - largest gap
first, then dollar volume - and the cap is configurable.
"""

from __future__ import annotations

from typing import Any

import pytest

from intraday_scanner.services import web_collection_service as wcs


class _Source:
    """Minimal stand-in for the resolved `sec_edgar` WebSourceConfig."""

    def __init__(self, params: dict[str, Any] | None = None) -> None:
        self.name = "sec_edgar"
        self.params = params or {}


def _row(ticker: str, gap: float, dollar_volume: float) -> dict[str, Any]:
    return {"ticker": ticker, "gap_pct": gap, "dollar_volume": dollar_volume}


@pytest.fixture
def captured(monkeypatch) -> list[list[str]]:
    """Record the ticker list handed to the SEC collector."""

    seen: list[list[str]] = []

    def _fake_collect_sec_risk(*, tickers: list[str], **_: Any) -> dict[str, Any]:
        seen.append(list(tickers))
        return {"status": "success", "events": [], "checked_tickers": list(tickers)}

    monkeypatch.setattr(wcs, "collect_sec_risk", _fake_collect_sec_risk)
    return seen


def _collect(monkeypatch, rows, source: _Source | None) -> None:
    monkeypatch.setattr(wcs, "get_source", lambda config, name: source)
    wcs._maybe_collect_sec(object(), wcs.Path("."), None, rows, False)


def test_budget_goes_to_the_largest_gaps_not_the_merge_order(monkeypatch, captured):
    """The old slice would have checked SLOW..; the strategy wants GAPA."""

    rows = [_row(f"SLOW{index}", 0.5, 1_000.0) for index in range(70)]
    rows.append(_row("GAPA", 93.0, 5_000_000.0))
    rows.append(_row("GAPB", 41.0, 4_000_000.0))

    _collect(monkeypatch, rows, _Source({"max_tickers": 3}))

    assert captured[-1][:2] == ["GAPA", "GAPB"], (
        "the two largest gappers in the collection were not checked"
    )


def test_negative_gaps_are_ranked_by_magnitude(monkeypatch, captured):
    rows = [_row("UP", 4.0, 1.0), _row("DOWN", -30.0, 1.0), _row("FLAT", 0.1, 1.0)]

    _collect(monkeypatch, rows, _Source({"max_tickers": 1}))

    assert captured[-1] == ["DOWN"]


def test_dollar_volume_breaks_a_gap_tie(monkeypatch, captured):
    rows = [_row("THIN", 20.0, 10_000.0), _row("THICK", 20.0, 9_000_000.0)]

    _collect(monkeypatch, rows, _Source({"max_tickers": 1}))

    assert captured[-1] == ["THICK"]


def test_ordering_is_deterministic_for_identical_rows(monkeypatch, captured):
    rows = [_row("ZZZ", 20.0, 1.0), _row("AAA", 20.0, 1.0)]

    _collect(monkeypatch, rows, _Source({"max_tickers": 1}))

    assert captured[-1] == ["AAA"]


def test_default_cap_covers_a_realistic_candidate_set(monkeypatch, captured):
    """A live premarket merge is ~166 rows; 20 left 146 permanently unknown."""

    rows = [_row(f"T{index:03d}", float(200 - index), 1.0) for index in range(200)]

    _collect(monkeypatch, rows, _Source())

    assert len(captured[-1]) == wcs.DEFAULT_SEC_RISK_MAX_TICKERS
    assert wcs.DEFAULT_SEC_RISK_MAX_TICKERS >= 60, (
        "the cap must cover the candidate set the scan actually enriches "
        "(premarket_enrichment_max_candidates is 60)"
    )


def test_operator_can_widen_or_narrow_the_cap(monkeypatch, captured):
    rows = [_row(f"T{index:03d}", float(100 - index), 1.0) for index in range(100)]

    _collect(monkeypatch, rows, _Source({"max_tickers": 7}))
    assert len(captured[-1]) == 7

    _collect(monkeypatch, rows, _Source({"max_tickers": 90}))
    assert len(captured[-1]) == 90


@pytest.mark.parametrize("value", ["", None, "not-a-number", 0, -5])
def test_unusable_cap_values_fall_back_to_a_safe_default(monkeypatch, captured, value):
    rows = [_row(f"T{index:03d}", float(100 - index), 1.0) for index in range(100)]

    _collect(monkeypatch, rows, _Source({"max_tickers": value}))

    checked = len(captured[-1])
    assert checked >= 1
    assert checked <= wcs.DEFAULT_SEC_RISK_MAX_TICKERS


def test_disabled_source_still_short_circuits(monkeypatch, captured):
    _collect(monkeypatch, [_row("AAA", 20.0, 1.0)], None)

    assert captured == [], "no request may be issued when the source is disabled"


def test_every_row_is_offered_when_the_collection_is_small(monkeypatch, captured):
    rows = [_row("AAA", 20.0, 1.0), _row("BBB", 10.0, 1.0)]

    _collect(monkeypatch, rows, _Source())

    assert sorted(captured[-1]) == ["AAA", "BBB"]
