from __future__ import annotations

import hashlib
import json
from copy import deepcopy

import pytest

from intraday_scanner.errors import StorageError
from intraday_scanner.services.daily_run_service import (
    REQUIRED_FULL_CHAIN_STAGES,
    record_daily_stage,
)
from scripts.verify_daily_finalize_receipt import FINALIZE_STAGES, verify


def _strict_publication_payload(market_date: str, release_sha: str) -> dict[str, object]:
    publication_set_sha = "b" * 64
    opportunity_projection_sha = "c" * 64
    v6_learning_sha = "d" * 64
    build_sha = hashlib.sha256(
        (
            f"{release_sha}:{publication_set_sha}:{opportunity_projection_sha}:"
            f"{v6_learning_sha}:{market_date}"
        ).encode()
    ).hexdigest()
    report = {
        "schema_version": "dawnstrike.account_session_report.v1",
        "status": "COMPLETE",
        "market_date": market_date,
        "code_sha": release_sha,
        "account_id": "alphaops_v5_simulated",
        "version_bucket": "v5",
        "cohort": "official_forward_paper",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
        "expected_session_count": 1,
        "ledger_row_count": 1,
        "complete_count": 1,
        "missing_count": 0,
        "partial_count": 0,
        "quarantined_count": 0,
        "unsafe_ledger_count": 0,
        "input_hash_sha256": "1" * 64,
        "expected_calendar_hash_sha256": "2" * 64,
        "source_hashes_sha256": "3" * 64,
        "research_only": True,
        "broker_execution_enabled": False,
        "series": [
            {
                "status": "COMPLETE",
                "market_date": market_date,
                "code_sha": release_sha,
                "account_id": "alphaops_v5_simulated",
                "version_bucket": "v5",
                "cohort": "official_forward_paper",
                "strategy_id": "alphaops_v5",
                "strategy_version": "dawnstrike-alphaops-v5.0.0",
                "expected_session_count": 1,
                "ledger_row_count": 1,
                "complete_count": 1,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        ],
    }
    report_hash = hashlib.sha256(
        json.dumps(report, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    alias = "https://dawnstrike.example.vercel.app"
    return {
        "schema_version": "dawnstrike.daily_deployment.v1",
        "status": "PRODUCTION_VERIFIED",
        "promoted": True,
        "allow_degraded": False,
        "source_sha": release_sha,
        "source_tree": "e" * 40,
        "market_date": market_date,
        "expected_market_date": market_date,
        "build_id": build_sha[:20],
        "build_sha": build_sha,
        "publication_set_sha256": publication_set_sha,
        "opportunity_projection_sha256": opportunity_projection_sha,
        "v6_learning_sha256": v6_learning_sha,
        "build_manifest_sha256": "4" * 64,
        "authorized_build_manifest_sha256": "5" * 64,
        "authorized_release_manifest_sha256": "6" * 64,
        "public_artifact_root_sha256": "7" * 64,
        "toolchain_identity_sha256": "8" * 64,
        "vercel_source_manifest_sha256": "9" * 64,
        "vercel_package_manifest_sha256": "a" * 64,
        "release_manifest_sha256": "b" * 64,
        "account_session_report": report,
        "account_session_report_sha256": report_hash,
        "prepublication_authorization_id": "c" * 64,
        "daily_ledger_authorization_id": "c" * 64,
        "production_aliases": [alias],
        "preview_url": "https://preview.example.vercel.app",
        "preview_artifact_proof": {
            "endpoint": "https://preview.example.vercel.app",
            "build_sha": build_sha,
            "asset_count": 17,
            "total_bytes": 1000,
            "file_hashes_sha256": "7" * 64,
        },
        "production_artifact_proofs": [
            {
                "endpoint": alias,
                "build_sha": build_sha,
                "asset_count": 17,
                "total_bytes": 1000,
                "file_hashes_sha256": "7" * 64,
            }
        ],
        "promoted_deployment_id": "deployment-1",
        "production_deployment_id": "deployment-1",
        "research_only": True,
        "live_trading_enabled": False,
        "broker_execution_enabled": False,
    }


def test_weekly_receipt_requires_complete_exact_release_daily_chain(tmp_path) -> None:
    db_path = tmp_path / "daily-state" / "daily.sqlite"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    market_date = "2026-08-03"
    release_sha = "a" * 40
    publication_payload = _strict_publication_payload(market_date, release_sha)
    build_id = publication_payload["build_id"]

    with pytest.raises(StorageError, match="does not exist"):
        verify(db_path, market_date, release_sha)
    assert not db_path.exists()
    assert not db_path.parent.exists()

    for stage in REQUIRED_FULL_CHAIN_STAGES:
        record_daily_stage(
            db_path=db_path,
            market_date=market_date,
            stage_name=stage,
            status=("NO_TRADE" if stage == "calendar_build" else "COMPLETE"),
            runtime_root=runtime,
            state_root=state,
            release_sha=release_sha,
            exit_code=0,
            payload=(
                publication_payload
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
            "publication_set_sha256": "b" * 64,
            "opportunity_projection_sha256": "c" * 64,
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

    # Recording only SKIPPED_NOT_APPLICABLE stages leaves the daily run in
    # that explicit terminal status; the Finalize gate still rejects it.
    assert result["run_status"] == "SKIPPED_NOT_APPLICABLE"
    assert result["ready"] is False
    assert result["missing_or_failed_finalize_stages"] == list(FINALIZE_STAGES)


@pytest.mark.parametrize("mutation", ["authorization", "proof", "broker", "account"])
def test_weekly_receipt_rejects_self_consistent_forged_terminal_evidence(
    tmp_path, mutation: str
) -> None:
    market_date = "2026-08-03"
    release_sha = "a" * 40
    payload = deepcopy(_strict_publication_payload(market_date, release_sha))
    if mutation == "authorization":
        payload["daily_ledger_authorization_id"] = "d" * 64
    elif mutation == "proof":
        payload["production_artifact_proofs"][0]["endpoint"] = "https://other.invalid"
    elif mutation == "broker":
        payload["broker_execution_enabled"] = "false"
    else:
        payload["account_session_report"]["account_id"] = "shadow-account"
        payload["account_session_report_sha256"] = hashlib.sha256(
            json.dumps(
                payload["account_session_report"],
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()

    db_path = tmp_path / mutation / "daily.sqlite"
    runtime = tmp_path / mutation / "runtime"
    state = tmp_path / mutation / "state"
    runtime.mkdir(parents=True)
    state.mkdir(parents=True)
    for stage in REQUIRED_FULL_CHAIN_STAGES:
        record_daily_stage(
            db_path=db_path,
            market_date=market_date,
            stage_name=stage,
            status="NO_TRADE" if stage == "calendar_build" else "COMPLETE",
            runtime_root=runtime,
            state_root=state,
            release_sha=release_sha,
            exit_code=0,
            payload=payload if stage == "publication" else None,
        )

    result = verify(db_path, market_date, release_sha)
    assert result["ready"] is False
    assert result["publication_identity_ready"] is False
