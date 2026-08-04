"""Verify the bounded, static Vercel artifact before it can be promoted."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
from pathlib import Path

MAX_SNAPSHOT_BYTES = 250 * 1024
REQUIRED_FILES = (
    "index.html",
    "favicon.svg",
    "readiness.json",
    "stage-manifest.json",
    "build-manifest.json",
    "assets/dawnstrike.css",
    "assets/dawnstrike.js",
    "data/performance.json",
    "data/performance.json.manifest.json",
    "data/calendar.json",
    "data/calendar.json.manifest.json",
    "data/publication-set.json",
    "data/v6-learning.json",
    "data/scenarios.json",
    "data/scenarios.json.manifest.json",
    "release-manifest.json",
)
FORBIDDEN_FILE_PARTS = (".sqlite", ".db", "telegram", "scanner", "ui.py")
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:(?:\\|\\\\)(?:Users|r)(?:\\|\\\\)|/(?:Users|home|var|opt)/)",
    flags=re.IGNORECASE,
)


def verify(root: Path, *, allow_degraded: bool = False) -> dict[str, object]:
    errors: list[str] = []
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    errors.extend(f"missing:{name}" for name in missing)

    forbidden = []
    if root.exists():
        for path in root.rglob("*"):
            if path.is_file() and any(part in path.name.lower() for part in FORBIDDEN_FILE_PARTS):
                forbidden.append(str(path.relative_to(root)).replace("\\", "/"))
    errors.extend(f"forbidden_file:{name}" for name in forbidden)
    exposed_paths = []
    if root.exists():
        for path in root.rglob("*"):
            if not path.is_file():
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                continue
            if ABSOLUTE_PATH_PATTERN.search(text):
                exposed_paths.append(str(path.relative_to(root)).replace("\\", "/"))
    errors.extend(f"forbidden_absolute_path:{name}" for name in exposed_paths)

    snapshot_path = root / "data" / "performance.json"
    manifest_path = root / "data" / "performance.json.manifest.json"
    build_manifest_path = root / "build-manifest.json"
    calendar_path = root / "data" / "calendar.json"
    calendar_manifest_path = root / "data" / "calendar.json.manifest.json"
    publication_set_path = root / "data" / "publication-set.json"
    scenarios_path = root / "data" / "scenarios.json"
    scenarios_manifest_path = root / "data" / "scenarios.json.manifest.json"
    snapshot: dict[str, object] = {}
    manifest: dict[str, object] = {}
    build_manifest: dict[str, object] = {}
    calendar_manifest: dict[str, object] = {}
    publication_set: dict[str, object] = {}
    scenarios_manifest: dict[str, object] = {}
    snapshot_row_count = 0
    compressed_byte_count: int | None = None
    if snapshot_path.is_file():
        encoded = snapshot_path.read_bytes()
        compressed_byte_count = len(gzip.compress(encoded, compresslevel=9, mtime=0))
        if compressed_byte_count > MAX_SNAPSHOT_BYTES:
            errors.append(f"snapshot_compressed_too_large:{compressed_byte_count}")
        snapshot = json.loads(encoded)
        rows = snapshot.get("rows")
        if isinstance(rows, list):
            snapshot_row_count = len(rows)
        if snapshot_row_count > 250:
            errors.append("row_limit_exceeded")
    if manifest_path.is_file():
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        snapshot_status = manifest.get("status")
        if snapshot_status not in {"complete", "no_trade"} and not (
            allow_degraded and snapshot_status == "degraded"
        ):
            errors.append("snapshot_not_publishable")
        if snapshot_path.is_file():
            if (
                manifest.get("payload_sha256")
                != hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            ):
                errors.append("snapshot_hash_mismatch")
            if manifest.get("byte_count") != snapshot_path.stat().st_size:
                errors.append("snapshot_byte_count_mismatch")
            if manifest.get("compressed_byte_count") != compressed_byte_count:
                errors.append("snapshot_compressed_byte_count_mismatch")
        if manifest.get("compression") != "gzip":
            errors.append("snapshot_compression_missing")
    if calendar_manifest_path.is_file():
        calendar_manifest = json.loads(calendar_manifest_path.read_text(encoding="utf-8"))
        if calendar_path.is_file() and (
            calendar_manifest.get("payload_sha256")
            != hashlib.sha256(calendar_path.read_bytes()).hexdigest()
        ):
            errors.append("calendar_hash_mismatch")
        if calendar_manifest.get("canonical_input_hash_sha256") != manifest.get(
            "input_hash_sha256"
        ):
            errors.append("calendar_canonical_hash_mismatch")
        if calendar_manifest.get("performance_payload_sha256") != manifest.get("payload_sha256"):
            errors.append("calendar_performance_hash_mismatch")
    if publication_set_path.is_file():
        publication_set = json.loads(publication_set_path.read_text(encoding="utf-8"))
        if publication_set.get("performance_payload_sha256") != manifest.get("payload_sha256"):
            errors.append("publication_set_performance_hash_mismatch")
        if publication_set.get("calendar_payload_sha256") != calendar_manifest.get(
            "payload_sha256"
        ):
            errors.append("publication_set_calendar_hash_mismatch")
    if scenarios_manifest_path.is_file():
        scenarios_manifest = json.loads(scenarios_manifest_path.read_text(encoding="utf-8"))
        if scenarios_path.is_file() and (
            scenarios_manifest.get("payload_sha256")
            != hashlib.sha256(scenarios_path.read_bytes()).hexdigest()
        ):
            errors.append("scenario_hash_mismatch")
        if scenarios_manifest.get("calibration_status") != "UNCALIBRATED":
            errors.append("scenario_calibration_disclosure_missing")
    if build_manifest_path.is_file():
        build_manifest = json.loads(build_manifest_path.read_text(encoding="utf-8"))
        if not build_manifest.get("source_sha"):
            errors.append("source_sha_missing")
        if build_manifest.get("source_clean") is not True:
            errors.append("source_not_clean")
        if not build_manifest.get("build_id"):
            errors.append("build_id_missing")
        if build_manifest.get("data_hash_sha256") != manifest.get("payload_sha256"):
            errors.append("build_data_hash_mismatch")
        if build_manifest.get("publication_set_sha256") != publication_set.get(
            "publication_set_sha256"
        ):
            errors.append("build_publication_set_hash_mismatch")
        recorded_hashes = build_manifest.get("file_hashes")
        if not isinstance(recorded_hashes, dict):
            errors.append("file_hashes_missing")
        else:
            for name, expected_hash in recorded_hashes.items():
                path = root / str(name)
                if not path.is_file():
                    errors.append(f"file_hash_path_missing:{name}")
                elif hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    errors.append(f"file_hash_mismatch:{name}")

    readiness_path = root / "readiness.json"
    readiness: dict[str, object] = {}
    if readiness_path.is_file():
        readiness = json.loads(readiness_path.read_text(encoding="utf-8"))
        if readiness.get("live_trading_enabled") is True:
            errors.append("live_trading_enabled")
        readiness_is_ready = (
            readiness.get("status") == "ready" and readiness.get("http_status") == 200
        )
        readiness_is_approved_degraded = (
            allow_degraded
            and manifest.get("status") == "degraded"
            and readiness.get("status") == "not_ready"
            and readiness.get("http_status") == 503
        )
        if not readiness_is_ready and not readiness_is_approved_degraded:
            errors.append("readiness_not_publishable")

    result = {
        "status": "PASS" if not errors else "FAIL",
        "root": str(root),
        "errors": errors,
        "snapshot_bytes": snapshot_path.stat().st_size if snapshot_path.is_file() else None,
        "snapshot_compressed_bytes": (
            len(gzip.compress(snapshot_path.read_bytes(), compresslevel=9, mtime=0))
            if snapshot_path.is_file()
            else None
        ),
        "snapshot_rows": snapshot_row_count,
        "snapshot_status": manifest.get("status"),
        "readiness_status": readiness.get("status"),
        "readiness_http_status": readiness.get("http_status"),
        "source_sha": build_manifest.get("source_sha"),
        "build_id": build_manifest.get("build_id"),
        "data_hash_sha256": build_manifest.get("data_hash_sha256"),
        "publication_policy": (
            "complete_or_no_trade_or_approved_degraded"
            if allow_degraded
            else "complete_or_no_trade"
        ),
    }
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="build/public")
    parser.add_argument("--allow-degraded", action="store_true")
    args = parser.parse_args(argv)
    result = verify(Path(args.root).resolve(), allow_degraded=args.allow_degraded)
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
