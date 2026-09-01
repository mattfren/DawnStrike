"""Exact no-follow file inventory for the public Dawnstrike artifact."""

from __future__ import annotations

import os
import stat
from collections.abc import Iterable
from pathlib import Path

PUBLIC_ARTIFACT_FILES = frozenset(
    {
        "index.html",
        "favicon.svg",
        "readiness.json",
        "stage-manifest.json",
        "build-manifest.json",
        "release-manifest.json",
        "assets/dawnstrike.css",
        "assets/dawnstrike.js",
        "data/performance.json",
        "data/performance.json.manifest.json",
        "data/calendar.json",
        "data/calendar.json.manifest.json",
        "data/publication-set.json",
        "data/v6-learning.json",
        "data/scenarios.json",
        "data/scenarios.json.manifest.json",
        "data/opportunity-projection.json",
        "data/opportunity-projection.json.manifest.json",
    }
)
PRIVATE_BUILD_FILES = frozenset({"daily-finalize.jsonl", ".daily-finalize.lock"})


class PublicArtifactInventoryError(RuntimeError):
    """The artifact path or exact file set is unsafe."""


def _is_reparse(details: os.stat_result) -> bool:
    return bool(
        stat.S_ISLNK(details.st_mode)
        or getattr(details, "st_file_attributes", 0)
        & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    )


def assert_contained_no_reparse(root: Path, target: Path) -> None:
    root = Path(os.path.abspath(root))
    target = Path(os.path.abspath(target))
    try:
        target.relative_to(root)
    except ValueError as exc:
        raise PublicArtifactInventoryError("public artifact path escaped its root") from exc
    cursor = target
    while True:
        if cursor.exists() or cursor.is_symlink():
            try:
                details = cursor.lstat()
            except OSError as exc:
                raise PublicArtifactInventoryError(
                    "public artifact ancestry is unavailable"
                ) from exc
            if _is_reparse(details):
                raise PublicArtifactInventoryError(
                    f"public artifact ancestry contains a reparse point: {cursor}"
                )
        if cursor == root:
            return
        if cursor.parent == cursor:
            raise PublicArtifactInventoryError("public artifact path escaped its root")
        cursor = cursor.parent


def inventory_files_no_reparse(root: Path) -> frozenset[str]:
    root = Path(os.path.abspath(root))
    assert_contained_no_reparse(root, root)
    if not root.is_dir():
        raise PublicArtifactInventoryError("public artifact root is not a directory")
    files: set[str] = set()
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            entries = list(os.scandir(directory))
        except OSError as exc:
            raise PublicArtifactInventoryError(
                "public artifact tree is unavailable"
            ) from exc
        for entry in entries:
            path = Path(entry.path)
            try:
                details = entry.stat(follow_symlinks=False)
            except OSError as exc:
                raise PublicArtifactInventoryError(
                    "public artifact entry is unavailable"
                ) from exc
            if _is_reparse(details):
                raise PublicArtifactInventoryError(
                    f"public artifact contains a reparse point: {path}"
                )
            relative = path.relative_to(root).as_posix()
            if stat.S_ISDIR(details.st_mode):
                pending.append(path)
            elif stat.S_ISREG(details.st_mode):
                files.add(relative)
            else:
                raise PublicArtifactInventoryError(
                    f"public artifact contains a non-file entry: {relative}"
                )
    return frozenset(files)


def assert_exact_public_inventory(
    root: Path, *, expected: Iterable[str] = PUBLIC_ARTIFACT_FILES
) -> frozenset[str]:
    expected_set = frozenset(expected)
    observed = inventory_files_no_reparse(root)
    if observed != expected_set:
        unexpected = sorted(observed - expected_set)
        missing = sorted(expected_set - observed)
        raise PublicArtifactInventoryError(
            f"public artifact inventory mismatch; unexpected={unexpected}; missing={missing}"
        )
    return observed
