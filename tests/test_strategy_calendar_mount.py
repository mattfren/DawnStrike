from contextlib import nullcontext
from typing import Any

import app as dashboard_app


def test_strategy_calendar_is_default_and_truthless_state_falls_back(
    monkeypatch: Any,
) -> None:
    state = {"sentinel": "preserved"}
    calls: list[tuple[str, Any]] = []
    warnings: list[str] = []
    expanders: list[tuple[str, bool]] = []

    def segmented_control(label: str, **kwargs: Any) -> str:
        assert label == "Calendar view"
        assert kwargs["options"] == ["Strategy fleet", "Intraday signal archive"]
        assert kwargs["default"] == "Strategy fleet"
        return "Strategy fleet"

    def expander(label: str, *, expanded: bool) -> Any:
        expanders.append((label, expanded))
        return nullcontext()

    monkeypatch.setattr(dashboard_app.st, "segmented_control", segmented_control)
    monkeypatch.setattr(dashboard_app.st, "warning", warnings.append)
    monkeypatch.setattr(dashboard_app.st, "expander", expander)
    monkeypatch.setattr(
        dashboard_app,
        "render_strategy_calendar",
        lambda: calls.append(("fleet", None)) or False,
    )
    monkeypatch.setattr(
        dashboard_app,
        "render_mover_strategy_calendar",
        lambda: calls.append(("movers", None)) or False,
    )
    monkeypatch.setattr(
        dashboard_app,
        "_alphaops_historical_calendar",
        lambda received: calls.append(("archive", received)),
    )

    dashboard_app._historical_calendar(state)

    assert calls == [("fleet", None), ("movers", None), ("archive", state)]
    assert expanders == [("Intraday signal archive", True)]
    assert len(warnings) == 1
    assert "No return data was fabricated" in warnings[0]


def test_intraday_archive_selection_skips_strategy_renderer(monkeypatch: Any) -> None:
    state = {"sentinel": "preserved"}
    calls: list[tuple[str, Any]] = []

    monkeypatch.setattr(
        dashboard_app.st,
        "segmented_control",
        lambda *args, **kwargs: "Intraday signal archive",
    )
    monkeypatch.setattr(
        dashboard_app,
        "render_strategy_calendar",
        lambda: calls.append(("fleet", None)) or True,
    )
    monkeypatch.setattr(
        dashboard_app,
        "render_mover_strategy_calendar",
        lambda: calls.append(("movers", None)) or True,
    )
    monkeypatch.setattr(
        dashboard_app,
        "_alphaops_historical_calendar",
        lambda received: calls.append(("archive", received)),
    )

    dashboard_app._historical_calendar(state)

    assert calls == [("archive", state)]
