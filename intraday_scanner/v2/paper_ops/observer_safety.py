"""Fail-closed, non-mutating preflight for PaperOps observer commands."""

from __future__ import annotations

import csv
import hashlib
import json
import math
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import TypedDict

from intraday_scanner.v2.paper_ops.models import stable_id


@dataclass
class PaperOpsObserverBlocked(RuntimeError):
    """An observer cannot safely inspect the requested PaperOps tree."""

    status: str
    detail: str

    def __str__(self) -> str:
        return f"{self.status}: {self.detail}"


@dataclass(frozen=True)
class ObserverCommandSpec:
    """Canonical, side-effect-free input contract for an observer command."""

    required_files: tuple[str, ...] = ()
    nonempty_files: tuple[str, ...] = ()
    requires_calendar_evidence: bool = False


class _CalendarRunIdentity(TypedDict):
    mode: str
    run_date: str
    data_snapshot_id: str
    calendar_policies: set[str]


class _LedgerRunIdentity(TypedDict):
    mode: str
    run_date: str
    policies: set[str]


class _ApplicableRunIdentity(_CalendarRunIdentity):
    ledger_policies: set[str]


OBSERVER_COMMAND_SPECS: dict[str, ObserverCommandSpec] = {
    "calendar": ObserverCommandSpec(requires_calendar_evidence=True),
    "report": ObserverCommandSpec(requires_calendar_evidence=True),
    "calendar-view": ObserverCommandSpec(requires_calendar_evidence=True),
    "reconcile": ObserverCommandSpec(nonempty_files=("ledger/paper_ledger.jsonl",)),
    "rebuild-ledger": ObserverCommandSpec(
        required_files=("state/paper_ops_config.json", "state/strategy_registry.json"),
        nonempty_files=("ledger/paper_ledger.jsonl",),
    ),
    "verify-calendar": ObserverCommandSpec(
        required_files=(
            "state/paper_ops_config.json",
            "state/strategy_registry.json",
            "state/execution_policy_manifest.json",
            "state/strategy_semantics_manifest.json",
        ),
        nonempty_files=("ledger/paper_ledger.jsonl",),
        requires_calendar_evidence=True,
    ),
    "verify-source-bars": ObserverCommandSpec(
        required_files=("state/execution_policy_manifest.json",),
        nonempty_files=("ledger/paper_ledger.jsonl",),
        requires_calendar_evidence=True,
    ),
    "blotter": ObserverCommandSpec(
        required_files=(
            "state/paper_ops_config.json",
            "state/strategy_registry.json",
            "state/execution_policy_manifest.json",
        ),
        nonempty_files=("ledger/paper_ledger.jsonl",),
    ),
    "verify-blotter": ObserverCommandSpec(
        required_files=(
            "exports/paper_trade_blotter.json",
            "state/paper_ops_config.json",
            "state/strategy_registry.json",
            "state/execution_policy_manifest.json",
        ),
        nonempty_files=("ledger/paper_ledger.jsonl",),
        requires_calendar_evidence=True,
    ),
    "evidence": ObserverCommandSpec(
        required_files=(
            "state/paper_ops_config.json",
            "state/strategy_registry.json",
            "state/execution_policy_manifest.json",
            "state/strategy_semantics_manifest.json",
        ),
        nonempty_files=("ledger/paper_ledger.jsonl",),
        requires_calendar_evidence=True,
    ),
    "readiness": ObserverCommandSpec(
        required_files=(
            "state/paper_ops_config.json",
            "state/strategy_registry.json",
            "state/execution_policy_manifest.json",
            "state/strategy_semantics_manifest.json",
        ),
        nonempty_files=("ledger/paper_ledger.jsonl",),
        requires_calendar_evidence=True,
    ),
}

_CALENDAR_FIELDS = (
    "date", "mode", "strategy_id", "strategy_version", "strategy_status",
    "execution_policy_version", "strategy_semantics_fingerprint", "data_snapshot_id",
    "starting_equity", "ending_equity", "realized_pnl", "unrealized_pnl", "total_pnl",
    "daily_return_pct", "cumulative_return_pct", "drawdown_pct", "trades_opened",
    "trades_closed", "pending_orders", "open_positions", "wins", "losses", "flats",
    "average_r", "expectancy_r", "exposure_pct", "fees_paid", "slippage_estimate",
    "warnings", "run_id",
)
_CALENDAR_NUMERIC_FIELDS = (
    "starting_equity",
    "ending_equity",
    "realized_pnl",
    "unrealized_pnl",
    "total_pnl",
    "daily_return_pct",
    "cumulative_return_pct",
    "drawdown_pct",
    "trades_opened",
    "trades_closed",
    "pending_orders",
    "open_positions",
    "wins",
    "losses",
    "flats",
    "average_r",
    "expectancy_r",
    "exposure_pct",
    "fees_paid",
    "slippage_estimate",
)
_CALENDAR_COUNT_FIELDS = frozenset(
    {
        "trades_opened",
        "trades_closed",
        "pending_orders",
        "open_positions",
        "wins",
        "losses",
        "flats",
    }
)


def require_observer_command(
    output_root: Path,
    command: str,
    *,
    mode: str | None = None,
) -> None:
    """Apply the one canonical preflight specification for an observer command."""

    try:
        spec = OBSERVER_COMMAND_SPECS[command]
    except KeyError as exc:
        raise ValueError(f"unknown PaperOps observer command: {command}") from exc
    require_observer_tree(
        output_root,
        required_files=spec.required_files,
        nonempty_files=spec.nonempty_files,
    )
    if spec.requires_calendar_evidence:
        _require_calendar_evidence(Path(output_root) / "calendar" / "strategy_daily_returns.csv")
    manifest_mode = {
        "verify-source-bars": mode,
        "verify-blotter": mode,
        "evidence": "forward",
        "readiness": "forward",
    }.get(command)
    if command in {"verify-source-bars", "verify-blotter", "evidence", "readiness"}:
        if manifest_mode is not None and manifest_mode not in {"forward", "replay"}:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT",
                f"{command} requires an explicit attestable PaperOps mode",
            )
        _require_run_manifest_candidate(Path(output_root), command, manifest_mode)


def _require_calendar_evidence(path: Path) -> None:
    """Require parseable canonical calendar data, never treating absent truth as zero."""

    if not path.is_file() or path.stat().st_size == 0:
        raise PaperOpsObserverBlocked("MISSING_INPUT", "Calendar evidence is absent or empty")
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            if (
                not reader.fieldnames
                or len(reader.fieldnames) != len(set(reader.fieldnames))
                or set(reader.fieldnames) != set(_CALENDAR_FIELDS)
            ):
                raise PaperOpsObserverBlocked("INVALID_INPUT", "Calendar CSV has an invalid header")
            rows = list(reader)
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise PaperOpsObserverBlocked("INVALID_INPUT", "Calendar CSV is not parseable") from exc
    if not rows:
        raise PaperOpsObserverBlocked("MISSING_INPUT", "Calendar CSV has no evidence rows")
    identity_fields = (
        "date",
        "mode",
        "strategy_id",
        "strategy_version",
        "strategy_status",
        "execution_policy_version",
        "strategy_semantics_fingerprint",
        "data_snapshot_id",
        "run_id",
    )
    has_malformed_row = any(
        None in row
        or not all(str(row.get(field) or "").strip() for field in identity_fields)
        for row in rows
    )
    if has_malformed_row:
        raise PaperOpsObserverBlocked(
            "INVALID_INPUT", "Calendar CSV contains a malformed evidence row"
        )
    for row in rows:
        try:
            date.fromisoformat(str(row["date"]))
        except ValueError as exc:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", "Calendar CSV contains invalid date"
            ) from exc
        if str(row["mode"]) not in {"forward", "replay", "demo"}:
            raise PaperOpsObserverBlocked("INVALID_INPUT", "Calendar CSV contains invalid mode")
        for field in _CALENDAR_NUMERIC_FIELDS:
            raw = str(row.get(field) or "").strip()
            try:
                value = float(raw)
            except ValueError as exc:
                raise PaperOpsObserverBlocked(
                    "INVALID_INPUT",
                    f"Calendar CSV contains invalid numeric field {field}",
                ) from exc
            if not math.isfinite(value):
                raise PaperOpsObserverBlocked(
                    "INVALID_INPUT",
                    f"Calendar CSV contains non-finite numeric field {field}",
                )
            if field in _CALENDAR_COUNT_FIELDS and (value < 0 or not value.is_integer()):
                raise PaperOpsObserverBlocked(
                    "INVALID_INPUT",
                    f"Calendar CSV contains invalid count field {field}",
                )


def _require_run_manifest_candidate(root: Path, command: str, mode: str | None) -> None:
    """Require a complete canonical manifest bound to the selected observer mode."""

    calendars = _calendar_run_identities(root / "calendar" / "strategy_daily_returns.csv")
    ledgers = _ledger_run_identities(root / "ledger" / "paper_ledger.jsonl")
    # A run is applicable only where both records describe the same identity.
    applicable: dict[str, _ApplicableRunIdentity] = {}
    for run_id, calendar in calendars.items():
        ledger = ledgers.get(run_id)
        if ledger is None:
            continue
        if calendar["mode"] != ledger["mode"] or calendar["run_date"] != ledger["run_date"]:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"calendar/ledger identity conflict for run {run_id}"
            )
        applicable[run_id] = {
            **calendar,
            "ledger_policies": ledger["policies"],
        }
    selected = {
        run_id: identity
        for run_id, identity in applicable.items()
        if mode is None or identity["mode"] == mode
    }
    if not selected:
        raise PaperOpsObserverBlocked(
            "MISSING_INPUT",
            f"{command} has no calendar/ledger runs for "
            f"{mode or 'forward/replay'} manifest validation",
        )
    valid_run_ids: set[str] = set()
    for path in sorted((root / "manifests").glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"Run manifest is not parseable: {path.name}"
            ) from exc
        if not isinstance(payload, dict):
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"Run manifest is not an object: {path.name}"
            )
        if _is_complete_manifest(payload, selected, mode):
            valid_run_ids.add(str(payload["run_id"]))
    if set(selected) <= valid_run_ids:
        return
    raise PaperOpsObserverBlocked(
        "MISSING_INPUT",
        f"{command} requires a complete applicable "
        f"{mode or 'forward/replay'} PaperOps run manifest",
    )


def _is_complete_manifest(
    payload: object,
    applicable_runs: dict[str, _ApplicableRunIdentity],
    mode: str | None,
) -> bool:
    """Validate the persisted v3 identity binding without accepting partial fixtures."""

    if not isinstance(payload, dict):
        return False
    if payload.get("schema_version") != "v2.paper_ops_manifest.v3":
        return False
    run_id = str(payload.get("run_id") or "").strip()
    identity = applicable_runs.get(run_id)
    if identity is None:
        return False
    if payload.get("mode") not in {"forward", "replay"}:
        return False
    if mode is not None and payload.get("mode") != mode:
        return False
    try:
        date.fromisoformat(str(payload.get("run_date") or ""))
    except ValueError:
        return False
    identity_fields = (
        "run_id",
        "data_snapshot_id",
        "execution_policy_version",
        "execution_policy_fingerprint",
        "universe_id",
        "data_snapshot_content_hash",
        "data_snapshot_manifest_payload_hash",
        "data_snapshot_normalized_hash",
        "data_snapshot_normalized_path",
        "data_truth_root_relative",
        "manifest_payload_hash",
    )
    if not all(
        isinstance(payload.get(field), str) and payload[field].strip()
        for field in identity_fields
    ):
        return False
    relative_root = Path(str(payload["data_truth_root_relative"]))
    if relative_root.is_absolute():
        return False
    list_fields = ("output_artifacts", "warnings", "universe_symbols")
    if not all(isinstance(payload.get(field), list) for field in list_fields):
        return False
    symbols = payload["universe_symbols"]
    if not symbols or not all(isinstance(item, str) and item.strip() for item in symbols):
        return False
    if (
        payload.get("mode") != identity["mode"]
        or payload.get("run_date") != identity["run_date"]
        or payload.get("data_snapshot_id") != identity["data_snapshot_id"]
    ):
        return False
    if run_id != stable_id(
        "paper_ops",
        str(payload["mode"]),
        str(payload["run_date"]),
        str(payload["data_snapshot_id"]),
    ):
        return False
    policy = str(payload.get("execution_policy_version") or "")
    if policy not in identity["calendar_policies"]:
        return False
    if identity["ledger_policies"] and policy not in identity["ledger_policies"]:
        return False
    hash_payload = dict(payload)
    observed_hash = hash_payload.pop("manifest_payload_hash")
    canonical = json.dumps(hash_payload, sort_keys=True, separators=(",", ":"))
    return observed_hash == hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _calendar_run_identities(path: Path) -> dict[str, _CalendarRunIdentity]:
    """Return conflict-free calendar identities, retaining only strategy policies."""

    result: dict[str, _CalendarRunIdentity] = {}
    try:
        with path.open("r", encoding="utf-8", newline="") as handle:
            rows = list(csv.DictReader(handle))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        raise PaperOpsObserverBlocked("INVALID_INPUT", "Calendar CSV is not parseable") from exc
    for row in rows:
        run_id = str(row.get("run_id") or "").strip()
        run_mode = str(row.get("mode") or "").strip()
        run_date = str(row.get("date") or "").strip()
        snapshot = str(row.get("data_snapshot_id") or "").strip()
        if not run_id or run_mode not in {"forward", "replay", "demo"} or not snapshot:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", "Calendar CSV contains malformed run identity"
            )
        try:
            date.fromisoformat(run_date)
        except ValueError as exc:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", "Calendar CSV contains invalid run date"
            ) from exc
        identity = result.setdefault(
            run_id,
            {
                "mode": run_mode,
                "run_date": run_date,
                "data_snapshot_id": snapshot,
                "calendar_policies": set(),
            },
        )
        if (
            identity["mode"],
            identity["run_date"],
            identity["data_snapshot_id"],
        ) != (run_mode, run_date, snapshot):
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"conflicting calendar identity for run {run_id}"
            )
        strategy_id = str(row.get("strategy_id") or "").strip().lower()
        policy = str(row.get("execution_policy_version") or "").strip()
        if policy and not _is_reference_calendar_row(strategy_id):
            identity["calendar_policies"].add(policy)
    return result


def _is_reference_calendar_row(strategy_id: str) -> bool:
    return (
        strategy_id in {"benchmark", "cash", "cash_benchmark", "buy_and_hold"}
        or "benchmark" in strategy_id
    )


def _ledger_run_identities(path: Path) -> dict[str, _LedgerRunIdentity]:
    result: dict[str, _LedgerRunIdentity] = {}
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError) as exc:
        raise PaperOpsObserverBlocked("INVALID_INPUT", "Ledger JSONL is not readable") from exc
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"Ledger JSONL has blank line {line_number}"
            )
        try:
            row = json.loads(line)
        except json.JSONDecodeError as exc:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"Ledger JSONL is malformed at line {line_number}"
            ) from exc
        if not isinstance(row, dict):
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"Ledger JSONL row {line_number} is not an object"
            )
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            continue
        run_mode = str(row.get("mode") or "").strip()
        run_date = str(row.get("trade_date") or "").strip()
        if run_mode not in {"forward", "replay", "demo"}:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"Ledger has invalid mode for run {run_id}"
            )
        try:
            date.fromisoformat(run_date)
        except ValueError as exc:
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"Ledger has invalid trade date for run {run_id}"
            ) from exc
        identity = result.setdefault(
            run_id,
            {"mode": run_mode, "run_date": run_date, "policies": set()},
        )
        if (identity["mode"], identity["run_date"]) != (run_mode, run_date):
            raise PaperOpsObserverBlocked(
                "INVALID_INPUT", f"conflicting ledger identity for run {run_id}"
            )
        payload = row.get("payload")
        if isinstance(payload, dict):
            policy = payload.get("execution_policy_version")
            if policy is not None:
                policy_text = str(policy).strip()
                if not policy_text:
                    raise PaperOpsObserverBlocked(
                        "INVALID_INPUT",
                        f"Ledger has malformed policy identity for run {run_id}",
                    )
                identity["policies"].add(policy_text)
    return result


def require_observer_tree(
    output_root: Path,
    *,
    required_files: tuple[str, ...] = (),
    nonempty_files: tuple[str, ...] = (),
) -> None:
    """Validate an existing tree without making directories, locks, or repairs."""

    root = Path(output_root)
    if not root.is_dir():
        raise PaperOpsObserverBlocked("MISSING_INPUT", f"PaperOps root does not exist: {root}")
    required = (
        "ledger",
        "state",
        "calendar",
        "reports",
        "manifests",
        "logs",
        "exports",
        "reconciliation",
    )
    missing = [name for name in required if not (root / name).is_dir()]
    if missing:
        raise PaperOpsObserverBlocked(
            "MISSING_INPUT",
            f"PaperOps tree is incomplete; missing directories: {', '.join(missing)}",
        )
    journal = root / "state" / "paper_transaction_pending.json"
    if journal.exists():
        raise PaperOpsObserverBlocked(
            "BLOCKED_PENDING_RECOVERY",
            f"Pending transaction journal retained for explicit writer recovery: {journal}",
        )
    for relative in required_files:
        if not (root / relative).is_file():
            raise PaperOpsObserverBlocked(
                "MISSING_INPUT", f"Required PaperOps input is absent: {relative}"
            )
    for relative in nonempty_files:
        path = root / relative
        if not path.is_file() or path.stat().st_size == 0:
            raise PaperOpsObserverBlocked(
                "MISSING_INPUT", f"Required PaperOps input is empty: {relative}"
            )
