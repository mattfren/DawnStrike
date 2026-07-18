from contextlib import nullcontext
from pathlib import Path
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


def test_resolved_no_entry_calendar_card_never_says_pending_or_zero() -> None:
    rendered = dashboard_app._calendar_card(
        {
            "date": "2026-07-16",
            "status": "RESOLVED NO ENTRY",
            "top_pick": "IQST",
            "top_pick_count": 1,
            "resolved_no_entry_count": 1,
            "missing_outcome_count": 0,
            "top3_return": None,
        },
        outside=False,
    )

    assert "Resolved · no entry" in rendered
    assert "N/A · no entry" in rendered
    assert "Trigger not reached" in rendered
    assert "Pending" not in rendered
    assert "0.00%" not in rendered


def test_mixed_complete_day_is_na_while_mixed_missing_day_stays_pending() -> None:
    mixed_complete = dashboard_app._calendar_card(
        {
            "date": "2026-07-17",
            "status": "AUDITED",
            "top_pick": "NOVA",
            "top_pick_count": 2,
            "resolved_no_entry_count": 1,
            "missing_outcome_count": 0,
            "top3_return": None,
        },
        outside=False,
    )
    mixed_missing = dashboard_app._calendar_card(
        {
            "date": "2026-07-18",
            "status": "OUTCOMES PARTIAL",
            "top_pick": "NOVA",
            "top_pick_count": 2,
            "resolved_no_entry_count": 1,
            "missing_outcome_count": 1,
            "top3_return": None,
        },
        outside=False,
    )

    assert "N/A · mixed entries" in mixed_complete
    assert "Mixed entry outcomes" in mixed_complete
    assert "Pending" not in mixed_complete
    assert "Pending" in mixed_missing
    assert "Outcome needed" in mixed_missing


def test_resolved_no_entry_return_cells_are_na_without_masking_pending_rows() -> None:
    resolved_row = {
        "audit_status": "resolved_no_entry",
        "entry_price": None,
        "entry_time": None,
        "recommended_exit_policy": "not_recorded",
        "recommended_exit_price": None,
        "close_return": None,
    }
    for column in (
        "entry_price",
        "entry_time",
        "recommended_exit_price",
        "close_return",
    ):
        assert dashboard_app._format_table_cell(resolved_row, column) == "N/A"
    assert (
        dashboard_app._format_table_cell(resolved_row, "recommended_exit_policy")
        == "N/A · no entry"
    )
    assert (
        dashboard_app._format_table_cell(
            {"audit_status": "outcome_needed", "close_return": None},
            "close_return",
        )
        == "Pending"
    )


def test_runtime_environment_replaces_blank_values_with_retained_paths(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    data_root = tmp_path / "data"
    paper_root = data_root / "v2_paper_ops_live"
    mover_root = data_root / "v2_mover_pattern_lab"
    paper_root.mkdir(parents=True)
    mover_root.mkdir()
    database_path = data_root / "shadow_real.sqlite"
    database_path.touch()
    monkeypatch.setattr(dashboard_app, "APP_REPO_ROOT", tmp_path)
    for variable in (
        "DAWNSTRIKE_RUNTIME_ROOT",
        "INTRADAY_DATABASE_PATH",
        "DAWNSTRIKE_PAPER_OPS_ROOT",
        "DAWNSTRIKE_MOVER_LAB_ROOT",
    ):
        monkeypatch.setenv(variable, "")

    dashboard_app._configure_runtime_environment()

    assert dashboard_app.os.environ["DAWNSTRIKE_RUNTIME_ROOT"] == str(tmp_path.resolve())
    assert dashboard_app.os.environ["INTRADAY_DATABASE_PATH"] == str(
        database_path.resolve()
    )
    assert dashboard_app.os.environ["DAWNSTRIKE_PAPER_OPS_ROOT"] == str(
        paper_root.resolve()
    )
    assert dashboard_app.os.environ["DAWNSTRIKE_MOVER_LAB_ROOT"] == str(
        mover_root.resolve()
    )


def test_runtime_environment_anchors_relative_overrides_to_runtime_root(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(dashboard_app, "APP_REPO_ROOT", tmp_path)
    monkeypatch.setenv("DAWNSTRIKE_RUNTIME_ROOT", ".")
    monkeypatch.setenv(
        "INTRADAY_DATABASE_PATH", str(Path("retained") / "custom.sqlite")
    )
    monkeypatch.setenv("DAWNSTRIKE_PAPER_OPS_ROOT", str(Path("retained") / "paper"))
    monkeypatch.setenv("DAWNSTRIKE_MOVER_LAB_ROOT", str(Path("retained") / "movers"))

    dashboard_app._configure_runtime_environment()

    assert dashboard_app.os.environ["DAWNSTRIKE_RUNTIME_ROOT"] == str(tmp_path.resolve())
    assert dashboard_app.os.environ["INTRADAY_DATABASE_PATH"] == str(
        (tmp_path / "retained" / "custom.sqlite").resolve()
    )
    assert dashboard_app.os.environ["DAWNSTRIKE_PAPER_OPS_ROOT"] == str(
        (tmp_path / "retained" / "paper").resolve()
    )
    assert dashboard_app.os.environ["DAWNSTRIKE_MOVER_LAB_ROOT"] == str(
        (tmp_path / "retained" / "movers").resolve()
    )
