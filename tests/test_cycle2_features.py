from __future__ import annotations

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.alpha.v6.cycle2_features import (
    FEATURE_SCHEMA_V2,
    build_cycle2_feature_vector,
    build_exact_common_oos_ablation_receipt,
    join_decision_time_catalyst_evidence,
)
from intraday_scanner.services.catalyst_evidence_service import build_catalyst_evidence_event

_CONFIG = {"universe": {"price_min_usd": 1.0, "price_max_usd": 500.0}}
_WINDOW = {"start": "2026-08-01T00:00:00Z", "end": "2026-08-29T23:59:59Z"}
_CONFIG_HASH = canonical_hash(_CONFIG)
_WINDOW_HASH = canonical_hash(_WINDOW)


def _feature_lineage() -> dict[str, object]:
    return {
        "decision_id": "d-feature",
        "config": _CONFIG,
        "config_hash_sha256": _CONFIG_HASH,
        "code_hash_sha256": "a" * 40,
        "evaluation_window": _WINDOW,
        "window_hash_sha256": _WINDOW_HASH,
    }


def _bars(
    closes: list[float], *, start: str = "2026-08-28T13:00:00+00:00"
) -> list[dict[str, object]]:
    return [
        {
            "observed_at": f"2026-08-{28 + index // 24:02d}T{13 + index % 24:02d}:00:00+00:00",
            "close": value,
        }
        for index, value in enumerate(closes)
    ]


def test_v2_features_are_pit_and_missing_is_explicit() -> None:
    vector = build_cycle2_feature_vector(
        {"ticker": "ABC", "premarket_price": 10, "gap_pct": 20, "source_hash_sha256": "f" * 64},
        decision_at="2026-08-29T14:00:00+00:00",
        **_feature_lineage(),
        benchmark_bars=[
            {"observed_at": "2026-08-28T14:00:00+00:00", "close": 100},
            {"observed_at": "2026-08-29T15:00:00+00:00", "close": 200},
        ],
        universe_rows=[
            {
                "observed_at": "2026-08-29T13:00:00+00:00",
                "return_pct": 2,
                "gap_pct": 10,
                "source_hash_sha256": "a" * 64,
            },
            {
                "observed_at": "2026-08-29T13:00:00+00:00",
                "return_pct": -1,
                "gap_pct": 30,
                "source_hash_sha256": "b" * 64,
            },
        ],
    )
    assert vector["schema_version"] == FEATURE_SCHEMA_V2
    assert vector["features"]["market_breadth"]["value"] == 0.5
    assert vector["features"]["benchmark_volatility"]["status"] == "UNKNOWN"
    assert vector["features"]["sector_breadth"]["status"] == "UNKNOWN"
    assert vector["point_in_time"]["all_inputs_observed_at_or_before_decision"] is False


def test_v2_rejects_impossible_legacy_120_percent_gap_regime() -> None:
    vector = build_cycle2_feature_vector(
        {"ticker": "ABC", "premarket_price": 10, "gap_pct": 120},
        decision_at="2026-08-29T14:00:00+00:00",
    )
    assert vector["status"] == "BLOCKED_MISSING_EXACT_LINEAGE"
    assert vector["features"]["gap_dispersion"]["status"] == "UNKNOWN"
    assert vector["universe_contract"]["max_gap_regime_pct"] == 50.0


def test_v2_valid_lineage_still_excludes_gap_outside_universe_without_crashing() -> None:
    lineage = _feature_lineage()
    vector = build_cycle2_feature_vector(
        {"ticker": "ABC", "premarket_price": 10, "gap_pct": 120, "source_hash_sha256": "f" * 64},
        decision_at="2026-08-29T14:00:00+00:00",
        **lineage,
    )
    assert vector["status"] == "V2_UNIVERSE_EXCLUDED"
    assert all(item["status"] == "UNKNOWN" for item in vector["features"].values())


def test_v2_observed_features_require_per_input_lineage() -> None:
    bars = [
        {"observed_at": "2026-08-28T13:00:00+00:00", "close": 100, "source_hash_sha256": "a" * 64},
        {
            "observed_at": "2026-08-28T14:00:00+00:00",
            "close": 100.5,
            "source_hash_sha256": "e" * 64,
        },
        {"observed_at": "2026-08-29T13:00:00+00:00", "close": 101, "source_hash_sha256": "b" * 64},
    ]
    vector = build_cycle2_feature_vector(
        {"ticker": "ABC", "premarket_price": 10, "gap_pct": 20, "source_hash_sha256": "f" * 64},
        decision_at="2026-08-29T14:00:00+00:00",
        **_feature_lineage(),
        benchmark_bars=bars,
        sector_bars={"tech": bars},
        universe_rows=[
            {
                "observed_at": "2026-08-29T13:00:00+00:00",
                "return_pct": 1,
                "gap_pct": 10,
                "source_hash_sha256": "c" * 64,
            },
            {
                "observed_at": "2026-08-29T13:00:00+00:00",
                "return_pct": -1,
                "gap_pct": 20,
                "source_hash_sha256": "d" * 64,
            },
        ],
    )
    assert vector["status"] == "OBSERVED"
    assert vector["point_in_time"]["all_inputs_observed_at_or_before_decision"] is True
    assert vector["features"]["benchmark_volatility"]["status"] == "OBSERVED"


def test_v2_invalid_supplied_source_manifest_blocks_even_without_observations() -> None:
    vector = build_cycle2_feature_vector(
        {"ticker": "ABC", "premarket_price": 10, "gap_pct": 20, "source_hash_sha256": "f" * 64},
        decision_at="2026-08-29T14:00:00+00:00",
        source_hashes=["NOT-A-CANONICAL-HASH"],
        **_feature_lineage(),
    )
    assert vector["status"] == "BLOCKED_SOURCE_MANIFEST_MISMATCH"
    assert vector["features"]["gap_dispersion"]["status"] == "UNKNOWN"


def test_naive_catalyst_timestamp_is_not_assumed_utc() -> None:
    joined = join_decision_time_catalyst_evidence(
        {"ticker": "ABC", "decision_at": "2026-08-29T14:00:00"}, []
    )
    assert joined["status"] == "BLOCKED_INVALID_DECISION_TIME"


def test_catalyst_join_excludes_future_and_other_symbols() -> None:
    def event(event_id: str, published_at: str, content_hash: str) -> dict[str, object]:
        result: dict[str, object] = {
            "event_id": event_id,
            "symbol": "ABC",
            "source_kind": "news",
            "canonical_url": "https://example.test/news",
            "published_at": published_at,
            "first_seen_at": published_at,
            "available_at": published_at,
            "available_at_decision": True,
            "source_content_hash_sha256": content_hash,
            "research_only": True,
            "broker_execution_enabled": False,
        }
        result["source_lineage_hash_sha256"] = canonical_hash(
            {
                "source_kind": result["source_kind"],
                "canonical_url": result["canonical_url"],
                "source_content_hash_sha256": content_hash,
            }
        )
        result["event_payload_hash_sha256"] = canonical_hash(result)
        return result

    joined = join_decision_time_catalyst_evidence(
        {"decision_id": "d1", "ticker": "ABC", "decision_at": "2026-08-29T14:00:00+00:00"},
        [
            event("pre", "2026-08-29T13:00:00+00:00", "a" * 64),
            event("future", "2026-08-29T15:00:00+00:00", "b" * 64),
            {**event("other", "2026-08-29T13:00:00+00:00", "c" * 64), "symbol": "XYZ"},
        ],
    )
    assert joined["status"] == "EVIDENCE_JOINED"
    assert joined["event_ids"] == ["pre"]
    assert joined["immutable"] is True
    assert joined["joined_at_decision"] is True
    assert len(joined["event_semantic_hashes"]) == 1
    assert len(joined["event_source_lineage_hashes"]) == 1
    assert len(joined["event_identity_hashes"]) == 3


def test_catalyst_join_requires_actual_available_at() -> None:
    event = {
        "event_id": "missing-availability",
        "symbol": "ABC",
        "source_kind": "news",
        "canonical_url": "https://example.test/news",
        "published_at": "2026-08-29T13:00:00+00:00",
        "first_seen_at": "2026-08-29T13:00:00+00:00",
        "source_content_hash_sha256": "a" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    event["source_lineage_hash_sha256"] = canonical_hash(
        {
            "source_kind": event["source_kind"],
            "canonical_url": event["canonical_url"],
            "source_content_hash_sha256": event["source_content_hash_sha256"],
        }
    )
    event["event_payload_hash_sha256"] = canonical_hash(event)
    joined = join_decision_time_catalyst_evidence(
        {"decision_id": "d1", "ticker": "ABC", "decision_at": "2026-08-29T14:00:00+00:00"},
        [event],
    )
    assert joined["status"] == "NO_DECISION_TIME_EVIDENCE"


def test_catalyst_join_invalid_time_fails_closed() -> None:
    joined = join_decision_time_catalyst_evidence({"ticker": "ABC"}, [])
    assert joined["status"] == "BLOCKED_INVALID_DECISION_TIME"


def test_catalyst_producer_to_join_uses_first_seen_not_published() -> None:
    event = build_catalyst_evidence_event(
        symbol="ABC",
        source_kind="news",
        canonical_url="https://example.test/news",
        content="ABC announces a contract.",
        published_at="2026-08-29T13:00:00Z",
        first_seen_at="2026-08-29T15:00:00Z",
        decision_at="2026-08-29T14:00:00Z",
    )
    assert event["available_at_decision"] is False
    joined = join_decision_time_catalyst_evidence(
        {"decision_id": "d1", "ticker": "ABC", "decision_at": "2026-08-29T14:00:00Z"},
        [event],
    )
    assert joined["status"] == "NO_DECISION_TIME_EVIDENCE"


def _ablation_rows(**extra: object) -> dict[str, list[dict[str, object]]]:
    hashes = {
        "config_hash_sha256": "a" * 64,
        "source_hash_sha256": "b" * 64,
        "code_hash_sha256": "c" * 64,
        "window_hash_sha256": "d" * 64,
        "model_hash_sha256": "e" * 64,
    }
    return {
        mode: [{"decision_id": f"d{i}", "is_oos": True, **hashes, **extra} for i in range(2)]
        for mode in ("full", "no_catalyst", "catalyst_only", "shuffled_negative_control")
    }


def test_ablation_rejects_self_asserted_return_without_authority() -> None:
    rows = _ablation_rows(net_excess_return_pct=99, outcome_status="COMPLETE_SOURCED")
    receipt = build_exact_common_oos_ablation_receipt(
        rows,
        config_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_hash_sha256="c" * 64,
        window_hash_sha256="d" * 64,
    )
    assert receipt["status"] == "BLOCKED"
    assert receipt["reason"] == "mode_lineage_mismatch"


def test_ablation_requires_exact_prediction_receipts_before_outcome_truth() -> None:
    rows = _ablation_rows()
    receipt = build_exact_common_oos_ablation_receipt(
        rows,
        config_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_hash_sha256="c" * 64,
        window_hash_sha256="d" * 64,
    )
    assert receipt["status"] == "BLOCKED"
    assert receipt["reason"] == "mode_lineage_mismatch"
    assert receipt["receipt_hash_sha256"] == canonical_hash(
        {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    )


def test_blocked_ablation_receipt_tamper_is_detectable() -> None:
    receipt = build_exact_common_oos_ablation_receipt(
        {},
        config_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_hash_sha256="c" * 64,
        window_hash_sha256="d" * 64,
    )
    tampered = {**receipt, "reason": "forged"}
    assert tampered["receipt_hash_sha256"] != canonical_hash(
        {key: value for key, value in tampered.items() if key != "receipt_hash_sha256"}
    )


def test_ablation_legitimate_producer_consumer_path_is_diagnostic_low_sample(monkeypatch) -> None:
    monkeypatch.setattr(
        "intraday_scanner.alpha.v6.cycle2_features.has_authenticated_committed_fill_truth",
        lambda _row: True,
    )
    rows = _ablation_rows()
    for mode, mode_rows in rows.items():
        for row in mode_rows:
            row["mode"] = mode
            row["market_date"] = "2026-08-29"
            row["market_date"] = "2026-08-29"
            outcome = {
                "decision_id": row["decision_id"],
                "market_date": row["market_date"],
                "source_hash_sha256": row["source_hash_sha256"],
                "config_hash_sha256": row["config_hash_sha256"],
                "code_hash_sha256": row["code_hash_sha256"],
                "window_hash_sha256": row["window_hash_sha256"],
                "net_excess_return_pct": 1,
                "research_only": True,
                "broker_execution_enabled": False,
            }
            outcome_hash = canonical_hash(outcome)
            outcome["outcome_payload_hash_sha256"] = outcome_hash
            row["authenticated_outcome_payload"] = {"authenticated": True, **outcome}
            row["outcome_payload_hash_sha256"] = outcome_hash
            prediction = {
                "decision_id": row["decision_id"],
                "mode": mode,
                "expected_net_excess_return_pct": 0.0 if mode == "catalyst_only" else 0.5,
                "research_only": True,
                "broker_execution_enabled": False,
            }
            prediction_hash = canonical_hash(prediction)
            row["prediction_payload"] = prediction
            row["prediction_payload_hash_sha256"] = prediction_hash
            receipt_payload = {
                "decision_id": row["decision_id"],
                "mode": mode,
                "market_date": row["market_date"],
                "prediction_payload_hash_sha256": prediction_hash,
                "model_hash_sha256": "e" * 64,
                "config_hash_sha256": row["config_hash_sha256"],
                "source_hash_sha256": row["source_hash_sha256"],
                "code_hash_sha256": row["code_hash_sha256"],
                "window_hash_sha256": row["window_hash_sha256"],
                "research_only": True,
                "broker_execution_enabled": False,
            }
            row["prediction_receipt_payload"] = receipt_payload
            row["prediction_receipt_hash_sha256"] = canonical_hash(receipt_payload)
    receipt = build_exact_common_oos_ablation_receipt(
        rows,
        config_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_hash_sha256="c" * 64,
        window_hash_sha256="d" * 64,
    )
    assert receipt["status"] == "DIAGNOSTIC_LOW_SAMPLE"
    assert receipt["metrics"]["full"]["mean_absolute_error_pct"] == 0.5
    assert receipt["metrics"]["catalyst_only"]["mean_absolute_error_pct"] == 1.0
    assert receipt["prediction_error_delta_vs_full"]["no_catalyst"] == 0.0
    assert receipt["realized_return_diagnostic_pct"] == 1.0
    assert receipt["model_hash_by_mode"]["full"] == "e" * 64
    assert receipt["receipt_hash_sha256"] == canonical_hash(
        {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    )


def test_ablation_tampered_lineage_is_blocked() -> None:
    rows = _ablation_rows()
    for mode, mode_rows in rows.items():
        for row in mode_rows:
            row["mode"] = mode
            row["market_date"] = "2026-08-29"
    rows["no_catalyst"][0]["code_hash_sha256"] = "x" * 64
    receipt = build_exact_common_oos_ablation_receipt(
        rows,
        config_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_hash_sha256="c" * 64,
        window_hash_sha256="d" * 64,
    )
    assert receipt["status"] == "BLOCKED"
    assert receipt["reason"] == "prediction_receipt_lineage_invalid"


def test_ablation_missing_row_lineage_is_blocked() -> None:
    rows = _ablation_rows()
    for mode, mode_rows in rows.items():
        for row in mode_rows:
            row["mode"] = mode
            row["market_date"] = "2026-08-29"
    rows["full"][0].pop("source_hash_sha256")
    receipt = build_exact_common_oos_ablation_receipt(
        rows,
        config_hash_sha256="a" * 64,
        source_hash_sha256="b" * 64,
        code_hash_sha256="c" * 64,
        window_hash_sha256="d" * 64,
    )
    assert receipt["status"] == "BLOCKED"
    assert receipt["reason"] == "row_lineage_hash_mismatch"
