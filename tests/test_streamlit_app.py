from streamlit.testing.v1 import AppTest


def test_streamlit_dashboard_renders_without_exceptions():
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
