"""Historical Alpha Calendar report writer."""

from __future__ import annotations

import csv
import json
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from intraday_scanner.dashboard.data_loader import (
    load_calendar_day_detail,
    load_calendar_days,
    load_calendar_equity_curve,
    load_calendar_missing_outcomes,
)


def calendar_report(
    *,
    db_path: str | Path = "data/shadow_real.sqlite",
    out_dir: str | Path = "outputs/calendar_report",
    start: str | None = None,
    end: str | None = None,
    month: str | None = None,
) -> dict[str, Any]:
    start_date, end_date = _resolve_range(start=start, end=end, month=month)
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    days = load_calendar_days(db_path, start_date, end_date)
    details = {row["date"]: load_calendar_day_detail(db_path, row["date"]) for row in days}
    equity_curve = load_calendar_equity_curve(db_path, start_date, end_date)
    missing = load_calendar_missing_outcomes(db_path, start_date, end_date)

    paths = {
        "calendar_days": output_dir / "calendar_days.csv",
        "calendar_day_details": output_dir / "calendar_day_details.json",
        "calendar_equity_curve": output_dir / "calendar_equity_curve.csv",
        "missing_outcomes": output_dir / "missing_outcomes.csv",
        "calendar_report": output_dir / "calendar_report.md",
    }
    _write_csv(paths["calendar_days"], days)
    _write_json(paths["calendar_day_details"], details)
    _write_csv(paths["calendar_equity_curve"], equity_curve)
    _write_csv(paths["missing_outcomes"], missing)
    _write_markdown(
        paths["calendar_report"],
        db_path=str(db_path),
        start_date=start_date,
        end_date=end_date,
        days=days,
        missing=missing,
        equity_curve=equity_curve,
    )
    return {
        "status": "complete",
        "db_path": str(db_path),
        "out_dir": str(output_dir),
        "start": start_date,
        "end": end_date,
        "day_count": len(days),
        "missing_outcome_count": len(missing),
        "audited_day_count": sum(1 for row in days if row.get("status") == "AUDITED"),
        "paths": {key: str(value) for key, value in paths.items()},
    }


def _resolve_range(
    *,
    start: str | None,
    end: str | None,
    month: str | None,
) -> tuple[str, str]:
    if month:
        anchor = datetime.strptime(month + "-01", "%Y-%m-%d").date()
        next_month = (anchor.replace(day=28) + timedelta(days=4)).replace(day=1)
        return anchor.isoformat(), (next_month - timedelta(days=1)).isoformat()
    today = date.today()
    start_date = _date_key(start) if start else today.replace(day=1).isoformat()
    end_date = _date_key(end) if end else today.isoformat()
    if start_date > end_date:
        return end_date, start_date
    return start_date, end_date


def _date_key(value: str | None) -> str:
    text = str(value or "").strip()
    if len(text) >= 10:
        return text[:10]
    raise ValueError("Date values must be YYYY-MM-DD.")


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    fieldnames = _fieldnames(rows)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow(row)


def _fieldnames(rows: list[dict[str, Any]]) -> list[str]:
    fields: list[str] = []
    for row in rows:
        for key in row:
            if key not in fields:
                fields.append(key)
    return fields or ["date", "status"]


def _write_markdown(
    path: Path,
    *,
    db_path: str,
    start_date: str,
    end_date: str,
    days: list[dict[str, Any]],
    missing: list[dict[str, Any]],
    equity_curve: list[dict[str, Any]],
) -> None:
    audited = [row for row in days if row.get("status") == "AUDITED"]
    real_days = len(audited)
    sufficiency = (
        "insufficient"
        if real_days < 20
        else "early evidence"
        if real_days < 60
        else "stronger evidence"
    )
    latest_curve = equity_curve[-1] if equity_curve else {}
    lines = [
        "# Dawnstrike Historical Alpha Calendar Report",
        "",
        "Research/watchlist only. No orders are placed.",
        "",
        f"- Database: `{db_path}`",
        f"- Date range: {start_date} to {end_date}",
        f"- Calendar days: {len(days)}",
        f"- Audited days: {real_days}",
        f"- Evidence status: {sufficiency}",
        f"- Missing outcomes: {len(missing)}",
        "- Latest top1 compounded close return: "
        f"{latest_curve.get('top1_compounded_return', 'n/a')}",
        "- Latest top3 compounded close return: "
        f"{latest_curve.get('top3_compounded_return', 'n/a')}",
        "- Latest top5 compounded close return: "
        f"{latest_curve.get('top5_compounded_return', 'n/a')}",
        "",
        "Evidence is insufficient until at least 20 real market days are audited.",
        "Scenario returns are not recommended returns. Recommended returns require an explicit",
        "saved exit or a saved monitor exit signal with a price.",
        "Missing outcomes are `Outcome needed` or `Pending`, never zero.",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
