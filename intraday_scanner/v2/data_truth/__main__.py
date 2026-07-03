"""CLI for the v2 DataTruth evidence layer."""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

from intraday_scanner.v2.data_truth import (
    build_data_truth_snapshot,
    import_local_csv_provider,
    load_datatruth_dataset,
    reconcile_datasets_v2,
    write_reconciliation_v2,
)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Dawnstrike v2 DataTruth")
    parser.add_argument("command", choices=("build", "import-csv", "summary"))
    parser.add_argument("--date", default=date.today().isoformat())
    parser.add_argument("--output-root", default="data/v2_data_truth")
    parser.add_argument("--no-fetch", action="store_true")
    parser.add_argument("--path")
    parser.add_argument("--provider-id", default="local_csv")
    parser.add_argument("--symbol")
    args = parser.parse_args(argv)

    output_root = Path(args.output_root)
    run_date = date.fromisoformat(args.date)
    if args.command == "build":
        result = build_data_truth_snapshot(
            as_of_date=run_date,
            output_root=output_root,
            allow_fetch=not args.no_fetch,
        )
        print(f"DataTruth snapshot: {result.manifest.snapshot_id}")
        print(f"Accepted bars: {result.manifest.accepted_bar_count}")
        print(f"Reconciliation: {result.reconciliation.status}")
        if result.warnings:
            print("Warnings:")
            for warning in result.warnings[:8]:
                print(f"- {warning}")
        return 0
    if args.command == "import-csv":
        if not args.path:
            parser.error("import-csv requires --path")
        import_result = import_local_csv_provider(
            path=Path(args.path),
            provider_id=args.provider_id,
            as_of_date=run_date,
            output_root=output_root,
            symbol=args.symbol,
        )
        print(f"Imported snapshot: {import_result.snapshot.manifest.snapshot_id}")
        print(f"Accepted bars: {import_result.snapshot.manifest.accepted_bar_count}")
        print(f"Rejected bars: {import_result.rejected_bar_count}")
        print(f"Skipped incomplete bars: {import_result.skipped_incomplete_bars}")
        try:
            canonical_dataset, canonical_manifest = load_datatruth_dataset(output_root=output_root)
        except (FileNotFoundError, KeyError):
            canonical_dataset = import_result.snapshot.dataset
            canonical_manifest = import_result.snapshot.manifest
            comparison_datasets = {}
        else:
            comparison_datasets = {
                import_result.snapshot.provider_id: import_result.snapshot.dataset
            }
        reconciliation = reconcile_datasets_v2(
            canonical_dataset=canonical_dataset,
            comparison_datasets=comparison_datasets,
            canonical_snapshot_id=canonical_manifest.snapshot_id,
            canonical_provider_id=canonical_manifest.provider_id,
        )
        artifacts = write_reconciliation_v2(result=reconciliation, output_root=output_root)
        print(f"Reconciliation: {reconciliation.report.status}")
        print(f"Forward blocked: {reconciliation.block_forward}")
        print(f"Diff: {artifacts['diff']}")
        _write_import_summary(output_root, import_result.to_dict(), reconciliation.to_dict())
        return 0
    summary_path = output_root / "reports" / "data_truth_summary.md"
    if not summary_path.exists():
        print("DataTruth summary does not exist; run build first.")
        return 1
    print(summary_path.read_text(encoding="utf-8"))
    return 0


def _write_import_summary(
    output_root: Path,
    import_payload: dict[str, object],
    reconciliation_payload: dict[str, object],
) -> None:
    reports = output_root / "reports"
    reports.mkdir(parents=True, exist_ok=True)
    manifest = import_payload["manifest"]
    assert isinstance(manifest, dict)
    report = reconciliation_payload["report"]
    assert isinstance(report, dict)
    lines = [
        "# DataTruth Evidence Hardening Summary",
        "",
        f"- Imported snapshot: `{manifest['snapshot_id']}`",
        f"- Provider: `{manifest['provider_id']}`",
        f"- Accepted bars: `{manifest['accepted_bar_count']}`",
        f"- Rejected bars: `{import_payload['rejected_bar_count']}`",
        f"- Skipped incomplete bars: `{import_payload['skipped_incomplete_bars']}`",
        f"- Reconciliation status: `{report['status']}`",
        "",
        "## Warnings",
        "",
    ]
    warnings = manifest.get("warnings", [])
    if isinstance(warnings, list) and warnings:
        lines.extend(f"- {warning}" for warning in warnings)
    else:
        lines.append("- None.")
    (reports / "data_truth_summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    raise SystemExit(main())
