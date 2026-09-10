"""OPS09 finite full-session delayed observer orchestration.

This module is an isolated extension of the existing finite cohort runner.  It
binds one date-bound request contract, OPS05 capture, OPS06 verification, and
the existing V6 daily/weekly consumers to an isolated output/database root.
It never writes the active database, registers a task, renews the cohort, or
enables broker execution.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from intraday_scanner.observation.cohort import (
    APPROVED_PYTHON,
    APPROVED_PYTHON_SHA256,
    REFERENCE_PANEL,
    expected_market_sessions,
)
from intraday_scanner.observation.store import ObservationLock, ObservationLockError
from intraday_scanner.observation.ops06_bars_adapter import (
    Ops06AdapterError,
    adapt_ops05_to_r3,
)
from intraday_scanner.services.alpha_v6_learning_service import (
    run_alpha_v6_daily_monitor,
    run_alpha_v6_weekly_training,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

OPS09_SCHEMA = "dawnstrike.observation.ops09_cohort.v1"
OPS09_STATE_SCHEMA = "dawnstrike.observation.ops09_state.v1"
MOVER_STRATA = ("selected", "rejected", "unselected", "missing_input")
SAMPLE_SEED = "dawnstrike-d042:27039"
MAX_PAGES = 100
MAX_ATTEMPTS = 3
MAX_EVENTS = 10_000
MAX_BYTES = 64 * 1024 * 1024
CAPTURE_BYTES = 48 * 1024 * 1024
DOWNSTREAM_BYTES = 16 * 1024 * 1024
MAX_RSS_BYTES = 256 * 1024 * 1024
MAX_WALL_SECONDS = 1_800


class Ops09Error(ValueError):
    """The OPS09 contract cannot safely continue."""


def _canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")


def _sha(value: Any) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _sha_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _tree_bytes(root: Path) -> int:
    """Return persisted session bytes without following links."""
    total = 0
    if not root.exists():
        return 0
    for path in root.rglob("*"):
        if path.is_file() and not path.is_symlink():
            total += path.stat().st_size
    return total


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{__import__('os').getpid()}.tmp")
    temporary.write_bytes(json.dumps(value, sort_keys=True, indent=2, default=str).encode() + b"\n")
    temporary.replace(path)


def _assert_budget(root: Path, reserve: int, phase: str) -> None:
    """Admit a bounded phase before it can create any child output."""
    used = _tree_bytes(root)
    if used + reserve > MAX_BYTES:
        raise Ops09Error(
            f"OPS09 cumulative byte budget rejects {phase}: used={used}, "
            f"reserve={reserve}, cap={MAX_BYTES}"
        )


def _read_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink() or not path.is_file():
        raise Ops09Error(f"{label} is not a regular file: {path}")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Ops09Error(f"{label} is invalid JSON") from exc
    if not isinstance(value, dict):
        raise Ops09Error(f"{label} must be an object")
    return value


def _sample_movers(rows: list[dict[str, Any]], *, seed: str = SAMPLE_SEED) -> dict[str, Any]:
    """Allocate the D042 four strata without normalizing missing input."""

    by_stratum = {
        name: sorted(
            [str(row.get("symbol") or "").upper() for row in rows if row.get("membership") == name]
        )
        for name in MOVER_STRATA
    }
    quotas = {"selected": 2, "rejected": 4, "unselected": 4, "missing_input": 2}
    chosen: list[dict[str, Any]] = []
    for name in MOVER_STRATA:
        ranked = sorted(
            by_stratum[name],
            key=lambda symbol: hashlib.sha256(f"{seed}:{name}:{symbol}".encode()).hexdigest(),
        )
        take = min(quotas[name], len(ranked))
        probability = take / len(ranked) if ranked else 0.0
        chosen.extend(
            {
                "symbol": symbol,
                "membership": name,
                "sampling_seed": seed,
                "inclusion_probability": probability,
            }
            for symbol in ranked[:take]
        )
    remaining = 12 - len(chosen)
    if remaining > 0:
        for name in MOVER_STRATA:
            ranked = sorted(
                set(by_stratum[name]) - {row["symbol"] for row in chosen},
                key=lambda symbol: hashlib.sha256(f"{seed}:{name}:{symbol}".encode()).hexdigest(),
            )
            for symbol in ranked[:remaining]:
                chosen.append(
                    {
                        "symbol": symbol,
                        "membership": name,
                        "sampling_seed": seed,
                        "inclusion_probability": min(1.0, quotas[name] / len(by_stratum[name]))
                        if by_stratum[name]
                        else 0.0,
                    }
                )
            remaining = 12 - len(chosen)
            if not remaining:
                break
    return {
        "seed": seed,
        "max_candidates": 12,
        "rows": chosen,
        "sampled_count": len(chosen),
        "population_counts": {name: len(by_stratum[name]) for name in MOVER_STRATA},
        "allocation_order": list(MOVER_STRATA),
        "outcome_independent": True,
        "full_census_retained": True,
    }


def validate_ops09_scope(path: Path, *, expected_date: str) -> dict[str, Any]:
    """Validate complete evolving census or an explicitly missing-input panel partial."""

    value = _read_object(path, "OPS09 scope")
    if value.get("schema_version") != "dawnstrike.observation.scope_declaration.v1":
        raise Ops09Error("OPS09 scope schema is unsupported")
    if value.get("market_date") != expected_date:
        raise Ops09Error("OPS09 scope date mismatch")
    scopes = value.get("scopes")
    if not isinstance(scopes, dict) or set(scopes) != {
        "original_small_cap_gap",
        "liquid_reference_panel",
    }:
        raise Ops09Error("OPS09 scope must preserve both named scopes")
    movers = scopes["original_small_cap_gap"]
    panel = scopes["liquid_reference_panel"]
    if not isinstance(movers, list) or not isinstance(panel, list):
        raise Ops09Error("OPS09 scope rows are invalid")
    if [str(row.get("symbol") or "").upper() for row in panel] != list(REFERENCE_PANEL):
        raise Ops09Error("OPS09 reference panel identity is invalid")
    if any(row.get("source_lane") != "reference_panel" for row in panel):
        raise Ops09Error("OPS09 reference panel is not source-bound")
    seen: set[str] = set()
    for row in movers:
        if not isinstance(row, dict):
            raise Ops09Error("OPS09 mover row is invalid")
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol or symbol in seen or row.get("source_lane") != "mover":
            raise Ops09Error("OPS09 mover census is duplicate or not source-bound")
        if row.get("membership") not in MOVER_STRATA:
            raise Ops09Error("OPS09 mover membership is invalid")
        seen.add(symbol)
    producer = value.get("producer_completeness") or {}
    missing_input = bool(value.get("missing_input") or producer.get("status") == "MISSING_INPUT")
    if missing_input:
        if movers or producer.get("status") not in {"MISSING_INPUT", "UNAVAILABLE"}:
            raise Ops09Error("OPS09 missing-input scope must not fabricate mover rows")
    else:
        if (
            producer.get("status") != "COMPLETE"
            or producer.get("source_count") != len(movers)
            or producer.get("declared_count") != len(movers)
            or producer.get("included_count") != len(movers)
            or producer.get("truncated") is not False
            or producer.get("survivorship_filter") is not False
            or producer.get("source_as_of") != expected_date
        ):
            raise Ops09Error("OPS09 producer completeness is not date-bound")
    sources = value.get("source_artifacts") or {}
    for name, item in sources.items():
        source = Path(str(item.get("path") or ""))
        if not source.is_file() or _sha_file(source) != str(item.get("sha256") or "").lower():
            raise Ops09Error(f"OPS09 scope source changed: {name}")
    source_identity = value.get("source_identity") or {}
    if source_identity.get("market_date") != expected_date:
        raise Ops09Error("OPS09 scope source identity is stale")
    sampling = _sample_movers(movers)
    return {
        "path": str(path.resolve()),
        "sha256": _sha_file(path),
        "market_date": expected_date,
        "mover_count": len(movers),
        "reference_count": len(panel),
        "missing_input": missing_input,
        "membership_counts": {
            name: sum(row.get("membership") == name for row in movers) for name in MOVER_STRATA
        },
        "sampling": sampling,
        "source_artifacts": sources,
        "producer_completeness": producer,
        "source_identity": source_identity,
        "scope": value,
    }


def _toolchain(path: Path) -> dict[str, Any]:
    if not path.is_file():
        raise Ops09Error(f"approved interpreter is missing: {path}")
    digest = _sha_file(path)
    if path.resolve() != APPROVED_PYTHON.resolve() or digest != APPROVED_PYTHON_SHA256:
        raise Ops09Error("OPS09 requires the approved Python 3.13.14 interpreter")
    return {"path": str(path.resolve()), "sha256": digest, "version": "3.13.14", "approved": True}


def _repo_identity(repo_root: Path) -> dict[str, str]:
    def run(arg: str) -> str:
        result = subprocess.run(["git", "-C", str(repo_root), "rev-parse", arg], check=True, capture_output=True, text=True)
        return result.stdout.strip()
    return {"code_sha": run("HEAD"), "tree_sha": run("HEAD^{tree}"), "root": str(repo_root.resolve())}


def prepare_ops09_cohort(
    *, output_root: Path, input_root: Path, scope_root: Path, database_root: Path,
    repo_root: Path, start_date: str = "2026-09-10", source_config_hash: str = "",
    source_config_path: Path | None = None,
    entitlement_receipt: Path | None = None, runtime_env: Path | None = None,
    dependency_stage_root: Path | None = None, dependency_stage_receipt_path: Path | None = None,
    python_path: Path = APPROVED_PYTHON,
) -> dict[str, Any]:
    toolchain = _toolchain(python_path)
    identity = _repo_identity(repo_root.resolve())
    if source_config_path is None or not source_config_path.is_file():
        raise Ops09Error("OPS09 requires an authenticated source-config path and SHA-256")
    source_config_path = source_config_path.resolve()
    actual_source_config_hash = _sha_file(source_config_path)
    if source_config_hash.lower() != actual_source_config_hash:
        raise Ops09Error("OPS09 source-config hash does not match the bound file")
    if dependency_stage_root is not None and not dependency_stage_root.is_dir():
        raise Ops09Error("OPS09 dependency stage root is missing")
    if dependency_stage_receipt_path is not None and not dependency_stage_receipt_path.is_file():
        raise Ops09Error("OPS09 dependency stage receipt is missing")
    if database_root.resolve() == Path(r"C:\r\dawnstrike-state\shadow_real.sqlite").resolve():
        raise Ops09Error("OPS09 refuses the active shadow database")
    sessions = expected_market_sessions(start_date)
    output_root = output_root.resolve(); input_root = input_root.resolve(); scope_root = scope_root.resolve(); database_root = database_root.resolve()
    plan: dict[str, Any] = {
        "schema_version": OPS09_SCHEMA,
        "mode": "ops09",
        "status": "PREPARED",
        "cohort_id": _sha({"repo": identity["code_sha"], "start": start_date})[:24],
        "created_at": datetime.now(UTC).isoformat(),
        "expected_session_count": 10,
        "expected_sessions": sessions,
        "input_root": str(input_root), "scope_root": str(scope_root), "database_root": str(database_root),
        "repository": identity, "toolchain": toolchain,
        "source_identity": {
            "provider": "alpaca", "feed": "sip", "source_config_sha256": actual_source_config_hash,
            "source_config_path": str(source_config_path),
            "entitlement_receipt_path": str(entitlement_receipt.resolve()) if entitlement_receipt else None,
            "entitlement_receipt_sha256": _sha_file(entitlement_receipt) if entitlement_receipt else None,
            "runtime_env_path": str(runtime_env.resolve()) if runtime_env else None,
            "dependency_stage_root": str(dependency_stage_root.resolve()) if dependency_stage_root else None,
            "dependency_stage_receipt_path": str(dependency_stage_receipt_path.resolve()) if dependency_stage_receipt_path else None,
            "entitlement_status": "BOUND" if entitlement_receipt else "UNBOUND_OFFLINE_ONLY",
            "raw_retention_policy": "immutable request/page/receipt lineage; no active-state writes",
        },
        "caps": {"max_pages": MAX_PAGES, "max_attempts_total": MAX_ATTEMPTS, "max_events": MAX_EVENTS,
                 "max_bytes": MAX_BYTES, "capture_bytes": CAPTURE_BYTES, "downstream_bytes": DOWNSTREAM_BYTES,
                 "max_rss_bytes": MAX_RSS_BYTES, "max_wall_seconds": MAX_WALL_SECONDS},
        "safety": {"research_only": True, "broker_execution_enabled": False, "orders_enabled": False,
                   "no_auto_renewal": True, "no_second_ops03_stream": True},
        "sessions": [
            {**session, "status": "EXPECTED", "attempts": 0,
             "scope_path": str(scope_root / session["market_date"] / "scope.json"),
             "fixture_path": str(input_root / session["market_date"] / "fixture.json"),
             "request_contract_path": str(output_root / session["market_date"] / "request-contract.json")}
            for session in sessions
        ],
    }
    _atomic_json(output_root / "cohort-plan.json", plan)
    _atomic_json(output_root / "cohort-state.json", {**plan, "schema_version": OPS09_STATE_SCHEMA})
    return plan


def _eligible(session: dict[str, Any], now: datetime) -> bool:
    try:
        return now >= datetime.fromisoformat(str(session["end_utc"]).replace("Z", "+00:00"))
    except ValueError as exc:
        raise Ops09Error("OPS09 session calendar boundary is invalid") from exc


def _request_contract(*, plan: dict[str, Any], session: dict[str, Any], scope: dict[str, Any], scope_path: Path) -> dict[str, Any]:
    lineage = plan["source_identity"]
    contract = {
        "schema_version": "dawnstrike.ops09.request_contract.v1", "status": "REQUESTED",
        "requested_at": datetime.now(UTC).isoformat(), "market_date": session["market_date"],
        "session_id": session["exchange_session_id"], "calendar": session,
        "scope_path": str(scope_path.resolve()), "scope_sha256": scope["sha256"],
        "source_config_sha256": lineage.get("source_config_sha256"),
        "entitlement_receipt_path": lineage.get("entitlement_receipt_path"),
        "entitlement_receipt_sha256": lineage.get("entitlement_receipt_sha256"),
        "provider": "alpaca", "feed": "sip", "endpoints": ["bars", "corporate_actions"],
        "reference_panel": list(REFERENCE_PANEL),
        "sampled_movers": scope["sampling"]["rows"], "missing_input": scope["missing_input"],
        "research_only": True, "broker_execution_enabled": False,
        "legacy_capture_receipt_hash_alias": True,
        "capture_receipt_hash_alias": {
            "source_field": "request_contract_sha256",
            "target_cli_argument": "--capture-receipt-hash",
            "semantics": "legacy OPS05 argument alias; request and capture identities remain distinct",
            "authenticated": True,
        },
        "allowed_request_parameters": {
            "provider": "alpaca", "feed": "sip", "endpoints": ["bars", "corporate_actions"],
            "window_names": ["full_session", "prior_close"], "execute": "offline_fixture_or_explicit_execute",
        },
        "limits": plan["caps"], "repository": plan["repository"], "toolchain": plan["toolchain"],
    }
    contract["request_contract_sha256"] = _sha(contract)
    return contract


def _validate_request_contract(
    contract: dict[str, Any], *, plan: dict[str, Any], session: dict[str, Any], scope: dict[str, Any], scope_path: Path
) -> None:
    declared = contract.get("request_contract_sha256")
    unsigned = dict(contract)
    unsigned.pop("request_contract_sha256", None)
    if not isinstance(declared, str) or _sha(unsigned) != declared:
        raise Ops09Error("OPS09 immutable request contract hash is invalid")
    if contract.get("status") != "REQUESTED" or contract.get("market_date") != session["market_date"]:
        raise Ops09Error("OPS09 immutable request contract conflicts with resumed session")
    if contract.get("session_id") != session["exchange_session_id"] or contract.get("scope_sha256") != scope["sha256"]:
        raise Ops09Error("OPS09 immutable request contract session or scope identity changed")
    if Path(str(contract.get("scope_path") or "")).resolve() != scope_path.resolve():
        raise Ops09Error("OPS09 immutable request contract scope path changed")
    lineage = plan["source_identity"]
    if contract.get("source_config_sha256") != lineage.get("source_config_sha256"):
        raise Ops09Error("OPS09 immutable request contract source-config identity changed")
    source_path = Path(str(lineage.get("source_config_path") or ""))
    if not source_path.is_file() or _sha_file(source_path) != str(lineage.get("source_config_sha256") or ""):
        raise Ops09Error("OPS09 bound source-config is missing or changed")
    if contract.get("repository") != plan.get("repository") or contract.get("toolchain") != plan.get("toolchain"):
        raise Ops09Error("OPS09 immutable request contract runtime identity changed")
    if contract.get("provider") != "alpaca" or contract.get("feed") != "sip" or contract.get("endpoints") != ["bars", "corporate_actions"]:
        raise Ops09Error("OPS09 request parameters are outside the allowed provider scope")
    alias = contract.get("capture_receipt_hash_alias")
    if not isinstance(alias, dict) or alias.get("source_field") != "request_contract_sha256" or alias.get("target_cli_argument") != "--capture-receipt-hash" or alias.get("authenticated") is not True:
        raise Ops09Error("OPS09 capture hash alias is not typed and authenticated")


def _run_capture(*, plan: dict[str, Any], session: dict[str, Any], contract: dict[str, Any],
                 scope: dict[str, Any], scope_path: Path, session_root: Path, fixture: Path | None,
                 execute: bool, timeout_seconds: int) -> dict[str, Any]:
    capture_root = session_root / "capture"
    census_path = session_root / "capture-census.json"
    _assert_budget(session_root.parent, CAPTURE_BYTES, "capture phase")
    movers = scope["scope"].get("scopes", {}).get("original_small_cap_gap", [])
    census_path.parent.mkdir(parents=True, exist_ok=True)
    census_path.write_text(json.dumps(movers, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    argument_list = [str(Path(plan["repository"]["root"]) / "scripts" / "ops05_historical_bars.py"),
                     "--market-date", session["market_date"], "--census", str(census_path), "--output-root", str(capture_root),
                     "--source-config-hash", str(contract["source_config_sha256"]),
                     "--capture-receipt-hash", contract["request_contract_sha256"], "--resume-across-roots"]
    if fixture is not None:
        argument_list += ["--fixture", str(fixture)]
    elif execute:
        argument_list += ["--execute", "--env-file", str(plan["source_identity"].get("runtime_env_path") or ".env")]
    else:
        return {"status": "READY", "request": contract, "reason": "execute not requested"}
    wrapper = Path(plan["repository"]["root"]) / "scripts" / "run_ops05_under_job.ps1"
    log_root = session_root / "native-wrapper"
    args = ["powershell.exe", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(wrapper),
            "-RepoRoot", str(plan["repository"]["root"]), "-LogRoot", str(log_root),
            "-ExpectedSourceSha", str(plan["repository"]["code_sha"]),
            "-ArgumentJson", json.dumps(argument_list, separators=(",", ":")), "-TimeoutSeconds", str(timeout_seconds)]
    stage_root = plan["source_identity"].get("dependency_stage_root")
    stage_receipt = plan["source_identity"].get("dependency_stage_receipt_path")
    if stage_root and stage_receipt:
        args += ["-DependencyStageRoot", str(stage_root), "-DependencyStageReceiptPath", str(stage_receipt)]
    try:
        completed = subprocess.run(args, cwd=plan["repository"]["root"], capture_output=True, text=True,
                                   timeout=max(1, timeout_seconds + 5), check=False)
    except subprocess.TimeoutExpired:
        return {"status": "DEGRADED", "reason": "capture_wall_timeout", "command": args}
    output = (completed.stdout or "").splitlines()
    payload: dict[str, Any] = {}
    if output:
        try: payload = json.loads(output[-1])
        except json.JSONDecodeError: payload = {}
    if completed.returncode != 0:
        return {"status": "PARTIAL", "reason": "capture_failed", "exit_code": completed.returncode,
                "stderr": completed.stderr[-2000:], "command": args}
    receipt = _read_object(capture_root / "receipt.json", "OPS05 receipt")
    coverage = receipt.get("coverage") if isinstance(receipt.get("coverage"), dict) else {}
    page_count = int(coverage.get("page_count") or 0)
    event_count = int(coverage.get("bar_count") or 0) + int(coverage.get("corporate_action_count") or 0) + int(coverage.get("boundary_event_count") or 0)
    capture_bytes = int(coverage.get("persisted_output_bytes") or 0)
    if page_count > MAX_PAGES or event_count > MAX_EVENTS or capture_bytes > CAPTURE_BYTES:
        return {"status": "DEGRADED", "reason": "capture budget exceeded", "receipt": receipt,
                "capture_budget": {"page_count": page_count, "event_count": event_count,
                                   "persisted_bytes": capture_bytes}, "capture_root": str(capture_root)}
    status = "CAPTURED" if receipt.get("status") == "CAPTURED" else "PARTIAL"
    return {"status": status, "command": args, "cli": payload, "receipt": receipt,
            "capture_root": str(capture_root), "capture_receipt_sha256": _sha_file(capture_root / "receipt.json"),
            "capture_budget": {"page_count": page_count, "event_count": event_count,
                               "persisted_bytes": capture_bytes, "max_pages": MAX_PAGES,
                               "max_events": MAX_EVENTS, "max_bytes": CAPTURE_BYTES}}


def _run_consumers(*, adapted: dict[str, Any], database_path: Path, session: dict[str, Any], repo_sha: str) -> dict[str, Any]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    store = SQLiteScanStore(database_path)
    store.initialize()
    daily = run_alpha_v6_daily_monitor(store, market_date=session["market_date"], observation_source=adapted)
    weekly = run_alpha_v6_weekly_training(store, code_sha=repo_sha, market_date=session["market_date"], observation_source=adapted)
    return {"daily": daily, "weekly": weekly, "database_path": str(database_path.resolve()), "isolated": True}


def _resume_ops09_unlocked(*, output_root: Path, input_root: Path, scope_root: Path, database_root: Path,
                           repo_root: Path, execute: bool = False, now: datetime | None = None,
                           fixture_root: Path | None = None, decision_root: Path | None = None) -> dict[str, Any]:
    output_root = output_root.resolve(); state_path = output_root / "cohort-state.json"
    state = _read_object(state_path, "OPS09 cohort state")
    if state.get("schema_version") != OPS09_STATE_SCHEMA or state.get("mode") != "ops09":
        raise Ops09Error("OPS09 state identity is invalid")
    identity = _repo_identity(repo_root.resolve())
    if state.get("repository", {}).get("code_sha") != identity["code_sha"] or state.get("repository", {}).get("tree_sha") != identity["tree_sha"]:
        raise Ops09Error("OPS09 repository SHA/tree changed")
    if state.get("database_root") != str(database_root.resolve()) or database_root.resolve() == Path(r"C:\r\dawnstrike-state\shadow_real.sqlite").resolve():
        raise Ops09Error("OPS09 database root is not isolated")
    now_utc = now or datetime.now(UTC)
    if not state.get("cohort_started_at"):
        state["cohort_started_at"] = now_utc.isoformat()
        state["cohort_deadline_at"] = (now_utc.timestamp() + MAX_WALL_SECONDS)
        _atomic_json(state_path, state)
    deadline = float(state.get("cohort_deadline_at") or now_utc.timestamp())
    stop_path = output_root / ".cohort.stop"
    for index, session in enumerate(state["sessions"]):
        if stop_path.exists():
            state["status"] = "STOPPED"
            state["stop_reason"] = "operator_stop_requested"
            for pending in state["sessions"][index:]:
                if pending.get("status") in {"EXPECTED", "READY", "RUNNING"}:
                    pending.update({"status": "STOPPED", "operator_intervention": "stop marker observed"})
            _atomic_json(state_path, state)
            return state
        if session.get("status") in {"COMPLETE", "MISSED_SESSION"}: continue
        if not _eligible(session, now_utc):
            session.update({"status": "EXPECTED", "reason": "awaiting session close and delayed-source grace"}); continue
        scope_path = Path(session["scope_path"])
        if not scope_path.is_file():
            session.update({"status": "MISSED_SESSION", "reason": "date-bound scope missing", "missing_input": "original_scope"}); continue
        try:
            scope = validate_ops09_scope(scope_path, expected_date=session["market_date"])
        except Ops09Error as exc:
            session.update({"status": "DEGRADED", "reason": str(exc), "operator_intervention": "repair exact date-bound scope"}); continue
        remaining = int(deadline - now_utc.timestamp())
        if remaining < 1:
            state["status"] = "DEGRADED"; state["reason"] = "cohort wall-time budget exhausted"
            _atomic_json(state_path, state)
            return state
        session_root = output_root / session["market_date"]; session_root.mkdir(parents=True, exist_ok=True)
        if int(session.get("attempts") or 0) >= MAX_ATTEMPTS:
            session.update({"status": "DEGRADED", "reason": "capture attempt budget exhausted", "decision_eligibility": "ZERO"})
            continue
        session["attempts"] = int(session.get("attempts") or 0) + 1
        session["status"] = "RUNNING"
        _atomic_json(state_path, state)
        contract_path = Path(session["request_contract_path"])
        if contract_path.is_file():
            contract = _read_object(contract_path, "OPS09 request contract")
            _validate_request_contract(contract, plan=state, session=session, scope=scope, scope_path=scope_path)
        else:
            contract = _request_contract(plan=state, session=session, scope=scope, scope_path=scope_path)
            _atomic_json(contract_path, contract)
        if not execute:
            session.update({"status": "READY", "request_contract_sha256": contract["request_contract_sha256"], "scope": scope}); continue
        fixture = (fixture_root.resolve() / session["market_date"] / "fixture.json") if fixture_root else None
        if fixture is not None and not fixture.is_file(): fixture = None
        capture = _run_capture(plan=state, session=session, contract=contract, scope=scope, scope_path=scope_path,
                               session_root=session_root, fixture=fixture, execute=execute, timeout_seconds=remaining)
        session.update({"capture": {k: v for k, v in capture.items() if k not in {"receipt"}},
                        "request_contract_sha256": contract["request_contract_sha256"]})
        if capture.get("status") != "CAPTURED":
            session.update({"status": "PARTIAL" if capture.get("status") == "PARTIAL" else "DEGRADED", "decision_eligibility": "ZERO"}); continue
        decision_path = (decision_root.resolve() / session["market_date"] / "decisions.json") if decision_root else None
        if decision_path is None or not decision_path.is_file():
            session.update({"status": "PARTIAL", "decision_status": "MISSING_INPUT", "decision_eligibility": "ZERO", "reason": "raw capture retained; decision artifact missing"}); continue
        try:
            _assert_budget(output_root, DOWNSTREAM_BYTES, "adapter and consumer phase")
            adapted = adapt_ops05_to_r3(observation_root=Path(capture["capture_root"]), decision_artifact=decision_path,
                                        output_root=session_root / "ops06", as_of=state["expected_sessions"][index]["end_utc"])
            consumers = _run_consumers(adapted=adapted, database_path=database_root / f"{session['market_date']}.sqlite",
                                       session=session, repo_sha=identity["code_sha"])
        except (Ops06AdapterError, ValueError, OSError) as exc:
            session.update({"status": "DEGRADED", "decision_status": "INVALID_OR_LATE", "decision_eligibility": "ZERO", "reason": str(exc)}); continue
        session.update({"status": "COMPLETE", "decision_status": "BOUND", "decision_eligibility": "DELAYED_LABEL_ONLY",
                        "ops06": {k: v for k, v in adapted.items() if k not in {"adapter_packet", "decisions"}},
                        "consumers": {"daily_status": consumers["daily"].get("status"), "weekly_status": consumers["weekly"].get("status"),
                                      "database_path": consumers["database_path"]},
                        "coverage_class": "delayed_historical_label_only",
                        "consumer_cadence": "existing_public_daily_monitor_and_existing_weekly_due_or_not_due"})
        session_bytes = _tree_bytes(session_root)
        capture_tree_bytes = _tree_bytes(session_root / "capture")
        downstream_tree_bytes = _tree_bytes(session_root / "ops06")
        session["resource_receipt"] = {
            "persisted_bytes": session_bytes,
            "capture_tree_bytes": capture_tree_bytes,
            "downstream_tree_bytes": downstream_tree_bytes,
            "capture_bytes_cap": CAPTURE_BYTES,
            "downstream_bytes_cap": DOWNSTREAM_BYTES,
            "total_bytes_cap": MAX_BYTES,
        }
        if (session_bytes > MAX_BYTES or capture_tree_bytes > CAPTURE_BYTES
                or downstream_tree_bytes > DOWNSTREAM_BYTES):
            session.update({"status": "DEGRADED", "decision_eligibility": "ZERO",
                            "reason": "session artifact budget exceeded"})
        _atomic_json(session_root / "session-receipt.json", session)
        _atomic_json(state_path, state)
    statuses = {str(row.get("status")) for row in state["sessions"]}
    state["status"] = "RUNNING" if statuses & {"EXPECTED", "RUNNING"} else "COMPLETE"
    _atomic_json(state_path, state)
    return state


def resume_ops09_cohort(*, output_root: Path, input_root: Path, scope_root: Path, database_root: Path,
                        repo_root: Path, execute: bool = False, now: datetime | None = None,
                        fixture_root: Path | None = None, decision_root: Path | None = None) -> dict[str, Any]:
    """Resume under the existing single-instance lock and operator stop marker."""
    output_root = output_root.resolve()
    lock_path = output_root / ".cohort.lock"
    try:
        with ObservationLock(lock_path):
            return _resume_ops09_unlocked(
                output_root=output_root, input_root=input_root, scope_root=scope_root,
                database_root=database_root, repo_root=repo_root, execute=execute, now=now,
                fixture_root=fixture_root, decision_root=decision_root,
            )
    except ObservationLockError as exc:
        raise Ops09Error(f"OPS09 cohort is already running: {lock_path}") from exc


__all__ = ["Ops09Error", "prepare_ops09_cohort", "resume_ops09_cohort", "validate_ops09_scope"]
