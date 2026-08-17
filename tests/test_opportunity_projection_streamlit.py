from pathlib import Path

from streamlit.testing.v1 import AppTest


def test_streamlit_projection_is_invisible_by_default(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "missing.sqlite"
    monkeypatch.setenv("INTRADAY_DATABASE_PATH", str(database))
    monkeypatch.delenv("DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED", raising=False)
    app = AppTest.from_file("app.py", default_timeout=30)

    app.run()

    assert not app.exception
    assert [tab.label for tab in app.tabs] == [
        "Today",
        "Picks",
        "Calendar",
        "Performance",
        "System",
    ]
    assert not any("Today's Best Opportunities" in item.value for item in app.markdown)


def test_streamlit_projection_reports_missing_data_without_no_trade_claim(
    tmp_path: Path,
    monkeypatch,
) -> None:
    database = tmp_path / "missing.sqlite"
    monkeypatch.setenv("INTRADAY_DATABASE_PATH", str(database))
    monkeypatch.setenv("DAWNSTRIKE_OPPORTUNITY_PROJECTION_ENABLED", "true")
    app = AppTest.from_file("app.py", default_timeout=30)

    app.run()

    assert not app.exception
    assert any("Today's Best Opportunities" in item.value for item in app.markdown)
    warnings = [item.value for item in app.warning]
    assert any("no persisted research store was found" in item for item in warnings)
    assert all("NO QUALIFYING TRADE CURRENTLY EXISTS." not in item for item in warnings)
