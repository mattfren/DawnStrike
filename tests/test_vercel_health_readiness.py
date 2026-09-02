import hashlib
import json
from datetime import date
from pathlib import Path

from test_daily_publish_gate import _write_publishable_fixture

from scripts.verify_public_artifact import _build_sha as verifier_build_sha
from scripts.verify_public_artifact import _publication_set_sha256 as verifier_publication_set_sha
from scripts.verify_public_artifact import verify as verify_artifact


def _bind_public_paths(monkeypatch, readiness, public_root: Path) -> None:
    """Point every packaged-artifact boundary at one immutable test root."""

    monkeypatch.setattr(readiness, "PUBLIC_ROOT", public_root)
    monkeypatch.setattr(readiness, "READINESS_PATH", public_root / "readiness.json")
    for attribute, relative in {
        "SNAPSHOT_PATH": "data/performance.json",
        "SNAPSHOT_MANIFEST_PATH": "data/performance.json.manifest.json",
        "CALENDAR_PATH": "data/calendar.json",
        "CALENDAR_MANIFEST_PATH": "data/calendar.json.manifest.json",
        "SCENARIO_PATH": "data/scenarios.json",
        "SCENARIO_MANIFEST_PATH": "data/scenarios.json.manifest.json",
        "OPPORTUNITY_PATH": "data/opportunity-projection.json",
        "OPPORTUNITY_MANIFEST_PATH": "data/opportunity-projection.json.manifest.json",
        "PUBLICATION_SET_PATH": "data/publication-set.json",
        "V6_LEARNING_PATH": "data/v6-learning.json",
        "BUILD_MANIFEST_PATH": "build-manifest.json",
        "RELEASE_MANIFEST_PATH": "release-manifest.json",
    }.items():
        monkeypatch.setattr(readiness, attribute, public_root / relative)
    readiness._IMMUTABLE_BYTES_CACHE.clear()


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

    public_root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    _bind_public_paths(monkeypatch, readiness, public_root)
    monkeypatch.setattr(readiness, "_freshness_failures", lambda _value: [])
    # The shared artifact fixture intentionally omits clock-bearing calendar
    # freshness. Dedicated tests below exercise that contract; this test owns
    # the complete cross-file hash and account-session joins.
    monkeypatch.setattr(
        readiness, "_calendar_contract_failures", lambda *_args, **_kwargs: []
    )
    readiness_payload = json.loads((public_root / "readiness.json").read_text("utf-8"))
    assert readiness._validate_public_state(readiness_payload) == []


def test_artifact_verifier_rejects_cross_day_performance_manifest(
    tmp_path: Path,
) -> None:
    public_root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    manifest_path = public_root / "data" / "performance.json.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["market_date"] = "2026-08-27"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    result = verify_artifact(public_root)

    assert result["status"] == "FAIL"
    assert "performance_manifest_market_date_mismatch" in result["errors"]


def test_cross_day_performance_date_cannot_pass_after_metadata_rehash(
    tmp_path: Path,
) -> None:
    public_root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    old_date = "2026-08-27"
    current_date = "2026-08-28"
    performance_manifest_path = (
        public_root / "data" / "performance.json.manifest.json"
    )
    performance_manifest = json.loads(performance_manifest_path.read_text("utf-8"))
    performance_manifest["market_date"] = old_date
    performance_manifest_path.write_text(
        json.dumps(performance_manifest), encoding="utf-8"
    )

    publication_path = public_root / "data" / "publication-set.json"
    publication = json.loads(publication_path.read_text("utf-8"))
    calendar_manifest = json.loads(
        (public_root / "data" / "calendar.json.manifest.json").read_text("utf-8")
    )
    scenario_manifest = json.loads(
        (public_root / "data" / "scenarios.json.manifest.json").read_text("utf-8")
    )
    readiness_path = public_root / "readiness.json"
    readiness = json.loads(readiness_path.read_text("utf-8"))
    publication_hash = verifier_publication_set_sha(
        performance_manifest,
        calendar_manifest,
        scenario_manifest,
        readiness["account_session_report"],
    )
    publication.update(market_date=old_date, publication_set_sha256=publication_hash)
    publication_path.write_text(json.dumps(publication), encoding="utf-8")

    v6_hash = hashlib.sha256(
        (public_root / "data" / "v6-learning.json").read_bytes()
    ).hexdigest()
    build_path = public_root / "build-manifest.json"
    build = json.loads(build_path.read_text("utf-8"))
    build_sha = verifier_build_sha(
        source_sha=str(build["source_sha"]),
        publication_set_sha256=publication_hash,
        opportunity_projection_sha256=str(build["opportunity_projection_sha256"]),
        v6_learning_sha256=v6_hash,
        market_date=current_date,
    )
    build.update(
        build_sha=build_sha,
        build_id=build_sha[:20],
        publication_set_sha256=publication_hash,
    )
    readiness.update(
        build_id=build_sha[:20],
        deployed_build_sha=build_sha,
        publication_set_sha256=publication_hash,
    )
    readiness_path.write_text(json.dumps(readiness), encoding="utf-8")

    release_path = public_root / "release-manifest.json"
    release = json.loads(release_path.read_text("utf-8"))
    release["build_sha"] = build_sha
    release["artifact_hashes"] = {
        name: hashlib.sha256((public_root / name).read_bytes()).hexdigest()
        for name in release["artifact_hashes"]
    }
    unsigned_release = {
        key: value
        for key, value in release.items()
        if key != "release_manifest_sha256"
    }
    release["release_manifest_sha256"] = hashlib.sha256(
        json.dumps(unsigned_release, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    release_path.write_text(json.dumps(release), encoding="utf-8")
    build["release_manifest_sha256"] = release["release_manifest_sha256"]
    build["file_hashes"] = {
        name: hashlib.sha256((public_root / name).read_bytes()).hexdigest()
        for name in build["file_hashes"]
    }
    build_path.write_text(json.dumps(build), encoding="utf-8")

    result = verify_artifact(public_root)

    assert result["status"] == "FAIL"
    assert result["errors"] == ["performance_manifest_market_date_mismatch"]


def test_artifact_verifier_rejects_cross_day_calendar_payload(
    tmp_path: Path,
) -> None:
    public_root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    calendar_path = public_root / "data" / "calendar.json"
    calendar_path.write_text(
        json.dumps({"as_of_market_date": "2026-08-27", "days": []}),
        encoding="utf-8",
    )

    result = verify_artifact(public_root)

    assert result["status"] == "FAIL"
    assert "calendar_payload_market_date_mismatch" in result["errors"]


def test_packaged_readiness_rejects_cross_day_calendar_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    from api import readiness

    public_root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    manifest_path = public_root / "data" / "calendar.json.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["market_date"] = "2026-08-27"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _bind_public_paths(monkeypatch, readiness, public_root)
    monkeypatch.setattr(readiness, "_freshness_failures", lambda _value: [])
    monkeypatch.setattr(
        readiness, "_calendar_contract_failures", lambda *_args, **_kwargs: []
    )
    readiness_payload = json.loads((public_root / "readiness.json").read_text("utf-8"))

    failures = readiness._validate_public_state(readiness_payload)

    assert "calendar_manifest_market_date_mismatch" in failures


def test_packaged_readiness_rejects_cross_day_performance_manifest(
    tmp_path: Path, monkeypatch
) -> None:
    from api import readiness

    public_root = _write_publishable_fixture(
        tmp_path,
        snapshot_status="no_trade",
        readiness_status="ready",
        readiness_http_status=200,
    )
    manifest_path = public_root / "data" / "performance.json.manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["market_date"] = "2026-08-27"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    _bind_public_paths(monkeypatch, readiness, public_root)
    monkeypatch.setattr(readiness, "_freshness_failures", lambda _value: [])
    monkeypatch.setattr(
        readiness, "_calendar_contract_failures", lambda *_args, **_kwargs: []
    )
    readiness_payload = json.loads((public_root / "readiness.json").read_text("utf-8"))

    failures = readiness._validate_public_state(readiness_payload)

    assert "performance_manifest_market_date_mismatch" in failures


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
    _bind_public_paths(monkeypatch, readiness, public_root)
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


def test_readiness_rejects_active_opportunity_from_prior_market_date() -> None:
    from api import readiness

    payload = json.dumps(
        {
            "schema_version": "dawnstrike.opportunity_projection.v1",
            "state": "QUALIFYING",
            "message": "research",
            "source_run_id": "opportunity-run:historical",
            "as_of": "2026-08-14T15:00:00+00:00",
            "market_date": "2026-08-14",
            "rows": [],
            "row_count": 0,
            "research_only": True,
            "order_execution_enabled": False,
        },
        sort_keys=True,
    ).encode()
    manifest = {
        "schema_version": "dawnstrike.opportunity_projection_manifest.v1",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "state": "QUALIFYING",
        "row_count": 0,
        "market_date": "2026-08-14",
        "source_run_id": "opportunity-run:historical",
        "as_of": "2026-08-14T15:00:00+00:00",
    }

    failures = readiness._opportunity_failures(
        payload,
        manifest,
        expected_market_date="2026-08-28",
    )

    assert "opportunity_as_of_market_date_mismatch" in failures


def test_readiness_binds_utc_cross_day_opportunity_to_new_york_market_date() -> None:
    from api import readiness

    payload = json.dumps(
        {
            "schema_version": "dawnstrike.opportunity_projection.v1",
            "state": "QUALIFYING",
            "message": "research",
            "source_run_id": "opportunity-run:ny-cross-day",
            "as_of": "2026-08-12T00:30:00+00:00",
            "market_date": "2026-08-11",
            "rows": [],
            "row_count": 0,
            "research_only": True,
            "order_execution_enabled": False,
        },
        sort_keys=True,
    ).encode()
    manifest = {
        "schema_version": "dawnstrike.opportunity_projection_manifest.v1",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "state": "QUALIFYING",
        "row_count": 0,
        "market_date": "2026-08-11",
        "source_run_id": "opportunity-run:ny-cross-day",
        "as_of": "2026-08-12T00:30:00+00:00",
    }

    failures = readiness._opportunity_failures(
        payload,
        manifest,
        expected_market_date="2026-08-11",
    )

    assert failures == []


def test_readiness_rejects_unversioned_or_naive_active_opportunity() -> None:
    from api import readiness

    payload_value = {
        "schema_version": "hostile",
        "state": "QUALIFYING",
        "message": "research",
        "source_run_id": "opportunity-run:current",
        "as_of": "2026-08-28T15:00:00",
        "market_date": "2026-08-28",
        "rows": [],
        "row_count": 0,
        "research_only": True,
        "order_execution_enabled": False,
    }
    payload = json.dumps(payload_value, sort_keys=True).encode()
    manifest = {
        "schema_version": "hostile",
        "payload_sha256": hashlib.sha256(payload).hexdigest(),
        "byte_count": len(payload),
        "state": "QUALIFYING",
        "row_count": 0,
        "market_date": "2026-08-28",
        "source_run_id": "opportunity-run:current",
        "as_of": "2026-08-28T15:00:00",
    }

    failures = readiness._opportunity_failures(
        payload,
        manifest,
        expected_market_date="2026-08-28",
    )

    assert "opportunity_schema_version_invalid" in failures
    assert "opportunity_manifest_schema_version_invalid" in failures
    assert "opportunity_as_of_invalid" in failures


def test_shared_opportunity_row_safety_rejects_forged_live_decision() -> None:
    from api.readiness import validate_opportunity_projection_rows

    row = {
        "rank": 1,
        "symbol": "AAPL",
        "strategy_id": "alphaops_v5",
        "strategy_version": "dawnstrike-alphaops-v5.0.0",
        "direction": "long",
        "decision": "LIVE_ORDER",
        "lifecycle": "production_eligible",
        "evidence_kind": "research",
        "validation_wording": "Research only.",
        "market_regime": "unknown",
        "market_regime_evidence_kind": "not_available",
        "security_regime": "unknown",
        "security_regime_evidence_kind": "not_available",
        "triggered_anomalies": [],
        "liquidity_score": None,
        "liquidity_evidence_kind": None,
        "why": [],
        "risks": [],
        "vetoes": [],
        "entry_price": None,
        "invalidation_price": None,
        "target_price": None,
        "limitations": [],
        "research_only": True,
        "order_execution_enabled": False,
    }

    failures = validate_opportunity_projection_rows([row])

    assert "opportunity_row_0_decision_invalid" in failures

    row["decision"] = "watch"
    failures = validate_opportunity_projection_rows([row])
    assert "opportunity_row_0_decision_invalid" not in failures

    row["decision"] = "WATCH"
    failures = validate_opportunity_projection_rows([row])
    assert "opportunity_row_0_decision_invalid" in failures
