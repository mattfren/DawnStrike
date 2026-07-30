from pathlib import Path


def test_public_dashboard_has_one_four_section_information_architecture() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    for label in ("Overview", "Performance", "Research", "System"):
        assert f">{label}<" in html
    assert "api/ui" not in html
    assert "No broker connection" in html


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
