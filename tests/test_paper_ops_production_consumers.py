from __future__ import annotations

import csv
import json
from pathlib import Path

from intraday_scanner.v2.command_center_x2.adapters import build_story_bundle
from intraday_scanner.v2.command_center_x3.core import build_command_center_x3
from intraday_scanner.v2.interface_apex.adapters import build_apex_model_from_artifacts


def test_operator_adapters_default_to_live_root_and_exclude_replay(tmp_path: Path) -> None:
    legacy_root = tmp_path / "data/v2_paper_ops"
    live_root = tmp_path / "data/v2_paper_ops_live"
    _write_json(legacy_root / "state/open_positions.json", [{"symbol": "STALE"}])
    _write_json(
        live_root / "state/open_positions.json",
        [{"symbol": "LIVE1"}, {"symbol": "LIVE2"}],
    )
    _write_json(live_root / "state/pending_orders.json", [])
    _write_json(live_root / "state/closed_trades.json", [])
    _write_calendar(
        live_root,
        [
            {
                "date": "2026-07-14",
                "mode": "replay",
                "strategy_id": "replay_only",
                "daily_return_pct": "0.25",
                "cumulative_return_pct": "0.25",
                "trades_opened": "1",
                "trades_closed": "0",
            },
            {
                "date": "2026-07-15",
                "mode": "forward",
                "strategy_id": "forward_live",
                "daily_return_pct": "0.01",
                "cumulative_return_pct": "0.01",
                "trades_opened": "1",
                "trades_closed": "0",
            },
        ],
    )

    story = build_story_bundle(repo_root=tmp_path)
    open_metric = next(
        metric for metric in story.app.top_metrics if metric.label == "Open paper positions"
    )
    assert open_metric.value == "2"
    assert {strategy.strategy_id for strategy in story.strategies} == {"forward_live"}
    assert story.strategies[0].forward_days == 1
    assert {row["mode"] for row in story.strategies[0].daily_series} == {"forward"}
    source_paths = {ref.path for ref in story.app.source_refs}
    assert "data/v2_paper_ops_live/state/open_positions.json" in source_paths
    assert not any("data/v2_paper_ops/" in path for path in source_paths)

    apex = build_apex_model_from_artifacts(repo_root=tmp_path)
    assert apex.mission.open_paper_trades == 2
    assert any(
        ref["path"] == "data/v2_paper_ops_live/ledger/paper_ledger.jsonl"
        for ref in apex.source_refs
    )

    x3_output = tmp_path / "x3"
    manifest = build_command_center_x3(repo_root=tmp_path, output_root=x3_output)
    assert manifest["paper_ops_root"] == "data/v2_paper_ops_live"
    rendered_strategies = json.loads(
        (x3_output / "data/strategies.json").read_text(encoding="utf-8")
    )
    assert {row["strategy_id"] for row in rendered_strategies} == {"forward_live"}


def test_operator_adapters_keep_explicit_custom_root_injectable(tmp_path: Path) -> None:
    custom_root = tmp_path / "custom-paper"
    _write_json(custom_root / "state/open_positions.json", [{"symbol": "CUSTOM"}])
    _write_json(custom_root / "state/pending_orders.json", [])
    _write_json(custom_root / "state/closed_trades.json", [])
    _write_calendar(
        custom_root,
        [
            {
                "date": "2026-07-15",
                "mode": "forward",
                "strategy_id": "custom_forward",
                "daily_return_pct": "",
                "cumulative_return_pct": "",
                "trades_opened": "0",
                "trades_closed": "0",
            }
        ],
    )

    story = build_story_bundle(repo_root=tmp_path, paper_ops_root=custom_root)
    open_metric = next(
        metric for metric in story.app.top_metrics if metric.label == "Open paper positions"
    )
    assert open_metric.value == "1"
    assert {strategy.strategy_id for strategy in story.strategies} == {"custom_forward"}

    apex = build_apex_model_from_artifacts(
        repo_root=tmp_path,
        paper_ops_root=custom_root,
    )
    assert apex.mission.open_paper_trades == 1


def _write_calendar(root: Path, rows: list[dict[str, str]]) -> None:
    path = root / "calendar/strategy_daily_returns.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
