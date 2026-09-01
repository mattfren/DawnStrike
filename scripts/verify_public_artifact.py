"""Verify the bounded, static Vercel artifact before it can be promoted."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT_TEXT = str(_REPO_ROOT)
if _REPO_ROOT_TEXT in sys.path:
    sys.path.remove(_REPO_ROOT_TEXT)
sys.path.insert(0, _REPO_ROOT_TEXT)

from api.readiness import (  # noqa: E402
    validate_opportunity_projection_rows,
)
from scripts import public_artifact_inventory as _public_inventory  # noqa: E402
from scripts import public_lineage as _public_lineage  # noqa: E402

_EXPECTED_LINEAGE = (_REPO_ROOT / "scripts" / "public_lineage.py").resolve()
if Path(_public_lineage.__file__).resolve() != _EXPECTED_LINEAGE:
    raise RuntimeError("public artifact verifier did not load the exact candidate lineage code")
_EXPECTED_INVENTORY = (_REPO_ROOT / "scripts" / "public_artifact_inventory.py").resolve()
if Path(_public_inventory.__file__).resolve() != _EXPECTED_INVENTORY:
    raise RuntimeError("public artifact verifier did not load the exact candidate inventory code")
is_lower_hex64 = _public_lineage.is_lower_hex64

MAX_SNAPSHOT_BYTES = 250 * 1024
V6_SCHEMA_VERSION = "dawnstrike.alphaops_v6.public_status.v1"
V6_TOP_LEVEL_KEYS = frozenset(
    {
        "schema_version",
        "strategy_version",
        "decision_count",
        "tracked_count",
        "outcome_count",
        "learning_eligible_outcome_count",
        "latest_model_run",
        "latest_evaluation",
        "latest_drift",
        "operational_freshness",
        "latest_promotion_review",
        "prediction_evidence_gate",
        "failure_attribution",
        "account_comparison",
        "decision_replay",
        "promotion_readiness",
        "missing_truth_is_zero",
        "research_only",
        "broker_execution_enabled",
    }
)
REQUIRED_FILES = tuple(sorted(_public_inventory.PUBLIC_ARTIFACT_FILES))
ARTIFACT_ROOT_FILES = tuple(name for name in REQUIRED_FILES if name != "build-manifest.json")
RELEASE_MANIFEST_KEYS = frozenset(
    {
        "schema_version", "source_sha", "build_sha", "v6_learning_sha256",
        "deployment_boundary", "deployment_boundary_sha256",
        "database_schema_version", "data_watermark", "strategy_versions",
        "scheduler_version", "artifact_hashes", "created_at", "research_only",
        "broker_execution_enabled", "release_manifest_sha256",
    }
)
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
BUILD_MANIFEST_KEYS = frozenset(
    {
        "schema_version",
        "source_sha",
        "source_clean",
        "build_id",
        "build_sha",
        "data_hash_sha256",
        "publication_set_sha256",
        "opportunity_projection_sha256",
        "v6_learning_sha256",
        "release_manifest_sha256",
        "market_date",
        "generated_at",
        "status",
        "readiness",
        "file_hashes",
        "research_only",
        "live_trading_enabled",
        "broker_execution_enabled",
    }
)
FORBIDDEN_FILE_PARTS = (".sqlite", ".db", "telegram", "scanner", "ui.py")
ABSOLUTE_PATH_PATTERN = re.compile(
    r"(?:[A-Za-z]:(?:\\|\\\\)(?:Users|r)(?:\\|\\\\)|/(?:Users|home|var|opt)/)",
    flags=re.IGNORECASE,
)


def verify(
    root: Path,
    *,
    allow_degraded: bool = False,
    expected_source_sha: str = "",
) -> dict[str, object]:
    errors: list[str] = []
    inventory_valid = False
    try:
        _public_inventory.assert_exact_public_inventory(root)
    except _public_inventory.PublicArtifactInventoryError as exc:
        errors.append(f"public_inventory_invalid:{exc}")
    else:
        inventory_valid = True
    missing = [name for name in REQUIRED_FILES if not (root / name).is_file()]
    errors.extend(f"missing:{name}" for name in missing)

    forbidden = []
    if inventory_valid:
        for name in REQUIRED_FILES:
            path = root / name
            if any(part in path.name.lower() for part in FORBIDDEN_FILE_PARTS):
                forbidden.append(str(path.relative_to(root)).replace("\\", "/"))
    errors.extend(f"forbidden_file:{name}" for name in forbidden)
    exposed_paths = []
    if inventory_valid:
        for name in REQUIRED_FILES:
            path = root / name
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
    opportunity_path = root / "data" / "opportunity-projection.json"
    opportunity_manifest_path = root / "data" / "opportunity-projection.json.manifest.json"
    v6_path = root / "data" / "v6-learning.json"
    snapshot: dict[str, object] = {}
    manifest: dict[str, object] = {}
    build_manifest: dict[str, object] = {}
    calendar_manifest: dict[str, object] = {}
    publication_set: dict[str, object] = {}
    scenarios_manifest: dict[str, object] = {}
    opportunity: dict[str, object] = {}
    opportunity_manifest: dict[str, object] = {}
    v6_hash = ""
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
        if not is_lower_hex64(publication_set.get("publication_set_sha256")):
            errors.append("publication_set_sha256_invalid")
    if scenarios_manifest_path.is_file():
        scenarios_manifest = json.loads(scenarios_manifest_path.read_text(encoding="utf-8"))
        if scenarios_path.is_file() and (
            scenarios_manifest.get("payload_sha256")
            != hashlib.sha256(scenarios_path.read_bytes()).hexdigest()
        ):
            errors.append("scenario_hash_mismatch")
        if scenarios_manifest.get("calibration_status") != "UNCALIBRATED":
            errors.append("scenario_calibration_disclosure_missing")
        if publication_set_path.is_file() and (
            publication_set.get("scenario_payload_sha256")
            != scenarios_manifest.get("payload_sha256")
        ):
            errors.append("publication_set_scenario_hash_mismatch")
        if publication_set_path.is_file() and publication_set.get(
            "scenario_manifest_sha256"
        ) != hashlib.sha256(scenarios_manifest_path.read_bytes()).hexdigest():
            errors.append("publication_set_scenario_manifest_hash_mismatch")

    readiness_lineage = {}
    try:
        candidate_readiness = json.loads((root / "readiness.json").read_bytes())
        if isinstance(candidate_readiness, dict):
            readiness_lineage = candidate_readiness
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        pass
    account_session_report = readiness_lineage.get("account_session_report")
    account_session_report = (
        account_session_report if isinstance(account_session_report, dict) else {}
    )
    expected_publication_set_hash = _publication_set_sha256(
        manifest,
        calendar_manifest,
        scenarios_manifest if scenarios_manifest else None,
        account_session_report,
    )
    if publication_set.get("publication_set_sha256") != expected_publication_set_hash:
        errors.append("publication_set_formula_mismatch")
    if publication_set.get("market_date") != manifest.get("market_date"):
        errors.append("publication_set_market_date_mismatch")
    if publication_set.get("canonical_input_hash_sha256") != manifest.get(
        "input_hash_sha256"
    ):
        errors.append("publication_set_canonical_hash_mismatch")
    if publication_set.get("performance_manifest_id") != manifest.get("manifest_id"):
        errors.append("publication_set_performance_manifest_id_mismatch")
    if publication_set.get("calendar_manifest_id") != calendar_manifest.get("manifest_id"):
        errors.append("publication_set_calendar_manifest_id_mismatch")
    if publication_set.get("research_only") is not True:
        errors.append("publication_set_research_only_missing")
    if publication_set.get("live_trading_enabled") is not False:
        errors.append("publication_set_live_trading_enabled")
    for field in (
        "status",
        "input_hash_sha256",
        "expected_calendar_hash_sha256",
        "code_sha",
        "ledger_lineage_sha256",
        "current_session_lineage_sha256",
        "expected_current_session_lineage_sha256",
        "current_session_lineage_match",
        *OFFICIAL_ACCOUNT_SESSION_IDENTITY,
    ):
        if publication_set.get(f"account_session_{field}") != account_session_report.get(field):
            errors.append(f"publication_set_account_session_{field}_mismatch")
    if opportunity_path.is_file():
        opportunity_bytes = opportunity_path.read_bytes()
        opportunity = json.loads(opportunity_bytes)
        if opportunity.get("schema_version") != "dawnstrike.opportunity_projection.v1":
            errors.append("opportunity_schema_version_invalid")
        rows = opportunity.get("rows")
        opportunity_rows = rows if isinstance(rows, list) else []
        if not isinstance(rows, list):
            errors.append("opportunity_rows_invalid")
        else:
            errors.extend(validate_opportunity_projection_rows(rows))
        if len(opportunity_rows) > 5:
            errors.append("opportunity_row_limit_exceeded")
        if opportunity.get("row_count") != len(opportunity_rows):
            errors.append("opportunity_row_count_mismatch")
        if opportunity.get("state") not in {
            "DISABLED",
            "DATA_UNAVAILABLE",
            "NO_QUALIFYING",
            "QUALIFYING",
        }:
            errors.append("opportunity_state_invalid")
        if opportunity.get("research_only") is not True:
            errors.append("opportunity_research_only_missing")
        if opportunity.get("order_execution_enabled") is not False:
            errors.append("opportunity_execution_boundary_invalid")
        if opportunity.get("state") == "DISABLED" and opportunity_rows:
            errors.append("disabled_opportunity_rows_present")
        if opportunity.get("state") == "DATA_UNAVAILABLE" and (
            opportunity_rows
            or opportunity.get("source_run_id") is not None
            or opportunity.get("as_of") is not None
        ):
            errors.append("unavailable_opportunity_exposes_source")
        if opportunity.get("state") == "DISABLED" and (
            opportunity.get("source_run_id") is not None
            or opportunity.get("as_of") is not None
        ):
            errors.append("disabled_opportunity_exposes_source")
        if opportunity.get("state") == "NO_QUALIFYING" and opportunity.get("message") != (
            "NO QUALIFYING TRADE CURRENTLY EXISTS."
        ):
            errors.append("opportunity_no_qualifying_message_invalid")
    if opportunity_manifest_path.is_file():
        opportunity_manifest = json.loads(
            opportunity_manifest_path.read_text(encoding="utf-8")
        )
        if (
            opportunity_manifest.get("schema_version")
            != "dawnstrike.opportunity_projection_manifest.v1"
        ):
            errors.append("opportunity_manifest_schema_version_invalid")
        if opportunity_path.is_file():
            if opportunity_manifest.get("payload_sha256") != hashlib.sha256(
                opportunity_path.read_bytes()
            ).hexdigest():
                errors.append("opportunity_hash_mismatch")
            if opportunity_manifest.get("byte_count") != opportunity_path.stat().st_size:
                errors.append("opportunity_byte_count_mismatch")
            if opportunity_manifest.get("state") != opportunity.get("state"):
                errors.append("opportunity_manifest_state_mismatch")
            if opportunity_manifest.get("row_count") != opportunity.get("row_count"):
                errors.append("opportunity_manifest_row_count_mismatch")
        if not is_lower_hex64(opportunity_manifest.get("payload_sha256")):
            errors.append("opportunity_projection_sha256_invalid")
        if opportunity.get("state") in {"QUALIFYING", "NO_QUALIFYING"}:
            _opportunity_lineage_failures(
                opportunity,
                opportunity_manifest,
                expected_market_date=str(
                    readiness_lineage.get("market_date") or ""
                ),
                errors=errors,
            )
    if v6_path.is_file():
        v6_bytes = v6_path.read_bytes()
        v6_hash = hashlib.sha256(v6_bytes).hexdigest()
        try:
            parsed_v6 = json.loads(v6_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            errors.append("v6_learning_unreadable")
        else:
            if not isinstance(parsed_v6, dict):
                errors.append("v6_learning_payload_invalid")
            else:
                errors.extend(_v6_contract_failures(parsed_v6))
    if build_manifest_path.is_file():
        build_manifest = json.loads(build_manifest_path.read_text(encoding="utf-8"))
        if set(build_manifest) != BUILD_MANIFEST_KEYS:
            errors.append("build_manifest_keys_invalid")
        if build_manifest.get("schema_version") != "dawnstrike.public_build.v1":
            errors.append("build_manifest_schema_invalid")
        if build_manifest.get("research_only") is not True:
            errors.append("build_manifest_research_only_invalid")
        if build_manifest.get("live_trading_enabled") is not False:
            errors.append("build_manifest_live_trading_enabled_invalid")
        if build_manifest.get("broker_execution_enabled") is not False:
            errors.append("build_manifest_broker_execution_enabled_invalid")
        if not build_manifest.get("source_sha"):
            errors.append("source_sha_missing")
        if expected_source_sha and build_manifest.get("source_sha") != expected_source_sha:
            errors.append("source_sha_not_expected_runtime_head")
        if build_manifest.get("source_clean") is not True:
            errors.append("source_not_clean")
        if manifest.get("market_date") != build_manifest.get("market_date"):
            errors.append("performance_manifest_market_date_mismatch")
        calendar_market_date = calendar_manifest.get("market_date")
        if calendar_market_date != build_manifest.get("market_date"):
            errors.append("calendar_manifest_market_date_mismatch")
        if calendar_path.is_file():
            try:
                calendar_payload = json.loads(calendar_path.read_bytes())
            except (UnicodeDecodeError, json.JSONDecodeError):
                calendar_payload = None
            calendar_payload_date = (
                calendar_payload.get("as_of_market_date")
                if isinstance(calendar_payload, dict)
                else None
            )
            if calendar_payload_date != build_manifest.get("market_date"):
                errors.append("calendar_payload_market_date_mismatch")
        if not build_manifest.get("build_id"):
            errors.append("build_id_missing")
        if build_manifest.get("data_hash_sha256") != manifest.get("payload_sha256"):
            errors.append("build_data_hash_mismatch")
        if build_manifest.get("publication_set_sha256") != publication_set.get(
            "publication_set_sha256"
        ):
            errors.append("build_publication_set_hash_mismatch")
        if build_manifest.get("opportunity_projection_sha256") != (
            opportunity_manifest.get("payload_sha256")
        ):
            errors.append("build_opportunity_projection_hash_mismatch")
        if opportunity.get("state") in {"QUALIFYING", "NO_QUALIFYING"}:
            _opportunity_lineage_failures(
                opportunity,
                opportunity_manifest,
                expected_market_date=str(build_manifest.get("market_date") or ""),
                errors=errors,
            )
        if build_manifest.get("v6_learning_sha256") != v6_hash:
            errors.append("build_v6_learning_hash_mismatch")
        if not is_lower_hex64(v6_hash):
            errors.append("v6_learning_sha256_invalid")
        if not is_lower_hex64(build_manifest.get("v6_learning_sha256")):
            errors.append("build_v6_learning_sha256_invalid")
        if not is_lower_hex64(build_manifest.get("build_sha")):
            errors.append("build_sha_invalid")
        expected_build_sha = _build_sha(
            source_sha=str(build_manifest.get("source_sha") or ""),
            publication_set_sha256=str(build_manifest.get("publication_set_sha256") or ""),
            opportunity_projection_sha256=str(
                build_manifest.get("opportunity_projection_sha256") or ""
            ),
            v6_learning_sha256=v6_hash,
            market_date=str(build_manifest.get("market_date") or ""),
        )
        if (
            is_lower_hex64(build_manifest.get("publication_set_sha256"))
            and is_lower_hex64(build_manifest.get("opportunity_projection_sha256"))
            and is_lower_hex64(v6_hash)
            and is_lower_hex64(build_manifest.get("build_sha"))
        ):
            if build_manifest.get("build_sha") != expected_build_sha:
                errors.append("build_sha_formula_mismatch")
            if build_manifest.get("build_id") != expected_build_sha[:20]:
                errors.append("build_id_formula_mismatch")
    release_manifest_path = root / "release-manifest.json"
    if release_manifest_path.is_file():
        try:
            release_manifest = json.loads(release_manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            release_manifest = {}
            errors.append("release_manifest_unreadable")
        if isinstance(release_manifest, dict):
            if set(release_manifest) != RELEASE_MANIFEST_KEYS:
                errors.append("release_manifest_keys_invalid")
            if release_manifest.get("schema_version") != "dawnstrike.release_manifest.v1":
                errors.append("release_manifest_schema_invalid")
            if release_manifest.get("source_sha") != build_manifest.get("source_sha"):
                errors.append("release_source_sha_mismatch")
            if release_manifest.get("build_sha") != build_manifest.get("build_sha"):
                errors.append("release_build_sha_mismatch")
            if release_manifest.get("v6_learning_sha256") != v6_hash:
                errors.append("release_v6_learning_hash_mismatch")
            if not is_lower_hex64(release_manifest.get("v6_learning_sha256")):
                errors.append("release_v6_learning_sha256_invalid")
            if release_manifest.get("deployment_boundary") != (
                "configured_runtime_and_durable_state"
            ) or not is_lower_hex64(release_manifest.get("deployment_boundary_sha256")):
                errors.append("release_deployment_boundary_invalid")
            schema_version = release_manifest.get("database_schema_version")
            if isinstance(schema_version, bool) or not isinstance(schema_version, int) or (
                schema_version < 1
            ):
                errors.append("release_database_schema_version_invalid")
            if release_manifest.get("data_watermark") != build_manifest.get("market_date"):
                errors.append("release_data_watermark_mismatch")
            if release_manifest.get("strategy_versions") != EXPECTED_STRATEGY_VERSIONS:
                errors.append("release_strategy_versions_invalid")
            if release_manifest.get("scheduler_version") != "dawnstrike-scheduler-v6":
                errors.append("release_scheduler_version_invalid")
            if release_manifest.get("research_only") is not True:
                errors.append("release_research_only_missing")
            if release_manifest.get("broker_execution_enabled") is not False:
                errors.append("release_broker_execution_enabled")
            release_unsigned = dict(release_manifest)
            recorded_release_self_hash = release_unsigned.pop(
                "release_manifest_sha256", None
            )
            expected_release_self_hash = hashlib.sha256(
                json.dumps(
                    release_unsigned, sort_keys=True, separators=(",", ":")
                ).encode()
            ).hexdigest()
            if recorded_release_self_hash != expected_release_self_hash:
                errors.append("release_manifest_self_hash_mismatch")
            if build_manifest.get("release_manifest_sha256") != recorded_release_self_hash:
                errors.append("build_release_manifest_hash_mismatch")
        recorded_hashes = build_manifest.get("file_hashes")
        if not isinstance(recorded_hashes, dict):
            errors.append("file_hashes_missing")
        else:
            expected_hash_names = set(REQUIRED_FILES) - {"build-manifest.json"}
            observed_hash_names = set(recorded_hashes)
            for name in sorted(expected_hash_names - observed_hash_names):
                errors.append(f"required_file_hash_missing:{name}")
            for name in sorted(observed_hash_names - expected_hash_names):
                errors.append(f"unexpected_file_hash:{name}")
            for name in sorted(expected_hash_names & observed_hash_names):
                expected_hash = recorded_hashes[name]
                path = root / name
                if not path.is_file():
                    errors.append(f"file_hash_path_missing:{name}")
                elif hashlib.sha256(path.read_bytes()).hexdigest() != expected_hash:
                    errors.append(f"file_hash_mismatch:{name}")
            release_hashes = release_manifest.get("artifact_hashes")
            expected_release_names = set(REQUIRED_FILES) - {
                "build-manifest.json",
                "release-manifest.json",
            }
            if not isinstance(release_hashes, dict):
                errors.append("release_artifact_hashes_missing")
            elif set(release_hashes) != expected_release_names:
                errors.append("release_artifact_hash_inventory_mismatch")
            else:
                for name in sorted(expected_release_names):
                    if release_hashes.get(name) != recorded_hashes.get(name):
                        errors.append(f"release_artifact_hash_mismatch:{name}")

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
        if readiness.get("v6_learning_sha256") != v6_hash:
            errors.append("readiness_v6_learning_hash_mismatch")
        if not is_lower_hex64(readiness.get("v6_learning_sha256")):
            errors.append("readiness_v6_learning_sha256_invalid")
        if readiness.get("build_id") != build_manifest.get("build_id"):
            errors.append("readiness_build_id_mismatch")
        if readiness.get("deployed_build_sha") != build_manifest.get("build_sha"):
            errors.append("readiness_build_sha_mismatch")
        if readiness.get("market_date") != build_manifest.get("market_date"):
            errors.append("readiness_market_date_mismatch")
        if expected_source_sha:
            if readiness.get("research_only") is not True:
                errors.append("readiness_research_only_missing")
            if readiness.get("broker_execution_enabled") is not False:
                errors.append("readiness_broker_execution_enabled")
        errors.extend(
            _account_session_report_failures(
                readiness.get("account_session_report"),
                market_date=str(build_manifest.get("market_date") or ""),
                source_sha=str(build_manifest.get("source_sha") or ""),
            )
        )
        errors.extend(
            _account_session_reconciliation_failures(
                readiness.get("account_session_reconciliation"),
                report=readiness.get("account_session_report"),
                market_date=str(build_manifest.get("market_date") or ""),
                source_sha=str(build_manifest.get("source_sha") or ""),
            )
        )

    try:
        _public_inventory.assert_exact_public_inventory(root)
    except _public_inventory.PublicArtifactInventoryError as exc:
        terminal_inventory_error = f"public_inventory_changed:{exc}"
        if terminal_inventory_error not in errors:
            errors.append(terminal_inventory_error)

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
    try:
        result.update(public_artifact_identity(root))
    except (OSError, _public_inventory.PublicArtifactInventoryError) as exc:
        errors.append(f"public_artifact_identity_unavailable:{exc}")
        result["status"] = "FAIL"
    return result


def public_artifact_identity(root: Path) -> dict[str, str]:
    """Return the non-circular exact identity of every public artifact byte."""

    root = Path(root)
    _public_inventory.assert_exact_public_inventory(root)
    artifact_hashes = {
        name: hashlib.sha256((root / name).read_bytes()).hexdigest()
        for name in ARTIFACT_ROOT_FILES
    }
    artifact_root = hashlib.sha256(
        json.dumps(
            artifact_hashes, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    return {
        "build_manifest_sha256": hashlib.sha256(
            (root / "build-manifest.json").read_bytes()
        ).hexdigest(),
        "release_manifest_raw_sha256": artifact_hashes["release-manifest.json"],
        "public_artifact_root_sha256": artifact_root,
    }


def _opportunity_lineage_failures(
    payload: dict[str, object],
    manifest: dict[str, object],
    *,
    expected_market_date: str,
    errors: list[str],
) -> None:
    """Bind an active opportunity projection to one exact market session.

    Historical opportunity runs are intentionally retained in the private
    store. The public artifact must not turn an older run into today's pick,
    even when every payload hash is internally consistent. The projection's
    ``as_of`` timestamp, derived market date, source run identity, and manifest
    metadata therefore form one small, independently checked lineage join.
    """

    if payload.get("schema_version") != "dawnstrike.opportunity_projection.v1":
        _append_unique_error(errors, "opportunity_schema_version_invalid")
    if manifest.get("schema_version") != "dawnstrike.opportunity_projection_manifest.v1":
        _append_unique_error(errors, "opportunity_manifest_schema_version_invalid")
    state = payload.get("state")
    as_of = payload.get("as_of")
    as_of_date: str | None = None
    if not isinstance(as_of, str) or not as_of.strip():
        _append_unique_error(errors, "opportunity_as_of_missing")
    else:
        try:
            parsed = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
            if parsed.utcoffset() is None:
                raise ValueError("opportunity as_of must include a timezone offset")
            as_of_date = parsed.astimezone(ZoneInfo("America/New_York")).date().isoformat()
        except ValueError:
            _append_unique_error(errors, "opportunity_as_of_invalid")
    if as_of_date is not None:
        if expected_market_date and as_of_date != expected_market_date:
            _append_unique_error(errors, "opportunity_as_of_market_date_mismatch")
        if payload.get("market_date") != as_of_date:
            _append_unique_error(errors, "opportunity_market_date_mismatch")
    elif payload.get("market_date") not in {None, ""}:
        _append_unique_error(errors, "opportunity_market_date_invalid")
    source_run_id = payload.get("source_run_id")
    if not isinstance(source_run_id, str) or not source_run_id.strip():
        _append_unique_error(errors, "opportunity_source_run_id_missing")
    manifest_market_date = manifest.get("market_date")
    expected_manifest_date = expected_market_date or as_of_date
    if expected_manifest_date and manifest_market_date != expected_manifest_date:
        _append_unique_error(errors, "opportunity_manifest_market_date_mismatch")
    if manifest.get("source_run_id") != source_run_id:
        _append_unique_error(errors, "opportunity_manifest_source_run_id_mismatch")
    if manifest.get("as_of") != as_of:
        _append_unique_error(errors, "opportunity_manifest_as_of_mismatch")
    if state not in {"QUALIFYING", "NO_QUALIFYING"}:
        _append_unique_error(errors, "opportunity_active_state_invalid")


def _append_unique_error(errors: list[str], value: str) -> None:
    if value not in errors:
        errors.append(value)


def _publication_set_sha256(
    performance_manifest: dict[str, object],
    calendar_manifest: dict[str, object],
    scenario_manifest: dict[str, object] | None,
    account_session_report: dict[str, object] | None,
) -> str:
    """Independently recompute the producer's exact publication-set formula."""

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


def _build_sha(
    *,
    source_sha: str,
    publication_set_sha256: str,
    opportunity_projection_sha256: str,
    v6_learning_sha256: str,
    market_date: str,
) -> str:
    """Independently recompute the exact documented public-build formula."""

    formula = (
        f"{source_sha}:{publication_set_sha256}:{opportunity_projection_sha256}:"
        f"{v6_learning_sha256}:{market_date}"
    )
    return hashlib.sha256(formula.encode("utf-8")).hexdigest()


def _v6_contract_failures(payload: dict[str, object]) -> list[str]:
    """Validate the exact safe projection emitted by ``v6_public_status``."""

    failures: list[str] = []
    keys = frozenset(payload)
    for name in sorted(V6_TOP_LEVEL_KEYS - keys):
        failures.append(f"v6_field_missing:{name}")
    for name in sorted(keys - V6_TOP_LEVEL_KEYS):
        failures.append(f"v6_field_unexpected:{name}")
    if payload.get("schema_version") != V6_SCHEMA_VERSION:
        failures.append("v6_schema_version_invalid")
    if payload.get("strategy_version") != "dawnstrike-alphaops-v6-shadow":
        failures.append("v6_strategy_version_invalid")
    for name in (
        "decision_count",
        "tracked_count",
        "outcome_count",
        "learning_eligible_outcome_count",
    ):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            failures.append(f"v6_{name}_invalid")
    if not isinstance(payload.get("decision_replay"), list):
        failures.append("v6_decision_replay_invalid")
    if not isinstance(payload.get("operational_freshness"), dict):
        failures.append("v6_operational_freshness_invalid")
    if not isinstance(payload.get("prediction_evidence_gate"), dict):
        failures.append("v6_prediction_evidence_gate_invalid")
    if not isinstance(payload.get("failure_attribution"), dict):
        failures.append("v6_failure_attribution_invalid")
    if not isinstance(payload.get("promotion_readiness"), dict):
        failures.append("v6_promotion_readiness_invalid")
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
        if promotion.get("research_only") is not True:
            failures.append("v6_promotion_research_only_invalid")
        if promotion.get("broker_execution_enabled") is not False:
            failures.append("v6_promotion_broker_execution_invalid")
        if promotion.get("status") not in {
            "NOT_ELIGIBLE_FOR_PROMOTION",
            "ELIGIBLE_FOR_MANUAL_REVIEW",
            "MANUALLY_APPROVED_FOR_CONTROLLED_PROMOTION",
        }:
            failures.append("v6_promotion_status_invalid")
        if promotion.get("performance_status") not in {
            "WAITING_FOR_FORWARD_EVIDENCE",
            "ELIGIBLE_FOR_MANUAL_REVIEW",
        }:
            failures.append("v6_performance_status_invalid")
    return failures


def _account_session_report_failures(
    value: object, *, market_date: str, source_sha: str
) -> list[str]:
    if not isinstance(value, dict):
        return ["account_session_report_missing"]
    failures: list[str] = []
    expected = value.get("expected_session_count")
    if (
        value.get("schema_version") != "dawnstrike.account_session_report.v1"
        or value.get("status") != "COMPLETE"
        or value.get("market_date") != market_date
        or value.get("code_sha") != source_sha
        or value.get("research_only") is not True
        or value.get("broker_execution_enabled") is not False
        or value.get("unsafe_ledger_count") != 0
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
    else:
        item = series[0]
        if (
            item.get("status") != "COMPLETE"
            or item.get("market_date") != market_date
            or item.get("code_sha") != source_sha
            or item.get("expected_session_count") != expected
            or item.get("ledger_row_count") != expected
            or item.get("complete_count") != expected
            or item.get("research_only") is not True
            or item.get("broker_execution_enabled") is not False
            or any(
                item.get(field) != expected
                for field, expected in OFFICIAL_ACCOUNT_SESSION_IDENTITY.items()
            )
        ):
            failures.append("account_session_report_series_invalid")
    for field in (
        "input_hash_sha256",
        "expected_calendar_hash_sha256",
        "source_hashes_sha256",
        "ledger_lineage_sha256",
        "current_session_lineage_sha256",
        "expected_current_session_lineage_sha256",
    ):
        if not is_lower_hex64(value.get(field)):
            failures.append(f"account_session_report_{field}_invalid")
    if (
        value.get("current_session_lineage_match") is not True
        or value.get("current_session_lineage_sha256")
        != value.get("expected_current_session_lineage_sha256")
    ):
        failures.append("account_session_report_lineage_invalid")
    if isinstance(series, list) and len(series) == 1 and isinstance(series[0], dict):
        item = series[0]
        if (
            item.get("current_session_lineage_match") is not True
            or item.get("ledger_lineage_sha256") != value.get("ledger_lineage_sha256")
            or item.get("current_session_lineage_sha256")
            != value.get("current_session_lineage_sha256")
            or item.get("expected_current_session_lineage_sha256")
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


def _v6_safety_flag_failures(value: object, path: str = "v6") -> list[str]:
    """Reject unsafe flags anywhere in the bounded projection."""

    failures: list[str] = []
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{path}.{key}"
            if key in {"research_only"} and item is not True:
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", default="build/public")
    parser.add_argument("--allow-degraded", action="store_true")
    parser.add_argument("--expected-source-sha", default="")
    args = parser.parse_args(argv)
    result = verify(
        Path(args.root).resolve(),
        allow_degraded=args.allow_degraded,
        expected_source_sha=args.expected_source_sha.strip(),
    )
    print(json.dumps(result, sort_keys=True, indent=2))
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
