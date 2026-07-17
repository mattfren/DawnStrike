from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.providers.daily_movers_base import write_daily_mover_csv
from intraday_scanner.providers.local_daily_movers_provider import (
    LocalDailyMoversProvider,
)
from intraday_scanner.v2.mover_pattern_lab.candidate_runtime import (
    _same_artifact_path,
    run_candidate_study,
)
from intraday_scanner.v2.mover_pattern_lab.core import (
    MoverLabPaths,
    _candidate_manifest_paths_confined,
    _split_registry_matches_source_file,
    analyze,
    build_snapshots_from_bars,
    paper_scan,
    reconcile_paper_signals,
    verify,
)

ET = ZoneInfo("America/New_York")
BAR_FIELDS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _session_bars(market_date: str, *, current: bool) -> list[dict[str, Any]]:
    timestamp = datetime.fromisoformat(f"{market_date}T09:35:00-04:00")
    close_at = datetime.fromisoformat(f"{market_date}T16:00:00-04:00")
    rows: list[dict[str, Any]] = []
    index = 0
    while timestamp <= close_at:
        if not current:
            open_price = close_price = 10.0
            volume = 50_000
        elif index == 0:
            open_price, close_price, volume = 10.8, 11.0, 200_000
        elif index == 1:
            open_price, close_price, volume = 11.0, 11.1, 200_000
        elif index == 2:
            open_price, close_price, volume = 11.1, 11.2, 200_000
        else:
            open_price = 11.2 + (index - 3) * 0.02
            close_price = open_price + 0.02
            volume = 100_000
        rows.append(
            {
                "symbol": "ABC",
                "timestamp": timestamp.isoformat(),
                "open": round(open_price, 4),
                "high": round(max(open_price, close_price) + 0.05, 4),
                "low": round(min(open_price, close_price) - 0.05, 4),
                "close": round(close_price, 4),
                "volume": volume,
            }
        )
        timestamp += timedelta(minutes=5)
        index += 1
    return rows


def _build_retained_snapshot(
    tmp_path: Path,
    *,
    prior_market_date: str = "2026-07-14",
) -> tuple[Path, Path, Path]:
    output_root = tmp_path / "lab"
    bars_path = tmp_path / "bars.csv"
    _write_csv(
        bars_path,
        BAR_FIELDS,
        _session_bars(prior_market_date, current=False)
        + _session_bars("2026-07-15", current=True),
    )
    context_path = tmp_path / "context.csv"
    universe_payload = {
        "schema_version": "v2.mover_candidate_universe.v1",
        "market_date": "2026-07-15",
        "feature_cutoff_at": "2026-07-15T09:45:00-04:00",
        "evidence_mode": "historical_replay",
        "system_received_at": None,
        "universe_selection_method": "scheduled_universe",
        "expected_symbols": ["ABC"],
        "expected_symbols_complete": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    universe_source = tmp_path / "universe_source.json"
    universe_source.write_text(
        json.dumps(universe_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    universe_digest = hashlib.sha256(
        json.dumps(
            universe_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    universe_ref = f"sha256:{universe_digest}:{universe_source.resolve()}"
    context = {
        "market_date": "2026-07-15",
        "symbol": "ABC",
        "context_observed_at": "2026-07-15T09:44:00-04:00",
        "universe_selected_at": "2026-07-15T08:30:00-04:00",
        "universe_source_ref": universe_ref,
        "universe_selection_method": "scheduled_universe",
        "spread_pct": 0.5,
        "split_adjusted": True,
        "reverse_split_days": 180,
        "reverse_split_lookback_clear": True,
        "recent_offering_days": 60,
        "offering_lookback_clear": True,
        "halt_state": "clear",
        "source_conflict": False,
        "catalyst_verified": False,
        "catalyst_published_at": "",
        "catalyst_source_url": "",
        "catalyst_source_type": "",
        "catalyst_artifact_ref": "",
        "source_refs": universe_ref,
    }
    _write_csv(context_path, tuple(context), [context])
    result = build_snapshots_from_bars(
        bars_csv=bars_path,
        context_csv=context_path,
        market_date="2026-07-15",
        cutoffs=("09:45",),
        min_baseline_sessions=1,
        bar_interval_minutes=5,
        bar_timestamp_semantics="bar_close",
        evidence_mode="historical_replay",
        output_root=output_root,
    )
    assert result["status"] == "passed"
    assert result["snapshot_count"] == 1
    return output_root, bars_path, Path(result["snapshot_path"])


def test_manifest_pipeline_separates_replay_and_renders_null_forward_days(
    tmp_path: Path,
) -> None:
    output_root, bars_path, snapshots_path = _build_retained_snapshot(tmp_path)
    scan = paper_scan(
        snapshots_path=snapshots_path,
        expected_market_dates=("2026-07-15", "2026-07-16"),
        output_root=output_root,
    )
    assert scan["status"] == "passed_with_not_evaluated"
    assert scan["signal_count"] == 1
    reconciliation = reconcile_paper_signals(
        signals_path=Path(scan["signals_path"]),
        bars_csv=bars_path,
        bar_interval_minutes=5,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )
    snapshots_path.unlink()
    bars_path.unlink()
    report = analyze(
        scan_manifest_path=Path(scan["run_manifest_path"]),
        reconcile_manifest_path=Path(reconciliation["run_manifest_path"]),
        output_root=output_root,
    )

    assert report["status"] == "passed_without_forward_performance"
    assert report["closed_trade_count"] == 0
    assert report["historical_replay_closed_trade_count"] == 1
    opening = next(
        row
        for row in report["strategy_results"]
        if row["strategy_id"] == "mover_opening_drive_rvol_v1"
    )
    assert opening["metrics"]["sample_size"] == 0
    assert opening["historical_replay_metrics"]["sample_size"] == 1
    assert opening["coverage_pct"] is None
    forward_days = [
        row
        for row in report["strategy_daily_calendar"]
        if row["evidence_mode"] == "forward_observation"
    ]
    assert len(forward_days) == 4
    assert all(row["status"] == "not_evaluated" for row in forward_days)
    assert all(row["paper_book_return_pct"] is None for row in forward_days)
    html_path = Path(report["strategy_daily_calendar_html_path"])
    assert html_path.is_file()
    assert "Historical replay" in html_path.read_text(encoding="utf-8")
    verification = verify(output_root=output_root)
    assert verification["status"] == "passed", verification


def test_candidate_runtime_labels_every_retained_snapshot(tmp_path: Path) -> None:
    output_root, bars_path, snapshots_path = _build_retained_snapshot(tmp_path)
    snapshot = json.loads(snapshots_path.read_text(encoding="utf-8").strip())

    source_ref = snapshot["universe_source_ref"]
    denominators_path = tmp_path / "denominators.json"
    denominators_path.write_text(
        json.dumps(
            [
                {
                    "market_date": snapshot["market_date"],
                    "feature_cutoff_at": snapshot["feature_cutoff_at"],
                    "expected_symbols": ["ABC"],
                    "source_ref": source_ref,
                    "expected_symbols_complete": True,
                    "evidence_mode": "historical_replay",
                    "system_received_at": None,
                    "universe_selection_method": "scheduled_universe",
                }
            ],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    splits_path = tmp_path / "splits.json"
    splits_path.write_text(
        json.dumps(
            {"assignments": {snapshot["snapshot_id"]: "discovery"}},
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    local_eod_source = tmp_path / "daily_movers_2026-07-15.csv"
    corporate_action_source = tmp_path / "corporate_actions_2026-07-15.json"
    corporate_action_bytes = json.dumps(
        {
            "schema_version": "v2.corporate_action_evidence.v1",
            "market_date": "2026-07-15",
            "symbol": "ABC",
            "corporate_action_status": "verified_clear",
            "source": "test_exchange_action_feed",
            "observed_at": "2026-07-15T16:05:00-04:00",
            "research_only": True,
            "broker_execution_enabled": False,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    corporate_action_source.write_bytes(corporate_action_bytes)
    corporate_action_ref = (
        "sha256:" + hashlib.sha256(corporate_action_bytes).hexdigest()
    )
    eod_fields = (
        "date",
        "ticker",
        "rank",
        "price",
        "change_pct",
        "volume",
        "source_coverage_complete",
        "list_coverage_complete",
        "expected_row_count",
        "corporate_action_status",
        "corporate_action_source_ref",
        "corporate_action_source_path",
        "extracted_at",
    )
    _write_csv(
        local_eod_source,
        eod_fields,
        [
            {
                "date": "2026-07-15",
                "ticker": "ABC",
                "rank": 1,
                "price": 12.5,
                "change_pct": 25.0,
                "volume": 5_000_000,
                "source_coverage_complete": True,
                "list_coverage_complete": True,
                "expected_row_count": 1,
                "corporate_action_status": "verified_clear",
                "corporate_action_source_ref": corporate_action_ref,
                "corporate_action_source_path": str(
                    corporate_action_source.resolve()
                ),
                "extracted_at": "2026-07-15T16:05:00-04:00",
            }
        ],
    )
    provider_result = LocalDailyMoversProvider(local_eod_source).collect(
        market_date="2026-07-15",
        out_dir=tmp_path / "eod_provider",
    )
    assert provider_result["status"] == "success"
    eod_path = tmp_path / "verified_eod.csv"
    write_daily_mover_csv(eod_path, provider_result["rows"])

    result = run_candidate_study(
        snapshots_path=snapshots_path,
        bars_csv=bars_path,
        universe_denominators_path=denominators_path,
        split_assignments_path=splits_path,
        descriptive_eod_movers_path=eod_path,
        bar_interval_minutes=5,
        slippage_bps=10,
        fee_bps=1,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )

    assert result["status"] == "passed"
    assert result["snapshot_count"] == 1
    assert result["complete_outcome_count"] == 1
    assert result["all_candidate_coverage_complete"] is True
    assert result["evidence_mode"] == "historical_replay"
    assert result["forward_learning_eligible"] is False
    outcomes = [
        json.loads(line)
        for line in Path(result["outcomes_path"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(outcomes) == 1
    assert outcomes[0]["split"] == "discovery"
    assert outcomes[0]["after_cost_close_return_pct"] is not None
    assert outcomes[0]["eod_mover_matched"] is True
    assert result["automatic_strategy_creation"] is False
    assert result["broker_execution_enabled"] is False
    for mutable_input in (bars_path, denominators_path, splits_path, eod_path):
        mutable_input.unlink()
    verification = verify(output_root=output_root)
    assert verification["status"] == "passed", verification

    retained_corporate_action = Path(
        provider_result["rows"][0]["corporate_action_source_path"]
    )
    tampered_payload = json.loads(
        retained_corporate_action.read_text(encoding="utf-8")
    )
    tampered_payload["source"] = "tampered_source"
    retained_corporate_action.write_text(
        json.dumps(tampered_payload, sort_keys=True, separators=(",", ":")),
        encoding="utf-8",
    )
    tampered = verify(output_root=output_root)
    assert tampered["status"] == "failed", tampered


def test_candidate_runtime_requires_distinct_corporate_action_artifact() -> None:
    same = "sha256:" + "a" * 64 + ":C:/evidence/shared.json"
    alias = "sha256:" + "b" * 64 + ":C:/evidence/shared.json"
    distinct = "sha256:" + "b" * 64 + ":C:/evidence/corporate.json"

    assert _same_artifact_path(same, same)
    assert _same_artifact_path(same, alias)
    assert not _same_artifact_path(same, distinct)


def test_candidate_verifier_binds_split_registry_to_retained_input(
    tmp_path: Path,
) -> None:
    split_path = tmp_path / "splits.json"
    split_path.write_text(
        json.dumps({"assignments": {"snapshot-a": "discovery"}}),
        encoding="utf-8",
    )
    matching = {"assignments": {"snapshot-a": "discovery"}}
    divergent = {"assignments": {"snapshot-a": "locked_test"}}

    assert _split_registry_matches_source_file(matching, split_path)
    assert not _split_registry_matches_source_file(divergent, split_path)


def test_candidate_verifier_rejects_output_path_escape(tmp_path: Path) -> None:
    paths = MoverLabPaths.create(tmp_path / "lab")
    manifest = {
        "study_path": tmp_path / "outside" / "study.json",
        "outcomes_path": paths.trades / "candidate_outcomes" / "outcomes.jsonl",
        "coverage_path": paths.reports / "candidate_studies" / "coverage.csv",
        "snapshots_path": (
            paths.source_artifacts / "candidate_study" / "snapshots" / "rows.jsonl"
        ),
        "bars_csv": paths.source_artifacts / "candidate_study" / "bars" / "bars.csv",
        "universe_denominators_path": (
            paths.source_artifacts
            / "candidate_study"
            / "denominators"
            / "denominators.json"
        ),
        "split_assignments_path": (
            paths.source_artifacts / "candidate_study" / "splits" / "splits.json"
        ),
        "split_registry_path": (
            paths.manifests / "candidate_split_registry" / "registry.json"
        ),
        "descriptive_eod_movers_path": None,
    }

    assert not _candidate_manifest_paths_confined(paths, manifest)


def test_snapshot_does_not_skip_missing_immediate_previous_session(
    tmp_path: Path,
) -> None:
    output_root, _, snapshots_path = _build_retained_snapshot(
        tmp_path,
        prior_market_date="2026-07-13",
    )
    snapshot = json.loads(snapshots_path.read_text(encoding="utf-8").strip())

    assert snapshot["previous_close"] is None
    assert snapshot["raw_payload"]["expected_previous_market_session"] == (
        "2026-07-14"
    )
    assert snapshot["raw_payload"]["previous_close_market_date"] is None
    scan = paper_scan(
        snapshots_path=snapshots_path,
        expected_market_dates=("2026-07-15",),
        output_root=output_root,
    )
    assert scan["signal_count"] == 0


def test_paper_scan_rejects_snapshot_content_not_matching_retained_artifact(
    tmp_path: Path,
) -> None:
    output_root, _, snapshots_path = _build_retained_snapshot(tmp_path)
    snapshot = json.loads(snapshots_path.read_text(encoding="utf-8").strip())
    snapshot["price"] = float(snapshot["price"]) + 1.0
    fabricated_path = tmp_path / "fabricated.jsonl"
    fabricated_path.write_text(
        json.dumps(snapshot, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="retained artifact"):
        paper_scan(
            snapshots_path=fabricated_path,
            expected_market_dates=("2026-07-15",),
            output_root=output_root,
        )


def test_reconciliation_observation_identity_includes_cost_assumptions(
    tmp_path: Path,
) -> None:
    output_root, bars_path, snapshots_path = _build_retained_snapshot(tmp_path)
    scan = paper_scan(
        snapshots_path=snapshots_path,
        expected_market_dates=("2026-07-15",),
        output_root=output_root,
    )
    first = reconcile_paper_signals(
        signals_path=Path(scan["signals_path"]),
        bars_csv=bars_path,
        slippage_bps=5,
        fee_bps=1,
        bar_interval_minutes=5,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )
    second = reconcile_paper_signals(
        signals_path=Path(scan["signals_path"]),
        bars_csv=bars_path,
        slippage_bps=20,
        fee_bps=2,
        bar_interval_minutes=5,
        bar_timestamp_semantics="bar_close",
        output_root=output_root,
    )
    first_trade = json.loads(Path(first["trades_path"]).read_text(encoding="utf-8"))
    second_trade = json.loads(Path(second["trades_path"]).read_text(encoding="utf-8"))
    assert first_trade["observation_id"] != second_trade["observation_id"]
    assert first_trade["total_cost"] != second_trade["total_cost"]


def test_snapshot_builder_rejects_naive_bar_clock(tmp_path: Path) -> None:
    bars_path = tmp_path / "naive.csv"
    _write_csv(
        bars_path,
        BAR_FIELDS,
        [
            {
                "symbol": "ABC",
                "timestamp": "2026-07-15T09:35:00",
                "open": 10,
                "high": 10.2,
                "low": 9.9,
                "close": 10.1,
                "volume": 1000,
            }
        ],
    )
    with pytest.raises(ValueError, match="timezone"):
        build_snapshots_from_bars(
            bars_csv=bars_path,
            market_date="2026-07-15",
            bar_timestamp_semantics="bar_close",
            output_root=tmp_path / "lab",
        )
