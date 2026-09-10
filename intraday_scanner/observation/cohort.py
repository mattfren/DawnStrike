"""Finite, resumable coordinator for the R2 observation cohort.

This module coordinates already authorized, retained capture inputs.  It does
not register a scheduler, renew itself, place orders, or decide strategy
eligibility.  Provider acquisition remains the separately scheduled SIP
capture route; this coordinator binds one fresh scope and one retained receipt
per expected session into the existing producer and observer contracts.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
from collections.abc import Callable
from datetime import UTC, date, datetime, timedelta
from datetime import time as clock_time
from pathlib import Path
from typing import Any

from intraday_scanner.market_calendar import (
    MARKET_TIMEZONE,
    REGULAR_CLOSE_ET,
    REGULAR_OPEN_ET,
    canonical_regular_session_id,
    market_session,
)
from intraday_scanner.observation.store import ObservationLock, ObservationLockError

COHORT_SCHEMA = "dawnstrike.observation.cohort.v1"
COHORT_STATE_SCHEMA = "dawnstrike.observation.cohort_state.v1"
MAX_EXPECTED_SESSIONS = 10
MAX_RETRIES = 3
REFERENCE_PANEL = ("DIA", "IWM", "QQQ", "SPY", "TLT")
MOVER_MEMBERSHIPS = ("selected", "rejected", "unselected")
APPROVED_PYTHON = Path(r"C:\Program Files\Dawnstrike\Python313\python.exe")
APPROVED_PYTHON_SHA256 = "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"


class CohortError(ValueError):
    """The cohort cannot safely resume from its durable inputs."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(path: Path, *, label: str, max_bytes: int = 16 * 1024 * 1024) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise CohortError(f"{label} is not a regular file: {path}")
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise CohortError(f"{label} exceeds its byte bound")
    try:
        value = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise CohortError(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise CohortError(f"{label} must be an object")
    return value


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    temporary.write_bytes(json.dumps(value, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    os.replace(temporary, path)


def _parse_date(value: str | date) -> date:
    if isinstance(value, date):
        return value
    try:
        parsed = date.fromisoformat(str(value))
    except ValueError as exc:
        raise CohortError("market date must be ISO YYYY-MM-DD") from exc
    if parsed.isoformat() != str(value):
        raise CohortError("market date must be ISO YYYY-MM-DD")
    return parsed


def _utc_boundary(day: date, value: clock_time) -> str:
    return datetime.combine(day, value, tzinfo=MARKET_TIMEZONE).astimezone(UTC).isoformat()


def expected_market_sessions(
    start_date: str | date, count: int = MAX_EXPECTED_SESSIONS
) -> list[dict[str, Any]]:
    """Derive a finite, calendar-bound session denominator with early-close identity."""

    if count != MAX_EXPECTED_SESSIONS:
        raise CohortError("OPS-02 requires exactly ten expected sessions")
    current = _parse_date(start_date)
    result: list[dict[str, Any]] = []
    while len(result) < count:
        decision = market_session(current)
        if decision.is_trading_day:
            close = (
                clock_time.fromisoformat(decision.close_time_et)
                if decision.close_time_et
                else REGULAR_CLOSE_ET
            )
            session_id = canonical_regular_session_id(current)
            result.append(
                {
                    "market_date": current.isoformat(),
                    "exchange": "XNYS",
                    "exchange_session_id": session_id,
                    "calendar_id": decision.calendar_id,
                    "calendar_authority": decision.calendar_authority,
                    "calendar_published_as_of": decision.calendar_published_as_of,
                    "session_status": decision.status.value,
                    "session_reason": decision.reason,
                    "is_early_close": decision.status.value == "early_close",
                    "start_utc": _utc_boundary(current, REGULAR_OPEN_ET),
                    "end_utc": _utc_boundary(current, close),
                    "source_deadline_grace_seconds": 900,
                }
            )
        current += timedelta(days=1)
    return result


def _sampling_plan(
    rows: list[dict[str, Any]], *, seed: str, max_candidates: int = 12
) -> dict[str, Any]:
    if max_candidates != 12:
        raise CohortError("OPS-02 sampling cap must remain twelve")
    by_membership = {
        membership: sorted([row["symbol"] for row in rows if row.get("membership") == membership])
        for membership in MOVER_MEMBERSHIPS
    }
    quotas = {"selected": 2, "rejected": 4, "unselected": 6}
    sampled: list[dict[str, Any]] = []
    for membership in MOVER_MEMBERSHIPS:
        population = by_membership[membership]
        quota = min(quotas[membership], len(population))
        ranked = sorted(
            population,
            key=lambda symbol: hashlib.sha256(f"{seed}:{membership}:{symbol}".encode()).hexdigest(),
        )
        probability = (quota / len(population)) if population else 0.0
        sampled.extend(
            {
                "symbol": symbol,
                "membership": membership,
                "inclusion_probability": probability,
                "sampling_seed": seed,
                "price_path_requested": True,
            }
            for symbol in ranked[:quota]
        )
    return {
        "seed": seed,
        "max_candidates": max_candidates,
        "sampled_count": len(sampled),
        "population_counts": {key: len(value) for key, value in by_membership.items()},
        "rows": sampled,
        "sampling_policy": "stratified_selected_rejected_unselected_hash_order",
        "outcome_independent": True,
    }


def validate_fresh_scope(path: Path, *, expected_date: str) -> dict[str, Any]:
    """Validate one daily mover census without admitting historical or core rows."""

    value = _read_json(path, label="daily scope declaration")
    if value.get("schema_version") != "dawnstrike.observation.scope_declaration.v1":
        raise CohortError("daily scope declaration schema is unsupported")
    if value.get("market_date") != expected_date:
        raise CohortError("daily scope declaration market_date does not match session")
    scopes = value.get("scopes")
    if not isinstance(scopes, dict) or set(scopes) != {
        "original_small_cap_gap",
        "liquid_reference_panel",
    }:
        raise CohortError("daily scope must contain the two named scopes")
    movers = scopes["original_small_cap_gap"]
    panel = scopes["liquid_reference_panel"]
    if not isinstance(movers, list) or len(movers) != 181:
        raise CohortError("daily mover census must contain exactly 181 rows")
    if not isinstance(panel, list) or [row.get("symbol") for row in panel] != list(REFERENCE_PANEL):
        raise CohortError("daily reference panel must be DIA/IWM/QQQ/SPY/TLT")
    symbols: set[str] = set()
    for row in movers:
        if not isinstance(row, dict):
            raise CohortError("daily mover row is not an object")
        symbol = str(row.get("symbol") or "").strip().upper()
        if symbol in symbols or not symbol:
            raise CohortError("daily mover census has duplicate or blank symbol")
        if row.get("source_lane") != "mover":
            raise CohortError("daily mover census contains a non-mover source lane")
        if row.get("membership") not in (*MOVER_MEMBERSHIPS, "missing_input"):
            raise CohortError("daily mover membership is invalid")
        symbols.add(symbol)
    for row in panel:
        if not isinstance(row, dict) or row.get("source_lane") != "reference_panel":
            raise CohortError("reference panel source identity is invalid")
    sources = value.get("source_artifacts")
    if not isinstance(sources, dict) or not sources:
        raise CohortError("daily scope lacks source artifact identities")
    for name, item in sources.items():
        if not isinstance(item, dict):
            raise CohortError(f"scope source identity is invalid: {name}")
        source_path = Path(str(item.get("path") or ""))
        expected_hash = str(item.get("sha256") or "").lower()
        if not source_path.is_file() or _sha256_file(source_path) != expected_hash:
            raise CohortError(f"scope source artifact is missing or changed: {name}")
    if value.get("scope_policy", {}).get("core_index_membership") != (
        "separate unavailable scope; never inferred here"
    ):
        raise CohortError("daily scope does not preserve separate core membership")
    return {
        "path": str(path.resolve()),
        "sha256": _sha256_file(path),
        "market_date": expected_date,
        "mover_count": len(movers),
        "reference_count": len(panel),
        "membership_counts": {
            membership: sum(row.get("membership") == membership for row in movers)
            for membership in (*MOVER_MEMBERSHIPS, "missing_input")
        },
        "sampling": _sampling_plan(movers, seed=f"dawnstrike-ops02:{expected_date}"),
        "source_artifacts": sources,
    }


def _git_identity(repo_root: Path) -> tuple[str, str]:
    head = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    tree = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD^{tree}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    dirty = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if dirty:
        raise CohortError("cohort repository must be clean")
    return head, tree


def _toolchain_identity(python_path: Path) -> dict[str, Any]:
    path = python_path.resolve(strict=True)
    version = subprocess.run(
        [str(path), "--version"], check=True, capture_output=True, text=True
    ).stdout.strip()
    return {
        "path": str(path),
        "version": version,
        "sha256": _sha256_file(path),
        "approved": str(path).casefold() == str(APPROVED_PYTHON).casefold()
        and _sha256_file(path).lower() == APPROVED_PYTHON_SHA256,
    }


def prepare_cohort(
    *,
    output_root: Path,
    input_root: Path,
    scope_root: Path,
    repo_root: Path,
    start_date: str | date = "2026-09-10",
    source_config_hash: str = "",
    entitlement_receipt: Path | None = None,
    runtime_env: Path | None = None,
    python_path: Path = APPROVED_PYTHON,
    max_pages: int = 1000,
    max_events: int = 10000,
    max_bytes: int = 64 * 1024 * 1024,
    max_rss_bytes: int = 256 * 1024 * 1024,
    max_wall_seconds: int = 1800,
) -> dict[str, Any]:
    if max_pages < 1 or max_pages > 1000 or max_events < 1 or max_events > 10000:
        raise CohortError("cohort request caps exceed the existing capture/observer bounds")
    if max_bytes < 1 or max_bytes > 64 * 1024 * 1024:
        raise CohortError("cohort byte cap exceeds the existing local bound")
    if max_rss_bytes < 1 or max_wall_seconds < 1:
        raise CohortError("cohort resource caps must be positive")
    code_sha, tree_sha = _git_identity(repo_root.resolve())
    toolchain = _toolchain_identity(python_path)
    if not toolchain["approved"]:
        raise CohortError("OPS-02 requires the approved Python 3.13.14 interpreter")
    sessions = expected_market_sessions(start_date)
    output_root = output_root.resolve()
    input_root = input_root.resolve()
    scope_root = scope_root.resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    plan = {
        "schema_version": COHORT_SCHEMA,
        "cohort_id": _sha256_bytes(
            _canonical({"repo": code_sha, "start": sessions[0]["market_date"]})
        )[:24],
        "status": "PREPARED",
        "created_at": datetime.now(UTC).isoformat(),
        "start_date": sessions[0]["market_date"],
        "expected_session_count": len(sessions),
        "expected_sessions": sessions,
        "input_root": str(input_root),
        "scope_root": str(scope_root),
        "repository": {
            "root": str(repo_root.resolve()),
            "code_sha": code_sha,
            "tree_sha": tree_sha,
        },
        "toolchain": toolchain,
        "source_identity": {
            "provider": "alpaca",
            "feed": "sip",
            "source_config_sha256": source_config_hash.lower(),
            "entitlement_receipt_path": str(entitlement_receipt.resolve())
            if entitlement_receipt
            else None,
            "entitlement_receipt_sha256": _sha256_file(entitlement_receipt)
            if entitlement_receipt
            else None,
            "runtime_env_path": str(runtime_env.resolve()) if runtime_env else None,
            "runtime_env_sha256": _sha256_file(runtime_env) if runtime_env else None,
            "raw_retention_policy": "retain provider receipt/state/pages; never overwrite",
        },
        "caps": {
            "max_pages": max_pages,
            "max_events": max_events,
            "max_bytes": max_bytes,
            "max_rss_bytes": max_rss_bytes,
            "max_wall_seconds": max_wall_seconds,
            "max_retries": MAX_RETRIES,
            "max_sessions": MAX_EXPECTED_SESSIONS,
        },
        "safety": {
            "research_only": True,
            "broker_execution_enabled": False,
            "orders_enabled": False,
            "trading_critical_state_mutation": False,
            "no_auto_renewal": True,
            "operator_intervention_required_for_source_or_scope": True,
        },
        "sessions": [
            {
                **session,
                "status": "EXPECTED",
                "attempts": 0,
                "scope_path": str(scope_root / session["market_date"] / "scope.json"),
                "capture_receipt_path": str(
                    input_root / session["market_date"] / "capture_run_receipt.json"
                ),
                "operator_intervention": (
                    "fresh daily 181-mover scope and retained capture receipt required"
                ),
            }
            for session in sessions
        ],
    }
    _atomic_json(output_root / "cohort-plan.json", plan)
    _atomic_json(output_root / "cohort-state.json", {**plan, "schema_version": COHORT_STATE_SCHEMA})
    return plan


def _process_memory(pid: int) -> dict[str, Any] | None:
    """Sample a Windows process tree without introducing a dependency."""

    if os.name != "nt":
        return None
    try:
        import ctypes
        from ctypes import wintypes

        class Entry(ctypes.Structure):
            _fields_ = [
                ("size", wintypes.DWORD),
                ("usage", wintypes.DWORD),
                ("pid", wintypes.DWORD),
                ("heap", ctypes.c_void_p),
                ("module", wintypes.DWORD),
                ("threads", wintypes.DWORD),
                ("parent", wintypes.DWORD),
                ("base", wintypes.LONG),
                ("flags", wintypes.DWORD),
                ("exe", wintypes.WCHAR * 260),
            ]

        class Counters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("page_faults", wintypes.DWORD),
                ("peak_working", ctypes.c_size_t),
                ("working", ctypes.c_size_t),
                ("quota_peak_paged", ctypes.c_size_t),
                ("quota_paged", ctypes.c_size_t),
                ("quota_peak_nonpaged", ctypes.c_size_t),
                ("quota_nonpaged", ctypes.c_size_t),
                ("pagefile", ctypes.c_size_t),
                ("peak_pagefile", ctypes.c_size_t),
                ("private_usage", ctypes.c_size_t),
            ]

        kernel = ctypes.windll.kernel32
        psapi = ctypes.windll.psapi
        snapshot = kernel.CreateToolhelp32Snapshot(0x00000002, 0)
        if snapshot == wintypes.HANDLE(-1).value:
            return None
        entry = Entry()
        entry.size = ctypes.sizeof(Entry)
        parents: dict[int, int] = {}
        try:
            if kernel.Process32FirstW(snapshot, ctypes.byref(entry)):
                while True:
                    parents[int(entry.pid)] = int(entry.parent)
                    if not kernel.Process32NextW(snapshot, ctypes.byref(entry)):
                        break
        finally:
            kernel.CloseHandle(snapshot)
        pids = {pid}
        changed = True
        while changed:
            changed = False
            for child, parent in parents.items():
                if parent in pids and child not in pids:
                    pids.add(child)
                    changed = True
        working = private = 0
        observed: list[dict[str, int]] = []
        for current in pids:
            handle = kernel.OpenProcess(0x1000 | 0x0400, False, current)
            if not handle:
                continue
            counters = Counters()
            counters.cb = ctypes.sizeof(Counters)
            try:
                if psapi.GetProcessMemoryInfo(handle, ctypes.byref(counters), counters.cb):
                    working += int(counters.working)
                    private += int(counters.private_usage)
                    observed.append(
                        {
                            "pid": current,
                            "parent_pid": parents.get(current, 0),
                            "working_set_bytes": int(counters.working),
                            "private_bytes": int(counters.private_usage),
                        }
                    )
            finally:
                kernel.CloseHandle(handle)
        return {
            "pid": pid,
            "pids": observed,
            "working_set_bytes": working,
            "private_bytes": private,
        }
    except (AttributeError, OSError, TypeError):
        return None


def _run_bounded(
    command: list[str],
    *,
    cwd: Path,
    timeout_seconds: int,
    max_rss_bytes: int,
    stop_path: Path,
) -> dict[str, Any]:
    started = datetime.now(UTC)
    monotonic_start = time.monotonic()
    process = subprocess.Popen(
        command, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
    )
    peak_working = 0
    peak_private = 0
    memory_observed = False
    termination_reason = None
    while process.poll() is None:
        sample = _process_memory(process.pid)
        if sample:
            memory_observed = True
            peak_working = max(peak_working, sample["working_set_bytes"])
            peak_private = max(peak_private, sample["private_bytes"])
            if max(peak_working, peak_private) > max_rss_bytes:
                termination_reason = "rss_cap_exceeded"
                process.terminate()
        if stop_path.exists() and termination_reason is None:
            termination_reason = "operator_stop_requested"
            process.terminate()
        if time.monotonic() - monotonic_start > timeout_seconds and termination_reason is None:
            termination_reason = "walltime_cap_exceeded"
            process.terminate()
        time.sleep(0.1)
    stdout, stderr = process.communicate()
    completed = datetime.now(UTC)
    return {
        "pid": process.pid,
        "pid_start_identity": f"{process.pid}:{started.isoformat()}",
        "started_at": started.isoformat(),
        "completed_at": completed.isoformat(),
        "wall_seconds": round(time.monotonic() - monotonic_start, 3),
        "exit_code": process.returncode,
        "stdout": stdout,
        "stderr": stderr,
        "termination_reason": termination_reason,
        "memory_observed": memory_observed,
        "peak_working_set_bytes": peak_working,
        "peak_private_bytes": peak_private,
        "max_rss_bytes": max_rss_bytes,
        "resource_peak_is_sampled": True,
        "descendant_processes_included": memory_observed,
    }


def _last_json(stdout: str, *, label: str) -> dict[str, Any]:
    for line in reversed(stdout.splitlines()):
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            return value
    raise CohortError(f"{label} did not emit a JSON receipt")


def _session_status(
    *,
    capture: dict[str, Any],
    observer: dict[str, Any],
    expected_symbol_count: int,
    corporate_actions_proven: bool,
) -> tuple[str, str, str]:
    capture_symbols = {str(symbol).upper() for symbol in capture.get("symbols", [])}
    if expected_symbol_count > len(capture_symbols):
        coverage = (
            "REFERENCE_PANEL_ONLY_DELAYED"
            if capture_symbols == set(REFERENCE_PANEL)
            else "PARTIAL_SYMBOL_COVERAGE"
        )
    else:
        coverage = "FULL_DECLARED_SYMBOL_COVERAGE"
    observer_status = str(observer.get("status") or "")
    if observer_status == "FAILED":
        return "DEGRADED", "observer_failed", coverage
    if observer_status == "PARTIAL" or int(observer.get("delayed_event_count") or 0) > 0:
        return "DEGRADED", "non_timely_or_partial_source", coverage
    if observer_status == "EMPTY":
        reason = (
            "healthy_empty_collection"
            if capture.get("status") == "COMPLETE"
            else "empty_incomplete_capture"
        )
        return "COMPLETE", reason, coverage
    if observer_status == "CAPTURED" and capture.get("status") == "COMPLETE":
        if not corporate_actions_proven:
            return "COMPLETE", "capture_complete_corporate_action_dependent_ineligible", coverage
        return "COMPLETE", "capture_complete", coverage
    return "DEGRADED", "capture_or_observer_not_complete", coverage


def _eligible_after(session: dict[str, Any], now: datetime) -> bool:
    end = datetime.fromisoformat(str(session["end_utc"]))
    return now >= end + timedelta(seconds=int(session["source_deadline_grace_seconds"]))


def _record_session(state: dict[str, Any], index: int, update: dict[str, Any]) -> None:
    state["sessions"][index] = {**state["sessions"][index], **update}


def resume_cohort(
    *,
    output_root: Path,
    input_root: Path,
    repo_root: Path,
    execute: bool = False,
    now: datetime | None = None,
    command_runner: Callable[..., dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Resume one finite cohort; ``execute`` only binds already retained inputs."""

    output_root = output_root.resolve()
    state_path = output_root / "cohort-state.json"
    state = _read_json(state_path, label="cohort state")
    if state.get("schema_version") != COHORT_STATE_SCHEMA:
        raise CohortError("unsupported cohort state schema")
    if state.get("repository", {}).get("root") != str(repo_root.resolve()):
        raise CohortError("cohort repository identity changed")
    if state.get("input_root") != str(input_root.resolve()):
        raise CohortError("cohort input root identity changed")
    current_sha, current_tree = _git_identity(repo_root.resolve())
    if (
        current_sha != state["repository"]["code_sha"]
        or current_tree != state["repository"]["tree_sha"]
    ):
        raise CohortError("cohort repository SHA/tree changed")
    now_utc = now or datetime.now(UTC)
    stop_path = output_root / ".cohort.stop"
    runner = command_runner or _run_bounded
    try:
        with ObservationLock(output_root / ".cohort.lock"):
            state["status"] = "RUNNING"
            _atomic_json(state_path, state)
            for index, session in enumerate(list(state["sessions"])):
                if stop_path.exists():
                    state["status"] = "STOPPED"
                    _record_session(
                        state,
                        index,
                        {"status": "STOPPED", "operator_intervention": "stop marker observed"},
                    )
                    _atomic_json(state_path, state)
                    return state
                if session.get("status") in {"COMPLETE", "MISSED_SESSION"}:
                    continue
                if not _eligible_after(session, now_utc):
                    _record_session(
                        state,
                        index,
                        {
                            "status": "EXPECTED",
                            "operator_intervention": (
                                "await session close plus delayed-source grace"
                            ),
                        },
                    )
                    continue
                scope_path = Path(str(session["scope_path"]))
                receipt_path = Path(str(session["capture_receipt_path"]))
                if not scope_path.is_file() or not receipt_path.is_file():
                    _record_session(
                        state,
                        index,
                        {
                            "status": "MISSED_SESSION",
                            "reason": "fresh_scope_or_retained_capture_receipt_missing",
                            "operator_intervention": (
                                "supply exact-date scope and separately scheduled capture receipt"
                            ),
                            "source_route": "separate_scheduled_sip_capture",
                        },
                    )
                    _atomic_json(state_path, state)
                    continue
                try:
                    scope_identity = validate_fresh_scope(
                        scope_path, expected_date=session["market_date"]
                    )
                    capture = _read_json(receipt_path, label="capture receipt")
                    if capture.get("session_id") != session["exchange_session_id"]:
                        raise CohortError(
                            "capture receipt session identity does not match expected session"
                        )
                    if capture.get("market_date") != session["market_date"]:
                        raise CohortError(
                            "capture receipt market date does not match expected session"
                        )
                    if (
                        state["source_identity"].get("source_config_sha256")
                        and str(capture.get("source_config_hash"))
                        != state["source_identity"]["source_config_sha256"]
                    ):
                        raise CohortError("capture receipt source config identity changed")
                    if (
                        capture.get("research_only") is not True
                        or capture.get("broker_execution_enabled") is not False
                    ):
                        raise CohortError(
                            "capture receipt is outside research-only broker-disabled boundary"
                        )
                except CohortError as exc:
                    _record_session(
                        state,
                        index,
                        {
                            "status": "DEGRADED",
                            "reason": str(exc),
                            "operator_intervention": "repair source identity before retry",
                        },
                    )
                    _atomic_json(state_path, state)
                    continue
                session_root = output_root / session["market_date"]
                producer_root = session_root / "producer"
                observer_root = session_root / "observer"
                commands: list[dict[str, Any]] = []
                result_payload: dict[str, Any] = {
                    "scope": scope_identity,
                    "capture_receipt_sha256": _sha256_file(receipt_path),
                }
                if not execute:
                    _record_session(
                        state,
                        index,
                        {
                            "status": "READY",
                            "scope": scope_identity,
                            "capture_receipt_sha256": result_payload["capture_receipt_sha256"],
                            "operator_intervention": (
                                "independent safeguards must approve --execute"
                            ),
                        },
                    )
                    _atomic_json(state_path, state)
                    continue
                for attempt in range(int(session.get("attempts") or 0) + 1, MAX_RETRIES + 1):
                    _record_session(
                        state,
                        index,
                        {
                            "status": "RUNNING",
                            "attempts": attempt,
                            "started_at": datetime.now(UTC).isoformat(),
                        },
                    )
                    _atomic_json(state_path, state)
                    producer_command = [
                        str(state["toolchain"]["path"]),
                        str(repo_root / "scripts" / "build_observation_inputs.py"),
                        "--capture-receipt",
                        str(receipt_path),
                        "--scope-declaration",
                        str(scope_path),
                        "--output-root",
                        str(producer_root),
                        "--decision-deadline",
                        session["end_utc"],
                        "--repository-root",
                        str(repo_root),
                        "--max-events",
                        str(state["caps"]["max_events"]),
                        "--max-bytes",
                        str(state["caps"]["max_bytes"]),
                        "--reduction-mode",
                        "bounded_derivative",
                    ]
                    producer_run = runner(
                        producer_command,
                        cwd=repo_root,
                        timeout_seconds=state["caps"]["max_wall_seconds"],
                        max_rss_bytes=state["caps"]["max_rss_bytes"],
                        stop_path=stop_path,
                    )
                    commands.append(
                        {
                            "role": "producer",
                            "command": producer_command,
                            "run": {
                                key: value
                                for key, value in producer_run.items()
                                if key not in {"stdout", "stderr"}
                            },
                        }
                    )
                    if producer_run.get("termination_reason") == "operator_stop_requested":
                        state["status"] = "STOPPED"
                        _record_session(state, index, {"status": "STOPPED", "commands": commands})
                        _atomic_json(state_path, state)
                        return state
                    if producer_run.get("exit_code") != 0:
                        continue
                    producer = _last_json(str(producer_run.get("stdout") or ""), label="producer")
                    observer_command = [
                        str(state["toolchain"]["path"]),
                        str(repo_root / "scripts" / "run_isolated_observer.py"),
                        "--manifest",
                        str(producer["manifest_path"]),
                        "--source-events",
                        str(producer["raw_events_path"]),
                        "--output-root",
                        str(observer_root),
                        "--repository-root",
                        str(repo_root),
                        "--max-pages",
                        str(state["caps"]["max_pages"]),
                        "--max-events",
                        str(state["caps"]["max_events"]),
                        "--max-bytes",
                        str(state["caps"]["max_bytes"]),
                        "--retries",
                        str(state["caps"]["max_retries"]),
                        "--stop-file",
                        str(observer_root.with_suffix(".stop")),
                    ]
                    observer_run = runner(
                        observer_command,
                        cwd=repo_root,
                        timeout_seconds=state["caps"]["max_wall_seconds"],
                        max_rss_bytes=state["caps"]["max_rss_bytes"],
                        stop_path=stop_path,
                    )
                    commands.append(
                        {
                            "role": "observer",
                            "command": observer_command,
                            "run": {
                                key: value
                                for key, value in observer_run.items()
                                if key not in {"stdout", "stderr"}
                            },
                        }
                    )
                    if observer_run.get("termination_reason") == "operator_stop_requested":
                        state["status"] = "STOPPED"
                        _record_session(state, index, {"status": "STOPPED", "commands": commands})
                        _atomic_json(state_path, state)
                        return state
                    if observer_run.get("exit_code") != 0:
                        continue
                    observer = _last_json(str(observer_run.get("stdout") or ""), label="observer")
                    entitlement = {}
                    entitlement_path = state["source_identity"].get("entitlement_receipt_path")
                    if entitlement_path and Path(entitlement_path).is_file():
                        entitlement = _read_json(
                            Path(entitlement_path), label="entitlement receipt"
                        )
                    corporate_actions_proven = "corporate_actions" in set(
                        entitlement.get("proven_endpoints") or []
                    )
                    final_status, reason, coverage = _session_status(
                        capture=capture,
                        observer=observer,
                        expected_symbol_count=186,
                        corporate_actions_proven=corporate_actions_proven,
                    )
                    result_payload.update(
                        {
                            "status": final_status,
                            "reason": reason,
                            "coverage_class": coverage,
                            "capture": {
                                key: capture.get(key)
                                for key in (
                                    "status",
                                    "session_id",
                                    "market_date",
                                    "symbols",
                                    "source_config_hash",
                                    "completed_at",
                                    "request_end",
                                )
                            },
                            "observer": {
                                key: observer.get(key)
                                for key in (
                                    "status",
                                    "reason",
                                    "captured_event_count",
                                    "delayed_event_count",
                                    "unavailable_event_count",
                                    "raw_events_sha256",
                                    "universe_manifest_sha256",
                                )
                            },
                            "availability_latency_seconds": _latency_seconds(
                                capture.get("completed_at"), capture.get("request_end")
                            ),
                            "corporate_actions_proven": corporate_actions_proven,
                            "decision_eligibility": "INELIGIBLE_CORPORATE_ACTION_UNPROVEN"
                            if not corporate_actions_proven
                            else "QUALIFIED_ONLY_FROM_ACTUAL_AVAILABILITY",
                            "commands": commands,
                            "completed_at": datetime.now(UTC).isoformat(),
                        }
                    )
                    _record_session(state, index, result_payload)
                    _atomic_json(state_path, state)
                    break
                else:
                    _record_session(
                        state,
                        index,
                        {
                            "status": "DEGRADED",
                            "reason": "bounded_retry_exhausted",
                            "commands": commands,
                        },
                    )
                    _atomic_json(state_path, state)
            statuses = {str(row.get("status")) for row in state["sessions"]}
            state["status"] = (
                "RUNNING"
                if statuses & {"EXPECTED", "RUNNING"}
                else "READY"
                if "READY" in statuses
                else "COMPLETE"
            )
            _atomic_json(state_path, state)
            return state
    except ObservationLockError as exc:
        raise CohortError(str(exc)) from exc


def _latency_seconds(completed: Any, request_end: Any) -> float | None:
    if not completed or not request_end:
        return None
    try:
        return round(
            (
                datetime.fromisoformat(str(completed)) - datetime.fromisoformat(str(request_end))
            ).total_seconds(),
            3,
        )
    except ValueError:
        return None


__all__ = [
    "APPROVED_PYTHON",
    "CohortError",
    "expected_market_sessions",
    "prepare_cohort",
    "resume_cohort",
    "validate_fresh_scope",
]
