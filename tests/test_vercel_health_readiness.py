import hashlib
import json
from datetime import date
from pathlib import Path

from test_daily_publish_gate import _write_publishable_fixture


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
