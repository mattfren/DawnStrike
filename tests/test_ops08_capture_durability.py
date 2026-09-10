from __future__ import annotations

import json

import pytest

import intraday_scanner.observation.ops05_historical_bars as ops05
from intraday_scanner.observation.ops05_historical_bars import (
    PANEL_SYMBOLS,
    Ops05Error,
    produce_historical_bars,
    select_movers,
)
from intraday_scanner.providers.base import IntradayPage


def _bar(symbol: str, timestamp: str) -> dict[str, object]:
    return {"symbol": symbol, "t": timestamp, "o": 10, "h": 11, "l": 9, "c": 10.5, "v": 100}


def _census() -> list[dict[str, str]]:
    return [{"symbol": "M001", "membership": "selected"}]


class DurableProvider:
    provider_name = "alpaca"
    feed = "sip"

    def __init__(self, *, interrupt_after_first: bool = False, fail_ca: bool = False) -> None:
        _rows, sample = select_movers(_census())
        symbols = [*PANEL_SYMBOLS, *sample["sampled_symbols"]]
        self.bars = [
            {"items": [_bar(symbol, "2026-09-09T13:30:00Z") for symbol in symbols]},
            {"items": [_bar(symbol, "2026-09-08T19:59:00Z") for symbol in symbols]},
        ]
        self.calls = 0
        self.ca_calls = 0
        self.interrupt_after_first = interrupt_after_first
        self.fail_ca = fail_ca

    def get_bars_page(self, symbols, start, end, config, *, page_token=None):
        if self.interrupt_after_first and self.calls == 1:
            self.calls += 1
            raise KeyboardInterrupt("in-flight interruption")
        page = self.bars[0 if "13:30" in start else 1]
        self.calls += 1
        return IntradayPage(
            provider="alpaca", feed="sip", endpoint="bars", items=tuple(page["items"]),
            next_page_token=None, raw_payload_hash_sha256="a" * 64,
        )

    def get_corporate_actions_page(self, symbols, start, end, config, *, page_token=None):
        self.ca_calls += 1
        if self.fail_ca:
            raise RuntimeError("late CA failure")
        return IntradayPage(
            provider="alpaca", feed="sip", endpoint="corporate_actions", items=tuple(),
            next_page_token=None, raw_payload_hash_sha256="b" * 64,
        )


def _run(root, provider, **kwargs):
    return produce_historical_bars(
        market_date="2026-09-09", census=_census(), provider=provider, config=object(),
        output_root=root, source_config_hash="a" * 64, capture_receipt_hash="b" * 64, **kwargs,
    )


def test_interruption_after_page_commit_resumes_without_redownload(tmp_path) -> None:
    root = tmp_path / "capture"
    with pytest.raises(KeyboardInterrupt):
        _run(root, DurableProvider(interrupt_after_first=True))
    state = json.loads((root / "capture-state.json").read_text())
    assert state["status"] == "PARTIAL"
    assert state["page_count"] == 1
    assert any(item["kind"] == "request_attempt_uncertain" for item in state["journal"])
    resumed = _run(root, DurableProvider())
    assert resumed["status"] == "CAPTURED"
    assert resumed["coverage"]["page_count"] == 3


def test_late_ca_failure_keeps_prior_bars_typed_partial(tmp_path) -> None:
    root = tmp_path / "capture"
    with pytest.raises(Ops05Error, match="failed after"):
        _run(root, DurableProvider(fail_ca=True))
    partial = json.loads((root / "partial-receipt.json").read_text())
    assert partial["status"] == "PARTIAL"
    assert partial["failure"]["phase"] == "corporate_actions"
    assert partial["prior_pages_retained"] is True
    assert partial["derived_bar_count"] > 0
    assert (root / "partial" / "raw-bars.jsonl").is_file()


def test_matching_replay_is_noop_and_tampered_output_is_rejected(tmp_path) -> None:
    root = tmp_path / "capture"
    first = _run(root, DurableProvider())
    second = _run(root, DurableProvider())
    assert second["raw_event_stream_sha256"] == first["raw_event_stream_sha256"]
    raw = root / "raw-bars.jsonl"
    raw.write_text(raw.read_text() + "{}\n")
    with pytest.raises(Ops05Error, match="raw artifact changed"):
        _run(root, DurableProvider())


def test_shared_identity_prevents_source_config_reset_in_new_output_root(tmp_path) -> None:
    _run(tmp_path / "one", DurableProvider(), resume_across_roots=True)
    with pytest.raises(Ops05Error, match="identity conflicts"):
        produce_historical_bars(
            market_date="2026-09-09", census=_census(), provider=DurableProvider(), config=object(),
            output_root=tmp_path / "two", source_config_hash="c" * 64,
            capture_receipt_hash="b" * 64, resume_across_roots=True,
        )


def test_page_tamper_and_stale_owner_are_fail_closed(tmp_path) -> None:
    root = tmp_path / "capture"
    with pytest.raises(KeyboardInterrupt):
        _run(root, DurableProvider(interrupt_after_first=True))
    state = json.loads((root / "capture-state.json").read_text())
    record = next(iter(state["pages"].values()))
    page_path = tmp_path / ".ops05-capture-ledger"
    page_files = list(page_path.rglob(record["page_path"].split("/")[-1]))
    assert len(page_files) == 1
    page = json.loads(page_files[0].read_text())
    page["raw_payload_items"][0]["c"] = 999
    page_files[0].write_text(json.dumps(page))
    with pytest.raises(Ops05Error, match="payload hash changed"):
        _run(root, DurableProvider())

    # A stale owner is never silently reclaimed; this is an admin decision.
    fresh = root
    stale_locks = list((tmp_path / ".ops05-capture-ledger").rglob("capture.lock"))
    assert stale_locks == []  # completed runs release their lock
    windows = ops05.build_windows("2026-09-09")
    rows, _ = ops05.select_movers(_census())
    identity = {
        "market_date": "2026-09-09", "provider": "alpaca", "feed": "sip",
        "source_config_sha256": "a" * 64, "capture_receipt_sha256": "b" * 64,
        "census_sha256": ops05._sha256(rows),
        "windows": {key: value.as_dict() for key, value in windows.items()},
    }
    durable = ops05._DurableCapture(root=fresh, identity=identity)
    durable.ledger.mkdir(parents=True, exist_ok=True)
    durable.lock_path.write_text(json.dumps({"pid": 99999999}))
    with pytest.raises(Ops05Error, match="stale capture owner"):
        _run(fresh, DurableProvider())
