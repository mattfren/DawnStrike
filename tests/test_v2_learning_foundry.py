from __future__ import annotations

import ast
import csv
import json
from datetime import date
from pathlib import Path
from typing import cast

import pytest

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.learning_foundry import (
    daily_learn,
    generate_candidates,
)
from intraday_scanner.v2.omega_sentinel import core as sentinel_core
from intraday_scanner.v2.strategies import build_strategy_catalog

RUN_DATE = date(2026, 6, 29)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_foundry_fixture(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    canonical_path = Path("data/v2_autodata/normalized/canonical/2026-06-29_canonical_intraday.csv")
    canonical_path.parent.mkdir(parents=True, exist_ok=True)
    with canonical_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=("symbol", "timestamp", "open", "high", "low", "close", "volume"),
        )
        writer.writeheader()
        for minute, close in enumerate((713.0, 715.0, 717.5, 718.25, 719.0), start=31):
            writer.writerow(
                {
                    "symbol": "QQQ",
                    "timestamp": f"2026-06-29T12:{minute:02d}:00+00:00",
                    "open": "713.0",
                    "high": str(close + 1.0),
                    "low": "712.5",
                    "close": str(close),
                    "volume": "10000",
                }
            )
    _write_json(
        Path("data/v2_autodata/reports/provider_reconciliation_latest.json"),
        {
            "canonical_duplicate_timestamp_count": 0,
            "canonical_provider_id": "alpaca_market_data",
            "canonical_selection": {
                "canonical_artifact_path": canonical_path.as_posix(),
                "canonical_artifact_sha256": "fixture-hash",
                "canonical_duplicate_timestamp_count": 0,
                "canonical_provider_id": "alpaca_market_data",
                "status": "passed",
            },
            "status": "passed",
        },
    )

    strategy_scores = []
    filltruth_rows = []
    for strategy in build_strategy_catalog():
        if strategy.status != "experimental":
            continue
        watch = strategy.strategy_id in {
            "cross_sectional_relative_strength",
            "volatility_contraction_breakout",
        }
        strategy_scores.append(
            {
                "committed_filltruth_forward_count": 1 if watch else 0,
                "evidence_status": "watch" if watch else "quarantined",
                "expectancy": 0.0,
                "forward_closed_trades": 0,
                "forward_days": 1,
                "intraday_supported_forward_fill_count": 1 if watch else 0,
                "max_drawdown_pct": 0.0,
                "overall_score": 31 if watch else 11,
                "profit_factor": 0.0,
                "strategy_id": strategy.strategy_id,
                "strategy_version": strategy.version,
            }
        )
        filltruth_rows.append(
            {
                "evidence_status": "watch" if watch else "quarantined",
                "execution_model_stability_score": 100,
                "fill_certainty_score": 100,
                "fill_reconciliation_score": 100,
                "forward_closed_trades": 0,
                "forward_days": 3,
                "shadow_forward_replay_days": 35,
                "strategy_id": strategy.strategy_id,
                "strategy_version": strategy.version,
                "validation_eligible": False,
            }
        )
    _write_json(
        Path("data/v2_paper_ops/reports/strategy_evidence_scores.json"),
        {"schema_version": "fixture", "scores": strategy_scores, "status": "passed"},
    )
    _write_json(
        Path("data/v2_fill_truth/reports/filltruth_strategy_evidence.json"),
        {"rows": filltruth_rows, "status": "passed"},
    )
    _write_json(
        Path("data/v2_evidence_commit/reports/latest_commit_events.json"),
        {
            "commit_events": [
                {
                    "event_type": "filltruth_commit",
                    "paper_order_id": (
                        "order:forward:2026-06-29:cross_sectional_relative_strength:"
                        "v1.0:QQQ:2026-06-26T13:30:00+00:00:long"
                    ),
                }
            ],
            "status": "passed",
        },
    )
    rows = [
        "date,mode,strategy_id,strategy_version,strategy_status,data_snapshot_id,realized_pnl,daily_return_pct,trades_closed,average_r,expectancy_r",
        "2026-06-29,forward,cross_sectional_relative_strength,v1.0,experimental,ledger_rebuild,0,0,0,0,0",
        "2026-06-29,demo,cross_sectional_relative_strength,v1.0,experimental,demo,10,1,1,1,1",
        "2026-06-29,replay,cross_sectional_relative_strength,v1.0,experimental,replay,10,1,1,1,1",
    ]
    _write_text(
        Path("data/v2_paper_ops/calendar/strategy_daily_returns.csv"),
        "\n".join(rows) + "\n",
    )
    _write_json(
        Path("data/v2_alpha_lab/reports/strategy_comparison.json"),
        [
            {"strategy_id": strategy.strategy_id, "total_return_pct": 0.0}
            for strategy in build_strategy_catalog()
            if strategy.status == "experimental"
        ],
    )
    _write_json(
        Path("data/v2_release_candidate/reports/current_state.json"),
        {
            "canonical_dataset_hash": "fixture-hash",
            "canonical_provider_id": "alpaca_market_data",
            "riskhub_kill_switch": True,
            "riskhub_status": "blocked",
            "strategy_validation_eligible_count": 0,
        },
    )


def test_learning_foundry_daily_learn_writes_shadow_only_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundry_fixture(tmp_path, monkeypatch)

    result = daily_learn(run_date=RUN_DATE)

    assert result["status"] == "passed"
    assert result["quality_score"] == 100
    features = json.loads(
        Path("data/v2_learning_foundry/features/2026-06-29_features.json").read_text()
    )
    labels = json.loads(
        Path("data/v2_learning_foundry/labels/2026-06-29_labels.json").read_text()
    )
    registry = json.loads(
        Path("data/v2_learning_foundry/candidates/challenger_registry.json").read_text()
    )
    promotion = json.loads(
        Path("data/v2_learning_foundry/reports/promotion_review.json").read_text()
    )

    feature_rows = cast(list[dict[str, object]], features["features"])
    label_rows = cast(list[dict[str, object]], labels["labels"])
    candidates = cast(list[dict[str, object]], registry["candidates"])

    assert feature_rows
    assert all(row["as_of_timestamp"] for row in feature_rows)
    assert all(row["timestamp"] <= row["as_of_timestamp"] for row in feature_rows)
    assert labels["excluded_demo_or_replay_rows"] == 2
    assert all(row["contamination_status"] == "clean_true_forward" for row in label_rows)
    assert candidates
    assert all(row["status"] == "shadow" for row in candidates)
    assert all(row["cannot_replace_parent"] is True for row in candidates)
    assert promotion["status"] == "blocked"
    assert not any(row["validation_eligible"] for row in promotion["reviews"])


def test_learning_foundry_command_center_pages_pass_static_qa(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundry_fixture(tmp_path, monkeypatch)
    daily_learn(run_date=RUN_DATE)

    result = build_command_center()
    qa = json.loads(Path("data/v2_command_center/command_center_qa.json").read_text())

    assert result.status == "passed"
    assert qa["status"] == "passed"
    for page in (
        "learning_foundry.html",
        "market_regimes.html",
        "feature_store.html",
        "news_events.html",
        "challenger_strategies.html",
        "model_lab.html",
        "daily_lessons.html",
        "promotion_review.html",
    ):
        text = Path("data/v2_command_center", page).read_text(encoding="utf-8").lower()
        assert "research-only; no live execution." in text
        assert "<script" not in text


def test_sentinel_learn_flag_runs_foundry_without_legacy_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundry_fixture(tmp_path, monkeypatch)
    import intraday_scanner.v2.evidence_commit as commitbridge
    import intraday_scanner.v2.fill_truth as filltruth

    monkeypatch.setattr(filltruth, "after_close", lambda *, run_date: {"status": "passed"})
    monkeypatch.setattr(commitbridge, "propose", lambda *, run_date: {"status": "passed"})
    monkeypatch.setattr(commitbridge, "review", lambda *, run_date: {"status": "passed"})
    monkeypatch.setattr(commitbridge, "reconcile", lambda *, run_date: {"status": "passed"})
    monkeypatch.setattr(commitbridge, "report", lambda: {"status": "passed"})
    monkeypatch.setattr(
        sentinel_core,
        "_real_intraday_cycle",
        lambda run_date, *, enabled: {"status": "skipped"},
    )

    result = sentinel_core.after_close(run_date=RUN_DATE, learn=True)

    foundry = cast(dict[str, object], result["learning_foundry"])
    assert foundry["status"] == "passed"
    assert Path("data/v2_learning_foundry/reports/promotion_review.json").exists()
    assert not Path("data/shadow_real.sqlite").exists()


def test_learning_foundry_core_safety_boundaries() -> None:
    forbidden_import_roots = {"app", "sqlite3", "streamlit"}
    forbidden_import_prefixes = {"intraday_scanner.integrations", "intraday_scanner.storage"}
    forbidden_calls = {"connect", "execute", "executemany", "submit" + "_order"}

    for path in Path("intraday_scanner/v2/learning_foundry").glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_import_roots, path
                    assert not any(
                        alias.name.startswith(prefix) for prefix in forbidden_import_prefixes
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_import_roots, path
                assert not any(
                    node.module.startswith(prefix) for prefix in forbidden_import_prefixes
                )
            elif isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in forbidden_calls, path
                elif isinstance(func, ast.Name):
                    assert func.id not in forbidden_calls, path


def test_candidate_generation_is_deterministic(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_foundry_fixture(tmp_path, monkeypatch)
    first = generate_candidates(run_date=RUN_DATE)
    first_payload = Path(cast(str, first["candidates"])).read_text(encoding="utf-8")
    second = generate_candidates(run_date=RUN_DATE)
    second_payload = Path(cast(str, second["candidates"])).read_text(encoding="utf-8")

    assert first_payload == second_payload
