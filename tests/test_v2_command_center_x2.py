from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from intraday_scanner.paper_ops_root import PAPER_OPS_ROOT_ENV
from intraday_scanner.v2.command_center_x2 import adapters
from intraday_scanner.v2.command_center_x2.adapters import build_story_bundle
from intraday_scanner.v2.command_center_x2.core import (
    build_command_center_x2,
    build_models_command_center_x2,
    inventory_command_center_x2,
    qa_command_center_x2,
    report_command_center_x2,
    verify_command_center_x2,
)
from intraday_scanner.v2.command_center_x2.qa import run_command_center_x2_qa

REPO_ROOT = Path(".")


def test_story_bundle_handles_sparse_artifacts_without_fabrication(tmp_path: Path) -> None:
    bundle = build_story_bundle(repo_root=tmp_path)

    assert bundle.app.latest_run_date
    assert bundle.app.trust_boundaries[0].label == "Research-only / paper-only"
    assert bundle.no_picks.accepted_count == 0
    assert bundle.no_picks.top_reasons
    assert bundle.automation.autonomous_runner_status == "missing"
    assert all(model.cumulative_return_pct == "n/a" for model in bundle.strategies)


def test_story_bundle_shows_registered_strategy_before_first_eligible_row(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        adapters,
        "load_paper_ops_calendar",
        lambda _root: {
            "official_series": [
                {
                    "strategy_id": "gap_up_continuation_atr",
                    "strategy_label": "Gap-Up Continuation (ATR)",
                    "strategy_version": "v1.0",
                    "execution_policy_version": "paper-policy-v2",
                    "strategy_semantics_fingerprint": "a" * 64,
                    "registry_inception_date": "2026-07-17",
                }
            ]
        },
    )

    bundle = build_story_bundle(repo_root=tmp_path)

    model = next(
        item
        for item in bundle.strategies
        if item.strategy_id == "gap_up_continuation_atr"
    )
    assert model.status == "registered / not yet eligible"
    assert model.daily_return_pct == "n/a"
    assert model.forward_days == 0
    assert "2026-07-17" in model.latest_signal_state


def test_story_bundle_uses_only_exact_registered_champions_for_fleet_returns(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    champion_fingerprint = "a" * 64
    second_fingerprint = "b" * 64
    shadow_fingerprint = "c" * 64
    future_fingerprint = "d" * 64
    policy = "paperops_daily_next_open_risk_v2"
    rows = [
        {
            "date": "2026-07-16",
            "mode": "forward",
            "strategy_id": "ts_momentum_sma_atr",
            "strategy_version": "v1.0",
            "execution_policy_version": policy,
            "strategy_semantics_fingerprint": champion_fingerprint,
            "strategy_status": "experimental",
            "series_role": "official",
            "series_key": (
                f"forward|ts_momentum_sma_atr|v1.0|{policy}|"
                f"{champion_fingerprint}"
            ),
            "registration_status": "registered",
            "registry_inception_date": "2026-07-15",
            "daily_return_pct": 0.0002,
            "cumulative_return_pct": 0.0002,
            "drawdown_pct": 0.0,
            "starting_equity": 100_000.0,
            "ending_equity": 100_020.0,
            "total_pnl": 20.0,
            "trades_opened": 1,
            "trades_closed": 0,
            "pending_orders": 0,
            "open_positions": 1,
            "wins": 0,
            "losses": 0,
            "data_snapshot_id": "retained-provider-snapshot",
        },
        {
            "date": "2026-07-16",
            "mode": "forward",
            "strategy_id": "donchian_breakout_20_10",
            "strategy_version": "v1.0",
            "execution_policy_version": policy,
            "strategy_semantics_fingerprint": second_fingerprint,
            "strategy_status": "experimental",
            "series_role": "official",
            "series_key": (
                f"forward|donchian_breakout_20_10|v1.0|{policy}|"
                f"{second_fingerprint}"
            ),
            "registration_status": "registered",
            "registry_inception_date": "2026-07-15",
            "daily_return_pct": -0.0004,
            "cumulative_return_pct": -0.0004,
            "drawdown_pct": -0.0004,
            "starting_equity": 100_000.0,
            "ending_equity": 99_960.0,
            "total_pnl": -40.0,
            "trades_opened": 1,
            "trades_closed": 0,
            "pending_orders": 0,
            "open_positions": 1,
            "wins": 0,
            "losses": 0,
            "data_snapshot_id": "retained-provider-snapshot",
        },
        {
            "date": "2026-07-16",
            "mode": "forward",
            "strategy_id": "ts_momentum_sma_atr",
            "strategy_version": "v2.0-shadow",
            "execution_policy_version": policy,
            "strategy_semantics_fingerprint": shadow_fingerprint,
            "strategy_status": "shadow",
            "series_role": "challenger",
            "challenger_id": "ts_momentum_sma_atr_shadow_v2",
            "series_key": (
                f"forward|ts_momentum_sma_atr|v2.0-shadow|{policy}|"
                f"{shadow_fingerprint}"
            ),
            "daily_return_pct": 0.05,
            "cumulative_return_pct": 0.05,
            "drawdown_pct": 0.0,
            "trades_opened": 1,
            "trades_closed": 0,
            "pending_orders": 1,
            "open_positions": 0,
            "wins": 0,
            "losses": 0,
            "data_snapshot_id": "same-retained-provider-snapshot",
        },
        {
            "date": "2026-07-16",
            "mode": "forward",
            "strategy_id": "benchmark_buy_hold_equal_weight",
            "strategy_version": "v1.0",
            "execution_policy_version": "benchmark-policy-v1",
            "strategy_semantics_fingerprint": "e" * 64,
            "strategy_status": "benchmark",
            "series_role": "benchmark",
            "series_key": "forward|benchmark|reference",
            "daily_return_pct": -0.02,
            "cumulative_return_pct": -0.02,
        },
        {
            "date": "2026-07-16",
            "mode": "forward",
            "strategy_id": "cash_no_trade_baseline",
            "strategy_version": "v1.0",
            "execution_policy_version": "cash-policy-v1",
            "strategy_semantics_fingerprint": "f" * 64,
            "strategy_status": "baseline",
            "series_role": "cash",
            "series_key": "forward|cash|reference",
            "daily_return_pct": 0.0,
            "cumulative_return_pct": 0.0,
        },
        {
            "date": "2026-07-16",
            "mode": "forward",
            "strategy_id": "gap_up_continuation",
            "strategy_version": "v1.0",
            "execution_policy_version": policy,
            "strategy_semantics_fingerprint": future_fingerprint,
            "strategy_status": "experimental",
            "series_role": "official",
            "series_key": (
                f"forward|gap_up_continuation|v1.0|{policy}|"
                f"{future_fingerprint}"
            ),
            "registration_status": "not_yet_registered",
            "registry_inception_date": "2026-07-17",
            "daily_return_pct": 0.0,
            "cumulative_return_pct": 0.0,
        },
    ]
    official_series = [
        {
            "strategy_id": strategy_id,
            "strategy_label": strategy_id.replace("_", " ").title(),
            "strategy_version": "v1.0",
            "execution_policy_version": policy,
            "strategy_semantics_fingerprint": fingerprint,
            "registry_inception_date": inception,
        }
        for strategy_id, fingerprint, inception in (
            ("ts_momentum_sma_atr", champion_fingerprint, "2026-07-15"),
            ("donchian_breakout_20_10", second_fingerprint, "2026-07-15"),
            ("gap_up_continuation", future_fingerprint, "2026-07-17"),
        )
    ]
    monkeypatch.setattr(
        adapters,
        "load_paper_ops_calendar",
        lambda _root: {
            "status": "verified",
            "rows": rows,
            "official_series": official_series,
        },
    )
    monkeypatch.setattr(
        adapters,
        "build_paper_ops_calendar_view",
        lambda dataset, mode: {
            "status": "verified",
            "mode": mode,
            "official_rows": [
                row for row in dataset["rows"] if row["series_role"] == "official"
            ],
            "challenger_rows": [
                row for row in dataset["rows"] if row["series_role"] == "challenger"
            ],
        },
    )

    bundle = build_story_bundle(repo_root=tmp_path)
    day = next(item for item in bundle.days if item.date == "2026-07-16")
    official = [
        item for item in bundle.strategies if item.role == "official_champion"
    ]
    shadow = [item for item in bundle.strategies if item.role == "paperops_shadow"]

    assert day.cumulative_returns == {
        "daily_return_pct": "-0.010000%",
        "cumulative_return_pct": "-0.010000%",
    }
    assert day.picks_summary["strategy_rows"] == 2
    assert {item.strategy_id for item in day.strategy_returns} == {
        "ts_momentum_sma_atr",
        "donchian_breakout_20_10",
    }
    assert {item.strategy_id for item in official} == {
        "ts_momentum_sma_atr",
        "donchian_breakout_20_10",
        "gap_up_continuation",
    }
    assert next(
        item for item in official if item.strategy_id == "ts_momentum_sma_atr"
    ).daily_return_pct == "0.020000%"
    future = next(
        item for item in official if item.strategy_id == "gap_up_continuation"
    )
    assert future.daily_return_pct == "n/a"
    assert future.daily_series == []
    assert len(shadow) == 1
    assert shadow[0].daily_return_pct == "5.000000%"
    assert "excluded from official" in shadow[0].evidence_quality
    assert not any(
        item.strategy_id
        in {"benchmark_buy_hold_equal_weight", "cash_no_trade_baseline"}
        for item in bundle.strategies
    )
    strategy_metric = next(
        item for item in bundle.app.top_metrics if item.label == "Strategies"
    )
    assert strategy_metric.value == "3"


def test_story_bundle_fails_closed_when_canonical_calendar_truth_is_blocked(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        adapters,
        "load_paper_ops_calendar",
        lambda _root: {
            "status": "blocked",
            "warnings": ["Calendar truth gate is stale."],
            "rows": [
                {
                    "date": "2026-07-16",
                    "mode": "forward",
                    "strategy_id": "ts_momentum_sma_atr",
                    "series_role": "official",
                    "registration_status": "registered",
                    "registry_inception_date": "2026-07-15",
                    "daily_return_pct": 0.25,
                }
            ],
            "official_series": [],
        },
    )

    bundle = build_story_bundle(repo_root=tmp_path)

    assert bundle.strategies == []
    assert all(
        day.cumulative_returns["daily_return_pct"] == "n/a"
        for day in bundle.days
    )


def test_build_generates_story_pages_assets_bridges_and_docs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        PAPER_OPS_ROOT_ENV,
        str(REPO_ROOT / "data/test_empty_paper_ops"),
    )
    output_root = tmp_path / "command_center_x2"

    inventory = inventory_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)
    models = build_models_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)
    build = build_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)
    qa = qa_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)
    report = report_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)
    verify = verify_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)

    assert inventory["status"] == "passed"
    assert models["day_count"] > 0
    assert build["status"] == "passed"
    assert build["day_count"] > 0
    assert build["month_count"] > 0
    assert build["strategy_count"] == 0
    assert qa["status"] == "passed"
    assert qa["checks"]["strategy_surface_truthful"] is True
    assert qa["checks"]["hero_present_all_pages"] is True
    assert qa["checks"]["warning_drawers_collapsed"] is True
    assert qa["checks"]["dense_card_walls_clear"] is True
    assert qa["checks"]["scan_pages_use_tables"] is True
    assert qa["checks"]["calendar_defaults_to_evidence_month"] is True
    assert report["final_status"] == "COMPLETE_COMMAND_CENTER_X2"
    assert report["quality_score"] == 100
    assert verify["status"] == "passed"
    assert (output_root / "assets/x2_design_tokens.json").exists()
    assert (output_root / "assets/x2.css").exists()
    assert (output_root / "assets/x2_components.css").exists()
    assert (output_root / "assets/x2_interactions.js").exists()
    assert (output_root / "assets/favicon.svg").exists()
    assert 'rel="icon"' in (output_root / "index.html").read_text(encoding="utf-8")
    assert (output_root / "pages/calendar.html").exists()
    assert (output_root / "pages/no_picks.html").exists()
    assert (output_root / "pages/day_trade_lab.html").exists()
    assert (output_root / "pages/day_trade_calendar.html").exists()
    assert (output_root / "pages/day_trade_strategies.html").exists()
    assert (output_root / "pages/day_trade_trades.html").exists()
    assert (output_root / "pages/day_trade_no_trade_days.html").exists()
    assert (output_root / "pages/day_trade_assumptions.html").exists()
    assert (output_root / "pages/day_trade_robustness.html").exists()
    assert (output_root / "pages/day_trade_slippage_stress.html").exists()
    assert (output_root / "pages/day_trade_oos.html").exists()
    assert (output_root / "pages/day_trade_refinements.html").exists()
    backtest_page = (output_root / "pages/six_month_backtest.html").read_text(encoding="utf-8")
    day_trade_page = (output_root / "pages/day_trade_lab.html").read_text(encoding="utf-8")
    day_strategies_page = (output_root / "pages/day_trade_strategies.html").read_text(
        encoding="utf-8"
    )
    robustness_page = (output_root / "pages/day_trade_robustness.html").read_text(
        encoding="utf-8"
    )
    slippage_page = (output_root / "pages/day_trade_slippage_stress.html").read_text(
        encoding="utf-8"
    )
    oos_page = (output_root / "pages/day_trade_oos.html").read_text(encoding="utf-8")
    refinements_page = (output_root / "pages/day_trade_refinements.html").read_text(
        encoding="utf-8"
    )
    today_page = (output_root / "pages/today.html").read_text(encoding="utf-8")
    strategies_page = (output_root / "pages/strategies.html").read_text(encoding="utf-8")
    assumptions_page = (output_root / "pages/day_trade_assumptions.html").read_text(
        encoding="utf-8"
    )
    interactions_js = (output_root / "assets/x2_interactions.js").read_text(encoding="utf-8")
    assert 'data-strategy-empty-state="true"' in strategies_page
    assert "Returns are N/A" in strategies_page
    assert "Day Trade Lab" in day_trade_page
    assert "Intraday-only strategy research" in day_trade_page
    assert "Corpus sessions" in day_trade_page
    assert "Overnight holds" in day_trade_page
    assert "Provider status" in day_trade_page
    assert "same-session" in day_trade_page
    assert "Daily Swing Research" in day_trade_page
    assert "Ranked Day Trade Strategies" in day_strategies_page
    assert "No Day Trade Lab strategy comparison found" in day_strategies_page
    assert "Robustness score" in day_strategies_page
    assert "Day Trade Robustness" in robustness_page
    assert "Provider/Data Limitations" in robustness_page
    assert "historical day-trade backtest only" in robustness_page.lower()
    assert "not validated" in robustness_page.lower()
    assert "Zero overnight holds" in robustness_page
    assert "Day Trade Slippage Stress" in slippage_page
    assert "Cost-adjusted replay" in slippage_page
    assert "Day Trade Out-of-Sample" in oos_page
    assert "Research vs holdout" in oos_page
    assert "Day Trade Refined Challengers" in refinements_page
    assert "Shadow Refinement Candidates" in refinements_page
    assert "not validated" in refinements_page.lower()
    assert "historical_daytrade_backtest" in assumptions_page
    assert "Day-trade corpus" in today_page
    assert "Research Lane Separation" in strategies_page
    assert "data-x2-toggle" not in day_strategies_page
    assert "data-x2-toggle" in backtest_page
    assert "trade-menu" in backtest_page
    assert 'role="dialog"' in backtest_page
    assert "data-x2-close" in backtest_page
    assert "Trade Ledger" in backtest_page
    assert "Daily-bar strategy backtest, not day-trading proof." in backtest_page
    assert "Horizon" in backtest_page
    assert "Hold" in backtest_page
    assert "daily bars" in backtest_page
    assert "Holds above 1 daily bar are not day trades." in backtest_page
    assert "x2_interactions.js?v=" in backtest_page
    assert "strategy-disclosure" in backtest_page
    assert "row-expander" not in backtest_page
    assert "aria-expanded" in interactions_js
    assert list((output_root / "days").glob("*.html"))
    assert list((output_root / "months").glob("*.html"))
    assert not list((output_root / "strategies").glob("*.html"))
    assert (REPO_ROOT / "data/v2_command_center/command_center_x2.html").exists()
    assert (REPO_ROOT / "data/v2_command_center_x/command_center_x2.html").exists()


def test_production_launcher_promotes_canonical_operator_dashboard_without_live_controls() -> None:
    script = Path("scripts/open_command_center_production.ps1").read_text(encoding="utf-8")

    assert "streamlit" in script
    assert "run" in script
    assert "app.py" in script
    assert "Dawnstrike operator dashboard" in script
    assert "Canonical tabs: Today, Review, History, Calendar, Performance, System" in script
    assert "8502" in script
    assert "http.server" not in script
    assert "intraday_scanner.v2.command_center_x2 demo" not in script
    assert "data/v2_command_center_x2" not in script
    forbidden = (
        "submit" + "_order",
        "place" + "_order",
        "create" + "_order",
        "live" + "_execute",
    )
    assert not any(term in script for term in forbidden)


def test_calendar_model_fails_closed_for_legacy_rows_without_exact_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The populated v1 tree predates exact policy/fingerprint identity. It may
    # remain retained evidence, but it cannot power an official X2/X3 return.
    monkeypatch.setenv(PAPER_OPS_ROOT_ENV, str(REPO_ROOT / "data/v2_paper_ops"))
    output_root = tmp_path / "command_center_x2"
    build_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)

    audit = json.loads((output_root / "reports/calendar_audit.json").read_text())
    months = json.loads((output_root / "data/months.json").read_text())

    assert audit["status"] == "passed"
    assert audit["source_row_count"] == 0
    assert audit["source_hash"]
    assert all(
        day["daily_return_pct"] == "n/a"
        for month in months
        for day in month["calendar_days"]
    )
    assert any(
        day["href"].startswith("../days/")
        for month in months
        for day in month["calendar_days"]
    )


def test_x2_qa_rejects_tampered_output(tmp_path: Path) -> None:
    output_root = tmp_path / "command_center_x2"
    build_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)

    target = output_root / "pages/today.html"
    target.write_text(
        target.read_text(encoding="utf-8")
        + '\n<script src="https://cdn.example.invalid/x.js"></script>\n'
        + "TELEGRAM_BOT_TOKEN\n"
        + '\n<span data-trust="validated">Validated</span>\n',
        encoding="utf-8",
        newline="\n",
    )
    strategies = output_root / "pages/strategies.html"
    strategies.write_text(
        strategies.read_text(encoding="utf-8").replace(
            '<details class="panel warnings-panel app-warnings-panel"',
            '<details class="panel warnings-panel app-warnings-panel" open',
        )
        + ("<div class=\"story-card strategy-card\">regression</div>\n" * 9),
        encoding="utf-8",
        newline="\n",
    )
    (output_root / "assets/x2_interactions.js").write_text(
        "fetch('/x'); eval('1')",
        encoding="utf-8",
        newline="\n",
    )

    qa = run_command_center_x2_qa(output_root=output_root, repo_root=REPO_ROOT)

    assert qa["status"] == "failed"
    assert qa["checks"]["external_dependencies_clear"] is False
    assert qa["checks"]["secret_values_clear"] is False
    assert qa["checks"]["invalid_validated_badges_clear"] is False
    assert qa["checks"]["local_js_safe"] is False
    assert qa["checks"]["warning_drawers_collapsed"] is False
    assert qa["checks"]["dense_card_walls_clear"] is False


def test_x2_qa_rejects_empty_strategy_state_without_na_copy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        PAPER_OPS_ROOT_ENV,
        str(REPO_ROOT / "data/test_empty_paper_ops"),
    )
    output_root = tmp_path / "command_center_x2"
    build_command_center_x2(repo_root=REPO_ROOT, output_root=output_root)
    target = output_root / "pages/strategies.html"
    target.write_text(
        target.read_text(encoding="utf-8").replace(
            "Returns are N/A until exact, source-gated forward evidence exists.",
            "Returns unavailable.",
        ),
        encoding="utf-8",
        newline="\n",
    )

    qa = run_command_center_x2_qa(output_root=output_root, repo_root=REPO_ROOT)

    assert qa["status"] == "failed"
    assert qa["checks"]["strategy_surface_truthful"] is False


def test_x2_package_keeps_read_only_local_import_surface() -> None:
    forbidden_roots = {
        "app",
        "sqlite3",
        "streamlit",
        "socket",
        "urllib",
        "requests",
        "httpx",
    }
    forbidden_calls = {"connect", "urlopen", "request"}

    for path in Path("intraday_scanner/v2/command_center_x2").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_roots, path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_roots, path
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path
