"""DataTruth v1 snapshot building and reconciliation."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from intraday_scanner.v2.data import (
    MarketBar,
    MarketDataset,
    load_ohlcv_csv,
    validate_dataset,
    write_ohlcv_csv,
)
from intraday_scanner.v2.data_truth.models import (
    DataTruthManifest,
    DataTruthReconciliationReport,
    ProviderDisagreement,
)


@dataclass(frozen=True)
class DataTruthPaths:
    root: Path
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


def build_data_truth_snapshot(
    *,
    as_of_date: date,
    output_root: Path = Path("data/v2_data_truth"),
    created_at: datetime | None = None,
    source_csv: Path | None = None,
    raw_dir: Path | None = None,
    allow_fetch: bool = True,
) -> DataTruthBuildResult:
    now = created_at or datetime.now(timezone.utc)
    paths = DataTruthPaths.create(output_root)
    source_csv, raw_dir, source_refs, source_warnings = _resolve_public_yahoo_source(
        paths,
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=allow_fetch,
    )
    raw_hashes = _artifact_hashes(tuple(sorted(raw_dir.glob("*.json"))) + (source_csv,))
    raw_dataset = load_ohlcv_csv(
        source_csv,
        dataset_id="public_yahoo_chart_2y_1d",
        source_kind="public_yahoo_chart",
        timeframe="1d",
    )
    normalized, rejected_count, skipped_incomplete, normalization_warnings = _normalize_daily(
        raw_dataset,
        as_of_date=as_of_date,
        source_refs=source_refs,
    )
    normalized_path = paths.normalized / "latest_ohlcv.csv"
    write_ohlcv_csv(normalized, normalized_path)
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
    snapshot_id = f"datatruth_public_yahoo_chart_1d_{accepted_end.replace('-', '')}"
    normalized_hash = _sha256(normalized_path)
    manifest = DataTruthManifest(
        snapshot_id=snapshot_id,
        created_at=now.isoformat(),
        provider_id="public_yahoo_chart",
        provider_name="Yahoo Finance Chart API",
        symbols=normalized.symbols,
        timeframe=normalized.timeframe,
        requested_start=requested_start,
        requested_end=as_of_date.isoformat() if requested_end != "n/a" else requested_end,
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
        source_url_or_reference=source_refs,
        code_version="0.1.0",
    )
    _write_json(paths.manifests / f"{snapshot_id}.json", manifest.to_dict())
    _write_json(paths.manifests / "latest.json", manifest.to_dict())

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
    _copy_raw_artifacts(raw_dir, paths.raw)
    return DataTruthBuildResult(
        dataset=normalized,
        manifest=manifest,
        reconciliation=reconciliation,
        warnings=tuple(dict.fromkeys(warnings + normalization_warnings)),
    )


def load_datatruth_dataset(
    *,
    output_root: Path = Path("data/v2_data_truth"),
) -> tuple[MarketDataset, DataTruthManifest]:
    paths = DataTruthPaths.create(output_root)
    manifest_payload = json.loads((paths.manifests / "latest.json").read_text(encoding="utf-8"))
    dataset = load_ohlcv_csv(
        paths.normalized / "latest_ohlcv.csv",
        dataset_id=str(manifest_payload["snapshot_id"]),
        source_kind=str(manifest_payload["provider_id"]),
        timeframe=str(manifest_payload["timeframe"]),
    )
    manifest = DataTruthManifest(
        snapshot_id=str(manifest_payload["snapshot_id"]),
        created_at=str(manifest_payload["created_at"]),
        provider_id=str(manifest_payload["provider_id"]),
        provider_name=str(manifest_payload["provider_name"]),
        symbols=tuple(str(item) for item in manifest_payload["symbols"]),
        timeframe=str(manifest_payload["timeframe"]),
        requested_start=str(manifest_payload["requested_start"]),
        requested_end=str(manifest_payload["requested_end"]),
        accepted_start=str(manifest_payload["accepted_start"]),
        accepted_end=str(manifest_payload["accepted_end"]),
        bar_count=int(manifest_payload["bar_count"]),
        accepted_bar_count=int(manifest_payload["accepted_bar_count"]),
        rejected_bar_count=int(manifest_payload["rejected_bar_count"]),
        skipped_incomplete_bars=int(manifest_payload["skipped_incomplete_bars"]),
        validation_status=str(manifest_payload["validation_status"]),
        warnings=tuple(str(item) for item in manifest_payload["warnings"]),
        raw_artifact_hashes=dict(manifest_payload["raw_artifact_hashes"]),
        normalized_artifact_hash=str(manifest_payload["normalized_artifact_hash"]),
        source_url_or_reference=tuple(
            str(item) for item in manifest_payload["source_url_or_reference"]
        ),
        code_version=manifest_payload.get("code_version"),
        schema_version=str(manifest_payload["schema_version"]),
    )
    dataset = MarketDataset(
        dataset_id=dataset.dataset_id,
        source_kind=dataset.source_kind,
        timeframe=dataset.timeframe,
        bars_by_symbol=dataset.bars_by_symbol,
        source_path=str((paths.normalized / "latest_ohlcv.csv").as_posix()),
        warnings=manifest.warnings,
        source_refs=manifest.source_url_or_reference,
    )
    return dataset, manifest


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
) -> tuple[Path, Path, tuple[str, ...], tuple[str, ...]]:
    if source_csv is not None and raw_dir is not None:
        return source_csv, raw_dir, _source_refs_from_cache(source_csv, raw_dir), ()
    alpha_cache = Path("data/v2_alpha_lab/fixtures/public_yahoo")
    alpha_csv = alpha_cache / "public_yahoo_ohlcv.csv"
    if alpha_csv.exists():
        return alpha_csv, alpha_cache, _source_refs_from_cache(alpha_csv, alpha_cache), ()
    local_cache = paths.cache / "public_yahoo"
    local_csv = local_cache / "public_yahoo_ohlcv.csv"
    if local_csv.exists():
        return local_csv, local_cache, _source_refs_from_cache(local_csv, local_cache), ()
    if allow_fetch:
        from intraday_scanner.public_data.yahoo_chart_fetcher import (
            fetch_yahoo_chart_daily_dataset,
        )

        fetched = fetch_yahoo_chart_daily_dataset(cache_dir=local_cache)
        if fetched.dataset.source_path:
            fetched_csv = Path(fetched.dataset.source_path)
            return (
                fetched_csv,
                local_cache,
                fetched.dataset.source_refs,
                fetched.warnings,
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


def _source_refs_from_cache(source_csv: Path, raw_dir: Path) -> tuple[str, ...]:
    refs = [source_csv.as_posix()]
    for path in sorted(raw_dir.glob("*_chart.json")):
        symbol = path.name.split("_", 1)[0].upper()
        refs.append(
            "https://query1.finance.yahoo.com/v8/finance/chart/"
            f"{symbol}?range=2y&interval=1d&includePrePost=false&events=history"
        )
        refs.append(path.as_posix())
    return tuple(refs)


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
    if as_of_date.weekday() >= 5:
        return [
            f"run date {as_of_date.isoformat()} is a weekend; latest completed trading date "
            "is resolved by accepted provider bars, not an exchange holiday calendar"
        ]
    return []
