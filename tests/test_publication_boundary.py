"""Hostile, clock-seamed tests for the scheduled publication date boundary."""

from datetime import UTC, datetime
from pathlib import Path

from scripts.publication_boundary import (
    authorize_market_date,
    current_due_market_session,
    prepublication_authorization_id,
    validate_production_lineage,
)


def _after_close(day: str = "2026-08-31") -> datetime:
    return datetime.fromisoformat(f"{day}T21:00:00+00:00")


def test_current_due_session_is_authoritative_and_open() -> None:
    result = current_due_market_session(_after_close())
    assert result["ready"] is True
    assert result["expected_market_date"] == "2026-08-31"
    assert result["session"]["is_trading_day"] is True


def test_production_rejects_prior_future_and_closed_dates() -> None:
    now = _after_close()
    for requested, expected_error in (
        ("2026-08-28", "requested_market_date_not_current_due"),
        ("2026-09-01", "requested_market_date_not_current_due"),
    ):
        result = authorize_market_date(requested, publication_mode="Production", now=now)
        assert result["ready"] is False
        assert expected_error in result["errors"]

    closed = authorize_market_date(
        "2026-09-07", publication_mode="Production", now=datetime(2026, 9, 7, 21, tzinfo=UTC)
    )
    assert closed["ready"] is False
    assert "market_closed" in closed["errors"]


def test_preview_requires_due_session_but_local_only_preserves_replay() -> None:
    before_close = datetime.fromisoformat("2026-08-31T19:00:00+00:00")
    preview = authorize_market_date(
        "2026-08-31", publication_mode="Preview", now=before_close
    )
    assert preview["ready"] is False
    assert "session_not_due" in preview["errors"]

    replay = authorize_market_date(
        "2026-08-28", publication_mode="LocalOnly", now=before_close
    )
    assert replay["ready"] is True
    assert replay["offline_replay"] is True


def test_authorization_identity_is_immutable_over_daily_ledger_inputs() -> None:
    kwargs = {
        "expected_market_date": "2026-08-31",
        "release_sha": "a" * 40,
        "run_id": "daily-run",
        "stage_statuses": {
            "canonical_performance": {
                "status": "COMPLETE",
                "attempt_no": 1,
                "completed_at": "2026-08-31T21:00:00Z",
                "output_hash_sha256": "b" * 64,
            }
        },
        "artifact_identity": {
            "build_id": "c" * 20,
            "build_sha": "d" * 64,
            "publication_set_sha256": "e" * 64,
            "release_manifest_sha256": "f" * 64,
        },
    }
    identity = prepublication_authorization_id(**kwargs)
    kwargs["stage_statuses"]["canonical_performance"]["attempt_no"] = 2
    assert prepublication_authorization_id(**kwargs) != identity


def test_production_lineage_rejects_regression_and_divergent_frozen_day() -> None:
    current = {
        "market_date": "2026-08-31",
        "source_sha": "a" * 40,
        "build_sha": "b" * 64,
        "publication_set_sha256": "c" * 64,
        "release_manifest_sha256": "d" * 64,
    }
    prior = dict(current, market_date="2026-08-28")
    assert "candidate_market_date_regressive" in validate_production_lineage(
        current, prior
    )["errors"]
    divergent = dict(current, build_sha="e" * 64)
    assert "same_day_lineage_conflict" in validate_production_lineage(
        current, divergent
    )["errors"]
    assert validate_production_lineage(current, dict(current))["idempotent"] is True


def test_direct_promotion_requires_governed_identity_before_provider_calls() -> None:
    script = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    assert "Direct -Promote is blocked" in script
    assert "ExpectedMarketDate" in script
    assert "PrepublicationAuthorizationId" in script
    assert script.index("Direct -Promote is blocked") < script.index("Vercel prebuilt deploy")
