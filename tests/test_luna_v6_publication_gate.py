"""Hostile V6 publication-lineage and prepublication-gate checks."""

import hashlib
import json
from pathlib import Path

from intraday_scanner.services.daily_run_service import (
    REQUIRED_FULL_CHAIN_STAGES,
    record_daily_stage,
)
from scripts import public_lineage
from scripts.verify_daily_finalize_receipt import (
    _is_structurally_pre_v6_receipt,
)
from scripts.verify_daily_finalize_receipt import verify as verify_finalize_receipt
from scripts.verify_daily_prepublication import verify as verify_prepublication
from scripts.verify_public_artifact import _build_sha as verifier_build_sha
from scripts.verify_public_artifact import verify as verify_artifact
from tests.test_daily_publish_gate import _write_publishable_fixture


def test_artifact_rejects_malformed_v6_hash_even_when_bytes_are_unchanged(tmp_path: Path) -> None:
    root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    manifest_path = root / "build-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["v6_learning_sha256"] = "A" * 64
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_artifact(root)

    assert result["status"] == "FAIL"
    assert "build_v6_learning_hash_mismatch" in result["errors"]
    assert "build_v6_learning_sha256_invalid" in result["errors"]


def test_publisher_propagates_and_compares_v6_lineage_across_deployments() -> None:
    script = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")

    assert "v6_learning_sha256 = $previewManifest.v6_learning_sha256" in script
    assert "$promotedManifest.v6_learning_sha256 -ne $previewManifest.v6_learning_sha256" in script
    assert (
        "$productionManifest.v6_learning_sha256 -ne "
        "$previewManifest.v6_learning_sha256"
    ) in script
    assert "strict five-input V6 formula" in script


def test_verifier_formula_does_not_delegate_to_producer_helper(monkeypatch) -> None:
    monkeypatch.setattr(public_lineage, "build_sha", lambda **_: "attacker-controlled")
    args = {
        "source_sha": "a" * 40,
        "publication_set_sha256": "b" * 64,
        "opportunity_projection_sha256": "c" * 64,
        "v6_learning_sha256": "d" * 64,
        "market_date": "2026-08-28",
    }
    expected = hashlib.sha256(
        f"{args['source_sha']}:{args['publication_set_sha256']}:{args['opportunity_projection_sha256']}:{args['v6_learning_sha256']}:{args['market_date']}".encode()
    ).hexdigest()

    assert verifier_build_sha(**args) == expected


def test_legacy_receipt_compatibility_requires_absence_of_all_v6_identity_fields() -> None:
    assert _is_structurally_pre_v6_receipt(
        {"publication_set_sha256": "b" * 64, "opportunity_projection_sha256": "c" * 64}
    )
    assert not _is_structurally_pre_v6_receipt(
        {
            "publication_set_sha256": "b" * 64,
            "opportunity_projection_sha256": "c" * 64,
            "schema_version": "dawnstrike.daily_deployment.v1",
        }
    )


def test_finalize_receipt_rejects_missing_v6_input_for_strict_hash_inputs(tmp_path: Path) -> None:
    db_path = tmp_path / "daily.sqlite"
    runtime = tmp_path / "runtime"
    state = tmp_path / "state"
    runtime.mkdir()
    state.mkdir()
    market_date = "2026-08-28"
    release_sha = "a" * 40
    publication_sha = "b" * 64
    opportunity_sha = "c" * 64
    build_sha = hashlib.sha256(
        f"{release_sha}:{publication_sha}:{opportunity_sha}:{'d' * 64}:{market_date}".encode()
    ).hexdigest()
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
            payload=(
                {
                    "status": "PRODUCTION_VERIFIED",
                    "schema_version": "dawnstrike.daily_deployment.v1",
                    "promoted": True,
                    "source_sha": release_sha,
                    "build_id": build_sha[:20],
                    "build_sha": build_sha,
                    "publication_set_sha256": publication_sha,
                    "opportunity_projection_sha256": opportunity_sha,
                    "promoted_deployment_id": "deployment-1",
                    "production_deployment_id": "deployment-1",
                }
                if stage == "publication"
                else None
            ),
        )

    result = verify_finalize_receipt(db_path, market_date, release_sha)

    assert result["ready"] is False
    assert result["publication_identity_ready"] is False


def test_publisher_rejects_degraded_promotion_before_any_upload() -> None:
    script = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")

    guard = 'if ($Promote -and $AllowDegraded) {'
    assert guard in script
    assert script.index(guard) < script.index("build_vercel_public_stage.ps1")
    assert "Production promotion requires readiness HTTP 200" in script


def test_prepublication_gate_is_fail_closed_and_excludes_publication_stage(tmp_path: Path) -> None:
    root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    result = verify_prepublication(
        tmp_path / "missing.sqlite",
        root,
        "2026-08-28",
        "a" * 40,
    )

    assert result["status"] == "BLOCKED"
    assert result["publication_stage_excluded"] is True
    assert "publication" not in result["required_stages"]
    assert any(error.startswith("daily_run_unreadable:") for error in result["errors"])
