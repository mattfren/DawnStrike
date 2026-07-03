"""PaperOps calendar truth verification."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.storage import read_jsonl, write_json


@dataclass(frozen=True)
class CalendarTruthResult:
    status: str
    duplicate_rows: tuple[str, ...]
    missing_rows: tuple[str, ...]
    math_mismatches: tuple[str, ...]
    ledger_mismatches: tuple[str, ...]
    warnings: tuple[str, ...]
    schema_version: str = "v2.paper_ops_calendar_truth.v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate_rows": list(self.duplicate_rows),
            "ledger_mismatches": list(self.ledger_mismatches),
            "math_mismatches": list(self.math_mismatches),
            "missing_rows": list(self.missing_rows),
            "schema_version": self.schema_version,
            "status": self.status,
            "warnings": list(self.warnings),
        }


def verify_calendar_truth(*, output_root: Path = Path("data/v2_paper_ops")) -> CalendarTruthResult:
    paths = PaperOpsPaths.create(output_root)
    rows = _read_calendar(paths.calendar / "strategy_daily_returns.csv")
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    duplicate_rows = _duplicates(rows)
    missing_rows = _missing_strategy_rows(paths, rows)
    math_mismatches = _math_mismatches(rows)
    rebuild = rebuild_ledger(output_root=output_root)
    ledger_mismatches = tuple(rebuild.calendar_mismatches)
    warnings = _warnings(rows, events)
    status = (
        "passed"
        if not duplicate_rows and not missing_rows and not math_mismatches and not ledger_mismatches
        else "failed"
    )
    result = CalendarTruthResult(
        status=status,
        duplicate_rows=tuple(duplicate_rows),
        missing_rows=tuple(missing_rows),
        math_mismatches=tuple(math_mismatches),
        ledger_mismatches=ledger_mismatches,
        warnings=tuple(warnings),
    )
    _write_reports(paths, result)
    return result


def _read_calendar(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _duplicates(rows: list[dict[str, str]]) -> list[str]:
    keys = [(row.get("date", ""), row.get("mode", ""), row.get("strategy_id", "")) for row in rows]
    return sorted({":".join(key) for key in keys if keys.count(key) > 1})


def _missing_strategy_rows(paths: PaperOpsPaths, rows: list[dict[str, str]]) -> list[str]:
    registry_payload = paths.state / "strategy_registry.json"
    import json

    if not registry_payload.exists():
        return ["strategy registry missing"]
    registry = json.loads(registry_payload.read_text(encoding="utf-8"))
    strategies = {
        str(row.get("strategy_id"))
        for row in registry
        if isinstance(row, dict) and row.get("strategy_id")
    }
    dates_modes = {(row.get("date", ""), row.get("mode", "")) for row in rows}
    present = {
        (row.get("date", ""), row.get("mode", ""), row.get("strategy_id", ""))
        for row in rows
    }
    missing: list[str] = []
    for row_date, mode in dates_modes:
        for strategy_id in strategies:
            if (row_date, mode, strategy_id) not in present:
                missing.append(f"{row_date}:{mode}:{strategy_id}")
    return sorted(missing)


def _math_mismatches(rows: list[dict[str, str]]) -> list[str]:
    mismatches: list[str] = []
    for row in rows:
        key = f"{row.get('date')}:{row.get('mode')}:{row.get('strategy_id')}"
        starting = _float(row.get("starting_equity"))
        ending = _float(row.get("ending_equity"))
        realized = _float(row.get("realized_pnl"))
        unrealized = _float(row.get("unrealized_pnl"))
        total = _float(row.get("total_pnl"))
        daily = _float(row.get("daily_return_pct"))
        cumulative = _float(row.get("cumulative_return_pct"))
        if abs((realized + unrealized) - total) > 0.01:
            mismatches.append(f"{key}: total pnl does not equal realized plus unrealized")
        if starting and abs((total / starting) - daily) > 0.0001:
            mismatches.append(f"{key}: daily return mismatch")
        if starting and abs(((ending - starting) / starting) - cumulative) > 0.0001:
            mismatches.append(f"{key}: cumulative return mismatch")
        if int(_float(row.get("pending_orders"))) and abs(realized) > 0.0001 and not int(
            _float(row.get("trades_closed"))
        ):
            mismatches.append(f"{key}: pending order appears to affect realized pnl")
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
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in result.warnings or ("None.",))
    (paths.reconciliation / "calendar_truth_latest.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0
