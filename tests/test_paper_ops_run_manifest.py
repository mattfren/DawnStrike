from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pytest

from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.data_truth.models import DataTruthManifest
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.models import (
    PAPER_EXECUTION_POLICY_VERSION,
    PaperOpsConfig,
    PaperRunMode,
)
from intraday_scanner.v2.paper_ops.storage import read_json, write_json

RUN_DATE = date(2026, 7, 14)
SNAPSHOT_ID = "snapshot-manifest-v3-test"


class _StopAfterManifest(RuntimeError):
    pass


def _config() -> PaperOpsConfig:
    return PaperOpsConfig(
        execution_policy_version=PAPER_EXECUTION_POLICY_VERSION,
        universe_id="manifest-test-universe-v1",
        universe_symbols=("AAA", "BBB"),
    )


def _manifest(snapshot_id: str = SNAPSHOT_ID) -> DataTruthManifest:
    return DataTruthManifest(
        snapshot_id=snapshot_id,
        created_at="2026-07-15T00:00:00+00:00",
        provider_id="fixture",
        provider_name="Manifest Fixture",
        symbols=("AAA", "BBB"),
        timeframe="1d",
        requested_start="2026-07-01",
        requested_end=RUN_DATE.isoformat(),
        accepted_start="2026-07-01",
        accepted_end=RUN_DATE.isoformat(),
        bar_count=20,
        accepted_bar_count=20,
        rejected_bar_count=0,
        skipped_incomplete_bars=0,
        validation_status="passed",
        warnings=("fixture warning bound to the run",),
        raw_artifact_hashes={"fixture.csv": "raw-hash"},
        normalized_artifact_hash="normalized-hash",
        source_url_or_reference=("fixture://manifest-v3",),
        normalized_artifact_path="snapshots/fixture/normalized.csv",
        snapshot_content_hash="snapshot-content-hash",
        manifest_payload_hash="data-manifest-payload-hash",
    )


def _dataset() -> MarketDataset:
    return MarketDataset(
        dataset_id=SNAPSHOT_ID,
        source_kind="fixture",
        timeframe="1d",
        bars_by_symbol={"AAA": (), "BBB": ()},
    )


def _manifest_hash(payload: dict[str, object]) -> str:
    hashed = dict(payload)
    hashed.pop("manifest_payload_hash", None)
    return hashlib.sha256(
        json.dumps(hashed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def test_manifest_v3_binds_snapshot_policy_and_no_trade_universe_idempotently(
    tmp_path: Path,
) -> None:
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    config = _config()
    data_manifest = _manifest()
    run = paper_ops_engine._paper_run(
        run_date=RUN_DATE,
        mode=PaperRunMode.REPLAY,
        data_snapshot_id=data_manifest.snapshot_id,
    )
    data_truth_root = tmp_path / "paper_ops" / "replay_data_truth"

    first = paper_ops_engine._ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=data_manifest,
        data_truth_root=data_truth_root,
    )
    manifest_path = paths.manifests / (
        f"{paper_ops_engine._safe_filename(run.run_id)}.json"
    )
    first_bytes = manifest_path.read_bytes()
    second = paper_ops_engine._ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=data_manifest,
        data_truth_root=data_truth_root,
    )

    assert second == first
    assert manifest_path.read_bytes() == first_bytes
    assert first["schema_version"] == "v2.paper_ops_manifest.v3"
    assert first["execution_policy_version"] == config.execution_policy_version
    assert first["execution_policy_fingerprint"] == (
        paper_ops_engine._execution_policy_fingerprint(config)
    )
    assert first["universe_id"] == config.universe_id
    assert first["universe_symbols"] == list(config.universe_symbols)
    assert first["output_artifacts"] == []
    assert first["data_snapshot_content_hash"] == data_manifest.snapshot_content_hash
    assert first["data_snapshot_manifest_payload_hash"] == (
        data_manifest.manifest_payload_hash
    )
    assert first["data_snapshot_normalized_hash"] == (
        data_manifest.normalized_artifact_hash
    )
    assert first["manifest_payload_hash"] == _manifest_hash(first)


def test_manifest_v3_rejects_self_consistent_same_run_conflict(tmp_path: Path) -> None:
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    config = _config()
    data_manifest = _manifest()
    run = paper_ops_engine._paper_run(
        run_date=RUN_DATE,
        mode=PaperRunMode.REPLAY,
        data_snapshot_id=data_manifest.snapshot_id,
    )
    paper_ops_engine._ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=data_manifest,
        data_truth_root=tmp_path / "paper_ops" / "replay_data_truth",
    )
    manifest_path = paths.manifests / (
        f"{paper_ops_engine._safe_filename(run.run_id)}.json"
    )
    conflicting = read_json(manifest_path, {})
    assert isinstance(conflicting, dict)
    conflicting["universe_symbols"] = ["AAA", "CCC"]
    conflicting["manifest_payload_hash"] = _manifest_hash(conflicting)
    write_json(manifest_path, conflicting)
    conflicting_bytes = manifest_path.read_bytes()

    with pytest.raises(ValueError, match="immutable same-run binding"):
        paper_ops_engine._ensure_run_manifest(
            paths,
            run,
            config=config,
            data_manifest=data_manifest,
            data_truth_root=tmp_path / "paper_ops" / "replay_data_truth",
        )

    assert manifest_path.read_bytes() == conflicting_bytes


def test_manifest_v3_rejects_run_snapshot_mismatch_before_persisting(
    tmp_path: Path,
) -> None:
    paths = paper_ops_engine.PaperOpsPaths.create(tmp_path / "paper_ops")
    run = paper_ops_engine._paper_run(
        run_date=RUN_DATE,
        mode=PaperRunMode.REPLAY,
        data_snapshot_id="different-snapshot",
    )

    with pytest.raises(ValueError, match="snapshot does not match"):
        paper_ops_engine._ensure_run_manifest(
            paths,
            run,
            config=_config(),
            data_manifest=_manifest(),
            data_truth_root=tmp_path / "paper_ops" / "replay_data_truth",
        )

    assert list(paths.manifests.glob("*.json")) == []
    assert not (paths.state / "execution_policy_manifest.json").exists()


def test_all_direct_phases_require_one_manifest_before_ledger_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "paper_ops"
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    config = _config()
    write_json(paths.state / "paper_ops_config.json", config.to_dict())
    data_manifest = _manifest()
    dataset = _dataset()
    actual_ensure = paper_ops_engine._ensure_run_manifest
    gated_run_ids: list[str] = []

    def fake_loader(**_kwargs: object) -> tuple[
        MarketDataset,
        DataTruthManifest,
        tuple[str, ...],
    ]:
        return dataset, data_manifest, data_manifest.warnings

    def persist_then_stop(
        phase_paths: paper_ops_engine.PaperOpsPaths,
        run: object,
        **kwargs: object,
    ) -> dict[str, object]:
        result = actual_ensure(phase_paths, run, **kwargs)  # type: ignore[arg-type]
        gated_run_ids.append(str(result["run_id"]))
        raise _StopAfterManifest

    monkeypatch.setattr(paper_ops_engine, "_load_dataset_for_mode", fake_loader)
    monkeypatch.setattr(paper_ops_engine, "_ensure_run_manifest", persist_then_stop)

    manifest_bytes: bytes | None = None
    for phase in (
        paper_ops_engine.scan,
        paper_ops_engine.enter,
        paper_ops_engine.check,
        paper_ops_engine.close,
    ):
        with pytest.raises(_StopAfterManifest):
            phase(
                run_date=RUN_DATE,
                mode=PaperRunMode.REPLAY,
                output_root=root,
                allow_fetch=False,
            )
        manifest_paths = list(paths.manifests.glob("*.json"))
        assert len(manifest_paths) == 1
        if manifest_bytes is None:
            manifest_bytes = manifest_paths[0].read_bytes()
        else:
            assert manifest_paths[0].read_bytes() == manifest_bytes
        assert not (paths.ledger / "paper_ledger.jsonl").exists()

    assert len(gated_run_ids) == 4
    assert len(set(gated_run_ids)) == 1
    payload = read_json(next(paths.manifests.glob("*.json")), {})
    assert isinstance(payload, dict)
    assert payload["schema_version"] == "v2.paper_ops_manifest.v3"
    assert payload["universe_symbols"] == list(config.universe_symbols)
