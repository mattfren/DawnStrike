"""DataTruth v1 snapshot building and reconciliation."""

from __future__ import annotations

import csv
import hashlib
import json
import math
import os
import re
import shutil
import tempfile
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from intraday_scanner.market_calendar import market_session
from intraday_scanner.v2.data import (
    MarketBar,
    MarketDataset,
    load_ohlcv_csv,
    validate_dataset,
    write_ohlcv_csv,
)
from intraday_scanner.v2.data.market import MAX_MARKET_CSV_BYTES, MAX_MARKET_VOLUME
from intraday_scanner.v2.data_truth.models import (
    DataTruthManifest,
    DataTruthReconciliationReport,
    ProviderDisagreement,
)

SNAPSHOT_ARTIFACT_SCHEMA_VERSION = "v2.data_truth_snapshot_artifacts.v1"
SNAPSHOT_IDENTITY_SCHEMA_VERSION = "v2.data_truth_snapshot_identity.v1"
DATA_TRUTH_MANIFEST_SCHEMA_VERSION = "v2.data_truth_manifest.v2"
DATA_TRUTH_NORMALIZED_TIMEFRAME = "1d"
PUBLIC_YAHOO_REQUEST_CONTRACT_SCHEMA = "v2.public_yahoo_chart_request.v1"


def _production_request_contract() -> dict[str, object]:
    """Return the immutable Yahoo request envelope required for production."""

    return {
        "events": "history",
        "includePrePost": False,
        "interval": "1d",
        "range": "2y",
        "schema_version": PUBLIC_YAHOO_REQUEST_CONTRACT_SCHEMA,
    }


class DataTruthAcquisitionIncomplete(RuntimeError):
    """Exact-universe acquisition did not produce a complete source set."""


@dataclass(frozen=True)
class DataTruthPaths:
    root: Path
    snapshots: Path
    raw: Path
    imports: Path
    normalized: Path
    manifests: Path
    reconciliation: Path
    reports: Path
    cache: Path

    @classmethod
    def create(cls, root: Path) -> DataTruthPaths:
        paths = cls(
            root=root,
            snapshots=root / "snapshots",
            raw=root / "raw",
            imports=root / "imports",
            normalized=root / "normalized",
            manifests=root / "manifests",
            reconciliation=root / "reconciliation",
            reports=root / "reports",
            cache=root / "cache",
        )
        for path in (
            paths.root,
            paths.snapshots,
            paths.raw,
            paths.imports,
            paths.normalized,
            paths.manifests,
            paths.reconciliation,
            paths.reports,
            paths.cache,
        ):
            path.mkdir(parents=True, exist_ok=True)
        return paths


@dataclass(frozen=True)
class DataTruthBuildResult:
    dataset: MarketDataset
    manifest: DataTruthManifest
    reconciliation: DataTruthReconciliationReport
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class _CapturedArtifact:
    logical_path: str
    source_path: Path
    content: bytes
    sha256: str


def build_data_truth_snapshot(
    *,
    as_of_date: date,
    output_root: Path = Path("data/v2_data_truth"),
    created_at: datetime | None = None,
    source_csv: Path | None = None,
    raw_dir: Path | None = None,
    allow_fetch: bool = True,
    symbols: tuple[str, ...] | None = None,
    fetch_max_workers: int = 12,
    fetch_max_requests_per_second: float | None = 8.0,
    fetch_time_budget_seconds: float | None = 90 * 60,
    require_production: bool = False,
) -> DataTruthBuildResult:
    if symbols is not None:
        from intraday_scanner.public_data.yahoo_chart_fetcher import (
            canonicalize_yahoo_symbols,
        )

        symbols = canonicalize_yahoo_symbols(symbols)
    if require_production and not symbols:
        raise ValueError(
            "production DataTruth requires a nonempty explicit requested universe"
        )
    required_bar_date = (
        _last_completed_market_session(as_of_date) if symbols is not None else None
    )
    minimum_history = _governed_minimum_history_bars() if symbols is not None else 0
    now = created_at or datetime.now(timezone.utc)
    paths = DataTruthPaths.create(output_root)
    source_csv, raw_dir, source_refs, source_warnings = _resolve_public_yahoo_source(
        paths,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=allow_fetch,
        symbols=symbols,
        required_bar_date=required_bar_date,
        fetch_max_workers=fetch_max_workers,
        fetch_max_requests_per_second=fetch_max_requests_per_second,
        fetch_time_budget_seconds=fetch_time_budget_seconds,
        minimum_history_bars=minimum_history,
        require_production=require_production,
    )
    source_artifacts = _capture_source_artifacts(
        source_csv=source_csv,
        raw_dir=raw_dir,
        source_refs=source_refs,
        require_content_addressed_raw=symbols is not None,
        require_content_addressed_csv=require_production,
    )
    raw_dataset = _load_captured_source_dataset(source_artifacts[0])
    normalized, rejected_count, skipped_incomplete, normalization_warnings = _normalize_daily(
        raw_dataset,
        as_of_date=as_of_date,
        source_refs=source_refs,
    )
    if symbols is not None:
        requested_symbols = tuple(
            sorted(dict.fromkeys(symbol.strip().upper() for symbol in symbols if symbol.strip()))
        )
        missing_exact = tuple(
            symbol
            for symbol in requested_symbols
            if not any(
                bar.timestamp.date() == required_bar_date
                for bar in normalized.bars_by_symbol.get(symbol, ())
            )
        )
        insufficient_history = tuple(
            symbol
            for symbol in requested_symbols
            if len(normalized.bars_by_symbol.get(symbol, ())) < minimum_history
        )
        if missing_exact or insufficient_history:
            raise DataTruthAcquisitionIncomplete(
                "DataTruth acquisition PARTIAL; explicit universe failed governed "
                f"completed-bar/history requirements; missing_exact={list(missing_exact)}; "
                f"insufficient_history={list(insufficient_history)}; "
                f"minimum_history_bars={minimum_history}"
            )
    normalized_bytes = _serialize_ohlcv(normalized)
    validation = validate_dataset(
        normalized,
        min_bars_per_symbol=120,
        max_staleness_days=10,
        as_of=datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc),
    )
    warnings = tuple(
        dict.fromkeys(
            list(source_warnings)
            + list(normalized.warnings)
            + list(validation.warnings)
            + list(validation.issues)
            + _calendar_warnings(as_of_date)
        )
    )
    accepted_start, accepted_end = _date_range(normalized)
    requested_start, requested_end = _date_range(raw_dataset)
    manifest_requested_end = (
        as_of_date.isoformat() if requested_end != "n/a" else requested_end
    )
    normalized_hash = _sha256_bytes(normalized_bytes)
    request_contract = _production_request_contract() if require_production else None
    request_contract_bytes = _json_bytes(request_contract) if request_contract is not None else None
    request_contract_hash = (
        _sha256_bytes(request_contract_bytes) if request_contract_bytes is not None else None
    )
    snapshot_content_hash = _snapshot_content_hash(
        provider_id="public_yahoo_chart",
        timeframe=normalized.timeframe,
        symbols=normalized.symbols,
        requested_start=requested_start,
        requested_end=manifest_requested_end,
        accepted_start=accepted_start,
        accepted_end=accepted_end,
        normalized_hash=normalized_hash,
        source_artifacts=source_artifacts,
        request_contract_hash=request_contract_hash,
    )
    snapshot_id = _snapshot_id(
        provider_id="public_yahoo_chart",
        timeframe=normalized.timeframe,
        accepted_end=accepted_end,
        content_hash=snapshot_content_hash,
    )
    snapshot_relative_path = f"snapshots/{snapshot_id}"
    request_contract_artifact_path = (
        f"{snapshot_relative_path}/source/request_contract.json"
        if request_contract_hash is not None
        else None
    )
    normalized_artifact_path = f"{snapshot_relative_path}/normalized/ohlcv.csv"
    raw_artifact_paths = tuple(
        f"{snapshot_relative_path}/{artifact.logical_path}" for artifact in source_artifacts
    )
    raw_hashes = {
        durable_path: artifact.sha256
        for durable_path, artifact in zip(
            raw_artifact_paths,
            source_artifacts,
            strict=True,
        )
    }
    source_url_or_reference = _durable_source_references(
        source_refs,
        source_artifacts=source_artifacts,
        durable_paths=raw_artifact_paths,
    )
    snapshot_manifest_path = paths.snapshots / snapshot_id / "manifest.json"
    manifest_created_at = _retained_manifest_created_at(
        snapshot_manifest_path,
        snapshot_id=snapshot_id,
        fallback=now.isoformat(),
    )
    manifest = DataTruthManifest(
        snapshot_id=snapshot_id,
        created_at=manifest_created_at,
        provider_id="public_yahoo_chart",
        provider_name="Yahoo Finance Chart API",
        symbols=normalized.symbols,
        timeframe=normalized.timeframe,
        requested_start=requested_start,
        requested_end=manifest_requested_end,
        accepted_start=accepted_start,
        accepted_end=accepted_end,
        bar_count=raw_dataset.total_bars,
        accepted_bar_count=normalized.total_bars,
        rejected_bar_count=rejected_count + len(validation.issues),
        skipped_incomplete_bars=skipped_incomplete,
        validation_status="passed_with_warnings" if warnings else "passed",
        warnings=warnings,
        raw_artifact_hashes=raw_hashes,
        normalized_artifact_hash=normalized_hash,
        source_url_or_reference=source_url_or_reference,
        snapshot_relative_path=snapshot_relative_path,
        normalized_artifact_path=normalized_artifact_path,
        raw_artifact_paths=raw_artifact_paths,
        snapshot_content_hash=snapshot_content_hash,
        artifact_schema_version=SNAPSHOT_ARTIFACT_SCHEMA_VERSION,
        code_version="0.1.0",
        required_bar_date=required_bar_date.isoformat() if required_bar_date else None,
        production_required=require_production,
        request_contract=request_contract,
        request_contract_hash=request_contract_hash,
        request_contract_artifact_path=request_contract_artifact_path,
        request_contract_artifact_hash=request_contract_hash,
        schema_version=DATA_TRUTH_MANIFEST_SCHEMA_VERSION,
    )
    manifest = replace(
        manifest,
        manifest_payload_hash=_manifest_payload_hash(manifest.to_dict()),
    )
    _retain_immutable_snapshot(
        paths=paths,
        manifest=manifest,
        normalized_bytes=normalized_bytes,
        source_artifacts=source_artifacts,
        request_contract_bytes=request_contract_bytes,
    )
    _write_latest_aliases(
        paths=paths,
        manifest=manifest,
        normalized_bytes=normalized_bytes,
        source_artifacts=source_artifacts,
    )

    reconciliation = DataTruthReconciliationReport(
        reconciliation_id=f"{snapshot_id}:reconciliation",
        created_at=now.isoformat(),
        canonical_snapshot_id=snapshot_id,
        provider_count=1,
        status="single_provider_unreconciled",
        canonical_provider_id=manifest.provider_id,
        compared_provider_ids=(),
        disagreements=(),
        warnings=(
            "only one comparable provider snapshot is available; no cross-provider "
            "OHLCV reconciliation was possible",
        ),
    )
    comparison_datasets = _comparison_datasets(
        paths=paths,
        canonical=normalized,
        accepted_start=accepted_start,
        accepted_end=accepted_end,
        allow_fetch=allow_fetch,
    )
    if comparison_datasets:
        from intraday_scanner.v2.data_truth.reconcile import (
            ReconciliationTolerances,
            reconcile_datasets_v2,
            write_reconciliation_v2,
        )

        reconciliation_result = reconcile_datasets_v2(
            canonical_dataset=normalized,
            comparison_datasets=comparison_datasets,
            canonical_snapshot_id=snapshot_id,
            canonical_provider_id=manifest.provider_id,
            tolerances=ReconciliationTolerances(
                price_abs_tolerance=0.05,
                price_minor_abs_tolerance=0.25,
                price_bps_tolerance=10.0,
                price_minor_bps_tolerance=50.0,
                volume_pct_tolerance=0.25,
                min_overlap_rows=max(120, normalized.total_bars // 2),
            ),
            created_at=now,
        )
        reconciliation = reconciliation_result.report
        write_reconciliation_v2(result=reconciliation_result, output_root=output_root)
    else:
        _write_json(paths.reconciliation / "latest_reconciliation.json", reconciliation.to_dict())
        _write_text(
            paths.reconciliation / "latest_reconciliation.md",
            _reconciliation_markdown(reconciliation),
        )
    _write_text(
        paths.reports / "data_truth_summary.md",
        _summary_markdown(manifest, reconciliation),
    )
    retained_dataset, retained_manifest = load_datatruth_snapshot(
        snapshot_id,
        output_root,
    )
    return DataTruthBuildResult(
        dataset=retained_dataset,
        manifest=retained_manifest,
        reconciliation=reconciliation,
        warnings=tuple(dict.fromkeys(warnings + normalization_warnings)),
    )


def load_datatruth_dataset(
    *,
    output_root: Path = Path("data/v2_data_truth"),
    snapshot_id: str | None = None,
) -> tuple[MarketDataset, DataTruthManifest]:
    if snapshot_id is not None:
        return load_datatruth_snapshot(snapshot_id, output_root)
    paths = DataTruthPaths.create(output_root)
    manifest_payload = json.loads((paths.manifests / "latest.json").read_text(encoding="utf-8"))
    if not isinstance(manifest_payload, dict):
        raise ValueError("DataTruth latest manifest must be a JSON object")
    manifest = _manifest_from_payload(manifest_payload)
    if manifest.schema_version == DATA_TRUTH_MANIFEST_SCHEMA_VERSION:
        return load_datatruth_snapshot(manifest.snapshot_id, output_root)
    latest_path = paths.normalized / "latest_ohlcv.csv"
    if not latest_path.is_file():
        raise FileNotFoundError(f"DataTruth latest normalized alias is missing: {latest_path}")
    if _sha256(latest_path) != manifest.normalized_artifact_hash:
        raise ValueError("DataTruth latest normalized alias hash does not match its manifest")
    return _load_manifest_dataset(latest_path, manifest)


def load_datatruth_snapshot(
    snapshot_id: str,
    output_root: Path = Path("data/v2_data_truth"),
) -> tuple[MarketDataset, DataTruthManifest]:
    """Load one immutable named snapshot after verifying every retained byte."""

    manifest = verify_datatruth_snapshot(snapshot_id, output_root)
    assert manifest.normalized_artifact_path is not None
    normalized_path = _artifact_path(output_root, manifest.normalized_artifact_path)
    return _load_manifest_dataset(normalized_path, manifest)


def verify_datatruth_snapshot(
    snapshot_id: str,
    output_root: Path = Path("data/v2_data_truth"),
) -> DataTruthManifest:
    """Verify a named snapshot's manifest identity and immutable artifact bytes."""

    snapshot_root = _named_snapshot_root(output_root, snapshot_id)
    manifest_path = snapshot_root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"DataTruth snapshot manifest is missing: {manifest_path}")
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("DataTruth immutable manifest must be a JSON object")
    manifest = _manifest_from_payload(payload)
    if manifest.snapshot_id != snapshot_id:
        raise ValueError("DataTruth immutable manifest snapshot identity mismatch")
    if manifest.schema_version != DATA_TRUTH_MANIFEST_SCHEMA_VERSION:
        raise ValueError("DataTruth immutable manifest schema is unsupported")
    if manifest.artifact_schema_version != SNAPSHOT_ARTIFACT_SCHEMA_VERSION:
        raise ValueError("DataTruth immutable artifact schema is unsupported")
    expected_snapshot_relative = f"snapshots/{snapshot_id}"
    if manifest.snapshot_relative_path != expected_snapshot_relative:
        raise ValueError("DataTruth immutable snapshot path does not match its identity")
    request_contract_bytes: bytes | None = None
    request_contract_logical_path: str | None = None
    has_request_contract_fields = any(
        value is not None
        for value in (
            manifest.request_contract,
            manifest.request_contract_hash,
            manifest.request_contract_artifact_path,
            manifest.request_contract_artifact_hash,
        )
    )
    if manifest.production_required or has_request_contract_fields:
        if not manifest.production_required:
            raise ValueError("DataTruth request contract is only valid for production snapshots")
        if (
            manifest.request_contract != _production_request_contract()
            or not manifest.request_contract_hash
            or not manifest.request_contract_artifact_path
            or manifest.request_contract_artifact_hash != manifest.request_contract_hash
        ):
            raise ValueError("DataTruth production request contract is incomplete or noncanonical")
        request_contract_bytes = _json_bytes(manifest.request_contract)
        if _sha256_bytes(request_contract_bytes) != manifest.request_contract_hash:
            raise ValueError("DataTruth production request contract hash mismatch")
        try:
            request_contract_logical_path = Path(
                manifest.request_contract_artifact_path
            ).relative_to(Path(expected_snapshot_relative)).as_posix()
        except ValueError as exc:
            raise ValueError(
                "DataTruth production request contract is outside its snapshot"
            ) from exc
        if request_contract_logical_path != "source/request_contract.json":
            raise ValueError("DataTruth production request contract path is noncanonical")
        request_contract_path = _artifact_path(output_root, manifest.request_contract_artifact_path)
        if not request_contract_path.is_file():
            raise FileNotFoundError(
                f"DataTruth production request contract is missing: {request_contract_path}"
            )
        if _bounded_source_bytes(request_contract_path) != request_contract_bytes:
            raise ValueError("DataTruth production request contract bytes are not canonical")
        if _sha256(request_contract_path) != manifest.request_contract_artifact_hash:
            raise ValueError("DataTruth production request contract artifact hash mismatch")
    expected_manifest_hash = _manifest_payload_hash(payload)
    if manifest.manifest_payload_hash != expected_manifest_hash:
        raise ValueError("DataTruth immutable manifest payload hash mismatch")
    if not manifest.normalized_artifact_path:
        raise ValueError("DataTruth immutable normalized artifact path is missing")
    normalized_path = _artifact_path(output_root, manifest.normalized_artifact_path)
    if not normalized_path.is_file():
        raise FileNotFoundError(
            f"DataTruth immutable normalized artifact is missing: {normalized_path}"
        )
    if _sha256(normalized_path) != manifest.normalized_artifact_hash:
        raise ValueError("DataTruth immutable normalized artifact hash mismatch")
    if (
        len(manifest.raw_artifact_paths) != len(set(manifest.raw_artifact_paths))
        or set(manifest.raw_artifact_paths) != set(manifest.raw_artifact_hashes)
    ):
        raise ValueError("DataTruth immutable raw artifact path/hash inventory mismatch")
    normalized_logical_path = Path(manifest.normalized_artifact_path).relative_to(
        Path(expected_snapshot_relative)
    ).as_posix()
    source_count = 0
    logical_raw_hashes: list[tuple[str, str]] = []
    snapshot_relative = Path(expected_snapshot_relative)
    for relative_path in manifest.raw_artifact_paths:
        artifact_path = _artifact_path(output_root, relative_path)
        if not artifact_path.is_file():
            raise FileNotFoundError(
                f"DataTruth immutable source artifact is missing: {artifact_path}"
            )
        expected_hash = manifest.raw_artifact_hashes[relative_path]
        if _sha256(artifact_path) != expected_hash:
            raise ValueError(
                f"DataTruth immutable source artifact hash mismatch: {relative_path}"
            )
        try:
            logical_path = Path(relative_path).relative_to(snapshot_relative).as_posix()
        except ValueError as exc:
            raise ValueError(
                "DataTruth immutable source artifact is outside its snapshot directory"
            ) from exc
        if logical_path == normalized_logical_path or logical_path == "manifest.json":
            raise ValueError("DataTruth raw artifact aliases a reserved snapshot artifact")
        if logical_path == "source/source.csv":
            source_count += 1
        elif (
            Path(logical_path).parent.as_posix() != "raw"
            or Path(logical_path).suffix.lower() != ".json"
            or len(Path(logical_path).parts) != 2
        ):
            raise ValueError("DataTruth raw artifact path has an invalid canonical kind")
        logical_raw_hashes.append((logical_path, expected_hash))
    if source_count != 1:
        raise ValueError("DataTruth raw artifact inventory must contain exactly one source CSV")
    recomputed_content_hash = _snapshot_content_hash_from_hashes(
        provider_id=manifest.provider_id,
        timeframe=manifest.timeframe,
        symbols=manifest.symbols,
        requested_start=manifest.requested_start,
        requested_end=manifest.requested_end,
        accepted_start=manifest.accepted_start,
        accepted_end=manifest.accepted_end,
        normalized_hash=manifest.normalized_artifact_hash,
        source_artifact_hashes=tuple(logical_raw_hashes),
        request_contract_hash=manifest.request_contract_artifact_hash,
    )
    if manifest.snapshot_content_hash != recomputed_content_hash:
        raise ValueError("DataTruth immutable snapshot content hash mismatch")
    if snapshot_id != _snapshot_id(
        provider_id=manifest.provider_id,
        timeframe=manifest.timeframe,
        accepted_end=manifest.accepted_end,
        content_hash=recomputed_content_hash,
    ):
        raise ValueError("DataTruth snapshot ID is not bound to retained artifact content")
    expected_files = {
        "manifest.json",
        Path(manifest.normalized_artifact_path)
        .relative_to(snapshot_relative)
        .as_posix(),
        *(
            Path(path).relative_to(snapshot_relative).as_posix()
            for path in manifest.raw_artifact_paths
        ),
    }
    if request_contract_logical_path is not None:
        expected_files.add(request_contract_logical_path)
    actual_files = {
        path.relative_to(snapshot_root).as_posix()
        for path in snapshot_root.rglob("*")
        if path.is_file()
    }
    if actual_files != expected_files:
        raise ValueError("DataTruth immutable snapshot contains undeclared or missing files")
    return manifest


def _capture_source_artifacts(
    *,
    source_csv: Path,
    raw_dir: Path,
    source_refs: tuple[str, ...] = (),
    require_content_addressed_raw: bool = False,
    require_content_addressed_csv: bool = False,
) -> tuple[_CapturedArtifact, ...]:
    if not source_csv.is_file():
        raise FileNotFoundError(f"DataTruth source CSV is missing: {source_csv}")
    if require_content_addressed_csv and not _is_full_digest_csv_name(source_csv):
        raise DataTruthAcquisitionIncomplete(
            "production cache CSV must be a full-digest content-addressed artifact"
        )
    selected_raw_paths: set[Path] | None = None
    if require_content_addressed_raw:
        selected_raw_paths = set()
        for reference in source_refs:
            if "_chart" not in reference or not reference.endswith(".json"):
                continue
            try:
                selected_raw_paths.add(Path(reference).resolve())
            except OSError:
                continue
    candidates = [
        ("source/source.csv", source_csv),
        *(
            (f"raw/{path.name}", path)
            for path in sorted(raw_dir.glob("*.json"))
            if path.is_file()
            and not path.name.endswith(".contract.json")
            and (selected_raw_paths is None or path.resolve() in selected_raw_paths)
        ),
    ]
    logical_paths = [logical_path for logical_path, _path in candidates]
    if len(logical_paths) != len(set(logical_paths)):
        raise ValueError("DataTruth source artifact names are not unique")
    captured: list[_CapturedArtifact] = []
    for logical_path, source_path in candidates:
        # Reapply the same payload ceiling at capture time.  Validation and
        # capture are separate filesystem observations; a cache object can be
        # replaced between them, so a plain ``read_bytes`` here would permit
        # an oversized allocation before the content-address check rejects it.
        content = _bounded_source_bytes(source_path)
        if logical_path == "source/source.csv" and require_content_addressed_csv:
            digest = source_path.stem.rsplit("_", 1)[-1]
            if hashlib.sha256(content).hexdigest() != digest:
                raise DataTruthAcquisitionIncomplete(
                    "content-addressed CSV changed during DataTruth capture"
                )
        elif logical_path.startswith("raw/") and require_content_addressed_raw:
            artifact_name = source_path.name
            marker = artifact_name.find("_chart_")
            prefix = artifact_name[: marker + len("_chart_")] if marker >= 0 else ""
            if not prefix or not _is_full_digest_raw_name(source_path, prefix):
                raise DataTruthAcquisitionIncomplete(
                    f"raw Yahoo artifact is not a canonical digest object: {source_path}"
                )
            digest = source_path.stem[len(prefix) :]
            if hashlib.sha256(content).hexdigest() != digest:
                raise DataTruthAcquisitionIncomplete(
                    f"raw Yahoo artifact changed during DataTruth capture: {source_path}"
                )
        captured.append(
            _CapturedArtifact(
                logical_path=logical_path,
                source_path=source_path,
                content=content,
                sha256=_sha256_bytes(content),
            )
        )
    return tuple(captured)


def _serialize_ohlcv(dataset: MarketDataset) -> bytes:
    with tempfile.TemporaryDirectory(prefix="dawnstrike_datatruth_") as directory:
        path = Path(directory) / "ohlcv.csv"
        write_ohlcv_csv(dataset, path)
        return path.read_bytes()


def _load_captured_source_dataset(artifact: _CapturedArtifact) -> MarketDataset:
    if artifact.logical_path != "source/source.csv":
        raise ValueError("DataTruth captured source CSV is missing from the artifact inventory")
    with tempfile.TemporaryDirectory(prefix="dawnstrike_datatruth_source_") as directory:
        path = Path(directory) / "source.csv"
        path.write_bytes(artifact.content)
        return load_ohlcv_csv(
            path,
            dataset_id="public_yahoo_chart_2y_1d",
            source_kind="public_yahoo_chart",
            timeframe="1d",
        )


def _snapshot_content_hash(
    *,
    provider_id: str,
    timeframe: str,
    symbols: tuple[str, ...],
    requested_start: str,
    requested_end: str,
    accepted_start: str,
    accepted_end: str,
    normalized_hash: str,
    source_artifacts: tuple[_CapturedArtifact, ...],
    request_contract_hash: str | None = None,
) -> str:
    return _snapshot_content_hash_from_hashes(
        provider_id=provider_id,
        timeframe=timeframe,
        symbols=symbols,
        requested_start=requested_start,
        requested_end=requested_end,
        accepted_start=accepted_start,
        accepted_end=accepted_end,
        normalized_hash=normalized_hash,
        source_artifact_hashes=tuple(
            (artifact.logical_path, artifact.sha256) for artifact in source_artifacts
        ),
        request_contract_hash=request_contract_hash,
    )


def _snapshot_content_hash_from_hashes(
    *,
    provider_id: str,
    timeframe: str,
    symbols: tuple[str, ...],
    requested_start: str,
    requested_end: str,
    accepted_start: str,
    accepted_end: str,
    normalized_hash: str,
    source_artifact_hashes: tuple[tuple[str, str], ...],
    request_contract_hash: str | None = None,
) -> str:
    payload = {
        "accepted_end": accepted_end,
        "accepted_start": accepted_start,
        "normalized_artifact": {
            "path": "normalized/ohlcv.csv",
            "sha256": normalized_hash,
        },
        "provider_id": provider_id,
        "requested_end": requested_end,
        "requested_start": requested_start,
        "schema_version": SNAPSHOT_IDENTITY_SCHEMA_VERSION,
        "source_artifacts": [
            {"path": path, "sha256": artifact_hash}
            for path, artifact_hash in source_artifact_hashes
        ],
        "symbols": list(symbols),
        "timeframe": timeframe,
    }
    if request_contract_hash is not None:
        payload["request_contract"] = {
            "path": "source/request_contract.json",
            "sha256": request_contract_hash,
        }
    return _sha256_bytes(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _snapshot_id(
    *,
    provider_id: str,
    timeframe: str,
    accepted_end: str,
    content_hash: str,
) -> str:
    provider_token = _identifier_token(provider_id)
    timeframe_token = _identifier_token(timeframe)
    accepted_token = (
        accepted_end.replace("-", "") if accepted_end != "n/a" else "noaccepteddate"
    )
    return f"datatruth_{provider_token}_{timeframe_token}_{accepted_token}_{content_hash}"


def _identifier_token(value: str) -> str:
    token = "".join(character.lower() if character.isalnum() else "_" for character in value)
    token = "_".join(part for part in token.split("_") if part)
    if not token:
        raise ValueError("DataTruth snapshot identity token must not be blank")
    return token


def _durable_source_references(
    source_refs: tuple[str, ...],
    *,
    source_artifacts: tuple[_CapturedArtifact, ...],
    durable_paths: tuple[str, ...],
) -> tuple[str, ...]:
    replacements: dict[str, str] = {}
    for artifact, durable_path in zip(source_artifacts, durable_paths, strict=True):
        for representation in (
            str(artifact.source_path),
            artifact.source_path.as_posix(),
            str(artifact.source_path.resolve()),
            artifact.source_path.resolve().as_posix(),
        ):
            replacements[representation] = durable_path
    retained: list[str] = []
    for reference in source_refs:
        replacement = replacements.get(reference)
        if replacement is None and "://" not in reference:
            try:
                replacement = replacements.get(str(Path(reference).resolve()))
            except OSError:
                replacement = None
        retained.append(replacement or reference)
    return tuple(dict.fromkeys(retained))


def _retained_manifest_created_at(
    manifest_path: Path,
    *,
    snapshot_id: str,
    fallback: str,
) -> str:
    if not manifest_path.exists():
        return fallback
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("DataTruth retained manifest must be a JSON object")
    if str(payload.get("snapshot_id") or "") != snapshot_id:
        raise ValueError("DataTruth retained manifest snapshot identity conflict")
    created_at = str(payload.get("created_at") or "")
    if not created_at:
        raise ValueError("DataTruth retained manifest created_at is missing")
    return created_at


def _manifest_payload_hash(payload: dict[str, object]) -> str:
    hash_payload = dict(payload)
    hash_payload.pop("manifest_payload_hash", None)
    return _sha256_bytes(
        json.dumps(hash_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    )


def _retain_immutable_snapshot(
    *,
    paths: DataTruthPaths,
    manifest: DataTruthManifest,
    normalized_bytes: bytes,
    source_artifacts: tuple[_CapturedArtifact, ...],
    request_contract_bytes: bytes | None = None,
) -> None:
    if not manifest.normalized_artifact_path:
        raise ValueError("DataTruth immutable normalized path is missing")
    if not manifest.snapshot_relative_path:
        raise ValueError("DataTruth immutable snapshot path is missing")
    if len(source_artifacts) != len(manifest.raw_artifact_paths):
        raise ValueError("DataTruth immutable source artifact inventory is incomplete")
    snapshot_relative = Path(manifest.snapshot_relative_path)
    try:
        normalized_logical_path = (
            Path(manifest.normalized_artifact_path)
            .relative_to(snapshot_relative)
            .as_posix()
        )
    except ValueError as exc:
        raise ValueError(
            "DataTruth immutable normalized artifact is outside its snapshot"
        ) from exc
    expected_files: dict[str, bytes] = {normalized_logical_path: normalized_bytes}
    if manifest.request_contract_artifact_path is not None:
        if request_contract_bytes is None:
            raise ValueError("DataTruth production request contract bytes are missing")
        try:
            request_contract_logical_path = Path(
                manifest.request_contract_artifact_path
            ).relative_to(snapshot_relative).as_posix()
        except ValueError as exc:
            raise ValueError(
                "DataTruth production request contract is outside its snapshot"
            ) from exc
        if request_contract_logical_path != "source/request_contract.json":
            raise ValueError("DataTruth production request contract path is noncanonical")
        if (
            manifest.request_contract_hash is None
            or manifest.request_contract_artifact_hash != manifest.request_contract_hash
            or _sha256_bytes(request_contract_bytes) != manifest.request_contract_hash
        ):
            raise ValueError("DataTruth production request contract hash is invalid")
        expected_files[request_contract_logical_path] = request_contract_bytes
    elif request_contract_bytes is not None:
        raise ValueError("DataTruth request contract bytes are not declared")
    for artifact, relative_path in zip(
        source_artifacts,
        manifest.raw_artifact_paths,
        strict=True,
    ):
        try:
            logical_path = Path(relative_path).relative_to(snapshot_relative).as_posix()
        except ValueError as exc:
            raise ValueError(
                "DataTruth immutable source artifact is outside its snapshot"
            ) from exc
        if logical_path != artifact.logical_path:
            raise ValueError("DataTruth immutable source artifact path identity mismatch")
        expected_files[logical_path] = artifact.content
    manifest_bytes = _json_bytes(manifest.to_dict())
    expected_files["manifest.json"] = manifest_bytes
    snapshot_root = _named_snapshot_root(paths.root, manifest.snapshot_id)
    _install_snapshot_directory(
        snapshot_root=snapshot_root,
        staging_parent=paths.snapshots,
        expected_files=expected_files,
        staging_token=str(manifest.snapshot_content_hash or manifest.snapshot_id)[-16:],
    )
    _write_immutable_bytes(paths.manifests / f"{manifest.snapshot_id}.json", manifest_bytes)
    verified = verify_datatruth_snapshot(manifest.snapshot_id, paths.root)
    if verified.to_dict() != manifest.to_dict():
        raise ValueError("DataTruth retained manifest differs from the proposed snapshot")


def _install_snapshot_directory(
    *,
    snapshot_root: Path,
    staging_parent: Path,
    expected_files: dict[str, bytes],
    staging_token: str,
) -> None:
    if snapshot_root.exists():
        _complete_or_verify_snapshot_directory(snapshot_root, expected_files)
        return
    staging_parent.mkdir(parents=True, exist_ok=True)
    staging_root = Path(
        tempfile.mkdtemp(
            dir=staging_parent,
            prefix=f".{staging_token}.",
            suffix=".staging",
        )
    )
    try:
        for relative_path, content in sorted(expected_files.items()):
            _write_new_file(_snapshot_member_path(staging_root, relative_path), content)
        _assert_snapshot_directory_bytes(staging_root, expected_files)
        try:
            staging_root.rename(snapshot_root)
        except OSError:
            if not snapshot_root.exists():
                raise
            _complete_or_verify_snapshot_directory(snapshot_root, expected_files)
    finally:
        if staging_root.exists():
            shutil.rmtree(staging_root)


def _complete_or_verify_snapshot_directory(
    snapshot_root: Path,
    expected_files: dict[str, bytes],
) -> None:
    if not snapshot_root.is_dir():
        raise ValueError(f"DataTruth immutable snapshot path is not a directory: {snapshot_root}")
    actual_files = _snapshot_directory_files(snapshot_root)
    unexpected = set(actual_files) - set(expected_files)
    if unexpected:
        raise ValueError(
            "DataTruth immutable snapshot contains undeclared files: "
            + ", ".join(sorted(unexpected))
        )
    for relative_path, actual_path in actual_files.items():
        if actual_path.read_bytes() != expected_files[relative_path]:
            raise ValueError(f"DataTruth immutable artifact conflict: {actual_path}")
    for relative_path in sorted(set(expected_files) - set(actual_files)):
        _write_immutable_bytes(
            _snapshot_member_path(snapshot_root, relative_path),
            expected_files[relative_path],
        )
    _assert_snapshot_directory_bytes(snapshot_root, expected_files)


def _assert_snapshot_directory_bytes(
    snapshot_root: Path,
    expected_files: dict[str, bytes],
) -> None:
    actual_files = _snapshot_directory_files(snapshot_root)
    if set(actual_files) != set(expected_files):
        raise ValueError("DataTruth snapshot staging inventory is incomplete")
    for relative_path, actual_path in actual_files.items():
        if actual_path.read_bytes() != expected_files[relative_path]:
            raise ValueError(f"DataTruth snapshot staging byte mismatch: {relative_path}")


def _snapshot_directory_files(snapshot_root: Path) -> dict[str, Path]:
    return {
        path.relative_to(snapshot_root).as_posix(): path
        for path in snapshot_root.rglob("*")
        if path.is_file()
    }


def _snapshot_member_path(snapshot_root: Path, relative_path: str) -> Path:
    candidate = (snapshot_root / relative_path).resolve()
    try:
        candidate.relative_to(snapshot_root.resolve())
    except ValueError as exc:
        raise ValueError("DataTruth snapshot member path escapes its directory") from exc
    return candidate


def _write_new_file(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("xb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _write_latest_aliases(
    *,
    paths: DataTruthPaths,
    manifest: DataTruthManifest,
    normalized_bytes: bytes,
    source_artifacts: tuple[_CapturedArtifact, ...],
) -> None:
    _write_mutable_bytes(paths.normalized / "latest_ohlcv.csv", normalized_bytes)
    _write_json(paths.manifests / "latest.json", manifest.to_dict())
    for artifact in source_artifacts:
        if artifact.logical_path.startswith("raw/"):
            _write_mutable_bytes(paths.raw / Path(artifact.logical_path).name, artifact.content)


def _write_immutable_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        if not path.is_file() or path.read_bytes() != content:
            raise ValueError(f"DataTruth immutable artifact conflict: {path}") from None


def _write_mutable_bytes(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    file_descriptor, temp_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temp_path = Path(temp_name)
    try:
        with os.fdopen(file_descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _replace_with_retry(temp_path, path)
    finally:
        temp_path.unlink(missing_ok=True)


def _json_bytes(payload: object) -> bytes:
    return (json.dumps(payload, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _sha256_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _named_snapshot_root(output_root: Path, snapshot_id: str) -> Path:
    if not snapshot_id or Path(snapshot_id).name != snapshot_id:
        raise ValueError("DataTruth snapshot ID is invalid")
    snapshots_root = (output_root / "snapshots").resolve()
    snapshot_root = (snapshots_root / snapshot_id).resolve()
    try:
        snapshot_root.relative_to(snapshots_root)
    except ValueError as exc:
        raise ValueError("DataTruth snapshot path escapes the configured root") from exc
    return snapshot_root


def _artifact_path(output_root: Path, relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute():
        raise ValueError("DataTruth artifact paths must be relative to the configured root")
    root = output_root.resolve()
    resolved = (root / path).resolve()
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("DataTruth artifact path escapes the configured root") from exc
    return resolved


def _manifest_from_payload(payload: dict[str, object]) -> DataTruthManifest:
    raw_hashes_payload = payload.get("raw_artifact_hashes", {})
    if not isinstance(raw_hashes_payload, dict):
        raise ValueError("DataTruth manifest raw_artifact_hashes must be an object")
    return DataTruthManifest(
        snapshot_id=str(payload["snapshot_id"]),
        created_at=str(payload["created_at"]),
        provider_id=str(payload["provider_id"]),
        provider_name=str(payload["provider_name"]),
        symbols=tuple(str(item) for item in _payload_list(payload, "symbols")),
        timeframe=str(payload["timeframe"]),
        requested_start=str(payload["requested_start"]),
        requested_end=str(payload["requested_end"]),
        accepted_start=str(payload["accepted_start"]),
        accepted_end=str(payload["accepted_end"]),
        bar_count=_manifest_int(payload, "bar_count"),
        accepted_bar_count=_manifest_int(payload, "accepted_bar_count"),
        rejected_bar_count=_manifest_int(payload, "rejected_bar_count"),
        skipped_incomplete_bars=_manifest_int(payload, "skipped_incomplete_bars"),
        validation_status=str(payload["validation_status"]),
        warnings=tuple(str(item) for item in _payload_list(payload, "warnings")),
        raw_artifact_hashes={
            str(path): str(artifact_hash)
            for path, artifact_hash in raw_hashes_payload.items()
        },
        normalized_artifact_hash=str(payload["normalized_artifact_hash"]),
        source_url_or_reference=tuple(
            str(item) for item in _payload_list(payload, "source_url_or_reference")
        ),
        snapshot_relative_path=_optional_manifest_string(payload, "snapshot_relative_path"),
        normalized_artifact_path=_optional_manifest_string(
            payload,
            "normalized_artifact_path",
        ),
        raw_artifact_paths=tuple(
            str(item) for item in _payload_list(payload, "raw_artifact_paths", required=False)
        ),
        snapshot_content_hash=_optional_manifest_string(payload, "snapshot_content_hash"),
        manifest_payload_hash=_optional_manifest_string(payload, "manifest_payload_hash"),
        artifact_schema_version=_optional_manifest_string(
            payload,
            "artifact_schema_version",
        ),
        code_version=_optional_manifest_string(payload, "code_version"),
        required_bar_date=_optional_manifest_string(payload, "required_bar_date"),
        production_required=_manifest_bool(payload, "production_required"),
        request_contract=_optional_manifest_object(payload, "request_contract"),
        request_contract_hash=_optional_manifest_string(payload, "request_contract_hash"),
        request_contract_artifact_path=_optional_manifest_string(
            payload,
            "request_contract_artifact_path",
        ),
        request_contract_artifact_hash=_optional_manifest_string(
            payload,
            "request_contract_artifact_hash",
        ),
        schema_version=str(payload.get("schema_version") or "v2.data_truth_manifest.v1"),
    )


def _payload_list(
    payload: dict[str, object],
    field_name: str,
    *,
    required: bool = True,
) -> list[object]:
    value = payload.get(field_name)
    if value is None and not required:
        return []
    if not isinstance(value, list):
        raise ValueError(f"DataTruth manifest {field_name} must be an array")
    return value


def _optional_manifest_string(payload: dict[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    return str(value) if value not in {None, ""} else None


def _optional_manifest_object(
    payload: dict[str, object],
    field_name: str,
) -> dict[str, object] | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError(f"DataTruth manifest {field_name} must be an object")
    return dict(value)


def _manifest_bool(payload: dict[str, object], field_name: str) -> bool:
    value = payload.get(field_name, False)
    if not isinstance(value, bool):
        raise ValueError(f"DataTruth manifest {field_name} must be a boolean")
    return value


def _manifest_int(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if isinstance(value, bool) or not isinstance(value, str | int | float):
        raise ValueError(f"DataTruth manifest {field_name} must be an integer")
    return int(value)


def _load_manifest_dataset(
    normalized_path: Path,
    manifest: DataTruthManifest,
) -> tuple[MarketDataset, DataTruthManifest]:
    dataset = load_ohlcv_csv(
        normalized_path,
        dataset_id=manifest.snapshot_id,
        source_kind=manifest.provider_id,
        timeframe=DATA_TRUTH_NORMALIZED_TIMEFRAME,
    )
    if dataset.timeframe != manifest.timeframe:
        raise ValueError("DataTruth retained dataset timeframe does not match its manifest")
    if dataset.symbols != manifest.symbols:
        raise ValueError("DataTruth retained dataset symbols do not match its manifest")
    if dataset.total_bars != manifest.accepted_bar_count:
        raise ValueError("DataTruth retained dataset bar count does not match its manifest")
    accepted_start, accepted_end = _date_range(dataset)
    if accepted_start != manifest.accepted_start or accepted_end != manifest.accepted_end:
        raise ValueError("DataTruth retained dataset accepted range does not match its manifest")
    retained = MarketDataset(
        dataset_id=manifest.snapshot_id,
        source_kind=manifest.provider_id,
        timeframe=manifest.timeframe,
        bars_by_symbol=dataset.bars_by_symbol,
        source_path=normalized_path.resolve().as_posix(),
        warnings=manifest.warnings,
        source_refs=manifest.source_url_or_reference,
    )
    return retained, manifest


def reconcile_provider_datasets(
    *,
    canonical_dataset: MarketDataset,
    comparison_datasets: dict[str, MarketDataset],
    canonical_snapshot_id: str,
    canonical_provider_id: str,
    created_at: datetime | None = None,
    price_tolerance: float = 0.01,
    volume_tolerance: int = 0,
) -> DataTruthReconciliationReport:
    """Compare provider datasets without averaging or mutating canonical bars."""

    now = created_at or datetime.now(timezone.utc)
    disagreements: list[ProviderDisagreement] = []
    for provider_id, dataset in sorted(comparison_datasets.items()):
        for symbol, canonical_bars in canonical_dataset.bars_by_symbol.items():
            other_by_timestamp = {
                bar.timestamp.isoformat(): bar
                for bar in dataset.bars_by_symbol.get(symbol, ())
            }
            for canonical_bar in canonical_bars:
                timestamp = canonical_bar.timestamp.isoformat()
                other = other_by_timestamp.get(timestamp)
                if other is None:
                    disagreements.append(
                        ProviderDisagreement(
                            symbol=symbol,
                            timestamp=timestamp,
                            field_name="bar",
                            canonical_value=None,
                            other_value=None,
                            provider_id=provider_id,
                            tolerance=0.0,
                            severity="error",
                        )
                    )
                    continue
                for field_name in ("open", "high", "low", "close"):
                    canonical_value = float(getattr(canonical_bar, field_name))
                    other_value = float(getattr(other, field_name))
                    if abs(canonical_value - other_value) > price_tolerance:
                        disagreements.append(
                            ProviderDisagreement(
                                symbol=symbol,
                                timestamp=timestamp,
                                field_name=field_name,
                                canonical_value=canonical_value,
                                other_value=other_value,
                                provider_id=provider_id,
                                tolerance=price_tolerance,
                            )
                        )
                if abs(canonical_bar.volume - other.volume) > volume_tolerance:
                    disagreements.append(
                        ProviderDisagreement(
                            symbol=symbol,
                            timestamp=timestamp,
                            field_name="volume",
                            canonical_value=canonical_bar.volume,
                            other_value=other.volume,
                            provider_id=provider_id,
                            tolerance=float(volume_tolerance),
                        )
                    )
    status = "reconciled" if not disagreements else "provider_disagreement"
    warnings = (
        ()
        if not disagreements
        else ("provider OHLCV disagreements exceeded configured tolerances",)
    )
    return DataTruthReconciliationReport(
        reconciliation_id=f"{canonical_snapshot_id}:provider_reconciliation",
        created_at=now.isoformat(),
        canonical_snapshot_id=canonical_snapshot_id,
        provider_count=1 + len(comparison_datasets),
        status=status,
        canonical_provider_id=canonical_provider_id,
        compared_provider_ids=tuple(sorted(comparison_datasets)),
        disagreements=tuple(disagreements),
        warnings=warnings,
    )


def _resolve_public_yahoo_source(
    paths: DataTruthPaths,
    *,
    source_csv: Path | None,
    raw_dir: Path | None,
    allow_fetch: bool,
    symbols: tuple[str, ...] | None,
    required_bar_date: date | None = None,
    fetch_max_workers: int = 12,
    fetch_max_requests_per_second: float | None = 8.0,
    fetch_time_budget_seconds: float | None = 90 * 60,
    minimum_history_bars: int = 0,
    require_production: bool = False,
) -> tuple[Path, Path, tuple[str, ...], tuple[str, ...]]:
    if source_csv is not None and raw_dir is not None:
        return (
            source_csv,
            raw_dir,
            _source_refs_from_cache(
                source_csv,
                raw_dir,
                required_symbols=symbols,
                required_bar_date=required_bar_date,
                minimum_history_bars=minimum_history_bars,
                require_production=require_production,
            ),
            (),
        )
    local_cache = paths.cache / "public_yahoo"
    local_csv = local_cache / "public_yahoo_ohlcv.csv"
    alpha_cache = Path("data/v2_alpha_lab/fixtures/public_yahoo")
    alpha_csv = alpha_cache / "public_yahoo_ohlcv.csv"
    fetch_warnings: tuple[str, ...] = ()
    refresh_failure: str | None = None
    if allow_fetch:
        from intraday_scanner.public_data.yahoo_chart_fetcher import (
            fetch_yahoo_chart_daily_dataset,
        )

        try:
            fetch_kwargs: dict[str, object] = {"cache_dir": local_cache}
            if symbols is not None:
                fetch_kwargs.update(
                    {
                        "symbols": symbols,
                        "required_bar_date": required_bar_date,
                        "max_workers": fetch_max_workers,
                        "max_requests_per_second": fetch_max_requests_per_second,
                        "time_budget_seconds": fetch_time_budget_seconds,
                        "minimum_history_bars": minimum_history_bars,
                    }
                )
            fetched = fetch_yahoo_chart_daily_dataset(**fetch_kwargs)
            fetch_warnings = tuple(fetched.warnings)
            if symbols is not None:
                requested_symbols = tuple(
                    sorted(
                        dict.fromkeys(
                            symbol.strip().upper() for symbol in symbols if symbol.strip()
                        )
                    )
                )
                if fetched.dataset.symbols != requested_symbols:
                    missing = tuple(
                        symbol
                        for symbol in requested_symbols
                        if symbol not in fetched.dataset.symbols
                    )
                    raise DataTruthAcquisitionIncomplete(
                        "DataTruth Yahoo acquisition PARTIAL; exact requested symbol set was not "
                        f"completed; missing={list(missing)}"
                    )
            if fetched.dataset.source_path:
                fetched_csv = Path(fetched.dataset.source_path)
                if fetched.dataset.total_bars and fetched_csv.exists():
                    if symbols is not None:
                        insufficient = tuple(
                            symbol
                            for symbol in symbols
                            if len(
                                {
                                    bar.timestamp
                                    for bar in fetched.dataset.bars_by_symbol.get(symbol, ())
                                }
                            )
                            < minimum_history_bars
                        )
                        missing_exact = tuple(
                            symbol
                            for symbol in symbols
                            if required_bar_date is not None
                            and required_bar_date
                            not in {
                                bar.timestamp.date()
                                for bar in fetched.dataset.bars_by_symbol.get(symbol, ())
                            }
                        )
                        if insufficient or missing_exact:
                            raise DataTruthAcquisitionIncomplete(
                                "DataTruth acquisition PARTIAL; explicit universe failed "
                                "governed completed-bar/history requirements; "
                                f"missing_exact={list(missing_exact)}; "
                                f"insufficient_history={list(insufficient)}; "
                                f"minimum_history_bars={minimum_history_bars}"
                            )
                    verified_refs = _source_refs_from_cache(
                        fetched_csv,
                        local_cache,
                        required_symbols=symbols,
                        required_bar_date=required_bar_date,
                        minimum_history_bars=minimum_history_bars,
                        require_production=require_production,
                    )
                    return (
                        fetched_csv,
                        local_cache,
                        tuple(dict.fromkeys((*fetched.dataset.source_refs, *verified_refs))),
                        fetch_warnings,
                    )
            refresh_failure = "refresh returned no usable daily bars"
        except (OSError, TimeoutError, TypeError, ValueError) as exc:
            refresh_failure = f"{type(exc).__name__}: {exc}"

    cache_pairs = ((local_csv, local_cache), (alpha_csv, alpha_cache))
    if symbols is not None:
        # The mutable compatibility alias is never authoritative for an
        # explicit scheduled universe. Search immutable CSV objects and select
        # the most complete exact-date candidate deterministically.
        candidate_pairs = [
            (path, cache_dir)
            for _legacy, cache_dir in cache_pairs
            for path in sorted(cache_dir.glob("public_yahoo_ohlcv_*.csv"))
            if _is_full_digest_csv_name(path)
        ]
    else:
        candidate_pairs = [
            (cached_csv, cache_dir)
            for cached_csv, cache_dir in cache_pairs
            if cached_csv.exists()
        ]
    valid_candidates: list[
        tuple[tuple[int, float, str], Path, Path, tuple[str, ...], tuple[str, ...]]
    ] = []
    for cached_csv, cache_dir in candidate_pairs:
        if not cached_csv.exists():
            continue
        fallback_warning: tuple[str, ...] = ()
        if refresh_failure is not None:
            fallback_warning = (
                "public_yahoo_chart: refresh failed "
                f"({refresh_failure}); using cached OHLCV from {cached_csv.as_posix()}",
            )
        try:
            refs = _source_refs_from_cache(
                cached_csv,
                cache_dir,
                required_symbols=symbols,
                required_bar_date=required_bar_date,
                minimum_history_bars=minimum_history_bars,
                require_production=require_production,
            )
        except (OSError, TypeError, ValueError, DataTruthAcquisitionIncomplete):
            continue
        if symbols is None:
            return (
                cached_csv,
                cache_dir,
                refs,
                tuple(dict.fromkeys(fetch_warnings + fallback_warning)),
            )
        selected = load_ohlcv_csv(
            cached_csv,
            dataset_id="cache_candidate_selection",
            source_kind="public_yahoo_chart",
            timeframe="1d",
        )
        bars = [bar for symbol in selected.symbols for bar in selected.bars_by_symbol[symbol]]
        span = (
            max(bar.timestamp for bar in bars) - min(bar.timestamp for bar in bars)
        ).total_seconds()
        digest = cached_csv.stem.rsplit("_", 1)[-1]
        valid_candidates.append(
            (
                (len({bar.timestamp for bar in bars}), span, digest),
                cached_csv,
                cache_dir,
                refs,
                fallback_warning,
            )
        )
    if valid_candidates:
        _score, cached_csv, cache_dir, refs, candidate_warning = max(
            valid_candidates, key=lambda item: item[0]
        )
        return (
            cached_csv,
            cache_dir,
            refs,
            tuple(dict.fromkeys(fetch_warnings + candidate_warning)),
        )
    if symbols is not None and refresh_failure is not None:
        raise DataTruthAcquisitionIncomplete(
            "DataTruth Yahoo acquisition terminal failure for exact requested symbol set: "
            f"{refresh_failure}"
        )
    raise FileNotFoundError("no cached public Yahoo OHLCV data was available")


def _comparison_datasets(
    *,
    paths: DataTruthPaths,
    canonical: MarketDataset,
    accepted_start: str,
    accepted_end: str,
    allow_fetch: bool,
) -> dict[str, MarketDataset]:
    if accepted_start == "n/a" or accepted_end == "n/a":
        return {}
    nasdaq_cache = paths.cache / "public_nasdaq"
    nasdaq_csv = nasdaq_cache / "public_nasdaq_ohlcv.csv"
    if allow_fetch:
        try:
            from intraday_scanner.public_data.nasdaq_historical_fetcher import (
                fetch_nasdaq_historical_daily_dataset,
            )

            fetched = fetch_nasdaq_historical_daily_dataset(
                cache_dir=nasdaq_cache,
                start=date.fromisoformat(accepted_start),
                end=date.fromisoformat(accepted_end),
                symbols=canonical.symbols,
            )
            if fetched.dataset.total_bars:
                return {fetched.dataset.source_kind: fetched.dataset}
        except (OSError, TimeoutError, ValueError, TypeError):
            pass
    if nasdaq_csv.exists():
        dataset = load_ohlcv_csv(
            nasdaq_csv,
            dataset_id="public_nasdaq_historical_1d",
            source_kind="public_nasdaq_historical",
            timeframe="1d",
        )
        if dataset.total_bars:
            return {dataset.source_kind: dataset}
    return {}


def _normalize_daily(
    dataset: MarketDataset,
    *,
    as_of_date: date,
    source_refs: tuple[str, ...],
) -> tuple[MarketDataset, int, int, tuple[str, ...]]:
    bars_by_symbol: dict[str, list[MarketBar]] = {}
    warnings: list[str] = list(dataset.warnings)
    rejected_count = 0
    skipped_incomplete = 0
    for symbol, bars in sorted(dataset.bars_by_symbol.items()):
        seen: set[datetime] = set()
        for bar in bars:
            if bar.timestamp.date() >= as_of_date:
                skipped_incomplete += 1
                warnings.append(
                    f"{symbol}: skipped incomplete daily bar {bar.timestamp.date().isoformat()}"
                )
                continue
            if bar.timestamp in seen:
                rejected_count += 1
                warnings.append(f"{symbol}: duplicate timestamp {bar.timestamp.isoformat()}")
                continue
            seen.add(bar.timestamp)
            if not all(math.isfinite(value) for value in (bar.open, bar.high, bar.low, bar.close)):
                rejected_count += 1
                warnings.append(f"{symbol}: rejected non-finite OHLC at {bar.timestamp}")
                continue
            if (
                type(bar.volume) is not int
                or bar.volume < 0
                or bar.volume > MAX_MARKET_VOLUME
            ):
                rejected_count += 1
                warnings.append(f"{symbol}: rejected invalid volume at {bar.timestamp}")
                continue
            if min(bar.open, bar.high, bar.low, bar.close) <= 0:
                rejected_count += 1
                warnings.append(f"{symbol}: rejected non-positive OHLC at {bar.timestamp}")
                continue
            if bar.high < max(bar.open, bar.close, bar.low):
                rejected_count += 1
                warnings.append(f"{symbol}: rejected invalid high at {bar.timestamp}")
                continue
            if bar.low > min(bar.open, bar.close, bar.high):
                rejected_count += 1
                warnings.append(f"{symbol}: rejected invalid low at {bar.timestamp}")
                continue
            if bar.volume <= 0:
                warnings.append(f"{symbol}: suspicious non-positive volume at {bar.timestamp}")
            bars_by_symbol.setdefault(symbol, []).append(bar)
    normalized = MarketDataset(
        dataset_id="datatruth_public_yahoo_chart_1d",
        source_kind="public_yahoo_chart",
        timeframe="1d",
        bars_by_symbol={
            symbol: tuple(sorted(symbol_bars, key=lambda item: item.timestamp))
            for symbol, symbol_bars in bars_by_symbol.items()
        },
        warnings=tuple(dict.fromkeys(warnings)),
        source_refs=source_refs,
    )
    return normalized, rejected_count, skipped_incomplete, tuple(dict.fromkeys(warnings))


def _source_refs_from_cache(
    source_csv: Path,
    raw_dir: Path,
    *,
    required_symbols: tuple[str, ...] | None = None,
    required_bar_date: date | None = None,
    minimum_history_bars: int = 0,
    require_production: bool = False,
) -> tuple[str, ...]:
    expected_request = _production_request_contract()
    _validate_cache_request_contract(
        source_csv,
        expected_request,
        required=require_production,
    )
    if require_production and not _is_full_digest_csv_name(source_csv):
        raise DataTruthAcquisitionIncomplete(
            "production cache CSV must be a full-digest content-addressed artifact"
        )
    if _is_full_digest_csv_name(source_csv):
        digest = source_csv.stem.rsplit("_", 1)[-1]
        content = _bounded_source_bytes(source_csv)
        if hashlib.sha256(content).hexdigest() != digest:
            raise DataTruthAcquisitionIncomplete(
                "content-addressed CSV digest does not match its bytes"
            )
    refs = [source_csv.as_posix()]
    selected = load_ohlcv_csv(
        source_csv,
        dataset_id="cache_source_selection",
        source_kind="public_yahoo_chart",
        timeframe="1d",
    )
    if required_symbols is not None and set(selected.symbols) != set(required_symbols):
        raise DataTruthAcquisitionIncomplete(
            "CSV source symbol set does not exactly match scheduled requested universe"
        )
    if required_symbols is not None:
        for symbol in required_symbols:
            bars = selected.bars_by_symbol.get(symbol, ())
            unique_timestamps = {bar.timestamp for bar in bars}
            if len(unique_timestamps) < minimum_history_bars:
                raise DataTruthAcquisitionIncomplete(
                    f"CSV source symbol {symbol} lacks governed minimum history"
                )
            if required_bar_date is not None:
                dates = [bar.timestamp.date() for bar in bars]
                if not dates or max(dates) != required_bar_date or required_bar_date not in dates:
                    raise DataTruthAcquisitionIncomplete(
                        f"CSV source symbol {symbol} lacks exact completed bar {required_bar_date}"
                    )
    from intraday_scanner.v2.data.yahoo_chart import _bars_from_payload

    for symbol in selected.symbols:
        prefix = f"{symbol.lower()}_chart_"
        matches: list[Path] = []
        for path in sorted(raw_dir.glob(f"{prefix}*.json")):
            if not _is_full_digest_raw_name(path, prefix) or not path.is_file():
                continue
            try:
                content = _bounded_source_bytes(path)
                digest = hashlib.sha256(content).hexdigest()
                if digest != path.stem[len(prefix) :]:
                    continue
                _validate_cache_request_contract(
                    path,
                    expected_request,
                    required=require_production,
                )
                payload = json.loads(content.decode("utf-8"))
                provider_symbol = symbol.replace(".", "-")
                chart = payload.get("chart")
                result = chart.get("result", []) if isinstance(chart, dict) else []
                first_result = result[0] if isinstance(result, list) and result else {}
                meta = first_result.get("meta", {}) if isinstance(first_result, dict) else {}
                if meta.get("symbol") != provider_symbol:
                    continue
                bars, _warnings = _bars_from_payload(symbol, payload)
                if _bar_fingerprint(bars) == _bar_fingerprint(selected.bars_by_symbol[symbol]):
                    matches.append(path)
            except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
                continue
        if not matches:
            if required_symbols is not None:
                raise DataTruthAcquisitionIncomplete(
                    f"no verified raw Yahoo artifact matches CSV symbol {symbol}"
                )
            continue
        if required_symbols is not None and len(matches) != 1:
            raise DataTruthAcquisitionIncomplete(
                f"expected exactly one verified raw Yahoo artifact for {symbol}; "
                f"found={len(matches)}"
            )
        path = matches[0]
        provider_symbol = symbol.replace(".", "-")
        refs.extend(
            (
                f"canonical_symbol:{symbol}",
                f"yahoo_symbol:{provider_symbol}",
                "https://query1.finance.yahoo.com/v8/finance/chart/"
                f"{provider_symbol}?range=2y&interval=1d&includePrePost=false&events=history",
                path.as_posix(),
            )
        )
    return tuple(refs)


def _validate_cache_request_contract(
    path: Path,
    expected: dict[str, object],
    *,
    required: bool,
) -> None:
    contract_path = path.with_name(f"{path.name}.contract.json")
    if not contract_path.exists():
        if required:
            raise DataTruthAcquisitionIncomplete(
                f"cache artifact lacks the governed request contract: {path}"
            )
        return
    try:
        payload = json.loads(_bounded_source_bytes(contract_path).decode("utf-8"))
    except (OSError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DataTruthAcquisitionIncomplete(
            f"cache artifact request contract is malformed: {contract_path}"
        ) from exc
    if payload != expected:
        raise DataTruthAcquisitionIncomplete(
            f"cache artifact request contract does not match governed 2y/1d request: {path}"
        )


def _is_full_digest_raw_name(path: Path, prefix: str) -> bool:
    digest = path.stem[len(prefix) :]
    return (
        path.name.startswith(prefix)
        and path.name.endswith(".json")
        and len(digest) == 64
        and re.fullmatch(r"[0-9a-f]{64}", digest) is not None
    )


def _is_full_digest_csv_name(path: Path) -> bool:
    match = re.fullmatch(r"public_yahoo_ohlcv_([0-9a-f]{64})\.csv", path.name)
    return match is not None


def _bounded_source_bytes(path: Path) -> bytes:
    max_bytes = MAX_MARKET_CSV_BYTES if path.suffix.lower() == ".csv" else 16 * 1024 * 1024
    if path.stat().st_size > max_bytes:
        raise ValueError("source artifact exceeds maximum payload size")
    chunks: list[bytes] = []
    total = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(min(65536, max_bytes + 1 - total))
            if not chunk:
                return b"".join(chunks)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("source artifact exceeds maximum payload size")
            chunks.append(chunk)


def _bar_fingerprint(
    bars: tuple[MarketBar, ...] | list[MarketBar],
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        (
            bar.timestamp.isoformat(),
            bar.open,
            bar.high,
            bar.low,
            bar.close,
            bar.volume,
        )
        for bar in bars
    )


def _artifact_hashes(paths: tuple[Path, ...]) -> dict[str, str]:
    return {path.as_posix(): _sha256(path) for path in paths if path.exists()}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _date_range(dataset: MarketDataset) -> tuple[str, str]:
    timestamps = [bar.timestamp for bars in dataset.bars_by_symbol.values() for bar in bars]
    if not timestamps:
        return "n/a", "n/a"
    return min(timestamps).date().isoformat(), max(timestamps).date().isoformat()


def _governed_minimum_history_bars() -> int:
    """Derive the fleet warm-up floor from the active strategy catalog."""

    from intraday_scanner.v2.strategies import build_strategy_catalog

    warmup_values: list[int] = []
    for strategy in build_strategy_catalog():
        for name, value in strategy.parameters.items():
            if not any(
                token in name.lower()
                for token in ("period", "window", "lookback", "index")
            ):
                continue
            if isinstance(value, bool) or not isinstance(value, int | float) or value <= 0:
                continue
            warmup_values.append(int(value))
    if not warmup_values:
        raise ValueError("active strategy catalog has no governed history requirement")
    # An indicator that starts at index N needs N+1 observations.
    return max(warmup_values) + 1


def _last_completed_market_session(value: date) -> date:
    """Resolve the prior exchange session, including weekends and holidays."""

    candidate = value - timedelta(days=1)
    while not market_session(candidate).is_trading_day:
        candidate -= timedelta(days=1)
    return candidate


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp_path = path.with_name(f".{path.name}.tmp")
    temp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _replace_with_retry(temp_path, path)


def _replace_with_retry(source: Path, target: Path) -> None:
    for attempt in range(10):
        try:
            source.replace(target)
            return
        except PermissionError:
            if attempt == 9:
                raise
            time.sleep(0.05 * (attempt + 1))


def _write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _copy_raw_artifacts(source_dir: Path, target_dir: Path) -> None:
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in sorted(source_dir.glob("*_chart.json")):
        target = target_dir / path.name
        if not target.exists() or _sha256(target) != _sha256(path):
            target.write_bytes(path.read_bytes())


def _summary_markdown(
    manifest: DataTruthManifest,
    reconciliation: DataTruthReconciliationReport,
) -> str:
    lines = [
        "# DataTruth v1 Summary",
        "",
        f"- Snapshot ID: `{manifest.snapshot_id}`",
        f"- Provider: `{manifest.provider_name}` (`{manifest.provider_id}`)",
        f"- Symbols: {', '.join(manifest.symbols)}",
        f"- Accepted bars: `{manifest.accepted_bar_count}`",
        f"- Accepted date range: `{manifest.accepted_start}` to `{manifest.accepted_end}`",
        f"- Validation status: `{manifest.validation_status}`",
        f"- Reconciliation status: `{reconciliation.status}`",
        "",
        "## Completed-Bar Policy",
        "",
        "- Daily bars dated on or after the requested run date are skipped.",
        "- Incomplete current-day bars are warnings, not accepted market evidence.",
        "",
        "## Warnings",
        "",
    ]
    if manifest.warnings:
        lines.extend(f"- {warning}" for warning in manifest.warnings)
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Limitations",
            "",
            "- This snapshot is single-provider unless a second comparable source is added.",
            "- Public/free OHLCV is not broker-grade market data.",
        ]
    )
    return "\n".join(lines) + "\n"


def _reconciliation_markdown(report: DataTruthReconciliationReport) -> str:
    lines = [
        "# DataTruth Reconciliation",
        "",
        f"- Reconciliation ID: `{report.reconciliation_id}`",
        f"- Canonical snapshot: `{report.canonical_snapshot_id}`",
        f"- Status: `{report.status}`",
        f"- Provider count: `{report.provider_count}`",
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in report.warnings)
    if report.disagreements:
        lines.extend(["", "## Disagreements", ""])
        for item in report.disagreements:
            lines.append(
                f"- {item.symbol} {item.timestamp} {item.field_name}: "
                f"{item.canonical_value} vs {item.other_value}"
            )
    return "\n".join(lines) + "\n"


def count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _calendar_warnings(as_of_date: date) -> list[str]:
    if not market_session(as_of_date).is_trading_day:
        latest = _last_completed_market_session(as_of_date)
        return [
            f"run date {as_of_date.isoformat()} is not a trading session; latest completed "
            f"trading date is {latest.isoformat()} per governed market calendar"
        ]
    return []
