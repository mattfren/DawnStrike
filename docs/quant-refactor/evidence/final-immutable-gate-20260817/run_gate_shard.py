"""Run one final-gate shard with immutable source and active-state guards."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
AUTHORITATIVE = ROOT / "docs/quant-refactor/evidence/final-repair-completion-20260816"
sys.path.insert(0, str(Path(__file__).resolve().parent))

from capture_active_state import capture


def source_identity() -> dict[str, object]:
    frozen = json.loads((AUTHORITATIVE / "source-test-freeze.json").read_text(encoding="utf-8"))
    aggregate = hashlib.sha256()
    mismatches = []
    for expected in frozen["files"]:
        relative = expected["path"]
        path = ROOT / relative
        if not path.is_file():
            mismatches.append(relative)
            continue
        content = path.read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        aggregate.update(relative.encode("utf-8"))
        aggregate.update(b"\0")
        aggregate.update(digest.encode("ascii"))
        aggregate.update(b"\n")
        if len(content) != expected["length"] or digest != expected["sha256"]:
            mismatches.append(relative)
    actual = aggregate.hexdigest()
    return {
        "file_count": len(frozen["files"]),
        "expected_aggregate_sha256": frozen["aggregate_sha256"],
        "actual_aggregate_sha256": actual,
        "mismatch_count": len(mismatches),
        "mismatches": mismatches,
        "valid": (
            len(frozen["files"]) == frozen["file_count"] == 572
            and actual == frozen["aggregate_sha256"]
            and not mismatches
        ),
    }


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--evidence-dir", type=Path, required=True)
    parser.add_argument("--active-database", type=Path, required=True)
    parser.add_argument("--active-baseline", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.shard_index < 16:
        parser.error("shard index must be within [0, 16)")

    evidence = args.evidence_dir.resolve()
    evidence.mkdir(parents=True, exist_ok=True)
    prefix = evidence / f"shard-{args.shard_index:02d}"
    baseline = json.loads(args.active_baseline.read_text(encoding="utf-8"))
    baseline_identity = baseline["after_read"]
    source_before = source_identity()
    active_before = capture(args.active_database.resolve())
    preflight_valid = (
        source_before["valid"]
        and active_before["valid"]
        and active_before["after_read"] == baseline_identity
    )
    write_json(prefix.with_suffix(".guard-before.json"), {
        "source": source_before,
        "active_state": active_before,
        "matches_baseline": active_before["after_read"] == baseline_identity,
        "valid": preflight_valid,
    })
    if not preflight_valid:
        return 20

    manifest = prefix.with_suffix(".manifest.json")
    command = [
        sys.executable,
        "scripts/run_pytest_shard.py",
        "--shard-index",
        str(args.shard_index),
        "--shard-count",
        "16",
        "--manifest",
        str(manifest.relative_to(ROOT)),
    ]
    prefix.with_suffix(".command.txt").write_text(
        subprocess.list2cmdline(command) + "\n", encoding="utf-8"
    )
    started = datetime.now(timezone.utc)
    started_clock = time.monotonic()
    with prefix.with_suffix(".stdout.txt").open("w", encoding="utf-8") as stdout, \
         prefix.with_suffix(".stderr.txt").open("w", encoding="utf-8") as stderr:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            check=False,
            stdout=stdout,
            stderr=stderr,
            text=True,
        )
    ended = datetime.now(timezone.utc)

    source_after = source_identity()
    active_after = capture(args.active_database.resolve())
    expected_inventory = json.loads(
        (AUTHORITATIVE / "canonical-pytest-inventory.json").read_text(encoding="utf-8")
    )
    shard_manifest = json.loads(manifest.read_text(encoding="utf-8")) if manifest.is_file() else {}
    expected_nodes = tuple(expected_inventory["nodes"])[args.shard_index::16]
    manifest_valid = (
        shard_manifest.get("schema_version") == "dawnstrike.pytest_shard.v1"
        and shard_manifest.get("shard_index") == args.shard_index
        and shard_manifest.get("shard_count") == 16
        and shard_manifest.get("collected_count") == 3156
        and shard_manifest.get("selected_count") == len(expected_nodes)
        and tuple(shard_manifest.get("selected_nodes", ())) == expected_nodes
    )
    post_guard_valid = (
        source_after["valid"]
        and active_after["valid"]
        and active_after["after_read"] == baseline_identity
        and manifest_valid
    )
    write_json(prefix.with_suffix(".guard-after.json"), {
        "source": source_after,
        "active_state": active_after,
        "matches_baseline": active_after["after_read"] == baseline_identity,
        "manifest_exact": manifest_valid,
        "valid": post_guard_valid,
    })
    result = {
        "schema_version": "dawnstrike.final_gate_shard_exit.v1",
        "shard_index": args.shard_index,
        "started_at_utc": started.isoformat(),
        "ended_at_utc": ended.isoformat(),
        "elapsed_seconds": time.monotonic() - started_clock,
        "pytest_exit_code": completed.returncode,
        "pre_guard_valid": preflight_valid,
        "post_guard_valid": post_guard_valid,
        "exit_code": completed.returncode if post_guard_valid else 20,
    }
    write_json(prefix.with_suffix(".exit.json"), result)
    print(json.dumps(result, sort_keys=True))
    if not post_guard_valid:
        return 20
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
