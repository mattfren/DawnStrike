from streamlit.testing.v1 import AppTest


def test_streamlit_dashboard_renders_without_exceptions(tmp_path, monkeypatch):
    default_db = tmp_path / "missing.sqlite"
    monkeypatch.setenv("INTRADAY_DATABASE_PATH", str(default_db))
    app = AppTest.from_file("app.py", default_timeout=30)

    app.run()

    assert not app.exception
    assert not app.error
    assert not default_db.exists()
    assert [warning.value for warning in app.warning][0] == (
        f"SQLite database not found at {default_db}. "
        "The dashboard is showing an empty, read-only state."
    )
    assert [tab.label for tab in app.tabs] == [
        "Today",
        "Picks",
        "Calendar",
        "Performance",
        "System",
    ]
