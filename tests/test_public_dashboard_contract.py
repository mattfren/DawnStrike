from pathlib import Path


def test_public_dashboard_has_five_section_information_architecture() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    for label in ("Overview", "Calendar", "Performance", "Research", "System"):
        assert f">{label}<" in html
    assert "api/ui" not in html
    assert "No broker connection" in html
    assert 'id="safety-details"' in html
    assert "Market safety evidence" in html


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
    ):
        assert f'id="{element_id}"' in html
    assert 'loadJson("/data/calendar.json")' in script
    assert "record?.eligible_for_return" in script
    assert "numberOrZero" not in script
    assert "No canonical observation exists for this market day" in script
    assert ".calendar-workspace > * { min-width:0; }" in stylesheet
    assert ".calendar-trade-card p {" in stylesheet
    assert "overflow-wrap:anywhere;" in stylesheet
