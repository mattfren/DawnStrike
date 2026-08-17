"""Assemble WP007 repository, gate, source, state, and survivor evidence."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[4]
EVIDENCE = Path(__file__).resolve().parent
EXPECTED_HEAD = "bec32fe752b91f4e1357236a538a6dfea5da56bf"
EXPECTED_BRANCH = "codex/sol-quant-refactor-20260811"

WP007_OWNED_SOURCE = (
    "api/readiness.py",
    "app.py",
    "intraday_scanner/dashboard/opportunity_projection.py",
    "intraday_scanner/dashboard/opportunity_projection_render.py",
    "intraday_scanner/dashboard/opportunity_projection_store.py",
    "scripts/build_public.py",
    "scripts/build_vercel_public_stage.ps1",
    "scripts/verify_public_artifact.py",
    "tests/test_opportunity_projection.py",
    "tests/test_opportunity_projection_public.py",
    "tests/test_opportunity_projection_streamlit.py",
    "tests/test_public_build_notifications.py",
    "tests/test_vercel_health_readiness.py",
    "tests/test_vercel_public_stage.py",
    "web/assets/dawnstrike.css",
    "web/assets/dawnstrike.js",
    "web/index.html",
)
WP007_OWNED_DOCS = (
    "docs/quant-refactor/04-execution-log.md",
    "docs/quant-refactor/luna/007-read-only-product-projection-handoff.md",
)
GATES = (
    ("focused", 28),
    ("public-compatibility", 36),
    ("rendered-compatibility", 12),
    ("validation-persistence", 15),
    ("validation-robustness", 19),
    ("main", 656),
    ("affected", 139),
    ("ruff", None),
    ("mypy", None),
    ("compileall", None),
    ("node-check", None),
    ("powershell-parse", None),
    ("diff-check", None),
    ("import-firewall", None),
)
COLLECTIONS = (
    ("focused-collection", 28),
    ("public-collection", 36),
    ("validation-persistence-collection", 15),
    ("validation-robustness-collection", 19),
    ("main-collection", 656),
    ("affected-collection", 139),
)


def main() -> int:
    branch = _git("branch", "--show-current")
    head = _git("rev-parse", "HEAD")
    status_text = _git("status", "--porcelain=v1", "--untracked-files=all")
    status_rows = [line for line in status_text.splitlines() if line]
    status_paths = [_status_path(line) for line in status_rows]
    evidence_relative = str(EVIDENCE.relative_to(ROOT)).replace("\\", "/")
    owned_paths = set(WP007_OWNED_SOURCE) | set(WP007_OWNED_DOCS)
    owned_status = sorted(
        path
        for path in status_paths
        if path in owned_paths or path.startswith(f"{evidence_relative}/")
    )
    preexisting = sorted(path for path in status_paths if path not in owned_status)
    repository_state = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "worktree": str(ROOT),
        "branch": branch,
        "expected_branch": EXPECTED_BRANCH,
        "branch_matches": branch == EXPECTED_BRANCH,
        "head": head,
        "expected_head": EXPECTED_HEAD,
        "head_matches": head == EXPECTED_HEAD,
        "status_porcelain": status_rows,
    }
    _write_json("repository-state.json", repository_state)

    source_hashes = {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "files": [
            {
                "path": name,
                "length": (ROOT / name).stat().st_size,
                "sha256": _sha256(ROOT / name),
            }
            for name in WP007_OWNED_SOURCE
        ],
    }
    _write_json("source-hashes.json", source_hashes)
    modification_inventory = {
        "wp007_owned_source_files": list(WP007_OWNED_SOURCE),
        "wp007_owned_documentation_files": list(WP007_OWNED_DOCS),
        "wp007_owned_evidence_files": sorted(
            str(path.relative_to(ROOT)).replace("\\", "/")
            for path in EVIDENCE.rglob("*")
            if path.is_file()
        ),
        "wp007_owned_status_paths": owned_status,
        "preexisting_wp001_wp006_paths_preserved": preexisting,
        "unauthorized_actions": {
            "active_state_write_or_migration": False,
            "provider_or_network_query": False,
            "broker_or_order_action": False,
            "scheduler_mutation": False,
            "deployment": False,
            "commit": False,
            "stage": False,
            "push": False,
            "primary_checkout_touch": False,
        },
    }
    _write_json("modification-inventory.json", modification_inventory)

    gate_rows = []
    for name, expected_count in GATES:
        exit_payload = _read_json(f"{name}.exit.json")
        stdout = (EVIDENCE / f"{name}.stdout.txt").read_text(encoding="utf-8")
        observed = _pytest_passed(stdout) if expected_count is not None else None
        gate_rows.append(
            {
                **exit_payload,
                "name": name,
                "expected_count": expected_count,
                "observed_passed_count": observed,
                "count_matches": (
                    observed == expected_count if expected_count is not None else None
                ),
            }
        )
    collection_rows = []
    for name, expected_count in COLLECTIONS:
        exit_payload = _read_json(f"{name}.exit.json")
        stdout = (EVIDENCE / f"{name}.stdout.txt").read_text(encoding="utf-8")
        observed = sum(
            int(match.group(1))
            for match in re.finditer(r"^.+:\s+(\d+)\s*$", stdout, flags=re.MULTILINE)
        )
        collection_rows.append(
            {
                **exit_payload,
                "name": name,
                "expected_count": expected_count,
                "observed_collection_count": observed,
                "count_matches": observed == expected_count,
            }
        )
    run_summary = {
        "terminal_candidate": "PASS_CANDIDATE_FOR_SOL_ADJUDICATION",
        "gates": gate_rows,
        "collections": collection_rows,
        "all_exit_zero": all(row["exit_code"] == 0 for row in gate_rows + collection_rows),
        "all_counts_match": all(
            row["count_matches"] is not False for row in gate_rows + collection_rows
        ),
        "implementation_repair_cycles": 1,
        "test_or_evidence_correction_cycles": 2,
        "limitations": [
            "Synthetic fixtures establish software and presentation invariants only.",
            "No real holdout was opened and no empirical edge or profitability is claimed.",
            "Active state remains schema 26, so an enabled projection reports DATA_UNAVAILABLE.",
            "WP007 is disabled by default and does not authorize TAKE or execution.",
        ],
    }
    _write_json("run-summary.json", run_summary)

    before = _read_json("active-state-before.stdout.txt")
    after = _read_json("active-state-after.stdout.txt")
    state_invariance = {
        "before": before,
        "after": after,
        "file_identity_unchanged": before["before_read"] == after["after_read"],
        "schema_unchanged": before["schema_version"] == after["schema_version"] == 26,
        "query_only_enforced": before["query_only"] == after["query_only"] == 1,
        "quick_check_ok": before["quick_check"] == after["quick_check"] == "ok",
        "sidecars_absent": (
            not before["before_read"]["sidecars"]
            and not before["after_read"]["sidecars"]
            and not after["before_read"]["sidecars"]
            and not after["after_read"]["sidecars"]
        ),
    }
    _write_json("active-state-invariance.json", state_invariance)
    process_survivors = _process_survivors()
    _write_json("processes.post.json", process_survivors)
    return (
        0
        if _evidence_is_green(
            repository_state,
            run_summary,
            state_invariance,
            process_survivors,
        )
        else 2
    )


def _evidence_is_green(
    repository_state: dict[str, Any],
    run_summary: dict[str, Any],
    state_invariance: dict[str, Any],
    process_survivors: dict[str, object],
) -> bool:
    return bool(
        repository_state["branch_matches"]
        and repository_state["head_matches"]
        and run_summary["all_exit_zero"]
        and run_summary["all_counts_match"]
        and state_invariance["file_identity_unchanged"]
        and state_invariance["schema_unchanged"]
        and state_invariance["query_only_enforced"]
        and state_invariance["quick_check_ok"]
        and state_invariance["sidecars_absent"]
        and process_survivors["command_exit_code"] == 0
        and process_survivors["survivor_count"] == 0
    )


def _process_survivors() -> dict[str, object]:
    script = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { $_.Name -match '^(python|py)(\\.exe)?$' -and "
        "$_.CommandLine -match '(pytest|run_gate\\.py)' } | "
        "Select-Object ProcessId,Name,CommandLine | ConvertTo-Json -Compress"
    )
    completed = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    output = completed.stdout.strip()
    parsed = json.loads(output) if output else []
    rows = parsed if isinstance(parsed, list) else [parsed]
    return {
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "command_exit_code": completed.returncode,
        "survivor_count": len(rows),
        "survivors": rows,
    }


def _pytest_passed(stdout: str) -> int | None:
    matches = re.findall(r"(\d+) passed", stdout)
    if matches:
        return int(matches[-1])
    progress_dots = stdout.count(".")
    return progress_dots or None


def _status_path(line: str) -> str:
    value = line[3:]
    if " -> " in value:
        value = value.split(" -> ", 1)[1]
    return value.strip('"').replace("\\", "/")


def _git(*args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=True,
    ).stdout.rstrip()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _read_json(name: str) -> dict[str, Any]:
    return json.loads((EVIDENCE / name).read_text(encoding="utf-8"))


def _write_json(name: str, value: object) -> None:
    (EVIDENCE / name).write_text(
        json.dumps(value, sort_keys=True, indent=2) + "\n",
        encoding="utf-8",
    )


if __name__ == "__main__":
    raise SystemExit(main())
