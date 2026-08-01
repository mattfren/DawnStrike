"""Build Dawnstrike's framework-free public site from the canonical snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.services.daily_finalize_service import DailyFinalizeService
from intraday_scanner.services.daily_run_service import release_manifest_payload
from intraday_scanner.services.scheduler_doctor_service import scheduler_doctor
from intraday_scanner.services.v6_learning_service import v6_public_status
from intraday_scanner.storage.migrations import get_schema_version
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db-path", default="data/shadow_real.sqlite")
    parser.add_argument(
        "--state-root",
        default=None,
        help="Approved durable state root for database and PaperOps state.",
    )
    parser.add_argument("--paper-ops-root", default="data/v2_paper_ops_live")
    parser.add_argument("--out-dir", default="build/public")
    parser.add_argument("--date", default=None)
    parser.add_argument("--retry-limit", type=int, default=2)
    parser.add_argument("--retry-delay-seconds", type=int, default=0)
    parser.add_argument(
        "--result-out",
        default=None,
        help="Private daily-finalize receipt path; it is never written into the public artifact.",
    )
    parser.add_argument("--deployment-url", default=None)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Build a diagnostic artifact from a dirty checkout; never use for deployment.",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    configured_state_root = (
        Path(args.state_root).resolve() if args.state_root else None
    )
    try:
        if configured_state_root is None:
            db_path = _resolve_repository_database(root, args.db_path)
        else:
            db_path = _resolve_repository_database(
                root,
                args.db_path,
                state_root=configured_state_root,
            )
    except ValueError as exc:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": "database_path_outside_approved_boundary",
                    "message": str(exc),
                    "next_action": (
                        "Use a database under the clean runtime or the explicit "
                        "durable state root."
                    ),
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 2
    source = _source_metadata(root)
    if source.get("source_clean") is not True and not args.allow_dirty:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": "source_not_clean",
                    "source_sha": source.get("source_sha"),
                    "next_action": "Commit the candidate, then rebuild from the clean SHA.",
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 2
    output_root = (root / args.out_dir).resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    _clear_known_outputs(output_root)
    market_date = args.date or datetime.now().date().isoformat()
    paper_ops_root = Path(args.paper_ops_root)
    if not paper_ops_root.is_absolute():
        paper_ops_root = root / paper_ops_root
    state_root = configured_state_root or db_path.parent.resolve()
    result = DailyFinalizeService(
        db_path,
        output_root,
        paper_ops_root=paper_ops_root,
        runtime_root=root,
        state_root=state_root,
        release_sha=str(source.get("source_sha") or ""),
    ).run(
        market_date=market_date,
        retry_limit=max(0, args.retry_limit),
        retry_delay_seconds=max(0, args.retry_delay_seconds),
    )
    shutil.copy2(root / "web" / "index.html", output_root / "index.html")
    shutil.copy2(root / "web" / "favicon.svg", output_root / "favicon.svg")
    shutil.copytree(root / "web" / "assets", output_root / "assets", dirs_exist_ok=True)
    readiness = (
        result.get("readiness")
        if isinstance(result.get("readiness"), dict)
        else {}
    )
    performance_hash = str(readiness.get("payload_sha256") or "")
    publication_set_hash = str(
        readiness.get("publication_set_sha256") or performance_hash
    )
    build_sha = hashlib.sha256(
        (
            f"{source.get('source_sha')}:{publication_set_hash}:"
            f"{market_date}"
        ).encode()
    ).hexdigest()
    build_id = build_sha[:20]
    scheduler = scheduler_doctor(root, state_root=state_root)
    readiness["scheduler"] = _public_scheduler_status(scheduler)
    readiness["next_scheduled_run"] = scheduler.get("next_scheduled_run")
    readiness["deployed_source_sha"] = source.get("source_sha")
    readiness["deployed_build_sha"] = build_sha
    readiness["build_id"] = build_id
    result["readiness"] = readiness
    (output_root / "readiness.json").write_text(
        json.dumps(readiness, sort_keys=True, indent=2, default=str),
        encoding="utf-8",
    )
    v6_path = output_root / "data" / "v6-learning.json"
    v6_path.parent.mkdir(parents=True, exist_ok=True)
    v6_path.write_text(
        json.dumps(v6_public_status(SQLiteScanStore(db_path)), sort_keys=True, indent=2),
        encoding="utf-8",
    )
    notification = _record_build_notification(
        db_path,
        result,
        market_date=market_date,
        build_id=build_id,
        data_hash=performance_hash,
        deployment_url=args.deployment_url,
    )
    result["notification"] = notification
    result["deployment_url"] = args.deployment_url
    if args.result_out:
        result_out = Path(args.result_out).resolve()
        result_out.parent.mkdir(parents=True, exist_ok=True)
        result_out.write_text(
            json.dumps(result, sort_keys=True, indent=2, default=str),
            encoding="utf-8",
        )
    artifact_hashes = _file_hashes(
        output_root,
        exclude={"build-manifest.json", "release-manifest.json"},
    )
    release_manifest = release_manifest_payload(
        source_sha=str(source.get("source_sha") or ""),
        build_sha=build_sha,
        runtime_root=root,
        state_root=state_root,
        schema_version=_database_schema_version(db_path),
        data_watermark=str(
            readiness.get("source_data_watermark") or market_date
        ),
        artifact_hashes=artifact_hashes,
    )
    (output_root / "release-manifest.json").write_text(
        json.dumps(release_manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    file_hashes = _file_hashes(output_root, exclude={"build-manifest.json"})
    generated_at = (
        datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    )
    (output_root / "build-manifest.json").write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.public_build.v1",
                "source_sha": source.get("source_sha"),
                "source_clean": source.get("source_clean"),
                "build_id": build_id,
                "build_sha": build_sha,
                "data_hash_sha256": performance_hash,
                "publication_set_sha256": publication_set_hash,
                "release_manifest_sha256": release_manifest.get(
                    "release_manifest_sha256"
                ),
                "market_date": market_date,
                "generated_at": generated_at,
                "status": result.get("status"),
                "readiness": result.get("readiness"),
                "file_hashes": file_hashes,
                "research_only": True,
                "live_trading_enabled": False,
            },
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(json.dumps(result, sort_keys=True, indent=2, default=str))
    return (
        0
        if result.get("status") == "COMPLETE"
        and readiness.get("status") == "ready"
        else 2
    )


def _resolve_repository_database(
    root: Path,
    value: str,
    *,
    state_root: Path | None = None,
) -> Path:
    """Resolve a persistence target inside the runtime or durable state root."""

    repository = root.resolve()
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = repository / candidate
    resolved = candidate.resolve()
    approved_roots = [repository]
    if state_root is not None:
        approved_roots.append(state_root.resolve())
    if not any(_is_relative_to(resolved, boundary) for boundary in approved_roots):
        approved = ", ".join(str(path) for path in approved_roots)
        raise ValueError(
            f"database path must be inside an approved root ({approved}): "
            f"{resolved}"
        )
    return resolved


def _record_build_notification(
    db_path: Path,
    result: dict[str, object],
    *,
    market_date: str,
    build_id: str,
    data_hash: str,
    deployment_url: str | None,
) -> dict[str, object]:
    readiness = result.get("readiness")
    readiness_payload = readiness if isinstance(readiness, dict) else {}
    status = str(result.get("status") or "FAILED")
    next_action = str(
        readiness_payload.get("reason")
        or "Inspect the daily stage manifest and rerun the bounded finalize flow."
    )
    reconciliation = result.get("reconciliation")
    reconciliation_payload = reconciliation if isinstance(reconciliation, dict) else {}
    notification = {
        "status": status,
        "market_date": market_date,
        "stage": "readiness",
        "build_id": build_id,
        "data_hash_sha256": data_hash,
        "coverage": reconciliation_payload.get("coverage"),
        "paper_ops": reconciliation_payload.get("paper_ops"),
        "deployment_url": deployment_url,
        "next_action": next_action,
    }
    SQLiteScanStore(db_path).record_notification(
        event_key=f"dawnstrike:daily-finalize:{market_date}:{build_id}",
        channel="console",
        payload=notification,
        run_id=str(result.get("run_id") or "") or None,
    )
    return notification


def _public_scheduler_status(value: dict[str, object]) -> dict[str, object]:
    """Project the scheduler proof into the public artifact without host paths."""

    rows = value.get("scheduled_tasks")
    tasks = rows if isinstance(rows, list) else []
    return {
        "schema_version": "dawnstrike.scheduler_public.v1",
        "status": value.get("status"),
        "failed_task_count": value.get("failed_task_count"),
        "next_scheduled_run": value.get("next_scheduled_run"),
        "runtime_boundary": "configured",
        "state_boundary": "configured",
        "scheduled_tasks": [
            {
                key: row.get(key)
                for key in (
                    "name",
                    "state",
                    "status",
                    "enabled",
                    "last_task_result",
                    "last_run_time",
                    "next_run_time",
                    "noninteractive",
                    "start_when_available",
                    "battery_safe",
                )
            }
            for row in tasks
            if isinstance(row, dict)
        ],
    }


def _source_metadata(root: Path) -> dict[str, object]:
    try:
        source_sha = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=root, check=True, capture_output=True, text=True
        ).stdout.strip()
        dirty = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        return {"source_sha": source_sha, "source_clean": not bool(dirty)}
    except (OSError, subprocess.CalledProcessError):
        return {"source_sha": None, "source_clean": False}


def _file_hashes(root: Path, *, exclude: set[str]) -> dict[str, str]:
    return {
        str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file() and str(path.relative_to(root)).replace("\\", "/") not in exclude
    }


def _database_schema_version(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return get_schema_version(connection)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _clear_known_outputs(output_root: Path) -> None:
    for name in (
        "index.html",
        "readiness.json",
        "stage-manifest.json",
        "build-manifest.json",
        "release-manifest.json",
        "daily-finalize-result.json",
    ):
        path = output_root / name
        if path.is_file():
            path.unlink()
    for name in ("assets", "data"):
        path = output_root / name
        if path.is_dir():
            shutil.rmtree(path)


if __name__ == "__main__":
    raise SystemExit(main())
