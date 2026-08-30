"""Fail-closed planning and identity validation for intraday capture operations.

This module is deliberately provider-read-only.  It validates the durable
inputs used by the capture runner before any network request is made.  The
validated plan is also the contract consumed by the task-registration preview.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.market_calendar import market_session

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GIT_OID = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_MODES = {"forward_observed", "retrospective_research"}


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
    entitlement_receipt: Path
    entitlement_receipt_sha256: str
    source_config: Path
    source_config_sha256: str
    env_file: Path
    max_pages: int = 100
    retries: int = 3

    def validate(self, *, now: datetime | None = None) -> dict[str, Any]:
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

        current_sha, current_tree_sha = _git_identity(repo)
        if current_sha != self.candidate_sha:
            raise CapturePlanError(
                f"candidate SHA mismatch: expected {self.candidate_sha}, running {current_sha}"
            )
        manifest = _read_json(regular_files["symbols_manifest"], "symbols manifest")
        symbols = _validate_manifest(manifest)
        if _sha256_file(regular_files["symbols_manifest"]) != self.symbols_manifest_sha256:
            raise CapturePlanError("symbols manifest hash mismatch")
        session = _read_json(regular_files["expected_session"], "expected session")
        start, end, session_identity = _validate_session(session)
        if end > now_utc - timedelta(minutes=15):
            raise CapturePlanError(
                "capture end must be at least 15 minutes old for delayed SIP evidence"
            )
        if start >= end:
            raise CapturePlanError("expected session window must have positive duration")
        entitlement = _read_json(regular_files["entitlement_receipt"], "entitlement receipt")
        if _sha256_file(regular_files["entitlement_receipt"]) != self.entitlement_receipt_sha256:
            raise CapturePlanError("entitlement receipt hash mismatch")
        entitlement_name = str(entitlement.get("entitlement") or "").strip()
        proof_id = str(entitlement.get("receipt") or entitlement.get("proof_id") or "").strip()
        if not entitlement_name or not proof_id:
            raise CapturePlanError("entitlement receipt requires entitlement and receipt/proof_id")
        if source_config is not None and _sha256_file(source_config) != self.source_config_sha256:
            raise CapturePlanError("source config hash mismatch")

        return {
            "schema_version": "dawnstrike.capture_operation_plan.v1",
            "status": "READY",
            "mode": self.mode,
            "provider": self.provider,
            "feed": self.feed,
            "candidate_sha": self.candidate_sha,
            "candidate_tree_sha": current_tree_sha,
            "candidate_worktree_clean": True,
            "symbols": symbols,
            "symbols_manifest_sha256": self.symbols_manifest_sha256,
            "market_date": session_identity["market_date"],
            "exchange_session_id": session_identity["exchange_session_id"],
            "request_start": start.isoformat(),
            "request_end": end.isoformat(),
            "expected_session_sha256": _sha256_file(regular_files["expected_session"]),
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

    def sanitized_entitlement_metadata(self, *, receipt_hash: str) -> dict[str, str]:
        """Return the only entitlement fields permitted into capture artifacts."""

        source = _read_json(
            _resolve_regular_file(self.entitlement_receipt, "entitlement_receipt"),
            "entitlement receipt",
        )
        entitlement = str(source.get("entitlement") or "").strip()
        if not entitlement:
            raise CapturePlanError("entitlement receipt requires entitlement")
        return {
            "entitlement": entitlement,
            "receipt": receipt_hash,
            "proof_id": receipt_hash,
            "receipt_file_sha256": receipt_hash,
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
    start = _parse_utc(value.get("start_utc") or value.get("request_start"), "start_utc")
    end = _parse_utc(value.get("end_utc") or value.get("request_end"), "end_utc")
    market_zone = ZoneInfo("America/New_York")
    start_date = start.astimezone(market_zone).date().isoformat()
    end_date = end.astimezone(market_zone).date().isoformat()
    if start_date != market_date or end_date != market_date:
        raise CapturePlanError("expected session window does not belong to market_date")
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


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CapturePlanError(f"{label} is unreadable JSON") from exc
    if not isinstance(value, dict):
        raise CapturePlanError(f"{label} must be a JSON object")
    return value


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


def _git_identity(repo: Path) -> tuple[str, str]:
    try:
        head = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        tree = subprocess.run(
            ["git", "rev-parse", "HEAD^{tree}"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        if dirty.strip():
            raise CapturePlanError("candidate repository worktree is not clean")
        if not _GIT_OID.fullmatch(head) or not _GIT_OID.fullmatch(tree):
            raise CapturePlanError("candidate repository identity is invalid")
        return head, tree
    except (OSError, subprocess.CalledProcessError) as exc:
        raise CapturePlanError("candidate repository SHA is unavailable") from exc


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def plan_as_dict(plan: CapturePlan, *, now: datetime | None = None) -> dict[str, Any]:
    result = plan.validate(now=now)
    result["plan_identity_sha256"] = hashlib.sha256(
        json.dumps(result, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return result


__all__ = ["CapturePlan", "CapturePlanError", "plan_as_dict"]
