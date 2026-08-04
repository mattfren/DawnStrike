"""Readiness endpoint backed by the generated static stage manifest."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from zoneinfo import ZoneInfo

from api.public_state import PUBLIC_STATE

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _resolve_public_root() -> Path:
    candidates = (
        REPOSITORY_ROOT / "public",
        REPOSITORY_ROOT / "api" / "public",
        REPOSITORY_ROOT / "build" / "public",
    )
    return next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])


PUBLIC_ROOT = _resolve_public_root()
READINESS_PATH = PUBLIC_ROOT / "readiness.json"


def _artifact_path(primary: Path, packaged: Path) -> Path:
    return primary if primary.is_file() else packaged


SNAPSHOT_PATH = _artifact_path(
    PUBLIC_ROOT / "data" / "performance.json",
    PUBLIC_ROOT / "data" / "performance-snapshot.json",
)
SNAPSHOT_MANIFEST_PATH = _artifact_path(
    PUBLIC_ROOT / "data" / "performance.json.manifest.json",
    PUBLIC_ROOT / "data" / "performance-snapshot-manifest.json",
)
CALENDAR_PATH = PUBLIC_ROOT / "data" / "calendar.json"
CALENDAR_MANIFEST_PATH = PUBLIC_ROOT / "data" / "calendar.json.manifest.json"
SCENARIO_PATH = _artifact_path(
    PUBLIC_ROOT / "data" / "scenarios.json",
    PUBLIC_ROOT / "data" / "scenarios.json",
)
SCENARIO_MANIFEST_PATH = _artifact_path(
    PUBLIC_ROOT / "data" / "scenarios.json.manifest.json",
    PUBLIC_ROOT / "data" / "scenarios.json.manifest.json",
)
PUBLICATION_SET_PATH = PUBLIC_ROOT / "data" / "publication-set.json"
BUILD_MANIFEST_PATH = PUBLIC_ROOT / "build-manifest.json"
REQUIRED_HASHED_FILES = {
    "index.html",
    "favicon.svg",
    "readiness.json",
    "stage-manifest.json",
    "assets/dawnstrike.css",
    "assets/dawnstrike.js",
    "data/performance.json",
    "data/performance.json.manifest.json",
    "data/calendar.json",
    "data/calendar.json.manifest.json",
    "data/scenarios.json",
    "data/scenarios.json.manifest.json",
    "data/publication-set.json",
    "release-manifest.json",
}


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        packaged_readiness = (
            PUBLIC_STATE.get("readiness")
            if isinstance(PUBLIC_STATE.get("readiness"), dict)
            else {}
        )
        if not READINESS_PATH.is_file() and not packaged_readiness:
            _send(
                self, {"status": "not_ready", "http_status": 503, "reason": "snapshot_missing"}, 503
            )
            return
        if READINESS_PATH.is_file():
            try:
                payload = json.loads(READINESS_PATH.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                _send(
                    self,
                    {"status": "not_ready", "http_status": 503, "reason": "snapshot_unreadable"},
                    503,
                )
                return
        else:
            payload = packaged_readiness
        if not isinstance(payload, dict):
            _send(
                self,
                {"status": "not_ready", "http_status": 503, "reason": "snapshot_invalid"},
                503,
            )
            return
        checks = (
            _validate_public_state(payload)
            if READINESS_PATH.is_file()
            else _validate_packaged_public_state(payload)
        )
        status = 200 if not checks else 503
        build_manifest = _read_object(BUILD_MANIFEST_PATH)
        if not build_manifest:
            build_manifest = _object_dict(PUBLIC_STATE.get("build_manifest"))
        payload = {
            **payload,
            "status": "ready" if status == 200 else "not_ready",
            "http_status": status,
            "source_sha": build_manifest.get("source_sha"),
            "build_id": build_manifest.get("build_id"),
            "data_hash_sha256": build_manifest.get("data_hash_sha256"),
            "failed_checks": checks,
            "reason": "all_public_integrity_checks_passed"
            if status == 200
            else "public_integrity_check_failed",
        }
        _send(self, payload, status)


def _validate_packaged_public_state(readiness: dict[str, object]) -> list[str]:
    failures: list[str] = []
    snapshot_manifest = _object_dict(PUBLIC_STATE.get("snapshot_manifest"))
    build_manifest = _object_dict(PUBLIC_STATE.get("build_manifest"))
    encoded_snapshot = PUBLIC_STATE.get("snapshot_b64")
    if not isinstance(encoded_snapshot, str):
        failures.append("snapshot_missing")
        payload_bytes = b""
    else:
        try:
            payload_bytes = base64.b64decode(encoded_snapshot, validate=True)
        except (ValueError, TypeError):
            failures.append("snapshot_unreadable")
            payload_bytes = b""
    calendar_manifest = _object_dict(PUBLIC_STATE.get("calendar_manifest"))
    publication_set = _object_dict(PUBLIC_STATE.get("publication_set"))
    encoded_calendar = PUBLIC_STATE.get("calendar_b64")
    if not isinstance(encoded_calendar, str):
        failures.append("calendar_missing")
        calendar_bytes = b""
    else:
        try:
            calendar_bytes = base64.b64decode(
                encoded_calendar,
                validate=True,
            )
        except (ValueError, TypeError):
            failures.append("calendar_unreadable")
            calendar_bytes = b""
    scenario_manifest = _object_dict(PUBLIC_STATE.get("scenario_manifest"))
    encoded_scenario = PUBLIC_STATE.get("scenario_b64")
    if not isinstance(encoded_scenario, str):
        failures.append("scenario_missing")
        scenario_bytes = b""
    else:
        try:
            scenario_bytes = base64.b64decode(encoded_scenario, validate=True)
        except (ValueError, TypeError):
            failures.append("scenario_unreadable")
            scenario_bytes = b""
    if not snapshot_manifest:
        failures.append("snapshot_manifest_missing")
    else:
        if snapshot_manifest.get("payload_sha256") != hashlib.sha256(payload_bytes).hexdigest():
            failures.append("snapshot_hash_mismatch")
        if snapshot_manifest.get("byte_count") != len(payload_bytes):
            failures.append("snapshot_byte_count_mismatch")
    if not calendar_manifest:
        failures.append("calendar_manifest_missing")
    else:
        if (
            calendar_manifest.get("payload_sha256")
            != hashlib.sha256(calendar_bytes).hexdigest()
        ):
            failures.append("calendar_hash_mismatch")
        if (
            calendar_manifest.get("canonical_input_hash_sha256")
            != snapshot_manifest.get("input_hash_sha256")
        ):
            failures.append("calendar_canonical_hash_mismatch")
        if (
            calendar_manifest.get("performance_payload_sha256")
            != snapshot_manifest.get("payload_sha256")
        ):
            failures.append("calendar_performance_hash_mismatch")
    if (
        publication_set.get("performance_payload_sha256")
        != snapshot_manifest.get("payload_sha256")
    ):
        failures.append("publication_set_performance_hash_mismatch")
    if (
        publication_set.get("calendar_payload_sha256")
        != calendar_manifest.get("payload_sha256")
    ):
        failures.append("publication_set_calendar_hash_mismatch")
    if not scenario_manifest:
        failures.append("scenario_manifest_missing")
    else:
        if scenario_manifest.get("payload_sha256") != hashlib.sha256(scenario_bytes).hexdigest():
            failures.append("scenario_hash_mismatch")
        if scenario_manifest.get("calibration_status") != "UNCALIBRATED":
            failures.append("scenario_calibration_disclosure_missing")
    if (
        publication_set.get("scenario_payload_sha256")
        != scenario_manifest.get("payload_sha256")
    ):
        failures.append("publication_set_scenario_hash_mismatch")
    if not build_manifest:
        failures.append("build_manifest_missing")
    if not build_manifest.get("source_sha"):
        failures.append("source_sha_missing")
    if not build_manifest.get("build_id"):
        failures.append("build_id_missing")
    if build_manifest.get("source_clean") is not True:
        failures.append("source_not_clean")
    if build_manifest.get("data_hash_sha256") != snapshot_manifest.get("payload_sha256"):
        failures.append("build_data_hash_mismatch")
    if (
        build_manifest.get("publication_set_sha256")
        != publication_set.get("publication_set_sha256")
    ):
        failures.append("build_publication_set_hash_mismatch")
    file_hashes = build_manifest.get("file_hashes")
    if not isinstance(file_hashes, dict):
        failures.append("file_hashes_missing")
    else:
        failures.extend(
            f"file_hash_missing:{name}"
            for name in sorted(REQUIRED_HASHED_FILES - {str(key) for key in file_hashes})
        )
    if PUBLIC_STATE.get("static_file_hashes_verified") is not True:
        failures.append("static_file_attestation_missing")
    if readiness.get("live_trading_enabled") is True:
        failures.append("live_trading_enabled")
    if readiness.get("research_only") is not True:
        failures.append("research_only_flag_missing")
    if readiness.get("safety_status") != "verified":
        failures.append("safety_evidence_unverified")
    if readiness.get("snapshot_status") not in {"complete", "no_trade"}:
        failures.append("snapshot_not_publishable")
    if snapshot_manifest and snapshot_manifest.get("status") != readiness.get("snapshot_status"):
        failures.append("snapshot_manifest_status_mismatch")
    if readiness.get("status") != "ready" or readiness.get("http_status") != 200:
        failures.append("pipeline_not_ready")
    failures.extend(_freshness_failures(readiness.get("market_date")))
    return list(dict.fromkeys(failures))


def _validate_public_state(readiness: dict[str, object]) -> list[str]:
    failures: list[str] = []
    if not SNAPSHOT_PATH.is_file():
        failures.append("snapshot_missing")
    if not SNAPSHOT_MANIFEST_PATH.is_file():
        failures.append("snapshot_manifest_missing")
    if not SCENARIO_PATH.is_file():
        failures.append("scenario_missing")
    if not SCENARIO_MANIFEST_PATH.is_file():
        failures.append("scenario_manifest_missing")
    if not BUILD_MANIFEST_PATH.is_file():
        failures.append("build_manifest_missing")
    snapshot_manifest: dict[str, object] = {}
    build_manifest: dict[str, object] = {}
    calendar_manifest: dict[str, object] = {}
    scenario_manifest: dict[str, object] = {}
    publication_set: dict[str, object] = {}
    if SNAPSHOT_PATH.is_file() and SNAPSHOT_MANIFEST_PATH.is_file():
        try:
            payload_bytes = SNAPSHOT_PATH.read_bytes()
            parsed_manifest = json.loads(SNAPSHOT_MANIFEST_PATH.read_text(encoding="utf-8"))
            if not isinstance(parsed_manifest, dict):
                raise json.JSONDecodeError("manifest is not an object", "", 0)
            snapshot_manifest = parsed_manifest
            if snapshot_manifest.get("payload_sha256") != hashlib.sha256(payload_bytes).hexdigest():
                failures.append("snapshot_hash_mismatch")
            if snapshot_manifest.get("byte_count") != len(payload_bytes):
                failures.append("snapshot_byte_count_mismatch")
        except (OSError, json.JSONDecodeError):
            failures.append("snapshot_unreadable")
    if BUILD_MANIFEST_PATH.is_file():
        try:
            build_manifest = _read_object(BUILD_MANIFEST_PATH)
            if not build_manifest:
                raise json.JSONDecodeError("manifest is not an object", "", 0)
        except (OSError, json.JSONDecodeError):
            failures.append("build_manifest_unreadable")
    if CALENDAR_PATH.is_file() and CALENDAR_MANIFEST_PATH.is_file():
        try:
            calendar_bytes = CALENDAR_PATH.read_bytes()
            calendar_manifest = _read_object(CALENDAR_MANIFEST_PATH)
            if (
                calendar_manifest.get("payload_sha256")
                != hashlib.sha256(calendar_bytes).hexdigest()
            ):
                failures.append("calendar_hash_mismatch")
            if (
                calendar_manifest.get("canonical_input_hash_sha256")
                != snapshot_manifest.get("input_hash_sha256")
            ):
                failures.append("calendar_canonical_hash_mismatch")
            if (
                calendar_manifest.get("performance_payload_sha256")
                != snapshot_manifest.get("payload_sha256")
            ):
                failures.append("calendar_performance_hash_mismatch")
        except OSError:
            failures.append("calendar_unreadable")
    else:
        failures.append("calendar_missing")
    if SCENARIO_PATH.is_file() and SCENARIO_MANIFEST_PATH.is_file():
        try:
            scenario_bytes = SCENARIO_PATH.read_bytes()
            scenario_manifest = _read_object(SCENARIO_MANIFEST_PATH)
            if (
                scenario_manifest.get("payload_sha256")
                != hashlib.sha256(scenario_bytes).hexdigest()
            ):
                failures.append("scenario_hash_mismatch")
            if scenario_manifest.get("calibration_status") != "UNCALIBRATED":
                failures.append("scenario_calibration_disclosure_missing")
        except OSError:
            failures.append("scenario_unreadable")
    publication_set = _read_object(PUBLICATION_SET_PATH)
    if (
        publication_set.get("performance_payload_sha256")
        != snapshot_manifest.get("payload_sha256")
    ):
        failures.append("publication_set_performance_hash_mismatch")
    if (
        publication_set.get("calendar_payload_sha256")
        != calendar_manifest.get("payload_sha256")
    ):
        failures.append("publication_set_calendar_hash_mismatch")
    if (
        publication_set.get("scenario_payload_sha256")
        != scenario_manifest.get("payload_sha256")
    ):
        failures.append("publication_set_scenario_hash_mismatch")
    if not build_manifest.get("source_sha"):
        failures.append("source_sha_missing")
    if not build_manifest.get("build_id"):
        failures.append("build_id_missing")
    if build_manifest.get("source_clean") is not True:
        failures.append("source_not_clean")
    if build_manifest.get("data_hash_sha256") != snapshot_manifest.get("payload_sha256"):
        failures.append("build_data_hash_mismatch")
    if (
        build_manifest.get("publication_set_sha256")
        != publication_set.get("publication_set_sha256")
    ):
        failures.append("build_publication_set_hash_mismatch")
    file_hashes = build_manifest.get("file_hashes")
    if not isinstance(file_hashes, dict):
        failures.append("file_hashes_missing")
    else:
        failures.extend(
            f"file_hash_missing:{name}"
            for name in sorted(REQUIRED_HASHED_FILES - {str(key) for key in file_hashes})
        )
        for name, expected_hash in file_hashes.items():
            path = PUBLIC_ROOT / str(name)
            if not path.is_file():
                failures.append(f"file_hash_path_missing:{name}")
            elif hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                failures.append(f"file_hash_mismatch:{name}")
    if readiness.get("live_trading_enabled") is True:
        failures.append("live_trading_enabled")
    if readiness.get("research_only") is not True:
        failures.append("research_only_flag_missing")
    if readiness.get("safety_status") != "verified":
        failures.append("safety_evidence_unverified")
    if readiness.get("snapshot_status") not in {"complete", "no_trade"}:
        failures.append("snapshot_not_publishable")
    if snapshot_manifest and snapshot_manifest.get("status") != readiness.get("snapshot_status"):
        failures.append("snapshot_manifest_status_mismatch")
    if readiness.get("status") != "ready" or readiness.get("http_status") != 200:
        failures.append("pipeline_not_ready")
    failures.extend(_freshness_failures(readiness.get("market_date")))
    return list(dict.fromkeys(failures))


def _freshness_failures(value: object) -> list[str]:
    if not value:
        return ["market_date_missing"]
    try:
        market_date = date.fromisoformat(str(value))
    except ValueError:
        return ["market_date_invalid"]
    today = datetime.now(ZoneInfo("America/Chicago")).date()
    if market_date > today:
        return ["market_date_in_future"]
    market_days_since = sum(
        1
        for offset in range(1, (today - market_date).days + 1)
        if _is_market_day(market_date + timedelta(days=offset))
    )
    if market_days_since > 2:
        return ["market_date_stale"]
    return []


def _is_market_day(value: date) -> bool:
    return value.weekday() < 5 and value not in _market_holidays(value.year)


def _market_holidays(year: int) -> set[date]:
    return {
        _observed_fixed_holiday(year, 1, 1),
        _nth_weekday(year, 1, 0, 3),
        _nth_weekday(year, 2, 0, 3),
        _good_friday(year),
        _last_weekday(year, 5, 0),
        _observed_fixed_holiday(year, 6, 19),
        _observed_fixed_holiday(year, 7, 4),
        _nth_weekday(year, 9, 0, 1),
        _nth_weekday(year, 11, 3, 4),
        _observed_fixed_holiday(year, 12, 25),
    }


def _observed_fixed_holiday(year: int, month: int, day: int) -> date:
    holiday = date(year, month, day)
    if holiday.weekday() == 5:
        return holiday - timedelta(days=1)
    if holiday.weekday() == 6:
        return holiday + timedelta(days=1)
    return holiday


def _nth_weekday(year: int, month: int, weekday: int, occurrence: int) -> date:
    first = date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + timedelta(days=offset + (occurrence - 1) * 7)


def _last_weekday(year: int, month: int, weekday: int) -> date:
    current = date(year, month + 1, 1) - timedelta(days=1) if month < 12 else date(year, 12, 31)
    while current.weekday() != weekday:
        current -= timedelta(days=1)
    return current


def _good_friday(year: int) -> date:
    # Anonymous Gregorian computus; the exchange is closed on Good Friday.
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    lunar_weekday = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * lunar_weekday) // 451
    month = (h + lunar_weekday - 7 * m + 114) // 31
    day = ((h + lunar_weekday - 7 * m + 114) % 31) + 1
    return date(year, month, day) - timedelta(days=2)


def _send(handler: BaseHTTPRequestHandler, payload: dict[str, object], status: int) -> None:
    body = json.dumps(payload, sort_keys=True).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def _read_object(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}
