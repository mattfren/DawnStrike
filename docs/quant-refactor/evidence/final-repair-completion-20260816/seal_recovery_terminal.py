"""Seal the post-automation recovery terminal packet."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def read_stdout(name: str) -> dict[str, object]:
    return read_json(EVIDENCE / f"{name}.stdout.txt")


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    source_before = read_stdout("recovery-source-freeze-before")
    source_after = read_stdout("recovery-source-freeze-after")
    inventory_before = read_stdout("recovery-inventory-before")
    inventory_after = read_stdout("recovery-inventory-after")
    active_before = read_stdout("recovery-active-state-before")
    active_after = read_stdout("recovery-active-state-after")
    process_before = read_stdout("recovery-process-audit-before")
    process_after = read_stdout("recovery-process-audit-after")
    shard_validation = read_stdout("recovery-shard-validation")
    shard_05_exit = read_json(EVIDENCE / "shard-05.exit.json")

    before_identity = active_before["after_read"]
    after_identity = active_after["after_read"]
    active_unchanged = before_identity == after_identity
    passing_shards = [
        item["shard_index"]
        for item in shard_validation["shards"]
        if item["status"] == "pass"
    ]
    passed_count = sum(item["passed_count"] for item in shard_validation["shards"])
    failed_count = sum(item["failed_count"] for item in shard_validation["shards"])
    skipped_count = sum(item["skipped_count"] for item in shard_validation["shards"])
    xfailed_count = sum(item["xfailed_count"] for item in shard_validation["shards"])
    xpassed_count = sum(item["xpassed_count"] for item in shard_validation["shards"])

    payload = {
        "schema_version": "dawnstrike.final_repair_recovery_result.v1",
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "terminal_event": "REPAIR_REQUIRED",
        "terminal_reason": "shard_test_failure",
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "frozen_source_test": {
            "file_count": source_after["file_count"],
            "aggregate_sha256": source_after["actual_aggregate_sha256"],
            "before_valid": source_before["valid"],
            "after_valid": source_after["valid"],
            "before_mismatch_count": source_before["mismatch_count"],
            "after_mismatch_count": source_after["mismatch_count"],
        },
        "frozen_pytest_inventory": {
            "node_count": inventory_after["actual_node_count"],
            "inventory_sha256": inventory_after["actual_inventory_sha256"],
            "before_valid": inventory_before["valid"],
            "after_valid": inventory_after["valid"],
            "before_nodes_exactly_equal": inventory_before["nodes_exactly_equal"],
            "after_nodes_exactly_equal": inventory_after["nodes_exactly_equal"],
        },
        "active_state": {
            "path": active_after["path"],
            "baseline_identity": before_identity,
            "post_run_identity": after_identity,
            "unchanged": active_unchanged,
            "database_schema_version": active_after["database_schema_version"],
            "sqlite_uri_mode": active_after["sqlite_uri_mode"],
            "sqlite_immutable": active_after["sqlite_immutable"],
            "query_only": active_after["query_only"],
            "quick_check": active_after["quick_check"],
        },
        "process_audit": {
            "before_unauthorized_process_count": process_before[
                "unauthorized_process_count"
            ],
            "after_unauthorized_process_count": process_after[
                "unauthorized_process_count"
            ],
        },
        "shard_result": {
            "sealed_passing_shards": passing_shards,
            "sealed_passing_shard_count": len(passing_shards),
            "fresh_executed_shards": [5],
            "not_run_after_terminal_failure": [6, 7],
            "selected_node_count_with_manifests": shard_validation[
                "selected_node_count"
            ],
            "passed_count": passed_count,
            "failed_count": failed_count,
            "skipped_count": skipped_count,
            "xfailed_count": xfailed_count,
            "xpassed_count": xpassed_count,
            "missing_count": shard_validation["missing_count"],
            "duplicate_count": shard_validation["duplicate_count"],
            "shard_05": next(
                item
                for item in shard_validation["shards"]
                if item["shard_index"] == 5
            ),
            "shard_05_elapsed_seconds": shard_05_exit["elapsed_seconds"],
            "shard_05_stdout_sha256": sha256(EVIDENCE / "shard-05.stdout.txt"),
            "failure_node": "tests/test_opportunity_discovery_metrics.py::test_multi_session_recall_is_exact_micro_aggregate[1-0.333333333333]",
            "failure_exception": "ValueError: outcome horizon session does not match symbol source series",
            "failure_source": "intraday_scanner/v2/opportunity/outcome_replay.py:185",
        },
        "scope_controls": {
            "source_or_test_edited": False,
            "active_state_mutated_or_restored": False,
            "operator_marker_used": False,
            "provider_broker_deploy_publish_promote_used": False,
            "git_stage_commit_push_used": False,
            "shards_06_07_skipped_due_to_terminal_failure": True,
        },
    }
    payload_path = EVIDENCE / "recovery-combined-result.payload.json"
    write_json(payload_path, payload)

    index_path = EVIDENCE / "recovery-evidence-index.md"
    index_path.write_text(
        "# Final repair post-automation recovery evidence index\n\n"
        "- Terminal event: `REPAIR_REQUIRED`\n"
        "- Combined payload: `recovery-combined-result.payload.json`\n"
        "- Final combined result: `recovery-combined-result.json`\n"
        "- Evidence manifest: `recovery-evidence-manifest.json`\n"
        "- Fresh shard 05: `shard-05.command.txt`, `shard-05.stdout.txt`, "
        "`shard-05.stderr.txt`, `shard-05.exit.json`, `shard-05.manifest.json`\n"
        "- Failure: `tests/test_opportunity_discovery_metrics.py::"
        "test_multi_session_recall_is_exact_micro_aggregate[1-0.333333333333]`\n"
        "- Shards 06-07: not run after the terminal shard-05 failure\n"
        "- Shard validation: `recovery-shard-validation.stdout.txt`\n"
        "- Active-state before/after: `recovery-active-state-before.stdout.txt`, "
        "`recovery-active-state-after.stdout.txt`\n"
        "- Source freeze before/after: `recovery-source-freeze-before.stdout.txt`, "
        "`recovery-source-freeze-after.stdout.txt`\n"
        "- Inventory before/after: `recovery-inventory-before.stdout.txt`, "
        "`recovery-inventory-after.stdout.txt`\n"
        "- Process audit before/after: `recovery-process-audit-before.stdout.txt`, "
        "`recovery-process-audit-after.stdout.txt`\n",
        encoding="utf-8",
    )

    excluded = {
        "recovery-evidence-manifest.json",
        "recovery-combined-result.json",
    }
    entries = []
    for path in sorted(EVIDENCE.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name in excluded or path.name.startswith("recovery-seal."):
            continue
        entries.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "length": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    manifest = {
        "schema_version": "dawnstrike.final_repair_recovery_manifest.v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "terminal_event": "REPAIR_REQUIRED",
        "entry_count": len(entries),
        "entries": entries,
    }
    manifest_path = EVIDENCE / "recovery-evidence-manifest.json"
    write_json(manifest_path, manifest)
    manifest_sha256 = sha256(manifest_path)

    combined = dict(payload)
    combined["evidence_index"] = index_path.relative_to(ROOT).as_posix()
    combined["evidence_manifest"] = {
        "path": manifest_path.relative_to(ROOT).as_posix(),
        "sha256": manifest_sha256,
        "entry_count": len(entries),
        "payload_sha256": sha256(payload_path),
    }
    combined_path = EVIDENCE / "recovery-combined-result.json"
    write_json(combined_path, combined)
    print(
        json.dumps(
            {
                "terminal_event": "REPAIR_REQUIRED",
                "combined_result": combined_path.relative_to(ROOT).as_posix(),
                "evidence_index": index_path.relative_to(ROOT).as_posix(),
                "evidence_manifest": manifest_path.relative_to(ROOT).as_posix(),
                "manifest_sha256": manifest_sha256,
                "manifest_entry_count": len(entries),
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
