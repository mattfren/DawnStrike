"""Provider snapshot contracts for DataTruth evidence hardening."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from intraday_scanner.v2.data import MarketDataset
from intraday_scanner.v2.data_truth.models import DataTruthManifest


@dataclass(frozen=True)
class DataTruthProviderSnapshot:
    provider_id: str
    provider_name: str
    dataset: MarketDataset
    manifest: DataTruthManifest
    normalized_path: Path
    source_paths: tuple[Path, ...]
    warnings: tuple[str, ...] = ()
    schema_version: str = "v2.data_truth_provider_snapshot.v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "dataset_id": self.dataset.dataset_id,
            "manifest": self.manifest.to_dict(),
            "normalized_path": self.normalized_path.as_posix(),
            "provider_id": self.provider_id,
            "provider_name": self.provider_name,
            "schema_version": self.schema_version,
            "source_paths": [path.as_posix() for path in self.source_paths],
            "warnings": list(self.warnings),
        }
