from __future__ import annotations

import hashlib
import json

import pytest

from intraday_scanner.observation.ops05_historical_bars import (
    PANEL_SYMBOLS,
    TRUSTED_CAPTURE_STATE_ROOT,
    Ops05Error,
    build_windows,
    produce_historical_bars,
    select_movers,
)
from intraday_scanner.providers.base import IntradayPage


class FakeProvider:
    provider_name = "alpaca"
    feed = "sip"

    def __init__(self, bars_pages, corporate_actions=None):
        self.bars_pages = list(bars_pages)
        self.corporate_actions = list(corporate_actions or [{"items": []}])
        self.bar_index = 0
        self.ca_index = 0

    def _page(self, endpoint, pages, index):
        value = pages[index]
        return IntradayPage(
            provider="alpaca",
            feed="sip",
            endpoint=endpoint,
            items=tuple(value["items"]),
            next_page_token=value.get("next_page_token"),
            raw_payload_hash_sha256=value.get("hash", "a" * 64),
        )

    def get_bars_page(self, symbols, start, end, config, *, page_token=None):
        page = self._page("bars", self.bars_pages, self.bar_index)
        self.bar_index += 1
        return page

    def get_corporate_actions_page(self, symbols, start, end, config, *, page_token=None):
        page = self._page("corporate_actions", self.corporate_actions, self.ca_index)
        self.ca_index += 1
        return page


def _bar(symbol: str, timestamp: str) -> dict[str, object]:
    return {
        "symbol": symbol,
        "t": timestamp,
        "o": 10,
        "h": 11,
        "l": 9,
        "c": 10.5,
        "v": 100,
    }


def _census(count: int = 18) -> list[dict[str, str]]:
    memberships = ("selected", "rejected", "unselected")
    return [
        {"symbol": f"M{index:03d}", "membership": memberships[index % 3]} for index in range(count)
    ]


def test_windows_include_full_session_prior_close_and_ca_window() -> None:
    windows = build_windows("2026-09-09")
    assert windows["full_session"].start.isoformat() == "2026-09-09T13:30:00+00:00"
    assert windows["full_session"].end.isoformat() == "2026-09-09T20:00:00+00:00"
    assert windows["prior_close"].start.isoformat() == "2026-09-08T19:30:00+00:00"
    assert windows["prior_close"].end.isoformat() == "2026-09-08T20:00:00+00:00"
    assert windows["corporate_actions"].start.date().isoformat() == "2026-09-08"


def test_sampling_retains_full_census_and_all_strata(tmp_path) -> None:
    rows, receipt = select_movers(_census())
    assert len(rows) == 18
    assert len(receipt["sampled_symbols"]) == 12
    assert {row["membership"] for row in rows if row["sampled_for_bars"]} == {
        "selected",
        "rejected",
        "unselected",
    }
    assert all(row["inclusion_probability"] > 0 for row in rows if row["sampled_for_bars"])
    assert all("sampled_for_bars" in row for row in rows)


def test_sampling_allocates_the_fourth_missing_input_stratum() -> None:
    census = [
        *({"symbol": f"S{index:03d}", "membership": "selected"} for index in range(2)),
        *({"symbol": f"R{index:03d}", "membership": "rejected"} for index in range(10)),
        *({"symbol": f"U{index:03d}", "membership": "unselected"} for index in range(167)),
        *({"symbol": f"M{index:03d}", "membership": "missing_input"} for index in range(2)),
    ]
    rows, receipt = select_movers(census)
    assert len(rows) == 181
    assert receipt["quotas"] == {
        "selected": 2,
        "rejected": 4,
        "unselected": 4,
        "missing_input": 2,
    }
    assert sum(row["sampled_for_bars"] for row in rows) == 12
    assert {row["membership"] for row in rows if row["sampled_for_bars"]} == {
        "selected",
        "rejected",
        "unselected",
        "missing_input",
    }


def test_producer_writes_bounded_delayed_panel_and_empty_ca(tmp_path) -> None:
    census = _census()
    rows, sample = select_movers(census)
    full_time = "2026-09-09T13:30:00Z"
    prior_time = "2026-09-08T19:59:00Z"
    full_symbols = list(PANEL_SYMBOLS) + sample["sampled_symbols"]
    provider = FakeProvider(
        [
            {"items": [_bar(symbol, full_time) for symbol in full_symbols]},
            {"items": [_bar(symbol, prior_time) for symbol in full_symbols]},
        ]
    )
    receipt = produce_historical_bars(
        market_date="2026-09-09",
        census=census,
        provider=provider,
        config=object(),
        output_root=tmp_path,
        source_config_hash="a" * 64,
        capture_receipt_hash="b" * 64,
    )
    assert receipt["status"] == "CAPTURED"
    assert receipt["coverage"]["bar_count"] == len(full_symbols) * 2
    assert receipt["coverage"]["decision_eligible"] is False
    assert receipt["coverage"]["cross_close_censored"] is True
    assert receipt["corporate_actions"]["status"] == "EMPTY"
    assert receipt["consumer_handoff"]["status"] == "ADAPTER_REQUIRED"
    assert receipt["consumer_handoff"]["eligible_labels"] == 0
    assert (tmp_path / "raw-bars.jsonl").is_file()
    assert len((tmp_path / "universe-census.json").read_text().splitlines()) > 1


def test_lower_byte_profile_is_recorded_and_higher_resume_is_rejected(tmp_path) -> None:
    census = _census()
    full_time = "2026-09-09T13:30:00Z"
    prior_time = "2026-09-08T19:59:00Z"
    symbols = list(PANEL_SYMBOLS) + select_movers(census)[1]["sampled_symbols"]
    source_hash = hashlib.sha256(str(tmp_path).encode()).hexdigest()
    capture_hash = hashlib.sha256((str(tmp_path) + "capture").encode()).hexdigest()
    provider = FakeProvider(
        [
            {"items": [_bar(symbol, full_time) for symbol in symbols]},
            {"items": [_bar(symbol, prior_time) for symbol in symbols]},
        ]
    )
    receipt = produce_historical_bars(
        market_date="2026-09-09",
        census=census,
        provider=provider,
        config=object(),
        output_root=tmp_path,
        source_config_hash=source_hash,
        capture_receipt_hash=capture_hash,
        max_bytes=1024 * 1024,
        resume_across_roots=True,
    )
    assert receipt["limits"]["max_bytes"] == 1024 * 1024
    states = list(TRUSTED_CAPTURE_STATE_ROOT.rglob("state.json"))
    assert any(json.loads(state.read_text()).get("max_bytes") == 1024 * 1024 for state in states)
    with pytest.raises(Ops05Error, match="configured byte limit"):
        produce_historical_bars(
            market_date="2026-09-09",
            census=census,
            provider=provider,
            config=object(),
            output_root=tmp_path,
            source_config_hash=source_hash,
            capture_receipt_hash=capture_hash,
            max_bytes=2 * 1024 * 1024,
            resume_across_roots=True,
        )


def test_lower_byte_profile_fails_before_page_commit(tmp_path) -> None:
    class Oversize(FakeProvider):
        def __init__(self):
            super().__init__([{"items": []}, {"items": []}])

        def get_bars_page(self, symbols, start, end, config, *, page_token=None):
            return IntradayPage(
                provider="alpaca",
                feed="sip",
                endpoint="bars",
                items=tuple(_bar("SPY", "2026-09-09T13:30:00Z") for _ in range(20000)),
                next_page_token=None,
                raw_payload_hash_sha256="c" * 64,
            )

    with pytest.raises(Ops05Error, match="configured byte bound"):
        produce_historical_bars(
            market_date="2026-09-09",
            census=_census(),
            provider=Oversize(),
            config=object(),
            output_root=tmp_path,
            source_config_hash="a" * 64,
            capture_receipt_hash="b" * 64,
            max_bytes=1024 * 1024,
        )


def test_provider_inclusive_boundary_is_preserved_as_excluded_reference(tmp_path) -> None:
    census = _census(3)
    windows = build_windows("2026-09-09")
    end = windows["full_session"].end.isoformat().replace("+00:00", "Z")
    provider = FakeProvider(
        [{"items": [_bar("SPY", end)]}, {"items": []}],
    )
    receipt = produce_historical_bars(
        market_date="2026-09-09",
        census=census,
        provider=provider,
        config=object(),
        output_root=tmp_path,
        source_config_hash="a" * 64,
        capture_receipt_hash="b" * 64,
    )
    assert receipt["coverage"]["boundary_event_count"] == 1
    assert receipt["coverage"]["bar_count"] == 0
    assert (
        "provider_inclusive_boundary_excluded" in (tmp_path / "boundary-events.jsonl").read_text()
    )


def test_out_of_window_event_fails_closed(tmp_path) -> None:
    provider = FakeProvider(
        [{"items": [_bar("SPY", "2026-09-09T13:29:00Z")]}, {"items": []}],
    )
    with pytest.raises(Ops05Error, match="out-of-window"):
        produce_historical_bars(
            market_date="2026-09-09",
            census=_census(3),
            provider=provider,
            config=object(),
            output_root=tmp_path,
            source_config_hash="a" * 64,
            capture_receipt_hash="b" * 64,
        )


def test_wrong_feed_is_rejected_before_collection(tmp_path) -> None:
    provider = FakeProvider([], [])
    provider.feed = "iex"
    with pytest.raises(Ops05Error, match="no feed fallback"):
        produce_historical_bars(
            market_date="2026-09-09",
            census=_census(3),
            provider=provider,
            config=object(),
            output_root=tmp_path,
            source_config_hash="a" * 64,
            capture_receipt_hash="b" * 64,
        )


def test_total_page_attempts_are_bounded_and_recorded(tmp_path) -> None:
    census = _census(3)
    full_time = "2026-09-09T13:30:00Z"
    prior_time = "2026-09-08T19:59:00Z"

    class Flaky(FakeProvider):
        def __init__(self):
            super().__init__(
                [
                    {"items": [_bar("SPY", full_time)]},
                    {"items": [_bar("SPY", prior_time)]},
                ]
            )
            self.failures = 2

        def get_bars_page(self, symbols, start, end, config, *, page_token=None):
            if self.failures:
                self.failures -= 1
                raise RuntimeError("temporary")
            return super().get_bars_page(symbols, start, end, config, page_token=page_token)

    receipt = produce_historical_bars(
        market_date="2026-09-09",
        census=census,
        provider=Flaky(),
        config=object(),
        output_root=tmp_path,
        source_config_hash="a" * 64,
        capture_receipt_hash="b" * 64,
    )
    assert receipt["pages"][0]["attempts"] == 3
    assert all(page["attempts"] <= 3 for page in receipt["pages"])
