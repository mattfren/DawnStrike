"""Calendar intelligence outputs for forward and shadow evidence."""

from __future__ import annotations

import csv
import json
from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.evidence_vault import EvidenceVaultPaths, create_paths


@dataclass(frozen=True)
class CalendarIntelligenceResult:
    status: str
    output_root: Path
    rows: int
    forward_days: int
    shadow_replay_days: int
    warnings: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "forward_days": self.forward_days,
            "output_root": self.output_root.as_posix(),
            "rows": self.rows,
            "shadow_replay_days": self.shadow_replay_days,
            "status": self.status,
            "warnings": list(self.warnings),
        }


REQUIRED_DAILY_FIELDS = (
    "date",
    "evidence_mode",
    "strategy_id",
    "strategy_version",
    "strategy_status",
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
    "wins",
    "losses",
    "flats",
    "pending_orders",
    "open_positions",
    "blocked_picks",
    "kill_switch_active",
    "average_r",
    "expectancy_r",
    "fees_paid",
    "slippage_estimate",
    "data_snapshot_id",
    "pick_set_hash",
    "ledger_rebuild_status",
    "calendar_truth_status",
    "warnings",
)


def build_calendar_intelligence(
    *,
    output_root: Path = Path("data/v2_forward_evidence"),
    paper_ops_root: Path = Path("data/v2_paper_ops"),
    shadow_paper_ops_root: Path | None = None,
) -> CalendarIntelligenceResult:
    paths = create_paths(output_root)
    shadow_root = shadow_paper_ops_root or paths.shadow_replay / "paper_ops"
    forward_rows = _paper_calendar_rows(paper_ops_root / "calendar" / "strategy_daily_returns.csv")
    shadow_rows = _paper_calendar_rows(shadow_root / "calendar" / "strategy_daily_returns.csv")
    risk = _read_json(output_root / "reports" / "riskhub_daily.json", {})
    readiness = _read_json(paper_ops_root / "reports" / "forward_readiness.json", {})
    frozen_by_date = _frozen_by_date(paths)
    rows: list[dict[str, object]] = []
    for row in forward_rows:
        rows.append(
            _daily_row(
                row,
                evidence_mode="forward",
                frozen_by_date=frozen_by_date,
                risk=risk,
                readiness=readiness,
            )
        )
    shadow_frozen = _frozen_by_date(paths, shadow=True)
    for row in shadow_rows:
        rows.append(
            _daily_row(
                row,
                evidence_mode="shadow_forward_replay",
                frozen_by_date=shadow_frozen,
                risk={},
                readiness=readiness,
            )
        )
    rows = sorted(
        rows,
        key=lambda item: (
            str(item["date"]),
            str(item["evidence_mode"]),
            str(item["strategy_id"]),
        ),
    )
    _write_csv(paths.calendar / "strategy_daily_returns.csv", rows, REQUIRED_DAILY_FIELDS)
    _write_json(paths.calendar / "strategy_daily_returns.json", rows)
    _write_json(paths.calendar / "strategy_calendar_summary.json", _summary_payload(rows))
    _write_matrix(paths, rows)
    _write_monthly(paths, rows)
    _write_equity(paths, rows)
    _write_drawdowns(paths, rows)
    _write_streaks(paths, rows)
    _write_decay(paths, rows)
    _write_overtrading(paths, rows)
    _write_summary_md(paths, rows)
    result = CalendarIntelligenceResult(
        status="passed",
        output_root=paths.calendar,
        rows=len(rows),
        forward_days=len({str(row["date"]) for row in rows if row["evidence_mode"] == "forward"}),
        shadow_replay_days=len(
            {str(row["date"]) for row in rows if row["evidence_mode"] == "shadow_forward_replay"}
        ),
        warnings=(),
    )
    _write_json(paths.calendar / "calendar_intelligence_result.json", result.to_dict())
    return result


def _daily_row(
    row: dict[str, str],
    *,
    evidence_mode: str,
    frozen_by_date: dict[str, dict[str, object]],
    risk: object,
    readiness: object,
) -> dict[str, object]:
    date_value = str(row.get("date", "unknown"))
    frozen = frozen_by_date.get(date_value, {})
    blocked_picks = len(_list(frozen.get("blocked_candidates")))
    pick_set_hash = str(frozen.get("pick_set_hash", "n/a"))
    kill_switch = bool(risk.get("kill_switch")) if isinstance(risk, dict) else False
    ledger_status = (
        str(readiness.get("ledger_rebuild_status", "unknown"))
        if isinstance(readiness, dict)
        else "unknown"
    )
    calendar_status = (
        str(readiness.get("calendar_truth_status", "unknown"))
        if isinstance(readiness, dict)
        else "unknown"
    )
    return {
        "average_r": _float(row.get("average_r")),
        "blocked_picks": blocked_picks,
        "calendar_truth_status": calendar_status,
        "cumulative_return_pct": _float(row.get("cumulative_return_pct")),
        "daily_return_pct": _float(row.get("daily_return_pct")),
        "data_snapshot_id": row.get("data_snapshot_id", "n/a"),
        "date": date_value,
        "drawdown_pct": _float(row.get("drawdown_pct")),
        "ending_equity": _float(row.get("ending_equity")),
        "evidence_mode": evidence_mode,
        "expectancy_r": _float(row.get("expectancy_r")),
        "fees_paid": _float(row.get("fees_paid")),
        "flats": _int(row.get("flats")),
        "kill_switch_active": kill_switch,
        "ledger_rebuild_status": ledger_status,
        "losses": _int(row.get("losses")),
        "open_positions": _int(row.get("open_positions")),
        "pending_orders": _int(row.get("pending_orders")),
        "pick_set_hash": pick_set_hash,
        "realized_pnl": _float(row.get("realized_pnl")),
        "slippage_estimate": _float(row.get("slippage_estimate")),
        "starting_equity": _float(row.get("starting_equity")),
        "strategy_id": row.get("strategy_id", "unknown"),
        "strategy_status": row.get("strategy_status", "unknown"),
        "strategy_version": row.get("strategy_version", "unknown"),
        "total_pnl": _float(row.get("total_pnl")),
        "trades_closed": _int(row.get("trades_closed")),
        "trades_opened": _int(row.get("trades_opened")),
        "unrealized_pnl": _float(row.get("unrealized_pnl")),
        "warnings": row.get("warnings", ""),
        "wins": _int(row.get("wins")),
    }


def _summary_payload(rows: list[dict[str, object]]) -> dict[str, object]:
    if not rows:
        return {
            "status": "empty",
            "true_forward_days_available": 0,
            "shadow_replay_days_available": 0,
        }
    latest_date = max(str(row["date"]) for row in rows)
    latest = [row for row in rows if str(row["date"]) == latest_date]
    best = max(latest, key=lambda item: _float(item.get("daily_return_pct")))
    worst = min(latest, key=lambda item: _float(item.get("daily_return_pct")))
    forward_days = len({str(row["date"]) for row in rows if row["evidence_mode"] == "forward"})
    shadow_days = len(
        {str(row["date"]) for row in rows if row["evidence_mode"] == "shadow_forward_replay"}
    )
    drawdown = sorted(
        {
            str(row["strategy_id"])
            for row in latest
            if _float(row.get("drawdown_pct")) < 0
        }
    )
    blocked = sorted(
        {
            str(row["strategy_id"])
            for row in latest
            if bool(row.get("kill_switch_active")) or _int(row.get("blocked_picks")) > 0
        }
    )
    return {
        "best_strategy_this_month": _best_month(rows),
        "best_strategy_today": best["strategy_id"],
        "latest_date": latest_date,
        "shadow_replay_days_available": shadow_days,
        "strategies_blocked_by_riskhub": blocked,
        "strategies_decaying": _decaying(rows),
        "strategies_in_drawdown": drawdown,
        "strategies_improving": _improving(rows),
        "strategies_overtrading": _overtrading(rows),
        "strategies_quarantined": _quarantined(),
        "true_forward_days_available": forward_days,
        "validation_eligibility": (
            "no strategy validated; needs 30 true forward days and 30 closed trades"
        ),
        "worst_strategy_this_month": _worst_month(rows),
        "worst_strategy_today": worst["strategy_id"],
    }


def _write_matrix(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    dates = sorted({str(row["date"]) for row in rows})
    strategies = sorted({str(row["strategy_id"]) for row in rows})
    output: list[dict[str, object]] = []
    for date_value in dates:
        row: dict[str, object] = {"date": date_value}
        for strategy in strategies:
            match = next(
                (
                    item
                    for item in rows
                    if item["date"] == date_value and item["strategy_id"] == strategy
                ),
                None,
            )
            row[strategy] = match["daily_return_pct"] if match else 0.0
        output.append(row)
    _write_csv(paths.calendar / "strategy_calendar_matrix.csv", output, ("date", *strategies))


def _write_monthly(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    output: list[dict[str, object]] = []
    keys = sorted({(str(row["date"])[:7], str(row["strategy_id"])) for row in rows})
    for month, strategy_id in keys:
        matches = [
            row
            for row in rows
            if str(row["date"]).startswith(month) and row["strategy_id"] == strategy_id
        ]
        output.append(
            {
                "month": month,
                "strategy_id": strategy_id,
                "monthly_return_pct": sum(_float(row.get("daily_return_pct")) for row in matches),
                "worst_drawdown_pct": min(_float(row.get("drawdown_pct")) for row in matches),
            }
        )
    _write_csv(
        paths.calendar / "strategy_monthly_returns.csv",
        output,
        ("month", "strategy_id", "monthly_return_pct", "worst_drawdown_pct"),
    )


def _write_equity(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    output = [
        {
            "date": row["date"],
            "evidence_mode": row["evidence_mode"],
            "strategy_id": row["strategy_id"],
            "ending_equity": row["ending_equity"],
            "cumulative_return_pct": row["cumulative_return_pct"],
        }
        for row in rows
    ]
    _write_csv(
        paths.calendar / "strategy_equity_curves.csv",
        output,
        ("date", "evidence_mode", "strategy_id", "ending_equity", "cumulative_return_pct"),
    )


def _write_drawdowns(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    output = [
        {
            "date": row["date"],
            "evidence_mode": row["evidence_mode"],
            "strategy_id": row["strategy_id"],
            "drawdown_pct": row["drawdown_pct"],
        }
        for row in rows
    ]
    _write_csv(
        paths.calendar / "strategy_drawdowns.csv",
        output,
        ("date", "evidence_mode", "strategy_id", "drawdown_pct"),
    )


def _write_streaks(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    output: list[dict[str, object]] = []
    for strategy in sorted({str(row["strategy_id"]) for row in rows}):
        matches = [row for row in rows if row["strategy_id"] == strategy]
        streak = 0
        for row in reversed(matches):
            value = _float(row.get("daily_return_pct"))
            if value == 0:
                break
            if streak == 0 or (streak > 0 and value > 0) or (streak < 0 and value < 0):
                streak += 1 if value > 0 else -1
            else:
                break
        output.append({"strategy_id": strategy, "current_streak": streak})
    _write_csv(paths.calendar / "strategy_streaks.csv", output, ("strategy_id", "current_streak"))


def _write_decay(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    output: list[dict[str, object]] = []
    for strategy in sorted({str(row["strategy_id"]) for row in rows}):
        matches = [row for row in rows if row["strategy_id"] == strategy]
        last_five = matches[-5:]
        recent = sum(_float(row.get("daily_return_pct")) for row in last_five)
        drawdown = min((_float(row.get("drawdown_pct")) for row in matches), default=0.0)
        output.append(
            {
                "strategy_id": strategy,
                "recent_return_pct": recent,
                "worst_drawdown_pct": drawdown,
                "status": "decaying" if recent < 0 or drawdown < -0.1 else "watch",
            }
        )
    _write_csv(
        paths.calendar / "strategy_decay_report.csv",
        output,
        ("strategy_id", "recent_return_pct", "worst_drawdown_pct", "status"),
    )


def _write_overtrading(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    output: list[dict[str, object]] = []
    for strategy in sorted({str(row["strategy_id"]) for row in rows}):
        matches = [row for row in rows if row["strategy_id"] == strategy]
        opened = sum(_int(row.get("trades_opened")) for row in matches[-5:])
        closed = sum(_int(row.get("trades_closed")) for row in matches[-5:])
        status = "overtrading" if opened + closed > 10 else "normal"
        output.append(
            {
                "strategy_id": strategy,
                "last_five_opened": opened,
                "last_five_closed": closed,
                "status": status,
            }
        )
    _write_csv(
        paths.calendar / "strategy_overtrading_report.csv",
        output,
        ("strategy_id", "last_five_opened", "last_five_closed", "status"),
    )


def _write_summary_md(paths: EvidenceVaultPaths, rows: list[dict[str, object]]) -> None:
    payload = _summary_payload(rows)
    lines = [
        "# Forward Evidence Calendar Summary",
        "",
        f"- Latest date: `{payload.get('latest_date', 'n/a')}`",
        f"- Best strategy today: `{payload.get('best_strategy_today', 'n/a')}`",
        f"- Worst strategy today: `{payload.get('worst_strategy_today', 'n/a')}`",
        f"- Best strategy this month: `{payload.get('best_strategy_this_month', 'n/a')}`",
        f"- Worst strategy this month: `{payload.get('worst_strategy_this_month', 'n/a')}`",
        f"- True forward days available: `{payload.get('true_forward_days_available', 0)}`",
        f"- Shadow replay days available: `{payload.get('shadow_replay_days_available', 0)}`",
        f"- Validation eligibility: {payload.get('validation_eligibility', 'n/a')}",
        "",
        "## Strategy Flags",
        "",
        f"- Drawdown: {', '.join(_list_str(payload.get('strategies_in_drawdown'))) or 'none'}",
        f"- Improving: {', '.join(_list_str(payload.get('strategies_improving'))) or 'none'}",
        f"- Decaying: {', '.join(_list_str(payload.get('strategies_decaying'))) or 'none'}",
        f"- Overtrading: {', '.join(_list_str(payload.get('strategies_overtrading'))) or 'none'}",
        "- RiskHub blocked: "
        f"{', '.join(_list_str(payload.get('strategies_blocked_by_riskhub'))) or 'none'}",
        f"- Quarantined: {', '.join(_list_str(payload.get('strategies_quarantined'))) or 'none'}",
    ]
    (paths.calendar / "strategy_calendar_summary.md").write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )


def _paper_calendar_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _frozen_by_date(
    paths: EvidenceVaultPaths, *, shadow: bool = False
) -> dict[str, dict[str, object]]:
    root = paths.shadow_replay / "frozen_picks" if shadow else paths.frozen_picks
    payloads: dict[str, dict[str, object]] = {}
    for path in sorted(root.glob("*_picks*.json")):
        payload = _read_json(path, {})
        if isinstance(payload, dict):
            payloads[str(payload.get("date", path.name[:10]))] = payload
    return payloads


def _best_month(rows: list[dict[str, object]]) -> str:
    totals = _month_totals(rows)
    return max(totals, key=lambda key: totals[key]) if totals else "n/a"


def _worst_month(rows: list[dict[str, object]]) -> str:
    totals = _month_totals(rows)
    return min(totals, key=lambda key: totals[key]) if totals else "n/a"


def _month_totals(rows: list[dict[str, object]]) -> dict[str, float]:
    totals: dict[str, float] = {}
    if not rows:
        return totals
    latest_month = max(str(row["date"])[:7] for row in rows)
    for row in rows:
        if str(row["date"]).startswith(latest_month):
            key = str(row["strategy_id"])
            totals[key] = totals.get(key, 0.0) + _float(row.get("daily_return_pct"))
    return totals


def _improving(rows: list[dict[str, object]]) -> list[str]:
    return sorted(
        {
            str(row["strategy_id"])
            for row in rows[-20:]
            if _float(row.get("daily_return_pct")) > 0
        }
    )


def _decaying(rows: list[dict[str, object]]) -> list[str]:
    return sorted(
        {
            str(row["strategy_id"])
            for row in rows[-20:]
            if _float(row.get("daily_return_pct")) < 0
            or _float(row.get("drawdown_pct")) < -0.1
        }
    )


def _overtrading(rows: list[dict[str, object]]) -> list[str]:
    return sorted(
        {
            str(row["strategy_id"])
            for row in rows
            if _int(row.get("trades_opened")) + _int(row.get("trades_closed")) > 10
        }
    )


def _quarantined() -> list[str]:
    payload = _read_json(Path("data/v2_paper_ops/reports/strategy_evidence_scores.json"), {})
    rows = payload.get("scores") if isinstance(payload, dict) else []
    if not isinstance(rows, list):
        return []
    return sorted(
        str(row.get("strategy_id"))
        for row in rows
        if isinstance(row, dict) and row.get("evidence_status") == "quarantined"
    )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
        handle.write("\n")


def _read_json(path: Path, default: object) -> object:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def _write_csv(path: Path, rows: list[dict[str, object]], fields: tuple[str, ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: _csv_value(row.get(field)) for field in fields})


def _csv_value(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, list | tuple):
        return " | ".join(str(item) for item in value)
    if isinstance(value, float):
        return f"{value:.8f}".rstrip("0").rstrip(".")
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def _float(value: object) -> float:
    if value in {None, ""}:
        return 0.0
    if isinstance(value, str | int | float):
        return float(value)
    return 0.0


def _int(value: object) -> int:
    if value in {None, ""}:
        return 0
    if isinstance(value, str | int | float):
        return int(float(value))
    return 0


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _list_str(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []
