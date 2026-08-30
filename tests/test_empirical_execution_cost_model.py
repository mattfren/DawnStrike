from __future__ import annotations

import copy

import intraday_scanner.alpha.empirical_execution_cost_challenger as cost


def _row(index: int, *, borrow: float | None = 1.0, price: float = 10.0) -> dict[str, object]:
    day = index % 5 + 1
    return {
        "observation_id": f"closed-{index:03d}",
        "receipt_id": f"receipt-{index:03d}",
        "market_date": f"2026-08-{day:02d}",
        "decision_at": f"2026-08-{day:02d}T14:00:00Z",
        "execution_status": "CLOSED",
        "fill_truth_status": "COMMITTED",
        "research_only": True,
        "broker_execution_enabled": False,
        "side": "BUY",
        "order_type": "LIMIT",
        "venue": "IEX",
        "feed": "SIP",
        "price": price,
        "dollar_liquidity": 20_000_000,
        "participation_rate": 0.002,
        "volatility": 2.0,
        "observed_cost_components": {
            "spread_bps": 3.0 + index % 3,
            "slippage_bps": 4.0 + index % 4,
            "fees_bps": 1.0,
            "regulatory_bps": 0.5,
            "borrow_bps": borrow,
        },
    }


def _build(monkeypatch, rows):
    monkeypatch.setattr(
        cost, "has_authenticated_committed_fill_truth", lambda value: value.get("trusted") is True
    )
    for row in rows:
        row.setdefault("trusted", True)
    return cost.build_empirical_execution_cost_model_receipt(
        rows,
        source_manifest={"artifact": "closed-fills"},
        code_sha="a" * 40,
        window={"start": "2026-08-01", "end": "2026-08-05"},
        minimum_observations=20,
        minimum_sessions=5,
        minimum_bucket_observations=5,
    )


def test_production_defaults_require_forward_scale_evidence(monkeypatch) -> None:
    monkeypatch.setattr(
        cost, "has_authenticated_committed_fill_truth", lambda value: value.get("trusted") is True
    )
    rows = [_row(index) for index in range(20)]
    for row in rows:
        row["trusted"] = True
    receipt = cost.build_empirical_execution_cost_model_receipt(
        rows,
        source_manifest={"artifact": "closed-fills"},
        code_sha="a" * 40,
        window={"start": "2026-08-01", "end": "2026-08-05"},
    )
    assert receipt["status"] == "NOT_EVALUABLE"
    assert receipt["configuration"]["minimum_observations"] == 300
    assert receipt["configuration"]["minimum_sessions"] == 60


def test_model_rejects_untrusted_json_even_with_closed_status(monkeypatch) -> None:
    receipt = _build(monkeypatch, [{**_row(index), "trusted": False} for index in range(20)])
    assert receipt["status"] == "NOT_EVALUABLE"
    assert receipt["rejected_observation_counts"]["unauthenticated_fill_truth"] == 20


def test_model_separates_components_percentiles_and_emits_two_x_stress(monkeypatch) -> None:
    receipt = _build(monkeypatch, [_row(index) for index in range(20)])
    assert receipt["status"] == "EVALUABLE"
    assert set(receipt["components"]) == {"spread", "slippage", "fees", "regulatory", "borrow"}
    assert receipt["components"]["spread"]["p50_bps"] is not None
    assert receipt["components"]["spread"]["p75_bps"] is not None
    assert receipt["components"]["spread"]["p90_bps"] is not None
    assert receipt["stress"]["multiplier"] == 2.0
    assert receipt["stress"]["total"]["p75"] == receipt["total"]["p75_bps"] * 2.0


def test_model_does_not_turn_missing_borrow_into_zero(monkeypatch) -> None:
    receipt = _build(monkeypatch, [_row(index, borrow=None) for index in range(20)])
    assert receipt["components"]["borrow"]["status"] == "NOT_EVALUABLE_MISSING_OBSERVATIONS"
    assert receipt["total"]["p75_bps"] is None
    assert receipt["stress"]["total"]["p75"] is None


def test_bucket_selection_isolated_and_sparse_fallback_is_labeled(monkeypatch) -> None:
    rows = [_row(index) for index in range(20)] + [
        _row(index + 100, price=80.0) for index in range(5)
    ]
    receipt = _build(monkeypatch, rows)
    exact = cost.select_empirical_cost(
        receipt,
        dimensions={
            "price": 10.0,
            "dollar_liquidity": 20_000_000,
            "participation_rate": 0.002,
            "volatility": 2.0,
            "time_of_day": "OPEN_30M",
            "side": "BUY",
            "order_type": "LIMIT",
            "venue": "IEX",
            "feed": "SIP",
        },
        quantile="p75",
    )
    assert exact["empirical_claim"] is True
    assert exact["fallback_level"] == "EXACT"
    sparse = cost.select_empirical_cost(
        receipt, dimensions={"price": 80.0, "venue": "OTHER"}, quantile="p90"
    )
    assert sparse["empirical_claim"] is True
    assert sparse["status"] == "EVALUABLE_WITH_SPARSE_FALLBACK"
    assert sparse["fallback_level"] != "EXACT"


def test_selection_rejects_hash_tamper_and_cannot_claim_empirical(monkeypatch) -> None:
    receipt = _build(monkeypatch, [_row(index) for index in range(20)])
    tampered = copy.deepcopy(receipt)
    tampered["components"]["fees"]["p75_bps"] = 9999.0
    result = cost.select_empirical_cost(tampered, quantile="p75")
    assert result["empirical_claim"] is False
    assert result["status"] == "NOT_EVALUABLE"


def test_challenger_does_not_change_provisional_champion(monkeypatch) -> None:
    receipt = _build(monkeypatch, [_row(index) for index in range(20)])
    assert receipt["champion_cost_model_version"] == cost.PROVISIONAL_COST_MODEL_VERSION
    assert receipt["champion_cost_model_unchanged"] is True
    assert receipt["promotion_eligible"] is False
