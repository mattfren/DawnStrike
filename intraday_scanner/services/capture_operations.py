"""Fail-closed planning and identity validation for intraday capture operations.

This module is deliberately provider-read-only.  It validates the durable
inputs used by the capture runner before any network request is made.  The
validated plan is also the contract consumed by the task-registration preview.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path, PureWindowsPath
from typing import Any, BinaryIO
from zoneinfo import ZoneInfo

from intraday_scanner.market_calendar import canonical_regular_session_id, market_session

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MODES = {"forward_observed", "retrospective_research"}
_APPROVED_WINDOWS_GIT = Path(r"C:\Program Files\Git\cmd\git.exe")
_APPROVED_WINDOWS_GIT_SHA256 = "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"


class CapturePlanError(ValueError):
    """A capture plan is incomplete, stale, or violates an isolation rule."""


@dataclass(frozen=True)
class CapturePlan:
    mode: str
    provider: str
    feed: str
    candidate_sha: str
    repo_root: Path
    db_path: Path
    evidence_root: Path
    run_root: Path
    output_root: Path
    symbols_manifest: Path
    symbols_manifest_sha256: str
    expected_session: Path
    expected_session_sha256: str
    entitlement_receipt: Path
    entitlement_receipt_sha256: str
    source_config: Path
    source_config_sha256: str
    env_file: Path
    max_pages: int = 100
    retries: int = 3

    def validate(self, *, now: datetime | None = None) -> dict[str, Any]:
        """Validate a preview while binding every authoritative JSON read once."""

        with self.admit(now=now) as admission:
            return dict(admission.prepared)

    def admit(self, *, now: datetime | None = None) -> CapturePlanAdmission:
        """Capture and retain the exact authority inputs for one operation."""

        held: list[tuple[Path, BinaryIO, tuple[int, ...]]] = []
        try:
            prepared, expected_session, entitlement = self._validate_admitted(
                now=now,
                held=held,
            )
            return CapturePlanAdmission(
                plan=self,
                prepared=prepared,
                expected_session=expected_session,
                entitlement=entitlement,
                held=held,
            )
        except Exception:
            _close_identity_locked_files(held)
            raise

    def _validate_admitted(
        self,
        *,
        now: datetime | None,
        held: list[tuple[Path, BinaryIO, tuple[int, ...]]],
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        now_utc = _utc(now or datetime.now(UTC))
        if self.mode not in _MODES:
            raise CapturePlanError("mode must be forward_observed or retrospective_research")
        if self.provider != "alpaca":
            raise CapturePlanError("capture operations require the read-only Alpaca provider")
        if self.feed != "sip":
            raise CapturePlanError("feed must be exactly sip; feed substitution is forbidden")
        if not _GIT_OID.fullmatch(self.candidate_sha):
            raise CapturePlanError("candidate_sha must be an exact lowercase Git object id")
        if not _SHA256.fullmatch(self.symbols_manifest_sha256):
            raise CapturePlanError("symbols_manifest_sha256 must be a lowercase SHA-256")
        if not _SHA256.fullmatch(self.expected_session_sha256):
            raise CapturePlanError("expected_session_sha256 must be a lowercase SHA-256")
        if not _SHA256.fullmatch(self.entitlement_receipt_sha256):
            raise CapturePlanError("entitlement_receipt_sha256 must be a lowercase SHA-256")
        if not _SHA256.fullmatch(self.source_config_sha256):
            raise CapturePlanError("source_config_sha256 must be a lowercase SHA-256")
        if not 1 <= self.max_pages <= 1000:
            raise CapturePlanError("max_pages must be between 1 and 1000")
        if not 1 <= self.retries <= 10:
            raise CapturePlanError("retries must be between 1 and 10")

        repo = _resolve_directory(self.repo_root, "repo_root")
        regular_files = {
            "symbols_manifest": _resolve_regular_file(self.symbols_manifest, "symbols_manifest"),
            "expected_session": _resolve_regular_file(self.expected_session, "expected_session"),
            "entitlement_receipt": _resolve_regular_file(
                self.entitlement_receipt, "entitlement_receipt"
            ),
            "env_file": _resolve_regular_file(self.env_file, "env_file"),
        }
        source_config = _resolve_regular_file(self.source_config, "source_config")

        for label, raw_path in {
            "db_path": self.db_path,
            "evidence_root": self.evidence_root,
            "run_root": self.run_root,
            "output_root": self.output_root,
        }.items():
            if _under_windows_path(raw_path, r"C:\r\dawnstrike-runtime"):
                raise CapturePlanError(f"{label} must not be under the active runtime")
            if _under_windows_path(raw_path, r"C:\r\dawnstrike-state"):
                raise CapturePlanError(f"{label} must not be under the active state")

        db = self.db_path.resolve(strict=False)
        evidence = self.evidence_root.resolve(strict=False)
        runs = self.run_root.resolve(strict=False)
        output = self.output_root.resolve(strict=False)
        for label, path in {
            "db_path": db,
            "evidence_root": evidence,
            "run_root": runs,
            "output_root": output,
        }.items():
            if _under(path, repo):
                raise CapturePlanError(f"{label} must not be under the repository")
            if _under(path, Path(r"C:\r\dawnstrike-runtime")):
                raise CapturePlanError(f"{label} must not be under the active runtime")
            if _under(path, Path(r"C:\r\dawnstrike-state")):
                raise CapturePlanError(f"{label} must not be under the active state")
        roots = {
            "database root": db.parent,
            "evidence root": evidence,
            "run root": runs,
            "output root": output,
        }
        for left_name, left in roots.items():
            for right_name, right in roots.items():
                if left_name >= right_name:
                    continue
                if _under(left, right) or _under(right, left):
                    raise CapturePlanError(
                        f"{left_name} and {right_name} must be separate non-overlapping roots"
                    )
        mode_evidence = evidence / self.mode
        mode_runs = runs / self.mode
        mode_output = output / self.mode
        for label, path in {
            "mode evidence root": mode_evidence,
            "mode run root": mode_runs,
            "mode output root": mode_output,
        }.items():
            if _under(path, repo):
                raise CapturePlanError(f"{label} must not be under the repository")

        current_sha, current_tree_sha, git_executable_sha256 = _git_identity(repo)
        if current_sha != self.candidate_sha:
            raise CapturePlanError(
                f"candidate SHA mismatch: expected {self.candidate_sha}, running {current_sha}"
            )
        manifest_raw = _read_identity_locked_bytes(
            regular_files["symbols_manifest"],
            label="symbols manifest",
            max_bytes=4 * 1024 * 1024,
            held=held,
        )
        manifest = _read_json_bytes(manifest_raw, "symbols manifest")
        symbols = _validate_manifest(manifest)
        if hashlib.sha256(manifest_raw).hexdigest() != self.symbols_manifest_sha256:
            raise CapturePlanError("symbols manifest hash mismatch")
        session_raw = _read_identity_locked_bytes(
            regular_files["expected_session"],
            label="expected session",
            max_bytes=1024 * 1024,
            held=held,
        )
        if hashlib.sha256(session_raw).hexdigest() != self.expected_session_sha256:
            raise CapturePlanError("expected session hash mismatch")
        session = _read_json_bytes(session_raw, "expected session")
        start, end, session_identity = _validate_session(session)
        if end > now_utc - timedelta(minutes=15):
            raise CapturePlanError(
                "capture end must be at least 15 minutes old for delayed SIP evidence"
            )
        if start >= end:
            raise CapturePlanError("expected session window must have positive duration")
        entitlement_raw = _read_identity_locked_bytes(
            regular_files["entitlement_receipt"],
            label="entitlement receipt",
            max_bytes=1024 * 1024,
            held=held,
        )
        entitlement = _read_json_bytes(entitlement_raw, "entitlement receipt")
        if hashlib.sha256(entitlement_raw).hexdigest() != self.entitlement_receipt_sha256:
            raise CapturePlanError("entitlement receipt hash mismatch")
        entitlement_name = str(entitlement.get("entitlement") or "").strip()
        proof_id = str(entitlement.get("receipt") or entitlement.get("proof_id") or "").strip()
        if not entitlement_name or not proof_id:
            raise CapturePlanError("entitlement receipt requires entitlement and receipt/proof_id")
        if entitlement.get("provider") != "alpaca" or entitlement.get("feed") != "sip":
            raise CapturePlanError("entitlement receipt provider/feed identity mismatch")
        if entitlement.get("probe_status") != "PASS":
            raise CapturePlanError("entitlement receipt does not contain a passing endpoint probe")
        proven_endpoints = set(entitlement.get("proven_endpoints") or [])
        if not {"bars", "trades", "quotes"}.issubset(proven_endpoints):
            raise CapturePlanError("entitlement receipt is missing proven market-data endpoints")
        if entitlement.get("retention_allowed") is not True:
            raise CapturePlanError("entitlement receipt does not permit private retention")
        if entitlement.get("approved_plan") is not True:
            raise CapturePlanError("entitlement receipt is not operator-approved")
        if entitlement.get("research_only") is not True:
            raise CapturePlanError("entitlement receipt is not research-only")
        if entitlement.get("broker_execution") != "disabled":
            raise CapturePlanError("entitlement receipt does not disable broker execution")
        source_config_raw = _read_identity_locked_bytes(
            source_config,
            label="source config",
            max_bytes=4 * 1024 * 1024,
            held=held,
        )
        if hashlib.sha256(source_config_raw).hexdigest() != self.source_config_sha256:
            raise CapturePlanError("source config hash mismatch")

        prepared = {
            "schema_version": "dawnstrike.capture_operation_plan.v1",
            "status": "READY",
            "mode": self.mode,
            "provider": self.provider,
            "feed": self.feed,
            "candidate_sha": self.candidate_sha,
            "candidate_tree_sha": current_tree_sha,
            "candidate_worktree_clean": True,
            "git_executable_sha256": git_executable_sha256,
            "symbols": symbols,
            "symbols_manifest_sha256": self.symbols_manifest_sha256,
            "market_date": session_identity["market_date"],
            "exchange_session_id": session_identity["exchange_session_id"],
            "request_start": start.isoformat(),
            "request_end": end.isoformat(),
            "expected_session_sha256": self.expected_session_sha256,
            "entitlement": entitlement_name,
            "entitlement_receipt_sha256": self.entitlement_receipt_sha256,
            "source_config_sha256": self.source_config_sha256,
            "mode_evidence_root": str(mode_evidence),
            "mode_run_root": str(mode_runs),
            "mode_output_root": str(mode_output),
            "db_path": str(db),
            "bounded_pages": self.max_pages,
            "bounded_retries": self.retries,
            "required_endpoints": ["bars", "trades", "quotes", "corporate_actions"],
            "full_microstructure_requested": True,
            "historical_membership_policy": str(manifest["membership_policy"]),
            "research_only": True,
            "broker_execution": "disabled",
        }
        return prepared, session, entitlement


@dataclass
class CapturePlanAdmission:
    """One exact, retained authority snapshot for a capture operation."""

    plan: CapturePlan
    prepared: dict[str, Any]
    expected_session: dict[str, Any]
    entitlement: dict[str, Any]
    held: list[tuple[Path, BinaryIO, tuple[int, ...]]]
    closed: bool = False

    def __enter__(self) -> CapturePlanAdmission:
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        try:
            if exc_type is None:
                self.assert_unchanged()
        finally:
            self.close()

    def assert_unchanged(self) -> None:
        if self.closed:
            raise CapturePlanError("capture plan admission is already closed")
        _assert_identity_locked_files_unchanged(self.held)

    def close(self) -> None:
        if not self.closed:
            _close_identity_locked_files(self.held)
            self.closed = True

    def as_dict(self) -> dict[str, Any]:
        self.assert_unchanged()
        result = dict(self.prepared)
        result["plan_identity_sha256"] = hashlib.sha256(
            json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return result

    def hold_exact_bytes(
        self,
        path: Path,
        *,
        expected: bytes,
        label: str,
        max_bytes: int,
    ) -> str:
        """Add a derived child input to this admission without a reopen gap."""

        self.assert_unchanged()
        raw = _read_identity_locked_bytes(
            _resolve_regular_file(path, label),
            label=label,
            max_bytes=max_bytes,
            held=self.held,
        )
        if raw != expected:
            raise CapturePlanError(f"{label} identity conflicts with admitted bytes")
        return hashlib.sha256(raw).hexdigest()

    def sanitized_entitlement_metadata(self, *, receipt_hash: str) -> dict[str, str]:
        """Return sanitized metadata from the exact admitted entitlement bytes."""

        self.assert_unchanged()
        if receipt_hash != self.plan.entitlement_receipt_sha256:
            raise CapturePlanError("entitlement receipt hash is not the admitted identity")
        entitlement = str(self.entitlement.get("entitlement") or "").strip()
        if not entitlement:
            raise CapturePlanError("entitlement receipt requires entitlement")
        return {
            "entitlement": entitlement,
            "receipt": receipt_hash,
            "proof_id": receipt_hash,
            "receipt_file_sha256": receipt_hash,
            "provider": "alpaca",
            "feed": "sip",
            "retention_allowed": "true",
            "research_only": "true",
            "broker_execution": "disabled",
        }


def _validate_manifest(value: dict[str, Any]) -> list[str]:
    if str(value.get("membership_policy") or "").strip() == "":
        raise CapturePlanError("symbol manifest requires membership_policy")
    if value.get("point_in_time_membership") not in {False, "research_control_only", "not_claimed"}:
        raise CapturePlanError("symbol manifest must not claim fabricated point-in-time membership")
    raw = value.get("symbols")
    if not isinstance(raw, list) or not raw:
        raise CapturePlanError("symbol manifest symbols must be a non-empty list")
    symbols: list[str] = []
    for item in raw:
        symbol = str(item).strip().upper()
        if not symbol or symbol != str(item).strip() or symbol in symbols:
            raise CapturePlanError("symbol manifest symbols must be unique uppercase identifiers")
        symbols.append(symbol)
    return symbols


def _validate_session(value: dict[str, Any]) -> tuple[datetime, datetime, dict[str, str]]:
    market_date = str(value.get("market_date") or "")
    exchange = str(value.get("exchange") or "").upper()
    session_id = str(value.get("exchange_session_id") or "")
    if exchange not in {"XNYS", "NYSE"} or not market_date or not session_id:
        raise CapturePlanError(
            "expected session requires exchange, market_date, and exchange_session_id"
        )
    if market_date not in session_id:
        raise CapturePlanError("exchange_session_id does not identify market_date")
    canonical_session_id = canonical_regular_session_id(market_date)
    if session_id != canonical_session_id:
        raise CapturePlanError(
            "expected session must use the full canonical regular-session identity"
        )
    try:
        decision = market_session(datetime.fromisoformat(market_date).date())
    except (ValueError, KeyError) as exc:
        raise CapturePlanError(
            "expected session market_date is outside the governed calendar"
        ) from exc
    if not decision.is_trading_day:
        raise CapturePlanError("expected session is not an open NYSE market session")
    if value.get("calendar_id") and value["calendar_id"] != decision.calendar_id:
        raise CapturePlanError("expected session calendar identity mismatch")
    session_start = _parse_utc(value.get("start_utc") or value.get("request_start"), "start_utc")
    session_end = _parse_utc(value.get("end_utc") or value.get("request_end"), "end_utc")
    start = _parse_utc(
        value.get("capture_start_utc") or session_start.isoformat(),
        "capture_start_utc",
    )
    end = _parse_utc(
        value.get("capture_end_utc") or session_end.isoformat(),
        "capture_end_utc",
    )
    market_zone = ZoneInfo("America/New_York")
    start_date = start.astimezone(market_zone).date().isoformat()
    end_date = end.astimezone(market_zone).date().isoformat()
    if start_date != market_date or end_date != market_date:
        raise CapturePlanError("expected session window does not belong to market_date")
    if session_start >= session_end or start < session_start or end > session_end:
        raise CapturePlanError("capture window must be bounded by the full expected session")
    return start, end, {"market_date": market_date, "exchange_session_id": session_id}


def _resolve_directory(path: Path, label: str) -> Path:
    resolved = path.resolve(strict=False)
    if resolved.exists() and not resolved.is_dir():
        raise CapturePlanError(f"{label} must be a directory")
    return resolved


def _resolve_regular_file(path: Path, label: str) -> Path:
    if path.is_symlink() or not path.is_file():
        raise CapturePlanError(f"{label} must be a regular file")
    return path.resolve()


def _read_json_bytes(raw: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(raw.decode("utf-8", "strict"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CapturePlanError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise CapturePlanError(f"{label} must be a JSON object")
    return value


def _read_identity_locked_bytes(
    path: Path,
    *,
    label: str,
    max_bytes: int,
    held: list[tuple[Path, BinaryIO, tuple[int, ...]]],
) -> bytes:
    """Read one exact regular file and retain its deny-write/delete handle."""

    from scripts.dawnstrike_python_bootstrap import _read_locked_exact_file

    handles: list[BinaryIO] = []
    try:
        raw = _read_locked_exact_file(path, handles)
        if len(raw) > max_bytes:
            raise CapturePlanError(f"{label} exceeds its byte ceiling")
        handle = handles[0]
        details = os.fstat(handle.fileno())
        snapshot: tuple[int, ...] = (
            int(details.st_dev),
            int(details.st_ino),
            int(details.st_size),
            int(details.st_mtime_ns),
        )
        if os.name != "nt":
            snapshot += (int(details.st_ctime_ns),)
        held.append((path, handle, snapshot))
        handles.clear()
        return raw
    except CapturePlanError:
        raise
    except (OSError, RuntimeError) as exc:
        raise CapturePlanError(f"{label} is unavailable") from exc
    finally:
        for handle in handles:
            handle.close()


def _assert_identity_locked_files_unchanged(
    held: list[tuple[Path, BinaryIO, tuple[int, ...]]],
) -> None:
    for path, handle, admitted in held:
        try:
            opened = os.fstat(handle.fileno())
            current = path.lstat()
        except OSError as exc:
            raise CapturePlanError("capture authority changed during execution") from exc
        fields: tuple[str, ...] = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
        if os.name != "nt":
            fields += ("st_ctime_ns",)
        opened_snapshot = tuple(int(getattr(opened, field)) for field in fields)
        current_snapshot = tuple(int(getattr(current, field)) for field in fields)
        if opened_snapshot != admitted or current_snapshot != admitted:
            raise CapturePlanError("capture authority changed during execution")


def _close_identity_locked_files(
    held: list[tuple[Path, BinaryIO, tuple[int, ...]]],
) -> None:
    for _path, handle, _snapshot in held:
        handle.close()
    held.clear()


def _parse_utc(value: Any, label: str) -> datetime:
    if not isinstance(value, str):
        raise CapturePlanError(f"{label} must be a timezone-aware UTC ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise CapturePlanError(f"{label} is invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() != UTC.utcoffset(parsed):
        raise CapturePlanError(f"{label} must be UTC")
    return parsed.astimezone(UTC)


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise CapturePlanError("now must be timezone-aware")
    return value.astimezone(UTC)


def _under(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


def _under_windows_path(path: Path, parent: str) -> bool:
    """Compare fixed Windows safety roots independently of the CI host OS."""

    try:
        PureWindowsPath(str(path)).relative_to(PureWindowsPath(parent))
        return True
    except ValueError:
        return False


def _approved_git_executable() -> tuple[Path, str]:
    if os.name != "nt":
        discovered = shutil.which("git")
        if not discovered:
            raise CapturePlanError("approved Git executable is unavailable")
        path = Path(discovered).resolve(strict=True)
        return path, _sha256_file(path)
    path = _APPROVED_WINDOWS_GIT
    try:
        cursor = path
        while True:
            details = cursor.lstat()
            if getattr(details, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT:
                raise CapturePlanError("approved Git executable path contains a reparse point")
            if cursor.parent == cursor:
                break
            cursor = cursor.parent
        digest = _sha256_file(path)
    except OSError as exc:
        raise CapturePlanError("approved Git executable is unavailable") from exc
    if digest != _APPROVED_WINDOWS_GIT_SHA256:
        raise CapturePlanError("approved Git executable hash changed")
    return path, digest


def _governed_git_environment(repo: Path) -> dict[str, str]:
    from intraday_scanner.approved_tools import sanitized_git_environment

    return sanitized_git_environment(repo)


def _git_identity(repo: Path) -> tuple[str, str, str]:
    from intraday_scanner.approved_tools import admitted_git_contract

    try:
        admitted = admitted_git_contract(repo)
    except (OSError, RuntimeError, TypeError, ValueError) as exc:
        raise CapturePlanError("admitted Git identity is unavailable") from exc
    if admitted is not None:
        return (
            str(admitted["candidate_sha"]),
            str(admitted["candidate_tree"]),
            str(admitted["git_executable_sha256"]),
        )
    try:
        git_path, git_sha256 = _approved_git_executable()
        environment = _governed_git_environment(repo)
        head = subprocess.run(
            [
                str(git_path),
                "-c",
                "core.autocrlf=true",
                "-C",
                str(repo),
                "rev-parse",
                "HEAD",
            ],
            cwd=repo,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            [
                str(git_path),
                "-c",
                "core.autocrlf=true",
                "-C",
                str(repo),
                "rev-parse",
                "HEAD^{tree}",
            ],
            cwd=repo,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            [
                str(git_path),
                "-c",
                "core.autocrlf=true",
                "-C",
                str(repo),
                "status",
                "--porcelain",
                "--untracked-files=all",
            ],
            cwd=repo,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if dirty.strip():
            raise CapturePlanError("candidate repository worktree is not clean")
        if not _GIT_OID.fullmatch(head) or not _GIT_OID.fullmatch(tree):
            raise CapturePlanError("candidate repository identity is invalid")
        return head, tree, git_sha256
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CapturePlanError("candidate repository SHA is unavailable") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_as_dict(plan: CapturePlan, *, now: datetime | None = None) -> dict[str, Any]:
    with plan.admit(now=now) as admission:
        return admission.as_dict()


__all__ = ["CapturePlan", "CapturePlanAdmission", "CapturePlanError", "plan_as_dict"]
