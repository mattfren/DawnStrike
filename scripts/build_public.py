"""Build Dawnstrike's framework-free public site from the canonical snapshot."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from intraday_scanner.approved_tools import read_git_bytes, run_git
from intraday_scanner.dashboard.opportunity_projection_store import (
    load_latest_opportunity_projection,
    write_public_opportunity_projection,
)
from intraday_scanner.services.daily_finalize_service import DailyFinalizeService
from intraday_scanner.services.daily_run_service import release_manifest_payload
from intraday_scanner.services.scenario_intelligence_service import scenario_public_snapshot
from intraday_scanner.services.scheduler_doctor_service import scheduler_doctor
from intraday_scanner.services.v6_learning_service import v6_public_status
from intraday_scanner.storage.migrations import get_schema_version
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from scripts.public_artifact_inventory import (
    PRIVATE_BUILD_FILES,
    PUBLIC_ARTIFACT_FILES,
    PublicArtifactInventoryError,
    assert_contained_no_reparse,
    assert_exact_public_inventory,
    inventory_files_no_reparse,
)
from scripts.public_lineage import build_sha as _lineage_build_sha
from scripts.verify_public_artifact import verify as verify_public_artifact
from scripts.verify_public_artifact_security import scan_public_artifact

_ACTIVE_PUBLIC_BUILD_OPERATION: _PublicBuildOperation | None = None


def _main(argv: list[str] | None = None) -> int:
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
    parser.add_argument(
        "--build-attempt-id",
        default=None,
        help="Caller-generated 32-hex identity binding the result to this invocation.",
    )
    parser.add_argument("--deployment-url", default=None)
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="Build a diagnostic artifact from a dirty checkout; never use for deployment.",
    )
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    configured_state_root = Path(args.state_root).resolve() if args.state_root else None
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
                        "Use a database under the clean runtime or the explicit durable state root."
                    ),
                },
                sort_keys=True,
                indent=2,
            )
        )
        return 2
    state_root = configured_state_root or db_path.parent.resolve()
    market_date = args.date or datetime.now().date().isoformat()
    build_attempt_id = str(args.build_attempt_id or uuid.uuid4().hex).lower()
    if len(build_attempt_id) != 32 or any(
        character not in "0123456789abcdef" for character in build_attempt_id
    ):
        print(json.dumps({"status": "FAILED", "reason": "invalid_build_attempt_id"}))
        return 2
    final_output_root = Path(os.path.abspath(root / args.out_dir))
    try:
        final_output_root.relative_to(root)
    except ValueError:
        print(json.dumps({"status": "FAILED", "reason": "public_output_outside_runtime"}))
        return 2
    result_out: Path | None = None
    if args.result_out:
        result_out = Path(os.path.abspath(args.result_out))
        try:
            result_out.relative_to(state_root)
            assert_contained_no_reparse(state_root, result_out)
            result_out.parent.mkdir(parents=True, exist_ok=True)
            assert_contained_no_reparse(state_root, result_out.parent)
            _write_private_finalize_result(
                result_out,
                {
                    "schema_version": "dawnstrike.daily_finalize_build_attempt.v1",
                    "status": "IN_PROGRESS",
                    "market_date": market_date,
                    "build_attempt_id": build_attempt_id,
                    "research_only": True,
                    "broker_execution_enabled": False,
                },
                state_root=state_root,
            )
        except (OSError, ValueError, PublicArtifactInventoryError) as exc:
            print(
                json.dumps(
                    {
                        "status": "FAILED",
                        "reason": "private_result_path_unsafe",
                        "detail": str(exc),
                    },
                    sort_keys=True,
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
    try:
        assert_contained_no_reparse(root, final_output_root)
        final_output_root.parent.mkdir(parents=True, exist_ok=True)
        assert_contained_no_reparse(root, final_output_root.parent)
        operation = _enter_public_build_operation(
            root,
            final_output_root,
            expected_source_sha=str(source.get("source_sha") or ""),
        )
    except (OSError, PublicArtifactInventoryError) as exc:
        print(
            json.dumps(
                {"status": "FAILED", "reason": "public_output_path_unsafe", "detail": str(exc)},
                sort_keys=True,
            )
        )
        return 2
    output_root = operation.begin(
        source_sha=str(source.get("source_sha") or ""), market_date=market_date
    )
    paper_ops_root = Path(args.paper_ops_root)
    if not paper_ops_root.is_absolute():
        paper_ops_root = root / paper_ops_root
    scenario_payload = scenario_public_snapshot(db_path=db_path)
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
        scenario_payload=scenario_payload,
    )
    result["build_attempt_id"] = build_attempt_id
    _remove_private_build_files(output_root)
    _write_committed_web_assets(
        root,
        output_root,
        source_sha=str(source.get("source_sha") or ""),
    )
    # The database retains historical immutable opportunity runs. Scope the
    # public projection to this finalize date before selecting a run so an old
    # database-wide "latest" row can never be published as today's pick.
    opportunity_projection = load_latest_opportunity_projection(
        db_path,
        expected_market_date=market_date,
    )
    opportunity_projection_manifest = write_public_opportunity_projection(
        output_root / "data",
        opportunity_projection,
        expected_market_date=market_date,
    )
    result["opportunity_projection"] = opportunity_projection_manifest
    readiness_value = result.get("readiness")
    if not isinstance(readiness_value, dict):
        raise RuntimeError("Daily Finalize did not return a readiness object.")
    readiness: dict[str, object] = dict(readiness_value)
    performance_hash = str(readiness.get("payload_sha256") or "")
    publication_set_hash = str(readiness.get("publication_set_sha256") or performance_hash)
    opportunity_projection_hash = str(
        opportunity_projection_manifest.get("payload_sha256") or ""
    )
    # V6 is a first-class immutable input to the public identity.  Write and
    # hash it before deriving build_sha so a V6-only byte change cannot retain
    # the identity of an older artifact.
    v6_path = output_root / "data" / "v6-learning.json"
    v6_path.parent.mkdir(parents=True, exist_ok=True)
    v6_bytes = json.dumps(
        v6_public_status(SQLiteScanStore(db_path)), sort_keys=True, indent=2
    ).encode("utf-8")
    v6_path.write_bytes(v6_bytes)
    v6_learning_hash = hashlib.sha256(v6_bytes).hexdigest()
    build_sha = _build_sha(
        source_sha=str(source.get("source_sha") or ""),
        publication_set_sha256=publication_set_hash,
        opportunity_projection_sha256=opportunity_projection_hash,
        v6_learning_sha256=v6_learning_hash,
        market_date=market_date,
    )
    build_id = build_sha[:20]
    readiness["v6_learning_sha256"] = v6_learning_hash
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
    assert_exact_public_inventory(
        output_root,
        expected=PUBLIC_ARTIFACT_FILES - {"build-manifest.json", "release-manifest.json"},
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
        data_watermark=str(readiness.get("source_data_watermark") or market_date),
        artifact_hashes=artifact_hashes,
        v6_learning_sha256=v6_learning_hash,
    )
    (output_root / "release-manifest.json").write_text(
        json.dumps(release_manifest, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    assert_exact_public_inventory(
        output_root, expected=PUBLIC_ARTIFACT_FILES - {"build-manifest.json"}
    )
    file_hashes = _file_hashes(output_root, exclude={"build-manifest.json"})
    generated_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
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
                "opportunity_projection_sha256": opportunity_projection_hash,
                "v6_learning_sha256": v6_learning_hash,
                "release_manifest_sha256": release_manifest.get("release_manifest_sha256"),
                "market_date": market_date,
                "generated_at": generated_at,
                "status": result.get("status"),
                "readiness": result.get("readiness"),
                "file_hashes": file_hashes,
                "research_only": True,
                "live_trading_enabled": False,
                "broker_execution_enabled": False,
            },
            sort_keys=True,
            indent=2,
        ),
        encoding="utf-8",
    )
    assert_exact_public_inventory(output_root)
    violations = scan_public_artifact(output_root)
    if violations:
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": "public_artifact_security_violation",
                    "violation_count": len(violations),
                    "violations": [
                        {
                            "file": str(item.path.relative_to(output_root)).replace("\\", "/"),
                            "rule": item.rule,
                        }
                        for item in violations
                    ],
                    "next_action": (
                        "Rebuild from an explicit safe public DTO; do not redact at deploy time."
                    ),
                },
                sort_keys=True,
                indent=2,
            )
        )
        _remove_tree_no_reparse(root, output_root)
        return 2
    final_source = _source_metadata(root)
    if final_source != source or final_source.get("source_clean") is not True:
        _remove_tree_no_reparse(root, output_root)
        print(
            json.dumps(
                {
                    "status": "FAILED",
                    "reason": "source_changed_during_public_build",
                },
                sort_keys=True,
            )
        )
        return 2
    publishable = result.get("status") == "COMPLETE" and readiness.get("status") == "ready"
    if not publishable:
        result["deployment_url"] = args.deployment_url
        if result_out is not None:
            _write_private_finalize_result(result_out, result, state_root=state_root)
        print(json.dumps(result, sort_keys=True, indent=2, default=str))
        return 2
    operation.mark("STAGED")
    _promote_public_artifact(root, output_root, final_output_root, operation=operation)
    # Make the verified installation irreversible before emitting success
    # evidence.  Otherwise a crash after either durable receipt write could
    # restore the prior artifact while leaving a false COMPLETE result.
    operation.commit()
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
    if result_out is not None:
        if _is_relative_to(result_out, final_output_root) or _is_relative_to(
            result_out, output_root
        ):
            raise ValueError("Private finalize result cannot be written into the public artifact")
        _write_private_finalize_result(
            result_out,
            result,
            state_root=state_root,
        )
    print(json.dumps(result, sort_keys=True, indent=2, default=str))
    return 0


def main(argv: list[str] | None = None) -> int:
    """Run one serialized build and converge any interrupted directory swap."""

    global _ACTIVE_PUBLIC_BUILD_OPERATION
    try:
        return _main(argv)
    finally:
        operation = _ACTIVE_PUBLIC_BUILD_OPERATION
        if operation is not None:
            try:
                operation.close()
            finally:
                _ACTIVE_PUBLIC_BUILD_OPERATION = None


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
        raise ValueError(f"database path must be inside an approved root ({approved}): {resolved}")
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
        "publication_contract": value.get("publication_contract")
        or {
            "schema_version": "dawnstrike.publication_schedule.v1",
            "timezone": "America/Chicago",
            "market_day_only": True,
            "scheduled_time_local": "17:30",
            "task_name": "Dawnstrike 10of10 Daily Finalize",
            "research_only": True,
            "live_trading_enabled": False,
        },
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
        source_sha = run_git(root, "rev-parse", "HEAD").stdout.strip()
        dirty = run_git(
            root, "status", "--porcelain", "--untracked-files=all"
        ).stdout.strip()
        return {"source_sha": source_sha, "source_clean": not bool(dirty)}
    except (OSError, subprocess.CalledProcessError):
        return {"source_sha": None, "source_clean": False}


def _write_committed_web_assets(
    root: Path,
    output_root: Path,
    *,
    source_sha: str,
) -> None:
    if len(source_sha) != 40 or any(char not in "0123456789abcdef" for char in source_sha):
        raise PublicArtifactInventoryError("public web source SHA is invalid")
    listed = read_git_bytes(
        root,
        "ls-tree",
        "-r",
        "--name-only",
        "-z",
        source_sha,
        "--",
        "web/assets",
        max_bytes=1024 * 1024,
    )
    try:
        asset_names = [item for item in listed.decode("utf-8").split("\0") if item]
    except UnicodeDecodeError as exc:
        raise PublicArtifactInventoryError("committed web asset names are invalid") from exc
    source_names = ["web/index.html", "web/favicon.svg", *asset_names]
    if not 3 <= len(source_names) <= 128 or len(source_names) != len(set(source_names)):
        raise PublicArtifactInventoryError("committed web asset inventory is invalid")
    total_bytes = 0
    for source_name in source_names:
        if (
            "\\" in source_name
            or source_name.startswith("/")
            or ".." in Path(source_name).parts
            or (
                source_name not in {"web/index.html", "web/favicon.svg"}
                and not source_name.startswith("web/assets/")
            )
        ):
            raise PublicArtifactInventoryError("committed web asset path is unsafe")
        payload = read_git_bytes(
            root,
            "show",
            f"{source_sha}:{source_name}",
            max_bytes=16 * 1024 * 1024,
        )
        total_bytes += len(payload)
        if total_bytes > 32 * 1024 * 1024:
            raise PublicArtifactInventoryError("committed web assets exceed byte ceiling")
        relative = source_name.removeprefix("web/")
        destination = output_root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        assert_contained_no_reparse(output_root, destination)
        destination.write_bytes(payload)


def _file_hashes(root: Path, *, exclude: set[str]) -> dict[str, str]:
    expected = PUBLIC_ARTIFACT_FILES - exclude
    observed = inventory_files_no_reparse(root)
    if observed != expected:
        raise PublicArtifactInventoryError(
            "public artifact inventory changed before hashing; "
            f"unexpected={sorted(observed - expected)}; "
            f"missing={sorted(expected - observed)}"
        )
    hashes: dict[str, str] = {}
    for relative in sorted(observed):
        path = root / relative
        assert_contained_no_reparse(root, path)
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode) or _stat_is_reparse(before):
            raise PublicArtifactInventoryError(
                f"public artifact hash input is not a regular file: {relative}"
            )
        payload = path.read_bytes()
        after = path.lstat()
        if not stat.S_ISREG(after.st_mode) or _stat_is_reparse(after):
            raise PublicArtifactInventoryError(
                f"public artifact hash input changed type: {relative}"
            )
        before_identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
        )
        after_identity = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if before_identity != after_identity or len(payload) != after.st_size:
            raise PublicArtifactInventoryError(
                f"public artifact hash input changed while reading: {relative}"
            )
        hashes[relative] = hashlib.sha256(payload).hexdigest()
    return hashes


def _build_sha(
    *,
    source_sha: str,
    publication_set_sha256: str,
    opportunity_projection_sha256: str,
    v6_learning_sha256: str,
    market_date: str,
) -> str:
    """Return the documented byte-lineage identity for a public build."""

    return _lineage_build_sha(
        source_sha=source_sha,
        publication_set_sha256=publication_set_sha256,
        opportunity_projection_sha256=opportunity_projection_sha256,
        v6_learning_sha256=v6_learning_sha256,
        market_date=market_date,
    )


def _database_schema_version(db_path: Path) -> int:
    with sqlite3.connect(db_path) as connection:
        return get_schema_version(connection)


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _remove_private_build_files(output_root: Path) -> None:
    for name in PRIVATE_BUILD_FILES:
        path = output_root / name
        if path.exists() or path.is_symlink():
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode) or _stat_is_reparse(details):
                raise PublicArtifactInventoryError(f"private build path is unsafe: {name}")
            path.unlink()


def _remove_tree_no_reparse(governed_root: Path, target: Path) -> None:
    assert_contained_no_reparse(governed_root, target)

    def remove(directory: Path) -> None:
        with os.scandir(directory) as entries:
            for entry in list(entries):
                path = Path(entry.path)
                details = entry.stat(follow_symlinks=False)
                if _stat_is_reparse(details):
                    raise PublicArtifactInventoryError(
                        f"refusing to remove reparse entry: {path}"
                    )
                if entry.is_dir(follow_symlinks=False):
                    remove(path)
                elif entry.is_file(follow_symlinks=False):
                    path.unlink()
                else:
                    raise PublicArtifactInventoryError(
                        f"refusing to remove non-file entry: {path}"
                    )
        directory.rmdir()

    remove(target)


def _stat_is_reparse(details: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(details.st_mode)
        or getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


class _PublicBuildOperation:
    """Serialize and converge the two-rename public artifact promotion."""

    _SCHEMA = "dawnstrike.public_build_operation.v1"
    _PHASES = frozenset(
        {
            "INIT", "BUILDING", "STAGED", "PRE_SWAP", "PRIOR_MOVED", "PROMOTED",
            "COMMITTED",
        }
    )

    def __init__(
        self,
        governed_root: Path,
        final_root: Path,
        *,
        expected_source_sha: str | None = None,
    ) -> None:
        self.governed_root = governed_root
        self.final_root = final_root
        self.stage_root = final_root.with_name(f".{final_root.name}-build-next")
        self.backup_root = final_root.with_name(f".{final_root.name}-retired")
        self.journal_path = final_root.with_name(f".{final_root.name}-build-operation.json")
        self.journal_temp_path = final_root.with_name(
            f".{final_root.name}-build-operation.json.tmp"
        )
        self.lock_path = final_root.with_name(f".{final_root.name}-build-operation.lock")
        self._lock_handle: object | None = None
        self._locked = False
        self.expected_source_sha = expected_source_sha
        for path in (
            self.final_root,
            self.stage_root,
            self.backup_root,
            self.journal_path,
            self.journal_temp_path,
            self.lock_path,
        ):
            assert_contained_no_reparse(governed_root, path)
        self._reject_legacy_operation_paths()
        self._acquire_lock()
        try:
            self.recover()
        except BaseException:
            self._release_lock()
            raise

    def _reject_legacy_operation_paths(self) -> None:
        allowed = {self.stage_root.name, self.backup_root.name}
        discovered = set(
            self.final_root.parent.glob(f".{self.final_root.name}-build-*")
        )
        discovered.update(
            self.final_root.parent.glob(f".{self.final_root.name}-retired-*")
        )
        candidates = {
            path
            for path in discovered
            if path.is_dir() or path.is_symlink()
        }
        unexpected = sorted(path.name for path in candidates if path.name not in allowed)
        if unexpected:
            raise PublicArtifactInventoryError(
                f"ambiguous legacy public build operation paths: {unexpected}"
            )

    def _acquire_lock(self) -> None:
        assert_contained_no_reparse(self.governed_root, self.lock_path)
        handle = self.lock_path.open("a+b")
        try:
            details = self.lock_path.lstat()
            if not stat.S_ISREG(details.st_mode) or _stat_is_reparse(details):
                raise PublicArtifactInventoryError("public build lock path is unsafe")
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()
                os.fsync(handle.fileno())
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BaseException:
            handle.close()
            raise
        self._lock_handle = handle
        self._locked = True

    def _release_lock(self) -> None:
        if not self._locked or self._lock_handle is None:
            return
        handle = self._lock_handle
        try:
            handle.seek(0)
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()
            self._lock_handle = None
            self._locked = False

    def _load_journal(self) -> dict[str, object]:
        try:
            raw = self.journal_path.read_text(encoding="utf-8")
            payload = json.loads(raw, object_pairs_hook=_reject_duplicate_json_pairs)
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise PublicArtifactInventoryError(
                "public build operation journal is unreadable"
            ) from exc
        if not isinstance(payload, dict) or set(payload) != {
            "schema_version",
            "source_sha",
            "market_date",
            "phase",
            "journal_sha256",
        }:
            raise PublicArtifactInventoryError("public build operation journal shape is invalid")
        if payload.get("schema_version") != self._SCHEMA:
            raise PublicArtifactInventoryError("public build operation schema is invalid")
        source_sha = payload.get("source_sha")
        market_date = payload.get("market_date")
        phase = payload.get("phase")
        if not isinstance(source_sha, str) or len(source_sha) != 40 or any(
            char not in "0123456789abcdef" for char in source_sha
        ):
            raise PublicArtifactInventoryError("public build operation source SHA is invalid")
        try:
            parsed_date = datetime.fromisoformat(str(market_date)).date().isoformat()
        except ValueError as exc:
            raise PublicArtifactInventoryError(
                "public build operation market date is invalid"
            ) from exc
        if parsed_date != market_date or phase not in self._PHASES:
            raise PublicArtifactInventoryError("public build operation identity is invalid")
        expected_hash = _public_build_journal_hash(
            source_sha=source_sha, market_date=str(market_date), phase=str(phase)
        )
        if payload.get("journal_sha256") != expected_hash:
            raise PublicArtifactInventoryError("public build operation hash is invalid")
        return payload

    def _write_journal(self, *, source_sha: str, market_date: str, phase: str) -> None:
        if phase not in self._PHASES:
            raise PublicArtifactInventoryError("public build operation phase is invalid")
        payload = {
            "schema_version": self._SCHEMA,
            "source_sha": source_sha,
            "market_date": market_date,
            "phase": phase,
            "journal_sha256": _public_build_journal_hash(
                source_sha=source_sha, market_date=market_date, phase=phase
            ),
        }
        if self.journal_temp_path.exists() or self.journal_temp_path.is_symlink():
            _remove_regular_operation_file(self.journal_temp_path)
        encoded = json.dumps(payload, sort_keys=True, indent=2).encode("utf-8")
        with self.journal_temp_path.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(self.journal_temp_path, self.journal_path)
        if self._load_journal() != payload:
            raise PublicArtifactInventoryError("public build operation journal did not persist")

    def begin(self, *, source_sha: str, market_date: str) -> Path:
        if self.journal_path.exists() or self.stage_root.exists() or self.backup_root.exists():
            raise PublicArtifactInventoryError("public build operation was not fully recovered")
        self._write_journal(
            source_sha=source_sha, market_date=market_date, phase="INIT"
        )
        self.stage_root.mkdir()
        assert_contained_no_reparse(self.governed_root, self.stage_root)
        self.mark("BUILDING")
        return self.stage_root

    def mark(self, phase: str) -> None:
        journal = self._load_journal()
        self._write_journal(
            source_sha=str(journal["source_sha"]),
            market_date=str(journal["market_date"]),
            phase=phase,
        )

    def finish(self) -> None:
        if self.stage_root.exists() or self.backup_root.exists():
            raise PublicArtifactInventoryError("public build terminal paths remain")
        assert_exact_public_inventory(self.final_root)
        _remove_regular_operation_file(self.journal_path)
        if self.journal_temp_path.exists() or self.journal_temp_path.is_symlink():
            _remove_regular_operation_file(self.journal_temp_path)

    def commit(self) -> None:
        journal = self._load_journal()
        if journal.get("phase") != "PROMOTED":
            raise PublicArtifactInventoryError("public build cannot commit before promotion")
        assert_exact_public_inventory(self.final_root)
        self.mark("COMMITTED")
        if self.backup_root.exists() or self.backup_root.is_symlink():
            _remove_tree_no_reparse(self.governed_root, self.backup_root)
        self.finish()

    def recover(self) -> None:
        if self.journal_temp_path.exists() or self.journal_temp_path.is_symlink():
            _remove_regular_operation_file(self.journal_temp_path)
        journal_exists = self.journal_path.exists() or self.journal_path.is_symlink()
        stage_exists = self.stage_root.exists() or self.stage_root.is_symlink()
        backup_exists = self.backup_root.exists() or self.backup_root.is_symlink()
        final_exists = self.final_root.exists() or self.final_root.is_symlink()
        if not journal_exists:
            if stage_exists or backup_exists:
                raise PublicArtifactInventoryError(
                    "public build operation paths exist without their journal"
                )
            return
        journal = self._load_journal()
        if (
            self.expected_source_sha is not None
            and journal.get("source_sha") != self.expected_source_sha
        ):
            raise PublicArtifactInventoryError(
                "public build recovery source SHA does not match current runtime"
            )
        phase = str(journal["phase"])
        if stage_exists:
            assert_contained_no_reparse(self.governed_root, self.stage_root)
        if backup_exists:
            assert_contained_no_reparse(self.governed_root, self.backup_root)
        if final_exists:
            assert_contained_no_reparse(self.governed_root, self.final_root)

        if backup_exists and not final_exists:
            _assert_safe_previous_public_artifact(self.backup_root)
            if stage_exists:
                _remove_tree_no_reparse(self.governed_root, self.stage_root)
                stage_exists = False
            os.replace(self.backup_root, self.final_root)
            backup_exists = False
            final_exists = True
        elif backup_exists and final_exists:
            if stage_exists:
                raise PublicArtifactInventoryError(
                    "public build operation has an ambiguous three-directory state"
                )
            _assert_safe_previous_public_artifact(self.backup_root)
            if (
                phase == "COMMITTED"
                and _is_recoverable_candidate_public_artifact(self.final_root, journal)
            ):
                _remove_tree_no_reparse(self.governed_root, self.backup_root)
            else:
                _remove_tree_no_reparse(self.governed_root, self.final_root)
                os.replace(self.backup_root, self.final_root)
            backup_exists = False
        elif stage_exists:
            if final_exists:
                _assert_safe_previous_public_artifact(self.final_root)
                _remove_tree_no_reparse(self.governed_root, self.stage_root)
                stage_exists = False
            else:
                _remove_tree_no_reparse(self.governed_root, self.stage_root)
                stage_exists = False
        elif final_exists and phase in {"PRIOR_MOVED", "PROMOTED", "COMMITTED"}:
            if phase != "COMMITTED" or not _is_recoverable_candidate_public_artifact(
                self.final_root, journal
            ):
                _remove_tree_no_reparse(self.governed_root, self.final_root)
                final_exists = False

        if stage_exists or backup_exists:
            raise PublicArtifactInventoryError("public build recovery did not converge")
        if final_exists and not _is_safe_previous_public_artifact(self.final_root):
            raise PublicArtifactInventoryError("recovered public artifact is unsafe")
        _remove_regular_operation_file(self.journal_path)

    def close(self) -> None:
        try:
            self.recover()
        finally:
            self._release_lock()


def _reject_duplicate_json_pairs(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise json.JSONDecodeError("duplicate object key", key, 0)
        result[key] = value
    return result


def _write_private_finalize_result(
    path: Path,
    payload: dict[str, object],
    *,
    state_root: Path,
) -> None:
    """Atomically write one private, StateRoot-contained build attempt receipt."""

    assert_contained_no_reparse(state_root, path)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    assert_contained_no_reparse(state_root, temporary)
    encoded = json.dumps(payload, sort_keys=True, indent=2, default=str).encode("utf-8")
    try:
        with temporary.open("xb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        assert_contained_no_reparse(state_root, path)
        os.replace(temporary, path)
        assert_contained_no_reparse(state_root, path)
        if path.read_bytes() != encoded:
            raise PublicArtifactInventoryError("private finalize result readback mismatch")
    finally:
        if temporary.exists() or temporary.is_symlink():
            _remove_regular_operation_file(temporary)


def _public_build_journal_hash(*, source_sha: str, market_date: str, phase: str) -> str:
    canonical = json.dumps(
        {
            "schema_version": _PublicBuildOperation._SCHEMA,
            "source_sha": source_sha,
            "market_date": market_date,
            "phase": phase,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def _remove_regular_operation_file(path: Path) -> None:
    details = path.lstat()
    if not stat.S_ISREG(details.st_mode) or _stat_is_reparse(details):
        raise PublicArtifactInventoryError(f"public build operation file is unsafe: {path}")
    path.unlink()


def _is_exact_public_artifact(path: Path) -> bool:
    try:
        assert_exact_public_inventory(path)
    except (OSError, PublicArtifactInventoryError):
        return False
    return True


def _is_recoverable_candidate_public_artifact(
    path: Path, journal: dict[str, object]
) -> bool:
    """Prove candidate bytes, lineage, and security before crash recovery promotes them."""

    try:
        assert_exact_public_inventory(path)
        build_manifest = json.loads(
            (path / "build-manifest.json").read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicate_json_pairs,
        )
        if not isinstance(build_manifest, dict):
            return False
        if build_manifest.get("source_sha") != journal.get("source_sha"):
            return False
        if build_manifest.get("market_date") != journal.get("market_date"):
            return False
        verification = verify_public_artifact(
            path,
            allow_degraded=True,
            expected_source_sha=str(journal["source_sha"]),
        )
        if verification.get("status") != "PASS" or verification.get("errors"):
            return False
        return not scan_public_artifact(path)
    except (
        OSError,
        UnicodeError,
        ValueError,
        TypeError,
        AttributeError,
        KeyError,
        PublicArtifactInventoryError,
    ):
        return False


def _is_safe_previous_public_artifact(path: Path) -> bool:
    try:
        observed = inventory_files_no_reparse(path)
    except (OSError, PublicArtifactInventoryError):
        return False
    return observed - PRIVATE_BUILD_FILES == PUBLIC_ARTIFACT_FILES


def _assert_safe_previous_public_artifact(path: Path) -> None:
    if not _is_safe_previous_public_artifact(path):
        raise PublicArtifactInventoryError("previous public artifact inventory is unsafe")


def _enter_public_build_operation(
    governed_root: Path,
    final_root: Path,
    *,
    expected_source_sha: str | None = None,
) -> _PublicBuildOperation:
    global _ACTIVE_PUBLIC_BUILD_OPERATION
    if _ACTIVE_PUBLIC_BUILD_OPERATION is not None:
        raise PublicArtifactInventoryError("a public build operation is already active")
    operation = _PublicBuildOperation(
        governed_root, final_root, expected_source_sha=expected_source_sha
    )
    _ACTIVE_PUBLIC_BUILD_OPERATION = operation
    return operation


def _promote_public_artifact(
    governed_root: Path,
    staged_root: Path,
    final_root: Path,
    *,
    operation: _PublicBuildOperation,
) -> None:
    assert_exact_public_inventory(staged_root)
    assert_contained_no_reparse(governed_root, final_root)
    backup = operation.backup_root
    assert_contained_no_reparse(governed_root, backup)
    operation.mark("PRE_SWAP")
    if final_root.exists() or final_root.is_symlink():
        _assert_safe_previous_public_artifact(final_root)
        os.replace(final_root, backup)
    operation.mark("PRIOR_MOVED")
    os.replace(staged_root, final_root)
    operation.mark("PROMOTED")
    assert_exact_public_inventory(final_root)


if __name__ == "__main__":
    raise SystemExit(main())
