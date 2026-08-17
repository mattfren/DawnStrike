"""Validate and seal the final commit-bound 16-shard gate."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
EVIDENCE_PREFIX = EVIDENCE.relative_to(ROOT).as_posix() + "/"
EXPECTED_HEAD = "fabca37fdcb61c9a2e7825b903ddd456adf1ec85"
EXPECTED_TREE = "08d69b9e7d1dc65745c4d7835541f3b093060921"
EXPECTED_PARENT = "2b5e2d20fe03cdc02005a03ba88c8899a2cdff52"
EXPECTED_BRANCH = "codex/sol-quant-refactor-20260811"
EXPECTED_CHECKOUT = (
    "83bf62c2:5a9d8fa6:616a4992:b5faae95:dd9c71a0:ba59d817:"
    "f972788a:236d07de"
)
EXPECTED_GIT_BLOBS = (
    "8ca0cf1c:266529db:88536bca:9fdf2d69:897fb557:5d9621cb:"
    "587b5bdf:45110a4a"
)
EXPECTED_INVENTORY = (
    "90360b41:ba6b42d5:b8317fe9:7ff95703:7d251a59:f8174e5d:"
    "76799f51:b218b781"
)
PROGRESS_RE = re.compile(r"^([.FEsxX]+)\s+\[\s*\d+%\]\s*$")


def read_json(name: str) -> dict[str, object]:
    payload = json.loads((EVIDENCE / name).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise TypeError(f"expected object in {name}")
    return payload


def write_json(name: str, payload: object) -> Path:
    path = EVIDENCE / name
    path.write_text(json.dumps(payload, sort_keys=True, indent=2) + "\n", encoding="utf-8")
    return path


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True).strip()


def without_time(payload: dict[str, object]) -> dict[str, object]:
    result = dict(payload)
    result.pop("captured_at_utc", None)
    return result


def source_valid(payload: dict[str, object]) -> bool:
    return (
        payload.get("file_count") == 580
        and payload.get("checkout_byte_aggregate_sha256") == EXPECTED_CHECKOUT
        and payload.get("checkout_git_blob_aggregate_sha256") == EXPECTED_GIT_BLOBS
        and payload.get("head_git_blob_aggregate_sha256") == EXPECTED_GIT_BLOBS
        and payload.get("head_commit_oid")
        == "fabca37f:dcb61c9a:2e7825b9:03ddd456:adf1ec85"
        and payload.get("head_tree_oid")
        == "08d69b9e:7d1dc657:45c4d783:5541f3b0:93060921"
        and payload.get("all_checkout_bytes_match_head") is True
    )


def active_valid(payload: dict[str, object]) -> bool:
    before = payload.get("before_read", {})
    after = payload.get("after_read", {})
    return (
        payload.get("valid") is True
        and payload.get("sqlite_uri_mode") == "ro"
        and payload.get("sqlite_immutable") is True
        and payload.get("query_only") == 1
        and payload.get("quick_check") == "ok"
        and payload.get("database_schema_version") == 26
        and before == after
        and before.get("sidecars") == []
    )


def parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def validate_shards(canonical_nodes: tuple[str, ...]) -> dict[str, object]:
    suffixes = (
        ".active-after.json",
        ".active-before.json",
        ".command.txt",
        ".exit.json",
        ".guard-after.json",
        ".guard-before.json",
        ".manifest.json",
        ".process-contract.json",
        ".source-after.json",
        ".source-before.json",
        ".stderr.txt",
        ".stdout.txt",
    )
    all_selected: list[str] = []
    shards = []
    artifacts = []
    errors = []
    active_captures = []
    for index in range(16):
        prefix = EVIDENCE / f"shard-{index:02d}"
        paths = tuple(prefix.with_suffix(suffix) for suffix in suffixes)
        if not all(path.is_file() for path in paths):
            errors.append(f"shard-{index:02d}:missing_artifact")
            continue
        artifacts.extend(
            {
                "path": path.relative_to(ROOT).as_posix(),
                "length": path.stat().st_size,
                "sha256": sha256(path),
            }
            for path in paths
        )
        manifest = read_json(f"shard-{index:02d}.manifest.json")
        exit_data = read_json(f"shard-{index:02d}.exit.json")
        before_guard = read_json(f"shard-{index:02d}.guard-before.json")
        after_guard = read_json(f"shard-{index:02d}.guard-after.json")
        source_before = read_json(f"shard-{index:02d}.source-before.json")
        source_after = read_json(f"shard-{index:02d}.source-after.json")
        active_before = read_json(f"shard-{index:02d}.active-before.json")
        active_after = read_json(f"shard-{index:02d}.active-after.json")
        process_contract = read_json(f"shard-{index:02d}.process-contract.json")
        stdout = (prefix.with_suffix(".stdout.txt")).read_text(encoding="utf-8")
        stderr = (prefix.with_suffix(".stderr.txt")).read_text(encoding="utf-8")
        progress = "".join(
            match.group(1)
            for line in stdout.splitlines()
            if (match := PROGRESS_RE.fullmatch(line.strip()))
        )
        passed = progress.count(".")
        failed = progress.count("F") + progress.count("E")
        skipped = progress.count("s")
        xfailed = progress.count("x")
        xpassed = progress.count("X")
        expected_nodes = canonical_nodes[index::16]
        expected_count = 198 if index < 14 else 197
        selected_nodes = tuple(manifest.get("selected_nodes", ()))
        command = process_contract.get("command", [])
        valid = (
            manifest.get("schema_version") == "dawnstrike.pytest_shard.v1"
            and manifest.get("shard_index") == index
            and manifest.get("shard_count") == 16
            and manifest.get("collected_count") == 3166
            and manifest.get("selected_count") == expected_count == len(expected_nodes)
            and selected_nodes == expected_nodes
            and exit_data.get("exit_code") == 0
            and exit_data.get("pytest_exit_code") == 0
            and exit_data.get("pre_guard_valid") is True
            and exit_data.get("post_guard_valid") is True
            and before_guard.get("valid") is True
            and after_guard.get("valid") is True
            and after_guard.get("manifest_exact") is True
            and source_valid(source_before)
            and source_valid(source_after)
            and active_valid(active_before)
            and active_valid(active_after)
            and process_contract.get("active_database_in_command") is False
            and process_contract.get("active_state_environment") == {}
            and process_contract.get("candidate_persistence_authority") == "none"
            and "scripts/run_pytest_shard.py" in command
            and passed == expected_count
            and failed == skipped == xfailed == xpassed == 0
            and stderr == ""
        )
        if not valid:
            errors.append(f"shard-{index:02d}:invalid_terminal_evidence")
        all_selected.extend(selected_nodes)
        active_captures.extend((active_before, active_after))
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
                "started_at_utc": exit_data.get("started_at_utc"),
                "ended_at_utc": exit_data.get("ended_at_utc"),
                "active_state_changed": exit_data.get("active_state_changed"),
                "active_change_classification": (
                    "AUTHORIZED_EXTERNAL_RUNTIME_DRIFT"
                    if exit_data.get("active_state_changed")
                    else "NO_HASH_DRIFT"
                ),
                "valid": valid,
            }
        )
    counts = Counter(all_selected)
    missing = [node for node in canonical_nodes if counts[node] == 0]
    duplicates = [node for node, count in counts.items() if count > 1]
    passed_total = sum(int(item["passed_count"]) for item in shards)
    valid = (
        not errors
        and len(shards) == 16
        and len(all_selected) == len(counts) == passed_total == 3166
        and not missing
        and not duplicates
    )
    return {
        "schema_version": "dawnstrike.final_commit_shard_validation.v1",
        "validated_at_utc": datetime.now(timezone.utc).isoformat(),
        "canonical_inventory_sha256": EXPECTED_INVENTORY,
        "canonical_node_count": len(canonical_nodes),
        "shard_count": len(shards),
        "artifact_count": len(artifacts),
        "selected_node_count": len(all_selected),
        "unique_selected_node_count": len(counts),
        "passed_count": passed_total,
        "failed_count": sum(int(item["failed_count"]) for item in shards),
        "skipped_count": sum(int(item["skipped_count"]) for item in shards),
        "xfailed_count": sum(int(item["xfailed_count"]) for item in shards),
        "xpassed_count": sum(int(item["xpassed_count"]) for item in shards),
        "missing_count": len(missing),
        "duplicate_count": len(duplicates),
        "missing_nodes": missing,
        "duplicate_nodes": duplicates,
        "errors": errors,
        "shards": shards,
        "artifacts": artifacts,
        "all_active_captures_valid": all(active_valid(item) for item in active_captures),
        "valid": valid,
    }


def git_state() -> dict[str, object]:
    status = subprocess.check_output(
        ["git", "status", "--porcelain=v1", "-z", "--untracked-files=all"],
        cwd=ROOT,
    )
    records = tuple(item.decode("utf-8") for item in status.split(b"\0") if item)
    unexpected = [
        item
        for item in records
        if not (item.startswith("?? ") and item[3:].startswith(EVIDENCE_PREFIX))
    ]
    payload = {
        "schema_version": "dawnstrike.final_commit_git_state.v1",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "branch": git("branch", "--show-current"),
        "head": git("rev-parse", "HEAD"),
        "tree": git("rev-parse", "HEAD^{tree}"),
        "parent": git("rev-parse", "HEAD^"),
        "tracked_worktree_clean": subprocess.run(
            ["git", "diff", "--quiet"], cwd=ROOT, check=False
        ).returncode
        == 0,
        "index_clean": subprocess.run(
            ["git", "diff", "--cached", "--quiet"], cwd=ROOT, check=False
        ).returncode
        == 0,
        "porcelain_record_count": len(records),
        "authorized_evidence_record_count": len(records) - len(unexpected),
        "unexpected_records": unexpected,
    }
    payload["valid"] = (
        payload["branch"] == EXPECTED_BRANCH
        and payload["head"] == EXPECTED_HEAD
        and payload["tree"] == EXPECTED_TREE
        and payload["parent"] == EXPECTED_PARENT
        and payload["tracked_worktree_clean"] is True
        and payload["index_clean"] is True
        and not unexpected
    )
    return payload


def active_attribution(shards: list[dict[str, object]]) -> dict[str, object]:
    active_before = read_json("active-state-before.json")
    active_after = read_json("active-state-after.json")
    task_before = read_json("task-state-before.json")
    task_after = read_json("task-state-after.json")
    task_collision = read_json("task-state-shard-03-preflight-collision.json")
    monitor = read_json("monitor-runtime-evidence-after.json")
    collision = read_json("shard-03-attempt-01.active-before.json")
    task_xml = (EVIDENCE / "scheduled-task-definition.xml").read_text(encoding="utf-16")
    captures = [active_before, active_after]
    for index in range(16):
        captures.extend(
            (
                read_json(f"shard-{index:02d}.active-before.json"),
                read_json(f"shard-{index:02d}.active-after.json"),
            )
        )
    identities = [capture["after_read"] for capture in captures]
    observed_times = sorted({parse_utc(item["mtime_utc"]) for item in identities})
    cadence_aligned = all(time.minute % 5 == 0 and time.second <= 30 for time in observed_times)
    collision_before = collision["before_read"]
    collision_after = collision["after_read"]
    collision_logs = [
        item
        for item in task_collision["matching_runtime_log_files"]
        if datetime(2026, 8, 17, 17, 0, tzinfo=timezone.utc)
        <= parse_utc(item["mtime_utc"])
        <= datetime(2026, 8, 17, 17, 0, 30, tzinfo=timezone.utc)
    ]
    collision_attributed = (
        collision.get("valid") is False
        and collision.get("query_only") == 1
        and collision.get("quick_check") == "ok"
        and collision.get("database_schema_version") == 26
        and collision_before.get("sidecars") == collision_after.get("sidecars") == []
        and collision_before != collision_after
        and parse_utc(task_collision["last_run_time_utc"])
        == datetime(2026, 8, 17, 17, 0, 1, tzinfo=timezone.utc)
        and task_collision.get("last_task_result") == 0
        and task_collision.get("state") == "Ready"
        and len(collision_logs) >= 7
    )
    receipts = monitor.get("receipts", [])
    receipts_valid = (
        monitor.get("task_export_exit_code") == 0
        and len(receipts) == 7
        and all(
            item["payload"].get("exit_code") == 0
            and item["payload"].get("research_only") is True
            and item["payload"].get("broker_execution_enabled") is False
            for item in receipts
        )
    )
    task_valid = (
        task_before.get("enabled") is True
        and task_after.get("enabled") is True
        and task_before.get("state") == task_after.get("state") == "Ready"
        and task_before.get("last_task_result") == task_after.get("last_task_result") == 0
        and parse_utc(task_after["last_run_time_utc"])
        == datetime(2026, 8, 17, 19, 25, 1, tzinfo=timezone.utc)
        and "<Interval>PT5M</Interval>" in task_xml
        and "C:\\r\\dawnstrike-runtime\\scripts\\run_alphaops_monitor.ps1" in task_xml
        and "-StateRoot \"C:\\r\\dawnstrike-state\"" in task_xml
    )
    process_contracts_valid = all(
        read_json(f"shard-{index:02d}.process-contract.json").get(
            "active_database_in_command"
        )
        is False
        and read_json(f"shard-{index:02d}.process-contract.json").get(
            "active_state_environment"
        )
        == {}
        and read_json(f"shard-{index:02d}.process-contract.json").get(
            "candidate_persistence_authority"
        )
        == "none"
        for index in range(16)
    )
    valid = (
        active_valid(active_before)
        and active_valid(active_after)
        and all(active_valid(capture) for capture in captures)
        and cadence_aligned
        and collision_attributed
        and receipts_valid
        and task_valid
        and process_contracts_valid
        and all(item["active_state_changed"] is True for item in shards)
    )
    return {
        "schema_version": "dawnstrike.final_commit_active_state_attribution.v1",
        "classified_at_utc": datetime.now(timezone.utc).isoformat(),
        "classification": (
            "AUTHORIZED_EXTERNAL_RUNTIME_DRIFT" if valid else "UNEXPLAINED_DRIFT"
        ),
        "candidate_hash_drift": False if valid else None,
        "active_database_frozen_claim": False,
        "candidate_non_interference_proven": valid,
        "pre_gate_identity": active_before["after_read"],
        "post_gate_identity": active_after["after_read"],
        "observed_unique_identity_count": len(
            {item["sha256"] for item in identities}
        ),
        "observed_mtime_count": len(observed_times),
        "all_observed_mtimes_align_five_minute_task": cadence_aligned,
        "all_read_only_captures_valid_and_sidecar_free": all(
            active_valid(capture) for capture in captures
        ),
        "scheduled_task": {
            "name": task_after["task_name"],
            "enabled_before": task_before["enabled"],
            "enabled_after": task_after["enabled"],
            "state_after": task_after["state"],
            "last_run_before_utc": task_before["last_run_time_utc"],
            "last_result_before": task_before["last_task_result"],
            "last_run_after_utc": task_after["last_run_time_utc"],
            "last_result_after": task_after["last_task_result"],
            "definition_sha256": monitor["task_xml_sha256"],
            "runner_sha256": monitor["runner_sha256"],
            "five_minute_interval_verified": "<Interval>PT5M</Interval>" in task_xml,
        },
        "final_runtime_receipts": {
            "count": len(receipts),
            "all_exit_zero_research_only_no_broker": receipts_valid,
            "entries": receipts,
        },
        "preflight_collision": {
            "shard_index": 3,
            "pytest_started": False,
            "before_identity": collision_before,
            "after_identity": collision_after,
            "scheduled_task_last_run_utc": task_collision["last_run_time_utc"],
            "scheduled_task_result": task_collision["last_task_result"],
            "matching_runtime_log_count": len(collision_logs),
            "classification": (
                "AUTHORIZED_EXTERNAL_RUNTIME_DRIFT"
                if collision_attributed
                else "UNEXPLAINED_DRIFT"
            ),
            "preserved_evidence_prefix": "shard-03-attempt-01",
        },
        "candidate_process_contract_count": 16,
        "candidate_process_contracts_exclude_active_db_and_persistence": (
            process_contracts_valid
        ),
        "shard_change_classifications": [
            {
                "shard_index": item["shard_index"],
                "changed": item["active_state_changed"],
                "classification": item["active_change_classification"],
            }
            for item in shards
        ],
        "valid": valid,
    }


def main() -> int:
    source_before = read_json("source-test-identity-before.json")
    source_after = read_json("source-test-identity-after.json")
    inventory_before = read_json("canonical-pytest-inventory.json")
    inventory_after = read_json("canonical-pytest-inventory-after.json")
    process_before = read_json("process-audit-before.json")
    process_after = read_json("process-audit-after.json")
    static = read_json("static-gates.json")
    canonical_nodes = tuple(inventory_before["nodes"])
    safety_nodes = [
        node
        for node in canonical_nodes
        if node.startswith(
            (
                "tests/test_active_state_isolation.py::",
                "tests/test_no_persist_sqlite_semantics.py::",
            )
        )
    ]
    shard_validation = validate_shards(canonical_nodes)
    write_json("shard-validation.json", shard_validation)
    git_payload = git_state()
    write_json("git-state.json", git_payload)
    attribution = active_attribution(shard_validation["shards"])
    write_json("active-state-attribution.json", attribution)

    inventory_valid = (
        inventory_before.get("node_count") == inventory_after.get("node_count") == 3166
        and inventory_before.get("inventory_sha256")
        == inventory_after.get("inventory_sha256")
        == EXPECTED_INVENTORY
        and tuple(inventory_after.get("nodes", ())) == canonical_nodes
        and len(canonical_nodes) == len(set(canonical_nodes))
    )
    source_identity_valid = (
        source_valid(source_before)
        and source_valid(source_after)
        and without_time(source_before) == without_time(source_after)
    )
    valid = (
        source_identity_valid
        and inventory_valid
        and shard_validation["valid"] is True
        and git_payload["valid"] is True
        and attribution["valid"] is True
        and process_before.get("unauthorized_process_count") == 0
        and process_after.get("unauthorized_process_count") == 0
        and static.get("all_passed") is True
        and static.get("gate_count") == 8
        and len(safety_nodes) > 0
    )
    if not valid:
        raise SystemExit("final commit gate evidence is not valid")

    payload = {
        "schema_version": "dawnstrike.final_commit_gate_result.v1",
        "sealed_at_utc": datetime.now(timezone.utc).isoformat(),
        "terminal_event": "PASS",
        "branch": git_payload["branch"],
        "head": git_payload["head"],
        "tree": git_payload["tree"],
        "parent": git_payload["parent"],
        "source_test_identity": {
            "path_count": 580,
            "checkout_byte_aggregate_sha256": EXPECTED_CHECKOUT,
            "checkout_git_blob_aggregate_sha256": EXPECTED_GIT_BLOBS,
            "head_git_blob_aggregate_sha256": EXPECTED_GIT_BLOBS,
            "all_checkout_paths_match_head": True,
            "before_after_exact": source_identity_valid,
        },
        "pytest_inventory": {
            "node_count": 3166,
            "unique_node_count": 3166,
            "inventory_sha256": EXPECTED_INVENTORY,
            "before_after_exact": inventory_valid,
            "active_state_isolation_no_persist_contract_node_count": len(safety_nodes),
        },
        "shard_result": {
            key: shard_validation[key]
            for key in (
                "shard_count",
                "artifact_count",
                "selected_node_count",
                "unique_selected_node_count",
                "passed_count",
                "failed_count",
                "skipped_count",
                "xfailed_count",
                "xpassed_count",
                "missing_count",
                "duplicate_count",
                "shards",
            )
        },
        "static_gates": static,
        "git_state": git_payload,
        "process_audit": {
            "before_unauthorized_process_count": process_before[
                "unauthorized_process_count"
            ],
            "after_unauthorized_process_count": process_after[
                "unauthorized_process_count"
            ],
        },
        "active_state": attribution,
        "scope_controls": {
            "source_or_test_edited_during_gate": False,
            "active_state_opened_writable_or_mutated_by_candidate": False,
            "active_state_globally_frozen_claimed": False,
            "external_monitor_drift_classified": True,
            "provider_broker_live_order_deploy_publish_promote_used": False,
            "installed_task_modified_disabled_or_run_by_gate": False,
            "git_stage_commit_push_used": False,
            "delegated_lane_count": 1,
            "delegated_lane_shards": list(range(8, 16)),
            "owner_lane_shards": list(range(0, 8)),
        },
        "commands": {
            "source_identity": (
                "py scripts/capture_source_test_identity.py --output <artifact>"
            ),
            "inventory": (
                "py scripts/capture_pytest_inventory.py --output <artifact>"
            ),
            "shard": (
                "py docs/quant-refactor/evidence/final-commit-gate-20260817/"
                "run_gate_shard.py --shard-index <00-15> --active-database "
                "C:\\r\\dawnstrike-state\\shadow_real.sqlite"
            ),
            "tracked_detect_secrets": (
                "py scripts/run_detect_secrets_tracked.py --baseline .secrets.baseline"
            ),
        },
    }
    payload_path = write_json("final-combined-result.payload.json", payload)
    index_path = EVIDENCE / "evidence-index.md"
    index_path.write_text(
        "# Final commit-bound 16-shard gate evidence\n\n"
        "- Terminal event: `PASS`\n"
        "- Commit: `fabca37fdcb61c9a2e7825b903ddd456adf1ec85`\n"
        "- Combined result: `final-combined-result.json`\n"
        "- Combined payload: `final-combined-result.payload.json`\n"
        "- Evidence manifest: `evidence-manifest.json`\n"
        "- Shard validation: `shard-validation.json`\n"
        "- Git state: `git-state.json`\n"
        "- Source identities: `source-test-identity-before.json`, "
        "`source-test-identity-after.json`\n"
        "- Inventories: `canonical-pytest-inventory.json`, "
        "`canonical-pytest-inventory-after.json`\n"
        "- Active-state attribution: `active-state-attribution.json`\n"
        "- Scheduled task and receipts: `scheduled-task-definition.xml`, "
        "`monitor-runtime-evidence-after.json`, `task-state-*.json`\n"
        "- Preserved failed-closed no-pytest probe: `shard-03-attempt-01.*`\n"
        "- Process audits: `process-audit-before.json`, `process-audit-after.json`\n"
        "- Static gates: `static-gates.json`, `static-*.stdout.txt`, "
        "`static-*.stderr.txt`\n"
        "- Shards: `shard-00.*` through `shard-15.*`\n"
        "- Result: 3,166 selected, 3,166 unique, 3,166 passed; zero "
        "failed/skipped/xfailed/xpassed/missing/duplicate.\n"
        "- Active DB was never claimed frozen. Every candidate probe was "
        "immutable read-only and sidecar-free; changes align to the enabled "
        "five-minute external monitor and are classified "
        "`AUTHORIZED_EXTERNAL_RUNTIME_DRIFT`.\n",
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
        "schema_version": "dawnstrike.final_commit_gate_manifest.v1",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "terminal_event": "PASS",
        "entry_count": len(entries),
        "entries": entries,
    }
    manifest_path = write_json("evidence-manifest.json", manifest)
    combined = dict(payload)
    combined["evidence_index"] = index_path.relative_to(ROOT).as_posix()
    combined["evidence_manifest"] = {
        "path": manifest_path.relative_to(ROOT).as_posix(),
        "sha256": sha256(manifest_path),
        "entry_count": len(entries),
        "payload_sha256": sha256(payload_path),
    }
    combined_path = write_json("final-combined-result.json", combined)
    print(
        json.dumps(
            {
                "terminal_event": "PASS",
                "passed_count": shard_validation["passed_count"],
                "selected_node_count": shard_validation["selected_node_count"],
                "unique_selected_node_count": shard_validation[
                    "unique_selected_node_count"
                ],
                "combined_result": combined_path.relative_to(ROOT).as_posix(),
                "combined_result_sha256": sha256(combined_path),
                "evidence_manifest": manifest_path.relative_to(ROOT).as_posix(),
                "manifest_sha256": sha256(manifest_path),
                "manifest_entry_count": len(entries),
                "active_state_classification": attribution["classification"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
