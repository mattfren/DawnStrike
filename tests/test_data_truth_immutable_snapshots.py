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
    build_data_truth_snapshot,
    load_datatruth_dataset,
    load_datatruth_snapshot,
    verify_datatruth_snapshot,
)


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
