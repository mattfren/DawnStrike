from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, datetime, timezone
from pathlib import Path

import pytest

import intraday_scanner.v2.data_truth.core as datatruth_core
from intraday_scanner.v2.data import MarketBar, MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data_truth import (
    DataTruthAcquisitionIncomplete,
    build_data_truth_snapshot,
    load_datatruth_dataset,
    load_datatruth_snapshot,
    verify_datatruth_snapshot,
)


def test_production_cache_rejects_mutable_csv_even_with_matching_contract(
    tmp_path: Path,
) -> None:
    source_csv = tmp_path / "mutable_source.csv"
    source_csv.write_text(
        "symbol,timestamp,open,high,low,close,volume\n"
        "TST,2026-01-02T00:00:00+00:00,10,11,9,10.5,100\n",
        encoding="utf-8",
    )
    source_csv.with_name(f"{source_csv.name}.contract.json").write_bytes(
        datatruth_core._json_bytes(datatruth_core._production_request_contract())
    )

    with pytest.raises(
        DataTruthAcquisitionIncomplete,
        match="full-digest content-addressed artifact",
    ):
        datatruth_core._source_refs_from_cache(
            source_csv,
            tmp_path / "raw",
            required_symbols=("TST",),
            required_bar_date=date(2026, 1, 2),
            minimum_history_bars=1,
            require_production=True,
        )


def test_capture_reapplies_bounded_reader_at_filesystem_boundary(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_csv = tmp_path / "source.csv"
    source_csv.write_text(
        "symbol,timestamp,open,high,low,close,volume\n"
        "TST,2026-01-02T00:00:00+00:00,10,11,9,10.5,100\n",
        encoding="utf-8",
    )
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    observed: list[Path] = []

    def bounded(path: Path) -> bytes:
        observed.append(path)
        with path.open("rb") as handle:
            return handle.read()

    monkeypatch.setattr(datatruth_core, "_bounded_source_bytes", bounded)

    artifacts = datatruth_core._capture_source_artifacts(
        source_csv=source_csv,
        raw_dir=raw_dir,
    )

    assert observed == [source_csv]
    assert artifacts[0].content == source_csv.read_bytes()


def test_production_snapshot_retains_request_contract_after_cache_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_csv, raw_dir = _production_source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"
    monkeypatch.setattr(datatruth_core, "_governed_minimum_history_bars", lambda: 1)

    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
        symbols=("TST",),
        require_production=True,
    )

    assert result.manifest.production_required is True
    assert result.manifest.request_contract == datatruth_core._production_request_contract()
    assert result.manifest.request_contract_artifact_path
    retained_contract = output_root / result.manifest.request_contract_artifact_path
    assert retained_contract.is_file()
    assert _sha256(retained_contract) == result.manifest.request_contract_artifact_hash

    for cache_artifact in raw_dir.iterdir():
        if cache_artifact.name.endswith(".contract.json"):
            if cache_artifact.name.endswith(".csv.contract.json"):
                cache_artifact.unlink()
            else:
                cache_artifact.write_text("{}", encoding="utf-8")
    verify_datatruth_snapshot(result.manifest.snapshot_id, output_root)

    retained_contract.write_text("{}", encoding="utf-8")
    with pytest.raises(ValueError, match="request contract bytes are not canonical"):
        verify_datatruth_snapshot(result.manifest.snapshot_id, output_root)

    retained_contract.write_bytes(datatruth_core._json_bytes(result.manifest.request_contract))
    manifest_path = output_root / result.manifest.snapshot_relative_path / "manifest.json"
    manifest_payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    fake_contract = datatruth_core._json_bytes({})
    fake_hash = hashlib.sha256(fake_contract).hexdigest()
    manifest_payload["request_contract"] = {}
    manifest_payload["request_contract_hash"] = fake_hash
    manifest_payload["request_contract_artifact_hash"] = fake_hash
    manifest_payload["manifest_payload_hash"] = datatruth_core._manifest_payload_hash(
        manifest_payload
    )
    manifest_path.write_bytes(datatruth_core._json_bytes(manifest_payload))
    with pytest.raises(ValueError, match="request contract is incomplete or noncanonical"):
        verify_datatruth_snapshot(result.manifest.snapshot_id, output_root)


def test_build_retains_content_bound_snapshot_and_reuses_identical_bytes(
    tmp_path: Path,
) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"

    first = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        created_at=datetime(2026, 1, 5, 22, tzinfo=timezone.utc),
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    second = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        created_at=datetime(2026, 1, 6, 22, tzinfo=timezone.utc),
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )

    manifest = first.manifest
    assert second.manifest == manifest
    assert manifest.schema_version == "v2.data_truth_manifest.v2"
    assert manifest.artifact_schema_version == "v2.data_truth_snapshot_artifacts.v1"
    assert manifest.snapshot_content_hash
    assert manifest.manifest_payload_hash
    assert manifest.snapshot_relative_path == f"snapshots/{manifest.snapshot_id}"
    assert manifest.normalized_artifact_path
    normalized_path = output_root / manifest.normalized_artifact_path
    assert normalized_path.is_file()
    assert _sha256(normalized_path) == manifest.normalized_artifact_hash
    assert set(manifest.raw_artifact_paths) == set(manifest.raw_artifact_hashes)
    assert (output_root / manifest.snapshot_relative_path / "manifest.json").is_file()
    assert Path(first.dataset.source_path or "").resolve() == normalized_path.resolve()
    assert "latest_ohlcv.csv" not in str(first.dataset.source_path)
    for relative_path in manifest.raw_artifact_paths:
        artifact_path = output_root / relative_path
        assert artifact_path.is_file()
        assert _sha256(artifact_path) == manifest.raw_artifact_hashes[relative_path]
    assert (output_root / manifest.raw_artifact_paths[0]).read_bytes() == source_csv.read_bytes()

    loaded, loaded_manifest = load_datatruth_snapshot(manifest.snapshot_id, output_root)
    assert loaded_manifest == manifest
    assert loaded.bars_by_symbol == first.dataset.bars_by_symbol
    assert Path(loaded.source_path or "").resolve() == normalized_path.resolve()


def test_production_datatruth_requires_nonempty_explicit_universe(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="nonempty explicit requested universe"):
        build_data_truth_snapshot(
            as_of_date=date(2026, 1, 5),
            output_root=tmp_path / "data_truth",
            allow_fetch=False,
            require_production=True,
        )


def test_raw_artifact_change_creates_new_snapshot_without_overwriting_prior(
    tmp_path: Path,
) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"
    first = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    first_raw_paths = {
        path: (output_root / path).read_bytes() for path in first.manifest.raw_artifact_paths
    }

    (raw_dir / "tst_chart.json").write_text(
        json.dumps({"chart": {"result": ["changed"]}}),
        encoding="utf-8",
    )
    second = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )

    assert second.manifest.snapshot_id != first.manifest.snapshot_id
    assert second.manifest.normalized_artifact_hash == first.manifest.normalized_artifact_hash
    assert second.manifest.snapshot_content_hash != first.manifest.snapshot_content_hash
    for relative_path, content in first_raw_paths.items():
        assert (output_root / relative_path).read_bytes() == content
    verify_datatruth_snapshot(first.manifest.snapshot_id, output_root)
    verify_datatruth_snapshot(second.manifest.snapshot_id, output_root)


def test_named_loader_and_rerun_fail_on_retained_normalized_drift(tmp_path: Path) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"
    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    assert result.manifest.normalized_artifact_path
    normalized_path = output_root / result.manifest.normalized_artifact_path
    normalized_path.write_bytes(normalized_path.read_bytes() + b"tampered\n")

    with pytest.raises(ValueError, match="normalized artifact hash mismatch"):
        load_datatruth_snapshot(result.manifest.snapshot_id, output_root)
    with pytest.raises(ValueError, match="immutable artifact conflict"):
        build_data_truth_snapshot(
            as_of_date=date(2026, 1, 5),
            output_root=output_root,
            source_csv=source_csv,
            raw_dir=raw_dir,
            allow_fetch=False,
        )


def test_named_verification_rejects_raw_manifest_and_missing_artifact_drift(
    tmp_path: Path,
) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"
    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    raw_path = output_root / result.manifest.raw_artifact_paths[-1]
    original_raw = raw_path.read_bytes()
    raw_path.write_bytes(original_raw + b"tampered")
    with pytest.raises(ValueError, match="source artifact hash mismatch"):
        verify_datatruth_snapshot(result.manifest.snapshot_id, output_root)
    raw_path.write_bytes(original_raw)

    snapshot_manifest_path = (
        output_root / str(result.manifest.snapshot_relative_path) / "manifest.json"
    )
    manifest_payload = json.loads(snapshot_manifest_path.read_text(encoding="utf-8"))
    manifest_payload["provider_name"] = "tampered"
    snapshot_manifest_path.write_text(json.dumps(manifest_payload), encoding="utf-8")
    with pytest.raises(ValueError, match="manifest payload hash mismatch"):
        verify_datatruth_snapshot(result.manifest.snapshot_id, output_root)

    with pytest.raises(FileNotFoundError, match="snapshot manifest is missing"):
        load_datatruth_snapshot("missing-snapshot", output_root)


def test_latest_alias_is_mutable_but_never_named_snapshot_authority(tmp_path: Path) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"
    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    latest_alias = output_root / "normalized" / "latest_ohlcv.csv"
    latest_alias.write_text("mutable alias drift\n", encoding="utf-8")

    named, _manifest = load_datatruth_snapshot(result.manifest.snapshot_id, output_root)
    latest_resolved, latest_manifest = load_datatruth_dataset(output_root=output_root)
    assert latest_manifest.snapshot_id == result.manifest.snapshot_id
    assert latest_resolved.bars_by_symbol == named.bars_by_symbol
    assert Path(latest_resolved.source_path or "").resolve() == Path(
        named.source_path or ""
    ).resolve()
    assert "latest_ohlcv.csv" not in str(latest_resolved.source_path)


def test_new_snapshot_is_staged_before_atomic_directory_publish(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"
    original_write = datatruth_core._write_new_file
    call_count = 0

    def fail_during_staging(path: Path, content: bytes) -> None:
        nonlocal call_count
        call_count += 1
        original_write(path, content)
        if call_count == 1:
            raise RuntimeError("injected staging failure")

    monkeypatch.setattr(datatruth_core, "_write_new_file", fail_during_staging)
    with pytest.raises(RuntimeError, match="injected staging failure"):
        build_data_truth_snapshot(
            as_of_date=date(2026, 1, 5),
            output_root=output_root,
            source_csv=source_csv,
            raw_dir=raw_dir,
            allow_fetch=False,
        )

    assert list((output_root / "snapshots").iterdir()) == []
    assert not (output_root / "manifests" / "latest.json").exists()
    assert not (output_root / "normalized" / "latest_ohlcv.csv").exists()


def test_rerun_recovers_a_byte_consistent_partial_snapshot_directory(tmp_path: Path) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    source_root = tmp_path / "source_data_truth"
    target_root = tmp_path / "target_data_truth"
    created_at = datetime(2026, 1, 5, 22, tzinfo=timezone.utc)
    source = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=source_root,
        created_at=created_at,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    assert source.manifest.normalized_artifact_path
    partial_normalized = target_root / source.manifest.normalized_artifact_path
    partial_normalized.parent.mkdir(parents=True)
    partial_normalized.write_bytes(
        (source_root / source.manifest.normalized_artifact_path).read_bytes()
    )

    recovered = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=target_root,
        created_at=created_at,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )

    assert recovered.manifest.snapshot_id == source.manifest.snapshot_id
    verify_datatruth_snapshot(recovered.manifest.snapshot_id, target_root)
    snapshot_root = target_root / str(recovered.manifest.snapshot_relative_path)
    retained_files = {
        path.relative_to(snapshot_root).as_posix()
        for path in snapshot_root.rglob("*")
        if path.is_file()
    }
    assert retained_files == {
        "manifest.json",
        "normalized/ohlcv.csv",
        "raw/tst_chart.json",
        "source/source.csv",
    }


@pytest.mark.parametrize(
    ("manifest_override", "message"),
    [
        ({"symbols": ("OTHER",)}, "symbols"),
        ({"accepted_bar_count": 999}, "bar count"),
        ({"accepted_start": "2025-01-01"}, "accepted range"),
        ({"accepted_end": "2026-01-03"}, "accepted range"),
        ({"timeframe": "5m"}, "timeframe"),
    ],
)
def test_retained_dataset_must_match_manifest_metadata(
    tmp_path: Path,
    manifest_override: dict[str, object],
    message: str,
) -> None:
    source_csv, raw_dir = _source_fixture(tmp_path)
    output_root = tmp_path / "data_truth"
    result = build_data_truth_snapshot(
        as_of_date=date(2026, 1, 5),
        output_root=output_root,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    assert result.manifest.normalized_artifact_path
    normalized_path = output_root / result.manifest.normalized_artifact_path
    conflicting_manifest = replace(result.manifest, **manifest_override)

    with pytest.raises(ValueError, match=message):
        datatruth_core._load_manifest_dataset(normalized_path, conflicting_manifest)


def _source_fixture(tmp_path: Path) -> tuple[Path, Path]:
    raw_dir = tmp_path / "provider_raw"
    raw_dir.mkdir()
    (raw_dir / "tst_chart.json").write_text(
        json.dumps({"chart": {"result": ["original"]}}),
        encoding="utf-8",
    )
    source_csv = tmp_path / "source.csv"
    dataset = MarketDataset(
        dataset_id="fixture",
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={
            "TST": (
                _bar(date(2026, 1, 1), 10.0, 11.0, 9.5, 10.5),
                _bar(date(2026, 1, 2), 10.5, 11.5, 10.0, 11.0),
                _bar(date(2026, 1, 5), 11.0, 12.0, 10.5, 11.5),
            )
        },
    )
    write_ohlcv_csv(dataset, source_csv)
    return source_csv, raw_dir


def _production_source_fixture(tmp_path: Path) -> tuple[Path, Path]:
    """Build a minimal, fully content-addressed Yahoo-shaped production cache."""

    from intraday_scanner.v2.data.yahoo_chart import _bars_from_payload

    raw_dir = tmp_path / "production_cache"
    raw_dir.mkdir()
    bars = (
        _bar(date(2026, 1, 1), 10.0, 11.0, 9.5, 10.5),
        _bar(date(2026, 1, 2), 10.5, 11.5, 10.0, 11.0),
    )
    dataset = MarketDataset(
        dataset_id="production-fixture",
        source_kind="public_yahoo_chart",
        timeframe="1d",
        bars_by_symbol={"TST": bars},
    )
    source_staging = tmp_path / "source_staging.csv"
    write_ohlcv_csv(dataset, source_staging)
    source_content = source_staging.read_bytes()
    source_csv = raw_dir / (
        "public_yahoo_ohlcv_" + hashlib.sha256(source_content).hexdigest() + ".csv"
    )
    source_csv.write_bytes(source_content)

    payload = {
        "chart": {
            "result": [
                {
                    "meta": {"symbol": "TST"},
                    "timestamp": [int(bar.timestamp.timestamp()) for bar in bars],
                    "indicators": {
                        "quote": [
                            {
                                "open": [bar.open for bar in bars],
                                "high": [bar.high for bar in bars],
                                "low": [bar.low for bar in bars],
                                "close": [bar.close for bar in bars],
                                "volume": [bar.volume for bar in bars],
                            }
                        ]
                    },
                }
            ]
        }
    }
    raw_content = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    raw_path = raw_dir / (
        "tst_chart_" + hashlib.sha256(raw_content).hexdigest() + ".json"
    )
    raw_path.write_bytes(raw_content)
    contract_bytes = datatruth_core._json_bytes(datatruth_core._production_request_contract())
    source_csv.with_name(f"{source_csv.name}.contract.json").write_bytes(contract_bytes)
    raw_path.with_name(f"{raw_path.name}.contract.json").write_bytes(contract_bytes)
    parsed, warnings = _bars_from_payload("TST", payload)
    assert not warnings
    assert tuple(parsed) == bars
    return source_csv, raw_dir


def _bar(
    session_date: date,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> MarketBar:
    return MarketBar(
        symbol="TST",
        timestamp=datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
