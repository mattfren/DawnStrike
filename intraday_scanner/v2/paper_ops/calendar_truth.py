"""PaperOps calendar truth verification."""

from __future__ import annotations

import csv
import json
import math
from collections import Counter
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from pathlib import Path

from intraday_scanner.errors import MarketCalendarCoverageError
from intraday_scanner.market_calendar import (
    FIRST_ELIGIBLE_ACTIVATION_POLICY,
    market_session,
    registration_coverage_inception_date,
)
from intraday_scanner.v2.paper_ops.engine import (
    PaperOpsPaths,
)
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.observer_safety import require_observer_tree
from intraday_scanner.v2.paper_ops.session_gaps import load_forward_session_gaps
from intraday_scanner.v2.paper_ops.storage import read_json, write_json

_NUMERIC_FIELDS = (
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
_COUNT_FIELDS = (
    "trades_opened",
    "trades_closed",
    "pending_orders",
    "open_positions",
    "wins",
    "losses",
    "flats",
)
_LEGACY_ACTIVATION_POLICY = FIRST_ELIGIBLE_ACTIVATION_POLICY

SeriesIdentity = tuple[str, str, str, str]


@dataclass(frozen=True)
class CalendarTruthResult:
    status: str
    duplicate_rows: tuple[str, ...]
    missing_rows: tuple[str, ...]
    math_mismatches: tuple[str, ...]
    ledger_mismatches: tuple[str, ...]
    warnings: tuple[str, ...]
    terminal_missing_sessions: tuple[str, ...] = ()
    schema_version: str = "v2.paper_ops_calendar_truth.v3"

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate_rows": list(self.duplicate_rows),
            "ledger_mismatches": list(self.ledger_mismatches),
            "math_mismatches": list(self.math_mismatches),
            "missing_rows": list(self.missing_rows),
            "schema_version": self.schema_version,
            "status": self.status,
            "terminal_missing_sessions": list(self.terminal_missing_sessions),
            "warnings": list(self.warnings),
        }


def verify_calendar_truth(*, output_root: Path = Path("data/v2_paper_ops")) -> CalendarTruthResult:
    require_observer_tree(
        output_root,
        required_files=(
            "calendar/strategy_daily_returns.csv",
            "state/paper_ops_config.json",
            "state/strategy_registry.json",
        ),
        nonempty_files=("ledger/paper_ledger.jsonl",),
    )
    paths = PaperOpsPaths.resolve(output_root)
    rows = _read_calendar(paths.calendar / "strategy_daily_returns.csv")
    events, ledger_integrity = _read_strict_ledger(paths.ledger / "paper_ledger.jsonl")
    session_gaps, session_gap_errors = load_forward_session_gaps(paths)
    terminal_missing_sessions = tuple(
        f"{row['market_date']}:{row['reason_code']}" for row in session_gaps
    )
    acknowledged_dates = {str(row["market_date"]) for row in session_gaps}
    duplicate_rows = _duplicates(rows)
    missing_rows = [
        *(("calendar has no rows",) if not rows else ()),
        *(("ledger has no events",) if not events else ()),
        *(f"terminal session gap ledger invalid: {item}" for item in session_gap_errors),
        *_missing_strategy_rows(
            paths,
            rows,
            events,
            acknowledged_session_dates=acknowledged_dates,
        ),
        *_completed_report_coverage(paths, rows, events),
    ]
    math_mismatches = _math_mismatches(rows)
    rebuild = rebuild_ledger(output_root=output_root)
    ledger_mismatches = tuple(
        [*ledger_integrity, *rebuild.calendar_mismatches]
        + [f"account mismatch: {item}" for item in rebuild.account_mismatches]
        + [f"ledger warning: {item}" for item in rebuild.warnings]
    )
    warnings = [
        *_warnings(rows, events),
        *(
            "forward session is terminal missing; returns remain absent, never zero: "
            f"{row['market_date']} ({row['reason_code']})"
            for row in session_gaps
        ),
    ]
    has_failures = bool(duplicate_rows or missing_rows or math_mismatches or ledger_mismatches)
    status = (
        "failed"
        if has_failures
        else "passed_with_warnings"
        if terminal_missing_sessions
        else "passed"
    )
    result = CalendarTruthResult(
        status=status,
        duplicate_rows=tuple(duplicate_rows),
        missing_rows=tuple(missing_rows),
        math_mismatches=tuple(math_mismatches),
        ledger_mismatches=ledger_mismatches,
        warnings=tuple(warnings),
        terminal_missing_sessions=terminal_missing_sessions,
    )
    _write_reports(paths, result)
    return result


def _read_calendar(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _read_strict_ledger(path: Path) -> tuple[list[dict[str, object]], list[str]]:
    """Read every JSONL record and reject silently dropped/colliding evidence."""

    if not path.exists():
        return [], []
    rows: list[dict[str, object]] = []
    mismatches: list[str] = []
    event_ids: Counter[str] = Counter()
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, start=1):
            stripped = line.strip()
            if not stripped:
                continue
            try:
                raw = json.loads(stripped)
            except json.JSONDecodeError:
                mismatches.append(f"ledger line {line_number} is not valid JSON")
                continue
            if not isinstance(raw, dict):
                mismatches.append(f"ledger line {line_number} is not an object")
                continue
            row = dict(raw)
            rows.append(row)
            event_id = str(row.get("event_id") or "").strip()
            if not event_id:
                mismatches.append(f"ledger line {line_number} has no event_id")
            else:
                event_ids[event_id] += 1
            if row.get("schema_version") != "v2.paper_ledger_event.v1":
                mismatches.append(f"ledger line {line_number} has unsupported event schema")
            for field in ("event_type", "mode", "run_id", "strategy_id", "trade_date"):
                if not str(row.get(field) or "").strip():
                    mismatches.append(f"ledger line {line_number} has no {field}")
            payload = row.get("payload")
            if not isinstance(payload, dict):
                mismatches.append(f"ledger line {line_number} payload is not an object")
                continue
            for field in ("mode", "strategy_id", "symbol"):
                outer = row.get(field)
                inner = payload.get(field)
                if outer not in {None, ""} and inner not in {None, ""} and outer != inner:
                    mismatches.append(
                        f"ledger line {line_number} envelope/payload {field} mismatch"
                    )
            _validate_run_lineage(
                row=row,
                payload=payload,
                line_number=line_number,
                mismatches=mismatches,
            )
    mismatches.extend(
        f"duplicate ledger event_id {event_id}"
        for event_id, count in sorted(event_ids.items())
        if count > 1
    )
    return rows, mismatches


def _validate_run_lineage(
    *,
    row: dict[str, object],
    payload: dict[str, object],
    line_number: int,
    mismatches: list[str],
) -> None:
    """Bind lifecycle events to today without erasing their origin run."""

    envelope_run_id = str(row.get("run_id") or "").strip()
    payload_run_id = str(payload.get("run_id") or "").strip()
    lifecycle_run_id = str(payload.get("lifecycle_run_id") or "").strip()
    origin_run_id = str(payload.get("origin_run_id") or "").strip()
    has_lifecycle_lineage = "lifecycle_run_id" in payload or "origin_run_id" in payload
    if not has_lifecycle_lineage:
        if payload_run_id and envelope_run_id != payload_run_id:
            mismatches.append(f"ledger line {line_number} envelope/payload run_id mismatch")
        return
    if not lifecycle_run_id:
        mismatches.append(f"ledger line {line_number} has no payload lifecycle_run_id")
    elif envelope_run_id != lifecycle_run_id:
        mismatches.append(f"ledger line {line_number} envelope/payload lifecycle_run_id mismatch")
    if not origin_run_id:
        mismatches.append(f"ledger line {line_number} has no payload origin_run_id")
    elif payload_run_id and origin_run_id != payload_run_id:
        mismatches.append(f"ledger line {line_number} payload origin/run_id mismatch")


def _duplicates(rows: list[dict[str, str]]) -> list[str]:
    keys = [
        (
            row.get("date", ""),
            row.get("mode", ""),
            row.get("strategy_id", ""),
            row.get("strategy_version", "unknown"),
            row.get("execution_policy_version", "legacy_unspecified"),
            row.get("strategy_semantics_fingerprint", "unknown"),
        )
        for row in rows
    ]
    counts = Counter(keys)
    return sorted(":".join(key) for key, count in counts.items() if count > 1)


def _missing_strategy_rows(
    paths: PaperOpsPaths,
    rows: list[dict[str, str]],
    events: list[dict[str, object]],
    *,
    acknowledged_session_dates: set[str] | frozenset[str] = frozenset(),
) -> list[str]:
    registry_path = paths.state / "strategy_registry.json"
    if not registry_path.exists():
        return ["strategy registry missing"]
    registry = read_json(registry_path, [])
    if not isinstance(registry, list):
        return ["strategy registry is not an array"]
    registry_inceptions, inception_errors = _registry_coverage_inceptions(paths, registry)
    if inception_errors:
        return inception_errors
    present = {
        (
            row.get("date", ""),
            row.get("mode", ""),
            row.get("strategy_id", ""),
            row.get("strategy_version", "unknown"),
            row.get("execution_policy_version", "legacy_unspecified"),
            row.get("strategy_semantics_fingerprint", "unknown"),
        )
        for row in rows
    }
    dates_modes = {
        (row.get("date", ""), row.get("mode", ""))
        for row in rows
        if row.get("date") and row.get("mode")
    }
    forward_dates = sorted(
        {
            str(row.get("date") or "")
            for row in rows
            if row.get("mode") == "forward" and row.get("date")
        }
    )
    session_errors: list[str] = []
    if len(forward_dates) >= 2:
        try:
            lower = date.fromisoformat(forward_dates[0])
            upper = date.fromisoformat(forward_dates[-1])
            current = lower
            while current <= upper:
                if market_session(current).is_trading_day:
                    dates_modes.add((current.isoformat(), "forward"))
                current += timedelta(days=1)
        except (ValueError, MarketCalendarCoverageError) as exc:
            session_errors.append(
                "forward whole-session coverage range is invalid or outside the "
                f"published market calendar: {exc}"
            )
    missing = {
        ":".join((row_date, mode, *identity))
        for row_date, mode in dates_modes
        for identity, inception_date in registry_inceptions.items()
        if row_date >= inception_date
        if mode != "forward" or row_date not in acknowledged_session_dates
        if (row_date, mode, *identity) not in present
    }
    pre_inception_forward = {
        "pre-inception forward row:" + ":".join((item[0], *item[2:]))
        for item in present
        if item[1] == "forward"
        if item[2:] in registry_inceptions
        if item[0] < registry_inceptions[item[2:]]
    }
    expected: set[tuple[str, str, str, str, str, str]] = set()
    for event in events:
        payload = event.get("payload")
        if not isinstance(payload, dict):
            continue
        strategy_id = str(event.get("strategy_id") or payload.get("strategy_id") or "")
        row_date = str(event.get("trade_date") or "")
        mode = str(event.get("mode") or payload.get("mode") or "")
        if not strategy_id or not row_date or not mode:
            continue
        strategy_version = str(payload.get("strategy_version") or "unknown")
        execution_policy_version = str(
            payload.get("execution_policy_version") or "legacy_unspecified"
        )
        strategy_semantics_fingerprint = str(
            payload.get("strategy_semantics_fingerprint") or "unknown"
        )
        expected.add(
            (
                row_date,
                mode,
                strategy_id,
                strategy_version,
                execution_policy_version,
                strategy_semantics_fingerprint,
            )
        )
    missing.update(":".join(key) for key in expected - present)
    missing.update(pre_inception_forward)
    missing.update(session_errors)
    return sorted(missing)


def _registry_coverage_inceptions(
    paths: PaperOpsPaths,
    registry: list[object],
) -> tuple[dict[SeriesIdentity, str], list[str]]:
    semantics_payload = read_json(
        paths.state / "strategy_semantics_manifest.json",
        {},
    )
    policy_payload = read_json(
        paths.state / "execution_policy_manifest.json",
        {},
    )
    semantics = semantics_payload.get("strategies") if isinstance(semantics_payload, dict) else None
    policies = policy_payload.get("policies") if isinstance(policy_payload, dict) else None
    if not isinstance(semantics, dict):
        return {}, ["strategy semantics manifest is unavailable for coverage inception"]
    if not isinstance(policies, dict):
        return {}, ["execution policy manifest is unavailable for coverage inception"]

    inceptions: dict[SeriesIdentity, str] = {}
    errors: list[str] = []
    for raw in registry:
        if not isinstance(raw, dict):
            errors.append("strategy registry contains a malformed row")
            continue
        strategy_id = str(raw.get("strategy_id") or "").strip()
        strategy_version = str(raw.get("strategy_version") or "").strip()
        policy_version = str(raw.get("execution_policy_version") or "").strip()
        fingerprint = str(raw.get("strategy_semantics_fingerprint") or "").strip()
        if not all((strategy_id, strategy_version, policy_version, fingerprint)):
            errors.append("strategy registry contains an incomplete exact identity")
            continue
        identity = (strategy_id, strategy_version, policy_version, fingerprint)
        if identity in inceptions:
            errors.append("strategy registry duplicates exact identity " + ":".join(identity))
            continue
        semantics_key = f"{strategy_id}@{strategy_version}"
        semantics_entry = semantics.get(semantics_key)
        policy_entry = policies.get(policy_version)
        if not isinstance(semantics_entry, dict):
            errors.append(f"strategy semantics are missing for {semantics_key}")
            continue
        if str(semantics_entry.get("fingerprint") or "") != fingerprint:
            errors.append(f"strategy semantics fingerprint mismatch for {semantics_key}")
            continue
        if not isinstance(policy_entry, dict):
            errors.append(f"execution policy is missing for {policy_version}")
            continue
        strategy_inception = _manifest_coverage_inception(
            semantics_entry,
            artifact=f"strategy semantics {semantics_key}",
        )
        policy_inception = _manifest_coverage_inception(
            policy_entry,
            artifact=f"execution policy {policy_version}",
        )
        if isinstance(strategy_inception, str) and strategy_inception.startswith("error:"):
            errors.append(strategy_inception.removeprefix("error:"))
            continue
        if isinstance(policy_inception, str) and policy_inception.startswith("error:"):
            errors.append(policy_inception.removeprefix("error:"))
            continue
        inceptions[identity] = max(strategy_inception, policy_inception)
    return inceptions, errors


def _manifest_coverage_inception(entry: dict[str, object], *, artifact: str) -> str:
    raw_registered = str(entry.get("registered_at") or "").strip()
    if raw_registered.endswith("Z"):
        raw_registered = raw_registered[:-1] + "+00:00"
    try:
        registered_at = datetime.fromisoformat(raw_registered)
    except ValueError:
        return f"error:{artifact} registered_at is invalid"
    if registered_at.tzinfo is None or registered_at.utcoffset() is None:
        return f"error:{artifact} registered_at must include a timezone"
    activation_policy = str(entry.get("activation_policy") or "").strip()
    try:
        expected_date = registration_coverage_inception_date(
            registered_at,
            activation_policy or _LEGACY_ACTIVATION_POLICY,
        )
    except ValueError:
        return f"error:{artifact} activation_policy is unsupported"
    expected = expected_date.isoformat()
    explicit = str(entry.get("coverage_inception_date") or "").strip()
    if explicit:
        try:
            stored = date.fromisoformat(explicit).isoformat()
        except ValueError:
            return f"error:{artifact} coverage_inception_date is invalid"
        if stored != expected:
            return f"error:{artifact} coverage inception conflicts with registered_at"
    return expected


def _completed_report_coverage(
    paths: PaperOpsPaths,
    rows: list[dict[str, str]],
    events: list[dict[str, object]],
) -> list[str]:
    """Require every completed close report to have exact ledger/calendar truth."""

    completed: set[tuple[str, str, str]] = set()
    gaps: list[str] = []
    daily_dir = paths.reports / "daily"
    if daily_dir.exists():
        for path in sorted(daily_dir.glob("*.json")):
            if path.name.startswith("shadow_"):
                continue
            payload = read_json(path, {})
            if not isinstance(payload, dict):
                continue
            stats = payload.get("stats")
            phases = payload.get("phases")
            close_complete = (isinstance(stats, dict) and stats.get("phase") == "close") or (
                isinstance(phases, dict) and "close" in phases
            )
            if not close_complete:
                continue
            row_date = str(payload.get("date") or "")
            mode = str(payload.get("mode") or "")
            run_id = str(payload.get("run_id") or "")
            if not row_date or not mode or not run_id:
                gaps.append(
                    "completed report identity is incomplete "
                    f"{path.name}:date={row_date or '<missing>'}:"
                    f"mode={mode or '<missing>'}:run_id={run_id or '<missing>'}"
                )
                continue
            completed.add((row_date, mode, run_id))

    registry = read_json(paths.state / "strategy_registry.json", [])
    registry_rows = registry if isinstance(registry, list) else []
    registry_series = {
        (
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or "unknown"),
            str(row.get("execution_policy_version") or "legacy_unspecified"),
            str(row.get("strategy_semantics_fingerprint") or "unknown"),
        )
        for row in registry_rows
        if isinstance(row, dict) and row.get("strategy_id")
    }
    for row_date, mode, run_id in sorted(completed):
        label = f"{row_date}:{mode}:{run_id}"
        expected_series = _report_strategy_series(
            paths,
            row_date=row_date,
            mode=mode,
            run_id=run_id,
            fallback=registry_series,
            gaps=gaps,
        )
        exact_events = [
            event
            for event in events
            if (
                str(event.get("trade_date") or "") == row_date
                and str(event.get("mode") or "") == mode
                and str(event.get("run_id") or "") == run_id
            )
        ]
        exact_rows = [
            row
            for row in rows
            if (
                str(row.get("date") or "") == row_date
                and str(row.get("mode") or "") == mode
                and str(row.get("run_id") or "") == run_id
            )
        ]
        if not exact_events:
            gaps.append(f"completed report has no exact ledger events {label}")
        if not exact_rows:
            gaps.append(f"completed report has no exact calendar rows {label}")
        event_series = {
            (
                str(event.get("strategy_id") or payload.get("strategy_id") or ""),
                str(payload.get("strategy_version") or "unknown"),
                str(payload.get("execution_policy_version") or "legacy_unspecified"),
                str(payload.get("strategy_semantics_fingerprint") or "unknown"),
            )
            for event in exact_events
            for payload in [event.get("payload")]
            if isinstance(payload, dict)
            and (event.get("strategy_id") or payload.get("strategy_id"))
        }
        row_series = {
            (
                str(row.get("strategy_id") or ""),
                str(row.get("strategy_version") or "unknown"),
                str(row.get("execution_policy_version") or "legacy_unspecified"),
                str(row.get("strategy_semantics_fingerprint") or "unknown"),
            )
            for row in exact_rows
            if row.get("strategy_id")
        }
        for series in sorted(expected_series - event_series):
            gaps.append(f"completed report has no exact ledger strategy {label}:{':'.join(series)}")
        for series in sorted(expected_series - row_series):
            gaps.append(
                f"completed report has no exact calendar strategy {label}:{':'.join(series)}"
            )
    return gaps


def _report_strategy_series(
    paths: PaperOpsPaths,
    *,
    row_date: str,
    mode: str,
    run_id: str,
    fallback: set[tuple[str, str, str, str]],
    gaps: list[str],
) -> set[tuple[str, str, str, str]]:
    """Resolve the series active on that day without applying today's versions."""

    label = f"{row_date}:{mode}:{run_id}"
    decision_path = paths.exports / f"strategy_decisions_{mode}_{row_date}.json"
    if not decision_path.exists():
        gaps.append(f"completed report has no strategy decision artifact {label}")
        return fallback
    payload = read_json(decision_path, None)
    if not isinstance(payload, list):
        gaps.append(f"completed report strategy decision artifact is not an array {label}")
        return fallback
    series: set[tuple[str, str, str, str]] = set()
    for index, raw in enumerate(payload):
        if not isinstance(raw, dict):
            gaps.append(f"completed report has malformed strategy decision {label}:{index}")
            continue
        decision_date = str(raw.get("trade_date") or raw.get("market_date") or "")[:10]
        identity = (
            str(raw.get("strategy_id") or ""),
            str(raw.get("strategy_version") or ""),
            str(raw.get("execution_policy_version") or ""),
            str(raw.get("strategy_semantics_fingerprint") or ""),
        )
        if (
            decision_date != row_date
            or str(raw.get("mode") or "") != mode
            or str(raw.get("run_id") or "") != run_id
        ):
            gaps.append(f"completed report strategy decision envelope mismatch {label}:{index}")
            continue
        if not all(identity):
            gaps.append(
                f"completed report strategy decision identity is incomplete {label}:{index}"
            )
            continue
        series.add(identity)
    if not series:
        gaps.append(f"completed report has no exact strategy decision series {label}")
        return fallback
    return series


def _math_mismatches(rows: list[dict[str, str]]) -> list[str]:
    mismatches: list[str] = []
    previous_equity: dict[tuple[str, str, str, str, str], float] = {}
    ordered = sorted(
        rows,
        key=lambda row: (
            row.get("date", ""),
            row.get("mode", ""),
            row.get("strategy_id", ""),
            row.get("strategy_version", "unknown"),
            row.get("execution_policy_version", "legacy_unspecified"),
            row.get("strategy_semantics_fingerprint", "unknown"),
        ),
    )
    for row in ordered:
        key = f"{row.get('date')}:{row.get('mode')}:{row.get('strategy_id')}"
        numeric: dict[str, float] = {}
        invalid_fields: list[str] = []
        for field in _NUMERIC_FIELDS:
            value = _finite_float(row.get(field))
            if value is None:
                invalid_fields.append(field)
            else:
                numeric[field] = value
        mismatches.extend(
            f"{key}: {field} is missing, invalid, or non-finite" for field in invalid_fields
        )
        if invalid_fields:
            continue
        account_key = (
            row.get("mode", ""),
            row.get("strategy_id", ""),
            row.get("strategy_version", "unknown"),
            row.get("execution_policy_version", "legacy_unspecified"),
            row.get("strategy_semantics_fingerprint", "unknown"),
        )
        starting = numeric["starting_equity"]
        ending = numeric["ending_equity"]
        prior_ending = previous_equity.get(account_key, starting)
        realized = numeric["realized_pnl"]
        total = numeric["total_pnl"]
        daily = numeric["daily_return_pct"]
        cumulative = numeric["cumulative_return_pct"]
        if starting <= 0:
            mismatches.append(f"{key}: starting equity must be positive")
        for field in _COUNT_FIELDS:
            value = numeric[field]
            if value < 0 or not value.is_integer():
                mismatches.append(f"{key}: {field} must be a non-negative integer")
        if numeric["wins"] + numeric["losses"] + numeric["flats"] != numeric["trades_closed"]:
            mismatches.append(f"{key}: close outcome counts do not equal trades closed")
        for field in ("fees_paid", "slippage_estimate", "exposure_pct"):
            if numeric[field] < 0:
                mismatches.append(f"{key}: {field} must not be negative")
        if numeric["drawdown_pct"] > 0.0000001:
            mismatches.append(f"{key}: drawdown must not be positive")
        expected_daily_pnl = ending - prior_ending
        if abs(expected_daily_pnl - total) > 0.01:
            mismatches.append(f"{key}: total pnl does not equal daily equity change")
        if prior_ending and abs((total / prior_ending) - daily) > 0.0001:
            mismatches.append(f"{key}: daily return mismatch")
        if starting and abs(((ending - starting) / starting) - cumulative) > 0.0001:
            mismatches.append(f"{key}: cumulative return mismatch")
        if (
            int(numeric["pending_orders"])
            and abs(realized) > 0.0001
            and not int(numeric["trades_closed"])
        ):
            mismatches.append(f"{key}: pending order appears to affect realized pnl")
        previous_equity[account_key] = ending
    return mismatches


def _warnings(rows: list[dict[str, str]], events: list[dict[str, object]]) -> list[str]:
    warnings: list[str] = []
    if not rows:
        warnings.append("calendar file has no rows")
    event_modes = {str(event.get("mode")) for event in events}
    row_modes = {str(row.get("mode")) for row in rows}
    if not row_modes.issubset(event_modes | {"demo"}):
        warnings.append("calendar includes modes not present in ledger events")
    return warnings


def _write_reports(paths: PaperOpsPaths, result: CalendarTruthResult) -> None:
    write_json(paths.reconciliation / "calendar_truth_latest.json", result.to_dict())
    lines = [
        "# PaperOps Calendar Truth",
        "",
        f"- Status: `{result.status}`",
        f"- Duplicate rows: `{len(result.duplicate_rows)}`",
        f"- Missing rows: `{len(result.missing_rows)}`",
        f"- Math mismatches: `{len(result.math_mismatches)}`",
        f"- Ledger mismatches: `{len(result.ledger_mismatches)}`",
        f"- Terminal missing sessions: `{len(result.terminal_missing_sessions)}`",
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in result.warnings or ("None.",))
    (paths.reconciliation / "calendar_truth_latest.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _finite_float(value: object) -> float | None:
    if value is None or value == "" or isinstance(value, bool):
        return None
    if not isinstance(value, str | int | float):
        return None
    try:
        parsed = float(value)
    except ValueError:
        return None
    return parsed if math.isfinite(parsed) else None
