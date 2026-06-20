"""CSV and JSON output helpers."""

from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

from intraday_scanner.models import CANDIDATE_COLUMNS, ScanResult, ScoredCandidate


def write_scan_outputs(result: ScanResult, out_dir: str | Path) -> dict[str, Path]:
    output_dir = Path(out_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    paths = {
        "ranked_candidates": output_dir / "ranked_candidates.csv",
        "top_explosive": output_dir / "top_explosive.csv",
        "avoid_list": output_dir / "avoid_list.csv",
        "summary": output_dir / "scan_summary.json",
    }
    _write_candidates(paths["ranked_candidates"], result.ranked_candidates)
    _write_candidates(paths["top_explosive"], result.top_explosive)
    _write_candidates(paths["avoid_list"], result.avoid_list)
    paths["summary"].write_text(
        json.dumps({"summary": result.summary(), "config": result.config}, indent=2),
        encoding="utf-8",
    )
    return paths


def _write_candidates(path: Path, candidates: list[ScoredCandidate]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=CANDIDATE_COLUMNS)
        writer.writeheader()
        for candidate in candidates:
            writer.writerow(candidate.to_dict())


def read_csv_dicts(path: str | Path) -> list[dict[str, Any]]:
    csv_path = Path(path)
    if not csv_path.exists():
        return []
    with csv_path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def read_scan_summary(path: str | Path) -> dict[str, Any]:
    summary_path = Path(path)
    if not summary_path.exists():
        return {}
    return json.loads(summary_path.read_text(encoding="utf-8"))
