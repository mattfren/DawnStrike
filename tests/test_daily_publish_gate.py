import gzip
import hashlib
import json
from pathlib import Path

from scripts.verify_public_artifact import verify


def test_artifact_gate_reports_bounded_public_payload(tmp_path: Path) -> None:
    result = verify(tmp_path / "missing-public")
    assert result["status"] == "FAIL"
    assert "missing:build-manifest.json" in result["errors"]


def test_artifact_gate_accepts_clean_explicit_no_trade_fixture(tmp_path: Path) -> None:
    root = tmp_path / "public"
    (root / "data").mkdir(parents=True)
    required = {
        "index.html": b"<main>fixture</main>",
        "favicon.svg": b"<svg />",
        "readiness.json": json.dumps(
            {
                "status": "ready",
                "http_status": 200,
                "snapshot_status": "no_trade",
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
    (root / "data" / "performance.json.manifest.json").write_text(
        json.dumps(
            {
                "status": "no_trade",
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
                "file_hashes": file_hashes,
            }
        ),
        encoding="utf-8",
    )

    result = verify(root)

    assert result["status"] == "PASS"
    assert result["snapshot_status"] == "no_trade"
