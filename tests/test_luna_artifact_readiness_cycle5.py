"""Hostile byte-lineage and V6 public-contract checks for cycle 5."""

import hashlib
import json
from pathlib import Path

from api import readiness
from scripts.build_public import _build_sha as producer_build_sha
from scripts.verify_public_artifact import _build_sha as verifier_build_sha
from scripts.verify_public_artifact import _v6_contract_failures


def _valid_v6() -> dict[str, object]:
    return {
        "schema_version": "dawnstrike.alphaops_v6.public_status.v1",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "decision_count": 0,
        "tracked_count": 0,
        "outcome_count": 0,
        "learning_eligible_outcome_count": 0,
        "latest_model_run": None,
        "latest_evaluation": None,
        "latest_drift": None,
        "operational_freshness": {
            "latest_daily_monitor": None,
            "latest_weekly_training": None,
        },
        "latest_promotion_review": None,
        "prediction_evidence_gate": {"passed": False},
        "failure_attribution": {},
        "account_comparison": None,
        "decision_replay": [],
        "promotion_readiness": {
            "status": "NOT_ELIGIBLE_FOR_PROMOTION",
            "automatic_promotion": False,
            "performance_status": "WAITING_FOR_FORWARD_EVIDENCE",
            "research_only": True,
            "broker_execution_enabled": False,
        },
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def test_v6_only_byte_change_changes_build_identity() -> None:
    common = {
        "source_sha": "a" * 40,
        "publication_set_sha256": "b" * 64,
        "opportunity_projection_sha256": "c" * 64,
        "market_date": "2026-08-28",
    }
    first = producer_build_sha(**common, v6_learning_sha256=hashlib.sha256(b"one").hexdigest())
    second = verifier_build_sha(**common, v6_learning_sha256=hashlib.sha256(b"two").hexdigest())
    assert first != second


def test_verifier_and_readiness_recompute_the_same_formula() -> None:
    args = {
        "source_sha": "a" * 40,
        "publication_set_sha256": "b" * 64,
        "opportunity_projection_sha256": "c" * 64,
        "v6_learning_sha256": "d" * 64,
        "market_date": "2026-08-28",
    }
    assert producer_build_sha(**args) == verifier_build_sha(**args) == readiness._build_sha(**args)


def test_v6_contract_rejects_flags_types_and_schema() -> None:
    payload = _valid_v6()
    assert _v6_contract_failures(payload) == []
    payload["schema_version"] = "wrong"
    payload["decision_count"] = "0"
    payload["research_only"] = False
    payload["promotion_readiness"] = {
        "status": "UNKNOWN",
        "automatic_promotion": True,
        "performance_status": "UNKNOWN",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    failures = _v6_contract_failures(payload)
    assert "v6_schema_version_invalid" in failures
    assert "v6_decision_count_invalid" in failures
    assert "v6_research_only_invalid" in failures
    assert "v6_automatic_promotion_invalid" in failures
    assert "v6_promotion_status_invalid" in failures


def test_readiness_cache_invalidates_changed_immutable_bytes(tmp_path: Path) -> None:
    path = tmp_path / "v6-learning.json"
    path.write_bytes(json.dumps(_valid_v6(), sort_keys=True).encode())
    original_root = readiness.PUBLIC_ROOT
    original_build = readiness.BUILD_MANIFEST_PATH
    try:
        readiness.PUBLIC_ROOT = tmp_path
        readiness.BUILD_MANIFEST_PATH = tmp_path / "build-manifest.json"
        readiness.BUILD_MANIFEST_PATH.write_text("{}", encoding="utf-8")
        first = readiness._read_cached_bytes(path)
        path.write_bytes(first + b" ")
        second = readiness._read_cached_bytes(path)
        assert first != second
    finally:
        readiness.PUBLIC_ROOT = original_root
        readiness.BUILD_MANIFEST_PATH = original_build


def test_stage_uses_packaged_files_without_embedded_payloads() -> None:
    script = Path("scripts/build_vercel_public_stage.ps1").read_text(encoding="utf-8")
    assert 'Copy-Item -Path (Join-Path $publicSource "*") -Destination $functionPublic' in script
    assert "snapshot_b64" not in script
    assert "calendar_b64" not in script
    assert "scenario_b64" not in script
    assert "opportunity_b64" not in script
    assert "performance-snapshot.json" not in script
