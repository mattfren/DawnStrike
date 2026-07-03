from __future__ import annotations

import ast
import json
from datetime import date
from pathlib import Path

from intraday_scanner.v2.command_center import build_command_center
from intraday_scanner.v2.fill_truth import (
    compare_models,
    evaluate,
    import_intraday,
    init,
    resolve_pending,
    verify,
)
from intraday_scanner.v2.fill_truth import core as fill_truth_core


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _seed_daily(
    *,
    days: tuple[str, ...] = ("2026-01-02", "2026-01-03"),
    high: float = 11.0,
    low: float = 9.8,
) -> None:
    _write_json(
        Path("data/v2_data_truth/manifests/latest.json"),
        {
            "accepted_end": days[-1],
            "provider_id": "fixture_daily",
            "schema_version": "fixture",
            "snapshot_id": f"daily_fixture_{days[-1].replace('-', '')}",
            "timeframe": "1d",
            "warnings": [],
        },
    )
    rows = ["symbol,timestamp,open,high,low,close,volume"]
    for day in days:
        rows.append(f"TST,{day}T21:00:00+00:00,10,{high},{low},10.5,1000")
    _write_text(Path("data/v2_data_truth/normalized/latest_ohlcv.csv"), "\n".join(rows) + "\n")


def _seed_pending(signal_time: str = "2026-01-02T14:30:00+00:00") -> None:
    _write_json(
        Path("data/v2_paper_ops/state/pending_orders.json"),
        [
            {
                "direction": "long",
                "earliest_fill_date": "2026-01-03",
                "entry": 10.0,
                "expected_fill_rule": "daily signal fills no earlier than next valid bar open",
                "order_id": "order:forward:TST",
                "quantity": 10,
                "risk_per_unit": 1.0,
                "signal_time": signal_time,
                "stop": 9.5,
                "strategy_id": "fixture_strategy",
                "symbol": "TST",
                "target": 10.8,
                "warnings": [],
            }
        ],
    )


def test_intraday_import_normalizes_variants_and_detects_bad_rows(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "TST_intraday.csv"
    source.write_text(
        "\n".join(
            [
                "Date_Time,Open,High,Low,Close,Volume",
                "2026-01-03T14:30:00,10,10.5,9.9,10.2,100",
                "2026-01-03T14:30:00,10,10.5,9.9,10.2,100",
                "2026-01-03T14:35:00,10,9,8,8.5,100",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = import_intraday(path=source, output_root=Path("data/v2_fill_truth"))

    assert result["status"] == "failed"
    assert result["bar_count"] == 1
    assert result["symbols"] == ["TST"]
    assert any("naive timestamp assumed UTC" in warning for warning in result["warnings"])
    assert result["raw_artifact_hashes"] == import_intraday(
        path=source,
        output_root=Path("data/v2_fill_truth"),
    )["raw_artifact_hashes"]


def test_same_day_daily_bar_does_not_fill_close_generated_signal(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily(days=("2026-01-02",))
    _seed_pending(signal_time="2026-01-02T14:30:00+00:00")

    result = resolve_pending(run_date=date(2026, 1, 2), output_root=Path("data/v2_fill_truth"))
    decision = result["decisions"][0]

    assert result["fills_resolved"] == 0
    assert decision["resolution_status"] == "pending"
    assert decision["fill_certainty"] == "rejected_policy"
    assert decision["daily_same_day_fill_blocked"] is True


def test_next_daily_open_fill_and_same_bar_stop_first_outcome(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily(days=("2026-01-02", "2026-01-03"), high=11.0, low=9.0)
    _seed_pending(signal_time="2026-01-02T21:00:00+00:00")

    resolution = resolve_pending(
        run_date=date(2026, 1, 3),
        output_root=Path("data/v2_fill_truth"),
    )
    outcome = evaluate(run_date=date(2026, 1, 3), output_root=Path("data/v2_fill_truth"))

    assert resolution["fills_resolved"] == 1
    assert resolution["decisions"][0]["execution_model"] == "daily_next_open"
    assert outcome["outcomes"][0]["outcome_status"] == "closed"
    assert outcome["outcomes"][0]["close_reason"] == "ambiguous_same_bar_stop_first"
    assert outcome["outcomes"][0]["outcome_certainty"] == "ambiguous_same_bar"


def test_model_comparison_and_command_center_pages_are_generated(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily(days=("2026-01-02", "2026-01-03"))
    _seed_pending(signal_time="2026-01-02T21:00:00+00:00")
    resolve_pending(run_date=date(2026, 1, 3), output_root=Path("data/v2_fill_truth"))
    evaluate(run_date=date(2026, 1, 3), output_root=Path("data/v2_fill_truth"))
    comparison = compare_models(
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
        output_root=Path("data/v2_fill_truth"),
    )
    fill_truth_core.report(output_root=Path("data/v2_fill_truth"))
    center = build_command_center()

    assert comparison["rows"]
    assert center.status == "passed"
    for page in (
        "fill_truth.html",
        "pending_orders.html",
        "execution_models.html",
        "fill_certainty.html",
    ):
        text = Path("data/v2_command_center", page).read_text(encoding="utf-8")
        assert "research-only; no live execution." in text.lower()
        assert "<script" not in text.lower()


def test_filltruth_verify_and_safety_scan_pass_after_artifacts(
    tmp_path: Path,
    monkeypatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    _seed_daily(days=("2026-01-02", "2026-01-03"))
    _seed_pending(signal_time="2026-01-02T21:00:00+00:00")
    init(output_root=Path("data/v2_fill_truth"))
    resolve_pending(run_date=date(2026, 1, 3), output_root=Path("data/v2_fill_truth"))
    evaluate(run_date=date(2026, 1, 3), output_root=Path("data/v2_fill_truth"))
    compare_models(
        start=date(2026, 1, 1),
        end=date(2026, 1, 3),
        output_root=Path("data/v2_fill_truth"),
    )
    fill_truth_core.report(output_root=Path("data/v2_fill_truth"))
    build_command_center()

    result = verify(output_root=Path("data/v2_fill_truth"))

    assert result["status"] == "passed"
    assert result["failures"] == []


def test_filltruth_modules_avoid_live_execution_and_database_imports() -> None:
    forbidden_import_roots = {
        "app",
        "httpx",
        "requests",
        "socket",
        "sqlite3",
        "streamlit",
        "urllib",
    }
    forbidden_import_prefixes = {"intraday_scanner.integrations", "intraday_scanner.storage"}
    forbidden_calls = {"connect", "execute", "executemany", "submit" + "_order"}

    for path in Path("intraday_scanner/v2/fill_truth").rglob("*.py"):
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
