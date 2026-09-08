import pathlib

from streamlit.testing.v1 import AppTest

# Streamlit 1.63 resolves a relative AppTest path against the file that
# calls it, not the working directory, and app.py lives at the repo root.
APP = str(pathlib.Path(__file__).resolve().parents[1] / "app.py")


def test_streamlit_dashboard_renders_without_exceptions(tmp_path, monkeypatch):
    default_db = tmp_path / "missing.sqlite"
    monkeypatch.setenv("INTRADAY_DATABASE_PATH", str(default_db))
    app = AppTest.from_file(APP, default_timeout=30)

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
