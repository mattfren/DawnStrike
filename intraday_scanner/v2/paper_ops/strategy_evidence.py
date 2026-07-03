"""Objective strategy evidence scoring for PaperOps."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.storage import read_json, read_jsonl, write_csv, write_json

MIN_FORWARD_DAYS = 30
MIN_CLOSED_TRADES = 30
MIN_PROFIT_FACTOR = 1.1
MAX_DRAWDOWN_LIMIT = -0.15


@dataclass(frozen=True)
class StrategyEvidenceResult:
    status: str
    scores: tuple[dict[str, object], ...]
    warnings: tuple[str, ...]
    schema_version: str = "v2.paper_ops_strategy_evidence.v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": self.schema_version,
            "scores": list(self.scores),
            "status": self.status,
            "warnings": list(self.warnings),
        }


def score_strategy_evidence(
    *,
    output_root: Path = Path("data/v2_paper_ops"),
    alpha_root: Path | None = None,
) -> StrategyEvidenceResult:
    paths = PaperOpsPaths.create(output_root)
    alpha_root = alpha_root or output_root.parent / "v2_alpha_lab"
    registry = _registry(paths)
    calendar_rows = _read_calendar(paths.calendar / "strategy_daily_returns.csv")
    events = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    data_status = _data_status(Path("data/v2_data_truth"))
    robustness_by_strategy = _robustness_by_strategy(alpha_root)
    commit_overlay = _commit_overlay_by_strategy()
    scores: list[dict[str, object]] = []
    for strategy in registry:
        strategy_id = str(strategy["strategy_id"])
        commit_row = commit_overlay.get(strategy_id, {})
        robustness = robustness_by_strategy.get(strategy_id, {})
        robustness_status = _robustness_status(strategy_id, robustness)
        robustness_blockers = _robustness_blockers(strategy_id, robustness)
        strategy_rows = [row for row in calendar_rows if row.get("strategy_id") == strategy_id]
        strategy_events = [event for event in events if event.get("strategy_id") == strategy_id]
        forward_days = len({row["date"] for row in strategy_rows if row.get("mode") == "forward"})
        replay_days = len({row["date"] for row in strategy_rows if row.get("mode") == "replay"})
        forward_closes = _closed_events(strategy_events, mode="forward")
        replay_closes = _closed_events(strategy_events, mode="replay")
        all_closes = forward_closes + replay_closes
        expectancy = _average([_to_float(item.get("net_pnl")) for item in forward_closes])
        replay_expectancy = _average([_to_float(item.get("net_pnl")) for item in replay_closes])
        profit_factor = _profit_factor(forward_closes)
        max_drawdown = _min_float(strategy_rows, "drawdown_pct")
        score = _score(
            data_status=data_status,
            forward_days=forward_days,
            forward_closed=len(forward_closes),
            expectancy=expectancy,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            robustness_status=robustness_status,
        )
        promoted_status, blockers = _status(
            base_status=str(strategy.get("strategy_status", "experimental")),
            data_status=data_status,
            forward_days=forward_days,
            forward_closed=len(forward_closes),
            expectancy=expectancy,
            profit_factor=profit_factor,
            max_drawdown=max_drawdown,
            robustness_status=robustness_status,
            robustness_blockers=robustness_blockers,
        )
        commit_blockers = _commit_blockers(commit_row)
        if commit_blockers:
            blockers = tuple(list(blockers) + list(commit_blockers))
            if promoted_status == "validated":
                promoted_status = "watch"
        scores.append(
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy["strategy_version"],
                "base_status": strategy.get("strategy_status", "experimental"),
                "evidence_status": promoted_status,
                "overall_score": score,
                "backtest_evidence_score": 20,
                "replay_evidence_score": min(20, replay_days * 5 + len(replay_closes)),
                "forward_evidence_score": min(25, forward_days + len(forward_closes)),
                "data_quality_score": (
                    20
                    if data_status in {"reconciled", "reconciled_with_minor_diffs"}
                    else 10
                ),
                "trade_sample_score": min(10, len(all_closes)),
                "expectancy": expectancy,
                "replay_expectancy": replay_expectancy,
                "profit_factor": profit_factor,
                "max_drawdown_pct": max_drawdown,
                "robustness_status": robustness_status,
                "robustness_test_return_pct": robustness.get("test_return_pct", "n/a"),
                "robustness_test_trade_count": robustness.get("test_trade_count", "n/a"),
                "robustness_warnings": _robustness_text(robustness),
                "forward_days": forward_days,
                "replay_days": replay_days,
                "forward_closed_trades": len(forward_closes),
                "replay_closed_trades": len(replay_closes),
                "committed_filltruth_forward_count": _int(
                    commit_row.get("committed_filltruth_forward_count")
                ),
                "uncommitted_overlay_count": _int(commit_row.get("uncommitted_overlay_count")),
                "rejected_filltruth_count": _int(commit_row.get("rejected_filltruth_count")),
                "approximate_fill_penalty": _int(commit_row.get("approximate_fill_penalty")),
                "intraday_supported_forward_fill_count": _int(
                    commit_row.get("intraday_supported_forward_fill_count")
                ),
                "validation_blocked_reason": str(
                    commit_row.get("validation_blocked_reason", "")
                ),
                "blockers": " | ".join(blockers),
            }
        )
    result = StrategyEvidenceResult(status="passed", scores=tuple(scores), warnings=())
    _write_reports(paths, result)
    return result


def _registry(paths: PaperOpsPaths) -> list[dict[str, object]]:
    payload = read_json(paths.state / "strategy_registry.json", [])
    return [row for row in payload if isinstance(row, dict)] if isinstance(payload, list) else []


def _read_calendar(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _closed_events(events: list[dict[str, object]], *, mode: str) -> list[dict[str, object]]:
    closes: list[dict[str, object]] = []
    for event in events:
        if event.get("mode") != mode or event.get("event_type") != "paper_position_closed":
            continue
        payload = event.get("payload")
        if isinstance(payload, dict):
            closes.append(payload)
    return closes


def _data_status(root: Path) -> str:
    payload = read_json(root / "reconciliation" / "latest_reconciliation.json", {})
    if isinstance(payload, dict):
        report = payload.get("report")
        if isinstance(report, dict):
            return str(report.get("status", "unknown"))
        return str(payload.get("status", "unknown"))
    return "unknown"


def _robustness_by_strategy(root: Path) -> dict[str, dict[str, object]]:
    payload = read_json(root / "reports" / "robustness_summary.json", {})
    rows = payload.get("rows") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    return {str(row.get("strategy_id")): row for row in rows if isinstance(row, dict)}


def _commit_overlay_by_strategy(
    root: Path = Path("data/v2_forward_evidence/strategy_evidence"),
) -> dict[str, dict[str, object]]:
    payload = read_json(root / "evidence_commit_strategy_overlay.json", {})
    rows = payload.get("rows") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return {}
    return {str(row.get("strategy_id")): row for row in rows if isinstance(row, dict)}


def _commit_blockers(row: dict[str, object]) -> tuple[str, ...]:
    if not row:
        return ("FillTruth CommitBridge evidence is missing",)
    blockers: list[str] = []
    if _int(row.get("uncommitted_overlay_count")) > 0:
        blockers.append("FillTruth evidence exists only as uncommitted overlay")
    if _int(row.get("rejected_filltruth_count")) > 0:
        blockers.append("Rejected FillTruth evidence cannot support validation")
    if _int(row.get("committed_filltruth_forward_count")) < 1:
        blockers.append("No committed FillTruth forward fills yet")
    return tuple(blockers)


def _robustness_status(strategy_id: str, row: dict[str, object]) -> str:
    if not row:
        return "unknown"
    if _is_benchmark_or_baseline(strategy_id, row):
        return str(row.get("robustness_status", "non_candidate"))
    return str(row.get("robustness_status", "unknown"))


def _robustness_blockers(strategy_id: str, row: dict[str, object]) -> tuple[str, ...]:
    if not row or _is_benchmark_or_baseline(strategy_id, row):
        return ()
    status = str(row.get("robustness_status", "unknown"))
    warnings = _robustness_text(row)
    if status == "fragile":
        return (f"Alpha Lab robustness status is fragile: {warnings}",)
    if status == "insufficient_oos_trades":
        return ("Alpha Lab robustness has insufficient out-of-sample trades",)
    return ()


def _robustness_text(row: dict[str, object]) -> str:
    if not row:
        return "No robustness row available."
    return str(row.get("warnings", "none"))


def _is_benchmark_or_baseline(strategy_id: str, row: dict[str, object]) -> bool:
    status = str(row.get("status", "")).lower()
    return (
        status in {"benchmark", "baseline"}
        or strategy_id.startswith("benchmark_")
        or strategy_id.startswith("cash_")
    )


def _score(
    *,
    data_status: str,
    forward_days: int,
    forward_closed: int,
    expectancy: float,
    profit_factor: float,
    max_drawdown: float,
    robustness_status: str,
) -> int:
    score = 0
    score += 20 if data_status in {"reconciled", "reconciled_with_minor_diffs"} else 10
    score += min(25, forward_days)
    score += min(20, forward_closed)
    score += 15 if expectancy > 0 else 0
    score += 10 if profit_factor >= MIN_PROFIT_FACTOR else 0
    score += 10 if max_drawdown >= MAX_DRAWDOWN_LIMIT else 0
    if robustness_status == "fragile":
        score = max(0, score - 20)
    elif robustness_status == "insufficient_oos_trades":
        score = max(0, score - 5)
    return score


def _status(
    *,
    base_status: str,
    data_status: str,
    forward_days: int,
    forward_closed: int,
    expectancy: float,
    profit_factor: float,
    max_drawdown: float,
    robustness_status: str,
    robustness_blockers: tuple[str, ...],
) -> tuple[str, tuple[str, ...]]:
    blockers: list[str] = []
    allowed_data_statuses = {
        "reconciled",
        "reconciled_with_minor_diffs",
        "single_provider_unreconciled",
    }
    if data_status not in allowed_data_statuses:
        blockers.append(f"data status {data_status} blocks promotion")
    if forward_days < MIN_FORWARD_DAYS:
        blockers.append(f"needs {MIN_FORWARD_DAYS - forward_days} more forward paper days")
    if forward_closed < MIN_CLOSED_TRADES:
        blockers.append(f"needs {MIN_CLOSED_TRADES - forward_closed} more forward closed trades")
    if expectancy <= 0:
        blockers.append("forward expectancy is not positive after costs")
    if profit_factor < MIN_PROFIT_FACTOR:
        blockers.append("profit factor below threshold")
    if max_drawdown < MAX_DRAWDOWN_LIMIT:
        blockers.append("drawdown exceeds threshold")
    blockers.extend(robustness_blockers)
    if robustness_status == "fragile":
        return "quarantined", tuple(blockers)
    if forward_closed and expectancy < 0:
        return "quarantined", tuple(blockers)
    if blockers:
        status = "watch" if base_status in {"experimental", "candidate"} else "probation"
        return status, tuple(blockers)
    return "validated", ()


def _average(values: list[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _profit_factor(closes: list[dict[str, object]]) -> float:
    wins = sum(
        _to_float(item.get("net_pnl"))
        for item in closes
        if _to_float(item.get("net_pnl")) > 0
    )
    losses = abs(
        sum(
            _to_float(item.get("net_pnl"))
            for item in closes
            if _to_float(item.get("net_pnl")) < 0
        )
    )
    if losses == 0:
        return wins if wins else 0.0
    return wins / losses


def _min_float(rows: list[dict[str, str]], field: str) -> float:
    values = [float(row.get(field, "0") or 0.0) for row in rows]
    return min(values) if values else 0.0


def _to_float(value: object) -> float:
    if value is None or value == "":
        return 0.0
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0


def _int(value: object) -> int:
    return int(_to_float(value))


def _write_reports(paths: PaperOpsPaths, result: StrategyEvidenceResult) -> None:
    write_json(paths.reports / "strategy_evidence_scores.json", result.to_dict())
    fields = (
        "strategy_id",
        "strategy_version",
        "base_status",
        "evidence_status",
        "overall_score",
        "forward_days",
        "forward_closed_trades",
        "committed_filltruth_forward_count",
        "uncommitted_overlay_count",
        "rejected_filltruth_count",
        "approximate_fill_penalty",
        "intraday_supported_forward_fill_count",
        "validation_blocked_reason",
        "replay_days",
        "replay_closed_trades",
        "expectancy",
        "profit_factor",
        "max_drawdown_pct",
        "robustness_status",
        "robustness_test_return_pct",
        "robustness_test_trade_count",
        "robustness_warnings",
        "blockers",
    )
    write_csv(paths.reports / "strategy_evidence_scores.csv", list(result.scores), fields)
    lines = [
        "# PaperOps Strategy Evidence",
        "",
        "No strategy is promoted to validated unless all explicit gates pass.",
        "",
        "## Scores",
        "",
    ]
    for row in result.scores:
        lines.append(
            f"- `{row['strategy_id']}` status `{row['evidence_status']}`, "
            f"robustness `{row.get('robustness_status', 'unknown')}`, "
            f"score `{row['overall_score']}`, blockers: {row['blockers'] or 'none'}"
        )
    (paths.reports / "strategy_evidence_summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
