from __future__ import annotations

import hashlib
from dataclasses import replace

import pytest

from intraday_scanner.decisioning.contracts import (
    ConditionResult,
    ConditionStatus,
    StrategyDecisionReceipt,
    canonical_json,
    parse_strategy_decision_receipt,
)
from intraday_scanner.services.strategy_decision_service import StrategyDecisionService


def _receipt() -> StrategyDecisionReceipt:
    service = StrategyDecisionService(code_sha="a" * 40, source_identity="sanitized-fixture")
    return service.build_receipt(
        {
            "strategy_id": "ts_momentum_sma_atr",
            "strategy_version": "v1.0",
            "symbol": "TEST",
            "market_date": "2026-08-22",
            "score": 80,
            "entry_reference": 10,
            "stop": 9,
            "target": 12,
            "reward_risk_ratio": 2,
        },
        condition_overrides={
            "valid_symbol": True,
            "point_in_time_ohlcv": True,
            "positive_current_price": True,
            "positive_current_volume": True,
            "source_identity_present": True,
            "source_fresh": True,
            "no_market_source_conflict": True,
            "not_currently_halted": True,
            "valid_entry_reference": True,
            "valid_stop_geometry": True,
            "valid_target_when_required": True,
            "reward_risk_at_least_1_50": True,
            "within_risk_budget": True,
            "spread_within_existing_policy": True,
            "trend_regime": True,
            "extension_guard": True,
            "volatility_regime": True,
            "offering_or_dilution": True,
            "corporate_action": True,
            "material_adverse_event": True,
            "float_known": True,
            "secondary_source_present": True,
            "historical_sample_sufficient": True,
            "catalyst_identified": True,
        },
        decision_at="2026-08-22T14:30:00+00:00",
    )


def _rehash_payload(payload: dict[str, object]) -> None:
    body = {
        key: value
        for key, value in payload.items()
        if key not in {"receipt_id", "receipt_hash_sha256"}
    }
    digest = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
    payload["receipt_hash_sha256"] = digest
    payload["receipt_id"] = "sdr-" + digest[:24]


def test_receipt_hash_is_canonical_and_stable() -> None:
    receipt = _receipt()
    assert receipt.receipt_hash_sha256 == receipt.compute_hash()
    assert receipt.receipt_id == "sdr-" + receipt.receipt_hash_sha256[:24]
    assert receipt.canonical_json() == receipt.canonical_json()


def test_typed_receipt_parser_roundtrips_canonical_condition_results() -> None:
    payload = _receipt().to_dict()

    parsed = parse_strategy_decision_receipt(payload, require_v2=True)

    assert parsed.condition_results
    assert all(isinstance(item, ConditionResult) for item in parsed.condition_results)
    assert parsed.to_dict() == payload
    assert parsed.canonical_json() == canonical_json(payload)


@pytest.mark.parametrize("missing_field", ["receipt_id", "receipt_hash_sha256"])
def test_typed_receipt_parser_rejects_missing_receipt_identity(missing_field: str) -> None:
    payload = _receipt().to_dict()
    payload.pop(missing_field)

    with pytest.raises(ValueError, match="typed schema|payload is not exact"):
        parse_strategy_decision_receipt(payload, require_v2=True)


def test_typed_receipt_parser_rejects_missing_source_identity() -> None:
    payload = _receipt().to_dict()
    payload["source_identity"] = ""

    with pytest.raises(ValueError, match="typed schema"):
        parse_strategy_decision_receipt(payload, require_v2=True)


def test_typed_receipt_parser_rejects_duplicate_serialized_condition_ids() -> None:
    payload = _receipt().to_dict()
    payload["condition_results"].append(dict(payload["condition_results"][0]))

    with pytest.raises(ValueError, match="typed schema"):
        parse_strategy_decision_receipt(payload, require_v2=True)


@pytest.mark.parametrize(
    ("field", "hostile_value"),
    (
        ("strategy_id", 7),
        ("source_identity", 7),
        ("research_pick_eligible", 1),
        ("paper_entry_eligible", 0),
        ("research_only", 1),
        ("broker_execution_enabled", 0),
        ("base_strategy_score", "80"),
        ("score_adjustment", True),
        ("final_score", True),
        ("entry_reference", "10"),
    ),
)
def test_typed_receipt_parser_rejects_rehashed_wrong_scalar_types(
    field: str,
    hostile_value: object,
) -> None:
    payload = _receipt().to_dict()
    payload[field] = hostile_value
    _rehash_payload(payload)

    with pytest.raises(ValueError, match="typed schema"):
        parse_strategy_decision_receipt(payload, require_v2=True)


@pytest.mark.parametrize("hostile_code_sha", ["short", "g" * 40, "A" * 40])
def test_typed_v2_receipt_requires_full_lowercase_git_sha(
    hostile_code_sha: str,
) -> None:
    payload = _receipt().to_dict()
    payload["code_sha"] = hostile_code_sha
    _rehash_payload(payload)

    with pytest.raises(ValueError, match="typed schema"):
        parse_strategy_decision_receipt(payload, require_v2=True)


def test_typed_receipt_parser_normalizes_malformed_condition_identity_to_value_error() -> None:
    payload = _receipt().to_dict()
    payload["condition_results"][0]["condition_id"] = 7
    _rehash_payload(payload)

    with pytest.raises(ValueError, match="typed schema"):
        parse_strategy_decision_receipt(payload, require_v2=True)


def test_condition_result_confidence_rejects_boolean_scalar() -> None:
    payload = _receipt().to_dict()
    payload["condition_results"][0]["confidence"] = True
    _rehash_payload(payload)

    with pytest.raises(ValueError, match="typed schema"):
        parse_strategy_decision_receipt(payload, require_v2=True)


def test_duplicate_condition_ids_are_rejected() -> None:
    receipt = _receipt()
    duplicate = receipt.condition_results + (receipt.condition_results[0],)
    with pytest.raises(ValueError, match="duplicate condition IDs"):
        replace(receipt, condition_results=duplicate, receipt_hash_sha256="")


def test_non_finite_condition_values_are_rejected() -> None:
    with pytest.raises(ValueError, match="non-finite"):
        ConditionResult("x", ConditionStatus.PASS, observed_value=float("nan"))


def test_tampered_receipt_hash_is_rejected() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match="receipt_hash_sha256"):
        replace(receipt, receipt_hash_sha256="0" * 64)


def test_receipts_cannot_leave_research_only_or_paper_entry_boundaries() -> None:
    receipt = _receipt()
    with pytest.raises(ValueError, match="research-only"):
        replace(receipt, research_only=False, receipt_hash_sha256="")
    with pytest.raises(ValueError, match="paper entry"):
        replace(
            receipt,
            research_pick_eligible=False,
            paper_entry_eligible=True,
            receipt_hash_sha256="",
        )
