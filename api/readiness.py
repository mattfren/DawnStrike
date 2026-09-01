"""Readiness endpoint backed by the generated static stage manifest."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler
from pathlib import Path
from typing import cast
from zoneinfo import ZoneInfo

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]


def _resolve_public_root() -> Path:
    candidates = (
        REPOSITORY_ROOT / "api" / "public",
        REPOSITORY_ROOT / "public",
        REPOSITORY_ROOT / "build" / "public",
    )
    return next((candidate for candidate in candidates if candidate.is_dir()), candidates[0])


PUBLIC_ROOT = _resolve_public_root()
READINESS_PATH = PUBLIC_ROOT / "readiness.json"


SNAPSHOT_PATH = PUBLIC_ROOT / "data" / "performance.json"
SNAPSHOT_MANIFEST_PATH = PUBLIC_ROOT / "data" / "performance.json.manifest.json"
CALENDAR_PATH = PUBLIC_ROOT / "data" / "calendar.json"
CALENDAR_MANIFEST_PATH = PUBLIC_ROOT / "data" / "calendar.json.manifest.json"
SCENARIO_PATH = PUBLIC_ROOT / "data" / "scenarios.json"
SCENARIO_MANIFEST_PATH = PUBLIC_ROOT / "data" / "scenarios.json.manifest.json"
OPPORTUNITY_PATH = PUBLIC_ROOT / "data" / "opportunity-projection.json"
OPPORTUNITY_MANIFEST_PATH = (
    PUBLIC_ROOT / "data" / "opportunity-projection.json.manifest.json"
)
PUBLICATION_SET_PATH = PUBLIC_ROOT / "data" / "publication-set.json"
V6_LEARNING_PATH = PUBLIC_ROOT / "data" / "v6-learning.json"
BUILD_MANIFEST_PATH = PUBLIC_ROOT / "build-manifest.json"
RELEASE_MANIFEST_PATH = PUBLIC_ROOT / "release-manifest.json"
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
    "data/opportunity-projection.json",
    "data/opportunity-projection.json.manifest.json",
    "data/publication-set.json",
    "data/v6-learning.json",
    "release-manifest.json",
}
EXPECTED_STRATEGY_VERSIONS = {
    "alphaops_v5": "dawnstrike-alphaops-v5.0.0",
    "alphaops_v6_shadow": "dawnstrike-alphaops-v6-shadow",
    "paperops": "immutable-strategy-semantics-manifest",
}
OFFICIAL_ACCOUNT_SESSION_IDENTITY = {
    "account_id": "alphaops_v5_simulated",
    "version_bucket": "v5",
    "cohort": "official_forward_paper",
    "strategy_id": "alphaops_v5",
    "strategy_version": "dawnstrike-alphaops-v5.0.0",
}

_IMMUTABLE_BYTES_CACHE: dict[tuple[object, ...], bytes] = {}
_LOWER_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def is_lower_hex64(value: object) -> bool:
    return isinstance(value, str) and _LOWER_HEX64.fullmatch(value) is not None


class handler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        if not READINESS_PATH.is_file():
            _send(
                self, {"status": "not_ready", "http_status": 503, "reason": "snapshot_missing"}, 503
            )
            return
        try:
            payload = json.loads(_read_cached_bytes(READINESS_PATH))
        except (OSError, json.JSONDecodeError):
            _send(
                self,
                {"status": "not_ready", "http_status": 503, "reason": "snapshot_unreadable"},
                503,
            )
            return
        if not isinstance(payload, dict):
            _send(
                self,
                {"status": "not_ready", "http_status": 503, "reason": "snapshot_invalid"},
                503,
            )
            return
        # Packaged files are authoritative; no metadata or caller state can
        # substitute for a missing/changed payload.
        checks = _validate_public_state(payload)
        status = 200 if not checks else 503
        build_manifest = _read_object(BUILD_MANIFEST_PATH)
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
    # Kept as a compatibility entry point for callers that imported the old
    # embedded-state validator.  Packaged readiness now always reads files
    # under api/public; metadata cannot stand in for missing bytes.
    return ["embedded_public_state_unsupported"]


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
    if not OPPORTUNITY_PATH.is_file():
        failures.append("opportunity_missing")
    if not OPPORTUNITY_MANIFEST_PATH.is_file():
        failures.append("opportunity_manifest_missing")
    if not BUILD_MANIFEST_PATH.is_file():
        failures.append("build_manifest_missing")
    if not V6_LEARNING_PATH.is_file():
        failures.append("v6_learning_missing")
    snapshot_manifest: dict[str, object] = {}
    build_manifest: dict[str, object] = {}
    calendar_manifest: dict[str, object] = {}
    scenario_manifest: dict[str, object] = {}
    opportunity_manifest: dict[str, object] = {}
    publication_set: dict[str, object] = {}
    release_manifest: dict[str, object] = {}
    v6_hash = ""
    if V6_LEARNING_PATH.is_file():
        try:
            v6_bytes = _read_cached_bytes(V6_LEARNING_PATH)
            v6_hash = hashlib.sha256(v6_bytes).hexdigest()
            parsed_v6 = json.loads(v6_bytes)
            if not isinstance(parsed_v6, dict):
                failures.append("v6_learning_invalid")
            else:
                failures.extend(_v6_contract_failures(parsed_v6))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            failures.append("v6_learning_unreadable")
    if SNAPSHOT_PATH.is_file() and SNAPSHOT_MANIFEST_PATH.is_file():
        try:
            payload_bytes = _read_cached_bytes(SNAPSHOT_PATH)
            parsed_manifest = json.loads(_read_cached_bytes(SNAPSHOT_MANIFEST_PATH))
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
    if RELEASE_MANIFEST_PATH.is_file():
        release_manifest = _read_object(RELEASE_MANIFEST_PATH)
        if not release_manifest:
            failures.append("release_manifest_unreadable")
    if CALENDAR_PATH.is_file() and CALENDAR_MANIFEST_PATH.is_file():
        try:
            calendar_bytes = _read_cached_bytes(CALENDAR_PATH)
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
            failures.extend(
                _calendar_contract_failures(calendar_bytes, calendar_manifest, readiness)
            )
        except OSError:
            failures.append("calendar_unreadable")
    else:
        failures.append("calendar_missing")
    if SCENARIO_PATH.is_file() and SCENARIO_MANIFEST_PATH.is_file():
        try:
            scenario_bytes = _read_cached_bytes(SCENARIO_PATH)
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
    if OPPORTUNITY_PATH.is_file() and OPPORTUNITY_MANIFEST_PATH.is_file():
        try:
            opportunity_bytes = _read_cached_bytes(OPPORTUNITY_PATH)
            opportunity_manifest = _read_object(OPPORTUNITY_MANIFEST_PATH)
            failures.extend(
                _opportunity_failures(opportunity_bytes, opportunity_manifest)
            )
        except OSError:
            failures.append("opportunity_unreadable")
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
    if not is_lower_hex64(publication_set.get("publication_set_sha256")):
        failures.append("publication_set_sha256_invalid")
    report_value = readiness.get("account_session_report")
    report_for_publication = (
        cast(dict[str, object], report_value) if isinstance(report_value, dict) else {}
    )
    expected_publication_set_hash = _publication_set_sha256(
        snapshot_manifest,
        calendar_manifest,
        scenario_manifest if scenario_manifest else None,
        report_for_publication,
    )
    if publication_set.get("publication_set_sha256") != expected_publication_set_hash:
        failures.append("publication_set_formula_mismatch")
    for publication_field, report_field in {
        "account_session_ledger_lineage_sha256": "ledger_lineage_sha256",
        "account_session_current_session_lineage_sha256": (
            "current_session_lineage_sha256"
        ),
        "account_session_expected_current_session_lineage_sha256": (
            "expected_current_session_lineage_sha256"
        ),
        "account_session_current_session_lineage_match": "current_session_lineage_match",
    }.items():
        if publication_set.get(publication_field) != report_for_publication.get(report_field):
            failures.append(f"publication_set_{publication_field}_mismatch")
    if publication_set.get("scenario_manifest_sha256") != hashlib.sha256(
        _read_cached_bytes(SCENARIO_MANIFEST_PATH)
    ).hexdigest():
        failures.append("publication_set_scenario_manifest_hash_mismatch")
    if not is_lower_hex64(opportunity_manifest.get("payload_sha256")):
        failures.append("opportunity_projection_sha256_invalid")
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
    if (
        build_manifest.get("opportunity_projection_sha256")
        != opportunity_manifest.get("payload_sha256")
    ):
        failures.append("build_opportunity_projection_hash_mismatch")
    if not is_lower_hex64(build_manifest.get("publication_set_sha256")):
        failures.append("build_publication_set_sha256_invalid")
    if not is_lower_hex64(build_manifest.get("opportunity_projection_sha256")):
        failures.append("build_opportunity_projection_sha256_invalid")
    if build_manifest.get("v6_learning_sha256") != v6_hash:
        failures.append("build_v6_learning_hash_mismatch")
    if not is_lower_hex64(v6_hash):
        failures.append("v6_learning_sha256_invalid")
    if not is_lower_hex64(build_manifest.get("build_sha")):
        failures.append("build_sha_invalid")
    if not is_lower_hex64(build_manifest.get("v6_learning_sha256")):
        failures.append("build_v6_learning_sha256_invalid")
    expected_build_sha = _build_sha(
        source_sha=str(build_manifest.get("source_sha") or ""),
        publication_set_sha256=str(build_manifest.get("publication_set_sha256") or ""),
        opportunity_projection_sha256=str(
            build_manifest.get("opportunity_projection_sha256") or ""
        ),
        v6_learning_sha256=v6_hash,
        market_date=str(build_manifest.get("market_date") or ""),
    )
    if build_manifest.get("build_sha") != expected_build_sha:
        failures.append("build_sha_formula_mismatch")
    if build_manifest.get("build_id") != expected_build_sha[:20]:
        failures.append("build_id_formula_mismatch")
    file_hashes_value = build_manifest.get("file_hashes")
    file_hashes = (
        cast(dict[str, object], file_hashes_value)
        if isinstance(file_hashes_value, dict)
        else {}
    )
    if not isinstance(file_hashes_value, dict):
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
            elif hashlib.sha256(_read_cached_bytes(path)).hexdigest() != expected_hash:
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
    if readiness.get("v6_learning_sha256") != v6_hash:
        failures.append("readiness_v6_learning_hash_mismatch")
    if not is_lower_hex64(readiness.get("v6_learning_sha256")):
        failures.append("readiness_v6_learning_sha256_invalid")
    if readiness.get("build_id") != build_manifest.get("build_id"):
        failures.append("readiness_build_id_mismatch")
    if readiness.get("deployed_build_sha") != build_manifest.get("build_sha"):
        failures.append("readiness_build_sha_mismatch")
    if readiness.get("market_date") != build_manifest.get("market_date"):
        failures.append("readiness_market_date_mismatch")
    failures.extend(
        _account_session_report_failures(
            readiness.get("account_session_report"),
            market_date=str(build_manifest.get("market_date") or ""),
            source_sha=str(build_manifest.get("source_sha") or ""),
        )
    )
    failures.extend(
        _account_session_reconciliation_failures(
            readiness.get("account_session_reconciliation"),
            report=readiness.get("account_session_report"),
            market_date=str(build_manifest.get("market_date") or ""),
            source_sha=str(build_manifest.get("source_sha") or ""),
        )
    )
    if release_manifest.get("build_sha") != build_manifest.get("build_sha"):
        failures.append("release_build_sha_mismatch")
    if release_manifest.get("source_sha") != build_manifest.get("source_sha"):
        failures.append("release_source_sha_mismatch")
    if release_manifest.get("v6_learning_sha256") != v6_hash:
        failures.append("release_v6_learning_hash_mismatch")
    if not is_lower_hex64(release_manifest.get("v6_learning_sha256")):
        failures.append("release_v6_learning_sha256_invalid")
    if release_manifest.get("deployment_boundary") != (
        "configured_runtime_and_durable_state"
    ) or not is_lower_hex64(release_manifest.get("deployment_boundary_sha256")):
        failures.append("release_deployment_boundary_invalid")
    if release_manifest.get("strategy_versions") != EXPECTED_STRATEGY_VERSIONS:
        failures.append("release_strategy_versions_invalid")
    if release_manifest.get("scheduler_version") != "dawnstrike-scheduler-v6":
        failures.append("release_scheduler_version_invalid")
    if release_manifest.get("data_watermark") != build_manifest.get("market_date"):
        failures.append("release_data_watermark_mismatch")
    if release_manifest.get("research_only") is not True:
        failures.append("release_research_only_missing")
    if release_manifest.get("broker_execution_enabled") is not False:
        failures.append("release_broker_execution_enabled")
    unsigned_release = dict(release_manifest)
    recorded_release_hash = unsigned_release.pop("release_manifest_sha256", None)
    expected_release_hash = hashlib.sha256(
        json.dumps(unsigned_release, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if recorded_release_hash != expected_release_hash:
        failures.append("release_manifest_self_hash_mismatch")
    if build_manifest.get("release_manifest_sha256") != recorded_release_hash:
        failures.append("build_release_manifest_hash_mismatch")
    release_hashes_value = release_manifest.get("artifact_hashes")
    expected_release_files = REQUIRED_HASHED_FILES - {"release-manifest.json"}
    if (
        not isinstance(release_hashes_value, dict)
        or set(release_hashes_value) != expected_release_files
    ):
        failures.append("release_artifact_hash_inventory_mismatch")
    else:
        release_hashes = cast(dict[str, object], release_hashes_value)
        for name in sorted(expected_release_files):
            if release_hashes.get(name) != file_hashes.get(name):
                failures.append(f"release_artifact_hash_mismatch:{name}")
    failures.extend(_freshness_failures(readiness.get("market_date")))
    return list(dict.fromkeys(failures))


def _publication_set_sha256(
    performance_manifest: dict[str, object],
    calendar_manifest: dict[str, object],
    scenario_manifest: dict[str, object] | None,
    account_session_report: dict[str, object] | None,
) -> str:
    payload: dict[str, object] = {
        "market_date": performance_manifest.get("market_date"),
        "canonical_input_hash_sha256": performance_manifest.get("input_hash_sha256"),
        "performance_payload_sha256": performance_manifest.get("payload_sha256"),
        "calendar_payload_sha256": calendar_manifest.get("payload_sha256"),
        "performance_manifest_id": performance_manifest.get("manifest_id"),
        "calendar_manifest_id": calendar_manifest.get("manifest_id"),
    }
    if scenario_manifest is not None:
        payload["scenario_payload_sha256"] = scenario_manifest.get("payload_sha256")
        payload["scenario_schema_version"] = scenario_manifest.get("schema_version")
    report = account_session_report if isinstance(account_session_report, dict) else {}
    payload.update(
        {
            "account_session_status": report.get("status"),
            "account_session_input_hash_sha256": report.get("input_hash_sha256"),
            "account_session_expected_calendar_hash_sha256": report.get(
                "expected_calendar_hash_sha256"
            ),
            "account_session_code_sha": report.get("code_sha"),
            **{
                f"account_session_{field}": report.get(field)
                for field in OFFICIAL_ACCOUNT_SESSION_IDENTITY
            },
            "account_session_ledger_lineage_sha256": report.get(
                "ledger_lineage_sha256"
            ),
            "account_session_current_session_lineage_sha256": report.get(
                "current_session_lineage_sha256"
            ),
            "account_session_expected_current_session_lineage_sha256": report.get(
                "expected_current_session_lineage_sha256"
            ),
            "account_session_current_session_lineage_match": report.get(
                "current_session_lineage_match"
            ),
        }
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _account_session_report_failures(
    value: object, *, market_date: str, source_sha: str
) -> list[str]:
    if not isinstance(value, dict):
        return ["account_session_report_missing"]
    expected = value.get("expected_session_count")
    failures: list[str] = []
    if (
        value.get("schema_version") != "dawnstrike.account_session_report.v1"
        or value.get("status") != "COMPLETE"
        or value.get("market_date") != market_date
        or value.get("code_sha") != source_sha
        or value.get("unsafe_ledger_count") != 0
        or value.get("research_only") is not True
        or value.get("broker_execution_enabled") is not False
        or any(
            value.get(field) != expected
            for field, expected in OFFICIAL_ACCOUNT_SESSION_IDENTITY.items()
        )
    ):
        failures.append("account_session_report_identity_or_status_invalid")
    if (
        isinstance(expected, bool)
        or not isinstance(expected, int)
        or expected < 1
        or value.get("ledger_row_count") != expected
        or value.get("complete_count") != expected
        or value.get("missing_count") != 0
        or value.get("partial_count") != 0
        or value.get("quarantined_count") != 0
    ):
        failures.append("account_session_report_coverage_incomplete")
    series = value.get("series")
    if not isinstance(series, list) or len(series) != 1 or not isinstance(series[0], dict):
        failures.append("account_session_report_series_ambiguous")
    elif any(
        series[0].get(field) != expected
        for field, expected in OFFICIAL_ACCOUNT_SESSION_IDENTITY.items()
    ):
        failures.append("account_session_report_series_identity_invalid")
    lineage_fields = (
        "ledger_lineage_sha256",
        "current_session_lineage_sha256",
        "expected_current_session_lineage_sha256",
    )
    if (
        any(not is_lower_hex64(value.get(field)) for field in lineage_fields)
        or value.get("current_session_lineage_match") is not True
        or value.get("current_session_lineage_sha256")
        != value.get("expected_current_session_lineage_sha256")
    ):
        failures.append("account_session_report_lineage_invalid")
    if isinstance(series, list) and len(series) == 1 and isinstance(series[0], dict):
        if (
            series[0].get("current_session_lineage_match") is not True
            or series[0].get("ledger_lineage_sha256")
            != value.get("ledger_lineage_sha256")
            or series[0].get("current_session_lineage_sha256")
            != value.get("current_session_lineage_sha256")
            or series[0].get("expected_current_session_lineage_sha256")
            != value.get("expected_current_session_lineage_sha256")
        ):
            failures.append("account_session_report_series_lineage_invalid")
    return failures


def _account_session_reconciliation_failures(
    value: object,
    *,
    report: object,
    market_date: str,
    source_sha: str,
) -> list[str]:
    if not isinstance(value, dict):
        return ["account_session_reconciliation_missing"]
    report_value = report if isinstance(report, dict) else {}
    lineage = value.get("ledger_lineage_sha256")
    lineage_list_hash = hashlib.sha256(
        json.dumps([lineage], sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if (
        value.get("schema_version") != "dawnstrike.daily_account_reconciliation.v1"
        or value.get("status") != "COMPLETE"
        or value.get("market_date") != market_date
        or value.get("release_sha") != source_sha
        or value.get("account_id") != OFFICIAL_ACCOUNT_SESSION_IDENTITY["account_id"]
        or value.get("account_status") not in {"AUTHENTICATED_NO_TRADE", "TRADE"}
        or value.get("research_only") is not True
        or value.get("broker_execution_enabled") is not False
        or not is_lower_hex64(lineage)
        or lineage_list_hash != report_value.get("current_session_lineage_sha256")
    ):
        return ["account_session_reconciliation_invalid"]
    return []


def _calendar_contract_failures(
    calendar_bytes: bytes,
    manifest: dict[str, object],
    readiness: dict[str, object],
) -> list[str]:
    """Validate freshness metadata independently of Calendar rendering."""

    authoritative: date | None = None
    try:
        payload = json.loads(calendar_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return ["calendar_contract_unreadable"]
    if not isinstance(payload, dict):
        return ["calendar_contract_invalid"]
    freshness = payload.get("freshness")
    if not isinstance(freshness, dict):
        return ["calendar_freshness_missing"]
    failures: list[str] = []
    manifest_freshness = manifest.get("freshness")
    readiness_freshness = readiness.get("calendar_freshness")
    if not isinstance(manifest_freshness, dict):
        failures.append("calendar_manifest_freshness_missing")
    elif manifest_freshness != freshness:
        failures.append("calendar_freshness_manifest_mismatch")
    if not isinstance(readiness_freshness, dict):
        failures.append("readiness_calendar_freshness_missing")
    elif readiness_freshness != freshness:
        failures.append("readiness_calendar_freshness_mismatch")
    required = {
        "schema_version",
        "status",
        "generated_at",
        "timezone",
        "authoritative_as_of_market_date",
        "latest_expected_market_date",
        "next_publication_market_date",
        "next_publication_at",
        "next_stale_after",
        "fail_closed",
    }
    failures.extend(
        f"calendar_freshness_field_missing:{name}"
        for name in sorted(required - set(freshness))
    )
    if freshness.get("schema_version") != "dawnstrike.calendar_freshness.v1":
        failures.append("calendar_freshness_schema_invalid")
    if freshness.get("timezone") != "America/Chicago":
        failures.append("calendar_freshness_timezone_invalid")
    if freshness.get("fail_closed") is not True:
        failures.append("calendar_freshness_fail_closed_missing")
    if freshness.get("status") in {"stale", "future", "unknown"}:
        failures.append(f"calendar_freshness_{freshness.get('status')}")
    try:
        timestamp = datetime.fromisoformat(
            str(freshness.get("generated_at") or "").replace("Z", "+00:00")
        )
        if timestamp.tzinfo is None or timestamp.utcoffset() is None:
            failures.append("calendar_freshness_generated_at_naive")
    except ValueError:
        failures.append("calendar_freshness_generated_at_invalid")
    try:
        authoritative = date.fromisoformat(
            str(freshness.get("authoritative_as_of_market_date") or "")
        )
        latest = date.fromisoformat(
            str(freshness.get("latest_expected_market_date") or "")
        )
        if authoritative > latest:
            failures.append("calendar_freshness_authoritative_date_ahead")
    except ValueError:
        failures.append("calendar_freshness_market_date_invalid")
    try:
        next_market_date = date.fromisoformat(
            str(freshness.get("next_publication_market_date") or "")
        )
        next_stale_after = datetime.fromisoformat(
            str(freshness.get("next_stale_after") or "").replace("Z", "+00:00")
        )
        if (
            next_stale_after.tzinfo is not None
            and datetime.now(next_stale_after.tzinfo) >= next_stale_after
            and authoritative is not None
            and authoritative < next_market_date
        ):
            failures.append("calendar_freshness_stale_by_clock")
    except (TypeError, ValueError):
        # Missing optional timestamp is already covered by the required-field
        # check; malformed values remain a contract failure, not a fresh claim.
        if freshness.get("next_stale_after") not in {None, ""}:
            failures.append("calendar_freshness_next_stale_after_invalid")
    return failures


def _opportunity_failures(
    payload_bytes: bytes,
    manifest: dict[str, object],
) -> list[str]:
    failures: list[str] = []
    if not manifest:
        failures.append("opportunity_manifest_missing")
        return failures
    if manifest.get("payload_sha256") != hashlib.sha256(payload_bytes).hexdigest():
        failures.append("opportunity_hash_mismatch")
    if manifest.get("byte_count") != len(payload_bytes):
        failures.append("opportunity_byte_count_mismatch")
    try:
        parsed = json.loads(payload_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError):
        failures.append("opportunity_unreadable")
        return failures
    if not isinstance(parsed, dict):
        failures.append("opportunity_payload_invalid")
        return failures
    rows = parsed.get("rows")
    if not isinstance(rows, list):
        failures.append("opportunity_rows_invalid")
        rows = []
    if len(rows) > 5:
        failures.append("opportunity_row_limit_exceeded")
    if parsed.get("row_count") != len(rows):
        failures.append("opportunity_row_count_mismatch")
    if parsed.get("state") not in {
        "DISABLED",
        "DATA_UNAVAILABLE",
        "NO_QUALIFYING",
        "QUALIFYING",
    }:
        failures.append("opportunity_state_invalid")
    if parsed.get("research_only") is not True:
        failures.append("opportunity_research_only_missing")
    if parsed.get("order_execution_enabled") is not False:
        failures.append("opportunity_execution_boundary_invalid")
    if manifest.get("state") != parsed.get("state"):
        failures.append("opportunity_manifest_state_mismatch")
    if manifest.get("row_count") != parsed.get("row_count"):
        failures.append("opportunity_manifest_row_count_mismatch")
    return failures


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


def _read_cached_bytes(path: Path) -> bytes:
    """Read immutable packaged bytes through an in-process stat fingerprint."""

    stat = path.stat()
    build_stat = BUILD_MANIFEST_PATH.stat() if BUILD_MANIFEST_PATH.is_file() else None
    key = (
        str(PUBLIC_ROOT),
        str(path),
        stat.st_size,
        stat.st_mtime_ns,
        getattr(stat, "st_ino", 0),
        getattr(build_stat, "st_size", None),
        getattr(build_stat, "st_mtime_ns", None),
    )
    cached = _IMMUTABLE_BYTES_CACHE.get(key)
    if cached is not None:
        return cached
    value = path.read_bytes()
    _IMMUTABLE_BYTES_CACHE[key] = value
    # A changed file gets a new key; discard prior entries for this path so a
    # long-lived function process cannot retain stale artifact generations.
    for old_key in tuple(_IMMUTABLE_BYTES_CACHE):
        if old_key != key and len(old_key) > 1 and old_key[1] == str(path):
            _IMMUTABLE_BYTES_CACHE.pop(old_key, None)
    return value


def _build_sha(
    *,
    source_sha: str,
    publication_set_sha256: str,
    opportunity_projection_sha256: str,
    v6_learning_sha256: str,
    market_date: str,
) -> str:
    formula = (
        f"{source_sha}:{publication_set_sha256}:{opportunity_projection_sha256}:"
        f"{v6_learning_sha256}:{market_date}"
    )
    return hashlib.sha256(formula.encode("utf-8")).hexdigest()


_V6_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version", "strategy_version", "decision_count", "tracked_count",
        "outcome_count", "learning_eligible_outcome_count", "latest_model_run",
        "latest_evaluation", "latest_drift", "operational_freshness",
        "latest_promotion_review", "prediction_evidence_gate", "failure_attribution",
        "account_comparison", "decision_replay", "promotion_readiness",
        "missing_truth_is_zero", "research_only", "broker_execution_enabled",
    }
)


def _v6_contract_failures(payload: dict[str, object]) -> list[str]:
    failures: list[str] = []
    for name in sorted(_V6_TOP_LEVEL_KEYS - frozenset(payload)):
        failures.append(f"v6_field_missing:{name}")
    for name in sorted(frozenset(payload) - _V6_TOP_LEVEL_KEYS):
        failures.append(f"v6_field_unexpected:{name}")
    if payload.get("schema_version") != "dawnstrike.alphaops_v6.public_status.v1":
        failures.append("v6_schema_version_invalid")
    if payload.get("strategy_version") != "dawnstrike-alphaops-v6-shadow":
        failures.append("v6_strategy_version_invalid")
    for name in (
        "decision_count", "tracked_count", "outcome_count",
        "learning_eligible_outcome_count",
    ):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            failures.append(f"v6_{name}_invalid")
    if not isinstance(payload.get("operational_freshness"), dict):
        failures.append("v6_operational_freshness_invalid")
    if not isinstance(payload.get("prediction_evidence_gate"), dict):
        failures.append("v6_prediction_evidence_gate_invalid")
    if not isinstance(payload.get("failure_attribution"), dict):
        failures.append("v6_failure_attribution_invalid")
    if not isinstance(payload.get("promotion_readiness"), dict):
        failures.append("v6_promotion_readiness_invalid")
    if not isinstance(payload.get("decision_replay"), list):
        failures.append("v6_decision_replay_invalid")
    for name in (
        "latest_model_run", "latest_evaluation", "latest_drift",
        "latest_promotion_review", "account_comparison",
    ):
        if payload.get(name) is not None and not isinstance(payload.get(name), dict):
            failures.append(f"v6_{name}_invalid")
    if payload.get("missing_truth_is_zero") is not False:
        failures.append("v6_missing_truth_is_zero_invalid")
    if payload.get("research_only") is not True:
        failures.append("v6_research_only_invalid")
    if payload.get("broker_execution_enabled") is not False:
        failures.append("v6_broker_execution_invalid")
    failures.extend(_v6_safety_flag_failures(payload))
    promotion = payload.get("promotion_readiness")
    if isinstance(promotion, dict):
        if promotion.get("automatic_promotion") is not False:
            failures.append("v6_automatic_promotion_invalid")
        if promotion.get("status") not in {
            "NOT_ELIGIBLE_FOR_PROMOTION", "ELIGIBLE_FOR_MANUAL_REVIEW",
            "MANUALLY_APPROVED_FOR_CONTROLLED_PROMOTION",
        }:
            failures.append("v6_promotion_status_invalid")
        if promotion.get("performance_status") not in {
            "WAITING_FOR_FORWARD_EVIDENCE", "ELIGIBLE_FOR_MANUAL_REVIEW",
        }:
            failures.append("v6_performance_status_invalid")
    return failures


def _v6_safety_flag_failures(value: object, path: str = "v6") -> list[str]:
    failures: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key == "research_only" and item is not True:
                failures.append(f"{child}_invalid")
            if (
                key
                in {
                    "broker_execution_enabled",
                    "live_trading_enabled",
                    "order_execution_enabled",
                }
                and item is not False
            ):
                failures.append(f"{child}_invalid")
            failures.extend(_v6_safety_flag_failures(item, child))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            failures.extend(_v6_safety_flag_failures(item, f"{path}[{index}]"))
    return failures


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
        payload = json.loads(_read_cached_bytes(path))
    except (OSError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


def _object_dict(value: object) -> dict[str, object]:
    if not isinstance(value, dict):
        return {}
    return {str(key): item for key, item in value.items()}
