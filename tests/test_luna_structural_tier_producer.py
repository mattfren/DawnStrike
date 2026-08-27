"""Focused adversarial coverage for the Luna structural/Tier producers."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

import pytest

import intraday_scanner.services.price_observation_service as price_observation_module
from intraday_scanner.alpha.plan_constructor import NO_VALID_PLAN
from intraday_scanner.alpha.v5_policy import ALPHAOPS_V5_ACCOUNT_ID
from intraday_scanner.config import load_config
from intraday_scanner.errors import DataProviderError
from intraday_scanner.providers.alpaca_provider import AlpacaProvider
from intraday_scanner.services.alpha_cycle_service import (
    _attach_authenticated_alpaca_structure,
    _build_modeled_cost_receipt,
    _signal_payload,
)
from intraday_scanner.services.luna_research_slate_service import (
    TIER1,
    _valid_modeled_cost_receipt,
    apply_publication_semantics,
    build_ranked_research_slate,
    validate_watcher_current_proof,
)
from intraday_scanner.services.premarket_enrichment_service import (
    observation_from_alpaca_bars,
)
from intraday_scanner.services.trade_watcher_service import (
    WatcherSettings,
    _build_watcher_current_proof,
    _decision_for_signal,
    _monitor_publication_receipt,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def _production_row() -> dict[str, object]:
    raw = {
        "ticker": "NOVA",
        "timestamp": "2026-08-25T00:00:00+00:00",
        "high": 12.0,
        "bar": {"t": "2026-08-25T00:00:00Z", "h": 12.0},
    }
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    observation = {
        "ticker": "NOVA",
        "status": "verified",
        "premarket_high": 10.0,
        "premarket_low": 9.0,
        "previous_close": 8.0,
        "latest_price": 9.5,
        "premarket_volume": 1000,
        "observed_at": "2026-08-26T13:00:00+00:00",
        "bar_completed_at": "2026-08-26T13:01:00+00:00",
        "is_complete": True,
        "bar_count": 1,
        "age_seconds": 30,
        "source": "alpaca_market_data_iex",
        "source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "failure_reason": "",
        "prior_daily_high": 12.0,
        "prior_daily_high_observed_at": "2026-08-25T00:00:00+00:00",
        "prior_daily_high_completed_at": "2026-08-26T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": "alpaca_market_data_iex",
        "prior_daily_high_source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "prior_daily_high_source_hash": hashlib.sha256(raw_json.encode()).hexdigest(),
        "prior_daily_high_raw_payload_json": raw_json,
    }
    observation_json = json.dumps(observation, sort_keys=True, separators=(",", ":"))
    premarket_raw = {
        "ticker": "NOVA",
        "feed": "iex",
        "requested_at": "2026-08-26T13:30:00+00:00",
        "bars": [{
            "timestamp": "2026-08-26T13:00:00+00:00",
            "high": 10.0,
            "low": 9.0,
            "close": 9.5,
            "volume": 1000,
        }],
    }
    premarket_raw_json = json.dumps(
        premarket_raw, sort_keys=True, separators=(",", ":")
    )
    return {
        "ticker": "NOVA",
        "strategy_id": "alphaops_v5",
        "premarket_high": 10.0,
        "premarket_low": 9.0,
        "premarket_range_source": "alpaca_market_data_iex",
        "premarket_range_source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "enrichment_primary_source": "alpaca_market_data_iex",
        "enrichment_status": "verified",
        "enrichment_is_complete": True,
        "enrichment_was_fallback": False,
        "enrichment_observed_at": "2026-08-26T13:00:00+00:00",
        "enrichment_bar_completed_at": "2026-08-26T13:01:00+00:00",
        "enrichment_observation_sha256": hashlib.sha256(
            observation_json.encode()
        ).hexdigest(),
        "enrichment_observation_payload_json": observation_json,
        "premarket_raw_payload_json": premarket_raw_json,
        "premarket_source_hash_sha256": hashlib.sha256(
            premarket_raw_json.encode()
        ).hexdigest(),
        "prior_daily_high": 12.0,
        "prior_daily_high_observed_at": "2026-08-25T00:00:00+00:00",
        "prior_daily_high_completed_at": "2026-08-26T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": "alpaca_market_data_iex",
        "prior_daily_high_source_url": "https://data.alpaca.markets/v2/stocks/bars",
        "prior_daily_high_source_hash": hashlib.sha256(raw_json.encode()).hexdigest(),
        "prior_daily_high_raw_payload_json": raw_json,
    }


def test_authenticated_production_plan_uses_prior_daily_high_only() -> None:
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(
            _production_row(), decision_at="2026-08-26T13:30:00+00:00"
        ),
        "scan",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    assert payload["plan_construction_status"] == "COMPLETE"
    assert payload["target_1"] == 12.0
    assert payload["target_basis_kind"] == "prior_day_resistance"


def test_target_raw_artifact_mutation_fails_closed() -> None:
    row = _production_row()
    row["prior_daily_high_raw_payload_json"] = json.dumps(
        {"ticker": "NOVA", "timestamp": "2026-08-25T00:00:00+00:00", "high": 99.0,
         "bar": {"t": "2026-08-25T00:00:00Z", "h": 99.0}},
        sort_keys=True,
        separators=(",", ":"),
    )
    enriched = _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )
    assert "market_structure_observations" not in enriched


def test_target_outer_value_mutation_fails_even_when_digest_replays() -> None:
    row = _production_row()
    row["prior_daily_high"] = 99.0
    assert "market_structure_observations" not in _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )


def test_premarket_aggregate_rehash_cannot_override_raw_bars() -> None:
    row = _production_row()
    payload = json.loads(row["enrichment_observation_payload_json"])
    payload["premarket_high"] = 99.0
    row["premarket_high"] = 99.0
    row["enrichment_observation_payload_json"] = json.dumps(
        payload, sort_keys=True, separators=(",", ":")
    )
    row["enrichment_observation_sha256"] = hashlib.sha256(
        row["enrichment_observation_payload_json"].encode()
    ).hexdigest()
    assert "market_structure_observations" not in _attach_authenticated_alpaca_structure(
        row, decision_at="2026-08-26T13:30:00+00:00"
    )


def test_premarket_observation_persists_prior_artifact_contract() -> None:
    raw = {
        "ticker": "NOVA",
        "timestamp": "2026-08-25T00:00:00+00:00",
        "high": 12.0,
        "bar": {"t": "2026-08-25T00:00:00Z", "h": 12.0},
    }
    raw_json = json.dumps(raw, sort_keys=True, separators=(",", ":"))
    observation = observation_from_alpaca_bars(
        "NOVA",
        [{"timestamp": "2026-08-26T12:59:00Z", "high": 10.0, "low": 9.0, "close": 9.5}],
        previous_close=8.0,
        requested_at=datetime.fromisoformat("2026-08-26T13:00:00+00:00"),
        max_age_seconds=1200,
        feed="iex",
        prior_daily_high={
            "ticker": "NOVA",
            "high": 12.0,
            "observed_at": raw["timestamp"],
            "completed_at": "2026-08-26T13:00:00+00:00",
            "completion_semantics": "availability_boundary",
            "source": "alpaca_market_data_iex",
            "source_url": "https://data.alpaca.markets/v2/stocks/bars",
            "source_hash": hashlib.sha256(raw_json.encode()).hexdigest(),
            "raw_payload_json": raw_json,
        },
    )
    assert observation.has_prior_daily_high
    assert observation.prior_daily_high_raw_payload_json == raw_json
    assert observation.prior_daily_high_completion_semantics == "availability_boundary"


def test_missing_target_is_no_valid_plan() -> None:
    row = _production_row()
    for key in tuple(row):
        if key.startswith("prior_daily_high"):
            row[key] = "" if isinstance(row[key], str) else None
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(
            row, decision_at="2026-08-26T13:30:00+00:00"
        ),
        "scan",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    assert payload["alphaops_market_structure_plan"]["status"] == NO_VALID_PLAN


def test_gross_only_ratio_cannot_qualify_tier_two() -> None:
    assert _valid_modeled_cost_receipt(
        {"reward_risk_ratio": 1.5}, "a" * 64
    ) is False


def test_alpaca_daily_bar_completion_is_after_interval_start(monkeypatch) -> None:
    provider = AlpacaProvider.__new__(AlpacaProvider)
    provider.feed = "iex"
    provider.base_url = "https://data.alpaca.markets"
    monkeypatch.setattr(
        provider,
        "_request_json",
        lambda *_args, **_kwargs: {
            "bars": {
                "NOVA": [{"t": "2026-08-25T00:00:00Z", "h": 12.0}]
            }
        },
    )
    result = provider.get_previous_daily_highs(
        ["NOVA"],
        market_date="2026-08-26",
        config=type("C", (), {"historical_intraday_page_limit": 100})(),
        available_at=datetime.fromisoformat("2026-08-26T13:00:00+00:00"),
    )
    assert result["NOVA"]["completed_at"] == "2026-08-26T13:00:00+00:00"
    assert result["NOVA"]["completion_semantics"] == "availability_boundary"
    assert len(result["NOVA"]["raw_payload_json"]) > 0


def test_modeled_cost_receipt_recomputes_frozen_plan_math() -> None:
    row = _production_row()
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(row, decision_at="2026-08-26T13:30:00+00:00"),
        "scan",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    receipt = _build_modeled_cost_receipt(payload)
    assert receipt is not None
    payload["modeled_cost_receipt"] = receipt
    assert _valid_modeled_cost_receipt(payload, payload["plan_hash_sha256"])
    receipt["reward_per_share_after_cost"] += 1.0
    assert not _valid_modeled_cost_receipt(payload, payload["plan_hash_sha256"])


def test_short_modeled_cost_receipt_uses_adverse_side_math() -> None:
    row = {
        "alphaops_market_structure_plan": {
            "status": "COMPLETE",
            "direction": "short",
            "entry": 10.0,
            "stop": 11.0,
            "target": 7.0,
            "plan_hash_sha256": "f" * 64,
        }
    }
    receipt = _build_modeled_cost_receipt(row)
    assert receipt is not None
    assert receipt["direction"] == "short"
    row["modeled_cost_receipt"] = receipt
    assert _valid_modeled_cost_receipt(row, "f" * 64)


def test_monitor_receipt_reuse_is_idempotent_and_divergence_fails(tmp_path) -> None:
    store = SQLiteScanStore(tmp_path / "monitor.sqlite")
    receipt = {
        "receipt_id": "monitor-1",
        "market_date": "2026-08-26",
        "ticker": "NOVA",
        "signal_id": "sig-1",
        "plan_hash_sha256": "a" * 64,
        "content_hash_sha256": "b" * 64,
        "publication_count": 1,
        "checked_at": "2026-08-26T13:30:00+00:00",
    }
    assert store.persist_monitor_publication_receipts([receipt])["inserted"] == 1
    assert store.persist_monitor_publication_receipts([receipt])["reused"] == 1
    with pytest.raises(Exception, match="collision"):
        store.persist_monitor_publication_receipts([{**receipt, "ticker": "BAD"}])


def test_publication_without_modeled_cost_receipt_stays_tier_one() -> None:
    row = {"ticker": "NOVA", "publication_tier": TIER1}
    assert apply_publication_semantics([row], slate={"rows": [row]})[0]["publication_tier"] == TIER1


def _watcher_signal() -> tuple[dict[str, object], dict[str, object]]:
    row = _production_row()
    row["signal_id"] = "sig-nova"
    payload = _signal_payload(
        _attach_authenticated_alpaca_structure(row, decision_at="2026-08-26T13:30:00+00:00"),
        "scan-watcher",
        "2026-08-26T13:30:00+00:00",
        1,
    )
    payload.update({"market_date": "2026-08-26", "signal_id": "sig-nova"})
    slate = build_ranked_research_slate(
        [{"ticker": "NOVA", "signal_id": "sig-nova", "score": 2.0}],
        target=1,
        market_date="2026-08-26",
        generated_at="2026-08-26T13:30:00+00:00",
        scan_id="scan-watcher",
    )
    member = slate["selection_ids"][0]
    payload.update(
        {
            "selection_id": member,
            "cohort": "official_telegram",
            "frozen_ranked_research_slate": slate,
            "frozen_slate_lineage": {
                "frozen_source_scan_id": "scan-watcher",
                "current_scan_id": "scan-watcher",
            },
        }
    )
    quote_raw = {
        "ticker": "NOVA",
        "quote": {
            "t": datetime.now(timezone.utc).isoformat(),
            "bp": 10.0,
            "ap": 10.1,
        },
    }
    quote_raw_json = json.dumps(quote_raw, sort_keys=True, separators=(",", ":"))
    return payload, {"quote_bid": 10.0, "quote_ask": 10.1, "price": 10.05,
                     "observed_at": quote_raw["quote"]["t"],
                     "quote_observed_at": quote_raw["quote"]["t"],
                     "source": "alpaca_market_data_iex",
                     "quote_source": "alpaca_market_data_iex",
                     "source_bar_hash_sha256": "c" * 64,
                     "quote_source_hash_sha256": hashlib.sha256(
                         quote_raw_json.encode()
                     ).hexdigest(),
                     "quote_raw_payload_json": quote_raw_json,
                     "is_usable": True}


def test_watcher_proof_requires_valid_frozen_lineage_and_strict_identity() -> None:
    signal, observation = _watcher_signal()
    trace = {
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "computed": {
            "actual_after_cost_reward_risk": 1.6,
            "stop_distance_pct": 10.0,
            "chase_pct": 1.0,
        },
    }
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is None or validate_watcher_current_proof(
        {**signal, "current_price": observation["quote_ask"], "watcher_current_proof": proof}
    )
    assert proof is not None
    tampered = {**proof, "ticker": "BAD"}
    assert not validate_watcher_current_proof(
        {**signal, "current_price": observation["quote_ask"], "watcher_current_proof": tampered}
    )
    no_lineage = {
        key: value
        for key, value in signal.items()
        if key != "frozen_ranked_research_slate"
    }
    assert _build_watcher_current_proof(no_lineage, observation, trace) is None
    bad_slate = dict(signal["frozen_ranked_research_slate"])
    bad_slate["content_hash_sha256"] = "d" * 64
    bad_signal = {**signal, "frozen_ranked_research_slate": bad_slate}
    assert _build_watcher_current_proof(bad_signal, observation, trace) is None


def test_watcher_rejects_wrong_account_quote_ticker_plan_and_entry_window() -> None:
    signal, observation = _watcher_signal()
    trace = {
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "computed": {
            "actual_after_cost_reward_risk": 1.6,
            "stop_distance_pct": 10.0,
            "chase_pct": 1.0,
        },
    }
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is not None
    for mutation in (
        {"simulated_account_id": "wrong-account"},
        {"ticker": "BAD"},
        {"plan_hash_sha256": "e" * 64},
        {"entry_window_status": "CLOSED"},
    ):
        mutated = json.loads(json.dumps(proof))
        if "simulated_account_id" in mutation:
            mutated["portfolio_receipt"].update(mutation)
        elif "entry_window_status" in mutation:
            mutated["quote_receipt"].update(mutation)
        else:
            mutated.update(mutation)
            for key in ("quote_receipt", "portfolio_receipt"):
                mutated[key].update(mutation)
            if "plan_hash_sha256" in mutation:
                mutated["evaluate_v5_official_paper"]["plan_hash_sha256"] = mutation[
                    "plan_hash_sha256"
                ]
                mutated["evaluate_v5_official_paper_trace"]["plan_hash_sha256"] = mutation[
                    "plan_hash_sha256"
                ]
        for key, hash_key in (("quote_receipt", "quote_hash_sha256"),
                              ("portfolio_receipt", "portfolio_hash_sha256")):
            mutated[hash_key] = hashlib.sha256(
                json.dumps(mutated[key], sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest()
        mutated["proof_hash_sha256"] = hashlib.sha256(
            json.dumps(
                {key: value for key, value in mutated.items() if key != "proof_hash_sha256"},
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        assert not validate_watcher_current_proof(
            {**signal, "current_price": observation["quote_ask"], "watcher_current_proof": mutated}
        )


def test_monitor_receipt_binds_frozen_lineage_and_is_research_only() -> None:
    signal, observation = _watcher_signal()
    trace = {
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "computed": {
            "actual_after_cost_reward_risk": 1.6,
            "stop_distance_pct": 10.0,
            "chase_pct": 1.0,
        },
    }
    proof = _build_watcher_current_proof(signal, observation, trace)
    assert proof is not None
    receipt = _monitor_publication_receipt(
        signal=signal, proof=proof, checked_at=proof["checked_at"]
    )
    assert receipt["selection_id"] == signal["selection_id"]
    assert receipt["source_scan_id"] == "scan-watcher"
    assert receipt["frozen_slate_id"] == signal["frozen_ranked_research_slate"]["slate_id"]
    assert receipt["publication_tier"] == "ALERTABLE_PAPER_ENTRY"
    assert receipt["broker_execution"] == "disabled"


def test_alpaca_quote_contract_survives_collect_persist_load(monkeypatch, tmp_path) -> None:
    class FakeAlpaca:
        def __init__(self, _config):
            pass

        def validate_credentials(self):
            return None

        def get_minute_bars(self, symbols, start, end, config):
            return [{
                "ticker": symbols[0],
                "timestamp": "2026-08-26T12:59:00Z",
                "high": 10.0,
                "low": 9.0,
                "close": 10.0,
                "volume": 100,
            }]

        def get_latest_quotes(self, symbols, config):
            raw = {"t": "2026-08-26T13:00:00Z", "bp": 10.0, "ap": 10.1}
            raw_json = json.dumps(
                {"ticker": symbols[0], "quote": raw},
                sort_keys=True,
                separators=(",", ":"),
            )
            return {
                symbols[0]: {
                    "ticker": symbols[0],
                    "timestamp": raw["t"],
                    "bid": 10.0,
                    "ask": 10.1,
                    "source": "alpaca_market_data_iex",
                    "raw_payload_json": raw_json,
                    "source_hash_sha256": hashlib.sha256(raw_json.encode()).hexdigest(),
                }
            }

    monkeypatch.setattr(price_observation_module, "AlpacaProvider", FakeAlpaca)
    db_path = tmp_path / "quotes.sqlite"
    result = price_observation_module.collect_price_observations(
        db_path=db_path,
        source="alpaca",
        tickers=["NOVA"],
        market_date="2026-08-26",
        requested_at="2026-08-26T13:00:00+00:00",
        persist=True,
    )
    assert result["usable_count"] == 1
    loaded = SQLiteScanStore(db_path).load_price_observations(
        market_date="2026-08-26", ticker="NOVA", usable_only=True
    )[0]
    assert loaded["quote_bid"] == 10.0
    assert loaded["quote_ask"] == 10.1
    assert loaded["quote_source"].startswith("alpaca_market_data_")
    record = SQLiteScanStore(db_path).load_price_observation_records(
        market_date="2026-08-26", ticker="NOVA"
    )[0]
    assert record["payload_json"]["quote_bid"] == 10.0
    monkeypatch.setattr(
        FakeAlpaca,
        "get_latest_quotes",
        lambda _self, _symbols, _config: (_ for _ in ()).throw(
            DataProviderError("quote unavailable")
        ),
    )
    degraded = price_observation_module.collect_price_observations(
        db_path=db_path,
        source="alpaca",
        tickers=["NOVA"],
        market_date="2026-08-26",
        requested_at="2026-08-26T13:00:00+00:00",
        persist=True,
    )
    assert degraded["usable_count"] == 1
    assert "quote_bid" not in SQLiteScanStore(db_path).load_price_observations(
        market_date="2026-08-26", ticker="NOVA", usable_only=True
    )[0]


def test_v5_missing_quote_stands_down_without_intent() -> None:
    signal, observation = _watcher_signal()
    for key in (
        "quote_bid",
        "quote_ask",
        "quote_observed_at",
        "quote_source",
        "quote_source_hash_sha256",
        "quote_raw_payload_json",
    ):
        observation.pop(key, None)
    result = _decision_for_signal(
        signal=signal,
        observation=observation,
        open_position=None,
        prior_entry=False,
        settings=WatcherSettings(),
        open_count=0,
        daily_entry_count=0,
        existing_symbol_notional=0.0,
        scanner_config=load_config(),
    )
    assert result["state"] == "STAND_DOWN"
    assert not result.get("intent")
