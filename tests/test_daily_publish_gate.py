import gzip
import hashlib
import json
from pathlib import Path

from scripts.verify_public_artifact import verify


def _v6_fixture() -> dict[str, object]:
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


def test_artifact_gate_reports_bounded_public_payload(tmp_path: Path) -> None:
    result = verify(tmp_path / "missing-public")
    assert result["status"] == "FAIL"
    assert "missing:build-manifest.json" in result["errors"]


def _write_publishable_fixture(
    tmp_path: Path,
    *,
    snapshot_status: str,
    readiness_status: str,
    readiness_http_status: int,
) -> Path:
    root = tmp_path / "public"
    (root / "data").mkdir(parents=True)
    required = {
        "index.html": b"<main>fixture</main>",
        "favicon.svg": b"<svg />",
        "readiness.json": json.dumps(
            {
                "status": readiness_status,
                "http_status": readiness_http_status,
                "snapshot_status": snapshot_status,
                "live_trading_enabled": False,
            }
        ).encode(),
        "stage-manifest.json": b"{}",
        "assets/dawnstrike.css": b"body {}",
        "assets/dawnstrike.js": b"console.log('fixture')",
        "data/performance.json": b'{"rows":[]}',
        "data/v6-learning.json": json.dumps(_v6_fixture(), sort_keys=True).encode(),
        "data/scenarios.json": b'{"records":[],"performance":[]}',
    }
    for name, payload in required.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(payload)
    snapshot = root / "data" / "performance.json"
    snapshot_hash = hashlib.sha256(snapshot.read_bytes()).hexdigest()
    canonical_hash = hashlib.sha256(b"canonical-fixture").hexdigest()
    (root / "data" / "performance.json.manifest.json").write_text(
        json.dumps(
            {
                "status": snapshot_status,
                "market_date": "2026-08-28",
                "manifest_id": "performance-fixture",
                "input_hash_sha256": canonical_hash,
                "payload_sha256": snapshot_hash,
                "byte_count": snapshot.stat().st_size,
                "compressed_byte_count": len(
                    gzip.compress(snapshot.read_bytes(), compresslevel=9, mtime=0)
                ),
                "compression": "gzip",
            }
        ),
        encoding="utf-8",
    )
    required["data/performance.json.manifest.json"] = (
        root / "data" / "performance.json.manifest.json"
    ).read_bytes()
    scenarios = root / "data" / "scenarios.json"
    (root / "data" / "scenarios.json.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.scenarios_public_manifest.v1",
                "payload_sha256": hashlib.sha256(scenarios.read_bytes()).hexdigest(),
                "calibration_status": "UNCALIBRATED",
            }
        ),
        encoding="utf-8",
    )
    scenario_hash = hashlib.sha256(scenarios.read_bytes()).hexdigest()
    current_ledger_lineage = "4" * 64
    current_session_lineage_sha256 = hashlib.sha256(
        json.dumps(
            [current_ledger_lineage], sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
    account_session_report = {
        "schema_version": "dawnstrike.account_session_report.v1",
        "status": "COMPLETE",
        "market_date": "2026-08-28",
        "code_sha": "clean-sha",
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
        "ledger_lineage_sha256": current_session_lineage_sha256,
        "current_session_lineage_sha256": current_session_lineage_sha256,
        "expected_current_session_lineage_sha256": current_session_lineage_sha256,
        "current_session_lineage_match": True,
        "research_only": True,
        "broker_execution_enabled": False,
        "series": [
            {
                "status": "COMPLETE",
                "market_date": "2026-08-28",
                "code_sha": "clean-sha",
                "account_id": "alphaops_v5_simulated",
                "version_bucket": "v5",
                "cohort": "official_forward_paper",
                "strategy_id": "alphaops_v5",
                "strategy_version": "dawnstrike-alphaops-v5.0.0",
                "expected_session_count": 1,
                "ledger_row_count": 1,
                "complete_count": 1,
                "ledger_lineage_sha256": current_session_lineage_sha256,
                "current_session_lineage_sha256": current_session_lineage_sha256,
                "expected_current_session_lineage_sha256": (
                    current_session_lineage_sha256
                ),
                "current_session_lineage_match": True,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        ],
    }
    opportunity = root / "data" / "opportunity-projection.json"
    opportunity.write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.opportunity_projection.v1",
                "state": "DISABLED",
                "message": "",
                "source_run_id": None,
                "as_of": None,
                "rows": [],
                "row_count": 0,
                "research_only": True,
                "order_execution_enabled": False,
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    opportunity_hash = hashlib.sha256(opportunity.read_bytes()).hexdigest()
    (root / "data" / "opportunity-projection.json.manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.opportunity_projection_manifest.v1",
                "payload_sha256": opportunity_hash,
                "byte_count": opportunity.stat().st_size,
                "state": "DISABLED",
                "row_count": 0,
            }
        ),
        encoding="utf-8",
    )
    calendar = root / "data" / "calendar.json"
    calendar.write_bytes(
        json.dumps(
            {"as_of_market_date": "2026-08-28", "days": []},
            sort_keys=True,
        ).encode()
    )
    calendar_hash = hashlib.sha256(calendar.read_bytes()).hexdigest()
    calendar_manifest = {
        "market_date": "2026-08-28",
        "status": snapshot_status,
        "manifest_id": "calendar-fixture",
        "canonical_input_hash_sha256": canonical_hash,
        "performance_payload_sha256": snapshot_hash,
        "payload_sha256": calendar_hash,
    }
    (root / "data" / "calendar.json.manifest.json").write_text(
        json.dumps(calendar_manifest),
        encoding="utf-8",
    )
    publication_pair = {
        "market_date": "2026-08-28",
        "canonical_input_hash_sha256": canonical_hash,
        "performance_payload_sha256": snapshot_hash,
        "calendar_payload_sha256": calendar_hash,
        "performance_manifest_id": "performance-fixture",
        "calendar_manifest_id": "calendar-fixture",
        "scenario_payload_sha256": scenario_hash,
        "scenario_schema_version": "dawnstrike.scenarios_public_manifest.v1",
        "account_session_status": "COMPLETE",
        "account_session_input_hash_sha256": "1" * 64,
        "account_session_expected_calendar_hash_sha256": "2" * 64,
        "account_session_code_sha": "clean-sha",
        "account_session_account_id": "alphaops_v5_simulated",
        "account_session_version_bucket": "v5",
        "account_session_cohort": "official_forward_paper",
        "account_session_strategy_id": "alphaops_v5",
        "account_session_strategy_version": "dawnstrike-alphaops-v5.0.0",
        "account_session_ledger_lineage_sha256": current_session_lineage_sha256,
        "account_session_current_session_lineage_sha256": (
            current_session_lineage_sha256
        ),
        "account_session_expected_current_session_lineage_sha256": (
            current_session_lineage_sha256
        ),
        "account_session_current_session_lineage_match": True,
    }
    publication_set_hash = hashlib.sha256(
        json.dumps(publication_pair, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    scenario_manifest_hash = hashlib.sha256(
        (root / "data" / "scenarios.json.manifest.json").read_bytes()
    ).hexdigest()
    (root / "data" / "publication-set.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.publication_set.v2",
                "market_date": "2026-08-28",
                "canonical_input_hash_sha256": canonical_hash,
                "performance_payload_sha256": snapshot_hash,
                "calendar_payload_sha256": calendar_hash,
                "performance_manifest_id": "performance-fixture",
                "calendar_manifest_id": "calendar-fixture",
                "scenario_payload_sha256": scenario_hash,
                "scenario_manifest_sha256": scenario_manifest_hash,
                "account_session_status": "COMPLETE",
                "account_session_input_hash_sha256": "1" * 64,
                "account_session_expected_calendar_hash_sha256": "2" * 64,
                "account_session_code_sha": "clean-sha",
                "account_session_account_id": "alphaops_v5_simulated",
                "account_session_version_bucket": "v5",
                "account_session_cohort": "official_forward_paper",
                "account_session_strategy_id": "alphaops_v5",
                "account_session_strategy_version": "dawnstrike-alphaops-v5.0.0",
                "account_session_ledger_lineage_sha256": (
                    current_session_lineage_sha256
                ),
                "account_session_current_session_lineage_sha256": (
                    current_session_lineage_sha256
                ),
                "account_session_expected_current_session_lineage_sha256": (
                    current_session_lineage_sha256
                ),
                "account_session_current_session_lineage_match": True,
                "publication_set_sha256": publication_set_hash,
                "generated_at": "2026-08-28T21:00:00+00:00",
                "research_only": True,
                "live_trading_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    v6_hash = hashlib.sha256((root / "data/v6-learning.json").read_bytes()).hexdigest()
    build_sha = hashlib.sha256(
        f"clean-sha:{publication_set_hash}:{opportunity_hash}:{v6_hash}:2026-08-28".encode()
    ).hexdigest()
    build_id = build_sha[:20]
    readiness_payload = {
        "status": readiness_status,
        "http_status": readiness_http_status,
        "snapshot_status": snapshot_status,
        "market_date": "2026-08-28",
        "live_trading_enabled": False,
        "research_only": True,
        "safety_status": "verified",
        "v6_learning_sha256": v6_hash,
        "build_id": build_id,
        "deployed_build_sha": build_sha,
        "publication_set_sha256": publication_set_hash,
        "account_session_report": account_session_report,
        "account_session_reconciliation": {
            "schema_version": "dawnstrike.daily_account_reconciliation.v1",
            "status": "COMPLETE",
            "market_date": "2026-08-28",
            "release_sha": "clean-sha",
            "account_id": "alphaops_v5_simulated",
            "account_status": "AUTHENTICATED_NO_TRADE",
            "ledger_lineage_sha256": current_ledger_lineage,
            "research_only": True,
            "broker_execution_enabled": False,
        },
    }
    (root / "readiness.json").write_text(json.dumps(readiness_payload), encoding="utf-8")
    required["readiness.json"] = (root / "readiness.json").read_bytes()
    required.update(
        {
            "data/calendar.json": calendar.read_bytes(),
            "data/calendar.json.manifest.json": (
                root / "data" / "calendar.json.manifest.json"
            ).read_bytes(),
            "data/publication-set.json": (
                root / "data" / "publication-set.json"
            ).read_bytes(),
            "data/scenarios.json.manifest.json": (
                root / "data" / "scenarios.json.manifest.json"
            ).read_bytes(),
            "data/opportunity-projection.json": opportunity.read_bytes(),
            "data/opportunity-projection.json.manifest.json": (
                root / "data" / "opportunity-projection.json.manifest.json"
            ).read_bytes(),
        }
    )
    release_payload = {
        "schema_version": "dawnstrike.release_manifest.v1",
        "source_sha": "clean-sha",
        "build_sha": build_sha,
        "v6_learning_sha256": v6_hash,
        "deployment_boundary": "configured_runtime_and_durable_state",
        "deployment_boundary_sha256": "a" * 64,
        "database_schema_version": 30,
        "data_watermark": "2026-08-28",
        "strategy_versions": {
            "alphaops_v5": "dawnstrike-alphaops-v5.0.0",
            "alphaops_v6_shadow": "dawnstrike-alphaops-v6-shadow",
            "paperops": "immutable-strategy-semantics-manifest",
        },
        "scheduler_version": "dawnstrike-scheduler-v6",
        "artifact_hashes": {
            name: hashlib.sha256((root / name).read_bytes()).hexdigest()
            for name in required
        },
        "created_at": "2026-08-28T21:00:00+00:00",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    release_payload["release_manifest_sha256"] = hashlib.sha256(
        json.dumps(release_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    (root / "release-manifest.json").write_text(
        json.dumps(release_payload), encoding="utf-8"
    )
    required["release-manifest.json"] = (root / "release-manifest.json").read_bytes()
    file_hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in required
    }
    (root / "build-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.public_build.v1",
                "source_sha": "clean-sha",
                "source_clean": True,
                "build_id": build_id,
                "build_sha": build_sha,
                "market_date": "2026-08-28",
                "v6_learning_sha256": v6_hash,
                "data_hash_sha256": snapshot_hash,
                "publication_set_sha256": publication_set_hash,
                "opportunity_projection_sha256": opportunity_hash,
                "release_manifest_sha256": release_payload["release_manifest_sha256"],
                "generated_at": "2026-08-28T21:00:00+00:00",
                "status": "READY",
                "readiness": {},
                "file_hashes": file_hashes,
                "research_only": True,
                "live_trading_enabled": False,
                "broker_execution_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    return root


def test_artifact_gate_accepts_clean_explicit_no_trade_fixture(tmp_path: Path) -> None:
    root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )

    result = verify(root)

    assert result["status"] == "PASS"
    assert result["snapshot_status"] == "no_trade"


def test_artifact_gate_accepts_only_explicitly_approved_degraded_fixture(
    tmp_path: Path,
) -> None:
    root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="degraded",
        readiness_status="not_ready",
        readiness_http_status=503,
    )

    blocked = verify(root)
    approved = verify(root, allow_degraded=True)

    assert blocked["status"] == "FAIL"
    assert blocked["errors"] == [
        "snapshot_not_publishable",
        "readiness_not_publishable",
    ]
    assert approved["status"] == "PASS"
    assert approved["snapshot_status"] == "degraded"
    assert approved["readiness_http_status"] == 503


def test_artifact_gate_rejects_host_path_disclosure(tmp_path: Path) -> None:
    root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    (root / "readiness.json").write_text(
        json.dumps({"runtime_root": r"C:\r\dawnstrike-runtime"}),
        encoding="utf-8",
    )

    result = verify(root)

    assert "forbidden_absolute_path:readiness.json" in result["errors"]
