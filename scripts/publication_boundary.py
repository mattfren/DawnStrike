"""Canonical market/date boundary for unattended daily publication.

The scheduled publisher is allowed to finalize only the currently due,
already-closed exchange session.  LocalOnly callers intentionally remain an
offline/replay escape hatch and therefore do not consult the wall clock.
This module is read-only; the only clock seam is the explicit ``now`` argument
used by tests (the CLI protects its equivalent behind a test-only environment
flag).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Mapping
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.market_calendar import (  # noqa: E402
    EARLY_CLOSE_ET,
    MARKET_TIMEZONE,
    REGULAR_CLOSE_ET,
    MarketSessionDecision,
    MarketSessionStatus,
    market_session,
)

SCHEMA = "dawnstrike.publication_boundary.v1"
AUTHORIZATION_SCHEMA = "dawnstrike.prepublication_authorization.v1"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
NON_LOCAL_MODES = frozenset({"Preview", "Production"})


def current_due_market_session(now: datetime | None = None) -> dict[str, Any]:
    """Resolve the one session that an unattended finalize may publish now."""

    observed = now or datetime.now(UTC)
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise ValueError("publication clock must include a timezone")
    local = observed.astimezone(MARKET_TIMEZONE)
    try:
        session = market_session(local.date())
    except Exception as exc:
        return _blocked("calendar_unavailable", local, error=type(exc).__name__)
    if not session.is_trading_day:
        return _blocked("market_closed", local, session=session)
    close = (
        EARLY_CLOSE_ET
        if session.status == MarketSessionStatus.EARLY_CLOSE
        else REGULAR_CLOSE_ET
    )
    due = datetime.combine(local.date(), close, tzinfo=MARKET_TIMEZONE)
    if local < due:
        return _blocked("session_not_due", local, session=session, due=due)
    return {
        "schema_version": SCHEMA,
        "status": "PASS",
        "ready": True,
        "market_date": session.market_date,
        "expected_market_date": session.market_date,
        "session": session.to_dict(),
        "due_at": due.astimezone(UTC).isoformat(),
        "observed_at": observed.astimezone(UTC).isoformat(),
        "timezone": str(MARKET_TIMEZONE),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def authorize_market_date(
    requested_market_date: str,
    *,
    publication_mode: str,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Authorize a requested finalize date under its publication mode."""

    requested = str(requested_market_date).strip()
    mode = str(publication_mode).strip()
    errors: list[str] = []
    parsed: date | None = None
    session: MarketSessionDecision | None = None
    if not DATE_RE.fullmatch(requested):
        errors.append("market_date_invalid")
    else:
        try:
            parsed = date.fromisoformat(requested)
        except ValueError:
            errors.append("market_date_invalid")
        if parsed is not None and parsed.isoformat() != requested:
            errors.append("market_date_invalid")
    if mode not in {"LocalOnly", *NON_LOCAL_MODES}:
        errors.append("publication_mode_invalid")

    # LocalOnly is explicitly used for historical replay and offline repair.
    # It still validates an ISO date and covered calendar row, but never
    # promotes or uses the wall clock as a source of truth.
    if mode == "LocalOnly" and parsed is not None and not errors:
        try:
            session = market_session(parsed)
        except Exception as exc:
            errors.append(f"calendar_unavailable:{type(exc).__name__}")
        else:
            return _authorization_result(
                requested, mode, session, errors, boundary=None, offline=True
            )

    boundary = current_due_market_session(now)
    expected = str(boundary.get("expected_market_date") or "")
    if boundary.get("ready") is not True:
        errors.append(str(boundary.get("reason") or "market_boundary_blocked"))
    if expected and requested != expected:
        errors.append("requested_market_date_not_current_due")
    session = None
    if parsed is not None and not any(item == "market_date_invalid" for item in errors):
        try:
            session = market_session(parsed)
        except Exception as exc:
            errors.append(f"calendar_unavailable:{type(exc).__name__}")
        else:
            if not session.is_trading_day:
                errors.append("requested_market_date_closed")
    return _authorization_result(requested, mode, session, errors, boundary=boundary)


def prepublication_authorization_id(
    *,
    expected_market_date: str,
    release_sha: str,
    run_id: str,
    stage_statuses: Mapping[str, Mapping[str, Any]],
    artifact_identity: Mapping[str, Any],
) -> str:
    """Derive an immutable identity from the daily ledger and artifact bytes."""

    payload = {
        "schema_version": AUTHORIZATION_SCHEMA,
        "expected_market_date": expected_market_date,
        "release_sha": release_sha,
        "run_id": run_id,
        "stage_statuses": {
            str(stage): {
                key: value
                for key, value in item.items()
                if key
                in {
                    "status",
                    "attempt_no",
                    "completed_at",
                    "input_hash_sha256",
                    "output_hash_sha256",
                }
            }
            for stage, item in sorted(stage_statuses.items())
        },
        "artifact_identity": dict(sorted(artifact_identity.items())),
    }
    return hashlib.sha256(_canonical(payload)).hexdigest()


def validate_production_lineage(
    current_manifest: Mapping[str, Any], candidate_manifest: Mapping[str, Any]
) -> dict[str, Any]:
    """Check that a candidate cannot move production backwards or fork today."""

    errors: list[str] = []
    current_text = str(current_manifest.get("market_date") or "")
    candidate_text = str(candidate_manifest.get("market_date") or "")
    try:
        current = date.fromisoformat(current_text)
        candidate = date.fromisoformat(candidate_text)
    except ValueError:
        errors.append("production_lineage_market_date_invalid")
        return {"ready": False, "status": "BLOCKED", "errors": errors}
    if current.isoformat() != current_text or candidate.isoformat() != candidate_text:
        errors.append("production_lineage_market_date_invalid")
    elif candidate < current:
        errors.append("candidate_market_date_regressive")
    elif candidate == current:
        fields = (
            "source_sha",
            "build_sha",
            "publication_set_sha256",
            "release_manifest_sha256",
        )
        if any(current_manifest.get(field) != candidate_manifest.get(field) for field in fields):
            errors.append("same_day_lineage_conflict")
    return {
        "ready": not errors,
        "status": "PASS" if not errors else "BLOCKED",
        "errors": errors,
        "idempotent": candidate == current and not errors,
        "current_market_date": current_text,
        "candidate_market_date": candidate_text,
    }


def _authorization_result(
    requested: str,
    mode: str,
    session: MarketSessionDecision | None,
    errors: list[str],
    *,
    boundary: Mapping[str, Any] | None,
    offline: bool = False,
) -> dict[str, Any]:
    expected = (
        str(boundary.get("expected_market_date") or "")
        if boundary is not None
        else requested if offline else None
    )
    return {
        "schema_version": SCHEMA,
        "status": "PASS" if not errors else "BLOCKED",
        "ready": not errors,
        "publication_mode": mode,
        "market_date": requested,
        "expected_market_date": expected or None,
        "current_market_date": (
            boundary.get("market_date") if boundary is not None else None
        ),
        "session": session.to_dict() if session is not None else None,
        "errors": list(dict.fromkeys(errors)),
        "offline_replay": offline,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _blocked(
    reason: str,
    local: datetime,
    *,
    session: MarketSessionDecision | None = None,
    due: datetime | None = None,
    error: str | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA,
        "status": "BLOCKED",
        "ready": False,
        "reason": reason,
        "market_date": session.market_date if session is not None else local.date().isoformat(),
        "expected_market_date": None,
        "session": session.to_dict() if session is not None else None,
        "due_at": due.astimezone(UTC).isoformat() if due is not None else None,
        "observed_at": local.astimezone(UTC).isoformat(),
        "error": error,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("validate", nargs="?")
    parser.add_argument("--market-date", required=True)
    parser.add_argument("--publication-mode", required=True)
    parser.add_argument("--now-utc", default=None)
    args = parser.parse_args(argv)
    if args.now_utc is not None and os.environ.get("DAWNSTRIKE_TEST_CLOCK") != "1":
        raise SystemExit("--now-utc is test-only")
    now = None
    if args.now_utc:
        try:
            now = datetime.fromisoformat(args.now_utc.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SystemExit("invalid --now-utc") from exc
    result = authorize_market_date(
        args.market_date, publication_mode=args.publication_mode, now=now
    )
    print(json.dumps(result, sort_keys=True))
    return 0 if result["ready"] is True else 4


if __name__ == "__main__":
    raise SystemExit(main())
