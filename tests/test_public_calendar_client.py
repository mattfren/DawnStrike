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
    assert 'loadJson("/api/readiness", request)' in source
    assert '[200, 503].includes(dynamic.status)' in source
    assert 'loadJson("/readiness.json", request)' in source
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
            "calendarFreshness",
            "calendarTimestampIsPast",
            "calendarFreshnessPublicationStatus",
            "calendarFallbackSessionStatus",
            "calendarContractPublicationStatus",
            "calendarPublicationStatus",
            "calendarCellStatus",
        )
    )
    probe = f"""
const CALENDAR_CLOSED_DATES = new Set(["2026-08-01"]);
const state = {{ calendar: {{ as_of_market_date: "2026-08-19" }} }};
function calendarNow() {{ return new Date("2026-08-20T12:00:00Z"); }}
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


def test_calendar_client_labels_publication_states_explicitly() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the public calendar client contract"
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    helper = _javascript_function(source, "calendarStatusLabel")
    probe = f"""
{helper}
console.log(JSON.stringify({{
  unpublished: calendarStatusLabel("NOT_PUBLISHED"),
  future: calendarStatusLabel("FUTURE"),
  stale: calendarStatusLabel("STALE"),
}}));
"""
    completed = subprocess.run(
        [node, "-e", probe], check=True, capture_output=True, text=True
    )
    assert json.loads(completed.stdout) == {
        "unpublished": "Not yet published",
        "future": "Future",
        "stale": "Stale / overdue",
    }


def test_calendar_client_prefers_contract_fields_and_marks_overdue_publication() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the public calendar client contract"
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    helpers = "\n".join(
        _javascript_function(source, name)
        for name in (
            "calendarPublicationDate",
            "calendarCurrentDateChicago",
            "calendarFreshness",
            "calendarTimestampIsPast",
            "calendarFreshnessPublicationStatus",
            "calendarFallbackSessionStatus",
            "calendarContractPublicationStatus",
            "calendarPublicationStatus",
            "calendarCellStatus",
        )
    )
    probe = f"""
const CALENDAR_CLOSED_DATES = new Set();
const state = {{ calendar: {{
  as_of_market_date: "2026-08-18",
  freshness: {{
    status: "current",
    authoritative_as_of_market_date: "2026-08-18",
    next_publication_market_date: "2026-08-20",
    next_publication_at: "2026-08-20T22:30:00+00:00",
    next_stale_after: "2026-08-20T23:30:00+00:00",
  }},
}} }};
let now = new Date("2026-08-20T22:00:00+00:00");
function calendarNow() {{ return now; }}
{helpers}
const preDeadline = calendarCellStatus({{
  date: "2026-08-20", status: "PENDING",
  publication_state: "awaiting_publication", authoritative: false,
  publication_due_at: "2026-08-20T22:30:00+00:00",
}}, []);
const future = calendarCellStatus({{
  date: "2026-08-21", status: "PENDING", publication_state: "future", authoritative: false,
}}, []);
const closedWeekend = calendarCellStatus({{ date: "2026-08-22" }}, []);
const historicalBefore = calendarCellStatus({{
  date: "2026-08-17", status: "MISSING", publication_state: "published", authoritative: true,
}}, []);
const historicalAt = calendarCellStatus({{
  date: "2026-08-18", status: "MISSING", publication_state: "published", authoritative: true,
}}, []);
now = new Date("2026-08-20T23:45:00+00:00");
const overdue = calendarCellStatus({{ date: "2026-08-19" }}, []);
const overdueCurrent = calendarCellStatus({{ date: "2026-08-20" }}, []);
console.log(JSON.stringify({{
  preDeadline, future, closedWeekend, historicalBefore, historicalAt,
  overdue, overdueCurrent,
}}));
"""
    completed = subprocess.run(
        [node, "-e", probe], check=True, capture_output=True, text=True
    )
    assert json.loads(completed.stdout) == {
        "preDeadline": "NOT_PUBLISHED",
        "future": "FUTURE",
        "closedWeekend": "UNAVAILABLE",
        "historicalBefore": "MISSING",
        "historicalAt": "MISSING",
        "overdue": "STALE",
        "overdueCurrent": "STALE",
    }
