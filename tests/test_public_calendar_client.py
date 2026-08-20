from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path


def _javascript_function(source: str, name: str) -> str:
    start = source.index(f"function {name}(")
    opening_brace = source.index("{", start)
    depth = 0
    for index in range(opening_brace, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[start : index + 1]
    raise AssertionError(f"JavaScript function is not balanced: {name}")


def test_calendar_refresh_revalidates_without_resetting_calendar_state() -> None:
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")

    assert 'cache: "no-store"' in source
    assert 'headers: { "Cache-Control": "no-cache" }' in source
    assert 'window.addEventListener("focus", requestDashboardRefresh)' in source
    assert 'document.addEventListener("visibilitychange", requestDashboardRefresh)' in source
    assert "window.setInterval(requestDashboardRefresh, DASHBOARD_REFRESH_INTERVAL_MS)" in source
    assert "if (initial) initializeCalendarFilters();" in source
    assert "const previousCalendarMonth = state.calendarMonth;" in source
    assert 'state.calendarMonth = String(calendar.payload?.as_of_market_date' in source


def test_calendar_client_distinguishes_historical_missing_from_unpublished_and_future() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the public calendar client contract"
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    helpers = "\n".join(
        _javascript_function(source, name)
        for name in (
            "calendarPublicationDate",
            "calendarPublicationStatus",
            "calendarCellStatus",
        )
    )
    probe = f"""
const state = {{ calendar: {{ as_of_market_date: "2026-08-19" }} }};
function calendarCurrentDateChicago() {{ return "2026-08-20"; }}
{helpers}
console.log(JSON.stringify({{
  historicalMissing: calendarCellStatus(
    {{ date: "2026-08-19", market_session_status: "open" }}, []),
  unpublished: calendarCellStatus(
    {{ date: "2026-08-20", market_session_status: "open" }}, []),
  future: calendarCellStatus(
    {{ date: "2026-08-21", market_session_status: "open" }}, []),
  closed: calendarCellStatus(
    {{ date: "2026-08-16", market_session_status: "closed" }}, []),
}}));
"""
    completed = subprocess.run(
        [node, "-e", probe], check=True, capture_output=True, text=True
    )
    assert json.loads(completed.stdout) == {
        "historicalMissing": "MISSING",
        "unpublished": "NOT_PUBLISHED",
        "future": "FUTURE",
        "closed": "UNAVAILABLE",
    }
