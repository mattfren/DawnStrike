from __future__ import annotations

import ast
import json
from datetime import date, datetime, time, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data_truth import (
    build_data_truth_snapshot,
    import_local_csv_provider,
    reconcile_provider_datasets,
)
from intraday_scanner.v2.data_truth.canonical import classify_canonical_data
from intraday_scanner.v2.data_truth.models import (
    DataTruthManifest,
    DataTruthReconciliationReport,
)
from intraday_scanner.v2.data_truth.reconcile import (
    ReconciliationTolerances,
    reconcile_datasets_v2,
)
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.models import (
    PaperPick,
    PaperPickDecision,
    PaperRunMode,
    stable_id,
    stable_json,
)
from intraday_scanner.v2.paper_ops.readiness import forward_readiness
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence
from intraday_scanner.v2.strategies import Direction

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


def _bar(
    symbol: str,
    day: date,
    open_price: float,
    high: float,
    low: float,
    close: float,
    volume: int = 1000,
) -> MarketBar:
    return MarketBar(
        symbol=symbol,
        timestamp=datetime.combine(day, time(21, 0), tzinfo=timezone.utc),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=volume,
    )


def _manifest(snapshot_id: str = "snapshot-test") -> DataTruthManifest:
    return DataTruthManifest(
        snapshot_id=snapshot_id,
        created_at=NOW.isoformat(),
        provider_id="public_yahoo_chart",
        provider_name="Yahoo Finance Chart API",
        symbols=("TST",),
        timeframe="1d",
        requested_start="2026-01-01",
        requested_end="2026-01-03",
        accepted_start="2026-01-01",
        accepted_end="2026-01-03",
        bar_count=3,
        accepted_bar_count=3,
        rejected_bar_count=0,
        skipped_incomplete_bars=0,
        validation_status="passed",
        warnings=(),
        raw_artifact_hashes={},
        normalized_artifact_hash="fixture",
        source_url_or_reference=("fixture",),
    )


def _dataset() -> MarketDataset:
    return MarketDataset(
        dataset_id="fixture",
        source_kind="public_yahoo_chart",
        timeframe="1d",
        bars_by_symbol={
            "TST": (
                _bar("TST", date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2),
                _bar("TST", date(2026, 1, 3), 10.5, 11.0, 9.6, 10.8),
            )
        },
    )


def _strategy_row(output_root: Path) -> dict[str, object]:
    registry = json.loads((output_root / "state" / "strategy_registry.json").read_text())
    assert registry
    row = registry[0]
    assert isinstance(row, dict)
    return row


def _accepted_pick(output_root: Path, run_date: date, mode: PaperRunMode) -> PaperPick:
    strategy = _strategy_row(output_root)
    signal_time = datetime.combine(run_date, time(21, 0), tzinfo=timezone.utc).isoformat()
    strategy_id = str(strategy["strategy_id"])
    strategy_version = str(strategy["strategy_version"])
    return PaperPick(
        pick_id=stable_id(
            mode.value,
            run_date.isoformat(),
            strategy_id,
            strategy_version,
            "TST",
            signal_time,
            Direction.LONG,
        ),
        run_id=stable_id("paper_ops", mode.value, run_date.isoformat(), "snapshot-test"),
        mode=mode,
        trade_date=run_date.isoformat(),
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_status=str(strategy["strategy_status"]),
        symbol="TST",
        signal_time=signal_time,
        direction=Direction.LONG,
        setup_score=85.0,
        entry_reference=10.2,
        stop=9.0,
        target=12.0,
        risk_per_unit=1.2,
        reward_per_unit=1.8,
        reward_risk=1.5,
        decision=PaperPickDecision.ACCEPTED,
        reason="accepted",
        evidence=("fixture accepted pick",),
    )


def test_datatruth_snapshot_skips_incomplete_and_rejects_bad_rows(tmp_path: Path) -> None:
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    (raw_dir / "tst_chart.json").write_text('{"fixture": true}', encoding="utf-8")
    source_csv = tmp_path / "source.csv"
    fixture = MarketDataset(
        dataset_id="source",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "TST": (
                _bar("TST", date(2026, 1, 1), 10.0, 10.5, 9.5, 10.2),
                _bar("TST", date(2026, 1, 1), 10.0, 10.6, 9.4, 10.3),
                _bar("TST", date(2026, 1, 2), 10.3, 10.8, 10.1, 10.6),
                _bar("TST", date(2026, 1, 3), 11.0, 10.5, 10.0, 10.2),
                _bar("TST", date(2026, 1, 5), 10.2, 10.4, 10.0, 10.1),
            )
        },
    )
    write_ohlcv_csv(fixture, source_csv)

    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=tmp_path / "datatruth",
        created_at=NOW,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    rerun = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=tmp_path / "datatruth",
        created_at=NOW,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )

    assert result.manifest.accepted_end == "2026-01-02"
    assert result.manifest.accepted_bar_count == 2
    assert result.manifest.rejected_bar_count >= 2
    assert result.manifest.skipped_incomplete_bars == 1
    assert result.manifest.normalized_artifact_hash == rerun.manifest.normalized_artifact_hash
    assert result.reconciliation.status == "single_provider_unreconciled"
    assert any("duplicate timestamp" in warning for warning in result.manifest.warnings)
    assert any("invalid high" in warning for warning in result.manifest.warnings)
    assert any("skipped incomplete daily bar" in warning for warning in result.manifest.warnings)


def test_datatruth_provider_reconciliation_detects_mismatch() -> None:
    canonical = MarketDataset(
        dataset_id="canonical",
        source_kind="provider_a",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", date(2026, 1, 2), 10.0, 11.0, 9.5, 10.5),)},
    )
    comparison = MarketDataset(
        dataset_id="comparison",
        source_kind="provider_b",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", date(2026, 1, 2), 10.0, 11.0, 9.5, 10.9),)},
    )
    report = reconcile_provider_datasets(
        canonical_dataset=canonical,
        comparison_datasets={"provider_b": comparison},
        canonical_snapshot_id="snapshot-a",
        canonical_provider_id="provider_a",
        created_at=NOW,
        price_tolerance=0.01,
    )

    assert report.status == "provider_disagreement"
    assert report.provider_count == 2
    assert report.disagreements[0].field_name == "close"


def test_local_csv_import_provider_normalizes_variants_and_hashes(tmp_path: Path) -> None:
    source = tmp_path / "TST.csv"
    source.write_text(
        "\n".join(
            [
                "Date,Open,High,Low,Adjusted_Close,Volume",
                "2026-01-01,10,11,9,10.5,1000",
                "2026-01-01,10,11,9,10.5,1000",
                "2026-01-02,10,9,8,8.5,1000",
                "2026-01-05,10,11,9,10.2,1000",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    first = import_local_csv_provider(
        path=source,
        provider_id="local_csv",
        as_of_date=date(2026, 1, 5),
        output_root=tmp_path / "datatruth",
    )
    second = import_local_csv_provider(
        path=source,
        provider_id="local_csv",
        as_of_date=date(2026, 1, 5),
        output_root=tmp_path / "datatruth",
    )

    assert first.snapshot.dataset.symbols == ("TST",)
    assert first.snapshot.manifest.accepted_bar_count == 1
    assert first.rejected_bar_count >= 2
    assert first.skipped_incomplete_bars == 1
    assert (
        first.snapshot.manifest.raw_artifact_hashes
        == second.snapshot.manifest.raw_artifact_hashes
    )
    assert (
        first.snapshot.manifest.normalized_artifact_hash
        == second.snapshot.manifest.normalized_artifact_hash
    )


def test_reconciliation_v2_classifies_match_minor_mismatch_and_overlap() -> None:
    canonical = MarketDataset(
        dataset_id="canonical",
        source_kind="provider_a",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", date(2026, 1, 2), 100.0, 101.0, 99.0, 100.0),)},
    )
    identical = MarketDataset(
        dataset_id="same",
        source_kind="provider_b",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", date(2026, 1, 2), 100.0, 101.0, 99.0, 100.0),)},
    )
    minor = MarketDataset(
        dataset_id="minor",
        source_kind="provider_b",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", date(2026, 1, 2), 100.03, 101.0, 99.0, 100.0),)},
    )
    mismatch = MarketDataset(
        dataset_id="bad",
        source_kind="provider_b",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", date(2026, 1, 2), 103.0, 104.0, 99.0, 103.0),)},
    )
    no_overlap = MarketDataset(
        dataset_id="none",
        source_kind="provider_b",
        timeframe="1d",
        bars_by_symbol={"ALT": (_bar("ALT", date(2026, 1, 2), 1.0, 1.1, 0.9, 1.0),)},
    )

    assert (
        reconcile_datasets_v2(
            canonical_dataset=canonical,
            comparison_datasets={"provider_b": identical},
            canonical_snapshot_id="snapshot",
            canonical_provider_id="provider_a",
        ).report.status
        == "reconciled"
    )
    assert (
        reconcile_datasets_v2(
            canonical_dataset=canonical,
            comparison_datasets={"provider_b": minor},
            canonical_snapshot_id="snapshot",
            canonical_provider_id="provider_a",
            tolerances=ReconciliationTolerances(
                price_abs_tolerance=0.01,
                price_minor_abs_tolerance=0.05,
                price_bps_tolerance=0.5,
                price_minor_bps_tolerance=10.0,
            ),
        ).report.status
        == "reconciled_with_minor_diffs"
    )
    mismatch_result = reconcile_datasets_v2(
        canonical_dataset=canonical,
        comparison_datasets={"provider_b": mismatch},
        canonical_snapshot_id="snapshot",
        canonical_provider_id="provider_a",
    )
    assert mismatch_result.report.status == "mismatch"
    assert mismatch_result.block_forward is True
    assert (
        reconcile_datasets_v2(
            canonical_dataset=canonical,
            comparison_datasets={"provider_b": no_overlap},
            canonical_snapshot_id="snapshot",
            canonical_provider_id="provider_a",
        ).report.status
        == "insufficient_overlap"
    )
    single = reconcile_datasets_v2(
        canonical_dataset=canonical,
        comparison_datasets={},
        canonical_snapshot_id="snapshot",
        canonical_provider_id="provider_a",
    )
    assert single.report.status == "single_provider_unreconciled"


def test_canonical_classification_blocks_mismatch_and_allows_explicit_single_provider() -> None:
    manifest = _manifest()
    mismatch_report = DataTruthReconciliationReport(
        reconciliation_id="recon",
        created_at=NOW.isoformat(),
        canonical_snapshot_id=manifest.snapshot_id,
        provider_count=2,
        status="mismatch",
        canonical_provider_id=manifest.provider_id,
        compared_provider_ids=("local_csv",),
        disagreements=(),
        warnings=("material mismatch",),
    )
    single_report = DataTruthReconciliationReport(
        reconciliation_id="single",
        created_at=NOW.isoformat(),
        canonical_snapshot_id=manifest.snapshot_id,
        provider_count=1,
        status="single_provider_unreconciled",
        canonical_provider_id=manifest.provider_id,
        compared_provider_ids=(),
        disagreements=(),
        warnings=(),
    )

    assert (
        classify_canonical_data(manifest=manifest, reconciliation=mismatch_report).allow_forward
        is False
    )
    assert (
        classify_canonical_data(manifest=manifest, reconciliation=single_report).allow_forward
        is True
    )


def test_paperops_models_have_stable_ids_and_json() -> None:
    assert stable_id("paper ops", "forward", None, "TST") == "paper_ops:forward:TST"
    left = {"b": 2, "a": {"mode": PaperRunMode.FORWARD}}
    right = {"a": {"mode": PaperRunMode.FORWARD}, "b": 2}
    assert stable_json(left) == stable_json(right)
    assert '"mode":"forward"' in stable_json(left)


def test_paperops_daily_fill_is_next_bar_and_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    dataset = _dataset()
    manifest = _manifest()
    reconciliation = DataTruthReconciliationReport(
        reconciliation_id="recon",
        created_at=NOW.isoformat(),
        canonical_snapshot_id=manifest.snapshot_id,
        provider_count=1,
        status="single_provider_unreconciled",
        canonical_provider_id=manifest.provider_id,
        compared_provider_ids=(),
        disagreements=(),
        warnings=("single provider",),
    )

    def fake_loader(**kwargs: object) -> tuple[MarketDataset, DataTruthManifest, tuple[str, ...]]:
        assert kwargs["mode"] is PaperRunMode.FORWARD
        return dataset, manifest, reconciliation.warnings

    monkeypatch.setattr(paper_ops_engine, "_load_dataset_for_mode", fake_loader)
    paper_ops_engine.init(output_root=output_root)
    pick = _accepted_pick(output_root, date(2026, 1, 2), PaperRunMode.FORWARD)
    paper_ops_engine.write_json(
        output_root / "exports" / "picks_forward_2026-01-02.json",
        [pick.to_dict()],
    )

    first_enter = paper_ops_engine.enter(run_date=date(2026, 1, 2), output_root=output_root)
    second_enter = paper_ops_engine.enter(run_date=date(2026, 1, 2), output_root=output_root)
    pending_before_fill = json.loads((output_root / "state" / "pending_orders.json").read_text())
    same_day = paper_ops_engine.check(run_date=date(2026, 1, 2), output_root=output_root)
    next_day = paper_ops_engine.check(run_date=date(2026, 1, 3), output_root=output_root)

    assert first_enter["orders_created"] == 1
    assert second_enter["orders_created"] == 0
    assert pending_before_fill[0]["earliest_fill_date"] == "2026-01-03"
    assert same_day["fills"] == 0
    assert next_day["fills"] == 1
    assert next_day["open_positions"] == 1
    events = paper_ops_engine.read_jsonl(output_root / "ledger" / "paper_ledger.jsonl")
    event_ids = [event["event_id"] for event in events]
    assert len(event_ids) == len(set(event_ids))
    assert any(event["event_type"] == "paper_position_checked_no_action" for event in events)


def test_paperops_earliest_fill_falls_back_to_next_weekday_when_no_next_bar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    dataset = MarketDataset(
        dataset_id="friday-only",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "TST": (_bar("TST", date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2),)
        },
    )
    manifest = _manifest()

    def fake_loader(**_kwargs: object) -> tuple[MarketDataset, DataTruthManifest, tuple[str, ...]]:
        return dataset, manifest, ()

    monkeypatch.setattr(paper_ops_engine, "_load_dataset_for_mode", fake_loader)
    paper_ops_engine.init(output_root=output_root)
    pick = _accepted_pick(output_root, date(2026, 1, 2), PaperRunMode.FORWARD)
    paper_ops_engine.write_json(
        output_root / "exports" / "picks_forward_2026-01-02.json",
        [pick.to_dict()],
    )

    paper_ops_engine.enter(run_date=date(2026, 1, 2), output_root=output_root)
    pending = json.loads((output_root / "state" / "pending_orders.json").read_text())

    assert pending[0]["earliest_fill_date"] == "2026-01-05"


def test_paperops_replay_state_is_isolated_from_forward_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    dataset = _dataset()
    manifest = _manifest()

    def fake_loader(**kwargs: object) -> tuple[MarketDataset, DataTruthManifest, tuple[str, ...]]:
        assert kwargs["mode"] is PaperRunMode.REPLAY
        return dataset, manifest, ()

    monkeypatch.setattr(paper_ops_engine, "_load_dataset_for_mode", fake_loader)
    paper_ops_engine.init(output_root=output_root)
    pick = _accepted_pick(output_root, date(2026, 1, 2), PaperRunMode.REPLAY)
    paper_ops_engine.write_json(
        output_root / "exports" / "picks_replay_2026-01-02.json",
        [pick.to_dict()],
    )
    result = paper_ops_engine.enter(
        run_date=date(2026, 1, 2),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )

    forward_pending = json.loads((output_root / "state" / "pending_orders.json").read_text())
    replay_pending = json.loads((output_root / "state" / "replay_pending_orders.json").read_text())
    assert result["orders_created"] == 1
    assert forward_pending == []
    assert replay_pending[0]["mode"] == "replay"


def test_paperops_repair_moves_replay_positions_out_of_forward_state(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    forward_position = {
        "position_id": "position:order:forward:2026-01-02:TST",
        "order_id": "order:forward:2026-01-02:TST",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "TST",
        "status": "open",
    }
    replay_position = {
        "position_id": "position:order:replay:2026-01-02:TST",
        "order_id": "order:replay:2026-01-02:TST",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "TST",
        "status": "open",
    }
    paper_ops_engine.write_json(
        output_root / "state" / "open_positions.json",
        [forward_position, replay_position],
    )

    paper_ops_engine.init(output_root=output_root)
    forward_rows = json.loads((output_root / "state" / "open_positions.json").read_text())
    replay_rows = json.loads((output_root / "state" / "replay_open_positions.json").read_text())

    assert forward_rows == [forward_position]
    assert replay_rows == [replay_position]


def test_paperops_replay_resets_stale_replay_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paper_ops_engine.write_json(
        output_root / "state" / "replay_open_positions.json",
        [{"position_id": "position:order:replay:stale"}],
    )
    seen: list[date] = []

    def fake_run_day(
        *,
        run_date: date,
        mode: PaperRunMode,
        output_root: Path,
        allow_fetch: bool = True,
    ) -> dict[str, object]:
        assert allow_fetch is True
        assert mode is PaperRunMode.REPLAY
        assert json.loads((output_root / "state" / "replay_open_positions.json").read_text()) == []
        seen.append(run_date)
        return {"run_id": run_date.isoformat()}

    monkeypatch.setattr(paper_ops_engine, "run_day", fake_run_day)

    result = paper_ops_engine.replay(
        start=date(2026, 1, 2),
        end=date(2026, 1, 3),
        output_root=output_root,
    )

    assert result["days"] == 2
    assert seen == [date(2026, 1, 2), date(2026, 1, 3)]


def test_paperops_reconciliation_flags_duplicate_and_orphan_events(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    duplicate = {
        "event_id": "dup",
        "event_type": "paper_order_created",
        "mode": "forward",
        "payload": {"order_id": "order-1"},
        "run_id": "run",
        "schema_version": "test",
        "strategy_id": "strategy",
        "symbol": "TST",
        "trade_date": "2026-01-02",
    }
    orphan_fill = {
        **duplicate,
        "event_id": "orphan-fill",
        "event_type": "paper_fill",
        "payload": {"fill_id": "fill-1", "order_id": "missing-order"},
    }
    (paths.ledger / "paper_ledger.jsonl").write_text(
        "\n".join(json.dumps(item) for item in (duplicate, duplicate, orphan_fill)) + "\n",
        encoding="utf-8",
    )

    report = paper_ops_engine.reconcile(output_root=output_root)
    assert report["status"] == "failed"
    assert report["duplicate_event_ids"] == ["dup"]
    assert report["orphan_fills"] == ["fill-1"]


def test_forward_mode_rejects_synthetic_datatruth(monkeypatch: pytest.MonkeyPatch) -> None:
    synthetic_manifest = DataTruthManifest(
        **{**_manifest().to_dict(), "provider_id": "synthetic", "provider_name": "Synthetic"}
    )
    synthetic_dataset = MarketDataset(
        dataset_id="synthetic",
        source_kind="synthetic",
        timeframe="1d",
        bars_by_symbol={"TST": (_bar("TST", date(2026, 1, 2), 10.0, 11.0, 9.5, 10.5),)},
    )
    reconciliation = DataTruthReconciliationReport(
        reconciliation_id="recon",
        created_at=NOW.isoformat(),
        canonical_snapshot_id=synthetic_manifest.snapshot_id,
        provider_count=1,
        status="single_provider_unreconciled",
        canonical_provider_id=synthetic_manifest.provider_id,
        compared_provider_ids=(),
        disagreements=(),
        warnings=(),
    )
    monkeypatch.setattr(
        paper_ops_engine,
        "build_data_truth_snapshot",
        lambda **_kwargs: SimpleNamespace(
            dataset=synthetic_dataset,
            manifest=synthetic_manifest,
            reconciliation=reconciliation,
        ),
    )

    with pytest.raises(ValueError, match="rejects synthetic"):
        paper_ops_engine._load_dataset_for_mode(
            run_date=date(2026, 1, 2),
            mode=PaperRunMode.FORWARD,
            allow_fetch=False,
        )


def test_forward_mode_blocks_mismatched_datatruth(monkeypatch: pytest.MonkeyPatch) -> None:
    manifest = _manifest()
    dataset = _dataset()
    reconciliation = DataTruthReconciliationReport(
        reconciliation_id="recon",
        created_at=NOW.isoformat(),
        canonical_snapshot_id=manifest.snapshot_id,
        provider_count=2,
        status="mismatch",
        canonical_provider_id=manifest.provider_id,
        compared_provider_ids=("local_csv",),
        disagreements=(),
        warnings=("mismatch",),
    )
    monkeypatch.setattr(
        paper_ops_engine,
        "build_data_truth_snapshot",
        lambda **_kwargs: SimpleNamespace(
            dataset=dataset,
            manifest=manifest,
            reconciliation=reconciliation,
        ),
    )

    with pytest.raises(ValueError, match="blocks DataTruth status mismatch"):
        paper_ops_engine._load_dataset_for_mode(
            run_date=date(2026, 1, 2),
            mode=PaperRunMode.FORWARD,
            allow_fetch=False,
        )


def test_ledger_rebuild_reconstructs_state_and_detects_stored_mismatch(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = _strategy_row(output_root)["strategy_id"]
    order = {
        "mode": "forward",
        "order_id": "order-1",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
    }
    position = {
        "mode": "forward",
        "position_id": "position-1",
        "order_id": "order-1",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "TST",
        "unrealized_pnl": 25.0,
    }
    close = {
        "mode": "forward",
        "position_id": "position-1",
        "close_id": "close-1",
        "strategy_id": strategy_id,
        "net_pnl": 50.0,
    }
    events = [
        _event_row("e1", "paper_order_created", order),
        _event_row(
            "e2",
            "paper_fill",
            {
                "fill_id": "fill-1",
                "mode": "forward",
                "order_id": "order-1",
                "strategy_id": strategy_id,
            },
        ),
        _event_row("e3", "paper_position_opened", position),
        _event_row("e4", "paper_position_closed", close),
    ]
    (output_root / "ledger" / "paper_ledger.jsonl").write_text(
        "\n".join(json.dumps(item) for item in events) + "\n",
        encoding="utf-8",
    )
    (output_root / "calendar" / "strategy_daily_returns.csv").write_text(
        "date,mode,strategy_id,realized_pnl,daily_return_pct,trades_opened,trades_closed\n"
        f"2026-01-02,forward,{strategy_id},0,0,0,0\n",
        encoding="utf-8",
    )

    result = rebuild_ledger(output_root=output_root)

    assert result.closed_positions
    assert result.account_rows
    assert result.calendar_mismatches
    assert result.status == "mismatch"


def test_ledger_rebuild_warns_on_event_payload_mode_disagreement(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    event = _event_row(
        "contaminated",
        "paper_position_checked_no_action",
        {
            "position_id": "position:order:replay:2026-01-02:TST",
            "order_id": "order:replay:2026-01-02:TST",
            "strategy_id": strategy_id,
            "symbol": "TST",
        },
    )
    (output_root / "ledger" / "paper_ledger.jsonl").write_text(
        json.dumps(event) + "\n",
        encoding="utf-8",
    )

    result = rebuild_ledger(output_root=output_root)

    assert any("payload mode replay" in warning for warning in result.warnings)


def test_calendar_truth_detects_duplicate_rows(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    header = (
        "date,mode,strategy_id,starting_equity,ending_equity,realized_pnl,unrealized_pnl,"
        "total_pnl,daily_return_pct,cumulative_return_pct,drawdown_pct,pending_orders,"
        "trades_closed\n"
    )
    row = f"2026-01-02,forward,{strategy_id},100000,100000,0,0,0,0,0,0,0,0\n"
    (output_root / "calendar" / "strategy_daily_returns.csv").write_text(
        header + row + row,
        encoding="utf-8",
    )

    result = verify_calendar_truth(output_root=output_root)

    assert result.status == "failed"
    assert result.duplicate_rows


def test_strategy_evidence_keeps_insufficient_forward_evidence_unvalidated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    (output_root / "calendar" / "strategy_daily_returns.csv").write_text(
        "date,mode,strategy_id,drawdown_pct\n"
        f"2026-01-02,replay,{strategy_id},0\n"
        f"2026-01-03,forward,{strategy_id},0\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.strategy_evidence._data_status",
        lambda _root: "reconciled",
    )

    result = score_strategy_evidence(output_root=output_root)
    first = result.scores[0]

    assert first["evidence_status"] == "watch"
    assert "more forward paper days" in str(first["blockers"])


def test_strategy_evidence_quarantines_fragile_robustness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    alpha_root = tmp_path / "v2_alpha_lab"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    (output_root / "calendar" / "strategy_daily_returns.csv").write_text(
        "date,mode,strategy_id,drawdown_pct\n"
        f"2026-01-02,replay,{strategy_id},0\n"
        f"2026-01-03,forward,{strategy_id},0\n",
        encoding="utf-8",
    )
    (alpha_root / "reports").mkdir(parents=True)
    (alpha_root / "reports" / "robustness_summary.json").write_text(
        json.dumps(
            {
                "rows": [
                    {
                        "robustness_status": "fragile",
                        "status": "experimental",
                        "strategy_id": strategy_id,
                        "test_return_pct": -0.04,
                        "test_trade_count": 7,
                        "warnings": "negative out-of-sample return",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.strategy_evidence._data_status",
        lambda _root: "reconciled",
    )

    result = score_strategy_evidence(output_root=output_root)
    row = next(item for item in result.scores if item["strategy_id"] == strategy_id)

    assert row["evidence_status"] == "quarantined"
    assert row["robustness_status"] == "fragile"
    assert row["robustness_test_return_pct"] == -0.04
    assert "Alpha Lab robustness status is fragile" in str(row["blockers"])


def test_forward_readiness_blocks_on_rebuild_or_calendar_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.readiness._data_status",
        lambda _root: "single_provider_unreconciled",
    )

    result = forward_readiness(output_root=output_root)

    assert result.status in {"blocked", "ready_with_warnings"}
    assert any("single-provider" in warning for warning in result.warnings)


def test_paperops_modules_avoid_live_execution_and_database_paths() -> None:
    forbidden_import_roots = {
        "app",
        "httpx",
        "requests",
        "socket",
        "sqlite3",
        "streamlit",
        "urllib",
    }
    forbidden_import_prefixes = {
        "intraday_scanner.integrations",
        "intraday_scanner.storage",
    }
    forbidden_calls = {
        "connect",
        "execute",
        "executemany",
        "submit" + "_order",
    }

    for path in Path("intraday_scanner/v2/paper_ops").rglob("*.py"):
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


def _event_row(event_id: str, event_type: str, payload: dict[str, object]) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "mode": payload.get("mode", "forward"),
        "payload": payload,
        "run_id": "run",
        "schema_version": "test",
        "strategy_id": payload.get("strategy_id"),
        "symbol": payload.get("symbol"),
        "trade_date": "2026-01-02",
    }
