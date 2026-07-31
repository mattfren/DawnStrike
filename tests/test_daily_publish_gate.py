import gzip
import hashlib
import json
from pathlib import Path

from scripts.verify_public_artifact import verify


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
    calendar = root / "data" / "calendar.json"
    calendar.write_bytes(b'{"days":[]}')
    calendar_hash = hashlib.sha256(calendar.read_bytes()).hexdigest()
    calendar_manifest = {
        "status": snapshot_status,
        "canonical_input_hash_sha256": canonical_hash,
        "performance_payload_sha256": snapshot_hash,
        "payload_sha256": calendar_hash,
    }
    (root / "data" / "calendar.json.manifest.json").write_text(
        json.dumps(calendar_manifest),
        encoding="utf-8",
    )
    publication_set_hash = hashlib.sha256(
        f"{snapshot_hash}:{calendar_hash}".encode()
    ).hexdigest()
    (root / "data" / "publication-set.json").write_text(
        json.dumps(
            {
                "performance_payload_sha256": snapshot_hash,
                "calendar_payload_sha256": calendar_hash,
                "publication_set_sha256": publication_set_hash,
            }
        ),
        encoding="utf-8",
    )
    (root / "release-manifest.json").write_text("{}", encoding="utf-8")
    required.update(
        {
            "data/calendar.json": calendar.read_bytes(),
            "data/calendar.json.manifest.json": (
                root / "data" / "calendar.json.manifest.json"
            ).read_bytes(),
            "data/publication-set.json": (
                root / "data" / "publication-set.json"
            ).read_bytes(),
            "release-manifest.json": (root / "release-manifest.json").read_bytes(),
        }
    )
    file_hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in required
    }
    (root / "build-manifest.json").write_text(
        json.dumps(
            {
                "source_sha": "clean-sha",
                "source_clean": True,
                "build_id": "fixture-build",
                "data_hash_sha256": snapshot_hash,
                "publication_set_sha256": publication_set_hash,
                "file_hashes": file_hashes,
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
