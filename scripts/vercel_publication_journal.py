"""Strict, atomic journal contract for Vercel production publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "dawnstrike.vercel_publication_journal.v1"
COMPENSATED_SCHEMA = "dawnstrike.vercel_publication_journal.v2"
EMPTY_SHA256 = hashlib.sha256(b"").hexdigest()
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
PHASES = {"PRE_MUTATION": 0, "POST_ALIASES": 1, "COMPLETE": 2, "COMPENSATED": 3}
KEYS = {
    "schema_version",
    "operation",
    "phase",
    "sequence",
    "project_id",
    "project_name",
    "production_aliases",
    "candidate_preview_url",
    "candidate_preview_deployment_id",
    "candidate_source_sha",
    "candidate_source_tree",
    "candidate_market_date",
    "candidate_build_id",
    "candidate_build_sha",
    "candidate_manifest_sha256",
    "candidate_package_manifest_sha256",
    "prior_aliases",
    "promoted_deployment_id",
    "promoted_deployment_url",
    "production_result_sha256",
    "result_relative_path",
    "result_payload",
    "prior_journal_file_sha256",
    "compensation_relative_path",
    "compensation_sha256",
    "recorded_at_utc",
    "research_only",
    "broker_execution_enabled",
    "journal_self_sha256",
}
AUTHORIZATION_KEYS = KEYS | {
    "expected_market_date",
    "prepublication_authorization_id",
    "daily_ledger_authorization_id",
}
COMPENSATION_KEYS = {
    "schema_version",
    "status",
    "operation",
    "candidate_source_sha",
    "candidate_source_tree",
    "candidate_preview_deployment_id",
    "prior_aliases",
    "failure_type",
    "research_only",
    "broker_execution_enabled",
    "recorded_at_utc",
    "receipt_self_sha256",
}


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def canonical_json(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _safe_relative(value: Any) -> None:
    if not isinstance(value, str) or not value or "\\" in value:
        raise ValueError("relative path is invalid")
    path = Path(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("relative path is unsafe")


def _utc(value: Any) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("recorded_at_utc must be canonical UTC")
    text = value[:-1]
    try:
        datetime.fromisoformat(text + "+00:00")
    except ValueError as exc:
        raise ValueError("recorded_at_utc is invalid") from exc


def _identity(value: Any, field: str) -> None:
    if not isinstance(value, str) or not HEX40.fullmatch(value):
        raise ValueError(f"{field} must be lowercase 40-hex")


def _hash(value: Any, field: str) -> None:
    if not isinstance(value, str) or not HEX64.fullmatch(value):
        raise ValueError(f"{field} must be lowercase 64-hex")


def _result_authorization(value: Any, expected_market_date: str) -> None:
    """Validate optional governed daily-publication identity fields."""

    if value is None:
        return
    if not isinstance(value, dict):
        raise ValueError("production result payload is invalid")
    present = {
        key
        for key in (
            "expected_market_date",
            "prepublication_authorization_id",
            "daily_ledger_authorization_id",
        )
        if key in value
    }
    if not present:
        return  # retain validation of historical v1 journal payloads
    if present != {
        "expected_market_date",
        "prepublication_authorization_id",
        "daily_ledger_authorization_id",
    }:
        raise ValueError("production result authorization identity is incomplete")
    if value["expected_market_date"] != expected_market_date:
        raise ValueError("production result expected market date mismatch")
    _hash(value["prepublication_authorization_id"], "prepublication_authorization_id")
    _hash(value["daily_ledger_authorization_id"], "daily_ledger_authorization_id")
    if value["prepublication_authorization_id"] != value["daily_ledger_authorization_id"]:
        raise ValueError("production result authorization identities diverge")


def _alias(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"alias", "deployment_id", "deployment_url"}:
        raise ValueError("prior alias keys are not exact")
    if not isinstance(value["alias"], str) or not value["alias"].startswith("https://"):
        raise ValueError("prior alias is invalid")
    if not isinstance(value["deployment_id"], str) or not value["deployment_id"]:
        raise ValueError("prior alias deployment ID is invalid")
    if not isinstance(value["deployment_url"], str) or not value["deployment_url"]:
        raise ValueError("prior alias deployment URL is invalid")


def validate(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid strict journal JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) not in (KEYS, AUTHORIZATION_KEYS):
        raise ValueError("journal keys are not exact")
    has_authorization = set(value) == AUTHORIZATION_KEYS
    if value["schema_version"] not in {SCHEMA, COMPENSATED_SCHEMA}:
        raise ValueError("journal schema is invalid")
    if value["operation"] != "vercel_publication":
        raise ValueError("journal operation is invalid")
    phase = value["phase"]
    if (
        phase not in PHASES
        or type(value["sequence"]) is not int
        or value["sequence"] != PHASES[phase]
    ):
        raise ValueError("journal phase sequence is invalid")
    if phase == "COMPENSATED" and value["schema_version"] != COMPENSATED_SCHEMA:
        raise ValueError("compensation requires the v2 schema")
    if phase != "COMPENSATED" and value["schema_version"] != SCHEMA:
        raise ValueError("non-compensated journal requires the v1 schema")
    for field in ("candidate_source_sha", "candidate_source_tree"):
        _identity(value[field], field)
    for field in (
        "candidate_build_sha",
        "candidate_manifest_sha256",
        "candidate_package_manifest_sha256",
        "production_result_sha256",
        "compensation_sha256",
        "journal_self_sha256",
    ):
        _hash(value[field], field)
    if not isinstance(value["project_id"], str) or not value["project_id"]:
        raise ValueError("project ID is invalid")
    if not isinstance(value["project_name"], str) or not value["project_name"]:
        raise ValueError("project name is invalid")
    if (
        not isinstance(value["production_aliases"], list)
        or not value["production_aliases"]
        or value["production_aliases"] != sorted(set(value["production_aliases"]))
    ):
        raise ValueError("production aliases must be sorted and unique")
    if any(
        not isinstance(item, str) or not item.startswith("https://")
        for item in value["production_aliases"]
    ):
        raise ValueError("production aliases are invalid")
    if not isinstance(value["prior_aliases"], list) or len(value["prior_aliases"]) != len(
        value["production_aliases"]
    ):
        raise ValueError("prior aliases are incomplete")
    for expected, item in zip(value["production_aliases"], value["prior_aliases"], strict=True):
        _alias(item)
        if item["alias"] != expected:
            raise ValueError("prior alias order or identity is invalid")
    if not isinstance(value["candidate_preview_url"], str) or not value[
        "candidate_preview_url"
    ].startswith("https://"):
        raise ValueError("candidate preview URL is invalid")
    if (
        not isinstance(value["candidate_preview_deployment_id"], str)
        or not value["candidate_preview_deployment_id"]
    ):
        raise ValueError("candidate preview deployment ID is invalid")
    if not isinstance(value["candidate_market_date"], str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", value["candidate_market_date"]
    ):
        raise ValueError("candidate market date is invalid")
    if has_authorization:
        if value["expected_market_date"] != value["candidate_market_date"]:
            raise ValueError("journal expected market date mismatch")
        _hash(value["prepublication_authorization_id"], "prepublication_authorization_id")
        _hash(value["daily_ledger_authorization_id"], "daily_ledger_authorization_id")
        if value["prepublication_authorization_id"] != value["daily_ledger_authorization_id"]:
            raise ValueError("journal authorization identities diverge")
    if not isinstance(value["candidate_build_id"], str) or not value["candidate_build_id"]:
        raise ValueError("candidate build ID is invalid")
    if not isinstance(value["result_relative_path"], str):
        raise ValueError("result relative path is invalid")
    _safe_relative(value["result_relative_path"])
    if value["compensation_relative_path"] != "NONE":
        _safe_relative(value["compensation_relative_path"])
    if (
        value["compensation_relative_path"] == "NONE"
        and value["compensation_sha256"] != EMPTY_SHA256
    ):
        raise ValueError("compensation hash sentinel is invalid")
    if phase == "COMPENSATED" and value["compensation_relative_path"] == "NONE":
        raise ValueError("compensation path is missing")
    if phase == "COMPENSATED" and value["compensation_sha256"] == EMPTY_SHA256:
        raise ValueError("compensation hash is missing")
    if phase == "PRE_MUTATION":
        if (
            value["promoted_deployment_id"] is not None
            or value["promoted_deployment_url"] is not None
        ):
            raise ValueError("PRE_MUTATION carries promoted identity")
        if value["production_result_sha256"] != EMPTY_SHA256 or value["result_payload"] is not None:
            raise ValueError("PRE_MUTATION carries result proof")
    else:
        if (
            not isinstance(value["promoted_deployment_id"], str)
            or not value["promoted_deployment_id"]
        ):
            raise ValueError("promoted deployment ID is missing")
        if not isinstance(value["promoted_deployment_url"], str) or not value[
            "promoted_deployment_url"
        ].startswith("https://"):
            raise ValueError("promoted deployment URL is missing")
    if phase in {"POST_ALIASES", "COMPLETE"}:
        if (
            not isinstance(value["result_payload"], dict)
            or value["production_result_sha256"] == EMPTY_SHA256
        ):
            raise ValueError("production result proof is missing")
        if (
            hashlib.sha256(canonical_json(value["result_payload"])).hexdigest()
            != value["production_result_sha256"]
        ):
            raise ValueError("production result hash mismatch")
        _result_authorization(value["result_payload"], value["candidate_market_date"])
    if phase == "COMPLETE" and value["result_payload"].get("status") != "PRODUCTION_VERIFIED":
        raise ValueError("COMPLETE result is not PRODUCTION_VERIFIED")
    if value["research_only"] is not True or value["broker_execution_enabled"] is not False:
        raise ValueError("journal safety boundary is invalid")
    _utc(value["recorded_at_utc"])
    unsigned = dict(value)
    claimed = unsigned.pop("journal_self_sha256")
    if hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed:
        raise ValueError("journal self hash mismatch")
    return value


def validate_compensation(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid strict compensation JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != COMPENSATION_KEYS:
        raise ValueError("compensation keys are not exact")
    if (
        value["schema_version"] != "dawnstrike.vercel_publication_compensation.v1"
        or value["status"] != "COMPENSATED"
    ):
        raise ValueError("compensation schema or status is invalid")
    if value["operation"] != "vercel_publication":
        raise ValueError("compensation operation is invalid")
    _identity(value["candidate_source_sha"], "candidate_source_sha")
    _identity(value["candidate_source_tree"], "candidate_source_tree")
    if (
        not isinstance(value["candidate_preview_deployment_id"], str)
        or not value["candidate_preview_deployment_id"]
    ):
        raise ValueError("compensation candidate deployment ID is invalid")
    if not isinstance(value["prior_aliases"], list) or not value["prior_aliases"]:
        raise ValueError("compensation prior aliases are invalid")
    for item in value["prior_aliases"]:
        _alias(item)
    if (
        not isinstance(value["failure_type"], str)
        or not value["failure_type"]
        or len(value["failure_type"]) > 256
    ):
        raise ValueError("compensation failure type is invalid")
    if value["research_only"] is not True or value["broker_execution_enabled"] is not False:
        raise ValueError("compensation safety boundary is invalid")
    _utc(value["recorded_at_utc"])
    unsigned = dict(value)
    claimed = unsigned.pop("receipt_self_sha256")
    if (
        not isinstance(claimed, str)
        or not HEX64.fullmatch(claimed)
        or hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed
    ):
        raise ValueError("compensation self hash mismatch")
    return value


def _read_regular(path: Path) -> bytes:
    before = path.lstat()
    if stat.S_ISLNK(before.st_mode) or not stat.S_ISREG(before.st_mode):
        raise ValueError("journal path must be a regular non-reparse leaf")
    if before.st_size > 1_048_576:
        raise ValueError("journal path is too large")
    raw = path.read_bytes()
    after = path.lstat()
    if (before.st_size, before.st_mtime_ns) != (after.st_size, after.st_mtime_ns):
        raise ValueError("journal changed while reading")
    return raw


def _contained(path: Path, root: Path) -> None:
    root = root.resolve(strict=True)
    resolved = path.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("journal path escapes StateRoot")
    cursor = path.parent
    while cursor != root.parent and cursor.exists():
        if cursor.is_symlink():
            raise ValueError("journal path contains a reparse component")
        if cursor == root:
            break
        cursor = cursor.parent


def _atomic_write(path: Path, raw: bytes, *, exclusive: bool = False) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=".vercel-journal-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if exclusive:
            os.link(temporary, path)
            os.unlink(temporary)
        else:
            os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def seal(source: Path, target: Path) -> dict[str, Any]:
    value = json.loads(_read_regular(source).decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(value, dict) or set(value) not in (
        KEYS - {"journal_self_sha256"},
        AUTHORIZATION_KEYS - {"journal_self_sha256"},
    ):
        raise ValueError("journal input keys are not exact")
    value["journal_self_sha256"] = hashlib.sha256(canonical_json(value)).hexdigest()
    raw = canonical_json(value)
    validate(raw)
    _atomic_write(target, raw)
    return {"payload": value, "raw_file_sha256": hashlib.sha256(raw).hexdigest()}


def transition(source: Path, target: Path, previous: Path) -> dict[str, Any]:
    value = json.loads(_read_regular(source).decode("utf-8"), object_pairs_hook=_pairs)
    if not isinstance(value, dict) or set(value) not in (
        KEYS - {"journal_self_sha256"},
        AUTHORIZATION_KEYS - {"journal_self_sha256"},
    ):
        raise ValueError("journal transition keys are not exact")
    prior_raw = _read_regular(previous)
    prior = validate(prior_raw)
    if value["prior_journal_file_sha256"] != hashlib.sha256(prior_raw).hexdigest():
        raise ValueError("journal prior raw hash mismatch")
    if value["operation"] != prior["operation"]:
        raise ValueError("journal operation changed")
    if value["phase"] != "COMPENSATED":
        expected = PHASES[prior["phase"]] + 1
        if value["sequence"] != expected or PHASES.get(value["phase"]) != expected:
            raise ValueError("journal transition is not adjacent")
    elif prior["phase"] in {"COMPLETE", "COMPENSATED"}:
        raise ValueError("terminal journal cannot be compensated")
    immutable = {
        key
        for key in set(value)
        if key
        not in {
            "phase",
            "sequence",
            "promoted_deployment_id",
            "promoted_deployment_url",
            "production_result_sha256",
            "result_payload",
            "prior_journal_file_sha256",
            "compensation_relative_path",
            "compensation_sha256",
            "recorded_at_utc",
            "schema_version",
            "journal_self_sha256",
        }
    }
    for key in immutable:
        if value[key] != prior[key]:
            raise ValueError(f"journal immutable field changed: {key}")
    if prior.get("result_payload") is not None or value.get("result_payload") is not None:
        prior_result = prior.get("result_payload") or {}
        next_result = value.get("result_payload") or {}
        for key in (
            "expected_market_date",
            "prepublication_authorization_id",
            "daily_ledger_authorization_id",
        ):
            if key in prior_result and next_result.get(key) != prior_result.get(key):
                raise ValueError(f"journal authorization identity changed: {key}")
    return seal(source, target)


def main() -> int:
    parser = argparse.ArgumentParser()
    subs = parser.add_subparsers(dest="command", required=True)
    verify = subs.add_parser("verify")
    verify.add_argument("path", type=Path)
    verify.add_argument("--state-root", required=True, type=Path)
    seal_parser = subs.add_parser("seal")
    seal_parser.add_argument("input", type=Path)
    seal_parser.add_argument("output", type=Path)
    seal_parser.add_argument("--state-root", required=True, type=Path)
    transition_parser = subs.add_parser("transition")
    transition_parser.add_argument("input", type=Path)
    transition_parser.add_argument("output", type=Path)
    transition_parser.add_argument("--previous", required=True, type=Path)
    transition_parser.add_argument("--state-root", required=True, type=Path)
    compensation_parser = subs.add_parser("verify-compensation")
    compensation_parser.add_argument("path", type=Path)
    compensation_parser.add_argument("--state-root", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "verify":
        _contained(args.path, args.state_root)
        raw = _read_regular(args.path)
        result = {"payload": validate(raw), "raw_file_sha256": hashlib.sha256(raw).hexdigest()}
    elif args.command == "verify-compensation":
        _contained(args.path, args.state_root)
        raw = _read_regular(args.path)
        result = {
            "payload": validate_compensation(raw),
            "raw_file_sha256": hashlib.sha256(raw).hexdigest(),
        }
    elif args.command == "seal":
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        result = seal(args.input, args.output)
    else:
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        _contained(args.previous, args.state_root)
        result = transition(args.input, args.output, args.previous)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
