from __future__ import annotations

import ast
import csv
import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data_truth import (
    build_data_truth_snapshot,
    import_local_csv_provider,
    reconcile_provider_datasets,
)
from intraday_scanner.v2.data_truth import core as data_truth_core
from intraday_scanner.v2.data_truth.canonical import classify_canonical_data
from intraday_scanner.v2.data_truth.models import (
    DataTruthManifest,
    DataTruthReconciliationReport,
)
from intraday_scanner.v2.data_truth.reconcile import (
    ReconciliationTolerances,
    reconcile_datasets_v2,
)
from intraday_scanner.v2.paper_ops import __main__ as paper_ops_cli
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops import governance as governance_module
from intraday_scanner.v2.paper_ops import ledger_rebuild as ledger_rebuild_module
from intraday_scanner.v2.paper_ops import readiness as readiness_module
from intraday_scanner.v2.paper_ops import strategy_evidence as strategy_evidence_module
from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.governance import apply_evidence_governance
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.models import (
    PaperOpsConfig,
    PaperPick,
    PaperPickDecision,
    PaperPosition,
    PaperPositionStatus,
    PaperRunMode,
    stable_id,
    stable_json,
)
from intraday_scanner.v2.paper_ops.readiness import forward_readiness
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence
from intraday_scanner.v2.strategies import Direction, build_strategy_catalog

NOW = datetime(2026, 1, 5, 12, 0, tzinfo=timezone.utc)


class _SourceTruthStub:
    status = "passed"
    warnings: tuple[str, ...] = ()


@pytest.fixture(autouse=True)
def _stub_strategy_evidence_source_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        strategy_evidence_module,
        "verify_source_bar_truth",
        lambda *, output_root, mode: _SourceTruthStub(),
    )


def _seed_observer_ledger(output_root: Path) -> None:
    """Seed the minimal retained event needed to reach observer semantics."""

    run_id = stable_id("paper_ops", "forward", "2026-01-02", "fixture-snapshot")

    paper_ops_engine.write_jsonl(
        output_root / "ledger" / "paper_ledger.jsonl",
        [
            {
                "event_id": "observer-fixture-event",
                "event_type": "paper_no_setup_decision",
                "mode": "forward",
                "payload": {
                    "decision_id": "observer-fixture-decision",
                    "execution_policy_version": "fixture-policy-v1",
                    "mode": "forward",
                    "run_id": run_id,
                },
                "run_id": run_id,
                "schema_version": "test",
                "strategy_id": "observer-fixture",
                "symbol": "TST",
                "trade_date": "2026-01-02",
            }
        ],
    )
    payload: dict[str, object] = {
        "schema_version": "v2.paper_ops_manifest.v3",
        "run_id": run_id,
        "mode": "forward",
        "run_date": "2026-01-02",
        "data_snapshot_id": "fixture-snapshot",
        "output_artifacts": [],
        "warnings": [],
        "execution_policy_version": "fixture-policy-v1",
        "execution_policy_fingerprint": "fixture-policy-fingerprint",
        "universe_id": "fixture-universe",
        "universe_symbols": ["TST"],
        "data_snapshot_content_hash": "fixture-content-hash",
        "data_snapshot_manifest_payload_hash": "fixture-manifest-hash",
        "data_snapshot_normalized_hash": "fixture-normalized-hash",
        "data_snapshot_normalized_path": "normalized/fixture.csv",
        "data_truth_root_relative": "../v2_data_truth",
    }
    payload["manifest_payload_hash"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    # The run ID contains colons, which are not legal in Windows file names;
    # manifest identity comes from the payload rather than the fixture name.
    paper_ops_engine.write_json(output_root / "manifests" / "observer-fixture-run.json", payload)


def _write_canonical_calendar_rows(
    output_root: Path,
    rows: list[dict[str, object]],
) -> None:
    canonical_rows: list[dict[str, object]] = []
    for overrides in rows:
        row: dict[str, object] = {
            field: 0 for field in paper_ops_engine.CALENDAR_FIELDNAMES
        }
        row.update(
            {
                "date": "2026-01-02",
                "mode": "forward",
                "strategy_id": "observer-fixture",
                "strategy_version": "fixture-v1",
                "strategy_status": "candidate",
                "execution_policy_version": "fixture-policy-v1",
                "strategy_semantics_fingerprint": "unknown",
                "data_snapshot_id": "fixture-snapshot",
                "warnings": "",
                "run_id": stable_id("paper_ops", "forward", "2026-01-02", "fixture-snapshot"),
            }
        )
        row.update(overrides)
        if "run_id" not in overrides:
            row["run_id"] = stable_id(
                "paper_ops",
                str(row["mode"]),
                str(row["date"]),
                str(row["data_snapshot_id"]),
            )
        canonical_rows.append(row)
    paper_ops_engine.write_csv(
        output_root / "calendar" / "strategy_daily_returns.csv",
        canonical_rows,
        paper_ops_engine.CALENDAR_FIELDNAMES,
    )
    forward_rows = [row for row in canonical_rows if row["mode"] == "forward"]
    if not forward_rows:
        return
    # This helper owns the observer fixture's retained event identities; reset
    # its seed so a same-run policy cannot leak into the test's calendar.
    ledger_rows: list[dict[str, object]] = []
    for existing_path in (output_root / "manifests").glob("*.json"):
        existing = paper_ops_engine.read_json(existing_path, {})
        if (
            isinstance(existing, dict)
            and existing.get("schema_version") == "v2.paper_ops_manifest.v3"
        ):
            existing_path.unlink()
    for row in forward_rows:
        run_id = str(row["run_id"])
        run_date = str(row["date"])
        snapshot = str(row["data_snapshot_id"])
        policy = str(row["execution_policy_version"])
        manifest = {
            "schema_version": "v2.paper_ops_manifest.v3",
            "run_id": run_id,
            "mode": "forward",
            "run_date": run_date,
            "data_snapshot_id": snapshot,
            "output_artifacts": [],
            "warnings": [],
            "execution_policy_version": policy,
            "execution_policy_fingerprint": "fixture-policy-fingerprint",
            "universe_id": "fixture-universe",
            "universe_symbols": ["TST"],
            "data_snapshot_content_hash": "fixture-content-hash",
            "data_snapshot_manifest_payload_hash": "fixture-manifest-hash",
            "data_snapshot_normalized_hash": "fixture-normalized-hash",
            "data_snapshot_normalized_path": "normalized/fixture.csv",
            "data_truth_root_relative": "../v2_data_truth",
        }
        manifest["manifest_payload_hash"] = hashlib.sha256(
            json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        paper_ops_engine.write_json(
            output_root / "manifests" / f"forward_{run_date}.json", manifest
        )
        if not any(item.get("run_id") == run_id for item in ledger_rows):
            ledger_rows.append(
                {
                    "event_id": stable_id("observer-calendar", run_id),
                    "event_type": "paper_no_setup_decision",
                    "mode": "forward",
                    "payload": {
                        "execution_policy_version": policy,
                        "mode": "forward",
                        "run_id": run_id,
                    },
                    "run_id": run_id,
                    "schema_version": "test",
                    "strategy_id": "observer-fixture",
                    "symbol": "TST",
                    "trade_date": run_date,
                }
            )
    paper_ops_engine.write_jsonl(output_root / "ledger" / "paper_ledger.jsonl", ledger_rows)


def _tree_bytes_and_directories(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    directories = tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    return directories, files


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
                _bar("TST", date(2026, 1, 3), 10.2, 11.0, 9.6, 10.8),
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
        target=12.3,
        risk_per_unit=1.2,
        reward_per_unit=2.1,
        reward_risk=1.75,
        decision=PaperPickDecision.ACCEPTED,
        reason="accepted",
        evidence=("fixture accepted pick",),
        strategy_semantics_fingerprint=str(strategy["strategy_semantics_fingerprint"]),
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


def test_datatruth_fetch_refreshes_local_cache_before_stale_alpha_fixture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    alpha_cache = Path("data/v2_alpha_lab/fixtures/public_yahoo")
    alpha_cache.mkdir(parents=True)
    write_ohlcv_csv(
        MarketDataset(
            dataset_id="stale-alpha-fixture",
            source_kind="public_yahoo_chart",
            timeframe="1d",
            bars_by_symbol={
                "TST": (_bar("TST", date(2026, 1, 1), 9.0, 9.5, 8.5, 9.1),)
            },
        ),
        alpha_cache / "public_yahoo_ohlcv.csv",
    )
    fetched_cache_dirs: list[Path] = []

    def fake_fetch(*, cache_dir: Path) -> SimpleNamespace:
        fetched_cache_dirs.append(cache_dir)
        fresh_csv = cache_dir / "public_yahoo_ohlcv.csv"
        fresh_dataset = MarketDataset(
            dataset_id="fresh-yahoo",
            source_kind="public_yahoo_chart",
            timeframe="1d",
            bars_by_symbol={
                "TST": (_bar("TST", date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2),)
            },
            source_path=fresh_csv.as_posix(),
            source_refs=("https://example.test/fresh-yahoo",),
        )
        write_ohlcv_csv(fresh_dataset, fresh_csv)
        return SimpleNamespace(dataset=fresh_dataset, warnings=("fresh Yahoo fetch",))

    from intraday_scanner.public_data import yahoo_chart_fetcher

    monkeypatch.setattr(yahoo_chart_fetcher, "fetch_yahoo_chart_daily_dataset", fake_fetch)
    monkeypatch.setattr(data_truth_core, "_comparison_datasets", lambda **_kwargs: {})

    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 3),
        output_root=Path("data/v2_data_truth"),
        created_at=NOW,
        allow_fetch=True,
    )

    assert fetched_cache_dirs == [Path("data/v2_data_truth/cache/public_yahoo")]
    assert result.manifest.accepted_end == "2026-01-02"
    assert result.dataset.bars_by_symbol["TST"][0].close == 10.2
    assert "https://example.test/fresh-yahoo" in result.manifest.source_url_or_reference


def test_datatruth_fetch_failure_falls_back_with_explicit_warning(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    alpha_cache = Path("data/v2_alpha_lab/fixtures/public_yahoo")
    alpha_cache.mkdir(parents=True)
    write_ohlcv_csv(
        MarketDataset(
            dataset_id="cached-alpha-fixture",
            source_kind="public_yahoo_chart",
            timeframe="1d",
            bars_by_symbol={
                "TST": (_bar("TST", date(2026, 1, 1), 9.0, 9.5, 8.5, 9.1),)
            },
        ),
        alpha_cache / "public_yahoo_ohlcv.csv",
    )

    from intraday_scanner.public_data import yahoo_chart_fetcher

    def failed_fetch(*, cache_dir: Path) -> SimpleNamespace:
        del cache_dir
        raise TimeoutError("offline")

    monkeypatch.setattr(yahoo_chart_fetcher, "fetch_yahoo_chart_daily_dataset", failed_fetch)
    monkeypatch.setattr(data_truth_core, "_comparison_datasets", lambda **_kwargs: {})

    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 3),
        output_root=Path("data/v2_data_truth"),
        created_at=NOW,
        allow_fetch=True,
    )

    assert result.manifest.accepted_end == "2026-01-01"
    assert any(
        "refresh failed (TimeoutError: offline); using cached OHLCV" in warning
        for warning in result.manifest.warnings
    )


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


def test_paperops_enforces_position_cap_and_persists_block_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    symbols = ("AAA", "BBB", "CCC", "DDD")
    dataset = MarketDataset(
        dataset_id="risk-cap-fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            symbol: (
                _bar(symbol, date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2),
                _bar(symbol, date(2026, 1, 3), 10.5, 11.0, 9.6, 10.8),
            )
            for symbol in symbols
        },
    )
    monkeypatch.setattr(
        paper_ops_engine,
        "_load_dataset_for_mode",
        lambda **_kwargs: (dataset, _manifest(), ()),
    )
    paper_ops_engine.init(output_root=output_root)
    base = _accepted_pick(output_root, date(2026, 1, 2), PaperRunMode.REPLAY)
    picks = [
        replace(
            base,
            pick_id=f"pick-{symbol}",
            symbol=symbol,
        )
        for symbol in symbols
    ]
    paper_ops_engine.write_json(
        output_root / "exports" / "picks_replay_2026-01-02.json",
        [pick.to_dict() for pick in picks],
    )

    result = paper_ops_engine.enter(
        run_date=date(2026, 1, 2),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )

    assert result["orders_created"] == 3
    assert result["orders_blocked"] == 1
    decisions = json.loads(
        (output_root / "exports" / "order_decisions_replay_2026-01-02.json").read_text()
    )
    blocked = [row for row in decisions if row["decision"] == "blocked"]
    assert blocked[0]["reason"] == "max_concurrent_positions"


def test_paperops_enforces_gross_exposure_cap_and_persists_block_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    symbols = ("AAA", "BBB")
    dataset = MarketDataset(
        dataset_id="gross-cap-fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            symbol: (
                _bar(symbol, date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2),
                _bar(symbol, date(2026, 1, 3), 10.5, 11.0, 9.6, 10.8),
            )
            for symbol in symbols
        },
    )
    monkeypatch.setattr(
        paper_ops_engine,
        "_load_dataset_for_mode",
        lambda **_kwargs: (dataset, _manifest(), ()),
    )
    config_path = output_root / "state" / "paper_ops_config.json"
    paper_ops_engine.write_json(config_path, {"max_gross_exposure_pct": 0.05})
    paper_ops_engine.init(output_root=output_root)
    base = _accepted_pick(output_root, date(2026, 1, 2), PaperRunMode.REPLAY)
    picks = [replace(base, pick_id=f"pick-{symbol}", symbol=symbol) for symbol in symbols]
    paper_ops_engine.write_json(
        output_root / "exports" / "picks_replay_2026-01-02.json",
        [pick.to_dict() for pick in picks],
    )

    result = paper_ops_engine.enter(
        run_date=date(2026, 1, 2),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )

    assert result["orders_created"] == 1
    assert result["orders_blocked"] == 1
    decisions = json.loads(
        (output_root / "exports" / "order_decisions_replay_2026-01-02.json").read_text()
    )
    blocked = [row for row in decisions if row["decision"] == "blocked"]
    assert blocked[0]["reason"] == "max_gross_exposure"


@pytest.mark.parametrize(
    "payload",
    (
        {"starting_equity": 0},
        {"risk_per_trade_pct": 1.1},
        {"max_concurrent_positions": 0},
        {"fee_bps": -1},
        {"allow_experimental": "false"},
        {"universe_symbols": []},
        {"universe_symbols": "SPY"},
    ),
)
def test_paperops_config_rejects_unsafe_or_ambiguous_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        paper_ops_engine._config_from_payload(payload)


def test_paperops_persists_explicit_no_setup_decisions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    strategy = next(
        item
        for item in paper_ops_engine.build_strategy_catalog()
        if item.strategy_id == "pullback_reclaim_uptrend"
    )
    bars = tuple(
        _bar("TST", date(2025, 8, 1) + timedelta(days=index), 10, 10, 10, 10)
        for index in range(140)
    )
    dataset = MarketDataset(
        dataset_id="no-setup-fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"TST": bars},
    )
    monkeypatch.setattr(paper_ops_engine, "build_strategy_catalog", lambda: (strategy,))
    monkeypatch.setattr(
        paper_ops_engine,
        "_load_dataset_for_mode",
        lambda **_kwargs: (dataset, _manifest(), ()),
    )
    paper_ops_engine.init(output_root=output_root)

    result = paper_ops_engine.scan(
        run_date=date(2026, 1, 2),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )

    assert result["decision_coverage_status"] == "complete"
    assert result["decision_coverage"] == 1
    assert result["no_setup_decisions"] == 1
    decisions = json.loads(
        (output_root / "exports" / "strategy_decisions_replay_2026-01-02.json").read_text()
    )
    assert decisions[0]["decision_status"] == "no_setup"
    assert decisions[0]["trade_return_eligible"] is False
    assert decisions[0]["trade_return_pct"] is None


def test_paperops_net_equity_charges_entry_and_exit_fees(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    dataset = MarketDataset(
        dataset_id="fee-fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "TST": (
                _bar("TST", date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2),
                    _bar("TST", date(2026, 1, 3), 10.2, 11.0, 9.6, 10.8),
                _bar("TST", date(2026, 1, 4), 11.0, 12.5, 10.8, 12.2),
            )
        },
    )
    monkeypatch.setattr(
        paper_ops_engine,
        "_load_dataset_for_mode",
        lambda **_kwargs: (dataset, _manifest(), ()),
    )
    paper_ops_engine.init(output_root=output_root)
    pick = _accepted_pick(output_root, date(2026, 1, 2), PaperRunMode.REPLAY)
    paper_ops_engine.write_json(
        output_root / "exports" / "picks_replay_2026-01-02.json",
        [pick.to_dict()],
    )
    paper_ops_engine.enter(
        run_date=date(2026, 1, 2),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )
    paper_ops_engine.check(
        run_date=date(2026, 1, 3),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )
    position = json.loads(
        (output_root / "state" / "replay_open_positions.json").read_text()
    )[0]
    expected_unrealized = (
        (10.8 - float(position["entry_price"])) * int(position["quantity"])
        - float(position["entry_fee"])
    )
    assert position["entry_fee"] > 0
    assert position["unrealized_pnl"] == pytest.approx(expected_unrealized)

    paper_ops_engine.check(
        run_date=date(2026, 1, 4),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )
    close_payload = next(
        event["payload"]
        for event in paper_ops_engine.read_jsonl(output_root / "ledger" / "paper_ledger.jsonl")
        if event["event_type"] == "paper_position_closed"
    )
    assert close_payload["net_pnl"] == pytest.approx(
        float(close_payload["gross_pnl"])
        - float(close_payload["entry_fee"])
        - float(close_payload["fee"])
    )


def test_paperops_blocks_a_new_fill_that_gaps_through_its_stop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    dataset = MarketDataset(
        dataset_id="gap-through-stop",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "TST": (
                _bar("TST", date(2026, 1, 2), 10.0, 10.5, 9.8, 10.2),
                _bar("TST", date(2026, 1, 3), 8.0, 8.5, 7.5, 8.1),
            )
        },
    )
    monkeypatch.setattr(
        paper_ops_engine,
        "_load_dataset_for_mode",
        lambda **_kwargs: (dataset, _manifest(), ()),
    )
    paper_ops_engine.init(output_root=output_root)
    pick = _accepted_pick(output_root, date(2026, 1, 2), PaperRunMode.REPLAY)
    paper_ops_engine.write_json(
        output_root / "exports" / "picks_replay_2026-01-02.json",
        [pick.to_dict()],
    )
    paper_ops_engine.enter(
        run_date=date(2026, 1, 2),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )

    result = paper_ops_engine.check(
        run_date=date(2026, 1, 3),
        mode=PaperRunMode.REPLAY,
        output_root=output_root,
    )

    assert result["fills"] == 0
    assert result["orders_blocked"] == 1
    assert json.loads(
        (output_root / "state" / "replay_open_positions.json").read_text()
    ) == []
    blocked = next(
        event["payload"]
        for event in paper_ops_engine.read_jsonl(output_root / "ledger" / "paper_ledger.jsonl")
        if event["event_type"] == "paper_order_blocked"
    )
    assert blocked["reason"] == "gap_through_stop"
    assert blocked["origin_run_id"] == pick.run_id
    assert blocked["lifecycle_run_id"] != pick.run_id


def test_paperops_existing_position_gap_stop_executes_at_observed_open() -> None:
    config = PaperOpsConfig()
    position = PaperPosition(
        position_id="position-gap",
        order_id="order-gap",
        strategy_id="strategy-gap",
        strategy_version="v1.0",
        symbol="TST",
        direction=Direction.LONG,
        status=PaperPositionStatus.OPEN,
        opened_at=datetime(2026, 1, 2, 21, 0, tzinfo=timezone.utc).isoformat(),
        quantity=10,
        entry_price=100.0,
        stop=99.0,
        target=110.0,
        last_mark_price=100.0,
        entry_fee=0.1,
    )
    gap_bar = _bar("TST", date(2026, 1, 5), 90.0, 92.0, 88.0, 91.0)
    run = paper_ops_engine._paper_run(
        run_date=date(2026, 1, 5),
        mode=PaperRunMode.REPLAY,
        data_snapshot_id="gap-snapshot",
    )

    _, close = paper_ops_engine._check_position(position, gap_bar, run, config)

    assert close is not None
    assert close.close_reason.value == "stop"
    assert close.close_price == pytest.approx(90.0 * (1 - config.slippage_bps / 10_000))
    assert close.net_pnl < 0


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


def test_paperops_strategy_version_change_fails_closed_with_live_exposure(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    registry_path = output_root / "state" / "strategy_registry.json"
    registry = json.loads(registry_path.read_text())
    strategy_id = str(registry[0]["strategy_id"])
    registry[0]["strategy_version"] = "v0.9"
    paper_ops_engine.write_json(registry_path, registry)
    paper_ops_engine.write_json(
        output_root / "state" / "open_positions.json",
        [
            {
                "mode": "forward",
                "position_id": "old-version-position",
                "strategy_id": strategy_id,
                "strategy_version": "v0.9",
            }
        ],
    )

    with pytest.raises(ValueError, match="strategy version changed with live forward"):
        paper_ops_engine.init(output_root=output_root)


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
    monkeypatch.setattr(
        paper_ops_engine,
        "_verify_replay_staging",
        lambda _root: {
            "calendar_truth": "passed",
            "ledger_rebuild": "passed",
            "reconciliation": "passed",
        },
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.source_bar_truth.verify_source_bar_truth",
        lambda *, output_root, mode: _SourceTruthStub(),
    )

    result = paper_ops_engine.replay(
        start=date(2026, 1, 2),
        end=date(2026, 1, 5),
        output_root=output_root,
    )

    assert result["days"] == 2
    assert result["skipped_closed_days"] == ["2026-01-03", "2026-01-04"]
    assert seen == [date(2026, 1, 2), date(2026, 1, 5)]


def test_paperops_replay_reset_replaces_replay_evidence_and_preserves_forward(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    ledger_path = output_root / "ledger" / "paper_ledger.jsonl"
    forward_event = _event_row(
        "forward-event",
        "paper_order_created",
        {"mode": "forward", "order_id": "forward-order", "strategy_id": strategy_id},
    )
    replay_event = dict(
        _event_row(
            "stale-replay-event",
            "paper_order_created",
            {"mode": "replay", "order_id": "replay-order", "strategy_id": strategy_id},
        ),
        mode="replay",
    )
    ledger_path.write_text(
        "\n".join(json.dumps(row) for row in (forward_event, replay_event)) + "\n",
        encoding="utf-8",
    )
    calendar_path = output_root / "calendar" / "strategy_daily_returns.csv"
    calendar_path.write_text(
        "date,mode,strategy_id,starting_equity,ending_equity,daily_return_pct,"
        "cumulative_return_pct,drawdown_pct\n"
        f"2026-01-02,forward,{strategy_id},100000,101000,0.01,0.01,0\n"
        f"2026-01-02,replay,{strategy_id},100000,199000,0.99,0.99,0\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(paper_ops_engine, "run_day", lambda **_kwargs: {"run_id": "fresh"})
    monkeypatch.setattr(
        paper_ops_engine,
        "_verify_replay_staging",
        lambda _root: {
            "calendar_truth": "passed",
            "ledger_rebuild": "passed",
            "reconciliation": "passed",
        },
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.source_bar_truth.verify_source_bar_truth",
        lambda *, output_root, mode: _SourceTruthStub(),
    )
    paper_ops_engine.replay(
        start=date(2026, 1, 5),
        end=date(2026, 1, 5),
        output_root=output_root,
    )

    retained_events = paper_ops_engine.read_jsonl(ledger_path)
    assert [row["event_id"] for row in retained_events] == ["forward-event"]
    retained_calendar = paper_ops_engine._read_calendar_rows(
        paper_ops_engine.PaperOpsPaths.create(output_root)
    )
    assert len(retained_calendar) == 1
    assert retained_calendar[0]["mode"] == "forward"
    assert retained_calendar[0]["daily_return_pct"] == "0.01"


def test_paperops_replay_initializes_a_new_promotion_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "new_paper_ops"
    source_verifier_roots: list[Path] = []

    monkeypatch.setattr(paper_ops_engine, "run_day", lambda **_kwargs: {"run_id": "fresh"})
    monkeypatch.setattr(
        paper_ops_engine,
        "_verify_replay_staging",
        lambda _root: {
            "calendar_truth": "passed",
            "ledger_rebuild": "passed",
            "reconciliation": "passed",
        },
    )

    def verify_promoted_source(*, output_root: Path, mode: PaperRunMode) -> _SourceTruthStub:
        assert mode is PaperRunMode.REPLAY
        assert (output_root / "state" / "execution_policy_manifest.json").is_file()
        source_verifier_roots.append(output_root)
        return _SourceTruthStub()

    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.source_bar_truth.verify_source_bar_truth",
        verify_promoted_source,
    )

    result = paper_ops_engine.replay(
        start=date(2026, 1, 5),
        end=date(2026, 1, 5),
        output_root=output_root,
    )

    assert result["trading_days"] == 1
    assert source_verifier_roots == [output_root]
    assert (output_root / "state" / "strategy_registry.json").is_file()


def test_paperops_replay_invalid_ranges_fail_before_root_mutation(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"

    with pytest.raises(ValueError, match="on or before"):
        paper_ops_engine.replay(
            start=date(2026, 1, 6),
            end=date(2026, 1, 5),
            output_root=output_root,
        )
    assert not output_root.exists()

    with pytest.raises(ValueError, match="no US equities trading sessions"):
        paper_ops_engine.replay(
            start=date(2026, 1, 3),
            end=date(2026, 1, 4),
            output_root=output_root,
        )
    assert not output_root.exists()


def test_paperops_replay_promotion_rolls_back_every_mutated_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    staging_root = tmp_path / "paper_ops_staging"
    paper_ops_engine.init(output_root=output_root)
    paper_ops_engine.init(output_root=staging_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    staging_paths = paper_ops_engine.PaperOpsPaths.create(staging_root)
    forward_event = _event_row(
        "forward-event",
        "paper_order_created",
        {"mode": "forward", "order_id": "forward-order"},
    )
    replay_event = {
        **_event_row(
            "replay-event",
            "paper_order_created",
            {"mode": "replay", "order_id": "replay-order"},
        ),
        "mode": "replay",
    }
    paper_ops_engine.write_jsonl(paths.ledger / "paper_ledger.jsonl", [forward_event])
    paper_ops_engine.write_jsonl(
        staging_paths.ledger / "paper_ledger.jsonl",
        [replay_event],
    )
    paper_ops_engine.write_json(
        paths.state / "replay_open_positions.json",
        [{"position_id": "old-replay-position"}],
    )
    paper_ops_engine.write_json(
        staging_paths.state / "replay_open_positions.json",
        [{"position_id": "new-replay-position"}],
    )
    paper_ops_engine.write_json(paths.exports / "old_replay.json", {"old": True})
    paper_ops_engine.write_json(staging_paths.exports / "new_replay.json", {"new": True})
    calendar_path = paths.calendar / "strategy_daily_returns.csv"
    calendar_path.write_text("date,mode\n2026-01-02,forward\n", encoding="utf-8")
    (staging_paths.calendar / "strategy_daily_returns.csv").write_text(
        "date,mode\n2026-01-02,replay\n",
        encoding="utf-8",
    )
    watched = (
        paths.ledger / "paper_ledger.jsonl",
        calendar_path,
        paths.state / "replay_open_positions.json",
        paths.exports / "old_replay.json",
    )
    before = {path: path.read_bytes() for path in watched}
    monkeypatch.setattr(
        paper_ops_engine,
        "_calendar_paths",
        lambda _paths: (_ for _ in ()).throw(RuntimeError("forced promotion failure")),
    )

    with pytest.raises(RuntimeError, match="forced promotion failure"):
        paper_ops_engine._promote_replay_staging(paths, staging_paths)

    assert {path: path.read_bytes() for path in watched} == before
    assert not (paths.exports / "new_replay.json").exists()


def test_paperops_recovers_pending_transaction_idempotently(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    event = _event_row(
        "recovered-event",
        "paper_order_created",
        {"mode": "forward", "order_id": "recovered-order"},
    )
    journal_path = paths.state / "paper_transaction_pending.json"
    events = [event]
    state_updates = {"state/pending_orders.json": []}
    paper_ops_engine.write_json(
        journal_path,
        {
            "events": events,
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": state_updates,
            "transaction_id": paper_ops_engine._paper_transaction_id(
                events,
                state_updates,
            ),
        },
    )

    paper_ops_engine.init(output_root=output_root)
    paper_ops_engine.init(output_root=output_root)

    recovered = [
        row
        for row in paper_ops_engine.read_jsonl(paths.ledger / "paper_ledger.jsonl")
        if row.get("event_id") == "recovered-event"
    ]
    assert len(recovered) == 1
    assert json.loads((paths.state / "pending_orders.json").read_text()) == []
    assert not journal_path.exists()


def test_strategy_semantics_fingerprints_are_bounded_sha256(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    registry = paper_ops_engine.read_json(paths.state / "strategy_registry.json", [])
    manifest = paper_ops_engine.read_json(
        paths.state / "strategy_semantics_manifest.json",
        {},
    )

    fingerprints = {
        str(row["strategy_semantics_fingerprint"])
        for row in registry
        if isinstance(row, dict)
    }
    assert fingerprints
    assert all(
        len(value) == 64 and set(value) <= set("0123456789abcdef")
        for value in fingerprints
    )
    for row in manifest["strategies"].values():
        configuration = row["configuration"]
        assert "generate_signal_source" not in configuration
        assert "implementation_module_source" not in configuration
        assert len(configuration["generate_signal_source_sha256"]) == 64
        assert len(configuration["implementation_module_sha256"]) == 64


def test_strategy_semantics_drift_requires_new_version(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    monkeypatch.setattr(
        "intraday_scanner.v2.strategy_identity.inspect.getsource",
        lambda _target: "drifted",
    )

    with pytest.raises(ValueError, match="changed under the same strategy version"):
        paper_ops_engine.init(output_root=output_root)


def test_deleted_semantics_manifest_cannot_bypass_registry_fingerprint(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    registry = paper_ops_engine.read_json(paths.state / "strategy_registry.json", [])
    assert isinstance(registry, list)
    assert isinstance(registry[0], dict)
    registry[0]["strategy_semantics_fingerprint"] = "0" * 64
    paper_ops_engine.write_json(paths.state / "strategy_registry.json", registry)
    (paths.state / "strategy_semantics_manifest.json").unlink()

    with pytest.raises(ValueError, match="stored strategy semantics do not match"):
        paper_ops_engine.init(output_root=output_root)


def test_paperops_rejects_transaction_journal_checksum_mismatch(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(
        journal_path,
        {
            "events": [
                _event_row(
                    "tampered-event",
                    "paper_order_created",
                    {"mode": "forward", "order_id": "tampered-order"},
                )
            ],
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": {"state/tampered.json": {"applied": True}},
            "transaction_id": "0" * 64,
        },
    )

    with pytest.raises(ValueError, match="checksum does not match"):
        paper_ops_engine.init(output_root=output_root)

    assert not (paths.state / "tampered.json").exists()
    assert not any(
        row.get("event_id") == "tampered-event"
        for row in paper_ops_engine.read_jsonl(paths.ledger / "paper_ledger.jsonl")
    )
    assert journal_path.exists()


def test_ledger_rebuild_recovers_pending_transaction_before_read(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    strategy = _strategy_row(output_root)
    event = _event_row(
        "journal-decision",
        "paper_no_setup_decision",
        {
            "decision_id": "decision-1",
            "execution_policy_version": strategy["execution_policy_version"],
            "mode": "forward",
            "strategy_id": strategy["strategy_id"],
            "strategy_semantics_fingerprint": strategy[
                "strategy_semantics_fingerprint"
            ],
            "strategy_version": strategy["strategy_version"],
            "symbol": "TST",
        },
    )
    state_updates: dict[str, object] = {}
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(
        journal_path,
        {
            "events": [event],
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": state_updates,
            "transaction_id": paper_ops_engine._paper_transaction_id(
                [event],
                state_updates,
            ),
        },
    )

    with pytest.raises(Exception, match="BLOCKED_PENDING_RECOVERY"):
        rebuild_ledger(output_root=output_root)

    assert journal_path.exists()
    assert not any(
        row.get("event_id") == "journal-decision"
        for row in paper_ops_engine.read_jsonl(paths.ledger / "paper_ledger.jsonl")
    )


def test_ledger_rebuild_fails_closed_on_empty_evidence(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)

    with pytest.raises(Exception, match="MISSING_INPUT"):
        rebuild_ledger(output_root=output_root)


def test_ledger_rebuild_separates_semantics_fingerprints(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    strategy = _strategy_row(output_root)
    wrong_fingerprint = "0" * 64
    event = _event_row(
        "archived-decision",
        "paper_no_setup_decision",
        {
            "decision_id": "archived-decision",
            "execution_policy_version": strategy["execution_policy_version"],
            "mode": "forward",
            "strategy_id": strategy["strategy_id"],
            "strategy_semantics_fingerprint": wrong_fingerprint,
            "strategy_version": strategy["strategy_version"],
            "symbol": "TST",
        },
    )
    paper_ops_engine.write_jsonl(paths.ledger / "paper_ledger.jsonl", [event])

    result = rebuild_ledger(output_root=output_root)

    matching = [
        row
        for row in result.account_rows
        if row["mode"] == "forward"
        and row["strategy_id"] == strategy["strategy_id"]
        and row["strategy_version"] == strategy["strategy_version"]
        and row["execution_policy_version"] == strategy["execution_policy_version"]
    ]
    assert {row["strategy_semantics_fingerprint"] for row in matching} == {
        strategy["strategy_semantics_fingerprint"],
        wrong_fingerprint,
    }
    assert result.status == "mismatch"


def test_ledger_calendar_comparison_includes_exposure(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    strategy = _strategy_row(output_root)
    rebuilt = {
        field: ""
        for field in ledger_rebuild_module.CALENDAR_FIELDNAMES
    }
    rebuilt.update(
        {
            "date": "2026-01-02",
            "execution_policy_version": strategy["execution_policy_version"],
            "exposure_pct": 0.25,
            "mode": "forward",
            "strategy_id": strategy["strategy_id"],
            "strategy_semantics_fingerprint": strategy[
                "strategy_semantics_fingerprint"
            ],
            "strategy_version": strategy["strategy_version"],
        }
    )
    stored = {**rebuilt, "exposure_pct": 0.5}
    paper_ops_engine.write_csv(
        paths.calendar / "strategy_daily_returns.csv",
        [stored],
        ledger_rebuild_module.CALENDAR_FIELDNAMES,
    )

    mismatches = ledger_rebuild_module._compare_calendar(paths, [rebuilt])

    assert any("exposure_pct" in mismatch for mismatch in mismatches)


def test_ledger_account_comparison_includes_realized_and_unrealized(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    strategy = _strategy_row(output_root)
    account_state = paper_ops_engine.read_json(paths.state / "paper_accounts.json", {})
    accounts = account_state["accounts"]
    assert isinstance(accounts, list)
    account = next(row for row in accounts if row["strategy_id"] == strategy["strategy_id"])
    account["realized_pnl"] = 10.0
    account["unrealized_pnl"] = -10.0
    paper_ops_engine.write_json(paths.state / "paper_accounts.json", account_state)
    rebuilt = {
        "current_equity": account["current_equity"],
        "execution_policy_version": account["execution_policy_version"],
        "mode": "forward",
        "realized_pnl": 0.0,
        "starting_equity": account["starting_equity"],
        "strategy_id": account["strategy_id"],
        "strategy_semantics_fingerprint": account[
            "strategy_semantics_fingerprint"
        ],
        "strategy_version": account["strategy_version"],
        "unrealized_pnl": 0.0,
    }

    mismatches = ledger_rebuild_module._compare_accounts(paths, [rebuilt])

    assert any("realized_pnl" in mismatch for mismatch in mismatches)
    assert any("unrealized_pnl" in mismatch for mismatch in mismatches)


def test_jsonl_append_quarantines_and_truncates_incomplete_tail(tmp_path: Path) -> None:
    path = tmp_path / "ledger" / "paper_ledger.jsonl"
    path.parent.mkdir(parents=True)
    path.write_bytes(b'{"event_id":"first"}\n{"event_id":"partial')

    appended = paper_ops_engine.append_jsonl_unique(
        path,
        [{"event_id": "second"}],
        "event_id",
    )

    assert appended == 1
    assert [row["event_id"] for row in paper_ops_engine.read_jsonl(path)] == [
        "first",
        "second",
    ]
    quarantine = list((path.parent / "quarantine").glob("*.json"))
    assert len(quarantine) == 1
    payload = json.loads(quarantine[0].read_text(encoding="utf-8"))
    assert payload["reason"] == "incomplete_or_malformed_final_jsonl_record"
    assert payload["byte_length"] > 0


def test_jsonl_reader_fails_closed_on_incomplete_tail(tmp_path: Path) -> None:
    path = tmp_path / "paper_ledger.jsonl"
    path.write_bytes(b'{"event_id":"first"}\n{"event_id":"partial')

    with pytest.raises(ValueError, match="incomplete JSONL tail"):
        paper_ops_engine.read_jsonl(path)


def test_jsonl_append_adds_separator_after_valid_unterminated_record(tmp_path: Path) -> None:
    path = tmp_path / "paper_ledger.jsonl"
    path.write_bytes(b'{"event_id":"first"}')

    paper_ops_engine.append_jsonl_unique(
        path,
        [{"event_id": "second"}],
        "event_id",
    )

    assert [row["event_id"] for row in paper_ops_engine.read_jsonl(path)] == [
        "first",
        "second",
    ]
    assert not (path.parent / "quarantine").exists()


def test_jsonl_reader_rejects_malformed_complete_record(tmp_path: Path) -> None:
    path = tmp_path / "paper_ledger.jsonl"
    path.write_bytes(b'{"event_id":"first"}\nnot-json\n')

    with pytest.raises(json.JSONDecodeError):
        paper_ops_engine.read_jsonl(path)


def test_paperops_calendar_tracks_incremental_equity_and_mode_scoped_events(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    strategy = _strategy_row(output_root)
    strategy_id = str(strategy["strategy_id"])
    strategy_version = str(strategy["strategy_version"])
    execution_policy_version = str(strategy["execution_policy_version"])
    strategy_semantics_fingerprint = str(strategy["strategy_semantics_fingerprint"])
    paper_ops_engine.write_json(paths.state / "strategy_registry.json", [strategy])
    # This calendar-math fixture deliberately exercises January 2026. Pin the
    # immutable exact-series lineage before that range instead of inheriting the
    # real wall-clock registration date created by init().
    semantics_manifest = paper_ops_engine.read_json(
        paths.state / "strategy_semantics_manifest.json", {}
    )
    semantics_entry = semantics_manifest["strategies"][
        f"{strategy_id}@{strategy_version}"
    ]
    semantics_entry.update(
        {
            "activation_policy": "first_eligible_session",
            "registered_at": "2026-01-01T12:00:00+00:00",
            "coverage_inception_date": "2026-01-02",
        }
    )
    paper_ops_engine.write_json(
        paths.state / "strategy_semantics_manifest.json", semantics_manifest
    )
    policy_manifest = paper_ops_engine.read_json(
        paths.state / "execution_policy_manifest.json", {}
    )
    policy_manifest["policies"][execution_policy_version].update(
        {
            "activation_policy": "first_eligible_session",
            "registered_at": "2026-01-01T12:00:00+00:00",
            "coverage_inception_date": "2026-01-02",
        }
    )
    paper_ops_engine.write_json(
        paths.state / "execution_policy_manifest.json", policy_manifest
    )

    def write_account(*, current: float, realized: float, unrealized: float) -> None:
        paper_ops_engine.write_json(
            paths.state / "paper_accounts.json",
            {
                "accounts": [
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                        "execution_policy_version": execution_policy_version,
                        "starting_equity": 100_000.0,
                        "current_equity": current,
                        "realized_pnl": realized,
                        "unrealized_pnl": unrealized,
                    }
                ],
                "schema_version": "v2.paper_account_state.v1",
            },
        )

    def write_calendar_day(
        run_date: date,
        *,
        current: float,
        realized: float,
        unrealized: float,
    ) -> None:
        write_account(current=current, realized=realized, unrealized=unrealized)
        run = paper_ops_engine._paper_run(
            run_date=run_date,
            mode=PaperRunMode.FORWARD,
            data_snapshot_id=f"snapshot-{run_date.isoformat()}",
        )
        paper_ops_engine._write_calendar_for_date(
            paths,
            run,
            _manifest(f"snapshot-{run_date.isoformat()}"),
            (),
        )

    write_calendar_day(
        date(2026, 1, 2),
        current=100_100.0,
        realized=0.0,
        unrealized=100.0,
    )
    events = [
        {
            "event_id": "forward-close",
            "event_type": "paper_position_closed",
            "mode": "forward",
            "payload": {
                "mode": "forward",
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "net_pnl": 150.0,
                "fee": 1.0,
                "slippage": 2.0,
                "r_multiple": 0.3,
            },
            "run_id": "forward-run",
            "strategy_id": strategy_id,
            "symbol": "TST",
            "trade_date": "2026-01-05",
        },
        {
            "event_id": "conflicting-payload-mode",
            "event_type": "paper_position_closed",
            "mode": "forward",
            "payload": {
                "mode": "replay",
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "net_pnl": 9_999.0,
                "fee": 99.0,
                "slippage": 99.0,
                "r_multiple": 99.0,
            },
            "run_id": "contaminated-run",
            "strategy_id": strategy_id,
            "symbol": "TST",
            "trade_date": "2026-01-05",
        },
        {
            "event_id": "replay-close",
            "event_type": "paper_position_closed",
            "mode": "replay",
            "payload": {
                "mode": "replay",
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "net_pnl": 8_888.0,
                "fee": 88.0,
                "slippage": 88.0,
                "r_multiple": 88.0,
            },
            "run_id": "replay-run",
            "strategy_id": strategy_id,
            "symbol": "TST",
            "trade_date": "2026-01-05",
        },
        {
            "event_id": "forward-loss",
            "event_type": "paper_position_closed",
            "mode": "forward",
            "payload": {
                "mode": "forward",
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "net_pnl": -100.0,
                "fee": 1.0,
                "slippage": 2.0,
                "r_multiple": -0.2,
            },
            "run_id": "forward-run-2",
            "strategy_id": strategy_id,
            "symbol": "TST",
            "trade_date": "2026-01-06",
        },
    ]
    (paths.ledger / "paper_ledger.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n",
        encoding="utf-8",
    )
    write_calendar_day(
        date(2026, 1, 5),
        current=100_150.0,
        realized=150.0,
        unrealized=0.0,
    )
    write_calendar_day(
        date(2026, 1, 6),
        current=100_050.0,
        realized=50.0,
        unrealized=0.0,
    )

    rows = {
        str(row["date"]): row
        for row in paper_ops_engine._read_calendar_rows(paths)
        if row["mode"] == "forward" and row["strategy_id"] == strategy_id
    }
    assert float(rows["2026-01-02"]["daily_return_pct"]) == pytest.approx(0.001)
    assert float(rows["2026-01-05"]["total_pnl"]) == pytest.approx(50.0)
    assert float(rows["2026-01-05"]["daily_return_pct"]) == pytest.approx(
        50.0 / 100_100.0
    )
    assert float(rows["2026-01-05"]["realized_pnl"]) == pytest.approx(150.0)
    assert int(rows["2026-01-05"]["trades_closed"]) == 1
    assert float(rows["2026-01-05"]["fees_paid"]) == pytest.approx(1.0)
    assert float(rows["2026-01-06"]["daily_return_pct"]) == pytest.approx(
        -100.0 / 100_150.0,
        abs=1e-8,
    )
    assert float(rows["2026-01-06"]["cumulative_return_pct"]) == pytest.approx(0.0005)
    assert float(rows["2026-01-06"]["drawdown_pct"]) == pytest.approx(
        -100.0 / 100_150.0,
        abs=1e-8,
    )


def test_paperops_calendar_outputs_isolate_modes_and_compound_monthly_returns(
    tmp_path: Path,
) -> None:
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    rows: list[dict[str, object]] = [
        {
            "date": "2026-01-03",
            "mode": "forward",
            "strategy_id": "strategy-a",
            "starting_equity": 100.0,
            "ending_equity": 99.0,
            "daily_return_pct": -0.1,
            "cumulative_return_pct": -0.01,
            "drawdown_pct": -0.1,
        },
        {
            "date": "2026-01-02",
            "mode": "forward",
            "strategy_id": "strategy-a",
            "starting_equity": 100.0,
            "ending_equity": 110.0,
            "daily_return_pct": 0.1,
            "cumulative_return_pct": 0.1,
            "drawdown_pct": 0.0,
        },
        {
            "date": "2026-01-02",
            "mode": "replay",
            "strategy_id": "strategy-a",
            "starting_equity": 100.0,
            "ending_equity": 150.0,
            "daily_return_pct": 0.5,
            "cumulative_return_pct": 0.5,
            "drawdown_pct": 0.0,
        },
        {
            "date": "2026-01-02",
            "mode": "replay",
            "strategy_id": "strategy-b",
            "starting_equity": 100.0,
            "ending_equity": 120.0,
            "daily_return_pct": 0.2,
            "cumulative_return_pct": 0.2,
            "drawdown_pct": 0.0,
        },
    ]

    paper_ops_engine._write_calendar_matrix(paths, rows)
    paper_ops_engine._write_monthly_returns(paths, rows)
    paper_ops_engine._write_equity_and_drawdown(paths, rows)

    def csv_rows(filename: str) -> list[dict[str, str]]:
        with (paths.calendar / filename).open(encoding="utf-8", newline="") as handle:
            return list(csv.DictReader(handle))

    matrix = csv_rows("strategy_calendar_matrix.csv")
    forward_first = next(
        row for row in matrix if row["date"] == "2026-01-02" and row["mode"] == "forward"
    )
    replay_first = next(
        row for row in matrix if row["date"] == "2026-01-02" and row["mode"] == "replay"
    )
    assert float(forward_first["aggregate"]) == pytest.approx(0.1)
    assert forward_first["strategy-b"] == ""
    assert float(replay_first["aggregate"]) == pytest.approx(0.35)

    monthly = csv_rows("strategy_monthly_returns.csv")
    forward_month = next(
        row
        for row in monthly
        if row["mode"] == "forward" and row["strategy_id"] == "strategy-a"
    )
    replay_month = next(
        row
        for row in monthly
        if row["mode"] == "replay" and row["strategy_id"] == "strategy-a"
    )
    assert float(forward_month["monthly_return_pct"]) == pytest.approx(-0.01)
    assert float(forward_month["cumulative_return_pct"]) == pytest.approx(-0.01)
    assert float(replay_month["monthly_return_pct"]) == pytest.approx(0.5)

    drawdowns = csv_rows("strategy_drawdowns.csv")
    forward_drawdown = next(
        row
        for row in drawdowns
        if row["date"] == "2026-01-03"
        and row["mode"] == "forward"
        and row["strategy_id"] == "strategy-a"
    )
    replay_drawdown = next(
        row
        for row in drawdowns
        if row["date"] == "2026-01-02"
        and row["mode"] == "replay"
        and row["strategy_id"] == "strategy-a"
    )
    assert float(forward_drawdown["peak_equity"]) == pytest.approx(110.0)
    assert float(forward_drawdown["drawdown_pct"]) == pytest.approx(-0.1)
    assert float(replay_drawdown["peak_equity"]) == pytest.approx(150.0)
    assert float(replay_drawdown["drawdown_pct"]) == pytest.approx(0.0)


def test_paperops_reference_calendar_keeps_cash_and_equal_weight_benchmark_distinct(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    dataset = MarketDataset(
        dataset_id="benchmark-fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "AAA": (
                _bar("AAA", date(2026, 1, 2), 10, 10, 10, 10),
                _bar("AAA", date(2026, 1, 5), 11, 11, 11, 11),
            ),
            "BBB": (
                _bar("BBB", date(2026, 1, 2), 20, 20, 20, 20),
                _bar("BBB", date(2026, 1, 5), 18, 18, 18, 18),
            ),
        },
    )
    run = paper_ops_engine._paper_run(
        run_date=date(2026, 1, 5),
        mode=PaperRunMode.REPLAY,
        data_snapshot_id="benchmark-fixture",
    )

    rows = paper_ops_engine._reference_calendar_rows(
        [],
        run=run,
        manifest=_manifest(),
        dataset=dataset,
    )

    by_id = {row["strategy_id"]: row for row in rows}
    assert by_id["cash_no_trade_baseline"]["daily_return_pct"] == 0.0
    assert by_id["benchmark_buy_hold_equal_weight"]["daily_return_pct"] == pytest.approx(0.0)
    assert (
        by_id["benchmark_buy_hold_equal_weight"]["execution_policy_version"]
        == "equal_weight_close_to_close_v1"
    )


def test_paperops_operator_summaries_never_blend_modes_versions_or_references(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    rows = [
        {
            "date": "2026-01-05",
            "mode": "forward",
            "strategy_id": "strategy-a",
            "strategy_version": "v2.0",
            "strategy_status": "candidate",
            "execution_policy_version": "policy-forward-v2",
            "daily_return_pct": 0.01,
            "cumulative_return_pct": 0.01,
            "drawdown_pct": 0.0,
        },
        {
            "date": "2026-01-05",
            "mode": "forward",
            "strategy_id": "benchmark_buy_hold_equal_weight",
            "strategy_version": "v1.0",
            "strategy_status": "benchmark",
            "execution_policy_version": "benchmark-policy-v1",
            "daily_return_pct": 0.50,
            "cumulative_return_pct": 0.50,
            "drawdown_pct": 0.0,
        },
        {
            "date": "2026-01-02",
            "mode": "replay",
            "strategy_id": "strategy-a",
            "strategy_version": "v1.0",
            "strategy_status": "candidate",
            "execution_policy_version": "policy-replay-v1",
            "daily_return_pct": -0.02,
            "cumulative_return_pct": -0.02,
            "drawdown_pct": -0.02,
        },
    ]
    _write_canonical_calendar_rows(output_root, rows)

    paper_ops_engine.report(output_root=output_root)
    paper_ops_engine._write_calendar_summary(paths, rows)

    report_text = (paths.reports / "paper_ops_summary.md").read_text(encoding="utf-8")
    forward_report = report_text.split("## Forward observed evidence", 1)[1].split(
        "## Historical replay research", 1
    )[0]
    replay_report = report_text.split("## Historical replay research", 1)[1].split(
        "## Synthetic demo only", 1
    )[0]
    assert "`v2.0`" in forward_report
    assert "`policy-forward-v2`" in forward_report
    assert "`policy-replay-v1`" not in forward_report
    assert "`v1.0`" in replay_report
    assert "`policy-replay-v1`" in replay_report

    calendar_text = (paths.calendar / "calendar_summary.md").read_text(encoding="utf-8")
    forward_calendar = calendar_text.split("## Forward observed evidence", 1)[1].split(
        "## Historical replay research", 1
    )[0]
    assert "Best strategy on that row: `strategy-a@v2.0 [policy-forward-v2]`" in (
        forward_calendar
    )
    assert "benchmark_buy_hold_equal_weight" in forward_calendar


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


def test_paperops_reconciliation_flags_duplicate_logical_lifecycle(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    first = _event_row(
        "snapshot-a-event",
        "paper_order_created",
        {"mode": "replay", "order_id": "same-order", "strategy_id": "strategy"},
    )
    second = {**first, "event_id": "snapshot-b-event", "run_id": "refreshed-snapshot"}
    (paths.ledger / "paper_ledger.jsonl").write_text(
        json.dumps(first) + "\n" + json.dumps(second) + "\n",
        encoding="utf-8",
    )

    report = paper_ops_engine.reconcile(output_root=output_root)

    assert report["status"] == "failed"
    assert any(str(item).startswith("logical:replay|") for item in report["duplicate_event_ids"])


def test_forward_mode_blocks_same_day_before_regular_session_close(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_date = date(2026, 1, 5)
    paper_ops_engine._DATASET_CACHE.clear()
    monkeypatch.setattr(
        paper_ops_engine,
        "_current_utc_time",
        lambda: datetime(2026, 1, 5, 20, 59, tzinfo=timezone.utc),
    )

    def unexpected_build(**_kwargs: object) -> SimpleNamespace:
        raise AssertionError("pre-close guard must run before DataTruth fetch or cache use")

    monkeypatch.setattr(paper_ops_engine, "build_data_truth_snapshot", unexpected_build)

    with pytest.raises(ValueError, match="regular session is complete"):
        paper_ops_engine._load_dataset_for_mode(
            run_date=run_date,
            mode=PaperRunMode.FORWARD,
            allow_fetch=True,
        )


def test_paperops_datatruth_roots_follow_the_mutable_output_state(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "state" / "v2_paper_ops_live"
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)

    assert paper_ops_engine._data_truth_root_for_mode(
        paths,
        PaperRunMode.FORWARD,
    ) == output_root.parent / "v2_data_truth"
    assert paper_ops_engine._data_truth_root_for_mode(
        paths,
        PaperRunMode.REPLAY,
    ) == output_root / "data_truth_replay"


def test_forward_mode_after_close_includes_completed_run_date_bar(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    run_date = date(2026, 1, 5)
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    source_csv = tmp_path / "source.csv"
    write_ohlcv_csv(
        MarketDataset(
            dataset_id="forward-cutoff-fixture",
            source_kind="public_yahoo_chart",
            timeframe="1d",
            bars_by_symbol={
                "TST": (
                    _bar("TST", run_date, 10.0, 10.5, 9.8, 10.2),
                    _bar("TST", date(2026, 1, 6), 10.2, 10.6, 10.0, 10.4),
                )
            },
        ),
        source_csv,
    )
    snapshot_as_of_dates: list[date] = []

    def build_fixture_snapshot(**kwargs: object) -> object:
        snapshot_as_of = kwargs["as_of_date"]
        assert isinstance(snapshot_as_of, date)
        snapshot_as_of_dates.append(snapshot_as_of)
        return build_data_truth_snapshot(
            as_of_date=snapshot_as_of,
            output_root=tmp_path / "data_truth",
            created_at=NOW,
            source_csv=source_csv,
            raw_dir=raw_dir,
            allow_fetch=False,
        )

    paper_ops_engine._DATASET_CACHE.clear()
    monkeypatch.setattr(
        paper_ops_engine,
        "_current_utc_time",
        lambda: datetime(2026, 1, 5, 21, 1, tzinfo=timezone.utc),
    )
    monkeypatch.setattr(
        paper_ops_engine,
        "build_data_truth_snapshot",
        build_fixture_snapshot,
    )

    try:
        dataset, manifest, _warnings = paper_ops_engine._load_dataset_for_mode(
            run_date=run_date,
            mode=PaperRunMode.FORWARD,
            allow_fetch=False,
            universe_symbols=("TST",),
        )
    finally:
        paper_ops_engine._DATASET_CACHE.clear()

    assert snapshot_as_of_dates == [date(2026, 1, 6)]
    assert manifest.accepted_end == run_date.isoformat()
    assert [bar.timestamp.date() for bar in dataset.bars_by_symbol["TST"]] == [run_date]


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
    monkeypatch.setattr(
        paper_ops_engine,
        "_current_utc_time",
        lambda: datetime(2026, 1, 2, 21, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="rejects synthetic"):
        paper_ops_engine._load_dataset_for_mode(
            run_date=date(2026, 1, 2),
            mode=PaperRunMode.FORWARD,
            allow_fetch=False,
            universe_symbols=("TST",),
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
    monkeypatch.setattr(
        paper_ops_engine,
        "_current_utc_time",
        lambda: datetime(2026, 1, 2, 21, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="blocks DataTruth status mismatch"):
        paper_ops_engine._load_dataset_for_mode(
            run_date=date(2026, 1, 2),
            mode=PaperRunMode.FORWARD,
            allow_fetch=False,
            universe_symbols=("TST",),
        )


def test_forward_mode_rejects_historical_backfill(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        paper_ops_engine,
        "_current_utc_time",
        lambda: datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc),
    )

    with pytest.raises(ValueError, match="use replay for historical"):
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


def test_ledger_rebuild_uses_trade_date_order_not_jsonl_append_order(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    opened = _event_row(
        "opened",
        "paper_position_opened",
        {
            "mode": "replay",
            "position_id": "position-1",
            "order_id": "order-1",
            "strategy_id": strategy_id,
            "strategy_version": "v1.0",
            "symbol": "TST",
            "unrealized_pnl": 0.0,
        },
    )
    older_mark = _event_row(
        "older-mark",
        "paper_position_marked_to_market",
        {
            **opened["payload"],
            "unrealized_pnl": 10.0,
        },
    )
    newer_mark = _event_row(
        "newer-mark",
        "paper_position_marked_to_market",
        {
            **opened["payload"],
            "unrealized_pnl": 20.0,
        },
    )
    opened["trade_date"] = "2026-01-02"
    older_mark["trade_date"] = "2026-01-02"
    newer_mark["trade_date"] = "2026-01-03"
    (output_root / "ledger" / "paper_ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in (opened, newer_mark, older_mark)) + "\n",
        encoding="utf-8",
    )

    result = rebuild_ledger(output_root=output_root)

    assert result.open_positions[0]["unrealized_pnl"] == 20.0
    latest = next(
        row
        for row in result.calendar_rows
        if row["date"] == "2026-01-03"
        and row["mode"] == "replay"
        and row["strategy_id"] == strategy_id
    )
    assert latest["ending_equity"] == pytest.approx(100_020.0)
    assert latest["daily_return_pct"] == pytest.approx(10.0 / 100_010.0)


def test_ledger_rebuild_uses_lifecycle_order_within_same_date(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    position = {
        "mode": "replay",
        "position_id": "position-1",
        "order_id": "order-1",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "TST",
        "unrealized_pnl": 25.0,
    }
    mark = _event_row("mark", "paper_position_marked_to_market", position)
    opened = _event_row("opened", "paper_position_opened", {**position, "unrealized_pnl": 0.0})
    (output_root / "ledger" / "paper_ledger.jsonl").write_text(
        json.dumps(mark) + "\n" + json.dumps(opened) + "\n",
        encoding="utf-8",
    )

    result = rebuild_ledger(output_root=output_root)

    assert result.open_positions[0]["unrealized_pnl"] == 25.0
    rebuilt = next(
        row
        for row in result.calendar_rows
        if row["mode"] == "replay" and row["strategy_id"] == strategy_id
    )
    assert rebuilt["ending_equity"] == pytest.approx(100_025.0)


def test_ledger_rebuild_removes_check_time_blocked_order_from_pending(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy = _strategy_row(output_root)
    order = {
        "mode": "replay",
        "order_id": "blocked-order",
        "strategy_id": strategy["strategy_id"],
        "strategy_version": strategy["strategy_version"],
        "execution_policy_version": strategy["execution_policy_version"],
        "symbol": "TST",
    }
    blocked = {**order, "decision": "blocked", "reason": "gap_through_stop"}
    events = [
        _event_row("order-created", "paper_order_created", order),
        _event_row("order-blocked", "paper_order_blocked", blocked),
    ]
    (output_root / "ledger" / "paper_ledger.jsonl").write_text(
        "\n".join(json.dumps(row) for row in events) + "\n",
        encoding="utf-8",
    )

    result = rebuild_ledger(output_root=output_root)

    assert result.pending_orders == ()


def test_ledger_rebuild_preserves_archived_version_and_policy_series(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    strategy_id = str(_strategy_row(output_root)["strategy_id"])
    archived = {
        "mode": "replay",
        "strategy_id": strategy_id,
        "strategy_version": "v0.9",
        "execution_policy_version": "paperops-risk-v1",
        "symbol": "TST",
    }
    events = [
        _event_row(
            "archived-close",
            "paper_position_closed",
            {
                **archived,
                "position_id": "archived-position",
                "close_id": "archived-close",
                "net_pnl": 50.0,
                "fee": 1.0,
                "slippage": 1.0,
                "r_multiple": 0.5,
            },
        )
    ]
    (output_root / "ledger" / "paper_ledger.jsonl").write_text(
        json.dumps(events[0]) + "\n",
        encoding="utf-8",
    )

    result = rebuild_ledger(output_root=output_root)

    archived_account = next(
        row
        for row in result.account_rows
        if row["mode"] == "replay"
        and row["strategy_id"] == strategy_id
        and row["strategy_version"] == "v0.9"
        and row["execution_policy_version"] == "paperops-risk-v1"
    )
    archived_calendar = next(
        row
        for row in result.calendar_rows
        if row["mode"] == "replay"
        and row["strategy_id"] == strategy_id
        and row["strategy_version"] == "v0.9"
        and row["execution_policy_version"] == "paperops-risk-v1"
    )
    assert archived_account["realized_pnl"] == pytest.approx(50.0)
    assert archived_calendar["realized_pnl"] == pytest.approx(50.0)


def test_calendar_truth_detects_duplicate_rows(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    row = {
        "strategy_id": strategy["strategy_id"],
        "strategy_version": strategy["strategy_version"],
        "execution_policy_version": strategy["execution_policy_version"],
        "strategy_semantics_fingerprint": strategy["strategy_semantics_fingerprint"],
        "starting_equity": 100000,
        "ending_equity": 100000,
    }
    _write_canonical_calendar_rows(output_root, [row, row])

    result = verify_calendar_truth(output_root=output_root)

    assert result.status == "failed"
    assert result.duplicate_rows


def test_strategy_evidence_keeps_insufficient_forward_evidence_unvalidated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    strategy_id = str(strategy["strategy_id"])
    strategy_version = str(strategy["strategy_version"])
    execution_policy_version = str(strategy["execution_policy_version"])
    _write_canonical_calendar_rows(
        output_root,
        [
            {
                "date": "2026-01-02",
                "mode": "replay",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy["strategy_semantics_fingerprint"],
                "drawdown_pct": 0,
            },
            {
                "date": "2026-01-03",
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy["strategy_semantics_fingerprint"],
                "drawdown_pct": 0,
            },
        ],
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.strategy_evidence._forward_data_status",
        lambda *_args: "reconciled",
    )

    result = score_strategy_evidence(output_root=output_root)
    first = result.scores[0]

    assert first["evidence_status"] == "watch"
    assert "more forward paper days" in str(first["blockers"])


def test_strategy_evidence_never_uses_replay_drawdown_as_forward_drawdown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    strategy_id = str(strategy["strategy_id"])
    strategy_version = str(strategy["strategy_version"])
    execution_policy_version = str(strategy["execution_policy_version"])
    strategy_semantics_fingerprint = str(strategy["strategy_semantics_fingerprint"])
    _write_canonical_calendar_rows(
        output_root,
        [
            {
                "mode": "replay",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "drawdown_pct": -0.50,
            },
            {
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "drawdown_pct": 0,
            },
        ],
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.strategy_evidence._forward_data_status",
        lambda *_args: "reconciled",
    )

    result = score_strategy_evidence(output_root=output_root)
    first = next(row for row in result.scores if row["strategy_id"] == strategy_id)

    assert first["max_drawdown_pct"] == 0.0
    assert first["replay_max_drawdown_pct"] == -0.5


def test_strategy_evidence_quarantines_fragile_robustness(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    alpha_root = tmp_path / "v2_alpha_lab"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    strategy_id = str(strategy["strategy_id"])
    strategy_version = str(strategy["strategy_version"])
    execution_policy_version = str(strategy["execution_policy_version"])
    strategy_semantics_fingerprint = str(strategy["strategy_semantics_fingerprint"])
    _write_canonical_calendar_rows(
        output_root,
        [
            {
                "date": "2026-01-02",
                "mode": "replay",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "drawdown_pct": 0,
            },
            {
                "date": "2026-01-03",
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "execution_policy_version": execution_policy_version,
                "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
                "drawdown_pct": 0,
            },
        ],
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
                        "strategy_version": strategy_version,
                        "strategy_semantics_fingerprint": strategy_semantics_fingerprint,
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
        "intraday_scanner.v2.paper_ops.strategy_evidence._forward_data_status",
        lambda *_args: "reconciled",
    )

    result = score_strategy_evidence(output_root=output_root)
    row = next(item for item in result.scores if item["strategy_id"] == strategy_id)

    assert row["evidence_status"] == "quarantined"
    assert row["robustness_status"] == "fragile"
    assert row["robustness_test_return_pct"] == -0.04
    assert "Alpha Lab robustness status is fragile" in str(row["blockers"])


@pytest.mark.parametrize(
    ("observer", "command"),
    (
        (score_strategy_evidence, "evidence"),
        (forward_readiness, "readiness"),
    ),
)
def test_evidence_and_readiness_leave_complete_state_tree_unchanged(
    tmp_path: Path,
    observer,
    command: str,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    _write_canonical_calendar_rows(
        output_root,
        [
            {
                "strategy_id": strategy["strategy_id"],
                "strategy_version": strategy["strategy_version"],
                "execution_policy_version": strategy["execution_policy_version"],
                "strategy_semantics_fingerprint": strategy[
                    "strategy_semantics_fingerprint"
                ],
            }
        ],
    )
    state_root = output_root / "state"

    before_direct = _tree_bytes_and_directories(state_root)
    observer(output_root=output_root)
    assert _tree_bytes_and_directories(state_root) == before_direct

    before_cli = _tree_bytes_and_directories(state_root)
    paper_ops_cli.main([command, "--output-root", str(output_root)])
    assert _tree_bytes_and_directories(state_root) == before_cli


def test_strategy_governance_pause_is_exact_and_never_auto_promotes(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    strategy_row = _strategy_row(output_root)
    score = {
        "blockers": "negative after-cost expectancy",
        "current_series": True,
        "evidence_status": "quarantined",
        "execution_policy_version": strategy_row["execution_policy_version"],
        "forward_closed_trades": 10,
        "strategy_id": strategy_row["strategy_id"],
        "strategy_semantics_fingerprint": strategy_row[
            "strategy_semantics_fingerprint"
        ],
        "strategy_version": strategy_row["strategy_version"],
    }

    strategy_evidence_module._update_governance_overlay(paths, (score,))

    overlay = paper_ops_engine.read_json(
        paths.state / "strategy_governance_overlay.json",
        {},
    )
    entry = overlay["entries"][0]
    assert entry["allow_entries"] is False
    assert entry["strategy_semantics_fingerprint"] == strategy_row[
        "strategy_semantics_fingerprint"
    ]
    strategy = next(
        item
        for item in build_strategy_catalog()
        if item.strategy_id == strategy_row["strategy_id"]
    )
    assert paper_ops_engine._governance_block_reason(
        paths,
        strategy,
        paper_ops_engine._config(paths),
    )

    strategy_evidence_module._update_governance_overlay(
        paths,
        ({**score, "evidence_status": "validated", "forward_closed_trades": 30},),
    )
    retained = paper_ops_engine.read_json(
        paths.state / "strategy_governance_overlay.json",
        {},
    )
    assert retained["entries"][0]["allow_entries"] is False


def test_apply_evidence_governance_is_explicit_and_idempotent(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    _write_canonical_calendar_rows(
        output_root,
        [
            {
                "strategy_id": strategy["strategy_id"],
                "strategy_version": strategy["strategy_version"],
                "execution_policy_version": strategy["execution_policy_version"],
                "strategy_semantics_fingerprint": strategy[
                    "strategy_semantics_fingerprint"
                ],
            }
        ],
    )

    first = apply_evidence_governance(output_root=output_root)
    overlay = output_root / "state" / "strategy_governance_overlay.json"
    before = overlay.read_bytes()
    second = apply_evidence_governance(output_root=output_root)
    cli_status = paper_ops_cli.main(
        ["apply-evidence-governance", "--output-root", str(output_root)]
    )

    assert first["status"] == second["status"] == "applied"
    assert cli_status == 0
    assert overlay.read_bytes() == before


def test_apply_evidence_governance_recovers_valid_writer_journal(tmp_path: Path) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    _write_canonical_calendar_rows(
        output_root,
        [
            {
                "strategy_id": strategy["strategy_id"],
                "strategy_version": strategy["strategy_version"],
                "execution_policy_version": strategy["execution_policy_version"],
                "strategy_semantics_fingerprint": strategy[
                    "strategy_semantics_fingerprint"
                ],
            }
        ],
    )
    state_updates = {"state/pending_orders.json": []}
    journal = output_root / "state" / "paper_transaction_pending.json"
    paper_ops_engine.write_json(
        journal,
        {
            "events": [],
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": state_updates,
            "transaction_id": paper_ops_engine._paper_transaction_id([], state_updates),
        },
    )

    result = apply_evidence_governance(output_root=output_root)

    assert result["status"] == "applied"
    assert not journal.exists()
    assert paper_ops_engine.read_json(output_root / "state" / "pending_orders.json", None) == []
    assert (output_root / "state" / "strategy_governance_overlay.json").is_file()


def test_strategy_governance_fails_closed_on_malformed_or_stale_overlay(
    tmp_path: Path,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    paths = paper_ops_engine.PaperOpsPaths.create(output_root)
    strategy_row = _strategy_row(output_root)
    strategy = next(
        item
        for item in build_strategy_catalog()
        if item.strategy_id == strategy_row["strategy_id"]
    )
    config = paper_ops_engine._config(paths)
    overlay_path = paths.state / "strategy_governance_overlay.json"
    row = {
        "allow_entries": False,
        "execution_policy_version": strategy_row["execution_policy_version"],
        "strategy_id": strategy_row["strategy_id"],
        "strategy_semantics_fingerprint": strategy_row[
            "strategy_semantics_fingerprint"
        ],
        "strategy_version": strategy_row["strategy_version"],
    }
    paper_ops_engine.write_json(overlay_path, {"entries": [row]})
    with pytest.raises(ValueError, match="schema is unsupported"):
        paper_ops_engine._governance_block_reason(paths, strategy, config)

    paper_ops_engine.write_json(
        overlay_path,
        {
            "entries": [{**row, "allow_entries": "false"}],
            "schema_version": "v2.strategy_governance_overlay.v1",
        },
    )
    with pytest.raises(ValueError, match="allow_entries must be boolean"):
        paper_ops_engine._governance_block_reason(paths, strategy, config)

    paper_ops_engine.write_json(
        overlay_path,
        {
            "entries": [{**row, "strategy_semantics_fingerprint": "0" * 64}],
            "schema_version": "v2.strategy_governance_overlay.v1",
        },
    )
    with pytest.raises(ValueError, match="fingerprint conflicts"):
        paper_ops_engine._governance_block_reason(paths, strategy, config)


def test_strategy_evidence_rejects_stale_semantics_overlays() -> None:
    current_fingerprint = "a" * 64
    stale_fingerprint = "b" * 64
    evidence = {
        "strategy_version": "1.0.0",
        "execution_policy_version": "paper-policy-v4",
        "strategy_semantics_fingerprint": stale_fingerprint,
        "status": "validated",
    }
    robustness = {
        "strategy_version": "1.0.0",
        "strategy_semantics_fingerprint": stale_fingerprint,
        "robustness_status": "passed",
    }

    assert (
        strategy_evidence_module._exact_overlay(
            evidence,
            "1.0.0",
            "paper-policy-v4",
            current_fingerprint,
        )
        == {}
    )
    assert (
        strategy_evidence_module._versioned_robustness(
            robustness,
            "1.0.0",
            current_fingerprint,
        )
        == {}
    )


def test_forward_readiness_blocks_on_rebuild_or_calendar_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    strategy = _strategy_row(output_root)
    _write_canonical_calendar_rows(
        output_root,
        [
            {
                "strategy_id": strategy["strategy_id"],
                "strategy_version": strategy["strategy_version"],
                "execution_policy_version": strategy["execution_policy_version"],
                "strategy_semantics_fingerprint": strategy[
                    "strategy_semantics_fingerprint"
                ],
                "drawdown_pct": 0,
            }
        ],
    )
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.readiness._data_status",
        lambda _root: "single_provider_unreconciled",
    )

    result = forward_readiness(output_root=output_root)

    assert result.status in {"blocked", "ready_with_warnings"}
    assert any("single-provider" in warning for warning in result.warnings)


def test_forward_readiness_blocks_unknown_or_future_data_truth() -> None:
    for status in ("unknown", "", "future_unreviewed_status", "provider_error"):
        assert readiness_module._hard_block(status, "passed", "passed") is True
    for status in (
        "reconciled",
        "reconciled_with_minor_diffs",
        "single_provider_unreconciled",
    ):
        assert readiness_module._hard_block(status, "passed", "passed") is False


def test_blocked_strategy_evidence_blocks_readiness_and_preserves_warnings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    _seed_observer_ledger(output_root)
    _write_canonical_calendar_rows(output_root, [{}])
    monkeypatch.setattr(readiness_module, "_data_status", lambda _root: "reconciled")
    monkeypatch.setattr(
        readiness_module,
        "rebuild_ledger",
        lambda **_kwargs: SimpleNamespace(status="passed"),
    )
    monkeypatch.setattr(
        readiness_module,
        "verify_calendar_truth",
        lambda **_kwargs: SimpleNamespace(status="passed", warnings=()),
    )
    monkeypatch.setattr(
        readiness_module,
        "score_strategy_evidence",
        lambda **_kwargs: SimpleNamespace(
            status="blocked", scores=(), warnings=("attestation missing",)
        ),
    )

    result = forward_readiness(output_root=output_root)

    assert result.status == "blocked"
    assert result.strategy_evidence_status == "blocked"
    assert "attestation missing" in result.warnings
    assert paper_ops_cli._result_exit_code("readiness", result.to_dict()) == 2


def test_blocked_evidence_governance_keeps_existing_overlay_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    output_root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=output_root)
    overlay = output_root / "state" / "strategy_governance_overlay.json"
    overlay.write_bytes(b'{"retained":true}\n')
    monkeypatch.setattr(
        governance_module,
        "score_strategy_evidence",
        lambda **_kwargs: SimpleNamespace(
            status="blocked", scores=(), warnings=("attestation missing",)
        ),
    )

    result = apply_evidence_governance(output_root=output_root)

    assert result == {
        "status": "blocked",
        "score_count": 0,
        "warnings": ["attestation missing"],
    }
    assert overlay.read_bytes() == b'{"retained":true}\n'
    assert paper_ops_cli._result_exit_code("apply-evidence-governance", result) == 2


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
