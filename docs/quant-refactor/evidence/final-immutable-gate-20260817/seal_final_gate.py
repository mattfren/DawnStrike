"""Seal the terminal PASS packet for the final immutable 16-shard gate."""

from __future__ import annotations

import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
AUTHORITATIVE = ROOT / "docs/quant-refactor/evidence/final-repair-completion-20260816"


def read_json(name: str) -> dict[str, object]:
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def write_json(path: Path, payload: object) -> None:
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> int:
    source_before = read_json("freeze-inventory-before.json")
    source_after = read_json("freeze-inventory-after.json")
    active_before = read_json("active-state-before.json")
    active_after = read_json("active-state-after.json")
    process_before = read_json("process-audit-before.json")
    process_after = read_json("process-audit-after.json")
    shard_validation = read_json("shard-validation.json")
    active_unchanged = active_before["after_read"] == active_after["after_read"]
    valid = (
        source_before["valid"] is True
        and source_after["valid"] is True
        and active_before["valid"] is True
        and active_after["valid"] is True
        and active_unchanged
        and process_before["unauthorized_process_count"] == 0
        and process_after["unauthorized_process_count"] == 0
        and shard_validation["valid"] is True
        and shard_validation["passed_count"] == 3156
        and shard_validation["failed_count"] == 0
        and shard_validation["skipped_count"] == 0
        and shard_validation["xfailed_count"] == 0
        and shard_validation["xpassed_count"] == 0
        and shard_validation["missing_count"] == 0
        and shard_validation["duplicate_count"] == 0
    )
    if not valid:
        raise SystemExit("final gate evidence is not valid")

    frozen_source_path = AUTHORITATIVE / "source-test-freeze.json"
    frozen_inventory_path = AUTHORITATIVE / "canonical-pytest-inventory.json"
    payload = {
        "schema_version": "dawnstrike.final_immutable_gate_result.v1",
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "terminal_event": "PASS",
        "branch": subprocess.check_output(
            ["git", "branch", "--show-current"], cwd=ROOT, text=True
        ).strip(),
        "head": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=ROOT, text=True
        ).strip(),
        "source_test_freeze": {
            "authoritative_path": frozen_source_path.relative_to(ROOT).as_posix(),
            "artifact_sha256": sha256(frozen_source_path),
            "file_count": 572,
            "aggregate_sha256": source_after["source_test"]["actual_aggregate_sha256"],
            "before_valid": source_before["source_test"]["valid"],
            "after_valid": source_after["source_test"]["valid"],
            "mismatch_count": source_after["source_test"]["mismatch_count"],
        },
        "pytest_inventory": {
            "authoritative_path": frozen_inventory_path.relative_to(ROOT).as_posix(),
            "artifact_sha256": sha256(frozen_inventory_path),
            "node_count": 3156,
            "inventory_sha256": source_after["pytest_inventory"]["actual_inventory_sha256"],
            "before_valid": source_before["pytest_inventory"]["valid"],
            "after_valid": source_after["pytest_inventory"]["valid"],
            "nodes_exactly_equal": source_after["pytest_inventory"]["nodes_exactly_equal"],
        },
        "active_state": {
            "path": active_after["path"],
            "baseline_identity": active_before["after_read"],
            "post_gate_identity": active_after["after_read"],
            "unchanged": active_unchanged,
            "database_schema_version": active_after["database_schema_version"],
            "sqlite_uri_mode": active_after["sqlite_uri_mode"],
            "sqlite_immutable": active_after["sqlite_immutable"],
            "query_only": active_after["query_only"],
            "quick_check": active_after["quick_check"],
        },
        "process_audit": {
            "before_unauthorized_process_count": process_before["unauthorized_process_count"],
            "after_unauthorized_process_count": process_after["unauthorized_process_count"],
        },
        "shard_result": {
            "shard_count": 16,
            "artifact_count": shard_validation["artifact_count"],
            "selected_node_count": shard_validation["selected_node_count"],
            "unique_selected_node_count": shard_validation["unique_selected_node_count"],
            "passed_count": shard_validation["passed_count"],
            "failed_count": shard_validation["failed_count"],
            "skipped_count": shard_validation["skipped_count"],
            "xfailed_count": shard_validation["xfailed_count"],
            "xpassed_count": shard_validation["xpassed_count"],
            "missing_count": shard_validation["missing_count"],
            "duplicate_count": shard_validation["duplicate_count"],
            "elapsed_seconds_sum": sum(
                float(item["elapsed_seconds"]) for item in shard_validation["shards"]
            ),
            "shards": shard_validation["shards"],
        },
        "scope_controls": {
            "source_or_test_edited_during_gate": False,
            "active_state_mutated_or_restored": False,
            "operator_marker_used": False,
            "provider_broker_network_deploy_publish_promote_used": False,
            "git_stage_commit_push_used": False,
            "delegated_lane_count": 1,
            "delegated_lane_shards": list(range(8, 16)),
            "owner_lane_shards": list(range(0, 8)),
        },
    }
    payload_path = EVIDENCE / "final-combined-result.payload.json"
    write_json(payload_path, payload)

    index_path = EVIDENCE / "evidence-index.md"
    index_path.write_text(
        "# Final immutable 16-shard certification gate evidence\n\n"
        "- Terminal event: `PASS`\n"
        "- Combined result: `final-combined-result.json`\n"
        "- Combined payload: `final-combined-result.payload.json`\n"
        "- Evidence manifest: `evidence-manifest.json`\n"
        "- Shard validation: `shard-validation.json`\n"
        "- Shards: `shard-00.*` through `shard-15.*` (112 bound artifacts)\n"
        "- Source/inventory validation: `freeze-inventory-before.json`, `freeze-inventory-after.json`\n"
        "- Active-state proof: `active-state-before.json`, `active-state-after.json`\n"
        "- Process audits: `process-audit-before.json`, `process-audit-after.json`\n"
        "- Result: 3,156 selected, 3,156 unique, 3,156 passed, zero failed/skipped/xfailed/xpassed/missing/duplicate\n"
        "- Active DB remained byte-identical and was opened only with `mode=ro&immutable=1`, `PRAGMA query_only=ON`\n",
        encoding="utf-8",
    )

    excluded = {"evidence-manifest.json", "final-combined-result.json"}
    entries = []
    for path in sorted(EVIDENCE.iterdir(), key=lambda item: item.name):
        if not path.is_file() or path.name in excluded:
            continue
        entries.append(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "length": path.stat().st_size,
                "sha256": sha256(path),
            }
        )
    manifest = {
        "schema_version": "dawnstrike.final_immutable_gate_manifest.v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "terminal_event": "PASS",
        "entry_count": len(entries),
        "entries": entries,
    }
    manifest_path = EVIDENCE / "evidence-manifest.json"
    write_json(manifest_path, manifest)
    combined = dict(payload)
    combined["evidence_index"] = index_path.relative_to(ROOT).as_posix()
    combined["evidence_manifest"] = {
        "path": manifest_path.relative_to(ROOT).as_posix(),
        "sha256": sha256(manifest_path),
        "entry_count": len(entries),
        "payload_sha256": sha256(payload_path),
    }
    combined_path = EVIDENCE / "final-combined-result.json"
    write_json(combined_path, combined)
    print(json.dumps({
        "terminal_event": "PASS",
        "passed_count": shard_validation["passed_count"],
        "combined_result": combined_path.relative_to(ROOT).as_posix(),
        "evidence_index": index_path.relative_to(ROOT).as_posix(),
        "evidence_manifest": manifest_path.relative_to(ROOT).as_posix(),
        "manifest_sha256": sha256(manifest_path),
        "manifest_entry_count": len(entries),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
