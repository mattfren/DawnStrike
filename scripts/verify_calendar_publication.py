"""Read-only operational verification for the public Calendar artifact.

This verifier deliberately does not import the runtime, open SQLite, invoke a
scheduled task, or write a receipt.  It compares the generated public files
with their manifests and, when requested, the public Vercel JSON endpoints.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from datetime import date, datetime
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse, urlunparse
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

CURRENT = "CURRENT"
NOT_DUE = "NOT_DUE"
STALE = "STALE"
HASH_MISMATCH = "HASH_MISMATCH"
DEPLOYMENT_SHA_MISMATCH = "DEPLOYMENT_SHA_MISMATCH"
UNAVAILABLE = "UNAVAILABLE"

CENTRAL = ZoneInfo("America/Chicago")
LOCAL_FILES = {
    "calendar": "data/calendar.json",
    "calendar_manifest": "data/calendar.json.manifest.json",
    "performance_manifest": "data/performance.json.manifest.json",
    "publication_set": "data/publication-set.json",
    "build_manifest": "build-manifest.json",
    "readiness": "readiness.json",
}
REMOTE_PATHS = {
    "health": "/api/health",
    "readiness": "/api/readiness",
    "calendar": "/data/calendar.json",
    "calendar_manifest": "/data/calendar.json.manifest.json",
    "performance_manifest": "/data/performance.json.manifest.json",
    "publication_set": "/data/publication-set.json",
    "build_manifest": "/build-manifest.json",
}


def verify(
    root: Path,
    *,
    expected_source_sha: str = "",
    expected_market_date: str | None = None,
    now: datetime | None = None,
    due_at: datetime | None = None,
    deployment_url: str | None = None,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    """Return a bounded status report without changing local or remote state."""

    local = _verify_local(root)
    errors = list(local["errors"])
    artifact_date = local.get("market_date")
    observed_now = _as_central(now or datetime.now(CENTRAL))
    expected_date = expected_market_date or artifact_date
    if expected_date and artifact_date:
        if artifact_date < expected_date:
            errors.append("calendar_market_date_stale")
        elif artifact_date > expected_date:
            errors.append("calendar_market_date_not_due")

    if due_at is not None and observed_now < _as_central(due_at):
        errors.append("publication_not_due")

    remote: dict[str, Any] = {"status": "NOT_CHECKED"}
    if deployment_url:
        remote = _verify_remote(
            deployment_url,
            local,
            timeout_seconds=max(0.1, timeout_seconds),
        )
        errors.extend(remote["errors"])

    if expected_source_sha and local.get("source_sha") != expected_source_sha:
        errors.append("deployment_source_sha_local_mismatch")

    hash_error = bool(local["errors"]) or any(
        error.endswith("hash_mismatch") or error.startswith("hash_") for error in errors
    )
    deployment_error = any(error.startswith("deployment_source_sha_") for error in errors)
    unavailable_error = any(error.startswith("remote_") for error in errors)
    timing_not_due = any(
        error == "publication_not_due" or error == "calendar_market_date_not_due"
        for error in errors
    )
    timing_stale = "calendar_market_date_stale" in errors
    return {
        "status": (
            HASH_MISMATCH
            if hash_error
            else DEPLOYMENT_SHA_MISMATCH
            if deployment_error
            else UNAVAILABLE
            if unavailable_error
            else NOT_DUE
            if timing_not_due
            else STALE
            if timing_stale
            else CURRENT
        ),
        "root": str(root.resolve()),
        "market_date": artifact_date,
        "expected_market_date": expected_date,
        "source_sha": local.get("source_sha"),
        "build_id": local.get("build_id"),
        "calendar_sha256": local.get("calendar_sha256"),
        "performance_sha256": local.get("performance_sha256"),
        "publication_set_sha256": local.get("publication_set_sha256"),
        "errors": sorted(set(errors)),
        "remote": remote,
    }


def _verify_local(root: Path) -> dict[str, Any]:
    errors: list[str] = []
    payloads: dict[str, dict[str, Any]] = {}
    raw: dict[str, bytes] = {}
    for name, relative in LOCAL_FILES.items():
        path = root / relative
        if not path.is_file():
            errors.append(f"missing:{relative}")
            continue
        try:
            content = path.read_bytes()
            raw[name] = content
            value = json.loads(content)
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            errors.append(f"unreadable:{relative}")
            continue
        if not isinstance(value, dict):
            errors.append(f"invalid_object:{relative}")
            continue
        payloads[name] = value

    calendar = payloads.get("calendar", {})
    calendar_manifest = payloads.get("calendar_manifest", {})
    performance_manifest = payloads.get("performance_manifest", {})
    publication_set = payloads.get("publication_set", {})
    build_manifest = payloads.get("build_manifest", {})
    readiness = payloads.get("readiness", {})
    calendar_sha = _sha(raw.get("calendar", b"")) if "calendar" in raw else None
    performance_sha = str(performance_manifest.get("payload_sha256") or "") or None
    publication_set_sha = str(publication_set.get("publication_set_sha256") or "") or None

    if calendar_sha and calendar_manifest.get("payload_sha256") != calendar_sha:
        errors.append("calendar_hash_mismatch")
    calendar_byte_count = calendar_manifest.get("byte_count")
    if calendar_byte_count is not None and calendar_byte_count != len(raw.get("calendar", b"")):
        errors.append("calendar_byte_count_mismatch")
    if calendar_manifest.get("canonical_input_hash_sha256") != performance_manifest.get(
        "input_hash_sha256"
    ):
        errors.append("calendar_canonical_hash_mismatch")
    if calendar_manifest.get("performance_payload_sha256") != performance_sha:
        errors.append("calendar_performance_hash_mismatch")
    if publication_set.get("calendar_payload_sha256") != calendar_manifest.get("payload_sha256"):
        errors.append("publication_set_calendar_hash_mismatch")
    if publication_set.get("performance_payload_sha256") != performance_sha:
        errors.append("publication_set_performance_hash_mismatch")
    if build_manifest.get("data_hash_sha256") != performance_sha:
        errors.append("build_data_hash_mismatch")
    if build_manifest.get("publication_set_sha256") != publication_set_sha:
        errors.append("build_publication_set_hash_mismatch")
    if not build_manifest.get("source_sha"):
        errors.append("source_sha_missing")
    if not build_manifest.get("build_id"):
        errors.append("build_id_missing")
    if readiness.get("source_sha") not in {None, build_manifest.get("source_sha")}:
        errors.append("readiness_source_sha_mismatch")
    if readiness.get("build_id") not in {None, build_manifest.get("build_id")}:
        errors.append("readiness_build_id_mismatch")
    if readiness.get("data_hash_sha256") not in {None, performance_sha}:
        errors.append("readiness_data_hash_mismatch")

    file_hashes = build_manifest.get("file_hashes")
    if isinstance(file_hashes, dict):
        for relative, expected in file_hashes.items():
            path = root / str(relative)
            if not path.is_file():
                errors.append(f"file_hash_path_missing:{relative}")
            elif _sha(path.read_bytes()) != str(expected):
                errors.append(f"file_hash_mismatch:{relative}")
    elif build_manifest:
        errors.append("file_hashes_missing")

    market_date = _first_date(
        build_manifest.get("market_date"),
        readiness.get("market_date"),
        calendar_manifest.get("market_date"),
        calendar.get("as_of_market_date"),
    )
    return {
        "errors": errors,
        "market_date": market_date,
        "source_sha": build_manifest.get("source_sha"),
        "build_id": build_manifest.get("build_id"),
        "calendar_sha256": calendar_sha,
        "performance_sha256": performance_sha,
        "publication_set_sha256": publication_set_sha,
    }


def _verify_remote(url: str, local: dict[str, Any], *, timeout_seconds: float) -> dict[str, Any]:
    origin = _origin(url)
    errors: list[str] = []
    responses: dict[str, dict[str, Any]] = {}
    for name, path in REMOTE_PATHS.items():
        response = _fetch_json(origin + path, timeout_seconds=timeout_seconds)
        if response["error"]:
            errors.append(f"remote_unavailable:{name}")
            responses[name] = {"status": response["status"]}
            continue
        responses[name] = {"status": response["status"]}
        if response["status"] != 200:
            errors.append(f"remote_http_status:{name}")
        value = response["value"]
        if name == "calendar":
            expected = local.get("calendar_sha256")
            if response["sha256"] != expected:
                errors.append("remote_calendar_hash_mismatch")
        elif name == "calendar_manifest":
            if value.get("payload_sha256") != local.get("calendar_sha256"):
                errors.append("remote_calendar_manifest_hash_mismatch")
        elif name == "performance_manifest":
            if value.get("payload_sha256") != local.get("performance_sha256"):
                errors.append("remote_performance_hash_mismatch")
        elif name == "publication_set":
            if value.get("publication_set_sha256") != local.get("publication_set_sha256"):
                errors.append("remote_publication_set_hash_mismatch")
        elif name == "build_manifest":
            if value.get("source_sha") != local.get("source_sha"):
                errors.append("remote_build_source_sha_mismatch")
            if value.get("build_id") != local.get("build_id"):
                errors.append("remote_build_id_mismatch")
        elif name == "health":
            if value.get("status") != "alive":
                errors.append("remote_health_not_alive")
            if value.get("source_sha") != local.get("source_sha"):
                errors.append("deployment_source_sha_health_mismatch")
        elif name == "readiness":
            if value.get("status") != "ready" or value.get("http_status") != 200:
                errors.append("remote_readiness_not_ready")
            if value.get("source_sha") != local.get("source_sha"):
                errors.append("deployment_source_sha_readiness_mismatch")
            if value.get("build_id") != local.get("build_id"):
                errors.append("remote_readiness_build_id_mismatch")
            if value.get("data_hash_sha256") != local.get("performance_sha256"):
                errors.append("remote_readiness_data_hash_mismatch")
    return {
        "status": "PASS" if not errors else "FAIL",
        "origin": origin,
        "errors": sorted(set(errors)),
        "responses": responses,
    }


def _fetch_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    separator = "&" if "?" in url else "?"
    request = Request(
        f"{url}{separator}verify=calendar-ops",
        headers={"Accept": "application/json", "Cache-Control": "no-cache"},
    )
    try:
        with urlopen(request, timeout=timeout_seconds) as response:
            encoded = response.read()
            value = json.loads(encoded)
            return {
                "status": int(response.status),
                "value": value if isinstance(value, dict) else {},
                "sha256": _sha(encoded),
                "error": False,
            }
    except (HTTPError, URLError, TimeoutError, OSError, json.JSONDecodeError):
        return {"status": None, "value": {}, "sha256": None, "error": True}


def _first_date(*values: object) -> str | None:
    for value in values:
        try:
            return date.fromisoformat(str(value)).isoformat()
        except (TypeError, ValueError):
            continue
    return None


def _as_central(value: datetime) -> datetime:
    return value.replace(tzinfo=CENTRAL) if value.tzinfo is None else value.astimezone(CENTRAL)


def _origin(url: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("deployment URL must be an HTTP(S) origin")
    return urlunparse((parsed.scheme, parsed.netloc, "", "", "", "")).rstrip("/")


def _sha(encoded: bytes) -> str:
    return hashlib.sha256(encoded).hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", default="build/public", type=Path)
    parser.add_argument("--expected-source-sha", default="")
    parser.add_argument("--expected-market-date")
    parser.add_argument("--due-at", type=_parse_datetime)
    parser.add_argument("--now", type=_parse_datetime)
    parser.add_argument("--deployment-url")
    parser.add_argument("--timeout-seconds", type=float, default=10.0)
    args = parser.parse_args(argv)
    result = verify(
        args.root.resolve(),
        expected_source_sha=args.expected_source_sha.strip(),
        expected_market_date=args.expected_market_date,
        due_at=args.due_at,
        now=args.now,
        deployment_url=args.deployment_url,
        timeout_seconds=args.timeout_seconds,
    )
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["status"] == CURRENT else 2


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    return _as_central(parsed)


if __name__ == "__main__":
    raise SystemExit(main())
