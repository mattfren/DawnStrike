"""Frozen, separately versioned PaperOps shadow challenger execution.

The runner reuses the hardened PaperOps fill/risk/close primitives, but keeps
candidate orders and positions in a shadow namespace.  Candidate account,
ledger, and calendar truth are isolated by the exact strategy/version/policy
triple.  Nothing in this module changes champion code or enables broker orders.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import re
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from intraday_scanner.market_calendar import MARKET_TIMEZONE
from intraday_scanner.v2.indicators import atr, sma
from intraday_scanner.v2.paper_ops import engine as paper_engine
from intraday_scanner.v2.paper_ops.models import (
    PaperJobPhase,
    PaperPickDecision,
    PaperRunMode,
    StrategyCalendarRow,
    StrategyPaperAccount,
)
from intraday_scanner.v2.paper_ops.storage import (
    append_jsonl_unique,
    exclusive_file_lock,
    read_json,
    read_jsonl,
    upsert_rows,
    write_json,
)
from intraday_scanner.v2.risk import RiskSettings
from intraday_scanner.v2.scanner import run_latest_scan
from intraday_scanner.v2.strategies import StrategySignal, StrategySpec, build_strategy_catalog

JsonDict = dict[str, Any]

SHADOW_REGISTRY_SCHEMA = "v2.paper_ops_challenger_registry.v1"
SHADOW_MANIFEST_SCHEMA = "v2.paper_ops_shadow_registration.v1"
REGISTERED_CHALLENGER_SCHEMA = "v2.paper_ops_registered_challenger.v1"
REGISTRATION_EVENT_SCHEMA = "v2.paper_ops_shadow_registration_event.v1"
SHADOW_RUN_SCHEMA = "v2.paper_ops_shadow_run.v1"
IMPLEMENTATION_KIND = "parent_signal_filter_v1"
_SAFE_ID = re.compile(r"^[a-z0-9][a-z0-9_.-]{2,80}$")


def initialize_shadow_registry(
    *,
    output_root: Path = Path("data/v2_paper_ops_live"),
) -> JsonDict:
    """Create an empty registry and editable registration template, idempotently."""

    state = output_root / "state"
    registry_path = state / "strategy_challenger_registry.json"
    if not registry_path.exists():
        write_json(
            registry_path,
            {"schema_version": SHADOW_REGISTRY_SCHEMA, "challengers": []},
        )
    else:
        _read_registry(output_root)
    template_path = state / "shadow_challenger_registration_template.json"
    if not template_path.exists():
        write_json(template_path, _registration_template(output_root))
    return {
        "status": "initialized",
        "registry": str(registry_path),
        "template": str(template_path),
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }


def register_shadow_challenger(
    *,
    manifest_path: Path,
    output_root: Path = Path("data/v2_paper_ops_live"),
) -> JsonDict:
    """Validate and freeze one shadow candidate without changing its champion."""

    initialize_shadow_registry(output_root=output_root)
    raw = read_json(manifest_path, {})
    if not isinstance(raw, dict):
        raise ValueError("shadow registration manifest must be a JSON object")
    registry_path = output_root / "state" / "strategy_challenger_registry.json"
    lock_path = registry_path.with_name(f".{registry_path.name}.lock")
    with exclusive_file_lock(lock_path):
        registry = _read_registry(output_root)
        rows = [_dict(row) for row in registry["challengers"]]
        challenger_id = str(raw.get("challenger_id") or "")
        existing_id = next(
            (
                row
                for row in rows
                if row.get("challenger_id") == challenger_id
            ),
            None,
        )
        if existing_id is not None:
            registration = _freeze_registration(
                dict(raw),
                output_root,
                registered_at=str(existing_id.get("registered_at") or ""),
            )
            if existing_id != registration:
                raise ValueError(
                    "challenger_id is already frozen with different semantics; "
                    "register a new ID and version"
                )
            return {
                "status": "already_registered",
                "challenger": registration,
                "registry": str(registry_path),
            }
        orphan = _matching_registration_event(dict(raw), output_root)
        if orphan is not None:
            registration = orphan
            if any(
                row.get("strategy_id") == registration["strategy_id"]
                and row.get("candidate_strategy_version")
                == registration["candidate_strategy_version"]
                for row in rows
            ):
                raise ValueError("candidate strategy version is already registered")
            _assert_no_preexisting_candidate_evidence(output_root, registration)
            rows.append(registration)
            rows.sort(key=lambda row: str(row.get("challenger_id") or ""))
            write_json(
                registry_path,
                {"schema_version": SHADOW_REGISTRY_SCHEMA, "challengers": rows},
            )
            return {
                "status": "recovered_registration",
                "challenger": registration,
                "registry": str(registry_path),
                "automatic_promotion_enabled": False,
                "broker_execution_allowed": False,
            }
        registration = _freeze_registration(
            dict(raw),
            output_root,
            registered_at=datetime.now(timezone.utc).isoformat(),
        )
        if any(
            row.get("strategy_id") == registration["strategy_id"]
            and row.get("candidate_strategy_version")
            == registration["candidate_strategy_version"]
            for row in rows
        ):
            raise ValueError("candidate strategy version is already registered")
        _assert_no_preexisting_candidate_evidence(output_root, registration)
        registration_event = {
            "schema_version": REGISTRATION_EVENT_SCHEMA,
            "event_type": "shadow_challenger_registered",
            "registration_event_id": registration["registration_id"],
            "registered_at": registration["registered_at"],
            "registration": registration,
        }
        append_jsonl_unique(
            _registration_ledger_path(output_root),
            [registration_event],
            "registration_event_id",
        )
        event_reasons = _registration_event_reasons(registration, output_root)
        if event_reasons:
            raise ValueError("registration event integrity failed: " + " | ".join(event_reasons))
        rows.append(registration)
        rows.sort(key=lambda row: str(row.get("challenger_id") or ""))
        write_json(
            registry_path,
            {"schema_version": SHADOW_REGISTRY_SCHEMA, "challengers": rows},
        )
    return {
        "status": "registered",
        "challenger": registration,
        "registry": str(registry_path),
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }


def verify_registration_integrity(
    registration: JsonDict,
    *,
    output_root: Path,
) -> tuple[str, ...]:
    """Recompute immutable implementation/parent fingerprints for a registry row."""

    reasons: list[str] = []
    try:
        parent = _parent_strategy(registration, output_root)
    except ValueError as exc:
        return (str(exc),)
    if registration.get("schema_version") != REGISTERED_CHALLENGER_SCHEMA:
        reasons.append("registered challenger schema is unsupported")
    if registration.get("status") != "shadow":
        reasons.append("registered challenger status is not shadow")
    if registration.get("implementation_source_sha256") != _implementation_source_sha256():
        reasons.append("shadow implementation source changed after freeze")
    if registration.get("parent_logic_sha256") != _strategy_logic_sha256(parent):
        reasons.append("parent strategy logic changed after challenger freeze")
    parent_semantics = paper_engine._strategy_semantics_fingerprint(parent)
    if registration.get("champion_strategy_semantics_fingerprint") != parent_semantics:
        reasons.append("champion strategy semantics changed after challenger freeze")
    try:
        candidate = _build_candidate_strategy(registration, output_root)
    except ValueError as exc:
        reasons.append(str(exc))
    else:
        expected_semantics = paper_engine._strategy_semantics_fingerprint(candidate)
        if registration.get("candidate_strategy_semantics_fingerprint") != expected_semantics:
            reasons.append("candidate strategy semantics changed after challenger freeze")
    registered_at = str(registration.get("registered_at") or "")
    if not registered_at:
        reasons.append("challenger registration is missing registered_at")
    else:
        try:
            _parse_utc(registered_at, field="registered_at")
        except ValueError as exc:
            reasons.append(str(exc))
    expected = _logic_artifact_sha256(registration)
    if registration.get("logic_artifact_sha256") != expected:
        reasons.append("challenger logic artifact hash does not match frozen registration")
    if registration.get("registration_id") != _registration_id(registration):
        reasons.append("challenger registration_id does not match immutable registration")
    reasons.extend(_registration_event_reasons(registration, output_root))
    return tuple(reasons)


def run_shadow_day(
    *,
    run_date: date,
    mode: PaperRunMode = PaperRunMode.FORWARD,
    output_root: Path = Path("data/v2_paper_ops_live"),
    allow_fetch: bool = True,
) -> JsonDict:
    """Run all frozen candidates on the champion's exact completed data snapshot."""

    if mode not in {PaperRunMode.FORWARD, PaperRunMode.REPLAY}:
        raise ValueError("shadow execution supports only forward or replay mode")
    initialize_shadow_registry(output_root=output_root)
    paper_engine._recover_pending_transaction(
        paper_engine.PaperOpsPaths.create(output_root)
    )
    registry = _read_registry(output_root)
    registrations = [_dict(row) for row in registry["challengers"]]
    source = _champion_source_context(output_root, run_date, mode)
    if not registrations:
        _assert_pre_shadow_truth_gates(output_root)
        result: JsonDict = {
            "schema_version": SHADOW_RUN_SCHEMA,
            "status": "no_registered_challengers",
            "date": run_date.isoformat(),
            "mode": mode.value,
            "run_id": source["run_id"],
            "data_snapshot_id": source["data_snapshot_id"],
            "challenger_count": 0,
            "registered_challenger_count": 0,
            "eligible_challenger_count": 0,
            "skipped_challenger_count": 0,
            "results": [],
            "skipped_challengers": [],
            "recovered_completed_transactions": False,
            "research_only": True,
            "automatic_promotion_enabled": False,
            "broker_execution_allowed": False,
        }
        write_json(
            output_root / "reports" / "daily" / f"shadow_{mode.value}_{run_date}.json",
            result,
        )
        return result

    config = paper_engine._config(paper_engine.PaperOpsPaths.create(output_root))
    eligible_registrations: list[JsonDict] = []
    skipped_challengers: list[JsonDict] = []
    for registration in registrations:
        reasons = verify_registration_integrity(registration, output_root=output_root)
        if reasons:
            raise ValueError(
                f"invalid frozen challenger {registration.get('challenger_id')}: "
                + " | ".join(reasons)
            )
        frozen_date = _market_date_from_timestamp(
            str(registration["frozen_at"]), field="frozen_at"
        )
        registered_date = _market_date_from_timestamp(
            str(registration["registered_at"]), field="registered_at"
        )
        eligible_after = max(frozen_date, registered_date)
        if run_date <= eligible_after:
            skipped_challengers.append(
                _not_yet_eligible_challenger_result(
                    registration=registration,
                    run_date=run_date,
                    eligible_after=eligible_after,
                )
            )
            continue
        eligible_registrations.append(registration)
    run = paper_engine._paper_run(
        run_date=run_date,
        mode=mode,
        data_snapshot_id=str(source["data_snapshot_id"]),
    )
    if run.run_id != source["run_id"]:
        raise ValueError("shadow run lineage does not match champion run lineage")
    if not eligible_registrations:
        _assert_pre_shadow_truth_gates(output_root)
        paper_engine.calendar(output_root=output_root)
        result = _shadow_day_result(
            run,
            [],
            recovered_only=False,
            skipped_challengers=skipped_challengers,
        )
        write_json(
            output_root / "reports" / "daily" / f"shadow_{mode.value}_{run_date}.json",
            result,
        )
        return result
    recovered = _recover_completed_shadow_manifests(
        output_root=output_root,
        run=run,
        registrations=eligible_registrations,
        config=config,
    )
    if len(recovered) == len(eligible_registrations):
        paper_engine.calendar(output_root=output_root)
        result = _shadow_day_result(
            run,
            recovered,
            recovered_only=True,
            skipped_challengers=skipped_challengers,
        )
        write_json(
            output_root / "reports" / "daily" / f"shadow_{mode.value}_{run_date}.json",
            result,
        )
        return result

    _assert_pre_shadow_truth_gates(output_root)
    dataset, manifest, warnings = paper_engine._load_dataset_for_mode(
        run_date=run_date,
        mode=mode,
        allow_fetch=allow_fetch,
        universe_symbols=config.universe_symbols,
        allow_single_provider_forward=config.allow_single_provider_forward,
        data_truth_root=(
            output_root / "data_truth_replay"
            if mode is PaperRunMode.REPLAY
            else Path("data/v2_data_truth")
        ),
    )
    if manifest.snapshot_id != source["data_snapshot_id"]:
        raise ValueError(
            "shadow dataset snapshot differs from the completed champion run; "
            "candidate execution is blocked"
        )
    if manifest.accepted_end != run_date.isoformat():
        raise ValueError("shadow dataset does not end on the exact completed run date")
    if tuple(sorted(dataset.symbols)) != tuple(sorted(config.universe_symbols)):
        raise ValueError("shadow dataset universe differs from the configured champion universe")

    prepared = [
        (registration, _build_candidate_strategy(registration, output_root))
        for registration in eligible_registrations
    ]
    results: list[JsonDict] = []
    for registration, strategy in prepared:
        results.append(
            _run_one_challenger(
                output_root=output_root,
                run_date=run_date,
                mode=mode,
                registration=registration,
                strategy=strategy,
                dataset=dataset,
                run=run,
                config=config,
                warnings=tuple(dict.fromkeys((*warnings, "shadow_only_candidate"))),
            )
        )
    paper_engine.calendar(output_root=output_root)
    result = _shadow_day_result(
        run,
        results,
        recovered_only=False,
        skipped_challengers=skipped_challengers,
    )
    write_json(
        output_root / "reports" / "daily" / f"shadow_{mode.value}_{run_date}.json",
        result,
    )
    return result


def _shadow_day_result(
    run: Any,
    results: list[JsonDict],
    *,
    recovered_only: bool,
    skipped_challengers: list[JsonDict] | None = None,
) -> JsonDict:
    skipped = list(skipped_challengers or [])
    status = "passed"
    if skipped and results:
        status = "passed_with_ineligible_challengers"
    elif skipped:
        status = "skipped_no_eligible_challengers"
    return {
        "schema_version": SHADOW_RUN_SCHEMA,
        "status": status,
        "date": run.run_date,
        "mode": run.mode.value,
        "run_id": run.run_id,
        "data_snapshot_id": run.data_snapshot_id,
        "challenger_count": len(results),
        "registered_challenger_count": len(results) + len(skipped),
        "eligible_challenger_count": len(results),
        "skipped_challenger_count": len(skipped),
        "results": results,
        "skipped_challengers": skipped,
        "recovered_completed_transactions": recovered_only,
        "research_only": True,
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }


def _not_yet_eligible_challenger_result(
    *,
    registration: JsonDict,
    run_date: date,
    eligible_after: date,
) -> JsonDict:
    """Describe a forward-only timing skip without creating candidate evidence."""

    return {
        "challenger_id": str(registration["challenger_id"]),
        "strategy_id": str(registration["strategy_id"]),
        "candidate_strategy_version": str(registration["candidate_strategy_version"]),
        "execution_policy_version": str(registration["execution_policy_version"]),
        "status": "skipped_not_yet_eligible",
        "reason_code": "forward_evidence_window_not_open",
        "reason": (
            "shadow evidence starts strictly after both frozen_at and append-only "
            "registered_at dates"
        ),
        "run_date": run_date.isoformat(),
        "frozen_at": str(registration["frozen_at"]),
        "registered_at": str(registration["registered_at"]),
        "eligibility_cutoff_date": eligible_after.isoformat(),
        "earliest_eligible_calendar_date": (
            eligible_after + timedelta(days=1)
        ).isoformat(),
        # Compatibility alias.  The earliest eligible calendar date can be a
        # weekend or market holiday, so it is not necessarily an actual run.
        "first_eligible_run_date": (eligible_after + timedelta(days=1)).isoformat(),
        "evidence_status": "not_applicable_not_yet_eligible",
        "evidence_created": False,
        "return_status": "not_applicable_no_evidence",
        "daily_return_pct": None,
        "after_cost_return_pct": None,
        "research_only": True,
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }


def _recover_completed_shadow_manifests(
    *,
    output_root: Path,
    run: Any,
    registrations: list[JsonDict],
    config: Any,
) -> list[JsonDict]:
    recovered: list[JsonDict] = []
    paths = paper_engine.PaperOpsPaths.create(output_root)
    ledger_events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    for registration in registrations:
        challenger_id = str(registration["challenger_id"])
        manifest_path = _shadow_manifest_path(
            output_root,
            run.mode,
            date.fromisoformat(run.run_date),
            challenger_id,
        )
        completed = _completed_shadow_manifest(
            manifest_path,
            run=run,
            registration=registration,
        )
        if completed is None:
            continue
        key = _registration_key(registration)
        state_dir = output_root / "state" / "shadow" / challenger_id
        pending_path = state_dir / f"{run.mode.value}_pending_orders.json"
        positions_path = state_dir / f"{run.mode.value}_open_positions.json"
        shadow_account_path = state_dir / f"{run.mode.value}_account.json"
        decisions_path = _shadow_decisions_path(
            output_root,
            run.mode,
            date.fromisoformat(run.run_date),
            challenger_id,
        )
        missing_state = [
            path.name
            for path in (
                pending_path,
                positions_path,
                shadow_account_path,
                decisions_path,
            )
            if not path.is_file()
        ]
        if missing_state:
            raise ValueError(
                "completed shadow manifest is missing transaction state: "
                + ", ".join(missing_state)
            )
        pending = _load_shadow_rows(
            pending_path,
            key,
        )
        positions = _load_shadow_rows(
            positions_path,
            key,
        )
        decisions = read_json(decisions_path, [])
        expected_coverage = _manifest_count(completed, "decision_coverage")
        if not isinstance(decisions, list) or any(
            not isinstance(row, dict) for row in decisions
        ):
            raise ValueError("completed shadow decision artifact is malformed")
        decision_rows = [dict(row) for row in decisions]
        if len(decision_rows) != expected_coverage:
            raise ValueError(
                "completed shadow manifest decision coverage state is inconsistent"
            )
        decision_symbols = tuple(
            sorted(str(row.get("symbol") or "") for row in decision_rows)
        )
        expected_symbols = tuple(sorted(config.universe_symbols))
        if (
            decision_symbols != expected_symbols
            or len(decision_symbols) != len(set(decision_symbols))
        ):
            raise ValueError(
                "completed shadow decision artifact does not cover the exact universe"
            )
        if any(
            _payload_key(row) != key
            or row.get("challenger_id") != challenger_id
            or row.get("logic_artifact_sha256")
            != registration.get("logic_artifact_sha256")
            or row.get("mode") != run.mode.value
            or row.get("run_id") != run.run_id
            for row in decision_rows
        ):
            raise ValueError(
                "completed shadow decision artifact has cross-lineage contamination"
            )
        if completed.get("decision_artifact_sha256") != _sha256(decision_rows):
            raise ValueError("completed shadow decision artifact hash mismatch")
        if completed.get("decision_symbols_sha256") != _sha256(list(expected_symbols)):
            raise ValueError("completed shadow decision universe hash mismatch")

        run_events = _shadow_run_events(
            ledger_events,
            run=run,
            key=key,
            challenger_id=challenger_id,
        )
        _validate_shadow_manifest_event_counts(
            completed,
            run_events=run_events,
            expected_decisions=expected_coverage,
        )
        later_events = [
            event
            for event in ledger_events
            if event.get("mode") == run.mode.value
            and str(event.get("trade_date") or "") > run.run_date
            and isinstance(event.get("payload"), dict)
            and _payload_key(_dict(event.get("payload"))) == key
        ]
        if later_events:
            raise ValueError(
                "cannot recover an older shadow manifest after later candidate state exists"
            )
        rebuilt_pending, rebuilt_positions, rebuilt_account = _rebuild_shadow_state(
            ledger_events,
            mode=run.mode,
            key=key,
            challenger_id=challenger_id,
            starting_equity=config.starting_equity,
        )
        if _state_rows_by_id(pending, "order_id") != _state_rows_by_id(
            rebuilt_pending, "order_id"
        ):
            raise ValueError("completed shadow pending state differs from ledger lifecycle")
        if _state_rows_by_id(positions, "position_id") != _state_rows_by_id(
            rebuilt_positions, "position_id"
        ):
            raise ValueError("completed shadow position state differs from ledger lifecycle")
        if _manifest_count(completed, "pending_orders") != len(pending):
            raise ValueError("completed shadow manifest pending order count mismatch")
        if _manifest_count(completed, "open_positions") != len(positions):
            raise ValueError("completed shadow manifest open position count mismatch")

        _assert_verified_shadow_lifecycle(output_root, key, challenger_id)
        account_state = read_json(
            paper_engine._paper_accounts_path(
                paths,
                run.mode,
            ),
            {},
        )
        account_rows = (
            account_state.get("accounts", [])
            if isinstance(account_state, dict)
            else []
        )
        exact_accounts = [
            row
            for row in account_rows
            if isinstance(row, dict) and _payload_key(row) == key
        ]
        if len(exact_accounts) != 1:
            raise ValueError(
                "completed shadow manifest has no unique canonical account state"
            )
        account = _load_shadow_account(
            output_root,
            run.mode,
            key,
            config.starting_equity,
            str(registration["candidate_strategy_semantics_fingerprint"]),
        )
        shadow_account = read_json(shadow_account_path, {})
        if (
            not isinstance(shadow_account, dict)
            or shadow_account.get("account") != account.to_dict()
            or account.to_dict() != rebuilt_account.to_dict()
        ):
            raise ValueError("completed shadow manifest account state is inconsistent")
        calendar_warnings = completed.get("calendar_warnings")
        if not isinstance(calendar_warnings, list) or any(
            not isinstance(item, str) for item in calendar_warnings
        ):
            raise ValueError("completed shadow manifest calendar warnings are malformed")
        expected_calendar = _candidate_calendar_row(
            output_root=output_root,
            run=run,
            registration=registration,
            account=rebuilt_account,
            pending=rebuilt_pending,
            positions=rebuilt_positions,
            warnings=tuple(calendar_warnings),
        )
        existing_calendar = [
            row
            for row in paper_engine._read_calendar_rows(
                paths
            )
            if str(row.get("date") or "") == run.run_date
            and str(row.get("mode") or "") == run.mode.value
            and _payload_key(row) == key
        ]
        if len(existing_calendar) > 1:
            raise ValueError("completed shadow transaction has duplicate calendar rows")
        if existing_calendar:
            row = existing_calendar[0]
            if _calendar_csv_projection(row) != _calendar_csv_projection(
                expected_calendar
            ):
                raise ValueError(
                    "completed shadow transaction calendar differs from full ledger rebuild"
                )
        else:
            _write_candidate_calendar(
                output_root=output_root,
                run=run,
                registration=registration,
                account=rebuilt_account,
                pending=rebuilt_pending,
                positions=rebuilt_positions,
                warnings=tuple(calendar_warnings),
            )
        recovered.append({**completed, "manifest_path": str(manifest_path)})
    return recovered


def _manifest_count(manifest: JsonDict, field: str) -> int:
    value = manifest.get(field)
    if isinstance(value, bool):
        raise ValueError(f"completed shadow manifest {field} is invalid")
    try:
        parsed = int(str(value))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"completed shadow manifest {field} is invalid") from exc
    if parsed < 0 or str(parsed) != str(value):
        raise ValueError(f"completed shadow manifest {field} is invalid")
    return parsed


def _shadow_run_events(
    events: list[JsonDict],
    *,
    run: Any,
    key: tuple[str, str, str, str],
    challenger_id: str,
) -> list[JsonDict]:
    result: list[JsonDict] = []
    for event in events:
        payload = event.get("payload")
        if (
            event.get("mode") != run.mode.value
            or str(event.get("trade_date") or "") != run.run_date
            or not isinstance(payload, dict)
            or _payload_key(dict(payload)) != key
        ):
            continue
        if (
            event.get("run_id") != run.run_id
            or event.get("strategy_id") != key[0]
            or payload.get("challenger_id") != challenger_id
        ):
            raise ValueError("completed shadow ledger has cross-lineage contamination")
        result.append(dict(event))
    return result


def _validate_shadow_manifest_event_counts(
    manifest: JsonDict,
    *,
    run_events: list[JsonDict],
    expected_decisions: int,
) -> None:
    expected_by_manifest = {
        "orders_created": "paper_order_created",
        "orders_blocked": "paper_order_blocked",
        "fills": "paper_fill",
        "closes": "paper_position_closed",
    }
    for field, event_type in expected_by_manifest.items():
        observed = sum(
            event.get("event_type") == event_type for event in run_events
        )
        if _manifest_count(manifest, field) != observed:
            raise ValueError(f"completed shadow manifest {field} event count mismatch")
    opened = sum(
        event.get("event_type") == "paper_position_opened" for event in run_events
    )
    if opened != _manifest_count(manifest, "fills"):
        raise ValueError("completed shadow position-open event count mismatch")
    decisions = sum(
        event.get("event_type")
        in {"paper_pick_decision", "paper_no_setup_decision"}
        for event in run_events
    )
    if decisions != expected_decisions:
        raise ValueError("completed shadow decision ledger coverage mismatch")
    if _manifest_count(manifest, "transaction_event_count") != len(run_events):
        raise ValueError("completed shadow transaction event count mismatch")
    event_ids = sorted(str(event.get("event_id") or "") for event in run_events)
    if any(not event_id for event_id in event_ids) or len(event_ids) != len(set(event_ids)):
        raise ValueError("completed shadow transaction event identities are invalid")
    if manifest.get("transaction_event_ids_sha256") != _sha256(event_ids):
        raise ValueError("completed shadow transaction event identity hash mismatch")
    if manifest.get("transaction_events_sha256") != _sha256(run_events):
        raise ValueError("completed shadow transaction event content hash mismatch")


def _rebuild_shadow_state(
    events: list[JsonDict],
    *,
    mode: PaperRunMode,
    key: tuple[str, str, str, str],
    challenger_id: str,
    starting_equity: float,
) -> tuple[list[JsonDict], list[JsonDict], StrategyPaperAccount]:
    pending: dict[str, JsonDict] = {}
    positions: dict[str, JsonDict] = {}
    realized = 0.0
    for event in events:
        payload = event.get("payload")
        if (
            event.get("mode") != mode.value
            or not isinstance(payload, dict)
            or _payload_key(dict(payload)) != key
        ):
            continue
        if payload.get("challenger_id") != challenger_id:
            raise ValueError("shadow ledger series contains another challenger identity")
        row = _shadow_state_payload(dict(payload))
        event_type = str(event.get("event_type") or "")
        if event_type == "paper_order_created":
            order_id = str(row.get("order_id") or "")
            if not order_id or order_id in pending:
                raise ValueError("shadow ledger contains an invalid duplicate order")
            pending[order_id] = row
        elif event_type == "paper_order_blocked":
            pending.pop(str(row.get("order_id") or ""), None)
        elif event_type == "paper_fill":
            order_id = str(row.get("order_id") or "")
            if order_id not in pending:
                raise ValueError("shadow ledger fill has no pending order")
            pending.pop(order_id)
        elif event_type == "paper_position_opened":
            position_id = str(row.get("position_id") or "")
            if not position_id or position_id in positions:
                raise ValueError("shadow ledger contains an invalid duplicate position")
            positions[position_id] = row
        elif event_type in {
            "paper_position_checked_no_action",
            "paper_position_marked_to_market",
        }:
            position_id = str(row.get("position_id") or "")
            if position_id not in positions:
                raise ValueError("shadow ledger mark has no opened position")
            positions[position_id] = row
        elif event_type == "paper_position_closed":
            position_id = str(row.get("position_id") or "")
            if position_id not in positions:
                raise ValueError("shadow ledger close has no opened position")
            positions.pop(position_id)
            net_pnl = row.get("net_pnl")
            if isinstance(net_pnl, bool):
                raise ValueError("shadow ledger close net_pnl is invalid")
            try:
                realized += float(str(net_pnl))
            except (TypeError, ValueError) as exc:
                raise ValueError("shadow ledger close net_pnl is invalid") from exc
    unrealized = sum(float(str(row.get("unrealized_pnl") or 0.0)) for row in positions.values())
    account = StrategyPaperAccount(
        strategy_id=key[0],
        strategy_version=key[1],
        starting_equity=starting_equity,
        current_equity=starting_equity + realized + unrealized,
        realized_pnl=realized,
        unrealized_pnl=unrealized,
        execution_policy_version=key[2],
        strategy_semantics_fingerprint=key[3],
    )
    return (
        [pending[item] for item in sorted(pending)],
        [positions[item] for item in sorted(positions)],
        account,
    )


def _shadow_state_payload(payload: JsonDict) -> JsonDict:
    result = dict(payload)
    result.pop("challenger_id", None)
    return result


def _state_rows_by_id(rows: list[JsonDict], id_field: str) -> dict[str, JsonDict]:
    result: dict[str, JsonDict] = {}
    for row in rows:
        identity = str(row.get(id_field) or "")
        if not identity or identity in result:
            raise ValueError(f"shadow state has invalid duplicate {id_field}")
        result[identity] = _shadow_state_payload(row)
    return result


def _assert_verified_shadow_lifecycle(
    output_root: Path,
    key: tuple[str, str, str, str],
    challenger_id: str,
) -> None:
    from intraday_scanner.v2.paper_ops.trade_blotter import _materialize_rows

    rows, warnings = _materialize_rows(output_root)
    if warnings:
        raise ValueError(
            "completed shadow lifecycle verification failed: " + " | ".join(warnings)
        )
    matching = [row for row in rows if _payload_key(row) == key]
    if any(str(row.get("challenger_id") or "") != challenger_id for row in matching):
        raise ValueError("completed shadow blotter has challenger identity contamination")


def _calendar_csv_projection(row: JsonDict) -> tuple[str, ...]:
    return tuple(_calendar_csv_value(row.get(field)) for field in paper_engine.CALENDAR_FIELDNAMES)


def _calendar_csv_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, tuple | list):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    return str(value)


def _run_one_challenger(
    *,
    output_root: Path,
    run_date: date,
    mode: PaperRunMode,
    registration: JsonDict,
    strategy: StrategySpec,
    dataset: Any,
    run: Any,
    config: Any,
    warnings: tuple[str, ...],
) -> JsonDict:
    key = _registration_key(registration)
    challenger_id = str(registration["challenger_id"])
    state_dir = output_root / "state" / "shadow" / challenger_id
    pending_path = state_dir / f"{mode.value}_pending_orders.json"
    positions_path = state_dir / f"{mode.value}_open_positions.json"
    manifest_path = _shadow_manifest_path(
        output_root, run.mode, date.fromisoformat(run.run_date), challenger_id
    )
    pending = _load_shadow_rows(pending_path, key)
    pending = paper_engine._repair_pending_order_rows(pending, dataset)
    position_rows = _load_shadow_rows(positions_path, key)
    candidate_semantics = str(
        registration["candidate_strategy_semantics_fingerprint"]
    )
    account = _load_shadow_account(
        output_root,
        mode,
        key,
        config.starting_equity,
        candidate_semantics,
    )
    accounts = {key[0]: account}
    completed = _completed_shadow_manifest(
        manifest_path,
        run=run,
        registration=registration,
    )
    if completed is not None:
        _write_candidate_calendar(
            output_root=output_root,
            run=run,
            registration=registration,
            account=account,
            pending=pending,
            positions=position_rows,
            warnings=warnings,
        )
        return {**completed, "manifest_path": str(manifest_path)}

    scan_output = run_latest_scan(
        dataset,
        (strategy,),
        {},
        risk_settings=RiskSettings(
            account_equity=max(account.current_equity, 0.0),
            risk_per_trade_pct=config.risk_per_trade_pct,
            max_position_pct=config.max_gross_exposure_pct,
            min_reward_risk=config.min_reward_risk,
            max_risk_per_trade_pct=config.risk_per_trade_pct,
        ),
        data_snapshot_id=run.data_snapshot_id,
        run_manifest_id=run.run_id,
    )
    picks = paper_engine._picks_from_scan(
        scan_output,
        (strategy,),
        run,
        config,
        warnings,
    )
    no_setup = [
        paper_engine._no_setup_decision(
            card,
            run,
            config,
            warnings,
            candidate_semantics,
        )
        for card in scan_output.no_setup
    ]
    observed_decision_symbols = tuple(
        sorted(
            [pick.symbol for pick in picks]
            + [str(row.get("symbol") or "") for row in no_setup]
        )
    )
    expected_decision_symbols = tuple(sorted(dataset.symbols))
    if (
        len(picks) + len(no_setup) != len(dataset.symbols)
        or observed_decision_symbols != expected_decision_symbols
        or len(observed_decision_symbols) != len(set(observed_decision_symbols))
    ):
        raise ValueError(
            f"shadow decision coverage mismatch for {challenger_id}: expected exact "
            f"universe {expected_decision_symbols}, observed {observed_decision_symbols}"
        )
    decision_rows: list[JsonDict] = [
        {
            **pick.to_dict(),
            "decision_status": pick.decision.value,
            "trade_return_eligible": pick.decision is PaperPickDecision.ACCEPTED,
            "trade_return_pct": None,
            "challenger_id": challenger_id,
            "logic_artifact_sha256": registration["logic_artifact_sha256"],
        }
        for pick in picks
    ] + [
        {
            **row,
            "challenger_id": challenger_id,
            "logic_artifact_sha256": registration["logic_artifact_sha256"],
        }
        for row in no_setup
    ]
    scan_events = (
        [
            paper_engine._event(
                run,
                PaperJobPhase.SCAN,
                pick.strategy_id,
                pick.symbol,
                "paper_pick_decision",
                pick.pick_id,
                {
                    **pick.to_dict(),
                    "challenger_id": challenger_id,
                    "logic_artifact_sha256": registration["logic_artifact_sha256"],
                },
            )
            for pick in picks
        ]
        + [
            paper_engine._event(
                run,
                PaperJobPhase.SCAN,
                str(row["strategy_id"]),
                str(row["symbol"]),
                "paper_no_setup_decision",
                str(row["decision_id"]),
                row,
            )
            for row in decision_rows
            if row.get("decision_status") == "no_setup"
        ]
    )

    ledger_events = read_jsonl(output_root / "ledger" / "paper_ledger.jsonl")
    existing_order_ids = {
        str(row.get("order_id")) for row in pending if row.get("order_id")
    } | {
        str(payload.get("order_id"))
        for event in ledger_events
        for payload in [event.get("payload")]
        if event.get("event_type") == "paper_order_created"
        and isinstance(payload, dict)
        and _payload_key(payload) == key
        and payload.get("order_id")
    }
    daily_net = paper_engine._daily_closed_net_by_strategy(
        ledger_events, run.run_date, mode
    ).get(key, 0.0)
    orders: list[Any] = []
    blocked: list[JsonDict] = []
    for pick in picks:
        if pick.decision is not PaperPickDecision.ACCEPTED:
            continue
        order = paper_engine._order_from_pick(
            pick,
            run,
            config,
            dataset,
            equity_basis=account.current_equity,
        )
        if order.order_id in existing_order_ids:
            continue
        reason = paper_engine._order_entry_block_reason(
            order,
            position_rows=position_rows,
            pending_rows=pending + [item.to_dict() for item in orders],
            account=account,
            config=config,
            daily_closed_net=daily_net,
        )
        if reason is None:
            orders.append(order)
            existing_order_ids.add(order.order_id)
        else:
            blocked.append(paper_engine._blocked_order_payload(order, reason, run))
    pending.extend(order.to_dict() for order in orders)
    enter_events = (
        [
            paper_engine._event(
                run,
                PaperJobPhase.ENTER,
                order.strategy_id,
                order.symbol,
                "paper_order_created",
                order.order_id,
                {**order.to_dict(), "challenger_id": challenger_id},
            )
            for order in orders
        ]
        + [
            paper_engine._event(
                run,
                PaperJobPhase.ENTER,
                str(row["strategy_id"]),
                str(row["symbol"]),
                "paper_order_blocked",
                f"{row['order_id']}:{row['reason']}",
                {**row, "challenger_id": challenger_id},
            )
            for row in blocked
        ]
    )

    terminal_orders: set[str] = set()
    fills: list[Any] = []
    new_positions: list[Any] = []
    fill_source_bars: dict[str, Any] = {}
    position_source_bars: dict[str, Any] = {}
    fill_blocks: list[JsonDict] = []
    pending_events: list[Any] = []
    for order in [paper_engine._order_from_row(row) for row in pending]:
        fill_bar = paper_engine._next_bar_after(
            dataset, order.symbol, order.signal_time, run_date
        )
        if fill_bar is None:
            pending_events.append(
                paper_engine._event(
                    run,
                    PaperJobPhase.CHECK,
                    order.strategy_id,
                    order.symbol,
                    "paper_order_pending_no_fill_data",
                    f"{order.order_id}:pending_check:{run_date}",
                    {
                        **paper_engine._pending_order_lifecycle_payload(order, run),
                        "challenger_id": challenger_id,
                    },
                )
            )
            continue
        if fill_bar.timestamp.date() < run_date:
            fill_blocks.append(
                paper_engine._blocked_order_payload(
                    order,
                    "missed_fill_session",
                    run,
                    source_bar=fill_bar,
                )
            )
            terminal_orders.add(order.order_id)
            continue
        fill = paper_engine._fill_order(order, fill_bar, run, config)
        position = paper_engine._position_from_fill(order, fill)
        reason = paper_engine._fill_entry_block_reason(
            order,
            fill=fill,
            position=position,
            fill_bar=fill_bar,
            position_rows=position_rows + [item.to_dict() for item in new_positions],
            pending_rows=[],
            account=account,
            config=config,
            daily_closed_net=daily_net,
        )
        if reason is None:
            fills.append(fill)
            new_positions.append(position)
            fill_source_bars[fill.fill_id] = fill_bar
            position_source_bars[position.position_id] = fill_bar
        else:
            fill_blocks.append(
                paper_engine._blocked_order_payload(
                    order,
                    reason,
                    run,
                    source_bar=fill_bar,
                )
            )
        terminal_orders.add(order.order_id)
    remaining_pending = [
        row for row in pending if str(row.get("order_id")) not in terminal_orders
    ]

    closes: list[Any] = []
    marks: list[Any] = []
    close_source_bars: dict[str, Any] = {}
    mark_source_bars: dict[str, Any] = {}
    updated_positions: list[JsonDict] = []
    for position in [
        paper_engine._position_from_row(row)
        for row in position_rows + [item.to_dict() for item in new_positions]
    ]:
        bar = paper_engine._latest_bar_on_or_before(dataset, position.symbol, run_date)
        if bar is None:
            updated_positions.append(position.to_dict())
            continue
        checked, close_record = paper_engine._check_position(position, bar, run, config)
        if close_record is not None:
            closes.append(close_record)
            close_source_bars[close_record.close_id] = bar
            accounts = paper_engine._apply_close(accounts, close_record)
        else:
            updated_positions.append(checked.to_dict())
            marks.append(checked)
            mark_source_bars[checked.position_id] = bar
            accounts = paper_engine._apply_mark(accounts, checked)
    accounts = paper_engine._recalculate_unrealized_accounts(accounts, updated_positions)
    account = accounts[key[0]]
    lifecycle_events = (
        pending_events
        + [
            paper_engine._event(
                run,
                PaperJobPhase.CHECK,
                fill.strategy_id,
                fill.symbol,
                "paper_fill",
                fill.fill_id,
                {
                    **paper_engine._with_source_bar(
                        fill.to_dict(), fill_source_bars[fill.fill_id], run
                    ),
                    "challenger_id": challenger_id,
                },
            )
            for fill in fills
        ]
        + [
            paper_engine._event(
                run,
                PaperJobPhase.CHECK,
                position.strategy_id,
                position.symbol,
                "paper_position_opened",
                position.position_id,
                {
                    **paper_engine._with_source_bar(
                        position.to_dict(),
                        position_source_bars[position.position_id],
                        run,
                    ),
                    "challenger_id": challenger_id,
                },
            )
            for position in new_positions
        ]
        + [
            paper_engine._event(
                run,
                PaperJobPhase.CHECK,
                close.strategy_id,
                close.symbol,
                "paper_position_closed",
                close.close_id,
                {
                    **paper_engine._with_source_bar(
                        close.to_dict(), close_source_bars[close.close_id], run
                    ),
                    "challenger_id": challenger_id,
                },
            )
            for close in closes
        ]
        + [
            paper_engine._event(
                run,
                PaperJobPhase.CHECK,
                str(row["strategy_id"]),
                str(row["symbol"]),
                "paper_order_blocked",
                f"{row['order_id']}:{row['reason']}",
                {**row, "challenger_id": challenger_id},
            )
            for row in fill_blocks
        ]
        + [
            paper_engine._event(
                run,
                PaperJobPhase.CLOSE,
                mark.strategy_id,
                mark.symbol,
                "paper_position_marked_to_market",
                f"{mark.position_id}:shadow_mark:{run_date}",
                {
                    **paper_engine._with_source_bar(
                        mark.to_dict(), mark_source_bars[mark.position_id], run
                    ),
                    "challenger_id": challenger_id,
                },
            )
            for mark in marks
        ]
    )
    transaction_events = [*scan_events, *enter_events, *lifecycle_events]
    normalized_event_rows = [_event_row(event) for event in transaction_events]
    calendar_warnings = list(
        dict.fromkeys(
            (
                *warnings,
                f"shadow challenger {registration['challenger_id']}",
                f"logic sha256 {registration['logic_artifact_sha256']}",
                "research only; automatic promotion disabled",
            )
        )
    )
    manifest = {
        "schema_version": SHADOW_RUN_SCHEMA,
        "status": "completed",
        "date": run.run_date,
        "mode": run.mode.value,
        "run_id": run.run_id,
        "data_snapshot_id": run.data_snapshot_id,
        "challenger_id": challenger_id,
        "strategy_id": key[0],
        "strategy_version": key[1],
        "execution_policy_version": key[2],
        "logic_artifact_sha256": registration["logic_artifact_sha256"],
        "strategy_semantics_fingerprint": candidate_semantics,
        "decision_coverage": len(decision_rows),
        "decision_coverage_status": "complete",
        "decision_artifact_sha256": _sha256(decision_rows),
        "decision_symbols_sha256": _sha256(list(expected_decision_symbols)),
        "transaction_event_count": len(normalized_event_rows),
        "transaction_event_ids_sha256": _sha256(
            sorted(str(row.get("event_id") or "") for row in normalized_event_rows)
        ),
        "transaction_events_sha256": _sha256(normalized_event_rows),
        "orders_created": len(orders),
        "orders_blocked": len(blocked) + len(fill_blocks),
        "fills": len(fills),
        "closes": len(closes),
        "pending_orders": len(remaining_pending),
        "open_positions": len(updated_positions),
        "calendar_warnings": calendar_warnings,
        "research_only": True,
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }
    paths = paper_engine.PaperOpsPaths.create(output_root)
    decisions_path = _shadow_decisions_path(
        output_root, run.mode, date.fromisoformat(run.run_date), challenger_id
    )
    picks_path = (
        output_root
        / "exports"
        / f"shadow_picks_{mode.value}_{run_date}_{_safe(challenger_id)}.json"
    )
    order_decisions_path = (
        output_root
        / "exports"
        / f"shadow_order_decisions_{mode.value}_{run_date}_{_safe(challenger_id)}.json"
    )
    shadow_account_payload = {
        "schema_version": "v2.paper_ops_shadow_account.v1",
        "account": account.to_dict(),
    }
    paper_engine._commit_paper_transaction(
        paths,
        events=transaction_events,
        state_updates={
            pending_path: remaining_pending,
            positions_path: updated_positions,
            state_dir / f"{mode.value}_account.json": shadow_account_payload,
            paper_engine._paper_accounts_path(paths, mode): paper_engine._account_state_payload(
                paths,
                mode,
                {challenger_id: account},
            ),
            decisions_path: decision_rows,
            picks_path: [pick.to_dict() for pick in picks],
            order_decisions_path: [
                *(
                    {
                        "decision": "created",
                        "reason": "risk_checks_passed",
                        **row.to_dict(),
                    }
                    for row in orders
                ),
                *blocked,
                *fill_blocks,
            ],
            manifest_path: manifest,
        },
    )
    _write_candidate_calendar(
        output_root=output_root,
        run=run,
        registration=registration,
        account=account,
        pending=remaining_pending,
        positions=updated_positions,
        warnings=warnings,
    )
    return {**manifest, "manifest_path": str(manifest_path)}


def _completed_shadow_manifest(
    path: Path,
    *,
    run: Any,
    registration: JsonDict,
) -> JsonDict | None:
    payload = read_json(path, {})
    if payload == {}:
        return None
    if not isinstance(payload, dict):
        raise ValueError("completed shadow manifest is not an object")
    key = _registration_key(registration)
    expected = {
        "schema_version": SHADOW_RUN_SCHEMA,
        "status": "completed",
        "date": run.run_date,
        "mode": run.mode.value,
        "run_id": run.run_id,
        "data_snapshot_id": run.data_snapshot_id,
        "challenger_id": registration.get("challenger_id"),
        "strategy_id": key[0],
        "strategy_version": key[1],
        "execution_policy_version": key[2],
        "logic_artifact_sha256": registration.get("logic_artifact_sha256"),
        "strategy_semantics_fingerprint": registration.get(
            "candidate_strategy_semantics_fingerprint"
        ),
        "decision_coverage_status": "complete",
        "research_only": True,
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }
    mismatches = [
        field for field, expected_value in expected.items() if payload.get(field) != expected_value
    ]
    if mismatches:
        raise ValueError(
            "completed shadow manifest conflicts with frozen run lineage: "
            + ", ".join(mismatches)
        )
    return dict(payload)


def _freeze_registration(
    raw: JsonDict,
    output_root: Path,
    *,
    registered_at: str,
) -> JsonDict:
    if raw.get("schema_version") != SHADOW_MANIFEST_SCHEMA:
        raise ValueError("unsupported shadow registration manifest schema")
    required = (
        "challenger_id",
        "strategy_id",
        "champion_strategy_version",
        "candidate_strategy_version",
        "execution_policy_version",
        "frozen_at",
        "hypothesis",
        "implementation",
    )
    missing = [
        field
        for field in required
        if raw.get(field) is None or raw.get(field) == ""
    ]
    if missing:
        raise ValueError(f"shadow registration is missing fields: {missing}")
    challenger_id = str(raw["challenger_id"])
    if not _SAFE_ID.fullmatch(challenger_id):
        raise ValueError("challenger_id must be a safe lowercase filesystem identifier")
    if raw.get("status") != "shadow":
        raise ValueError("shadow registration status must be shadow")
    frozen_at = _parse_utc(str(raw["frozen_at"]), field="frozen_at")
    registered = _parse_utc(registered_at, field="registered_at")
    implementation = _normalize_implementation(_dict(raw["implementation"]))
    champion_rows = _champion_registry(output_root)
    strategy_id = str(raw["strategy_id"])
    champion = champion_rows.get(strategy_id)
    if champion is None:
        raise ValueError("shadow registration parent is not an active champion")
    if str(raw["champion_strategy_version"]) != champion[0]:
        raise ValueError("shadow registration champion version does not match active registry")
    if str(raw["execution_policy_version"]) != champion[1]:
        raise ValueError("shadow registration policy does not match active champion policy")
    if str(raw["candidate_strategy_version"]) == champion[0]:
        raise ValueError("shadow candidate version must differ from champion version")
    parent = _parent_strategy(raw, output_root)
    parent_semantics = paper_engine._strategy_semantics_fingerprint(parent)
    if champion[2] != parent_semantics:
        raise ValueError(
            "active champion semantics fingerprint does not match its implementation"
        )
    registration: JsonDict = {
        "schema_version": REGISTERED_CHALLENGER_SCHEMA,
        "challenger_id": challenger_id,
        "strategy_id": strategy_id,
        "champion_strategy_version": champion[0],
        "champion_strategy_semantics_fingerprint": champion[2],
        "candidate_strategy_version": str(raw["candidate_strategy_version"]),
        "execution_policy_version": champion[1],
        "status": "shadow",
        "frozen_at": frozen_at.isoformat(),
        "registered_at": registered.isoformat(),
        "hypothesis": str(raw["hypothesis"]).strip(),
        "implementation": implementation,
        "implementation_source_sha256": _implementation_source_sha256(),
        "parent_logic_sha256": _strategy_logic_sha256(parent),
        "research_only": True,
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
    }
    registration["logic_artifact_sha256"] = _logic_artifact_sha256(registration)
    candidate = _build_candidate_strategy(registration, output_root)
    registration["candidate_strategy_semantics_fingerprint"] = (
        paper_engine._strategy_semantics_fingerprint(candidate)
    )
    registration["registration_id"] = _registration_id(registration)
    return registration


def _registration_template(output_root: Path) -> JsonDict:
    champions = _champion_registry(output_root)
    if champions:
        strategy_id = sorted(champions)[0]
        champion_version, policy, _champion_semantics = champions[strategy_id]
    else:
        strategy_id = "replace_with_champion_strategy_id"
        champion_version = "replace_with_champion_version"
        policy = "replace_with_execution_policy_version"
    return {
        "schema_version": SHADOW_MANIFEST_SCHEMA,
        "challenger_id": f"{strategy_id}_shadow_v2",
        "strategy_id": strategy_id,
        "champion_strategy_version": champion_version,
        "candidate_strategy_version": "v2.0",
        "execution_policy_version": policy,
        "status": "shadow",
        "frozen_at": "REPLACE_WITH_UTC_TIMESTAMP",
        "hypothesis": "State one predeclared, falsifiable improvement hypothesis.",
        "implementation": {
            "kind": IMPLEMENTATION_KIND,
            "parameters": {
                "trend_sma_period": 50,
                "atr_period": 14,
                "max_atr_pct": 0.06,
                "min_parent_score": 0.0,
            },
        },
    }


def _normalize_implementation(raw: JsonDict) -> JsonDict:
    if raw.get("kind") != IMPLEMENTATION_KIND:
        raise ValueError(f"unsupported shadow implementation: {raw.get('kind')}")
    parameters = _dict(raw.get("parameters"))
    allowed = {"trend_sma_period", "atr_period", "max_atr_pct", "min_parent_score"}
    unknown = sorted(set(parameters) - allowed)
    if unknown:
        raise ValueError(f"unknown shadow implementation parameters: {unknown}")
    trend_period = _bounded_int(parameters.get("trend_sma_period", 50), 1, 250)
    atr_period = _bounded_int(parameters.get("atr_period", 14), 1, 100)
    max_atr_pct = _bounded_float(parameters.get("max_atr_pct", 0.06), 0.0001, 1.0)
    min_parent_score = _bounded_float(
        parameters.get("min_parent_score", 0.0), 0.0, 100.0
    )
    return {
        "kind": IMPLEMENTATION_KIND,
        "parameters": {
            "trend_sma_period": trend_period,
            "atr_period": atr_period,
            "max_atr_pct": max_atr_pct,
            "min_parent_score": min_parent_score,
        },
    }


def _build_candidate_strategy(registration: JsonDict, output_root: Path) -> StrategySpec:
    parent = _parent_strategy(registration, output_root)
    parameters = _dict(_dict(registration["implementation"])["parameters"])

    def signal(
        spec: StrategySpec,
        dataset: Any,
        symbol: str,
        bars: tuple[Any, ...],
        index: int,
    ) -> StrategySignal | None:
        parent_signal = parent.signal(dataset, symbol, bars, index)
        if parent_signal is None or not _filter_allows(parent_signal, bars, index, parameters):
            return None
        return replace(
            parent_signal,
            strategy_id=spec.strategy_id,
            strategy_version=spec.version,
            evidence=parent_signal.evidence
            + (
                f"frozen shadow challenger={registration['challenger_id']}",
                f"logic_sha256={registration['logic_artifact_sha256']}",
            ),
            warnings=parent_signal.warnings + ("shadow_only_candidate",),
        )

    return StrategySpec(
        strategy_id=parent.strategy_id,
        version=str(registration["candidate_strategy_version"]),
        status="shadow",
        description=f"Frozen shadow filter for {parent.strategy_id}",
        compatible_timeframe=parent.compatible_timeframe,
        required_data_fields=parent.required_data_fields,
        parameters={**parent.parameters, **parameters, "shadow_filter": IMPLEMENTATION_KIND},
        indicators=parent.indicators + ("frozen_shadow_filter",),
        entry_logic=parent.entry_logic + " Frozen shadow trend/volatility/score filter.",
        exit_logic=parent.exit_logic,
        stop_logic=parent.stop_logic,
        target_logic=parent.target_logic,
        position_sizing_assumption=parent.position_sizing_assumption,
        known_failure_modes=parent.known_failure_modes
        + ("shadow filter may reduce coverage and miss profitable parent entries",),
        validation_status="shadow_only_not_validated",
        generate_signal=signal,
    )


def _filter_allows(
    parent_signal: StrategySignal,
    bars: tuple[Any, ...],
    index: int,
    parameters: JsonDict,
) -> bool:
    trend_period = int(parameters["trend_sma_period"])
    atr_period = int(parameters["atr_period"])
    if index + 1 < max(trend_period, atr_period):
        return False
    closes = [float(bar.close) for bar in bars[: index + 1]]
    trend = sma(closes, trend_period)[index]
    current_atr = atr(bars[: index + 1], atr_period)[index]
    close = float(bars[index].close)
    return bool(
        trend is not None
        and current_atr is not None
        and close > 0
        and close >= trend
        and float(current_atr) / close <= float(parameters["max_atr_pct"])
        and parent_signal.score >= float(parameters["min_parent_score"])
    )


def _parent_strategy(registration: JsonDict, output_root: Path) -> StrategySpec:
    strategy_id = str(registration.get("strategy_id") or "")
    version = str(registration.get("champion_strategy_version") or "")
    match = next(
        (
            strategy
            for strategy in build_strategy_catalog()
            if strategy.strategy_id == strategy_id and strategy.version == version
        ),
        None,
    )
    if match is None:
        raise ValueError(
            f"frozen challenger parent implementation is unavailable: {strategy_id}@{version}"
        )
    champion = _champion_registry(output_root).get(strategy_id)
    if champion is None or champion[0] != version:
        raise ValueError("frozen challenger parent is not the active champion version")
    return match


def _strategy_logic_sha256(strategy: StrategySpec) -> str:
    payload = {
        "strategy_id": strategy.strategy_id,
        "version": strategy.version,
        "parameters": strategy.parameters,
        "entry_logic": strategy.entry_logic,
        "exit_logic": strategy.exit_logic,
        "stop_logic": strategy.stop_logic,
        "target_logic": strategy.target_logic,
        "generate_signal_source": inspect.getsource(strategy.generate_signal),
    }
    return _sha256(payload)


def _implementation_source_sha256() -> str:
    return _sha256(
        {
            "kind": IMPLEMENTATION_KIND,
            "builder": inspect.getsource(_build_candidate_strategy),
            "filter": inspect.getsource(_filter_allows),
        }
    )


def _logic_artifact_sha256(registration: JsonDict) -> str:
    fields = (
        "challenger_id",
        "strategy_id",
        "champion_strategy_version",
        "champion_strategy_semantics_fingerprint",
        "candidate_strategy_version",
        "execution_policy_version",
        "status",
        "frozen_at",
        "hypothesis",
        "implementation",
        "implementation_source_sha256",
        "parent_logic_sha256",
        "research_only",
        "automatic_promotion_enabled",
        "broker_execution_allowed",
    )
    return _sha256({field: registration.get(field) for field in fields})


def _registration_id(registration: JsonDict) -> str:
    return _sha256(
        {
            key: value
            for key, value in registration.items()
            if key != "registration_id"
        }
    )


def _registration_ledger_path(output_root: Path) -> Path:
    return output_root / "state" / "shadow_registration_ledger.jsonl"


def _registration_event_reasons(
    registration: JsonDict,
    output_root: Path,
) -> tuple[str, ...]:
    registration_id = str(registration.get("registration_id") or "")
    matches = [
        event
        for event in read_jsonl(_registration_ledger_path(output_root))
        if str(event.get("registration_event_id") or "") == registration_id
    ]
    if len(matches) != 1:
        return (
            "append-only registration event is missing"
            if not matches
            else "append-only registration event is duplicated",
        )
    event = matches[0]
    reasons: list[str] = []
    if event.get("schema_version") != REGISTRATION_EVENT_SCHEMA:
        reasons.append("append-only registration event schema is unsupported")
    if event.get("event_type") != "shadow_challenger_registered":
        reasons.append("append-only registration event type is invalid")
    if event.get("registered_at") != registration.get("registered_at"):
        reasons.append("append-only registration timestamp differs from registry")
    if event.get("registration") != registration:
        reasons.append("registry row differs from append-only registration event")
    return tuple(reasons)


def _matching_registration_event(
    raw: JsonDict,
    output_root: Path,
) -> JsonDict | None:
    challenger_id = str(raw.get("challenger_id") or "")
    requested_key = (
        str(raw.get("strategy_id") or ""),
        str(raw.get("candidate_strategy_version") or ""),
        str(raw.get("execution_policy_version") or ""),
    )
    matches: list[JsonDict] = []
    for event in read_jsonl(_registration_ledger_path(output_root)):
        stored = event.get("registration")
        if not isinstance(stored, dict):
            continue
        stored_registration = dict(stored)
        stored_key = _registration_key(stored_registration)[:3]
        if (
            str(stored_registration.get("challenger_id") or "") == challenger_id
            or stored_key == requested_key
        ):
            matches.append(stored_registration)
    if not matches:
        return None
    canonical = {_sha256(row) for row in matches}
    if len(matches) != 1 or len(canonical) != 1:
        raise ValueError(
            "append-only registration ledger contains conflicting challenger identity"
        )
    stored_registration = matches[0]
    candidate = _freeze_registration(
        raw,
        output_root,
        registered_at=str(stored_registration.get("registered_at") or ""),
    )
    if candidate != stored_registration:
        raise ValueError(
            "append-only registration event freezes different semantics; "
            "register a new challenger ID and candidate version"
        )
    reasons = _registration_event_reasons(stored_registration, output_root)
    if reasons:
        raise ValueError("registration event integrity failed: " + " | ".join(reasons))
    return stored_registration


def _assert_no_preexisting_candidate_evidence(
    output_root: Path,
    registration: JsonDict,
) -> None:
    key = _registration_key(registration)
    calendar_rows = paper_engine._read_calendar_rows(
        paper_engine.PaperOpsPaths.create(output_root)
    )
    if any(_payload_key(row)[:3] == key[:3] for row in calendar_rows):
        raise ValueError("candidate evidence predates immutable registration")
    for event in read_jsonl(output_root / "ledger" / "paper_ledger.jsonl"):
        payload = event.get("payload")
        if isinstance(payload, dict) and _payload_key(payload)[:3] == key[:3]:
            raise ValueError("candidate ledger evidence predates immutable registration")


def _assert_pre_shadow_truth_gates(output_root: Path) -> None:
    source_paths = (
        output_root / "calendar" / "strategy_daily_returns.csv",
        output_root / "ledger" / "paper_ledger.jsonl",
    )
    newest_source = max(
        (path.stat().st_mtime_ns for path in source_paths if path.exists()),
        default=0,
    )
    required = (
        output_root / "reconciliation" / "reconciliation_latest.json",
        output_root / "reconciliation" / "calendar_truth_latest.json",
        output_root / "reconciliation" / "ledger_rebuild_latest.json",
    )
    for path in required:
        payload = read_json(path, {})
        if not isinstance(payload, dict) or payload.get("status") != "passed":
            raise ValueError(f"pre-shadow truth gate is missing or not passed: {path.name}")
        if path.stat().st_mtime_ns < newest_source:
            raise ValueError(f"pre-shadow truth gate is stale: {path.name}")


def _champion_source_context(
    output_root: Path,
    run_date: date,
    mode: PaperRunMode,
) -> JsonDict:
    report = read_json(
        output_root / "reports" / "daily" / f"{mode.value}_{run_date}.json",
        {},
    )
    if not isinstance(report, dict) or not report:
        raise ValueError("completed champion close report is missing")
    stats = report.get("stats")
    if (
        report.get("mode") != mode.value
        or report.get("date") != run_date.isoformat()
        or not isinstance(stats, dict)
        or stats.get("phase") != "close"
    ):
        raise ValueError("champion daily report is not an exact completed close report")
    preflight = read_json(
        output_root / "exports" / f"preflight_{mode.value}_{run_date}.json",
        {},
    )
    if not isinstance(preflight, dict) or not preflight:
        raise ValueError("champion preflight evidence is missing")
    if (
        preflight.get("status") not in {"passed", "passed_with_warnings"}
        or preflight.get("mode") != mode.value
        or preflight.get("run_id") != report.get("run_id")
        or preflight.get("data_snapshot_id") != report.get("data_snapshot_id")
        or preflight.get("latest_completed_date") != run_date.isoformat()
    ):
        raise ValueError("champion preflight lineage does not match completed close report")
    calendar_rows = paper_engine._read_calendar_rows(
        paper_engine.PaperOpsPaths.create(output_root)
    )
    registry = _champion_registry(output_root)
    for strategy_id, (version, policy, semantics) in registry.items():
        matches = [
            row
            for row in calendar_rows
            if str(row.get("date") or "") == run_date.isoformat()
            and str(row.get("mode") or "") == mode.value
            and _payload_key(row) == (strategy_id, version, policy, semantics)
            and str(row.get("run_id") or "") == str(report.get("run_id") or "")
            and str(row.get("data_snapshot_id") or "")
            == str(report.get("data_snapshot_id") or "")
        ]
        if len(matches) != 1:
            raise ValueError(
                f"champion calendar lineage is incomplete for {strategy_id}@{version}"
            )
    return {
        "run_id": str(report["run_id"]),
        "data_snapshot_id": str(report["data_snapshot_id"]),
    }


def _write_candidate_calendar(
    *,
    output_root: Path,
    run: Any,
    registration: JsonDict,
    account: StrategyPaperAccount,
    pending: list[JsonDict],
    positions: list[JsonDict],
    warnings: tuple[str, ...],
) -> None:
    paths = paper_engine.PaperOpsPaths.create(output_root)
    row = _candidate_calendar_row(
        output_root=output_root,
        run=run,
        registration=registration,
        account=account,
        pending=pending,
        positions=positions,
        warnings=warnings,
    )
    upsert_rows(
        paths.calendar / "strategy_daily_returns.csv",
        [row],
        (
            "date",
            "mode",
            "strategy_id",
            "strategy_version",
            "execution_policy_version",
            "strategy_semantics_fingerprint",
        ),
        paper_engine.CALENDAR_FIELDNAMES,
    )
    all_rows = paper_engine._read_calendar_rows(paths)
    write_json(paths.calendar / "strategy_daily_returns.json", all_rows)


def _candidate_calendar_row(
    *,
    output_root: Path,
    run: Any,
    registration: JsonDict,
    account: StrategyPaperAccount,
    pending: list[JsonDict],
    positions: list[JsonDict],
    warnings: tuple[str, ...],
) -> JsonDict:
    paths = paper_engine.PaperOpsPaths.create(output_root)
    key = _registration_key(registration)
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    day_payloads = [
        _dict(event.get("payload"))
        for event in events
        if event.get("mode") == run.mode.value
        and event.get("trade_date") == run.run_date
        and isinstance(event.get("payload"), dict)
        and _payload_key(_dict(event.get("payload"))) == key
    ]
    closes = [row for row in day_payloads if row.get("close_id")]
    fills = [row for row in day_payloads if row.get("fill_id")]
    rows = paper_engine._read_calendar_rows(paths)
    prior = sorted(
        (
            row
            for row in rows
            if str(row.get("mode") or "") == run.mode.value
            and _payload_key(row) == key
            and str(row.get("date") or "") < run.run_date
        ),
        key=lambda row: str(row.get("date") or ""),
    )
    previous_ending = (
        float(str(prior[-1]["ending_equity"]))
        if prior and prior[-1].get("ending_equity") not in {None, ""}
        else account.starting_equity
    )
    historical_peak = max(
        [account.starting_equity]
        + [
            float(str(row["ending_equity"]))
            for row in prior
            if row.get("ending_equity") not in {None, ""}
        ]
    )
    peak = max(historical_peak, account.current_equity)
    outcomes = [float(row["net_pnl"]) for row in closes]
    r_values = [float(row["r_multiple"]) for row in closes]
    exposure = sum(
        float(row.get("last_mark_price") or 0.0) * int(row.get("quantity") or 0)
        for row in positions
    )
    row = StrategyCalendarRow(
        date=run.run_date,
        mode=run.mode,
        strategy_id=key[0],
        strategy_version=key[1],
        strategy_status="shadow",
        data_snapshot_id=run.data_snapshot_id,
        starting_equity=account.starting_equity,
        ending_equity=account.current_equity,
        realized_pnl=sum(outcomes),
        unrealized_pnl=account.unrealized_pnl,
        total_pnl=account.current_equity - previous_ending,
        daily_return_pct=(
            (account.current_equity - previous_ending) / previous_ending
            if previous_ending
            else 0.0
        ),
        cumulative_return_pct=(
            (account.current_equity - account.starting_equity) / account.starting_equity
            if account.starting_equity
            else 0.0
        ),
        drawdown_pct=(account.current_equity - peak) / peak if peak else 0.0,
        trades_opened=len(fills),
        trades_closed=len(closes),
        pending_orders=len(pending),
        open_positions=len(positions),
        wins=sum(value > 0 for value in outcomes),
        losses=sum(value < 0 for value in outcomes),
        flats=sum(value == 0 for value in outcomes),
        average_r=sum(r_values) / len(r_values) if r_values else 0.0,
        expectancy_r=sum(r_values) / len(r_values) if r_values else 0.0,
        exposure_pct=exposure / account.current_equity if account.current_equity else 0.0,
        fees_paid=sum(float(row["fee"]) for row in fills + closes),
        slippage_estimate=sum(float(row["slippage"]) for row in fills + closes),
        warnings=tuple(
            dict.fromkeys(
                (
                    *warnings,
                    f"shadow challenger {registration['challenger_id']}",
                    f"logic sha256 {registration['logic_artifact_sha256']}",
                    "research only; automatic promotion disabled",
                )
            )
        ),
        run_id=run.run_id,
        execution_policy_version=key[2],
        strategy_semantics_fingerprint=str(
            registration["candidate_strategy_semantics_fingerprint"]
        ),
    )
    return dict(row.to_dict())


def _shadow_decisions_path(
    output_root: Path,
    mode: PaperRunMode,
    run_date: date,
    challenger_id: str,
) -> Path:
    return (
        output_root
        / "exports"
        / f"shadow_strategy_decisions_{mode.value}_{run_date}_{_safe(challenger_id)}.json"
    )


def _load_shadow_account(
    output_root: Path,
    mode: PaperRunMode,
    key: tuple[str, str, str, str],
    starting_equity: float,
    strategy_semantics_fingerprint: str,
) -> StrategyPaperAccount:
    path = paper_engine._paper_accounts_path(
        paper_engine.PaperOpsPaths.create(output_root), mode
    )
    payload = read_json(path, {})
    rows = payload.get("accounts", []) if isinstance(payload, dict) else []
    matches = [
        row for row in rows if isinstance(row, dict) and _payload_key(row) == key
    ]
    if len(matches) > 1:
        raise ValueError("duplicate shadow account identity")
    if not matches:
        return StrategyPaperAccount(
            strategy_id=key[0],
            strategy_version=key[1],
            starting_equity=starting_equity,
            current_equity=starting_equity,
            realized_pnl=0.0,
            unrealized_pnl=0.0,
            execution_policy_version=key[2],
            strategy_semantics_fingerprint=strategy_semantics_fingerprint,
        )
    row = matches[0]
    if row.get("strategy_semantics_fingerprint") != strategy_semantics_fingerprint:
        raise ValueError("shadow account strategy semantics fingerprint mismatch")
    return StrategyPaperAccount(
        strategy_id=key[0],
        strategy_version=key[1],
        starting_equity=float(row["starting_equity"]),
        current_equity=float(row["current_equity"]),
        realized_pnl=float(row["realized_pnl"]),
        unrealized_pnl=float(row["unrealized_pnl"]),
        execution_policy_version=key[2],
        strategy_semantics_fingerprint=strategy_semantics_fingerprint,
    )


def _load_shadow_rows(
    path: Path,
    key: tuple[str, str, str, str],
) -> list[JsonDict]:
    payload = read_json(path, [])
    if not isinstance(payload, list):
        raise ValueError(f"shadow state is not a list: {path}")
    rows = [_dict(row) for row in payload]
    if any(_payload_key(row) != key for row in rows):
        raise ValueError(f"shadow state contains cross-series contamination: {path}")
    return rows


def _read_registry(output_root: Path) -> JsonDict:
    payload = read_json(output_root / "state" / "strategy_challenger_registry.json", {})
    if not isinstance(payload, dict) or payload.get("schema_version") != SHADOW_REGISTRY_SCHEMA:
        raise ValueError("PaperOps shadow registry is missing or unsupported")
    if not isinstance(payload.get("challengers"), list):
        raise ValueError("PaperOps shadow registry challengers must be a list")
    integrity_reasons: list[str] = []
    seen_ids: set[str] = set()
    seen_versions: set[tuple[str, str]] = set()
    for raw in payload["challengers"]:
        if not isinstance(raw, dict):
            integrity_reasons.append("shadow registry contains a non-object row")
            continue
        registration = dict(raw)
        challenger_id = str(registration.get("challenger_id") or "")
        version_key = (
            str(registration.get("strategy_id") or ""),
            str(registration.get("candidate_strategy_version") or ""),
        )
        if challenger_id in seen_ids:
            integrity_reasons.append(f"duplicate challenger_id in registry: {challenger_id}")
        if version_key in seen_versions:
            integrity_reasons.append(
                "duplicate candidate strategy/version in registry: " + "/".join(version_key)
            )
        seen_ids.add(challenger_id)
        seen_versions.add(version_key)
        if registration.get("registration_id") != _registration_id(registration):
            integrity_reasons.append(
                f"registry registration_id drift for {challenger_id or '<unknown>'}"
            )
        integrity_reasons.extend(_registration_event_reasons(registration, output_root))
    if integrity_reasons:
        raise ValueError(
            "PaperOps shadow registry integrity failed: "
            + " | ".join(integrity_reasons)
        )
    return dict(payload)


def _champion_registry(output_root: Path) -> dict[str, tuple[str, str, str]]:
    payload = read_json(output_root / "state" / "strategy_registry.json", [])
    if not isinstance(payload, list):
        raise ValueError("PaperOps champion registry is missing")
    result: dict[str, tuple[str, str, str]] = {}
    for row in payload:
        if not isinstance(row, dict):
            continue
        strategy_id = str(row.get("strategy_id") or "")
        version = str(row.get("strategy_version") or "")
        policy = str(row.get("execution_policy_version") or "")
        semantics = str(row.get("strategy_semantics_fingerprint") or "")
        if strategy_id and version and policy and semantics and semantics != "unknown":
            result[strategy_id] = (version, policy, semantics)
    if not result:
        raise ValueError("PaperOps champion registry has no exact strategy series")
    return result


def _registration_key(registration: JsonDict) -> tuple[str, str, str, str]:
    return (
        str(registration.get("strategy_id") or ""),
        str(registration.get("candidate_strategy_version") or ""),
        str(registration.get("execution_policy_version") or ""),
        str(registration.get("candidate_strategy_semantics_fingerprint") or ""),
    )


def _payload_key(payload: JsonDict) -> tuple[str, str, str, str]:
    return (
        str(payload.get("strategy_id") or ""),
        str(payload.get("strategy_version") or ""),
        str(payload.get("execution_policy_version") or ""),
        str(payload.get("strategy_semantics_fingerprint") or "unknown"),
    )


def _shadow_manifest_path(
    output_root: Path,
    mode: PaperRunMode,
    run_date: date,
    challenger_id: str,
) -> Path:
    return (
        output_root
        / "manifests"
        / f"shadow_{mode.value}_{run_date}_{_safe(challenger_id)}.json"
    )


def _parse_utc(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO-8601 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"{field} must include an explicit UTC offset")
    return parsed.astimezone(timezone.utc)


def _market_date_from_timestamp(value: str, *, field: str) -> date:
    """Resolve immutable registration timestamps on the US market date."""

    return _parse_utc(value, field=field).astimezone(MARKET_TIMEZONE).date()


def _bounded_int(value: Any, lower: int, upper: int) -> int:
    if isinstance(value, bool):
        raise ValueError("shadow integer parameter cannot be boolean")
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid shadow integer parameter") from exc
    if parsed < lower or parsed > upper:
        raise ValueError(f"shadow integer parameter must be in [{lower}, {upper}]")
    return parsed


def _bounded_float(value: Any, lower: float, upper: float) -> float:
    if isinstance(value, bool):
        raise ValueError("shadow numeric parameter cannot be boolean")
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("invalid shadow numeric parameter") from exc
    if not math.isfinite(parsed) or parsed < lower or parsed > upper:
        raise ValueError(f"shadow numeric parameter must be in [{lower}, {upper}]")
    return parsed


def _dict(value: Any) -> JsonDict:
    return dict(value) if isinstance(value, dict) else {}


def _event_row(value: Any) -> JsonDict:
    if isinstance(value, dict):
        return dict(value)
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        payload = to_dict()
        if isinstance(payload, dict):
            return dict(payload)
    raise ValueError("shadow transaction event is not serializable")


def _safe(value: str) -> str:
    return value.replace(":", "_").replace("/", "_").replace("\\", "_")


def _sha256(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()
