"""Validate the final source/test freeze and canonical pytest inventory without refresh."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
AUTHORITATIVE = ROOT / "docs/quant-refactor/evidence/final-repair-completion-20260816"
def source_validation() -> dict[str, object]:
    frozen = json.loads((AUTHORITATIVE / "source-test-freeze.json").read_text(encoding="utf-8"))
    frozen_paths = tuple(item["path"] for item in frozen["files"])
    aggregate = hashlib.sha256()
    mismatches = []
    for expected in frozen["files"]:
        relative = expected["path"]
        path = ROOT / relative
        if not path.is_file():
            mismatches.append({"path": relative, "reason": "missing"})
            continue
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
        if len(content) != expected["length"] or digest != expected["sha256"]:
            mismatches.append({"path": relative, "reason": "identity_mismatch"})
    actual = aggregate.hexdigest()
    valid = (
        len(frozen_paths) == frozen["file_count"] == 572
        and actual == frozen["aggregate_sha256"]
        and not mismatches
    )
    return {
        "file_count": len(frozen_paths),
        "expected_aggregate_sha256": frozen["aggregate_sha256"],
        "actual_aggregate_sha256": actual,
        "frozen_path_order_exact": tuple(sorted(frozen_paths)) == frozen_paths,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "valid": valid,
    }


def inventory_validation() -> dict[str, object]:
    canonical = json.loads(
        (AUTHORITATIVE / "canonical-pytest-inventory.json").read_text(encoding="utf-8")
    )
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "--collect-only",
            "-q",
            "-o",
            "addopts=",
            "-p",
            "no:cacheprovider",
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    nodes = tuple(
        line.strip()
        for line in completed.stdout.splitlines()
        if "::" in line and not line.startswith(("=", " "))
    )
    digest = hashlib.sha256(("\n".join(nodes) + "\n").encode("utf-8")).hexdigest()
    expected_nodes = tuple(canonical["nodes"])
    valid = (
        completed.returncode == 0
        and len(nodes) == canonical["node_count"] == 3156
        and nodes == expected_nodes
        and digest == canonical["inventory_sha256"]
    )
    return {
        "collect_exit_code": completed.returncode,
        "node_count": len(nodes),
        "expected_inventory_sha256": canonical["inventory_sha256"],
        "actual_inventory_sha256": digest,
        "nodes_exactly_equal": nodes == expected_nodes,
        "stderr": completed.stderr,
        "valid": valid,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    source = source_validation()
    inventory = inventory_validation()
    payload = {
        "schema_version": "dawnstrike.final_gate_freeze_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "source_test": source,
        "pytest_inventory": inventory,
        "valid": source["valid"] and inventory["valid"],
    }
    rendered = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    return 0 if payload["valid"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
