from pathlib import Path


def test_public_dashboard_has_semantic_tables_and_headings() -> None:
    html = Path("web/index.html").read_text(encoding="utf-8")
    assert html.count("<h1") == 1
    assert html.count("<table") >= 2
    assert 'scope="col"' in html
    assert "Skip to content" in html
    assert html.count('aria-pressed="false"') == 4
    assert html.count('aria-live="polite"') >= 3
