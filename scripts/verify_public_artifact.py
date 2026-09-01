"""Verify the bounded, static Vercel artifact before it can be promoted."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import re
import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[1]
_REPO_ROOT_TEXT = str(_REPO_ROOT)
if _REPO_ROOT_TEXT in sys.path:
    sys.path.remove(_REPO_ROOT_TEXT)
sys.path.insert(0, _REPO_ROOT_TEXT)

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
    if opportunity_path.is_file():
        opportunity_bytes = opportunity_path.read_bytes()
        opportunity = json.loads(opportunity_bytes)
        rows = opportunity.get("rows")
        opportunity_rows = rows if isinstance(rows, list) else []
        if not isinstance(rows, list):
            errors.append("opportunity_rows_invalid")
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
        if opportunity.get("state") == "NO_QUALIFYING" and opportunity.get("message") != (
            "NO QUALIFYING TRADE CURRENTLY EXISTS."
        ):
            errors.append("opportunity_no_qualifying_message_invalid")
    if opportunity_manifest_path.is_file():
        opportunity_manifest = json.loads(
            opportunity_manifest_path.read_text(encoding="utf-8")
        )
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
        if not build_manifest.get("source_sha"):
            errors.append("source_sha_missing")
        if expected_source_sha and build_manifest.get("source_sha") != expected_source_sha:
            errors.append("source_sha_not_expected_runtime_head")
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
        if build_manifest.get("opportunity_projection_sha256") != (
            opportunity_manifest.get("payload_sha256")
        ):
            errors.append("build_opportunity_projection_hash_mismatch")
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
            if release_manifest.get("build_sha") != build_manifest.get("build_sha"):
                errors.append("release_build_sha_mismatch")
            if release_manifest.get("v6_learning_sha256") != v6_hash:
                errors.append("release_v6_learning_hash_mismatch")
            if not is_lower_hex64(release_manifest.get("v6_learning_sha256")):
                errors.append("release_v6_learning_sha256_invalid")
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
    return result


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
