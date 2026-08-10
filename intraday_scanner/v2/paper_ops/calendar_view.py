"""Static HTML calendar viewer for PaperOps returns."""

from __future__ import annotations

import csv
import html
from pathlib import Path

from intraday_scanner.v2.paper_ops.engine import PaperOpsPaths
from intraday_scanner.v2.paper_ops.observer_safety import require_observer_tree
from intraday_scanner.v2.paper_ops.session_gaps import load_forward_session_gaps


def write_calendar_view(*, output_root: Path = Path("data/v2_paper_ops")) -> dict[str, object]:
    require_observer_tree(output_root, required_files=("calendar/strategy_daily_returns.csv",))
    paths = PaperOpsPaths.resolve(output_root)
    rows = _read_rows(paths.calendar / "strategy_daily_returns.csv")
    gaps, gap_errors = load_forward_session_gaps(paths)
    html_path = paths.calendar / "calendar_view.html"
    html_path.write_text(_html(rows, gaps, gap_errors), encoding="utf-8")
    return {
        "calendar_view": html_path.as_posix(),
        "rows": len(rows),
        "terminal_missing_sessions": len(gaps),
        "gap_errors": gap_errors,
        "status": ("failed" if gap_errors else "passed_with_warnings" if gaps else "passed"),
    }


def _read_rows(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def _html(
    rows: list[dict[str, str]],
    gaps: list[dict[str, object]],
    gap_errors: list[str],
) -> str:
    headers = (
        "date",
        "mode",
        "strategy_id",
        "daily_return_pct",
        "cumulative_return_pct",
        "drawdown_pct",
    )
    body = []
    for row in rows:
        daily = float(row.get("daily_return_pct", "0") or 0)
        css_class = "flat"
        if daily > 0:
            css_class = "positive"
        elif daily < 0:
            css_class = "negative"
        cells = "".join(f"<td>{html.escape(str(row.get(header, '')))}</td>" for header in headers)
        body.append(f'<tr class="{css_class}">{cells}</tr>')
    gap_rows = "".join(
        '<tr class="missing">'
        f"<td>{html.escape(str(row.get('market_date', '')))}</td>"
        f"<td>{html.escape(str(row.get('reason_code', '')))}</td>"
        "<td>Missing - not zero</td></tr>"
        for row in gaps
    )
    gap_error_rows = "".join(f"<li>{html.escape(error)}</li>" for error in gap_errors)
    return (
        "<!doctype html>\n"
        '<html><head><meta charset="utf-8"><title>PaperOps Calendar</title>'
        "<style>"
        "body{font-family:Arial,sans-serif;margin:24px;color:#18202a}"
        "table{border-collapse:collapse;width:100%;font-size:13px}"
        "th,td{border:1px solid #ccd3dc;padding:6px;text-align:left}"
        "th{background:#eef2f6}.positive{background:#e7f6ec}"
        ".negative{background:#fdecec}.flat{background:#fafafa}"
        ".missing{background:#fff4d6}"
        "</style></head><body>"
        "<h1>PaperOps Calendar</h1>"
        "<p>Returns are shown only when sourced. Missing sessions are never treated as zero.</p>"
        "<table><thead><tr>"
        + "".join(f"<th>{header}</th>" for header in headers)
        + "</tr></thead><tbody>"
        + "".join(body)
        + "</tbody></table>"
        + "<h2>Terminal missing sessions</h2>"
        + "<table><thead><tr><th>Date</th><th>Reason</th><th>Return status</th>"
        + "</tr></thead><tbody>"
        + gap_rows
        + "</tbody></table>"
        + ("<h2>Gap ledger errors</h2><ul>" + gap_error_rows + "</ul>" if gap_errors else "")
        + "</body></html>\n"
    )
