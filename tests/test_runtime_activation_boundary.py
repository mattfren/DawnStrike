from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path

from scripts.runtime_activation_contract import activation_boundary


def _at(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace("Z", "+00:00"))


def test_activation_boundary_allows_next_session_after_completed_monday() -> None:
    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
    )

    assert result["status"] == "PASS"
    assert result["expected_market_date"] == "2026-09-01"
    assert result["window"] == "POST_MORNING_NEXT_SESSION"
    assert result["research_only"] is True
    assert result["broker_execution_enabled"] is False


def test_activation_boundary_rejects_same_day_after_close_and_arbitrary_dates() -> None:
    after_close = _at("2026-08-31T22:00:00Z")
    same_day = activation_boundary("2026-08-31", now=after_close)
    arbitrary = activation_boundary("2026-09-03", now=after_close)

    assert same_day["status"] == "BLOCKED"
    assert "activation_target_is_not_governed_next_session" in same_day["errors"]
    assert arbitrary["status"] == "BLOCKED"
    assert "activation_target_is_not_governed_next_session" in arbitrary["errors"]


def test_activation_boundary_allows_target_before_morning_but_not_after_morning() -> None:
    before_morning = activation_boundary(
        "2026-09-01",
        now=_at("2026-09-01T12:30:00Z"),  # 08:30 ET
    )
    after_morning = activation_boundary(
        "2026-09-01",
        now=_at("2026-09-01T14:00:00Z"),  # 10:00 ET
    )

    assert before_morning["status"] == "PASS"
    assert before_morning["window"] == "PRE_MORNING"
    assert after_morning["status"] == "BLOCKED"
    assert after_morning["expected_market_date"] == "2026-09-02"


def test_activation_boundary_blocks_target_date_public_build(tmp_path: Path) -> None:
    public = tmp_path / "runtime" / "build" / "public"
    public.mkdir(parents=True)
    (public / "build-manifest.json").write_text(
        json.dumps({"market_date": "2026-09-01"}),
        encoding="utf-8",
    )

    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    assert result["status"] == "BLOCKED"
    assert "target_date_has_authoritative_finalizer_or_public_artifacts" in result[
        "errors"
    ]
    assert any(path.endswith("build-manifest.json") for path in result["authoritative_artifacts"])


def test_activation_boundary_blocks_target_date_deployment_receipt(tmp_path: Path) -> None:
    build = tmp_path / "runtime" / "build"
    build.mkdir(parents=True)
    (build / "daily-deployment-result.json").write_text(
        json.dumps({"market_date": "2026-09-01", "status": "PRODUCTION_VERIFIED"}),
        encoding="utf-8",
    )

    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    assert result["status"] == "BLOCKED"
    assert any(
        path.endswith("daily-deployment-result.json")
        for path in result["authoritative_artifacts"]
    )


def test_activation_boundary_fails_closed_on_unbound_deployment_receipt(tmp_path: Path) -> None:
    build = tmp_path / "runtime" / "build"
    build.mkdir(parents=True)
    (build / "daily-deployment-result.json").write_text(
        json.dumps({"status": "PRODUCTION_VERIFIED"}),
        encoding="utf-8",
    )

    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    assert result["status"] == "BLOCKED"
    assert "deployment_market_date_missing" in result["errors"]


def test_activation_boundary_blocks_target_date_finalizer_output(tmp_path: Path) -> None:
    output = tmp_path / "state" / "outputs" / "daily_finalize" / "2026-09-01"
    output.mkdir(parents=True)
    (output / "daily-finalize-result.json").write_text("{}", encoding="utf-8")

    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
        state_root=tmp_path / "state",
    )

    assert result["status"] == "BLOCKED"
    assert any(
        path.endswith("daily-finalize-result.json")
        for path in result["authoritative_artifacts"]
    )


def test_activation_boundary_blocks_target_date_database_finalizer(tmp_path: Path) -> None:
    state = tmp_path / "state"
    state.mkdir()
    with sqlite3.connect(state / "shadow_real.sqlite") as connection:
        connection.execute(
            "CREATE TABLE daily_finalize_runs (market_date TEXT NOT NULL)"
        )
        connection.execute(
            "INSERT INTO daily_finalize_runs (market_date) VALUES (?)",
            ("2026-09-01",),
        )

    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
        state_root=state,
    )

    assert result["status"] == "BLOCKED"
    assert "sqlite:daily_finalize_runs:2026-09-01" in result["authoritative_artifacts"]


def test_activation_boundary_fails_closed_on_unbound_public_build(tmp_path: Path) -> None:
    public = tmp_path / "runtime" / "build" / "public"
    public.mkdir(parents=True)
    (public / "build-manifest.json").write_text("{}", encoding="utf-8")

    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    assert result["status"] == "BLOCKED"
    assert "public_build_market_date_missing" in result["errors"]


def test_activation_boundary_fails_closed_on_malformed_public_build(tmp_path: Path) -> None:
    public = tmp_path / "runtime" / "build" / "public"
    public.mkdir(parents=True)
    (public / "build-manifest.json").write_text("{not-json", encoding="utf-8")

    result = activation_boundary(
        "2026-09-01",
        now=_at("2026-08-31T22:00:00Z"),
        state_root=tmp_path / "state",
        runtime_root=tmp_path / "runtime",
    )

    assert result["status"] == "BLOCKED"
    assert "unreadable_public_artifact:build-manifest.json" in result["errors"]
