import hashlib
import json
from datetime import date
from pathlib import Path


def test_minimal_health_and_readiness_modules_import_without_scanner_runtime() -> None:
    import api.health as health
    import api.readiness as readiness

    assert health.handler is not None
    assert readiness.handler is not None


def test_readiness_accepts_complete_hash_consistent_public_state(
    tmp_path: Path, monkeypatch
) -> None:
    from api import readiness

    public_root = tmp_path / "public"
    data_root = public_root / "data"
    data_root.mkdir(parents=True)
    payload = b'{"rows":[]}'
    snapshot = data_root / "performance.json"
    snapshot.write_bytes(payload)
    snapshot_hash = hashlib.sha256(payload).hexdigest()
    (data_root / "performance.json.manifest.json").write_text(
        json.dumps(
            {"payload_sha256": snapshot_hash, "byte_count": len(payload), "status": "no_trade"}
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
    readiness_payload = {
        "status": "ready",
        "http_status": 200,
        "snapshot_status": "no_trade",
        "market_date": "2026-07-29",
        "live_trading_enabled": False,
        "research_only": True,
    }
    monkeypatch.setattr(readiness, "PUBLIC_ROOT", public_root)
    monkeypatch.setattr(readiness, "SNAPSHOT_PATH", snapshot)
    monkeypatch.setattr(
        readiness, "SNAPSHOT_MANIFEST_PATH", data_root / "performance.json.manifest.json"
    )
    monkeypatch.setattr(readiness, "BUILD_MANIFEST_PATH", build_manifest)
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
        }
    )
    assert "snapshot_not_publishable" in failures
    assert "pipeline_not_ready" in failures


def test_readiness_market_calendar_skips_exchange_holidays() -> None:
    from api import readiness

    assert not readiness._is_market_day(date(2026, 9, 7))
    assert readiness._is_market_day(date(2026, 9, 8))
