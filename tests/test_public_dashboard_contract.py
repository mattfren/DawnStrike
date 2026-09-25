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


def test_public_dashboard_has_five_section_information_architecture() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    for label in ("Overview", "Calendar", "Performance", "Research", "System"):
        assert f">{label}<" in html
    assert "api/ui" not in html
    assert "No broker connection" in html
    assert 'id="safety-details"' in html
    assert "Market safety evidence" in html


def test_public_dashboard_has_a_read_only_trade_journal() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    script = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")

    assert '>Journal<' in html
    assert 'id="view-journal"' in html
    assert 'id="journal-status-filter"' in html
    assert 'id="journal-table"' in html
    assert "Paper only" in html
    assert "function renderJournal()" in script
    assert "official_forward_paper" in script
    assert "No published paper trades match this filter" in script


def test_public_dashboard_uses_command_center_visual_system() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    stylesheet = Path("web/assets/dawnstrike.css").read_text(encoding="utf-8")

    assert "Evidence-grade alpha operations" in html
    assert "Signal, <span>without the noise.</span>" in html
    assert 'class="desk-status"' in html
    assert 'class="hero-note"' in html
    assert "--signal:#b9ff66;" in stylesheet
    assert "position:sticky;" in stylesheet
    assert "@media (prefers-reduced-motion:reduce)" in stylesheet


def test_public_dashboard_overview_exposes_required_portfolio_metrics() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    for metric_id in (
        "kpi-date",
        "kpi-return",
        "kpi-cumulative",
        "kpi-benchmark",
        "kpi-excess",
        "kpi-pnl",
        "kpi-drawdown",
        "kpi-open",
        "kpi-coverage",
        "kpi-system",
    ):
        assert f'id="{metric_id}"' in html
    assert "Not reported" in html
    assert 'id="kpi-context"' in html


def test_public_dashboard_paginates_bounded_detail_tables() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    script = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")

    assert 'id="performance-page-status"' in html
    assert 'id="research-page-status"' in html
    assert html.count('data-direction="-1"') == 2
    assert html.count('data-direction="1"') == 2
    assert "const PAGE_SIZE = 10;" in script
    assert "updatePager" in script
    assert "formatPercentText(item.gross_return_pct)" in script


def test_public_dashboard_calendar_is_filterable_and_null_safe() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    script = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    stylesheet = Path("web/assets/dawnstrike.css").read_text(encoding="utf-8")

    for element_id in (
        "calendar-grid",
        "calendar-cohort-filter",
        "calendar-strategy-filter",
        "calendar-version-filter",
        "calendar-policy-filter",
        "calendar-account-filter",
        "calendar-detail-metrics",
        "calendar-detail-trades",
        "calendar-observation-note",
        "calendar-show-observed",
    ):
        assert f'id="{element_id}"' in html
    assert 'loadJson("/data/calendar.json")' in script
    assert "const net = record.eligible_for_return" in script
    assert "calendarDisplayReturn(record)" in script
    assert 'basis: "gross_observed"' in script
    assert "showLatestObservedCalendar" in script
    assert "Gross ${returnText}" in script
    assert "Gross observed / net pending" in html
    assert "gross P&L divided by deployed capital" in script
    assert "calendarCellStatus(day, matches)" in script
    assert 'records.length === 1' in script
    assert 'market_session_status === "closed" ? "UNAVAILABLE" : "MISSING"' in script
    assert "record?.status || day?.status" not in script
    assert "numberOrZero" not in script
    assert "No canonical observation exists for this market day" in script
    assert ".calendar-workspace > * { min-width:0; }" in stylesheet
    assert ".calendar-trade-card p {" in stylesheet
    assert "overflow-wrap:anywhere;" in stylesheet


def test_calendar_display_keeps_eligible_net_and_pending_gross_distinct() -> None:
    node = shutil.which("node")
    assert node is not None, "Node.js is required for the public Calendar display contract"
    source = Path("web/assets/dawnstrike.js").read_text(encoding="utf-8")
    helpers = "\n".join(
        _javascript_function(source, name)
        for name in ("numberOrNull", "calendarDisplayReturn")
    )
    probe = f"""
{helpers}
console.log(JSON.stringify({{
  net: calendarDisplayReturn({{
    eligible_for_return: true,
    net_return_pct: 1.25,
    observed_gross_return_pct: 1.5,
  }}),
  gross: calendarDisplayReturn({{
    eligible_for_return: false,
    net_return_pct: null,
    observed_gross_return_pct: -2.5,
  }}),
  missing: calendarDisplayReturn({{
    eligible_for_return: false,
    net_return_pct: null,
    observed_gross_return_pct: null,
  }}),
}}));
"""

    completed = subprocess.run(
        [node, "-e", probe],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["net"] == {
        "value": 1.25,
        "basis": "net_after_costs",
        "label": "Net return",
    }
    assert result["gross"] == {
        "value": -2.5,
        "basis": "gross_observed",
        "label": "Gross observed return",
    }
    assert result["missing"] == {
        "value": None,
        "basis": None,
        "label": "Return not reported",
    }
