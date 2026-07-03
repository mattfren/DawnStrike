from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

import pytest

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.market_masters import (
    demo,
    evaluate,
    generate_challengers,
    generate_primitives,
    source_register,
    sync_learning_foundry,
)

RUN_DATE = date(2026, 6, 29)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _seed_frontier(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(tmp_path)
    champions = {
        "champions": [
            {
                "can_be_replaced_by_learning_foundry": False,
                "champion_status": "immutable_baseline",
                "strategy_id": "ts_momentum_sma_atr",
            },
            {
                "can_be_replaced_by_learning_foundry": False,
                "champion_status": "immutable_baseline",
                "strategy_id": "donchian_breakout_20_10",
            },
            {
                "can_be_replaced_by_learning_foundry": False,
                "champion_status": "immutable_baseline",
                "strategy_id": "cross_sectional_relative_strength",
            },
            {
                "can_be_replaced_by_learning_foundry": False,
                "champion_status": "immutable_baseline",
                "strategy_id": "volatility_contraction_breakout",
            },
        ]
    }
    _write_json(Path("data/v2_learning_foundry/reports/champion_registry.json"), champions)
    _write_json(
        Path("data/v2_forward_evidence/strategy_evidence/strategy_evidence_omega.json"),
        {
            "rows": [
                {
                    "evidence_status": "watch",
                    "strategy_id": "ts_momentum_sma_atr",
                    "validation_eligible": False,
                }
            ],
            "status": "passed",
        },
    )


def test_source_register_requires_credible_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_frontier(tmp_path, monkeypatch)

    result = source_register()
    payload = json.loads(
        Path("data/v2_market_masters/source_register/source_register.json").read_text()
    )

    assert result["status"] == "passed"
    assert payload["source_count"] >= 10
    for row in payload["rows"]:
        assert row["title"]
        assert str(row["url"]).startswith("https://")
        assert row["source_tier"] in {"tier_1", "tier_2", "tier_3"}
        assert row["methodology_category"]
    assert not any(
        "secret strategy revealed" in row["key_claim"].lower() for row in payload["rows"]
    )


def test_methodologies_and_primitives_are_mechanical_shadow_outputs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_frontier(tmp_path, monkeypatch)

    generate_primitives()
    taxonomy = json.loads(
        Path("data/v2_market_masters/methodologies/methodology_taxonomy.json").read_text()
    )
    primitives = json.loads(
        Path("data/v2_market_masters/primitives/strategy_primitives.json").read_text()
    )
    source_ids = {
        row["source_id"]
        for row in json.loads(
            Path("data/v2_market_masters/source_register/source_register.json").read_text()
        )["rows"]
    }

    assert taxonomy["methodology_count"] >= 8
    for row in taxonomy["methodologies"]:
        assert set(row["source_ids"]) <= source_ids
        assert "Medallion" not in row["mechanical_translation"]
    for row in primitives["primitives"]:
        assert row["mechanical_rule"]
        assert row["required_features"]
        assert row["leakage_risks"]
        assert row["not_live_trading"] is True
        assert row["status"] in {"shadow", "parked"}


def test_challengers_do_not_mutate_champions_and_start_shadow_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_frontier(tmp_path, monkeypatch)
    before = Path("data/v2_learning_foundry/reports/champion_registry.json").read_text()

    generate_challengers(run_date=RUN_DATE)
    registry = json.loads(
        Path("data/v2_market_masters/candidates/challenger_registry.json").read_text()
    )

    assert Path("data/v2_learning_foundry/reports/champion_registry.json").read_text() == before
    assert registry["challenger_count"] >= 6
    champion_ids = {row["strategy_id"] for row in json.loads(before)["champions"]}
    for row in registry["challengers"]:
        assert row["challenger_id"] not in champion_ids
        assert row["status"] == "shadow"
        assert row["evidence_mode"] == "shadow"
        assert row["cannot_replace_parent"] is True
        assert row["no_live_trading"] is True


def test_evaluation_blocks_promotion_and_learning_sync_is_separate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_frontier(tmp_path, monkeypatch)
    before = Path("data/v2_learning_foundry/reports/champion_registry.json").read_text()

    evaluate(run_date=RUN_DATE)
    sync = sync_learning_foundry(run_date=RUN_DATE)
    eval_payload = json.loads(Path("data/v2_market_masters/evals/2026-06-29_eval.json").read_text())

    assert eval_payload["automatic_validation"] is False
    assert eval_payload["promotion_result"] == "blocked_no_true_forward_sample"
    assert sync["candidate_count"] >= 6
    assert Path("data/v2_learning_foundry/reports/champion_registry.json").read_text() == before
    assert Path("data/v2_learning_foundry/candidates/market_masters_sync_2026-06-29.json").exists()


def test_demo_generates_command_center_pages_without_live_controls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _seed_frontier(tmp_path, monkeypatch)

    result = demo()
    center = build_command_center()
    qa = json.loads(Path("data/v2_command_center/command_center_qa.json").read_text())

    assert result["status"] == "passed"
    assert center.status.startswith("passed")
    assert qa["status"].startswith("passed")
    for page in (
        "market_masters.html",
        "market_masters_sources.html",
        "methodology_taxonomy.html",
        "strategy_primitives.html",
        "market_masters_challengers.html",
        "market_masters_shadow.html",
        "market_masters_evals.html",
        "market_masters_lessons.html",
    ):
        text = Path("data/v2_command_center", page).read_text(encoding="utf-8")
        assert "<script" not in text.lower()
        assert "Research-only; no live execution." in text


def test_market_masters_core_has_no_ui_database_or_network_imports() -> None:
    forbidden_import_roots = {
        "app",
        "httpx",
        "requests",
        "socket",
        "sqlite3",
        "streamlit",
        "urllib",
    }
    for path in Path("intraday_scanner/v2/market_masters").rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert alias.name.split(".")[0] not in forbidden_import_roots, path
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_import_roots, path
