"""Canonical DataTruth trust classification."""

from __future__ import annotations

from dataclasses import dataclass

from intraday_scanner.v2.data_truth.models import DataTruthManifest, DataTruthReconciliationReport


@dataclass(frozen=True)
class CanonicalDataDecision:
    canonical_snapshot_id: str
    status: str
    allow_forward: bool
    reason: str
    warnings: tuple[str, ...]
    schema_version: str = "v2.canonical_data_decision.v1"

    def to_dict(self) -> dict[str, object]:
        return {
            "allow_forward": self.allow_forward,
            "canonical_snapshot_id": self.canonical_snapshot_id,
            "reason": self.reason,
            "schema_version": self.schema_version,
            "status": self.status,
            "warnings": list(self.warnings),
        }


def classify_canonical_data(
    *,
    manifest: DataTruthManifest,
    reconciliation: DataTruthReconciliationReport,
    allow_single_provider: bool = True,
    allow_mismatch: bool = False,
) -> CanonicalDataDecision:
    status = reconciliation.status
    warnings = tuple(dict.fromkeys(manifest.warnings + reconciliation.warnings))
    if manifest.provider_id == "synthetic":
        return CanonicalDataDecision(
            canonical_snapshot_id=manifest.snapshot_id,
            status="synthetic_blocked",
            allow_forward=False,
            reason="synthetic data is not allowed for forward PaperOps",
            warnings=warnings,
        )
    if status in {"reconciled", "reconciled_with_minor_diffs"}:
        return CanonicalDataDecision(
            canonical_snapshot_id=manifest.snapshot_id,
            status=status,
            allow_forward=True,
            reason="provider data is reconciled within configured tolerances",
            warnings=warnings,
        )
    if status == "single_provider_unreconciled":
        return CanonicalDataDecision(
            canonical_snapshot_id=manifest.snapshot_id,
            status=status,
            allow_forward=allow_single_provider,
            reason=(
                "single-provider data allowed by explicit config"
                if allow_single_provider
                else "single-provider data blocked by config"
            ),
            warnings=warnings + ("single-provider data is not independently reconciled",),
        )
    if status == "mismatch":
        return CanonicalDataDecision(
            canonical_snapshot_id=manifest.snapshot_id,
            status=status,
            allow_forward=allow_mismatch,
            reason=(
                "provider mismatch allowed only by explicit override"
                if allow_mismatch
                else "provider mismatch blocks forward PaperOps"
            ),
            warnings=warnings,
        )
    return CanonicalDataDecision(
        canonical_snapshot_id=manifest.snapshot_id,
        status=status,
        allow_forward=False,
        reason=f"canonical status {status} blocks forward PaperOps",
        warnings=warnings,
    )
