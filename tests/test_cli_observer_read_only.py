"""Regression coverage for the CLI's read-only observer contract."""

from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner import cli
from intraday_scanner.config import ScannerConfig

OBSERVER_COMMANDS = {
    "audit-manual-outcomes",
    "free-shadow-report",
    "web-source-doctor",
    "daily-orchestrator-status",
    "alpha-status",
    "alpha-doctor",
    "alpha-report",
    "scenario-doctor",
    "scenario-report",
    "historical-report",
    "calendar-report",
    "paper-audit",
    "audit-latest",
    "backfill-audit",
    "performance-report",
    "probability-doctor",
    "scheduler-doctor",
    "dashboard-doctor",
}
OBSERVER_KEYWORDS = ("status", "audit", "report", "doctor", "verify")
NON_KEYWORD_READ_ONLY_HANDLERS = {
    "_run_evaluate_intelligence_outcomes",
    "_run_import_manual_outcomes",
    "_run_alpha_v6_attribution",
    "_run_alpha_v6_research_packet",
    "_run_alpha_v6_preview_universe",
}
NON_KEYWORD_OBSERVER_COMMANDS = {
    "outcome-gap": "_run_outcome_gap",
    "alpha-attribution": "_run_alpha_attribution",
    "attribute-returns": "_run_attribute_returns",
    "alpha-alert-replay": "_run_alpha_alert_replay",
}


def _parser_command_literals() -> set[str]:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    return {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_parser"
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
    }


def test_observer_command_registry_is_complete_and_classified() -> None:
    actual = {
        name for name in _parser_command_literals() if any(key in name for key in OBSERVER_KEYWORDS)
    }
    drift = actual ^ OBSERVER_COMMANDS
    assert actual == OBSERVER_COMMANDS, (
        "CLI observer registry drifted; classify every status/audit/report/doctor/verify "
        "command in docs/audit/harvest/CYCLE-001_read_only_surface_matrix.md: "
        f"{drift}"
    )


def test_non_keyword_observer_handlers_remain_explicitly_read_only() -> None:
    source = Path(cli.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    handlers = {
        node.name: ast.get_source_segment(source, node) or ""
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
    }
    for handler in NON_KEYWORD_READ_ONLY_HANDLERS:
        assert "read_only=" in handlers[handler], f"{handler} must keep an explicit read-only store"
    assert "persist=False" in handlers["_run_alpha_v6_attribution"]
    commands = _parser_command_literals()
    assert set(NON_KEYWORD_OBSERVER_COMMANDS) <= commands
    assert set(NON_KEYWORD_OBSERVER_COMMANDS.values()) <= set(handlers)


@pytest.mark.parametrize(
    ("runner", "attrs"),
    [
        ("_run_audit_manual_outcomes", {"out_dir": "out"}),
        ("_run_free_shadow_report", {"out_dir": "out"}),
        ("_run_import_manual_outcomes", {"input": "outcomes.csv", "replace": False}),
        (
            "_run_evaluate_intelligence_outcomes",
            {"run_id": None, "min_samples": 20, "out_dir": "out"},
        ),
        (
            "_run_audit_latest",
            {
                "minute_bars": "bars.csv",
                "out_dir": "out",
                "top_n": 3,
                "slippage_bps": None,
                "entry_mode": "open",
            },
        ),
        ("_run_performance_report", {}),
    ],
)
@pytest.mark.parametrize("persist", [False, True])
def test_optional_persist_observers_construct_correct_store(
    monkeypatch: pytest.MonkeyPatch,
    runner: str,
    attrs: dict[str, object],
    persist: bool,
    tmp_path: Path,
) -> None:
    observed: list[bool] = []

    class RecorderStore:
        def __init__(self, _path: Path, *, read_only: bool = False) -> None:
            observed.append(read_only)

        def load_latest_scan(self) -> None:
            return None

        def load_paper_audit_trades(self) -> list[object]:
            return []

        def load_latest_paper_audit_summary(self) -> None:
            return None

    monkeypatch.setattr(cli, "SQLiteScanStore", RecorderStore)
    monkeypatch.setattr(
        cli, "load_config", lambda **_kwargs: ScannerConfig(database_path=tmp_path / "state.sqlite")
    )
    if runner == "_run_audit_manual_outcomes":
        monkeypatch.setattr(
            cli,
            "audit_manual_outcomes",
            lambda **_kwargs: {
                "summary": {},
                "paths": {"trades": tmp_path / "trades.csv", "summary": tmp_path / "summary.json"},
            },
        )
    elif runner == "_run_free_shadow_report":
        monkeypatch.setattr(
            cli,
            "build_free_shadow_report",
            lambda **_kwargs: {"report": {}, "paths": {"report": tmp_path / "report.json"}},
        )
    elif runner == "_run_import_manual_outcomes":
        monkeypatch.setattr(cli, "import_manual_outcomes", lambda **_kwargs: {})
    elif runner == "_run_evaluate_intelligence_outcomes":
        monkeypatch.setattr(
            cli, "evaluate_intelligence_outcomes", lambda **_kwargs: {"summary": {}}
        )
        monkeypatch.setattr(
            cli,
            "write_intelligence_outcome_outputs",
            lambda *_args: {"rows": tmp_path / "rows.csv", "summary": tmp_path / "summary.json"},
        )

    args = SimpleNamespace(db_path=str(tmp_path / "state.sqlite"), persist=persist, **attrs)
    getattr(cli, runner)(args)
    assert observed == [not persist]


def test_v6_observer_handlers_construct_read_only_store_and_never_persist(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    observed: list[bool] = []
    attribution_persist: list[bool] = []

    class RecorderStore:
        def __init__(self, _path: str, *, read_only: bool = False) -> None:
            observed.append(read_only)

    monkeypatch.setattr(cli, "SQLiteScanStore", RecorderStore)
    monkeypatch.setattr(
        cli,
        "build_v6_failure_attribution",
        lambda _store, *, persist: attribution_persist.append(persist) or {},
    )
    monkeypatch.setattr(cli, "write_alpha_v6_research_packet", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(
        cli,
        "_read_alpha_v6_universe_candidate",
        lambda _path: {"as_of_date": "2026-08-01", "members": [], "source_lineage": []},
    )
    monkeypatch.setattr(cli, "preview_alpha_v6_universe", lambda *_args, **_kwargs: {})

    cli._run_alpha_v6_attribution(SimpleNamespace(db_path=str(tmp_path / "state.sqlite")))
    cli._run_alpha_v6_research_packet(
        SimpleNamespace(db_path=str(tmp_path / "state.sqlite"), code_sha="test", out_dir=tmp_path)
    )
    cli._run_alpha_v6_preview_universe(
        SimpleNamespace(db_path=str(tmp_path / "state.sqlite"), input=tmp_path / "candidate.json")
    )

    assert observed == [True, True, True]
    assert attribution_persist == [False]


def test_non_keyword_observer_handlers_dispatch_without_writer_flags(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls: dict[str, dict[str, object]] = {}

    def record(name: str, result: dict[str, object]):
        def inner(**kwargs: object) -> dict[str, object]:
            calls[name] = kwargs
            return result

        return inner

    monkeypatch.setattr(cli, "outcome_gap_report", record("outcome-gap", {"status": "NO_ELIGIBLE"}))
    monkeypatch.setattr(
        cli,
        "generate_alpha_attribution_report",
        record("alpha-attribution", {"status": "complete"}),
    )
    monkeypatch.setattr(cli, "attribute_returns", record("attribute-returns", {}))
    monkeypatch.setattr(
        cli, "write_alpha_alert_replay_report", record("alpha-alert-replay", {"status": "PASS"})
    )
    db_path = str(tmp_path / "state.sqlite")
    assert (
        cli._run_outcome_gap(
            SimpleNamespace(db_path=db_path, market_date=None, out=tmp_path / "gap.json")
        )
        == 0
    )
    assert (
        cli._run_alpha_attribution(
            SimpleNamespace(
                db_path=db_path, out_dir=tmp_path, start=None, end=None, paper_ops_root=tmp_path
            )
        )
        == 0
    )
    assert (
        cli._run_attribute_returns(
            SimpleNamespace(db_path=db_path, out_dir=tmp_path, persist=False, notify="")
        )
        == 0
    )
    assert (
        cli._run_alpha_alert_replay(SimpleNamespace(db_path=db_path, out=tmp_path / "replay.json"))
        == 0
    )
    assert calls["attribute-returns"]["persist"] is False
