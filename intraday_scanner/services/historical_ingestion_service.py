"""Historical minute-bar ingestion and snapshot backfill helpers."""

from __future__ import annotations

import csv
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

from intraday_scanner.config import ScannerConfig
from intraday_scanner.errors import SnapshotValidationError
from intraday_scanner.models import validate_required_columns
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.reporting import write_scan_outputs
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.snapshot_builder import MINUTE_BAR_COLUMNS, build_snapshot
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def ingest_minute_bars(
    *,
    input_path: str | Path,
    out_dir: str | Path,
    source_date: str | None = None,
    file_format: str = "csv",
) -> dict[str, Any]:
    source = Path(input_path)
    destination_dir = Path(out_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    if file_format == "parquet":
        rows = _read_parquet_rows(source)
        destination = destination_dir / f"{source.stem}.csv"
        _write_csv(destination, rows)
    else:
        rows = _read_csv_rows(source)
        destination = destination_dir / source.name
        if source.resolve() != destination.resolve():
            shutil.copyfile(source, destination)
    _validate_minute_rows(rows)
    return {
        "source": str(source),
        "output": str(destination),
        "row_count": len(rows),
        "date": source_date or _first_date(rows),
        "fixture_only": True,
    }


def backfill_snapshot_runs(
    *,
    minute_bars: str | Path,
    previous_close: str | Path,
    metadata: str | Path,
    out_dir: str | Path,
    config: ScannerConfig,
    persist: bool = False,
) -> dict[str, Any]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = output_dir / "historical_snapshot.csv"
    snapshots = build_snapshot(minute_bars, previous_close, metadata, snapshot_path)
    store = SQLiteScanStore(config.database_path) if persist else None
    result = ScanService(CsvSnapshotProvider(snapshot_path), store=store).run(
        config, persist=persist
    )
    paths = write_scan_outputs(result, output_dir / "scan")
    return {
        "snapshot_path": str(snapshot_path),
        "scan_paths": {key: str(path) for key, path in paths.items()},
        "snapshot_row_count": len(snapshots),
        "ranked_count": len(result.ranked_candidates),
        "fixture_only": True,
        "no_lookahead_note": (
            "Snapshot rows are built from the supplied minute-bar window; use only bars "
            "available by the configured signal time for point-in-time simulations."
        ),
    }


def _read_csv_rows(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise SnapshotValidationError(f"Minute bars file does not exist: {path}")
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise SnapshotValidationError(f"Minute bars file is empty: {path}")
        validate_required_columns(set(reader.fieldnames), MINUTE_BAR_COLUMNS, str(path))
        return list(reader)


def _read_parquet_rows(path: Path) -> list[dict[str, Any]]:
    try:
        import pandas as pd  # type: ignore[import-untyped]
    except ImportError as exc:
        raise SnapshotValidationError(
            "Parquet ingestion requires pandas/pyarrow installed in the local environment."
        ) from exc
    frame = pd.read_parquet(path)
    rows = frame.to_dict(orient="records")
    validate_required_columns(set(frame.columns), MINUTE_BAR_COLUMNS, str(path))
    return rows


def _write_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=MINUTE_BAR_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def _validate_minute_rows(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        timestamp = str(row.get("timestamp", ""))
        try:
            parsed = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise SnapshotValidationError(f"Invalid minute-bar timestamp: {timestamp}") from exc
        if parsed.tzinfo is None:
            raise SnapshotValidationError(
                f"Minute-bar timestamp must include timezone: {timestamp}"
            )


def _first_date(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    return str(rows[0].get("timestamp", ""))[:10]

