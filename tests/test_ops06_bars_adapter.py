from __future__ import annotations

import hashlib
import json

import pytest

from intraday_scanner.cli import main
from intraday_scanner.observation.ops05_historical_bars import (
    PANEL_SYMBOLS,
    produce_historical_bars,
)
from intraday_scanner.observation.ops06_bars_adapter import adapt_ops05_to_r3
from intraday_scanner.providers.base import IntradayPage


class FakeProvider:
    provider_name = "alpaca"
    feed = "sip"

    def __init__(self, pages):
        self.pages = list(pages)
        self.index = 0

    def get_bars_page(self, symbols, start, end, config, *, page_token=None):
        row = self.pages[self.index]
        self.index += 1
        return IntradayPage("alpaca", "sip", "bars", tuple(row), None, "a" * 64)

    def get_corporate_actions_page(self, symbols, start, end, config, *, page_token=None):
        return IntradayPage("alpaca", "sip", "corporate_actions", (), None, "b" * 64)


def _bar(symbol: str, timestamp: str, close: float) -> dict[str, object]:
    return {
        "symbol": symbol,
        "t": timestamp,
        "o": close - 1,
        "h": close + 1,
        "l": close - 2,
        "c": close,
        "v": 100,
    }


def _archive(tmp_path):
    census = [{"symbol": "M001", "membership": "selected"}]
    symbols = [*PANEL_SYMBOLS, "M001"]
    pages = [
        [
            item
            for symbol in symbols
            for item in (
                _bar(symbol, "2026-09-09T13:30:00Z", 101),
                _bar(symbol, "2026-09-09T14:30:00Z", 102),
                _bar(symbol, "2026-09-09T17:30:00Z", 103),
            )
        ],
        [_bar(symbol, "2026-09-08T19:59:00Z", 99) for symbol in symbols],
    ]
    receipt = produce_historical_bars(
        market_date="2026-09-09",
        census=census,
        provider=FakeProvider(pages),
        config=object(),
        output_root=tmp_path / "ops05",
        source_config_hash="a" * 64,
        capture_receipt_hash="b" * 64,
    )
    raw = (tmp_path / "ops05" / "raw-bars.jsonl").read_bytes()
    source = {
        "market_date": "2026-09-09",
        "source": receipt["source_lineage"],
        "raw": hashlib.sha256(raw).hexdigest(),
    }
    generation = (
        "ops05-r3-"
        + hashlib.sha256(
            json.dumps(source, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()[:24]
    )
    decision = {
        "decision_id": "ops06-positive",
        "market_date": "2026-09-09",
        "decision_at": "2026-09-09T13:30:00+00:00",
        "ticker": "SPY",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "model_version": "fixture-model",
        "feature_schema_version": "fixture-feature",
        "feature_hash_sha256": "c" * 64,
        "input_hash_sha256": "d" * 64,
        "source_lineage_hash_sha256": "e" * 64,
        "action": "SHADOW_TRACK",
        "decision_state": "SELECTED",
        "research_only": True,
        "broker_execution_enabled": False,
        "safety_vetoes": [],
        "score_components": {},
        "uncertainty": {},
        "execution_assumptions": {},
        "universe_membership": {"universe_id": generation, "source_lineage_hash_sha256": "f" * 64},
        "point_in_time": {
            "all_inputs_observed_at_or_before_decision": True,
            "feature_timestamp": "2026-09-09T13:29:00+00:00",
            "feature_available_at": "2026-09-09T13:29:30+00:00",
            "feature_ingested_at": "2026-09-09T13:29:45+00:00",
        },
    }
    decision_path = tmp_path / "decisions.json"
    decision_path.write_text(json.dumps({"v6_decision_records": [decision]}), encoding="utf-8")
    return tmp_path / "ops05", decision_path


def test_ops05_archive_adapts_to_matured_label_only_horizons(tmp_path) -> None:
    root, decisions = _archive(tmp_path)
    result = adapt_ops05_to_r3(
        observation_root=root, decision_artifact=decisions, as_of="2026-09-10T00:00:00+00:00"
    )
    packet = result["adapter_packet"]
    assert result["status"] == "READY"
    assert packet["status"] == "READY"
    assert packet["labels"][0]["eligibility_state"] == "LABEL_ONLY_DELAYED_SOURCE"
    assert packet["labels"][0]["learning_eligible"] is False
    first_event = json.loads((root / "r3-adapter" / "raw-events.jsonl").read_text().splitlines()[0])
    assert first_event["source"] == "alpaca:sip"
    assert [row["status"] for row in packet["horizon_summary"][0]] == [
        "MATURED_DELAYED_LABEL",
        "MATURED_DELAYED_LABEL",
        "CENSORED_BY_SESSION_CLOSE",
    ]
    assert packet["ops06_adapter"]["page_hashes_verified"] == 3


def test_ops05_archive_routes_through_public_daily_cli(tmp_path, capsys) -> None:
    root, decisions = _archive(tmp_path)
    assert (
        main(
            [
                "alpha-v6-daily-monitor",
                "--db-path",
                str(tmp_path / "state.sqlite"),
                "--market-date",
                "2026-09-09",
                "--observation-root",
                str(root),
                "--decision-artifact",
                str(decisions),
            ]
        )
        == 0
    )
    report = json.loads(capsys.readouterr().out)
    assert report["observation_dataset"]["status"] == "READY"
    assert report["observation_dataset"]["research_only"] is True
    assert report["observation_dataset"]["broker_execution_enabled"] is False


def test_ops05_raw_hash_swap_is_rejected(tmp_path) -> None:
    root, decisions = _archive(tmp_path)
    raw = root / "raw-bars.jsonl"
    raw.write_text(
        raw.read_text(encoding="utf-8").replace('"close": 101', '"close": 100'), encoding="utf-8"
    )
    try:
        adapt_ops05_to_r3(observation_root=root, decision_artifact=decisions)
    except ValueError as exc:
        assert "raw event content does not match receipt" in str(exc)
    else:
        raise AssertionError("tampered OPS05 bytes were accepted")


def test_same_length_page_body_swap_is_rejected(tmp_path) -> None:
    root, decisions = _archive(tmp_path)
    receipt_path = root / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["pages"][0]["raw_payload_items"][0]["c"] = 999.0
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="page body does not match"):
        adapt_ops05_to_r3(observation_root=root, decision_artifact=decisions)


def test_recomputed_raw_stream_cannot_change_row_source_identity(tmp_path) -> None:
    root, decisions = _archive(tmp_path)
    raw_path = root / "raw-bars.jsonl"
    rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines()]
    rows[0]["source_artifact_hash_sha256"] = "c" * 64
    raw_path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    receipt_path = root / "receipt.json"
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    receipt["raw_event_stream_sha256"] = hashlib.sha256(
        json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    with pytest.raises(ValueError, match="capture binding does not match receipt"):
        adapt_ops05_to_r3(observation_root=root, decision_artifact=decisions)
