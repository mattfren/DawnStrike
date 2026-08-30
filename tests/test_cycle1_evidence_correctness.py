from __future__ import annotations

from intraday_scanner.alpha.alpha_model import AlphaModel
from intraday_scanner.alpha.feature_factory import build_feature_vector
from intraday_scanner.services.alpha_attribution_service import (
    _attempt_missing_classification as attribution_missing_classification,
)
from intraday_scanner.services.alpha_attribution_service import (
    _v6_outcome_quality,
)
from intraday_scanner.services.alpha_outcome_capture_service import (
    _bind_capture_attempt_lineage,
    _capture_attempt,
)
from intraday_scanner.services.outcome_gap_service import (
    _attempt_missing_classification as gap_missing_classification,
)
from intraday_scanner.services.source_reliability_service import (
    build_source_reliability,
)


def _candidate() -> dict[str, object]:
    return {
        "ticker": "NOVA",
        "score": 80,
        "explosive_score": 80,
        "catalyst_score": 70,
        "gap_pct": 20,
        "premarket_price": 10,
        "premarket_volume": 1_000_000,
        "dollar_volume": 2_000_000,
        "source": "fixture_public_table",
        "preferred_source": "fixture_public_table",
        "source_confidence": 90,
        "tradability_score": 80,
        "breakout_trigger": 10.2,
        "invalidation_level": 9.5,
        "first_target": 11,
    }


def test_collection_quality_cannot_bias_alpha_without_outcome_evidence() -> None:
    weak = build_source_reliability(
        {
            "attempts": [
                {
                    "source": "fixture_public_table",
                    "rows_extracted": 10,
                    "rows_normalized": 2,
                    "rows_rejected": 8,
                }
            ]
        }
    )[0]
    strong = {**weak, "reliability_score": 99.0}
    candidate = _candidate()
    weak_score = AlphaModel().score_candidates(
        [candidate],
        [
            build_feature_vector(
                candidate,
                scan_id="scan",
                timestamp="now",
                source_reliability={weak["source"]: weak},
            )
        ],
    )[0]
    strong_score = AlphaModel().score_candidates(
        [candidate],
        [
            build_feature_vector(
                candidate,
                scan_id="scan",
                timestamp="now",
                source_reliability={strong["source"]: strong},
            )
        ],
    )[0]
    assert weak["outcome_count"] == 0
    assert weak["outcome_evidence_status"] == "collection_only"
    assert weak_score["source_reliability_adjustment"] == 0
    assert strong_score["source_reliability_adjustment"] == 0


def test_universe_filter_rejects_are_not_quality_failures() -> None:
    row = {
        "attempts": [
            {
                "source": "alpaca",
                "rows_extracted": 10,
                "rows_normalized": 5,
                "rows_rejected": 5,
                "rejection_reason_counts": {
                    "unsupported_exchange": 3,
                    "invalid_symbol": 1,
                    "asset_identity_missing": 1,
                },
            }
        ]
    }
    reliability = build_source_reliability(row)[0]
    assert reliability["universe_filter_rejected_count"] == 3
    assert reliability["data_quality_rejected_count"] == 2
    assert reliability["reliability_score"] == 67.43


def test_unmarked_winner_shape_is_not_authenticated_source_evidence() -> None:
    reliability = build_source_reliability(
        {
            "attempts": [
                {
                    "source": "fixture_public_table",
                    "rows_extracted": 1,
                    "rows_normalized": 1,
                }
            ]
        },
        outcomes=[{"source": "fixture_public_table", "winner_close": True}],
    )[0]
    assert reliability["outcome_count"] == 0
    assert reliability["outcome_evidence_status"] == "collection_only"


def test_authenticated_source_outcomes_are_idempotent_by_identity() -> None:
    summary = {
        "attempts": [
            {
                "source": "fixture_public_table",
                "rows_extracted": 2,
                "rows_normalized": 2,
            }
        ]
    }
    full_history = [
        {
            "source": "fixture_public_table",
            "outcome_id": "outcome-1",
            "authenticated_outcome": True,
            "winner_close": True,
        },
        {
            "source": "fixture_public_table",
            "outcome_id": "outcome-2",
            "authenticated_outcome": True,
            "winner_close": False,
        },
        # A repeated history row must not inflate either denominator or wins.
        {
            "source": "fixture_public_table",
            "outcome_id": "outcome-1",
            "authenticated_outcome": True,
            "winner_close": True,
        },
    ]
    first = build_source_reliability(summary, outcomes=full_history)[0]
    second = build_source_reliability(summary, outcomes=full_history, previous={
        first["source"]: first,
    })[0]
    assert first["outcome_count"] == 2
    assert first["winner_count"] == 1
    assert second["outcome_count"] == 2
    assert second["winner_count"] == 1
    assert "outcome_identities" not in second
    assert "outcome_winner_identities" not in second


def test_authenticated_snapshot_removal_does_not_retain_quarantined_identity() -> None:
    summary = {
        "attempts": [
            {"source": "fixture_public_table", "rows_extracted": 2, "rows_normalized": 2}
        ]
    }
    first = build_source_reliability(
        summary,
        outcomes=[
            {
                "source": "fixture_public_table",
                "outcome_id": "kept",
                "authenticated_outcome": True,
                "winner_close": True,
            },
            {
                "source": "fixture_public_table",
                "outcome_id": "quarantined",
                "authenticated_outcome": True,
                "winner_close": False,
            },
        ],
    )[0]
    second = build_source_reliability(
        summary,
        outcomes=[
            {
                "source": "fixture_public_table",
                "outcome_id": "kept",
                "authenticated_outcome": True,
                "winner_close": True,
            }
        ],
        previous={first["source"]: first},
    )[0]
    assert first["outcome_count"] == 2
    assert second["outcome_count"] == 1
    assert second["winner_count"] == 1
    assert second["outcome_identity_set_hash_sha256"] != first[
        "outcome_identity_set_hash_sha256"
    ]
    assert "outcome_identities" not in second


def test_conflicting_authenticated_duplicate_is_quarantined_and_diagnosed() -> None:
    reliability = build_source_reliability(
        {
            "attempts": [
                {"source": "fixture_public_table", "rows_extracted": 2, "rows_normalized": 2}
            ]
        },
        outcomes=[
            {
                "source": "fixture_public_table",
                "outcome_id": "conflict",
                "authenticated_outcome": True,
                "winner_close": True,
            },
            {
                "source": "other_fixture_source",
                "outcome_id": "conflict",
                "authenticated_outcome": True,
                "winner_close": True,
            },
        ],
    )[0]
    assert reliability["outcome_count"] == 0
    assert reliability["winner_count"] == 0
    assert reliability["outcome_conflicting_identity_count"] == 1
    assert reliability["outcome_snapshot_status"] == "quarantined_conflicting_identity"
    assert reliability["outcome_evidence_status"] == "collection_only"


def test_authenticated_snapshot_hash_tracks_changed_winner_truth() -> None:
    summary = {
        "attempts": [
            {"source": "fixture_public_table", "rows_extracted": 1, "rows_normalized": 1}
        ]
    }
    winning = build_source_reliability(
        summary,
        outcomes=[
            {
                "source": "fixture_public_table",
                "outcome_id": "same",
                "authenticated_outcome": True,
                "winner_close": True,
            }
        ],
    )[0]
    losing = build_source_reliability(
        summary,
        outcomes=[
            {
                "source": "fixture_public_table",
                "outcome_id": "same",
                "authenticated_outcome": True,
                "winner_close": False,
            }
        ],
    )[0]
    assert winning["outcome_identity_set_hash_sha256"] == losing[
        "outcome_identity_set_hash_sha256"
    ]
    assert winning["outcome_snapshot_hash_sha256"] != losing[
        "outcome_snapshot_hash_sha256"
    ]


def test_degraded_partial_snapshot_cannot_enable_alpha_adjustment() -> None:
    reliability = build_source_reliability(
        {
            "attempts": [
                {"source": "fixture_public_table", "rows_extracted": 2, "rows_normalized": 2}
            ]
        },
        outcomes=[
            {
                "source": "fixture_public_table",
                "outcome_id": "clean",
                "authenticated_outcome": True,
                "winner_close": True,
            },
            {
                "source": "fixture_public_table",
                "authenticated_outcome": True,
                "winner_close": True,
            },
        ],
    )[0]
    assert reliability["outcome_count"] == 1
    assert reliability["unidentified_authenticated_outcome_count"] == 1
    assert reliability["outcome_snapshot_status"] == "degraded_unidentified_authenticated_outcome"
    assert reliability["outcome_evidence_status"] == "collection_only"
    assert reliability["alpha_adjustment_eligible"] is False


def test_cross_source_snapshot_degradation_blocks_clean_source_bonus() -> None:
    rows = build_source_reliability(
        {
            "attempts": [
                {"source": "source_a", "rows_extracted": 2, "rows_normalized": 1},
                {"source": "source_b", "rows_extracted": 1, "rows_normalized": 1},
            ]
        },
        outcomes=[
            {
                "source": "source_a",
                "outcome_id": "clean-a",
                "authenticated_outcome": True,
                "winner_close": True,
            },
            {
                "source": "source_b",
                "outcome_id": "conflict-b",
                "authenticated_outcome": True,
                "winner_close": True,
            },
            {
                "source": "other-source-b",
                "outcome_id": "conflict-b",
                "authenticated_outcome": True,
                "winner_close": True,
            },
        ],
    )
    source_a = next(row for row in rows if row["source"] == "source_a")
    source_b = next(row for row in rows if row["source"] == "source_b")
    assert source_a["outcome_count"] == 1
    assert source_a["outcome_conflicting_identity_count"] == 0
    assert source_a["outcome_snapshot_status"] == "quarantined_conflicting_identity"
    assert source_a["outcome_evidence_status"] == "collection_only"
    assert source_a["alpha_adjustment_eligible"] is False
    assert source_a["reliability_score"] == 50.0
    assert source_b["outcome_conflicting_identity_count"] == 1


def test_authenticated_outcome_without_identity_is_not_counted() -> None:
    reliability = build_source_reliability(
        {
            "attempts": [
                {
                    "source": "fixture_public_table",
                    "rows_extracted": 1,
                    "rows_normalized": 1,
                }
            ]
        },
        outcomes=[
            {
                "source": "fixture_public_table",
                "authenticated_outcome": True,
                "winner_close": True,
            }
        ],
    )[0]
    assert reliability["outcome_count"] == 0
    assert reliability["unidentified_authenticated_outcome_count"] == 1


def test_consumers_do_not_promote_explicit_recoverable_attempt_to_terminal() -> None:
    attempt = {
        "status": "terminal_missing",
        "missing_classification": "recoverable",
        "terminal": True,
    }
    assert gap_missing_classification(attempt) == "recoverable"
    assert attribution_missing_classification(attempt) == "recoverable"
    assert _v6_outcome_quality(
        {
            "outcome_status": "terminal_missing",
            "missing_classification": "recoverable",
        }
    ) == "unattributed_recoverable_missing_evidence"


def test_capture_attempt_marks_recoverable_missing_without_claiming_truth() -> None:
    signal = {"signal_id": "signal-1", "ticker": "NOVA"}
    diagnostic = {
        "signal_id": "signal-1",
        "ticker": "NOVA",
        "status": "ineligible_provider_error",
        "detail": "provider unavailable",
    }
    attempt = _capture_attempt(
        signal,
        diagnostic,
        market_date="2026-08-29",
        captured_at="2026-08-29T22:00:00Z",
        requested_at=__import__("datetime").datetime.fromisoformat("2026-08-29T22:00:00+00:00"),
        source_evidence={},
        source_requests=[],
    )
    assert attempt["status"] == "terminal_missing"  # legacy status
    assert attempt["missing_classification"] == "recoverable"
    assert attempt["retryable"] is True
    assert attempt["authoritative_terminal"] is False
    assert attempt["missing_truth_is_zero"] is False


def test_capture_attempt_lineage_is_additive_and_immutable() -> None:
    signal = {"signal_id": "signal-1", "ticker": "NOVA"}
    diagnostic = {
        "signal_id": "signal-1",
        "ticker": "NOVA",
        "status": "ineligible_provider_error",
    }
    requested_at = __import__("datetime").datetime.fromisoformat("2026-08-29T22:00:00+00:00")
    first = _capture_attempt(
        signal,
        diagnostic,
        market_date="2026-08-29",
        captured_at="2026-08-29T22:00:00Z",
        requested_at=requested_at,
        source_evidence={},
        source_requests=[],
    )
    second = _capture_attempt(
        signal,
        diagnostic,
        market_date="2026-08-29",
        captured_at="2026-08-29T22:05:00Z",
        requested_at=requested_at.replace(minute=5),
        source_evidence={},
        source_requests=[],
    )
    bound = _bind_capture_attempt_lineage(second, [first])
    assert bound["attempt_id"] != first["attempt_id"]
    assert bound["supersedes_attempt_id"] == first["attempt_id"]
    assert bound["attempt_number"] == 2
    assert first.get("supersedes_attempt_id", "") == ""


def test_authoritative_missing_contract_is_not_a_zero_outcome() -> None:
    signal = {"signal_id": "signal-1", "ticker": "NOVA"}
    diagnostic = {
        "signal_id": "signal-1",
        "ticker": "NOVA",
        "status": "ineligible_malformed_ohlc",
        "detail": "invalid OHLC",
    }
    attempt = _capture_attempt(
        signal,
        diagnostic,
        market_date="2026-08-29",
        captured_at="2026-08-29T22:00:00Z",
        requested_at=__import__("datetime").datetime.fromisoformat("2026-08-29T22:00:00+00:00"),
        source_evidence={},
        source_requests=[],
    )
    assert attempt["missing_classification"] == "authoritative_terminal"
    assert attempt["retryable"] is False
    assert attempt["authoritative_terminal"] is True
    assert attempt["learning_eligible"] is False
