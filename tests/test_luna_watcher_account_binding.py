from __future__ import annotations

import hashlib
import json

import pytest

from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    evaluate_v5_official_paper,
)
from intraday_scanner.services.alpha_cycle_service import (
    _attach_authenticated_alpaca_structure,
    _signal_payload,
)
from intraday_scanner.services.luna_research_slate_service import (
    _first_nonblank,
    _watcher_current,
    build_ranked_research_slate,
)
from intraday_scanner.services.trade_watcher_service import _build_watcher_current_proof


def _hash(payload: dict[str, object]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _row(*, portfolio_account_id: str, row_account_id: str = "") -> dict[str, object]:
    decision_at = "2026-08-26T13:30:00+00:00"
    signal_id = "signal-account-binding"
    row = {
        "ticker": "AAA",
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
        "prior_daily_high": 12.0,
        "prior_daily_high_observed_at": "2026-08-25T00:00:00+00:00",
        "prior_daily_high_completed_at": "2026-08-26T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": "alpaca_market_data_iex",
        "prior_daily_high_source_url": "https://data.alpaca.markets/v2/stocks/bars",
    }
    premarket_raw = {
        "ticker": "AAA",
        "feed": "iex",
        "requested_at": decision_at,
        "bars": [
            {
                "ticker": "AAA",
                "timestamp": "2026-08-26T13:00:00+00:00",
                "high": 10.0,
                "low": 9.0,
                "close": 9.5,
                "volume": 1000,
            }
        ],
    }
    premarket_raw_json = json.dumps(premarket_raw, sort_keys=True, separators=(",", ":"))
    prior_raw = {
        "ticker": "AAA",
        "timestamp": "2026-08-25T00:00:00+00:00",
        "high": 12.0,
        "bar": {"t": "2026-08-25T00:00:00Z", "h": 12.0},
    }
    prior_raw_json = json.dumps(prior_raw, sort_keys=True, separators=(",", ":"))
    observation = {
        "ticker": "AAA",
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
        "prior_daily_high": 12.0,
        "prior_daily_high_observed_at": "2026-08-25T00:00:00+00:00",
        "prior_daily_high_completed_at": "2026-08-26T00:00:00+00:00",
        "prior_daily_high_completion_semantics": "availability_boundary",
        "prior_daily_high_source": "alpaca_market_data_iex",
        "prior_daily_high_source_url": "https://data.alpaca.markets/v2/stocks/bars",
    }
    observation_json = json.dumps(observation, sort_keys=True, separators=(",", ":"))
    row.update(
        {
            "enrichment_observation_sha256": _hash(observation),
            "enrichment_observation_payload_json": observation_json,
            "premarket_raw_payload_json": premarket_raw_json,
            "premarket_source_hash_sha256": _hash(premarket_raw),
            "prior_daily_high_raw_payload_json": prior_raw_json,
            "prior_daily_high_source_hash": _hash(prior_raw),
        }
    )
    signal = _signal_payload(
        _attach_authenticated_alpaca_structure(row, decision_at=decision_at),
        "scan-account-binding",
        decision_at,
        1,
    )
    signal["signal_id"] = signal_id
    signal["market_date"] = "2026-08-26"
    signal.update(
        {
            "decision": "clean_edge",
            "decision_tier": "clean_edge",
            "alert_gate_status": "PASS",
            "manual_confirmation_required": False,
            "source_confidence": 92,
            "source_count": 3,
            "source_quality_status": "verified",
            "stale_data_flag": False,
            "float_shares": 8_000_000,
            "float_status": "verified",
            "float_source": "verified_snapshot",
            "catalyst_summary": "FDA clearance announced before market open",
            "catalyst_url": "https://example.test/catalyst",
            "catalyst_status": "verified",
            "catalyst_tier": "A",
            "halt_status": "clear",
            "sec_risk_status": "clear",
            "corporate_action_status": "clear",
            "dollar_volume": 5_000_000,
            "previous_close": 8.0,
            "premarket_price": 10.0,
            "premarket_volume": 500_000,
            "gap_pct": 25.0,
            "spread_pct": 0.5,
            "liquidity_tier": "high_liquidity",
        }
    )
    slate = build_ranked_research_slate(
        [signal],
        target=1,
        generated_at=decision_at,
        market_date="2026-08-26",
        scan_id="scan-account-binding",
    )
    signal.update(
        {
            "selection_id": slate["selection_ids"][0],
            "cohort": "official_telegram",
            "frozen_ranked_research_slate": slate,
            "frozen_slate_lineage": {
                "schema_version": "dawnstrike.luna.frozen_slate_selection_lineage.v1",
                "slate_id": slate["slate_id"],
                "slate_content_hash_sha256": slate["content_hash_sha256"],
                "frozen_source_scan_id": "scan-account-binding",
                "current_scan_id": "scan-account-binding",
                "reuse_status": "CURRENT_SCAN",
            },
        }
    )
    quote_raw = {
        "ticker": "AAA",
        "quote": {"t": decision_at, "bp": 9.9, "ap": 10.0},
    }
    quote_raw_json = json.dumps(quote_raw, sort_keys=True, separators=(",", ":"))
    observation = {
        "quote_bid": 9.9,
        "quote_ask": 10.0,
        "price": 10.0,
        "observed_at": decision_at,
        "quote_observed_at": decision_at,
        "requested_at": decision_at,
        "freshness_seconds": 0,
        "quote_freshness_seconds": 0,
        "source": "alpaca_market_data_iex",
        "quote_source": "alpaca_market_data_iex",
        "source_bar_hash_sha256": "c" * 64,
        "quote_source_hash_sha256": _hash(quote_raw),
        "quote_raw_payload_json": quote_raw_json,
        "is_usable": True,
    }
    trace = evaluate_v5_official_paper(
        signal,
        observation,
        simulated_equity=100_000,
        existing_symbol_notional=0.0,
        decision_time=decision_at,
    )
    assert trace.eligible_for_official_paper, trace.reasons
    proof = _build_watcher_current_proof(signal, observation, trace.to_dict())
    assert proof is not None
    if portfolio_account_id != ALPHAOPS_V5_ACCOUNT_ID:
        portfolio = dict(proof["portfolio_receipt"])
        portfolio["simulated_account_id"] = portfolio_account_id
        proof["portfolio_receipt"] = portfolio
        proof["portfolio_hash_sha256"] = _hash(portfolio)
        proof["proof_hash_sha256"] = _hash(
            {key: value for key, value in proof.items() if key != "proof_hash_sha256"}
        )
    signal["account_id"] = row_account_id
    signal["current_price"] = 10.0
    signal["watcher_current_proof"] = proof
    return signal


def test_watcher_rejects_hash_valid_admission_for_wrong_simulated_account() -> None:
    assert not _watcher_current(_row(portfolio_account_id="WRONG_ACCOUNT"))


@pytest.mark.parametrize("invalid", (0.0, -1.0, float("nan"), float("inf")))
def test_watcher_price_alias_presence_does_not_mask_invalid_primary(invalid: float) -> None:
    assert _first_nonblank({"last": invalid, "price": 10.0}, "last", "price") is invalid


@pytest.mark.parametrize("missing", (None, "", "   "))
def test_watcher_price_alias_allows_missing_primary_fallback(missing: object) -> None:
    assert _first_nonblank({"last": missing, "price": 10.0}, "last", "price") == 10.0


def test_watcher_requires_row_and_trace_account_to_match_v5_account() -> None:
    assert _watcher_current(
        _row(
            portfolio_account_id=ALPHAOPS_V5_ACCOUNT_ID,
            row_account_id=ALPHAOPS_V5_ACCOUNT_ID,
        )
    )
    assert not _watcher_current(
        _row(
            portfolio_account_id=ALPHAOPS_V5_ACCOUNT_ID,
            row_account_id="WRONG_ACCOUNT",
        )
    )
    trace_mismatch = _row(portfolio_account_id=ALPHAOPS_V5_ACCOUNT_ID)
    trace_mismatch["decision_trace"] = {"account_id": "WRONG_ACCOUNT"}
    assert not _watcher_current(trace_mismatch)
