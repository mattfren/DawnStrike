import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from test_luna_artifact_readiness_cycle5 import _valid_v6


def test_minimal_health_and_readiness_modules_import_without_scanner_runtime() -> None:
    import api.health as health
    import api.readiness as readiness

    assert health.handler is not None
    assert readiness.handler is not None


def test_health_uses_embedded_public_state_when_static_manifest_is_not_packaged(
    tmp_path: Path, monkeypatch
) -> None:
    from api import health

    monkeypatch.setattr(health, "REPOSITORY_ROOT", tmp_path)
    monkeypatch.setattr(
        health,
        "PUBLIC_STATE",
        {"build_manifest": {"source_sha": "abc123", "build_id": "build123"}},
    )
    assert health._build_metadata() == {"source_sha": "abc123", "build_id": "build123"}


def test_readiness_accepts_complete_hash_consistent_public_state(
    tmp_path: Path, monkeypatch
) -> None:
    from api import readiness

    current_market_date = datetime.now(ZoneInfo("America/Chicago")).date().isoformat()
    public_root = tmp_path / "public"
    data_root = public_root / "data"
    data_root.mkdir(parents=True)
    (data_root / "v6-learning.json").write_text(
        json.dumps(_valid_v6(), sort_keys=True), encoding="utf-8"
    )
    payload = b'{"rows":[]}'
    snapshot = data_root / "performance.json"
    snapshot.write_bytes(payload)
    snapshot_hash = hashlib.sha256(payload).hexdigest()
    canonical_hash = hashlib.sha256(b"canonical-fixture").hexdigest()
    (data_root / "performance.json.manifest.json").write_text(
        json.dumps(
            {
                "payload_sha256": snapshot_hash,
                "byte_count": len(payload),
                "status": "no_trade",
                "input_hash_sha256": canonical_hash,
            }
        ),
        encoding="utf-8",
    )
    calendar = data_root / "calendar.json"
    calendar_freshness = {
        "schema_version": "dawnstrike.calendar_freshness.v1",
        "status": "current",
        "generated_at": datetime.now(ZoneInfo("UTC")).replace(microsecond=0).isoformat(),
        "timezone": "America/Chicago",
        "publication_time_local": "17:30",
        "publication_cadence": "market_days",
        "authoritative_as_of_market_date": current_market_date,
        "latest_expected_market_date": current_market_date,
        "expected_publication_at": None,
        "stale_after": None,
        "next_publication_market_date": current_market_date,
        "next_publication_at": None,
        "next_stale_after": None,
        "grace_period_seconds": 3600,
        "fail_closed": True,
        "research_only": True,
        "live_trading_enabled": False,
    }
    calendar.write_text(
        json.dumps({"days": [], "freshness": calendar_freshness}), encoding="utf-8"
    )
    calendar_hash = hashlib.sha256(calendar.read_bytes()).hexdigest()
    calendar_manifest = data_root / "calendar.json.manifest.json"
    calendar_manifest.write_text(
        json.dumps(
                {
                    "payload_sha256": calendar_hash,
                    "canonical_input_hash_sha256": canonical_hash,
                    "performance_payload_sha256": snapshot_hash,
                    "freshness": calendar_freshness,
            }
        ),
        encoding="utf-8",
    )
    scenario = data_root / "scenarios.json"
    scenario.write_bytes(b'{"records":[],"performance":[]}')
    scenario_hash = hashlib.sha256(scenario.read_bytes()).hexdigest()
    scenario_manifest = data_root / "scenarios.json.manifest.json"
    scenario_manifest.write_text(
        json.dumps(
            {
                "payload_sha256": scenario_hash,
                "calibration_status": "UNCALIBRATED",
            }
        ),
        encoding="utf-8",
    )
    opportunity = data_root / "opportunity-projection.json"
    opportunity.write_text(
        json.dumps(
            {
                "state": "DISABLED",
                "rows": [],
                "row_count": 0,
                "research_only": True,
                "order_execution_enabled": False,
            },
            sort_keys=True,
            separators=(",", ":"),
        ),
        encoding="utf-8",
    )
    opportunity_hash = hashlib.sha256(opportunity.read_bytes()).hexdigest()
    opportunity_manifest = data_root / "opportunity-projection.json.manifest.json"
    opportunity_manifest.write_text(
        json.dumps(
            {
                "payload_sha256": opportunity_hash,
                "byte_count": opportunity.stat().st_size,
                "state": "DISABLED",
                "row_count": 0,
            }
        ),
        encoding="utf-8",
    )
    publication_set_hash = hashlib.sha256(
        f"{snapshot_hash}:{calendar_hash}".encode()
    ).hexdigest()
    publication_set = data_root / "publication-set.json"
    publication_set.write_text(
        json.dumps(
            {
                "performance_payload_sha256": snapshot_hash,
                "calendar_payload_sha256": calendar_hash,
                "scenario_payload_sha256": scenario_hash,
                "publication_set_sha256": publication_set_hash,
            }
        ),
        encoding="utf-8",
    )
    required_hashes = {"data/performance.json": snapshot_hash}
    for name in sorted(readiness.REQUIRED_HASHED_FILES - set(required_hashes)):
        path = public_root / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        required_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    v6_hash = hashlib.sha256((data_root / "v6-learning.json").read_bytes()).hexdigest()
    build_sha = hashlib.sha256(
        f"abc123:{publication_set_hash}:{opportunity_hash}:{v6_hash}:{current_market_date}".encode()
    ).hexdigest()
    (public_root / "release-manifest.json").write_text(
        json.dumps({"build_sha": build_sha, "v6_learning_sha256": v6_hash}),
        encoding="utf-8",
    )
    required_hashes["release-manifest.json"] = hashlib.sha256(
        (public_root / "release-manifest.json").read_bytes()
    ).hexdigest()
    build_manifest = public_root / "build-manifest.json"
    build_manifest.write_text(
        json.dumps(
            {
                "source_sha": "abc123",
                "build_id": build_sha[:20],
                "build_sha": build_sha,
                "market_date": current_market_date,
                "v6_learning_sha256": v6_hash,
                    "source_clean": True,
                    "data_hash_sha256": snapshot_hash,
                    "publication_set_sha256": publication_set_hash,
                    "opportunity_projection_sha256": opportunity_hash,
                    "file_hashes": required_hashes,
            }
        ),
        encoding="utf-8",
    )
    readiness_payload = {
        "status": "ready",
        "http_status": 200,
        "snapshot_status": "no_trade",
        "market_date": current_market_date,
        "live_trading_enabled": False,
        "research_only": True,
        "safety_status": "verified",
        "v6_learning_sha256": v6_hash,
        "build_id": build_sha[:20],
        "deployed_build_sha": build_sha,
        "calendar_freshness": calendar_freshness,
    }
    monkeypatch.setattr(readiness, "PUBLIC_ROOT", public_root)
    monkeypatch.setattr(readiness, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(
        readiness, "SNAPSHOT_MANIFEST_PATH", data_root / "performance.json.manifest.json"
    )
    monkeypatch.setattr(readiness, "CALENDAR_PATH", calendar)
    monkeypatch.setattr(readiness, "CALENDAR_MANIFEST_PATH", calendar_manifest)
    monkeypatch.setattr(readiness, "SCENARIO_PATH", scenario)
    monkeypatch.setattr(readiness, "SCENARIO_MANIFEST_PATH", scenario_manifest)
    monkeypatch.setattr(readiness, "OPPORTUNITY_PATH", opportunity)
    monkeypatch.setattr(readiness, "OPPORTUNITY_MANIFEST_PATH", opportunity_manifest)
    monkeypatch.setattr(readiness, "PUBLICATION_SET_PATH", publication_set)
    monkeypatch.setattr(readiness, "BUILD_MANIFEST_PATH", build_manifest)
    monkeypatch.setattr(readiness, "V6_LEARNING_PATH", data_root / "v6-learning.json")
    monkeypatch.setattr(readiness, "RELEASE_MANIFEST_PATH", public_root / "release-manifest.json")
    assert readiness._validate_public_state(readiness_payload) == []


def test_readiness_rejects_degraded_public_state(tmp_path: Path, monkeypatch) -> None:
    from api import readiness

    public_root = tmp_path / "public"
    data_root = public_root / "data"
    data_root.mkdir(parents=True)
    payload = b'{"rows":[]}'
    snapshot = data_root / "performance.json"
    snapshot.write_bytes(payload)
    snapshot_hash = hashlib.sha256(payload).hexdigest()
    snapshot_manifest = data_root / "performance.json.manifest.json"
    snapshot_manifest.write_text(
        json.dumps(
            {"payload_sha256": snapshot_hash, "byte_count": len(payload), "status": "degraded"}
        ),
        encoding="utf-8",
    )
    required_hashes = {"data/performance.json": snapshot_hash}
    for name in sorted(readiness.REQUIRED_HASHED_FILES - set(required_hashes)):
        path = public_root / name
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"fixture")
        required_hashes[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    build_manifest = public_root / "build-manifest.json"
    build_manifest.write_text(
        json.dumps(
            {
                "source_sha": "abc123",
                "build_id": "build123",
                "source_clean": True,
                "data_hash_sha256": snapshot_hash,
                "file_hashes": required_hashes,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(readiness, "PUBLIC_ROOT", public_root)
    monkeypatch.setattr(readiness, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(readiness, "SNAPSHOT_MANIFEST_PATH", snapshot_manifest)
    monkeypatch.setattr(readiness, "BUILD_MANIFEST_PATH", build_manifest)
    failures = readiness._validate_public_state(
        {
            "status": "not_ready",
            "http_status": 503,
            "snapshot_status": "degraded",
            "market_date": "2026-07-29",
            "live_trading_enabled": False,
            "research_only": True,
            "safety_status": "verified",
        }
    )
    assert "snapshot_not_publishable" in failures
    assert "pipeline_not_ready" in failures


def test_readiness_market_calendar_skips_exchange_holidays() -> None:
    from api import readiness

    assert not readiness._is_market_day(date(2026, 9, 7))
    assert readiness._is_market_day(date(2026, 9, 8))


def test_readiness_rejects_calendar_contract_past_stale_after() -> None:
    from api import readiness

    freshness = {
        "schema_version": "dawnstrike.calendar_freshness.v1",
        "status": "current",
        "generated_at": "2026-08-18T22:30:00+00:00",
        "timezone": "America/Chicago",
        "authoritative_as_of_market_date": "2026-08-18",
        "latest_expected_market_date": "2026-08-18",
        "next_publication_market_date": "2026-08-19",
        "next_publication_at": "2026-08-19T22:30:00+00:00",
        "next_stale_after": "2026-08-19T23:30:00+00:00",
        "fail_closed": True,
    }
    payload = json.dumps({"freshness": freshness}).encode("utf-8")
    failures = readiness._calendar_contract_failures(
        payload,
        {"freshness": freshness},
        {"calendar_freshness": freshness},
    )
    assert "calendar_freshness_stale_by_clock" in failures
