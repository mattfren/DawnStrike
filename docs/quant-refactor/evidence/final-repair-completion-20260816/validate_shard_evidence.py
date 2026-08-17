"""Validate exact 16-shard coverage and terminal pytest outcomes."""

from __future__ import annotations

import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


EVIDENCE = Path(__file__).resolve().parent
PROGRESS_RE = re.compile(r"^([.FEsxX]+)\s+\[\s*\d+%\]\s*$")


def main() -> int:
    canonical = json.loads(
        (EVIDENCE / "canonical-pytest-inventory.json").read_text(encoding="utf-8")
    )
    canonical_nodes = tuple(canonical["nodes"])
    all_selected: list[str] = []
    shards = []
    errors = []
    for index in range(16):
        prefix = EVIDENCE / f"shard-{index:02d}"
        required_paths = tuple(
            prefix.with_suffix(suffix)
            for suffix in (".manifest.json", ".exit.json", ".stdout.txt", ".stderr.txt")
        )
        if not all(path.is_file() for path in required_paths):
            errors.append(f"shard-{index:02d}:missing_evidence")
            shards.append(
                {
                    "shard_index": index,
                    "selected_count": 0,
                    "passed_count": 0,
                    "failed_count": 0,
                    "skipped_count": 0,
                    "xfailed_count": 0,
                    "xpassed_count": 0,
                    "exit_code": None,
                    "status": "not_run",
                    "valid": False,
                }
            )
            continue
        manifest = json.loads(prefix.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        exit_data = json.loads(prefix.with_suffix(".exit.json").read_text(encoding="utf-8"))
        stdout = prefix.with_suffix(".stdout.txt").read_text(encoding="utf-8")
        stderr = prefix.with_suffix(".stderr.txt").read_text(encoding="utf-8")
        expected_nodes = canonical_nodes[index::16]
        selected_nodes = tuple(manifest["selected_nodes"])
        progress = "".join(
            match.group(1)
            for line in stdout.splitlines()
            if (match := PROGRESS_RE.fullmatch(line.strip()))
        )
        dot_count = progress.count(".")
        excluded_count = len(progress) - dot_count
        valid = (
            manifest["schema_version"] == "dawnstrike.pytest_shard.v1"
            and manifest["shard_index"] == index
            and manifest["shard_count"] == 16
            and manifest["collected_count"] == 3156
            and manifest["selected_count"] == len(expected_nodes)
            and selected_nodes == expected_nodes
            and exit_data["exit_code"] == 0
            and dot_count == len(expected_nodes)
            and excluded_count == 0
            and not stderr
        )
        if not valid:
            errors.append(f"shard-{index:02d}:terminal_evidence_invalid")
        all_selected.extend(selected_nodes)
        shards.append(
            {
                "shard_index": index,
                "selected_count": len(selected_nodes),
                "passed_count": dot_count,
                "failed_count": progress.count("F") + progress.count("E"),
                "skipped_count": progress.count("s"),
                "xfailed_count": progress.count("x"),
                "xpassed_count": progress.count("X"),
                "exit_code": exit_data["exit_code"],
                "status": "pass" if valid else "failed",
                "valid": valid,
            }
        )
    counts = Counter(all_selected)
    missing = [node for node in canonical_nodes if counts[node] == 0]
    duplicates = [node for node, count in counts.items() if count > 1]
    coverage_digest = hashlib.sha256(
        ("\n".join(all_selected) + "\n").encode("utf-8")
    ).hexdigest()
    valid = not errors and not missing and not duplicates and len(all_selected) == 3156
    payload = {
        "schema_version": "dawnstrike.pytest_shard_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "canonical_inventory_sha256": canonical["inventory_sha256"],
        "canonical_node_count": len(canonical_nodes),
        "selected_node_count": len(all_selected),
        "unique_selected_node_count": len(counts),
        "missing_count": len(missing),
        "duplicate_count": len(duplicates),
        "missing_nodes": missing,
        "duplicate_nodes": duplicates,
        "shard_error_count": len(errors),
        "shard_errors": errors,
        "ordered_shard_selection_sha256": coverage_digest,
        "shards": shards,
        "valid": valid,
    }
    print(json.dumps(payload, sort_keys=True, indent=2))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
