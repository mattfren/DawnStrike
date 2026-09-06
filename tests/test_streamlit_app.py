from pathlib import Path

from streamlit.testing.v1 import AppTest

# ``AppTest.from_file`` resolves a relative path against the file that calls it,
# not the working directory, so the dashboard entrypoint must be addressed
# absolutely from the repository root.  A relative "app.py" silently resolves to
# tests/app.py and the only end-to-end dashboard render check stops running.
APP = Path(__file__).resolve().parents[1] / "app.py"


def test_streamlit_dashboard_renders_without_exceptions(tmp_path, monkeypatch):
    default_db = tmp_path / "missing.sqlite"
    monkeypatch.setenv("INTRADAY_DATABASE_PATH", str(default_db))
    app = AppTest.from_file(str(APP), default_timeout=90)

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
