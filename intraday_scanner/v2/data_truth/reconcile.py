"""Provider reconciliation v2 for DataTruth."""

from __future__ import annotations

import csv
import json
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from intraday_scanner.v2.data import MarketBar, MarketDataset
from intraday_scanner.v2.data_truth.core import DataTruthPaths
from intraday_scanner.v2.data_truth.models import (
    DataTruthReconciliationReport,
    ProviderDisagreement,
)


@dataclass(frozen=True)
class ReconciliationTolerances:
    price_abs_tolerance: float = 0.01
    price_minor_abs_tolerance: float = 0.05
    price_bps_tolerance: float = 5.0
    price_minor_bps_tolerance: float = 25.0
    volume_pct_tolerance: float = 0.05
    min_overlap_rows: int = 1


@dataclass(frozen=True)
class ReconciliationV2Result:
    report: DataTruthReconciliationReport
    diff_rows: tuple[dict[str, object], ...]
    canonical_status: str
    block_forward: bool
    schema_version: str = "v2.data_truth_reconciliation_result.v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "block_forward": self.block_forward,
            "canonical_status": self.canonical_status,
            "diff_rows": list(self.diff_rows),
            "report": self.report.to_dict(),
            "schema_version": self.schema_version,
        }


def reconcile_datasets_v2(
    *,
    canonical_dataset: MarketDataset,
    comparison_datasets: dict[str, MarketDataset],
    canonical_snapshot_id: str,
    canonical_provider_id: str,
    tolerances: ReconciliationTolerances | None = None,
    created_at: datetime | None = None,
) -> ReconciliationV2Result:
    active_tolerances = tolerances or ReconciliationTolerances()
    now = created_at or datetime.now(timezone.utc)
    if not comparison_datasets:
        report = DataTruthReconciliationReport(
            reconciliation_id=f"{canonical_snapshot_id}:reconciliation_v2",
            created_at=now.isoformat(),
            canonical_snapshot_id=canonical_snapshot_id,
            provider_count=1,
            status="single_provider_unreconciled",
            canonical_provider_id=canonical_provider_id,
            compared_provider_ids=(),
            disagreements=(),
            warnings=("only one provider snapshot is available",),
        )
        return ReconciliationV2Result(
            report=report,
            diff_rows=(),
            canonical_status="single_provider_unreconciled",
            block_forward=False,
        )

    disagreements: list[ProviderDisagreement] = []
    diff_rows: list[dict[str, object]] = []
    overlap_count = 0
    material_count = 0
    minor_count = 0
    for provider_id, dataset in sorted(comparison_datasets.items()):
        other_index = _bar_index(dataset)
        for symbol, bars in canonical_dataset.bars_by_symbol.items():
            for canonical_bar in bars:
                key = (symbol, canonical_bar.timestamp.date().isoformat())
                other = other_index.get(key)
                if other is None:
                    continue
                overlap_count += 1
                for field_name in ("open", "high", "low", "close"):
                    canonical_value = float(getattr(canonical_bar, field_name))
                    other_value = float(getattr(other, field_name))
                    severity = _price_severity(
                        canonical_value,
                        other_value,
                        active_tolerances,
                    )
                    if severity == "match":
                        continue
                    if severity == "minor":
                        minor_count += 1
                    else:
                        material_count += 1
                    disagreements.append(
                        ProviderDisagreement(
                            symbol=symbol,
                            timestamp=canonical_bar.timestamp.isoformat(),
                            field_name=field_name,
                            canonical_value=canonical_value,
                            other_value=other_value,
                            provider_id=provider_id,
                            tolerance=active_tolerances.price_abs_tolerance,
                            severity=severity,
                        )
                    )
                    diff_rows.append(
                        _diff_row(
                            provider_id,
                            symbol,
                            canonical_bar,
                            field_name,
                            canonical_value,
                            other_value,
                            severity,
                        )
                    )
                volume_severity = _volume_severity(
                    canonical_bar.volume,
                    other.volume,
                    active_tolerances.volume_pct_tolerance,
                )
                if volume_severity != "match":
                    if volume_severity == "minor":
                        minor_count += 1
                    else:
                        material_count += 1
                    disagreements.append(
                        ProviderDisagreement(
                            symbol=symbol,
                            timestamp=canonical_bar.timestamp.isoformat(),
                            field_name="volume",
                            canonical_value=canonical_bar.volume,
                            other_value=other.volume,
                            provider_id=provider_id,
                            tolerance=active_tolerances.volume_pct_tolerance,
                            severity=volume_severity,
                        )
                    )
                    diff_rows.append(
                        _diff_row(
                            provider_id,
                            symbol,
                            canonical_bar,
                            "volume",
                            float(canonical_bar.volume),
                            float(other.volume),
                            volume_severity,
                        )
                    )

    warnings: tuple[str, ...]
    if overlap_count < active_tolerances.min_overlap_rows:
        status = "insufficient_overlap"
        warnings = ("provider datasets have insufficient symbol/date overlap",)
    elif material_count:
        status = "mismatch"
        warnings = ("material provider differences exceeded tolerances",)
    elif minor_count:
        status = "reconciled_with_minor_diffs"
        warnings = ("minor provider differences were within configured tolerance",)
    else:
        status = "reconciled"
        warnings = ()
    report = DataTruthReconciliationReport(
        reconciliation_id=f"{canonical_snapshot_id}:reconciliation_v2",
        created_at=now.isoformat(),
        canonical_snapshot_id=canonical_snapshot_id,
        provider_count=1 + len(comparison_datasets),
        status=status,
        canonical_provider_id=canonical_provider_id,
        compared_provider_ids=tuple(sorted(comparison_datasets)),
        disagreements=tuple(disagreements),
        warnings=warnings,
    )
    return ReconciliationV2Result(
        report=report,
        diff_rows=tuple(diff_rows),
        canonical_status=status,
        block_forward=status in {"mismatch", "insufficient_overlap", "provider_error"},
    )


def write_reconciliation_v2(
    *,
    result: ReconciliationV2Result,
    output_root: Path = Path("data/v2_data_truth"),
) -> dict[str, str]:
    paths = DataTruthPaths.create(output_root)
    snapshot_id = result.report.canonical_snapshot_id
    json_path = paths.reconciliation / "latest_reconciliation.json"
    md_path = paths.reconciliation / "latest_reconciliation.md"
    diff_path = paths.reconciliation / f"provider_diff_{snapshot_id}.csv"
    _write_json(json_path, result.to_dict())
    _write_markdown(md_path, result)
    _write_diff_csv(diff_path, result.diff_rows)
    return {
        "diff": diff_path.as_posix(),
        "json": json_path.as_posix(),
        "markdown": md_path.as_posix(),
    }


def _bar_index(dataset: MarketDataset) -> dict[tuple[str, str], MarketBar]:
    return {
        (symbol, bar.timestamp.date().isoformat()): bar
        for symbol, bars in dataset.bars_by_symbol.items()
        for bar in bars
    }


def _price_severity(
    canonical_value: float,
    other_value: float,
    tolerances: ReconciliationTolerances,
) -> str:
    diff = abs(canonical_value - other_value)
    bps = diff / canonical_value * 10_000 if canonical_value else 0.0
    if diff <= tolerances.price_abs_tolerance or bps <= tolerances.price_bps_tolerance:
        return "match"
    if diff <= tolerances.price_minor_abs_tolerance or bps <= tolerances.price_minor_bps_tolerance:
        return "minor"
    return "material"


def _volume_severity(canonical_volume: int, other_volume: int, tolerance: float) -> str:
    if canonical_volume == other_volume:
        return "match"
    base = max(abs(canonical_volume), 1)
    pct = abs(canonical_volume - other_volume) / base
    return "minor" if pct <= tolerance else "material"


def _diff_row(
    provider_id: str,
    symbol: str,
    canonical_bar: MarketBar,
    field_name: str,
    canonical_value: float,
    other_value: float,
    severity: str,
) -> dict[str, object]:
    return {
        "canonical_value": canonical_value,
        "date": canonical_bar.timestamp.date().isoformat(),
        "field_name": field_name,
        "other_value": other_value,
        "provider_id": provider_id,
        "severity": severity,
        "symbol": symbol,
    }


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


def _write_markdown(path: Path, result: ReconciliationV2Result) -> None:
    report = result.report
    lines = [
        "# DataTruth Reconciliation v2",
        "",
        f"- Status: `{report.status}`",
        f"- Canonical snapshot: `{report.canonical_snapshot_id}`",
        f"- Canonical provider: `{report.canonical_provider_id}`",
        f"- Compared providers: {', '.join(report.compared_provider_ids) or 'none'}",
        f"- Forward blocked: `{result.block_forward}`",
        "",
        "## Warnings",
        "",
    ]
    lines.extend(f"- {warning}" for warning in report.warnings or ("None.",))
    lines.extend(["", "## Differences", ""])
    if result.diff_rows:
        for row in result.diff_rows[:50]:
            lines.append(
                f"- {row['symbol']} {row['date']} {row['field_name']}: "
                f"{row['canonical_value']} vs {row['other_value']} ({row['severity']})"
            )
    else:
        lines.append("- None.")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_diff_csv(path: Path, rows: tuple[dict[str, object], ...]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fields = (
        "provider_id",
        "symbol",
        "date",
        "field_name",
        "canonical_value",
        "other_value",
        "severity",
    )
    temp_path = path.with_name(f".{path.name}.tmp")
    with temp_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            writer.writerow({field: row.get(field, "") for field in fields})
    _replace_with_retry(temp_path, path)
