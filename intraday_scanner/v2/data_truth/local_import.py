"""Local CSV OHLCV import provider for DataTruth."""

from __future__ import annotations

import csv
import hashlib
import json
import time
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

from intraday_scanner.v2.data import MarketBar, MarketDataset, validate_dataset, write_ohlcv_csv
from intraday_scanner.v2.data_truth.core import DataTruthPaths
from intraday_scanner.v2.data_truth.models import DataTruthManifest
from intraday_scanner.v2.data_truth.providers import DataTruthProviderSnapshot

TIMESTAMP_COLUMNS = ("timestamp", "datetime", "date")
SYMBOL_COLUMNS = ("symbol", "ticker")
COLUMN_ALIASES = {
    "open": ("open", "o"),
    "high": ("high", "h"),
    "low": ("low", "l"),
    "close": ("close", "adj_close", "adjusted_close", "c"),
    "volume": ("volume", "vol", "v"),
}


@dataclass(frozen=True)
class LocalImportResult:
    snapshot: DataTruthProviderSnapshot
    rejected_bar_count: int
    skipped_incomplete_bars: int

    def to_dict(self) -> dict[str, object]:
        return {
            "manifest": self.snapshot.manifest.to_dict(),
            "normalized_path": self.snapshot.normalized_path.as_posix(),
            "rejected_bar_count": self.rejected_bar_count,
            "skipped_incomplete_bars": self.skipped_incomplete_bars,
            "warnings": list(self.snapshot.warnings),
        }


def import_local_csv_provider(
    *,
    path: Path,
    provider_id: str,
    as_of_date: date,
    output_root: Path = Path("data/v2_data_truth"),
    symbol: str | None = None,
    created_at: datetime | None = None,
) -> LocalImportResult:
    paths = DataTruthPaths.create(output_root)
    source_paths = _csv_paths(path)
    if not source_paths:
        raise FileNotFoundError(f"no CSV files found at {path}")
    now = created_at or datetime.now(timezone.utc)
    bars_by_symbol: dict[str, list[MarketBar]] = {}
    warnings: list[str] = []
    if as_of_date.weekday() >= 5:
        warnings.append(
            f"run date {as_of_date.isoformat()} is a weekend; latest completed trading date "
            "is resolved by imported bars, not an exchange holiday calendar"
        )
    rejected = 0
    skipped_incomplete = 0

    for source_path in source_paths:
        imported_path = paths.imports / source_path.name
        if not imported_path.exists() or _sha256(imported_path) != _sha256(source_path):
            imported_path.write_bytes(source_path.read_bytes())
        with source_path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            header = tuple(reader.fieldnames or ())
            column_map = _column_map(header)
            inferred_symbol = (symbol or _sidecar_symbol(source_path) or source_path.stem).upper()
            if not column_map.get("timestamp"):
                warnings.append(f"{source_path.name}: missing timestamp/date column")
                continue
            missing = [key for key in ("open", "high", "low", "close") if key not in column_map]
            if missing:
                warnings.append(f"{source_path.name}: missing OHLC columns {', '.join(missing)}")
                continue
            seen: set[datetime] = set()
            for row_number, row in enumerate(reader, start=2):
                row_symbol = _row_symbol(row, column_map.get("symbol"), inferred_symbol)
                try:
                    timestamp = _parse_timestamp(row[column_map["timestamp"]])
                    bar = MarketBar(
                        symbol=row_symbol,
                        timestamp=timestamp,
                        open=float(row[column_map["open"]]),
                        high=float(row[column_map["high"]]),
                        low=float(row[column_map["low"]]),
                        close=float(row[column_map["close"]]),
                        volume=int(float(row.get(column_map.get("volume", ""), "0") or 0)),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    rejected += 1
                    warnings.append(
                        f"{source_path.name}:{row_number}: rejected invalid row ({exc})"
                    )
                    continue
                if timestamp.date() >= as_of_date:
                    skipped_incomplete += 1
                    warnings.append(
                        f"{row_symbol}: skipped incomplete daily bar {timestamp.date().isoformat()}"
                    )
                    continue
                if timestamp in seen:
                    rejected += 1
                    warnings.append(f"{row_symbol}: duplicate timestamp {timestamp.isoformat()}")
                    continue
                seen.add(timestamp)
                invalid_reason = _invalid_bar_reason(bar)
                if invalid_reason:
                    rejected += 1
                    warnings.append(
                        f"{row_symbol}: rejected {invalid_reason} at {timestamp.isoformat()}"
                    )
                    continue
                bars_by_symbol.setdefault(row_symbol, []).append(bar)

    dataset = MarketDataset(
        dataset_id=f"{provider_id}_local_csv_1d",
        source_kind=provider_id,
        timeframe="1d",
        bars_by_symbol={
            item_symbol: tuple(sorted(symbol_bars, key=lambda bar: bar.timestamp))
            for item_symbol, symbol_bars in bars_by_symbol.items()
        },
        warnings=tuple(dict.fromkeys(warnings)),
        source_refs=tuple(path.as_posix() for path in source_paths),
    )
    normalized_path = paths.normalized / f"{provider_id}_ohlcv.csv"
    write_ohlcv_csv(dataset, normalized_path)
    validation = validate_dataset(
        dataset,
        min_bars_per_symbol=1,
        max_staleness_days=10,
        as_of=datetime.combine(as_of_date, datetime.min.time(), tzinfo=timezone.utc),
    )
    warnings = list(dict.fromkeys(warnings + list(validation.warnings) + list(validation.issues)))
    accepted_start, accepted_end = _date_range(dataset)
    requested_start, requested_end = _source_date_range(source_paths)
    snapshot_id = f"datatruth_{provider_id}_1d_{accepted_end.replace('-', '')}"
    source_hashes = {source_path.as_posix(): _sha256(source_path) for source_path in source_paths}
    manifest = DataTruthManifest(
        snapshot_id=snapshot_id,
        created_at=now.isoformat(),
        provider_id=provider_id,
        provider_name=f"Local CSV Import ({provider_id})",
        symbols=dataset.symbols,
        timeframe="1d",
        requested_start=requested_start,
        requested_end=requested_end,
        accepted_start=accepted_start,
        accepted_end=accepted_end,
        bar_count=sum(_count_csv_rows(source_path) for source_path in source_paths),
        accepted_bar_count=dataset.total_bars,
        rejected_bar_count=rejected + len(validation.issues),
        skipped_incomplete_bars=skipped_incomplete,
        validation_status="passed_with_warnings" if warnings else "passed",
        warnings=tuple(warnings),
        raw_artifact_hashes=source_hashes,
        normalized_artifact_hash=_sha256(normalized_path),
        source_url_or_reference=tuple(path.as_posix() for path in source_paths),
        code_version="0.1.0",
    )
    _write_json(paths.manifests / f"{snapshot_id}.json", manifest.to_dict())
    _write_json(paths.manifests / f"{provider_id}_latest.json", manifest.to_dict())
    snapshot = DataTruthProviderSnapshot(
        provider_id=provider_id,
        provider_name=manifest.provider_name,
        dataset=dataset,
        manifest=manifest,
        normalized_path=normalized_path,
        source_paths=source_paths,
        warnings=tuple(warnings),
    )
    _write_json(paths.manifests / f"{provider_id}_provider_snapshot.json", snapshot.to_dict())
    return LocalImportResult(
        snapshot=snapshot,
        rejected_bar_count=rejected + len(validation.issues),
        skipped_incomplete_bars=skipped_incomplete,
    )


def _csv_paths(path: Path) -> tuple[Path, ...]:
    if path.is_file():
        return (path,)
    if path.is_dir():
        return tuple(sorted(item for item in path.glob("*.csv") if item.is_file()))
    return ()


def _column_map(header: tuple[str, ...]) -> dict[str, str]:
    normalized = {column.strip().lower(): column for column in header}
    mapping: dict[str, str] = {}
    for timestamp_column in TIMESTAMP_COLUMNS:
        if timestamp_column in normalized:
            mapping["timestamp"] = normalized[timestamp_column]
            break
    for target, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            if alias in normalized:
                mapping[target] = normalized[alias]
                break
    for alias in SYMBOL_COLUMNS:
        if alias in normalized:
            mapping["symbol"] = normalized[alias]
            break
    return mapping


def _row_symbol(row: dict[str, str], symbol_column: str | None, fallback: str) -> str:
    if symbol_column:
        value = row.get(symbol_column)
        if value:
            return value.strip().upper()
    return fallback.strip().upper()


def _sidecar_symbol(path: Path) -> str | None:
    sidecar = path.with_suffix(".json")
    if not sidecar.exists():
        return None
    try:
        payload = json.loads(sidecar.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    if isinstance(payload, dict) and isinstance(payload.get("symbol"), str):
        return str(payload["symbol"]).strip().upper()
    return None


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    timestamp = datetime.fromisoformat(normalized)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)
    return timestamp.astimezone(timezone.utc)


def _invalid_bar_reason(bar: MarketBar) -> str | None:
    if min(bar.open, bar.high, bar.low, bar.close) <= 0:
        return "non-positive OHLC"
    if bar.high < max(bar.open, bar.low, bar.close):
        return "invalid high"
    if bar.low > min(bar.open, bar.high, bar.close):
        return "invalid low"
    if bar.volume < 0:
        return "negative volume"
    return None


def _date_range(dataset: MarketDataset) -> tuple[str, str]:
    timestamps = [bar.timestamp for bars in dataset.bars_by_symbol.values() for bar in bars]
    if not timestamps:
        return "n/a", "n/a"
    return min(timestamps).date().isoformat(), max(timestamps).date().isoformat()


def _source_date_range(paths: tuple[Path, ...]) -> tuple[str, str]:
    dates: list[datetime] = []
    for path in paths:
        with path.open("r", encoding="utf-8", newline="") as handle:
            reader = csv.DictReader(handle)
            mapping = _column_map(tuple(reader.fieldnames or ()))
            timestamp_column = mapping.get("timestamp")
            if not timestamp_column:
                continue
            for row in reader:
                try:
                    dates.append(_parse_timestamp(row[timestamp_column]))
                except (KeyError, ValueError):
                    continue
    if not dates:
        return "n/a", "n/a"
    return min(dates).date().isoformat(), max(dates).date().isoformat()


def _count_csv_rows(path: Path) -> int:
    with path.open("r", encoding="utf-8", newline="") as handle:
        return sum(1 for _ in csv.DictReader(handle))


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


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
