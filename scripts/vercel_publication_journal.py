"""Strict, atomic journal contract for Vercel production publication."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = "dawnstrike.vercel_publication_journal.v1"
COMPENSATED_SCHEMA = "dawnstrike.vercel_publication_journal.v2"
COMPENSATION_SCHEMA = "dawnstrike.vercel_publication_compensation.v1"
LOCK_SCHEMA = "dawnstrike.vercel_publication_lock.v1"
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
    "promoted_deployment_id",
    "promoted_deployment_url",
    "prior_aliases",
    "rollback_evidence",
    "rollback_status",
    "failure_type",
    "research_only",
    "broker_execution_enabled",
    "recorded_at_utc",
    "receipt_self_sha256",
}
LOCK_KEYS = {
    "schema_version",
    "operation",
    "owner_id",
    "pid",
    "process_start_time_utc",
    "candidate_source_sha",
    "candidate_source_tree",
    "candidate_market_date",
    "journal_path",
    "acquired_at_utc",
    "lock_self_sha256",
}


def _pairs(items: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in items:
        if key in result:
            raise ValueError(f"duplicate JSON field is forbidden: {key}")
        result[key] = value
    return result


def canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False
    ).encode("utf-8")


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
    if not isinstance(value, dict):
        raise ValueError("production result payload is invalid")
    for field in ("prepublication_authorization_id", "daily_ledger_authorization_id"):
        _hash(value.get(field), field)
    if value["prepublication_authorization_id"] != value["daily_ledger_authorization_id"]:
        raise ValueError("production result authorization identities diverge")
    if value.get("expected_market_date") != expected_market_date:
        raise ValueError("production result expected market date mismatch")


def _alias(value: Any) -> None:
    if not isinstance(value, dict) or set(value) != {"alias", "deployment_id", "deployment_url"}:
        raise ValueError("prior alias keys are not exact")
    if not isinstance(value["alias"], str) or not value["alias"].startswith("https://"):
        raise ValueError("prior alias is invalid")
    if not isinstance(value["deployment_id"], str) or not value["deployment_id"]:
        raise ValueError("prior alias deployment ID is invalid")
    if not isinstance(value["deployment_url"], str) or not value["deployment_url"]:
        raise ValueError("prior alias deployment URL is invalid")


def _rollback_evidence(value: Any) -> None:
    keys = {
        "alias",
        "expected_deployment_id",
        "expected_deployment_url",
        "observed_deployment_id",
        "observed_deployment_url",
        "restored",
    }
    if not isinstance(value, dict) or set(value) != keys:
        raise ValueError("rollback evidence keys are not exact")
    if not isinstance(value["alias"], str) or not value["alias"].startswith("https://"):
        raise ValueError("rollback evidence alias is invalid")
    for field in ("expected_deployment_id", "observed_deployment_id"):
        if not isinstance(value[field], str) or not value[field]:
            raise ValueError(f"rollback evidence {field} is invalid")
    for field in ("expected_deployment_url", "observed_deployment_url"):
        if not isinstance(value[field], str) or not value[field].startswith("https://"):
            raise ValueError(f"rollback evidence {field} is invalid")
    if value["restored"] is not True:
        raise ValueError("rollback evidence is not restored")


def validate(
    raw: bytes,
    *,
    state_root: Path | None = None,
    journal_path: Path | None = None,
) -> dict[str, Any]:
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
    elif phase in {"COMPLETE", "COMPENSATED"}:
        raise ValueError("terminal journal authorization identity is missing")
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
    elif phase == "COMPENSATED":
        if (value["promoted_deployment_id"] is None) != (
            value["promoted_deployment_url"] is None
        ):
            raise ValueError("compensated promoted identity is incomplete")
        if value["promoted_deployment_id"] is not None:
            if not isinstance(value["promoted_deployment_id"], str) or not value[
                "promoted_deployment_id"
            ]:
                raise ValueError("promoted deployment ID is invalid")
            if not isinstance(value["promoted_deployment_url"], str) or not value[
                "promoted_deployment_url"
            ].startswith("https://"):
                raise ValueError("promoted deployment URL is invalid")
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
    if phase == "COMPLETE":
        if state_root is None:
            raise ValueError("COMPLETE journal verification requires StateRoot")
        result = value["result_payload"]
        bindings = {
            "schema_version": "dawnstrike.daily_deployment.v1",
            "preview_url": value["candidate_preview_url"],
            "preview_deployment_id": value["candidate_preview_deployment_id"],
            "source_sha": value["candidate_source_sha"],
            "source_tree": value["candidate_source_tree"],
            "market_date": value["candidate_market_date"],
            "build_id": value["candidate_build_id"],
            "build_sha": value["candidate_build_sha"],
            "project_id": value["project_id"],
            "promoted_deployment_id": value["promoted_deployment_id"],
            "production_deployment_id": value["promoted_deployment_id"],
            "vercel_source_manifest_sha256": value["candidate_manifest_sha256"],
            "vercel_package_manifest_sha256": value["candidate_package_manifest_sha256"],
            "allow_degraded": False,
            "promoted": True,
            "live_trading_enabled": False,
            "research_only": True,
            "status": "PRODUCTION_VERIFIED",
        }
        # COMPLETE is only valid for the production promotion invocation.
        # Bind every identity and safety field that the deployment receipt
        # exposes, so a self-consistent but different receipt cannot be
        # replayed under an old journal.
        if type(result.get("allow_degraded")) is not bool:
            raise ValueError("COMPLETE result authorization is invalid")
        if type(result.get("promoted")) is not bool:
            raise ValueError("COMPLETE result promotion authorization is invalid")
        _result_authorization(result, value["candidate_market_date"])
        for field in (
            "expected_market_date",
            "prepublication_authorization_id",
            "daily_ledger_authorization_id",
        ):
            if result[field] != value[field]:
                raise ValueError(f"COMPLETE result authorization mismatch: {field}")
        for field, expected in bindings.items():
            if result.get(field) != expected:
                raise ValueError(f"COMPLETE result identity mismatch: {field}")
    if value["research_only"] is not True or value["broker_execution_enabled"] is not False:
        raise ValueError("journal safety boundary is invalid")
    _utc(value["recorded_at_utc"])
    unsigned = dict(value)
    claimed = unsigned.pop("journal_self_sha256")
    if hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed:
        raise ValueError("journal self hash mismatch")
    if raw != canonical_json(value):
        raise ValueError("journal raw bytes are not canonical JSON")
    if phase == "COMPLETE":
        if state_root is None:
            raise ValueError("COMPLETE journal verification requires StateRoot")
        result_path = state_root / Path(value["result_relative_path"])
        _contained(result_path, state_root)
        result_raw = _read_regular(result_path)
        if hashlib.sha256(result_raw).hexdigest() != value["production_result_sha256"]:
            raise ValueError("production result raw hash mismatch")
        if result_raw != canonical_json(value["result_payload"]):
            raise ValueError("production result raw bytes are not canonical JSON")
    if phase == "COMPENSATED":
        if state_root is None or journal_path is None:
            raise ValueError("COMPENSATED journal verification requires StateRoot and journal path")
        relative = value["compensation_relative_path"]
        compensation_path = state_root / Path(relative)
        _contained(compensation_path, state_root)
        if compensation_path.resolve(strict=False) == journal_path.resolve(strict=False):
            raise ValueError("compensation receipt cannot be the journal")
        compensation_raw = _read_regular(compensation_path)
        if hashlib.sha256(compensation_raw).hexdigest() != value["compensation_sha256"]:
            raise ValueError("compensation receipt raw hash mismatch")
        compensation = validate_compensation(compensation_raw)
        if compensation["candidate_source_sha"] != value["candidate_source_sha"] or compensation[
            "candidate_source_tree"
        ] != value["candidate_source_tree"]:
            raise ValueError("compensation candidate identity mismatch")
        if compensation["candidate_preview_deployment_id"] != value[
            "candidate_preview_deployment_id"
        ]:
            raise ValueError("compensation candidate deployment mismatch")
        if compensation["prior_aliases"] != value["prior_aliases"]:
            raise ValueError("compensation prior aliases mismatch")
        if compensation["promoted_deployment_id"] != value["promoted_deployment_id"]:
            raise ValueError("compensation promoted identity mismatch")
        if compensation["promoted_deployment_url"] != value["promoted_deployment_url"]:
            raise ValueError("compensation promoted URL mismatch")
    return value


def validate_compensation(raw: bytes) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid strict compensation JSON: {exc}") from exc
    if not isinstance(value, dict) or set(value) != COMPENSATION_KEYS:
        raise ValueError("compensation keys are not exact")
    if (
        value["schema_version"] != COMPENSATION_SCHEMA
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
    promoted_id = value["promoted_deployment_id"]
    promoted_url = value["promoted_deployment_url"]
    if (promoted_id is None) != (promoted_url is None):
        raise ValueError("compensation promoted identity is incomplete")
    if promoted_id is not None and (
        not isinstance(promoted_id, str)
        or not promoted_id
        or not isinstance(promoted_url, str)
        or not promoted_url.startswith("https://")
    ):
        raise ValueError("compensation promoted identity is invalid")
    evidence = value["rollback_evidence"]
    if not isinstance(evidence, list) or len(evidence) != len(value["prior_aliases"]):
        raise ValueError("compensation rollback evidence is incomplete")
    for item, prior in zip(evidence, value["prior_aliases"], strict=True):
        _rollback_evidence(item)
        if item["alias"] != prior["alias"]:
            raise ValueError("compensation rollback evidence order is invalid")
        if item["expected_deployment_id"] != prior["deployment_id"]:
            raise ValueError("compensation rollback expected deployment mismatch")
        if item["expected_deployment_url"] != prior["deployment_url"]:
            raise ValueError("compensation rollback expected URL mismatch")
    if value["rollback_status"] != "ROLLED_BACK":
        raise ValueError("compensation rollback status is invalid")
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
    if raw != canonical_json(value):
        raise ValueError("compensation raw bytes are not canonical JSON")
    return value


def _is_reparse(path: Path) -> bool:
    info = path.lstat()
    attributes = getattr(info, "st_file_attributes", 0)
    reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    return stat.S_ISLNK(info.st_mode) or bool(attributes & reparse_flag)


def _read_regular(path: Path) -> bytes:
    before = path.lstat()
    if _is_reparse(path) or not stat.S_ISREG(before.st_mode):
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
    unresolved = Path(os.path.abspath(path))
    resolved = unresolved.resolve(strict=False)
    if not resolved.is_relative_to(root):
        raise ValueError("journal path escapes StateRoot")
    cursor = unresolved.parent
    while cursor != root:
        if cursor == cursor.parent:
            raise ValueError("journal path does not descend from StateRoot")
        if cursor.exists() and _is_reparse(cursor):
            raise ValueError("journal path contains a reparse component")
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


def _process_start_time_utc(pid: int) -> str:
    """Return a process creation identity suitable for stale-lock detection."""

    try:
        import psutil  # type: ignore[import-not-found]

        created = psutil.Process(pid).create_time()
        return (
            datetime.fromtimestamp(created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
    except Exception:
        # Windows' process start API is deliberately avoided here because the
        # lock still fails closed for a live owner; dead-owner adoption only
        # needs a non-empty, strictly typed identity in that fallback case.
        return f"pid:{pid}"


def _process_owner_is_live(pid: int, expected_start_time_utc: str) -> bool:
    """Check a lock owner without sending a Windows console-control event.

    ``os.kill(pid, 0)`` is a POSIX liveness probe, but on Windows signal zero
    is ``CTRL_C_EVENT`` and can interrupt the caller's console process group.
    Prefer psutil's creation-time identity.  If that dependency is unavailable
    on Windows, OpenProcess is used only to distinguish a definitely missing
    PID; any accessible or ambiguous PID fails closed as live because its
    creation identity cannot be proven.
    """

    try:
        import psutil  # type: ignore[import-not-found]

        try:
            created = psutil.Process(pid).create_time()
        except psutil.NoSuchProcess:
            return False
        except (psutil.AccessDenied, OSError):
            return True
        observed = (
            datetime.fromtimestamp(created, tz=timezone.utc)
            .isoformat()
            .replace("+00:00", "Z")
        )
        return observed == expected_start_time_utc
    except ImportError:
        pass

    if os.name == "nt":
        import ctypes

        process_query_limited_information = 0x1000
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.restype = ctypes.c_void_p
        ctypes.set_last_error(0)
        handle = kernel32.OpenProcess(process_query_limited_information, False, pid)
        if handle:
            kernel32.CloseHandle(handle)
            return True
        # ERROR_INVALID_PARAMETER means the PID definitely does not exist.
        # Access-denied and every other error remain fail-closed as live.
        return ctypes.get_last_error() != 87

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return expected_start_time_utc == f"pid:{pid}"


def _validate_lock(value: dict[str, Any]) -> None:
    if set(value) != LOCK_KEYS or value["schema_version"] != LOCK_SCHEMA:
        raise ValueError("publication lock keys or schema are invalid")
    if value["operation"] != "vercel_publication":
        raise ValueError("publication lock operation is invalid")
    for field in ("candidate_source_sha", "candidate_source_tree"):
        _identity(value[field], field)
    if not isinstance(value["owner_id"], str) or not value["owner_id"]:
        raise ValueError("publication lock owner is invalid")
    if type(value["pid"]) is not int or value["pid"] <= 0:
        raise ValueError("publication lock PID is invalid")
    if not isinstance(value["process_start_time_utc"], str) or not value[
        "process_start_time_utc"
    ]:
        raise ValueError("publication lock process start is invalid")
    if not isinstance(value["candidate_market_date"], str) or not re.fullmatch(
        r"\d{4}-\d{2}-\d{2}", value["candidate_market_date"]
    ):
        raise ValueError("publication lock market date is invalid")
    _safe_relative(value["journal_path"])
    _utc(value["acquired_at_utc"])
    unsigned = dict(value)
    claimed = unsigned.pop("lock_self_sha256")
    if not isinstance(claimed, str) or not HEX64.fullmatch(claimed):
        raise ValueError("publication lock self hash is invalid")
    if hashlib.sha256(canonical_json(unsigned)).hexdigest() != claimed:
        raise ValueError("publication lock self hash mismatch")


def _read_lock(path: Path, state_root: Path) -> tuple[dict[str, Any], bytes]:
    _contained(path, state_root)
    raw = _read_regular(path)
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=_pairs)
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"invalid publication lock JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("publication lock is not an object")
    _validate_lock(value)
    if raw != canonical_json(value):
        raise ValueError("publication lock raw bytes are not canonical JSON")
    return value, raw


def acquire_lock(
    path: Path,
    *,
    state_root: Path,
    owner_id: str,
    pid: int | None = None,
    candidate_source_sha: str,
    candidate_source_tree: str,
    candidate_market_date: str,
    journal_path: str,
) -> dict[str, Any]:
    """Atomically acquire the one publication lock, adopting only dead owners."""

    pid = os.getpid() if pid is None else pid
    _identity(candidate_source_sha, "candidate_source_sha")
    _identity(candidate_source_tree, "candidate_source_tree")
    if not isinstance(owner_id, str) or not owner_id:
        raise ValueError("publication lock owner is invalid")
    payload: dict[str, Any] = {
        "schema_version": LOCK_SCHEMA,
        "operation": "vercel_publication",
        "owner_id": owner_id,
        "pid": pid,
        "process_start_time_utc": _process_start_time_utc(pid),
        "candidate_source_sha": candidate_source_sha,
        "candidate_source_tree": candidate_source_tree,
        "candidate_market_date": candidate_market_date,
        "journal_path": journal_path.replace("\\", "/"),
        "acquired_at_utc": datetime.now(timezone.utc)
        .replace(microsecond=0)
        .isoformat()
        .replace("+00:00", "Z"),
    }
    payload["lock_self_sha256"] = hashlib.sha256(canonical_json(payload)).hexdigest()
    raw = canonical_json(payload)
    _validate_lock(payload)
    _contained(path, state_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        _atomic_write(path, raw, exclusive=True)
    except FileExistsError:
        existing, _ = _read_lock(path, state_root)
        owner_is_live = _process_owner_is_live(
            int(existing["pid"]), str(existing["process_start_time_utc"])
        )
        if owner_is_live:
            raise ValueError("publication lock is held by a live owner") from None
        # Rename the stale owner first, then retry exclusive creation. A
        # concurrent adopter can win; either outcome remains fail-closed.
        stale = path.with_name(
            f"{path.name}.stale-{os.getpid()}-{os.urandom(8).hex()}"
        )
        try:
            os.replace(path, stale)
        except OSError as exc:
            raise ValueError("publication lock owner is stale but adoption raced") from exc
        try:
            try:
                _atomic_write(path, raw, exclusive=True)
            except FileExistsError as exc:
                raise ValueError("publication lock is held by another owner") from exc
        finally:
            try:
                stale.unlink()
            except OSError:
                pass
    return payload


def release_lock(
    path: Path,
    *,
    state_root: Path,
    owner_id: str,
    pid: int | None = None,
) -> None:
    pid = os.getpid() if pid is None else pid
    existing, raw = _read_lock(path, state_root)
    if existing["owner_id"] != owner_id or existing["pid"] != pid:
        raise ValueError("publication lock owner binding mismatch")
    if existing["process_start_time_utc"] != _process_start_time_utc(pid):
        raise ValueError("publication lock process identity mismatch")
    # Re-read immediately before the atomic move, then remove only the moved
    # identity. A new owner may acquire ``path`` after the move, but this
    # releaser never unlinks that new owner's path.
    current = _read_regular(path)
    if current != raw:
        raise ValueError("publication lock changed before release")
    released = path.with_name(f"{path.name}.released-{pid}-{os.urandom(8).hex()}")
    _contained(released, state_root)
    try:
        os.replace(path, released)
    except OSError as exc:
        raise ValueError("publication lock could not be atomically released") from exc
    moved = _read_regular(released)
    if moved != raw:
        raise ValueError("publication lock identity changed during release")
    released.unlink()


def seal(source: Path, target: Path, *, state_root: Path | None = None) -> dict[str, Any]:
    value = json.loads(_read_regular(source).decode("utf-8"), object_pairs_hook=_pairs)
    input_keys = set(value) if isinstance(value, dict) else None
    if input_keys not in (
        KEYS - {"journal_self_sha256"},
        AUTHORIZATION_KEYS - {"journal_self_sha256"},
    ):
        raise ValueError("journal input keys are not exact")
    value["journal_self_sha256"] = hashlib.sha256(canonical_json(value)).hexdigest()
    raw = canonical_json(value)
    validate(raw, state_root=state_root, journal_path=target)
    _atomic_write(target, raw)
    return {"payload": value, "raw_file_sha256": hashlib.sha256(raw).hexdigest()}


def transition(
    source: Path,
    target: Path,
    previous: Path,
    *,
    state_root: Path | None = None,
) -> dict[str, Any]:
    value = json.loads(_read_regular(source).decode("utf-8"), object_pairs_hook=_pairs)
    input_keys = set(value) if isinstance(value, dict) else None
    if input_keys not in (
        KEYS - {"journal_self_sha256"},
        AUTHORIZATION_KEYS - {"journal_self_sha256"},
    ):
        raise ValueError("journal transition keys are not exact")
    prior_raw = _read_regular(previous)
    prior = validate(prior_raw, state_root=state_root, journal_path=previous)
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
        for key in AUTHORIZATION_KEYS
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
        if value.get(key) != prior.get(key):
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
    return seal(source, target, state_root=state_root)


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
    lock_parser = subs.add_parser("acquire-lock")
    lock_parser.add_argument("path", type=Path)
    lock_parser.add_argument("--state-root", required=True, type=Path)
    lock_parser.add_argument("--owner-id", required=True)
    lock_parser.add_argument("--pid", required=True, type=int)
    lock_parser.add_argument("--candidate-source-sha", required=True)
    lock_parser.add_argument("--candidate-source-tree", required=True)
    lock_parser.add_argument("--candidate-market-date", required=True)
    lock_parser.add_argument("--journal-path", required=True)
    release_parser = subs.add_parser("release-lock")
    release_parser.add_argument("path", type=Path)
    release_parser.add_argument("--state-root", required=True, type=Path)
    release_parser.add_argument("--owner-id", required=True)
    release_parser.add_argument("--pid", required=True, type=int)
    args = parser.parse_args()
    result: dict[str, Any]
    if args.command == "verify":
        _contained(args.path, args.state_root)
        raw = _read_regular(args.path)
        result = {
            "payload": validate(raw, state_root=args.state_root, journal_path=args.path),
            "raw_file_sha256": hashlib.sha256(raw).hexdigest(),
        }
    elif args.command == "verify-compensation":
        _contained(args.path, args.state_root)
        raw = _read_regular(args.path)
        result = {
            "payload": validate_compensation(raw),
            "raw_file_sha256": hashlib.sha256(raw).hexdigest(),
        }
    elif args.command == "acquire-lock":
        result = {
            "payload": acquire_lock(
                args.path,
                state_root=args.state_root,
                owner_id=args.owner_id,
                pid=args.pid,
                candidate_source_sha=args.candidate_source_sha,
                candidate_source_tree=args.candidate_source_tree,
                candidate_market_date=args.candidate_market_date,
                journal_path=args.journal_path,
            )
        }
    elif args.command == "release-lock":
        release_lock(
            args.path,
            state_root=args.state_root,
            owner_id=args.owner_id,
            pid=args.pid,
        )
        result = {"released": True}
    elif args.command == "seal":
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        result = seal(args.input, args.output, state_root=args.state_root)
    else:
        _contained(args.input, args.state_root)
        _contained(args.output, args.state_root)
        _contained(args.previous, args.state_root)
        result = transition(args.input, args.output, args.previous, state_root=args.state_root)
    print(json.dumps(result, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
