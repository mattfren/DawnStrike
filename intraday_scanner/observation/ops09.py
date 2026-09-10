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
import os
import sqlite3
import subprocess
import time
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
NATIVE_CAPTURE_BYTES = 8 * 1024 * 1024
NATIVE_METADATA_BYTES = 1 * 1024 * 1024
NATIVE_RESERVED_BYTES = NATIVE_CAPTURE_BYTES + NATIVE_METADATA_BYTES
DOWNSTREAM_BYTES = 7 * 1024 * 1024
MAX_RSS_BYTES = 256 * 1024 * 1024
MAX_WALL_SECONDS = 1_800


class Ops09Error(ValueError):
    """The OPS09 contract cannot safely continue."""


class _ByteLedger:
    """Durable, source-owned reservation ledger for one logical session."""

    def __init__(self, *, output_root: Path, database_root: Path, identity: dict[str, Any]) -> None:
        self.output_root = output_root.resolve()
        self.database_root = database_root.resolve()
        self.path = self.output_root / ".ops09-byte-ledger.json"
        self.identity = identity
        self.output_root.mkdir(parents=True, exist_ok=True)
        if self.path.is_file():
            payload = _read_object(self.path, "OPS09 byte ledger")
            if payload.get("schema_version") != "dawnstrike.ops09.byte_ledger.v1":
                raise Ops09Error("OPS09 byte ledger schema is unsupported")
            if payload.get("identity") != identity:
                raise Ops09Error("OPS09 byte ledger identity changed")
            if int(payload.get("max_bytes") or 0) != MAX_BYTES:
                raise Ops09Error("OPS09 byte ledger limit changed")
            reservations = payload.get("reservations")
            self.reservations = reservations if isinstance(reservations, dict) else {}
        else:
            self.reservations: dict[str, int] = {}
            self._write()

    def _actual_bytes(self) -> int:
        return _tree_bytes(self.output_root) + _tree_bytes(self.database_root)

    def _write(self) -> None:
        payload = {
            "schema_version": "dawnstrike.ops09.byte_ledger.v1",
            "max_bytes": MAX_BYTES,
            "identity": self.identity,
            "reservations": self.reservations,
            "actual_bytes": self._actual_bytes(),
        }
        temporary = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        temporary.write_bytes(_canonical(payload) + b"\n")
        temporary.replace(self.path)

    def admit(self, phase: str, reserve: int) -> None:
        if phase in self.reservations:
            raise Ops09Error(f"OPS09 byte ledger phase is already admitted: {phase}")
        if reserve < 0 or reserve > MAX_BYTES:
            raise Ops09Error(f"OPS09 byte ledger reservation is invalid: {phase}")
        actual = self._actual_bytes()
        active = sum(int(value) for value in self.reservations.values())
        if actual + active + reserve > MAX_BYTES:
            raise Ops09Error(
                f"OPS09 cumulative byte budget rejects {phase}: used={actual}, "
                f"reserved={active}, reserve={reserve}, cap={MAX_BYTES}"
            )
        self.reservations[phase] = reserve
        self._write()

    def release(self, phase: str) -> dict[str, int]:
        reserved = int(self.reservations.pop(phase, 0))
        actual = self._actual_bytes()
        if actual > MAX_BYTES:
            raise Ops09Error(
                f"OPS09 cumulative byte budget exceeded after {phase}: "
                f"used={actual}, cap={MAX_BYTES}"
            )
        self._write()
        return {"reserved_bytes": reserved, "actual_bytes": actual}


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
    if root.is_file() and not root.is_symlink():
        return root.stat().st_size
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
    # D042 starts each available stratum at three, then redistributes the
    # remainder deterministically.  The resulting 2/4/4/2 is data-shaped for
    # the Sep-9 census, not a future-session rule.
    quotas = {name: 3 for name in MOVER_STRATA}
    chosen: list[dict[str, Any]] = []
    for name in MOVER_STRATA:
        ranked = sorted(
            by_stratum[name],
            key=lambda symbol: hashlib.sha256(f"{seed}:{name}:{symbol}".encode()).hexdigest(),
        )
        take = min(quotas[name], len(ranked))
        quotas[name] = take
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
    while remaining > 0:
        progressed = False
        for name in MOVER_STRATA:
            selected = {row["symbol"] for row in chosen}
            ranked = sorted(
                set(by_stratum[name]) - selected,
                key=lambda symbol: hashlib.sha256(f"{seed}:{name}:{symbol}".encode()).hexdigest(),
            )
            if not ranked:
                continue
            symbol = ranked[0]
            quotas[name] += 1
            chosen.append(
                {
                    "symbol": symbol,
                    "membership": name,
                    "sampling_seed": seed,
                    "inclusion_probability": min(1.0, quotas[name] / len(by_stratum[name])),
                }
            )
            remaining -= 1
            progressed = True
            if not remaining:
                break
        if not progressed:
            break
    for row in chosen:
        row["inclusion_probability"] = min(
            1.0, quotas[row["membership"]] / len(by_stratum[row["membership"]])
        ) if by_stratum[row["membership"]] else 0.0
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
                 "native_capture_bytes": NATIVE_CAPTURE_BYTES, "native_metadata_bytes": NATIVE_METADATA_BYTES,
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
        "source_config_path": lineage.get("source_config_path"),
        "entitlement_receipt_path": lineage.get("entitlement_receipt_path"),
        "entitlement_receipt_sha256": lineage.get("entitlement_receipt_sha256"),
        "dependency_stage_root": lineage.get("dependency_stage_root"),
        "dependency_stage_receipt_path": lineage.get("dependency_stage_receipt_path"),
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
    if contract.get("source_config_path") != lineage.get("source_config_path"):
        raise Ops09Error("OPS09 immutable request contract source-config path changed")
    if contract.get("dependency_stage_root") != lineage.get("dependency_stage_root"):
        raise Ops09Error("OPS09 immutable request contract dependency stage changed")
    if contract.get("dependency_stage_receipt_path") != lineage.get("dependency_stage_receipt_path"):
        raise Ops09Error("OPS09 immutable request contract dependency receipt changed")
    if contract.get("repository") != plan.get("repository") or contract.get("toolchain") != plan.get("toolchain"):
        raise Ops09Error("OPS09 immutable request contract runtime identity changed")
    if contract.get("provider") != "alpaca" or contract.get("feed") != "sip" or contract.get("endpoints") != ["bars", "corporate_actions"]:
        raise Ops09Error("OPS09 request parameters are outside the allowed provider scope")
    alias = contract.get("capture_receipt_hash_alias")
    if not isinstance(alias, dict) or alias.get("source_field") != "request_contract_sha256" or alias.get("target_cli_argument") != "--capture-receipt-hash" or alias.get("authenticated") is not True:
        raise Ops09Error("OPS09 capture hash alias is not typed and authenticated")


def _run_capture_unbudgeted(*, plan: dict[str, Any], session: dict[str, Any], contract: dict[str, Any],
                 scope: dict[str, Any], scope_path: Path, session_root: Path, fixture: Path | None,
                 execute: bool, timeout_seconds: int, decision_artifact: Path | None,
                 database_path: Path, as_of: str, downstream_max_bytes: int) -> dict[str, Any]:
    capture_root = session_root / "capture"
    census_path = session_root / "capture-census.json"
    _assert_budget(session_root.parent, CAPTURE_BYTES, "capture phase")
    movers = scope["scope"].get("scopes", {}).get("original_small_cap_gap", [])
    census_path.parent.mkdir(parents=True, exist_ok=True)
    census_path.write_text(json.dumps(movers, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    argument_list = [str(Path(plan["repository"]["root"]) / "scripts" / "ops09_pipeline.py"),
                     "--market-date", session["market_date"], "--census", str(census_path), "--output-root", str(capture_root),
                     "--source-config-hash", str(contract["source_config_sha256"]),
                      "--capture-receipt-hash", contract["request_contract_sha256"],
                      "--max-bytes", str(CAPTURE_BYTES),
                      "--repo-sha", str(plan["repository"]["code_sha"]), "--as-of", as_of,
                      "--database-path", str(database_path),
                      "--adapter-output-root", str(session_root / "ops06")]
    if decision_artifact is not None:
        argument_list += [
            "--decision-artifact", str(decision_artifact),
            "--downstream-max-bytes", str(downstream_max_bytes),
            "--in-memory-consumer",
        ]
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
        wrapper_environment = os.environ.copy()
        wrapper_environment["PSModulePath"] = (
            r"C:\Windows\System32\WindowsPowerShell\v1.0\Modules;"
            r"C:\Program Files\WindowsPowerShell\Modules"
        )
        # Keep the observer's numerical dependency thread fan-out inside the
        # native Job Object budget; this does not alter unrelated callers.
        wrapper_environment["OPENBLAS_NUM_THREADS"] = "1"
        wrapper_environment["OMP_NUM_THREADS"] = "1"
        wrapper_environment["MKL_NUM_THREADS"] = "1"
        # Keep interpreter and atomic temporary files inside the admitted
        # session roots so the ledger accounts for them before dispatch.
        temporary_root = session_root / "temp"
        temporary_root.mkdir(parents=True, exist_ok=True)
        wrapper_environment["TEMP"] = str(temporary_root)
        wrapper_environment["TMP"] = str(temporary_root)
        completed = subprocess.run(
            args,
            cwd=plan["repository"]["root"],
            env=wrapper_environment,
            capture_output=True,
            text=True,
            timeout=max(1, timeout_seconds + 5),
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"status": "DEGRADED", "reason": "capture_wall_timeout", "command": args}
    output = (completed.stdout or "").splitlines()
    # The native runner durably mirrors child stdout into its observer log and
    # may not replay that stream to the PowerShell caller.  Read that existing
    # bounded log as the authoritative pipeline status channel; never infer a
    # successful downstream phase from the capture receipt alone.
    stdout_log = Path(log_root) / "ops05_observer.stdout.log"
    if stdout_log.is_file():
        output.extend(stdout_log.read_text(encoding="utf-8").splitlines())
    payload: dict[str, Any] = {}
    for line in reversed(output):
        try:
            candidate = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(candidate, dict) and candidate.get("decision_status") == "BOUND":
            payload = candidate
            break
        if not payload and isinstance(candidate, dict):
            payload = candidate
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
    return {"status": status, "command": args, "cli": payload, "pipeline": payload,
            "receipt": receipt,
            "capture_root": str(capture_root), "capture_receipt_sha256": _sha_file(capture_root / "receipt.json"),
            "capture_budget": {"page_count": page_count, "event_count": event_count,
                               "persisted_bytes": capture_bytes, "max_pages": MAX_PAGES,
                               "max_events": MAX_EVENTS, "max_bytes": CAPTURE_BYTES}}


def _run_capture(*, ledger: _ByteLedger, plan: dict[str, Any], session: dict[str, Any],
                 contract: dict[str, Any], scope: dict[str, Any], scope_path: Path,
                 session_root: Path, fixture: Path | None, execute: bool, timeout_seconds: int,
                 decision_artifact: Path | None, database_path: Path, as_of: str) -> dict[str, Any]:
    """Reserve capture plus all native output before starting the child."""
    phase = f"capture:{session['market_date']}"
    ledger.admit(phase, CAPTURE_BYTES + NATIVE_RESERVED_BYTES)
    downstream_max_bytes = max(
        0,
        MAX_BYTES - ledger._actual_bytes() - CAPTURE_BYTES - NATIVE_RESERVED_BYTES,
    )
    try:
        return _run_capture_unbudgeted(
            plan=plan, session=session, contract=contract, scope=scope, scope_path=scope_path,
            session_root=session_root, fixture=fixture, execute=execute,
            timeout_seconds=timeout_seconds, decision_artifact=decision_artifact,
            database_path=database_path, as_of=as_of,
            downstream_max_bytes=downstream_max_bytes,
        )
    finally:
        ledger.release(phase)


def _run_consumers(
    *, adapted: dict[str, Any], database_path: Path, session: dict[str, Any], repo_sha: str,
    in_memory: bool = False,
) -> dict[str, Any]:
    database_path.parent.mkdir(parents=True, exist_ok=True)
    connection: sqlite3.Connection | None = None
    if in_memory:
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA temp_store=MEMORY")
        connection.execute("PRAGMA journal_mode=MEMORY")
        store = SQLiteScanStore(database_path, connection_factory=lambda: connection)
    else:
        store = SQLiteScanStore(database_path)
    try:
        store.initialize()
        daily = run_alpha_v6_daily_monitor(store, market_date=session["market_date"], observation_source=adapted)
        weekly = run_alpha_v6_weekly_training(
            store, code_sha=repo_sha, market_date=session["market_date"], observation_source=adapted
        )
    finally:
        if connection is not None:
            connection.close()
    return {
        "daily": daily, "weekly": weekly, "database_path": str(database_path.resolve()),
        "database_mode": "in_memory" if in_memory else "disk", "isolated": True,
    }


def _admitted_repository_identity(state: dict[str, Any], repo_root: Path) -> dict[str, str]:
    """Use the identity admitted at prepare time; never spawn Git after admission."""
    identity = state.get("repository")
    if not isinstance(identity, dict):
        raise Ops09Error("OPS09 admitted repository identity is missing")
    root = Path(str(identity.get("root") or "")).resolve()
    if root != repo_root.resolve():
        raise Ops09Error("OPS09 repository root changed after admission")
    code_sha = str(identity.get("code_sha") or "").lower()
    tree_sha = str(identity.get("tree_sha") or "").lower()
    if len(code_sha) != 40 or any(char not in "0123456789abcdef" for char in code_sha):
        raise Ops09Error("OPS09 admitted repository commit identity is invalid")
    if len(tree_sha) != 40 or any(char not in "0123456789abcdef" for char in tree_sha):
        raise Ops09Error("OPS09 admitted repository tree identity is invalid")
    return {"root": str(root), "code_sha": code_sha, "tree_sha": tree_sha}


def _refresh_session_elapsed(
    *, state: dict[str, Any], session: dict[str, Any], now_utc: datetime,
    state_path: Path, invocation_started_mono: float, phase: str,
) -> int:
    """Persist session elapsed time before every phase and return remaining seconds."""
    started = session.get("session_started_at")
    if not started:
        session["session_started_at"] = now_utc.isoformat()
        started = session["session_started_at"]
    try:
        started_wall = datetime.fromisoformat(str(started).replace("Z", "+00:00")).timestamp()
    except ValueError as exc:
        raise Ops09Error("OPS09 session elapsed clock is invalid") from exc
    persisted = float(session.get("elapsed_seconds") or 0.0)
    wall_elapsed = max(0.0, now_utc.timestamp() - started_wall)
    monotonic_elapsed = max(0.0, time.monotonic() - invocation_started_mono)
    elapsed = max(persisted, wall_elapsed, monotonic_elapsed)
    session["elapsed_seconds"] = round(elapsed, 6)
    session["last_phase"] = phase
    _atomic_json(state_path, state)
    return max(0, int(MAX_WALL_SECONDS - elapsed))


def _resume_ops09_unlocked(*, output_root: Path, input_root: Path, scope_root: Path, database_root: Path,
                           repo_root: Path, execute: bool = False, now: datetime | None = None,
                           fixture_root: Path | None = None, decision_root: Path | None = None) -> dict[str, Any]:
    output_root = output_root.resolve(); state_path = output_root / "cohort-state.json"
    state = _read_object(state_path, "OPS09 cohort state")
    if state.get("schema_version") != OPS09_STATE_SCHEMA or state.get("mode") != "ops09":
        raise Ops09Error("OPS09 state identity is invalid")
    identity = _admitted_repository_identity(state, repo_root)
    if state.get("database_root") != str(database_root.resolve()) or database_root.resolve() == Path(r"C:\r\dawnstrike-state\shadow_real.sqlite").resolve():
        raise Ops09Error("OPS09 database root is not isolated")
    now_utc = now or datetime.now(UTC)
    invocation_started_mono = time.monotonic()
    stop_path = output_root / ".cohort.stop"
    due_indices = [
        index for index, candidate in enumerate(state["sessions"])
        if candidate.get("status") not in {"COMPLETE", "MISSED_SESSION"} and _eligible(candidate, now_utc)
    ]
    if len(due_indices) > 1:
        for missed_index in due_indices[:-1]:
            missed = state["sessions"][missed_index]
            missed.update({"status": "MISSED_SESSION", "reason": "prior due date was not collected; no automatic backfill"})
        due_indices = due_indices[-1:]
    due_index = due_indices[0] if due_indices else None
    for index, session in enumerate(state["sessions"]):
        if stop_path.exists():
            state["status"] = "STOPPED"
            state["stop_reason"] = "operator_stop_requested"
            for pending in state["sessions"][index:]:
                if pending.get("status") in {"EXPECTED", "READY", "RUNNING"}:
                    pending.update({"status": "STOPPED", "operator_intervention": "stop marker observed"})
            _atomic_json(state_path, state)
            return state
        if session.get("status") in {"COMPLETE", "MISSED_SESSION"} or index != due_index: continue
        if not _eligible(session, now_utc):
            session.update({"status": "EXPECTED", "reason": "awaiting session close and delayed-source grace"}); continue
        scope_path = Path(session["scope_path"])
        if not scope_path.is_file():
            session.update({"status": "MISSED_SESSION", "reason": "date-bound scope missing", "missing_input": "original_scope"}); continue
        try:
            scope = validate_ops09_scope(scope_path, expected_date=session["market_date"])
        except Ops09Error as exc:
            session.update({"status": "DEGRADED", "reason": str(exc), "operator_intervention": "repair exact date-bound scope"}); continue
        remaining = _refresh_session_elapsed(
            state=state, session=session, now_utc=now_utc,
            state_path=state_path, invocation_started_mono=invocation_started_mono, phase="before_capture",
        )
        if remaining < 1:
            state["status"] = "DEGRADED"; state["reason"] = "cohort wall-time budget exhausted"
            _atomic_json(state_path, state)
            return state
        session_root = output_root / session["market_date"]; session_root.mkdir(parents=True, exist_ok=True)
        database_path = database_root / f"{session['market_date']}.sqlite"
        # Each expected date owns an independent durable ledger.  A cohort-wide
        # ledger would allow an earlier date's output to consume a later date's
        # budget and would make retry/restart accounting ambiguous.
        ledger = _ByteLedger(
            output_root=session_root,
            database_root=database_path,
            identity={
                "cohort_id": state.get("cohort_id"),
                "market_date": session["market_date"],
                "output_root": str(session_root.resolve()),
                "database_root": str(database_path.resolve()),
                "repository": identity,
            },
        )
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
        decision_path = (decision_root.resolve() / session["market_date"] / "decisions.json") if decision_root else None
        capture = _run_capture(
            ledger=ledger, plan=state, session=session, contract=contract, scope=scope, scope_path=scope_path,
            session_root=session_root, fixture=fixture, execute=execute, timeout_seconds=remaining,
            decision_artifact=decision_path if decision_path is not None and decision_path.is_file() else None,
            database_path=database_path,
            as_of=state["expected_sessions"][index]["end_utc"],
        )
        session.update({"capture": {k: v for k, v in capture.items() if k not in {"receipt"}},
                        "request_contract_sha256": contract["request_contract_sha256"]})
        if capture.get("status") != "CAPTURED":
            session.update({"status": "PARTIAL" if capture.get("status") == "PARTIAL" else "DEGRADED", "decision_eligibility": "ZERO"}); continue
        if capture.get("pipeline", {}).get("decision_status") == "BOUND":
            pipeline = capture["pipeline"]
            session.pop("reason", None)
            session.update({
                "status": "COMPLETE", "decision_status": "BOUND",
                "decision_eligibility": "DELAYED_LABEL_ONLY",
                "ops06": {"adapter_output_root": pipeline.get("adapter_output_root"), "label_count": pipeline.get("label_count")},
                "consumers": pipeline.get("consumers", {}),
                "coverage_class": "delayed_historical_label_only",
                "consumer_cadence": "existing_public_daily_monitor_and_existing_weekly_due_or_not_due",
            })
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
                "ledger_scope": "single_market_date",
                "database_mode": pipeline.get("consumers", {}).get("database_mode", "in_memory"),
            }
            if (session_bytes > MAX_BYTES or capture_tree_bytes > CAPTURE_BYTES
                    or downstream_tree_bytes > DOWNSTREAM_BYTES):
                session.update({"status": "DEGRADED", "decision_eligibility": "ZERO",
                                "reason": "session artifact budget exceeded"})
            _atomic_json(session_root / "session-receipt.json", session)
            _atomic_json(state_path, state)
            continue
        if decision_path is None or not decision_path.is_file():
            session.update({"status": "PARTIAL", "decision_status": "MISSING_INPUT", "decision_eligibility": "ZERO", "reason": "raw capture retained; decision artifact missing"}); continue
        # A CAPTURED child that did not return BOUND is never completed by a
        # parent-side adapter or consumer fallback.  That would escape the
        # native admission boundary and make the receipt untrustworthy.
        session.update({
            "status": "DEGRADED",
            "decision_status": "CONSUMER_INCOMPLETE",
            "decision_eligibility": "ZERO",
            "reason": "guarded child did not complete adapter and consumers",
        })
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
