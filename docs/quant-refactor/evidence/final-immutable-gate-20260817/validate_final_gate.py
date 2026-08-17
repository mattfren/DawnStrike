"""Validate the exact terminal evidence for all 16 immutable pytest shards."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
AUTHORITATIVE = ROOT / "docs/quant-refactor/evidence/final-repair-completion-20260816"
PROGRESS_RE = re.compile(r"^([.FEsxX]+)\s+\[\s*\d+%\]\s*$")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    canonical = json.loads(
        (AUTHORITATIVE / "canonical-pytest-inventory.json").read_text(encoding="utf-8")
    )
    canonical_nodes = tuple(canonical["nodes"])
    all_selected: list[str] = []
    shards = []
    errors = []
    artifact_entries = []
    suffixes = (
        ".command.txt",
        ".stdout.txt",
        ".stderr.txt",
        ".manifest.json",
        ".guard-before.json",
        ".guard-after.json",
        ".exit.json",
    )
    for index in range(16):
        prefix = EVIDENCE / f"shard-{index:02d}"
        paths = tuple(prefix.with_suffix(suffix) for suffix in suffixes)
        if not all(path.is_file() for path in paths):
            errors.append(f"shard-{index:02d}:missing_artifact")
            continue
        for path in paths:
            artifact_entries.append(
                {
                    "path": path.relative_to(ROOT).as_posix(),
                    "length": path.stat().st_size,
                    "sha256": sha256(path),
                }
            )
        manifest = json.loads(prefix.with_suffix(".manifest.json").read_text(encoding="utf-8"))
        exit_data = json.loads(prefix.with_suffix(".exit.json").read_text(encoding="utf-8"))
        before = json.loads(prefix.with_suffix(".guard-before.json").read_text(encoding="utf-8"))
        after = json.loads(prefix.with_suffix(".guard-after.json").read_text(encoding="utf-8"))
        stdout = prefix.with_suffix(".stdout.txt").read_text(encoding="utf-8")
        stderr = prefix.with_suffix(".stderr.txt").read_text(encoding="utf-8")
        progress = "".join(
            match.group(1)
            for line in stdout.splitlines()
            if (match := PROGRESS_RE.fullmatch(line.strip()))
        )
        expected_nodes = canonical_nodes[index::16]
        selected_nodes = tuple(manifest.get("selected_nodes", ()))
        passed = progress.count(".")
        failed = progress.count("F") + progress.count("E")
        skipped = progress.count("s")
        xfailed = progress.count("x")
        xpassed = progress.count("X")
        expected_count = 198 if index < 4 else 197
        valid = (
            manifest.get("schema_version") == "dawnstrike.pytest_shard.v1"
            and manifest.get("shard_index") == index
            and manifest.get("shard_count") == 16
            and manifest.get("collected_count") == 3156
            and manifest.get("selected_count") == expected_count == len(expected_nodes)
            and selected_nodes == expected_nodes
            and exit_data.get("exit_code") == 0
            and exit_data.get("pytest_exit_code") == 0
            and exit_data.get("pre_guard_valid") is True
            and exit_data.get("post_guard_valid") is True
            and before.get("valid") is True
            and after.get("valid") is True
            and passed == expected_count
            and failed == skipped == xfailed == xpassed == 0
            and stderr == ""
        )
        if not valid:
            errors.append(f"shard-{index:02d}:invalid_terminal_evidence")
        all_selected.extend(selected_nodes)
        shards.append(
            {
                "shard_index": index,
                "selected_count": len(selected_nodes),
                "passed_count": passed,
                "failed_count": failed,
                "skipped_count": skipped,
                "xfailed_count": xfailed,
                "xpassed_count": xpassed,
                "pytest_exit_code": exit_data.get("pytest_exit_code"),
                "elapsed_seconds": exit_data.get("elapsed_seconds"),
                "valid": valid,
            }
        )
    counts = Counter(all_selected)
    missing = [node for node in canonical_nodes if counts[node] == 0]
    duplicates = [node for node, count in counts.items() if count > 1]
    passed_total = sum(item["passed_count"] for item in shards)
    valid = (
        not errors
        and len(shards) == 16
        and len(all_selected) == len(counts) == passed_total == 3156
        and not missing
        and not duplicates
    )
    payload = {
        "schema_version": "dawnstrike.final_immutable_gate_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "canonical_inventory_sha256": canonical["inventory_sha256"],
        "canonical_node_count": len(canonical_nodes),
        "shard_count": len(shards),
        "artifact_count": len(artifact_entries),
        "selected_node_count": len(all_selected),
        "unique_selected_node_count": len(counts),
        "passed_count": passed_total,
        "failed_count": sum(item["failed_count"] for item in shards),
        "skipped_count": sum(item["skipped_count"] for item in shards),
        "xfailed_count": sum(item["xfailed_count"] for item in shards),
        "xpassed_count": sum(item["xpassed_count"] for item in shards),
        "missing_count": len(missing),
        "duplicate_count": len(duplicates),
        "missing_nodes": missing,
        "duplicate_nodes": duplicates,
        "errors": errors,
        "shards": shards,
        "artifacts": artifact_entries,
        "valid": valid,
    }
    rendered = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({
        "valid": valid,
        "shard_count": len(shards),
        "artifact_count": len(artifact_entries),
        "selected_node_count": len(all_selected),
        "unique_selected_node_count": len(counts),
        "passed_count": passed_total,
        "failed_count": payload["failed_count"],
        "skipped_count": payload["skipped_count"],
        "xfailed_count": payload["xfailed_count"],
        "xpassed_count": payload["xpassed_count"],
        "missing_count": len(missing),
        "duplicate_count": len(duplicates),
    }, sort_keys=True))
    return 0 if valid else 2


if __name__ == "__main__":
    raise SystemExit(main())
