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
    normalized: list[dict[str, str]] = []
    officials: list[dict[str, str]] = []
    semantics: dict[str, object] = {}
    policy_version = "fixture-paper-policy-v1"
    for source in rows:
        row = dict(source)
        strategy_id = row["strategy_id"]
        strategy_version = row.setdefault("strategy_version", "v1.0")
        fingerprint = row.setdefault(
            "strategy_semantics_fingerprint",
            f"fixture-{strategy_id}-semantics-v1",
        )
        row.setdefault("execution_policy_version", policy_version)
        row.setdefault("strategy_status", "experimental")
        row.setdefault("data_snapshot_id", f"fixture:{row['date']}")
        row.setdefault("starting_equity", "100000")
        row.setdefault("ending_equity", "100000")
        row.setdefault("total_pnl", "0")
        row.setdefault("drawdown_pct", "0")
        row.setdefault("run_id", f"fixture:{row['mode']}:{row['date']}")
        normalized.append(row)
        if row["mode"] != "forward":
            continue
        identity = {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "execution_policy_version": policy_version,
            "strategy_semantics_fingerprint": fingerprint,
        }
        if identity not in officials:
            officials.append(identity)
        semantics[f"{strategy_id}@{strategy_version}"] = {
            "configuration": {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
            },
            "fingerprint": fingerprint,
            "registered_at": "2026-06-30T12:00:00+00:00",
        }

    path = root / "calendar/strategy_daily_returns.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(normalized[0]))
        writer.writeheader()
        writer.writerows(normalized)

    _write_json(root / "state/strategy_registry.json", officials)
    _write_json(
        root / "state/strategy_semantics_manifest.json",
        {
            "schema_version": "v2.strategy_semantics_manifest.v1",
            "strategies": semantics,
        },
    )
    _write_json(
        root / "state/execution_policy_manifest.json",
        {
            "schema_version": "v2.paper_execution_policy_manifest.v1",
            "active_execution_policy_version": policy_version,
            "policies": {
                policy_version: {
                    "configuration": {"fixture": True},
                    "fingerprint": "fixture-paper-policy-fingerprint-v1",
                    "registered_at": "2026-06-30T12:00:00+00:00",
                }
            },
        },
    )
    _write_json(
        root / "state/strategy_challenger_registry.json",
        {
            "schema_version": "v2.paper_ops_challenger_registry.v1",
            "challengers": [],
        },
    )
    reconciliation = root / "reconciliation"
    for name, schema in (
        ("reconciliation_latest.json", "v2.paper_ops_reconciliation.v1"),
        ("calendar_truth_latest.json", "v2.paper_ops_calendar_truth.v2"),
        ("ledger_rebuild_latest.json", "v2.paper_ops_ledger_rebuild.v1"),
    ):
        _write_json(
            reconciliation / name,
            {"schema_version": schema, "status": "passed"},
        )
    for mode in {row["mode"] for row in normalized}:
        _write_json(
            reconciliation / f"source_bar_truth_{mode}_latest.json",
            {
                "schema_version": "v2.paper_ops_source_bar_truth.v1",
                "status": "passed",
                "mode": mode,
            },
        )


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")
