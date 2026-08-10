"""Forward readiness report for PaperOps."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.observer_safety import require_observer_command
from intraday_scanner.v2.paper_ops.storage import read_json, write_json
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence

_ALLOWED_DATA_STATUSES = frozenset(
    {
        "reconciled",
        "reconciled_with_minor_diffs",
        "single_provider_unreconciled",
    }
)


@dataclass(frozen=True)
class ForwardReadinessResult:
    status: str
    strategy_evidence_status: str
    data_status: str
    ledger_rebuild_status: str
    calendar_truth_status: str
    pending_orders: int
    open_positions: int
    eligible_strategies: tuple[str, ...]
    blocked_strategies: tuple[str, ...]
    quarantined_strategies: tuple[str, ...]
    warnings: tuple[str, ...]
    next_commands: tuple[str, ...]
    schema_version: str = "v2.paper_ops_forward_readiness.v2"

    def to_dict(self) -> dict[str, object]:
        return {
            "blocked_strategies": list(self.blocked_strategies),
            "calendar_truth_status": self.calendar_truth_status,
            "data_status": self.data_status,
            "eligible_strategies": list(self.eligible_strategies),
            "ledger_rebuild_status": self.ledger_rebuild_status,
            "next_commands": list(self.next_commands),
            "open_positions": self.open_positions,
            "pending_orders": self.pending_orders,
            "quarantined_strategies": list(self.quarantined_strategies),
            "schema_version": self.schema_version,
            "status": self.status,
            "strategy_evidence_status": self.strategy_evidence_status,
            "warnings": list(self.warnings),
        }


def forward_readiness(*, output_root: Path = Path("data/v2_paper_ops")) -> ForwardReadinessResult:
    require_observer_command(output_root, "readiness")
    paths = PaperOpsPaths.resolve(output_root)
    data_status = _data_status(paths.root.parent / "v2_data_truth")
    ledger = rebuild_ledger(output_root=output_root)
    calendar = verify_calendar_truth(output_root=output_root)
    evidence = score_strategy_evidence(output_root=output_root)
    pending = _list_file(paths.state / "pending_orders.json")
    open_positions = _list_file(paths.state / "open_positions.json")
    eligible = [
        str(row["strategy_id"])
        for row in evidence.scores
        if row.get("evidence_status") in {"watch", "candidate", "validated"}
    ]
    quarantined = [
        str(row["strategy_id"])
        for row in evidence.scores
        if row.get("evidence_status") == "quarantined"
    ]
    blocked = [
        str(row["strategy_id"])
        for row in evidence.scores
        if row.get("evidence_status") in {"probation", "rejected"}
    ]
    warnings: list[str] = []
    if data_status == "single_provider_unreconciled":
        warnings.append("data is single-provider only; not independently reconciled")
    if data_status not in _ALLOWED_DATA_STATUSES:
        warnings.append(f"data status {data_status} blocks forward readiness")
    if ledger.status != "passed":
        warnings.append("ledger rebuild did not match stored state/calendar")
    if calendar.status == "failed":
        warnings.append("calendar truth verification failed")
    warnings.extend(calendar.warnings)
    if evidence.status != "passed":
        warnings.append(f"strategy evidence status {evidence.status} blocks forward readiness")
    warnings.extend(evidence.warnings)
    status = (
        "ready_with_warnings"
        if not _hard_block(data_status, ledger.status, calendar.status, evidence.status)
        else "blocked"
    )
    result = ForwardReadinessResult(
        status=status,
        strategy_evidence_status=evidence.status,
        data_status=data_status,
        ledger_rebuild_status=ledger.status,
        calendar_truth_status=calendar.status,
        pending_orders=len(pending),
        open_positions=len(open_positions),
        eligible_strategies=tuple(sorted(eligible)),
        blocked_strategies=tuple(sorted(blocked)),
        quarantined_strategies=tuple(sorted(quarantined)),
        warnings=tuple(dict.fromkeys(warnings)),
        next_commands=(
            "py -m intraday_scanner.v2.data_truth build --date 2026-06-29 --no-fetch",
            "py -m intraday_scanner.v2.paper_ops run-day --date 2026-06-29",
            "py -m intraday_scanner.v2.paper_ops rebuild-ledger",
            "py -m intraday_scanner.v2.paper_ops verify-calendar",
            "py -m intraday_scanner.v2.paper_ops evidence",
            "py -m intraday_scanner.v2.paper_ops readiness",
        ),
    )
    _write_reports(paths, result)
    return result


def _data_status(root: Path) -> str:
    payload = read_json(root / "reconciliation" / "latest_reconciliation.json", {})
    if isinstance(payload, dict):
        report = payload.get("report")
        if isinstance(report, dict):
            return str(report.get("status", "unknown"))
        return str(payload.get("status", "unknown"))
    return "unknown"


def _list_file(path: Path) -> list[object]:
    payload = read_json(path, [])
    return payload if isinstance(payload, list) else []


def _hard_block(
    data_status: str,
    ledger_status: str,
    calendar_status: str,
    strategy_evidence_status: str = "passed",
) -> bool:
    return (
        data_status not in _ALLOWED_DATA_STATUSES
        or ledger_status != "passed"
        or calendar_status not in {"passed", "passed_with_warnings"}
        or strategy_evidence_status != "passed"
    )


def _write_reports(paths: PaperOpsPaths, result: ForwardReadinessResult) -> None:
    write_json(paths.reports / "forward_readiness.json", result.to_dict())
    lines = [
        "# PaperOps Forward Readiness",
        "",
        f"- Status: `{result.status}`",
        f"- Data status: `{result.data_status}`",
        f"- Ledger rebuild: `{result.ledger_rebuild_status}`",
        f"- Calendar truth: `{result.calendar_truth_status}`",
        f"- Strategy evidence: `{result.strategy_evidence_status}`",
        f"- Pending orders: `{result.pending_orders}`",
        f"- Open positions: `{result.open_positions}`",
        f"- Eligible tomorrow: {', '.join(result.eligible_strategies) or 'none'}",
        f"- Blocked: {', '.join(result.blocked_strategies) or 'none'}",
        f"- Quarantined: {', '.join(result.quarantined_strategies) or 'none'}",
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in result.warnings or ("None.",))
    lines.extend(["", "## Suggested Commands", ""])
    lines.extend(f"- `{command}`" for command in result.next_commands)
    (paths.reports / "forward_readiness.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
