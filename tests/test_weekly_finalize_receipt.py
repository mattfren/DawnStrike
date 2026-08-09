from __future__ import annotations

import hashlib

import pytest

from intraday_scanner.errors import StorageError
from intraday_scanner.services.daily_run_service import (
    REQUIRED_FULL_CHAIN_STAGES,
    record_daily_stage,
)
from scripts.verify_daily_finalize_receipt import FINALIZE_STAGES, verify


def test_weekly_receipt_requires_complete_exact_release_daily_chain(tmp_path) -> None:
    db_path = tmp_path / "daily-state" / "daily.sqlite"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    market_date = "2026-08-03"
    release_sha = "a" * 40
    publication_set_sha = "publication-set-1"
    build_id = hashlib.sha256(
        f"{release_sha}:{publication_set_sha}:{market_date}".encode()
    ).hexdigest()[:20]

    with pytest.raises(StorageError, match="does not exist"):
        verify(db_path, market_date, release_sha)
    assert not db_path.exists()
    assert not db_path.parent.exists()

    for stage in REQUIRED_FULL_CHAIN_STAGES:
        record_daily_stage(
            db_path=db_path,
            market_date=market_date,
            stage_name=stage,
            status="COMPLETE",
            runtime_root=runtime,
            state_root=state,
            release_sha=release_sha,
            exit_code=0,
            payload=(
                {
                    "status": "PRODUCTION_VERIFIED",
                    "promoted": True,
                    "source_sha": release_sha,
                    "build_id": build_id,
                    "publication_set_sha256": publication_set_sha,
                    "promoted_deployment_id": "deployment-1",
                    "production_deployment_id": "deployment-1",
                }
                if stage == "publication"
                else None
            ),
        )

    result = verify(db_path, market_date, release_sha)
    wrong_release = verify(db_path, market_date, "b" * 40)

    assert result["ready"] is True
    assert result["run_status"] == "COMPLETE"
    assert result["missing_or_failed_finalize_stages"] == []
    assert result["publication_identity_ready"] is True
    assert result["expected_build_id"] == build_id
    assert wrong_release["ready"] is False

    record_daily_stage(
        db_path=db_path,
        market_date=market_date,
        stage_name="publication",
        status="COMPLETE",
        runtime_root=runtime,
        state_root=state,
        release_sha=release_sha,
        exit_code=0,
        payload={
            "status": "PRODUCTION_VERIFIED",
            "promoted": True,
            "source_sha": release_sha,
            "build_id": "not-the-promoted-build",
            "publication_set_sha256": publication_set_sha,
            "promoted_deployment_id": "deployment-a",
            "production_deployment_id": "deployment-b",
        },
    )
    mismatched = verify(db_path, market_date, release_sha)
    assert mismatched["ready"] is False
    assert mismatched["publication_identity_ready"] is False


def test_weekly_receipt_rejects_skipped_finalize_stages(tmp_path) -> None:
    db_path = tmp_path / "daily.sqlite"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    market_date = "2026-08-03"
    release_sha = "c" * 40

    for stage in REQUIRED_FULL_CHAIN_STAGES:
        record_daily_stage(
            db_path=db_path,
            market_date=market_date,
            stage_name=stage,
            status="SKIPPED_NOT_APPLICABLE",
            runtime_root=runtime,
            state_root=state,
            release_sha=release_sha,
            exit_code=0,
        )

    result = verify(db_path, market_date, release_sha)

    assert result["run_status"] == "COMPLETE"
    assert result["ready"] is False
    assert result["missing_or_failed_finalize_stages"] == list(FINALIZE_STAGES)
