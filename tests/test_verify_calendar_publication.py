from __future__ import annotations

import hashlib
import json
from datetime import datetime
from pathlib import Path

from scripts import verify_calendar_publication as verifier


def _write_artifact(
    root: Path, *, market_date: str = "2026-08-20", source_sha: str = "a" * 40
) -> None:
    data = root / "data"
    data.mkdir(parents=True)
    calendar_bytes = json.dumps(
        {"schema_version": "dawnstrike.public_calendar.v1", "as_of_market_date": market_date},
        sort_keys=True,
    ).encode()
    calendar_sha = hashlib.sha256(calendar_bytes).hexdigest()
    performance_sha = "b" * 64
    canonical_sha = "c" * 64
    publication_sha = "d" * 64
    (data / "calendar.json").write_bytes(calendar_bytes)
    (data / "calendar.json.manifest.json").write_text(
        json.dumps(
            {
                "market_date": market_date,
                "payload_sha256": calendar_sha,
                "byte_count": len(calendar_bytes),
                "canonical_input_hash_sha256": canonical_sha,
                "performance_payload_sha256": performance_sha,
            }
        ),
        encoding="utf-8",
    )
    (data / "performance.json.manifest.json").write_text(
        json.dumps({"payload_sha256": performance_sha, "input_hash_sha256": canonical_sha}),
        encoding="utf-8",
    )
    (data / "publication-set.json").write_text(
        json.dumps(
            {
                "publication_set_sha256": publication_sha,
                "calendar_payload_sha256": calendar_sha,
                "performance_payload_sha256": performance_sha,
            }
        ),
        encoding="utf-8",
    )
    build = {
        "market_date": market_date,
        "source_sha": source_sha,
        "build_id": "build-123",
        "data_hash_sha256": performance_sha,
        "publication_set_sha256": publication_sha,
        "file_hashes": {},
    }
    (root / "build-manifest.json").write_text(json.dumps(build), encoding="utf-8")
    (root / "readiness.json").write_text(
        json.dumps(
            {
                "market_date": market_date,
                "source_sha": source_sha,
                "build_id": "build-123",
                "data_hash_sha256": performance_sha,
            }
        ),
        encoding="utf-8",
    )


def test_current_calendar_artifact_is_current(tmp_path: Path) -> None:
    _write_artifact(tmp_path)

    result = verifier.verify(
        tmp_path,
        expected_source_sha="a" * 40,
        expected_market_date="2026-08-20",
        now=datetime.fromisoformat("2026-08-20T18:00:00-05:00"),
    )

    assert result["status"] == verifier.CURRENT
    assert result["errors"] == []


def test_not_due_and_stale_are_distinguished(tmp_path: Path) -> None:
    _write_artifact(tmp_path)
    not_due = verifier.verify(
        tmp_path,
        expected_market_date="2026-08-21",
        now=datetime.fromisoformat("2026-08-20T12:00:00-05:00"),
        due_at=datetime.fromisoformat("2026-08-20T17:30:00-05:00"),
    )
    assert not_due["status"] == verifier.NOT_DUE

    stale = verifier.verify(tmp_path, expected_market_date="2026-08-21")
    assert stale["status"] == verifier.STALE


def test_calendar_payload_tampering_is_hash_mismatch(tmp_path: Path) -> None:
    _write_artifact(tmp_path)
    (tmp_path / "data" / "calendar.json").write_bytes(b'{"tampered":true}')

    result = verifier.verify(tmp_path)

    assert result["status"] == verifier.HASH_MISMATCH
    assert "calendar_hash_mismatch" in result["errors"]


def test_expected_source_sha_is_deployment_sha_mismatch(tmp_path: Path) -> None:
    _write_artifact(tmp_path, source_sha="a" * 40)

    result = verifier.verify(tmp_path, expected_source_sha="e" * 40)

    assert result["status"] == verifier.DEPLOYMENT_SHA_MISMATCH


def test_remote_source_sha_mismatch_is_deployment_sha_mismatch(tmp_path: Path, monkeypatch) -> None:
    _write_artifact(tmp_path)
    result_calendar_sha = hashlib.sha256(
        (tmp_path / "data" / "calendar.json").read_bytes()
    ).hexdigest()

    def fake_fetch(url: str, *, timeout_seconds: float) -> dict[str, object]:
        path = url.split("?")[0].rsplit("/", 1)[-1]
        values: dict[str, object] = {
            "health": {"source_sha": "e" * 40},
            "readiness": {
                "status": "ready",
                "http_status": 200,
                "source_sha": "e" * 40,
                "build_id": "build-123",
                "data_hash_sha256": "b" * 64,
            },
            "calendar.json": {"as_of_market_date": "2026-08-20"},
            "calendar.json.manifest.json": {
                "payload_sha256": result_calendar_sha,
            },
            "performance.json.manifest.json": {"payload_sha256": "b" * 64},
            "publication-set.json": {"publication_set_sha256": "d" * 64},
            "build-manifest.json": {"source_sha": "e" * 40, "build_id": "build-123"},
        }
        value = values[path]
        encoded = json.dumps(value, sort_keys=True).encode()
        if path == "calendar.json":
            encoded = (tmp_path / "data" / "calendar.json").read_bytes()
        return {
            "status": 200,
            "value": value,
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "error": False,
        }

    monkeypatch.setattr(verifier, "_fetch_json", fake_fetch)
    result = verifier.verify(tmp_path, deployment_url="https://example.test")

    assert result["status"] == verifier.DEPLOYMENT_SHA_MISMATCH
    assert "deployment_source_sha_health_mismatch" in result["errors"]
