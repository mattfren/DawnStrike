"""Run one commit-bound shard with immutable source/inventory and active-state guards."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
CAPTURE_ACTIVE = (
    ROOT
    / "docs/quant-refactor/evidence/final-immutable-gate-20260817/capture_active_state.py"
)


def read_json(path: Path) -> dict[str, object]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"expected object in {path}")
    return payload


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def capture_source(path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, "scripts/capture_source_test_identity.py", "--output", str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = read_json(path) if path.is_file() else {}
    payload["capture_exit_code"] = completed.returncode
    payload["capture_stdout"] = completed.stdout
    payload["capture_stderr"] = completed.stderr
    return payload


def capture_active(database: Path, path: Path) -> dict[str, object]:
    completed = subprocess.run(
        [sys.executable, str(CAPTURE_ACTIVE), str(database), "--output", str(path)],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    payload = read_json(path) if path.is_file() else {}
    payload["capture_exit_code"] = completed.returncode
    payload["capture_stdout"] = completed.stdout
    payload["capture_stderr"] = completed.stderr
    return payload


def source_valid(payload: dict[str, object], frozen: dict[str, object]) -> bool:
    return (
        payload.get("capture_exit_code") == 0
        and payload.get("file_count") == 580
        and payload.get("head_commit_oid") == frozen.get("head_commit_oid")
        and payload.get("head_tree_oid") == frozen.get("head_tree_oid")
        and payload.get("checkout_byte_aggregate_sha256")
        == frozen.get("checkout_byte_aggregate_sha256")
        and payload.get("checkout_git_blob_aggregate_sha256")
        == frozen.get("checkout_git_blob_aggregate_sha256")
        and payload.get("head_git_blob_aggregate_sha256")
        == frozen.get("head_git_blob_aggregate_sha256")
        and payload.get("all_checkout_bytes_match_head") is True
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--shard-index", type=int, required=True)
    parser.add_argument("--active-database", type=Path, required=True)
    args = parser.parse_args()
    if not 0 <= args.shard_index < 16:
        parser.error("shard index must be within [0, 16)")

    index = args.shard_index
    prefix = EVIDENCE / f"shard-{index:02d}"
    frozen_source = read_json(EVIDENCE / "source-test-identity-before.json")
    inventory = read_json(EVIDENCE / "canonical-pytest-inventory.json")
    expected_nodes = tuple(inventory["nodes"])[index::16]

    source_before = capture_source(prefix.with_suffix(".source-before.json"))
    active_before = capture_active(
        args.active_database.resolve(), prefix.with_suffix(".active-before.json")
    )
    pre_guard_valid = (
        source_valid(source_before, frozen_source)
        and active_before.get("capture_exit_code") == 0
        and active_before.get("valid") is True
        and not active_before["after_read"]["sidecars"]
        and inventory.get("node_count") == 3166
        and inventory.get("inventory_sha256")
        == "90360b41:ba6b42d5:b8317fe9:7ff95703:7d251a59:f8174e5d:76799f51:b218b781"
        and len(expected_nodes) == (198 if index < 14 else 197)
    )
    write_json(
        prefix.with_suffix(".guard-before.json"),
        {
            "schema_version": "dawnstrike.final_commit_gate_shard_guard.v1",
            "source_valid": source_valid(source_before, frozen_source),
            "active_state_valid": active_before.get("valid") is True,
            "expected_selected_count": len(expected_nodes),
            "valid": pre_guard_valid,
        },
    )
    if not pre_guard_valid:
        return 20

    manifest = prefix.with_suffix(".manifest.json")
    command = [
        sys.executable,
        "scripts/run_pytest_shard.py",
        "--shard-index",
        str(index),
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
    environment = {
        key: value
        for key, value in sorted(os.environ.items())
        if key.upper()
        in {
            "DAWNSTRIKE_DB_PATH",
            "DAWNSTRIKE_STATE_ROOT",
            "DATABASE_URL",
            "PYTEST_ADDOPTS",
        }
    }
    write_json(
        prefix.with_suffix(".process-contract.json"),
        {
            "schema_version": "dawnstrike.final_commit_gate_process_contract.v1",
            "command": command,
            "cwd": str(ROOT),
            "active_database": str(args.active_database.resolve()),
            "active_database_in_command": str(args.active_database.resolve()).lower()
            in " ".join(command).lower(),
            "active_state_environment": environment,
            "candidate_persistence_authority": "none",
        },
    )
    stdout_path = prefix.with_suffix(".stdout.txt")
    stderr_path = prefix.with_suffix(".stderr.txt")
    with stdout_path.open("w", encoding="utf-8") as stdout, stderr_path.open(
        "w", encoding="utf-8"
    ) as stderr:
        completed = subprocess.run(
            command, cwd=ROOT, check=False, stdout=stdout, stderr=stderr, text=True
        )
    ended = datetime.now(timezone.utc)

    source_after = capture_source(prefix.with_suffix(".source-after.json"))
    active_after = capture_active(
        args.active_database.resolve(), prefix.with_suffix(".active-after.json")
    )
    shard_manifest = read_json(manifest) if manifest.is_file() else {}
    manifest_valid = (
        shard_manifest.get("schema_version") == "dawnstrike.pytest_shard.v1"
        and shard_manifest.get("shard_index") == index
        and shard_manifest.get("shard_count") == 16
        and shard_manifest.get("collected_count") == 3166
        and shard_manifest.get("selected_count") == len(expected_nodes)
        and tuple(shard_manifest.get("selected_nodes", ())) == expected_nodes
    )
    post_guard_valid = (
        source_valid(source_after, frozen_source)
        and active_after.get("capture_exit_code") == 0
        and active_after.get("valid") is True
        and not active_after["after_read"]["sidecars"]
        and manifest_valid
    )
    active_changed = active_before.get("after_read") != active_after.get("after_read")
    write_json(
        prefix.with_suffix(".guard-after.json"),
        {
            "schema_version": "dawnstrike.final_commit_gate_shard_guard.v1",
            "source_valid": source_valid(source_after, frozen_source),
            "active_state_valid": active_after.get("valid") is True,
            "manifest_exact": manifest_valid,
            "active_state_changed_during_shard_window": active_changed,
            "active_change_classification": (
                "PENDING_EXTERNAL_RUNTIME_ATTRIBUTION" if active_changed else "NO_HASH_DRIFT"
            ),
            "valid": post_guard_valid,
        },
    )
    result = {
        "schema_version": "dawnstrike.final_commit_gate_shard_exit.v1",
        "shard_index": index,
        "started_at_utc": started.isoformat(),
        "ended_at_utc": ended.isoformat(),
        "elapsed_seconds": time.monotonic() - started_clock,
        "pytest_exit_code": completed.returncode,
        "pre_guard_valid": pre_guard_valid,
        "post_guard_valid": post_guard_valid,
        "active_state_changed": active_changed,
        "exit_code": completed.returncode if post_guard_valid else 20,
    }
    write_json(prefix.with_suffix(".exit.json"), result)
    print(json.dumps(result, sort_keys=True))
    return int(result["exit_code"])


if __name__ == "__main__":
    raise SystemExit(main())
