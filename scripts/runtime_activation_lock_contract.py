"""Strict contract for the cross-operation Dawnstrike activation lock."""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = "dawnstrike.runtime_activation_lock.v2"
OPERATIONS = {
    "capture_task_hardening",
    "capture_task_rebind",
    "runtime_activation",
    "runtime_rollback",
    "state_preparation",
    "recovery",
}
KEYS = {
    "schema_version",
    "operation",
    "candidate_sha",
    "candidate_tree",
    "origin_identity",
    "origin_identity_sha256",
    "process_id",
    "process_started_at_utc",
    "acquired_at_utc",
    "lock_token",
    "research_only",
    "broker_execution_enabled",
}
HEX40 = re.compile(r"^[0-9a-f]{40}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
TOKEN = re.compile(r"^[0-9a-f]{32}$")


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key, value in pairs:
        if key in out:
            raise ValueError(f"duplicate key: {key}")
        out[key] = value
    return out


def _utc(value: Any, label: str) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError(f"{label} must be canonical UTC")
    normalized = value[:-1]
    if "." in normalized:
        head, fraction = normalized.rsplit(".", 1)
        normalized = head + "." + fraction[:6]
    try:
        parsed = datetime.fromisoformat(normalized + "+00:00")
    except ValueError as exc:
        raise ValueError(f"{label} is invalid") from exc
    offset = parsed.utcoffset()
    if offset is None or offset.total_seconds() != 0:
        raise ValueError(f"{label} must be UTC")


def validate(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid strict JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != KEYS:
        raise ValueError("lock keys are not exact")
    if value["schema_version"] != SCHEMA or value["operation"] not in OPERATIONS:
        raise ValueError("lock schema or operation is invalid")
    for key in ("candidate_sha", "candidate_tree"):
        if not isinstance(value[key], str) or not HEX40.fullmatch(value[key]):
            raise ValueError(f"{key} is invalid")
    origin = value["origin_identity"]
    if origin != "github.com/mattfren/dawnstrike":
        raise ValueError("origin identity is unsafe")
    origin_hash = hashlib.sha256(origin.encode()).hexdigest()
    if (
        not HEX64.fullmatch(str(value["origin_identity_sha256"]))
        or value["origin_identity_sha256"] != origin_hash
    ):
        raise ValueError("origin identity hash mismatch")
    if type(value["process_id"]) is not int or value["process_id"] <= 0:
        raise ValueError("process_id is invalid")
    _utc(value["process_started_at_utc"], "process_started_at_utc")
    _utc(value["acquired_at_utc"], "acquired_at_utc")
    if not isinstance(value["lock_token"], str) or not TOKEN.fullmatch(value["lock_token"]):
        raise ValueError("lock token is invalid")
    if value["research_only"] is not True or value["broker_execution_enabled"] is not False:
        raise ValueError("lock safety flags are invalid")
    return value


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("path", type=Path, nargs="?")
    parser.add_argument("--captured-base64")
    args = parser.parse_args()
    if (args.path is None) == (args.captured_base64 is None):
        parser.error("provide exactly one lock path or --captured-base64")
    if args.captured_base64 is not None:
        try:
            raw = base64.b64decode(args.captured_base64, validate=True)
        except ValueError as exc:
            raise ValueError("captured lock bytes are not strict base64") from exc
    else:
        raw = args.path.read_bytes()
    if len(raw) > 16384:
        raise ValueError("lock bytes exceed the strict ceiling")
    value = validate(raw)
    print(
        json.dumps(
            {"payload": value, "raw_file_sha256": hashlib.sha256(raw).hexdigest()},
            separators=(",", ":"),
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
