from __future__ import annotations

from typing import Any

from intraday_scanner.alpha.feature_factory import build_feature_vector, feature_for_model
from intraday_scanner.models import EVIDENCE_CONFIDENCE_VERSION, SnapshotRow


def _snapshot_mapping(**overrides: Any) -> dict[str, Any]:
    mapping: dict[str, Any] = {
        "ticker": "TEST",
        "company": "Test Company",
        "previous_close": 10.0,
        "premarket_price": 12.0,
        "premarket_high": 12.5,
        "premarket_low": 11.0,
        "premarket_volume": 100_000,
        "dollar_volume": 1_200_000,
        "gap_pct": 20.0,
        "float_shares": 1_000_000,
        "market_cap": 12_000_000,
        "spread_pct": 1.0,
        "short_float_pct": 5.0,
        "has_news": True,
        "catalyst_headline": "Test headline",
        "catalyst_url": "https://example.test/news",
        "current_halt": False,
        "recent_offering": False,
        "reverse_split_90d": False,
        "source": "fixture",
        "as_of_timestamp": "2026-08-09T13:00:00+00:00",
        "source_confidence": 92.0,
    }
    mapping.update(overrides)
    return mapping


def test_completeness_does_not_claim_independent_reconciliation() -> None:
    row = SnapshotRow.from_mapping(
        _snapshot_mapping(
            field_completeness_score=100.0,
            source_reliability_prior=60.0,
            reconciliation_status="single_source",
            reconciliation_confidence_score=0.0,
            evidence_confidence_version=EVIDENCE_CONFIDENCE_VERSION,
        )
    )

    payload = row.to_dict()
    assert payload["field_completeness_score"] == 100.0
    assert payload["source_reliability_prior"] == 60.0
    assert payload["reconciliation_status"] == "single_source"
    assert payload["reconciliation_confidence_score"] == 0.0
    assert payload["reconciliation_status"] != "reconciled"
    assert payload["source_confidence"] == 92.0


def test_authenticated_single_feed_is_not_labeled_reconciled() -> None:
    row = SnapshotRow.from_mapping(
        _snapshot_mapping(
            source="alpaca_api",
            score_consensus="single_authenticated_source",
            source_confidence=92.0,
            field_completeness_score=88.0,
            source_reliability_prior=85.0,
            reconciliation_status="single_source",
            reconciliation_confidence_score=0.0,
            evidence_confidence_version=EVIDENCE_CONFIDENCE_VERSION,
        )
    )

    assert row.score_consensus == "single_authenticated_source"
    assert row.reconciliation_status == "single_source"
    assert row.reconciliation_confidence_score == 0.0


def test_provider_failure_is_distinct_from_no_evidence() -> None:
    base = _snapshot_mapping()
    failed = dict(base, reconciliation_status="provider_failed")
    absent = dict(base, reconciliation_status="no_evidence")
    failed_features = feature_for_model(
        build_feature_vector(failed, scan_id="failed", timestamp=base["as_of_timestamp"])
    )
    absent_features = feature_for_model(
        build_feature_vector(absent, scan_id="absent", timestamp=base["as_of_timestamp"])
    )

    assert failed_features["reconciliation_status"] == "provider_failed"
    assert absent_features["reconciliation_status"] == "no_evidence"
    assert failed_features["reconciliation_status"] != absent_features["reconciliation_status"]


def test_legacy_payload_round_trips_without_new_confidence_fields() -> None:
    legacy = _snapshot_mapping()
    row = SnapshotRow.from_mapping(legacy)
    payload = row.to_dict()

    assert payload["source_confidence"] == legacy["source_confidence"]
    assert "field_completeness_score" not in payload
    assert "source_reliability_prior" not in payload
    assert "reconciliation_status" not in payload
    assert "reconciliation_confidence_score" not in payload
    assert "evidence_confidence_version" not in payload


def test_legacy_fixed_rr_fixture_does_not_acquire_current_target_provenance() -> None:
    legacy_fixed_rr_fixture = {
        "signal_id": "legacy-fixed-rr-1",
        "entry_watch_level": 10.0,
        "invalidation_level": 8.0,
        "target_1": 13.0,
        "reward_risk_ratio": 1.5,
        "target_derived_from_risk": None,
        "target_policy_version": None,
    }

    assert legacy_fixed_rr_fixture["reward_risk_ratio"] == 1.5
    assert legacy_fixed_rr_fixture["target_derived_from_risk"] is None
    assert legacy_fixed_rr_fixture["target_policy_version"] is None
