"""Write a durable observer-only process configuration without registering it."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(*, plan: Path, output: Path, repo_root: Path, runner: Path) -> dict[str, Any]:
    value = json.loads(plan.read_text(encoding="utf-8"))
    if value.get("schema_version") != "dawnstrike.observation.cohort.v1":
        raise ValueError("cohort plan schema is unsupported")
    if value.get("safety", {}).get("no_auto_renewal") is not True:
        raise ValueError("cohort plan does not prohibit renewal")
    command = [
        str(value["toolchain"]["path"]),
        str(runner.resolve()),
        "resume",
        "--output-root",
        str(plan.parent.resolve()),
        "--input-root",
        str(value["input_root"]),
        "--scope-root",
        str(value["scope_root"]),
        "--repo-root",
        str(repo_root.resolve()),
        "--python",
        str(value["toolchain"]["path"]),
        "--execute",
    ]
    job = {
        "schema_version": "dawnstrike.observation.cohort_job.v1",
        "status": "PREPARED_ONLY",
        "job_id": hashlib.sha256(
            json.dumps(command, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()[:24],
        "trigger": {
            "kind": "after_existing_capture_task_completion",
            "task_name": "Dawnstrike Delayed SIP Capture",
            "task_action": "cmd.exe /c C:\\r\\dawnstrike-ops\\run_sip_capture.cmd",
            "observed_next_run": "2026-09-10T15:20:00-05:00",
            "minimum_source_age_minutes": 15,
        },
        "command": command,
        "finite_horizon": {
            "expected_sessions": value["expected_session_count"],
            "no_auto_renewal": True,
            "hard_stop_after_final_session": True,
        },
        "isolation": {
            "observer_only": True,
            "research_only": True,
            "broker_execution_enabled": False,
            "orders_enabled": False,
            "task_registration_performed": False,
            "provider_call_performed": False,
        },
        "plan_path": str(plan.resolve()),
        "plan_sha256": _sha256(plan),
        "producer_reduction_mode": str(
            value.get("producer_reduction_mode") or "bounded_derivative"
        ),
        "runner_path": str(runner.resolve()),
        "runner_sha256": _sha256(runner),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_bytes(json.dumps(job, sort_keys=True, indent=2).encode("utf-8") + b"\n")
    return {
        "status": job["status"],
        "path": str(output),
        "sha256": _sha256(output),
        "job_id": job["job_id"],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, required=True)
    parser.add_argument("--runner", type=Path, required=True)
    args = parser.parse_args()
    try:
        print(json.dumps(build(**vars(args)), sort_keys=True))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"status": "BLOCKED", "reason": str(exc)}, sort_keys=True))
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
