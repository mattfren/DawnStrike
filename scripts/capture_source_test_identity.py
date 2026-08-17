"""Capture portable checkout-byte and Git-blob identities for source/test paths."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
PREFIXES = (".github/workflows/", "api/", "intraday_scanner/", "scripts/", "tests/", "web/")
ROOT_FILES = {
    "app.py",
    "pyproject.toml",
    "requirements.txt",
    "requirements-dev.txt",
    "vercel.json",
}


def selected_paths() -> tuple[str, ...]:
    tracked = _git("ls-files", "--cached", "--others", "--exclude-standard").splitlines()
    return tuple(
        sorted(
            path
            for path in tracked
            if path in ROOT_FILES or path.startswith(PREFIXES)
        )
    )


def capture_identity() -> dict[str, object]:
    object_format = _git("rev-parse", "--show-object-format").strip()
    if object_format not in hashlib.algorithms_available:
        raise RuntimeError(f"unsupported Git object format: {object_format}")
    commit_oid = _git("rev-parse", "HEAD").strip()
    tree_oid = _git("rev-parse", "HEAD^{tree}").strip()
    head_entries = _head_blob_entries()
    paths = selected_paths()
    checkout_blob_entries = _checkout_blob_entries(paths)
    checkout_aggregate = hashlib.sha256()
    checkout_blob_aggregate = hashlib.sha256()
    head_blob_aggregate = hashlib.sha256()
    entries: list[dict[str, object]] = []
    for relative in paths:
        content = (ROOT / relative).read_bytes()
        content_sha256 = hashlib.sha256(content).hexdigest()
        checkout_blob_oid = checkout_blob_entries[relative]
        head_blob_oid = head_entries.get(relative)
        matches_head = head_blob_oid == checkout_blob_oid
        _aggregate(checkout_aggregate, relative, content_sha256)
        _aggregate(checkout_blob_aggregate, relative, checkout_blob_oid)
        _aggregate(head_blob_aggregate, relative, head_blob_oid or "MISSING")
        entries.append(
            {
                "path": relative,
                "length": len(content),
                "checkout_sha256": _portable_hex(content_sha256),
                "checkout_git_blob_oid": _portable_hex(checkout_blob_oid),
                "head_git_blob_oid": _portable_hex(head_blob_oid) if head_blob_oid else None,
                "checkout_matches_head_blob": matches_head,
            }
        )
    return {
        "schema_version": "dawnstrike.source_test_git_binding.v1",
        "hex_encoding": "colon-delimited-groups-of-8",
        "captured_at_utc": datetime.now(timezone.utc).isoformat(),
        "path_selection_contract": {
            "prefixes": PREFIXES,
            "root_files": tuple(sorted(ROOT_FILES)),
        },
        "git_object_format": object_format,
        "head_commit_oid": _portable_hex(commit_oid),
        "head_tree_oid": _portable_hex(tree_oid),
        "file_count": len(entries),
        "checkout_byte_aggregate_sha256": _portable_hex(checkout_aggregate.hexdigest()),
        "checkout_git_blob_aggregate_sha256": _portable_hex(
            checkout_blob_aggregate.hexdigest()
        ),
        "head_git_blob_aggregate_sha256": _portable_hex(head_blob_aggregate.hexdigest()),
        "all_checkout_bytes_match_head": all(
            bool(item["checkout_matches_head_blob"]) for item in entries
        ),
        "files": entries,
    }


def verify_payload(expected: dict[str, object]) -> tuple[bool, dict[str, object]]:
    actual = capture_identity()
    expected_without_time = dict(expected)
    actual_without_time = dict(actual)
    expected_without_time.pop("captured_at_utc", None)
    actual_without_time.pop("captured_at_utc", None)
    expected_json = json.dumps(expected_without_time, sort_keys=True, separators=(",", ":"))
    actual_json = json.dumps(actual_without_time, sort_keys=True, separators=(",", ":"))
    return expected_json == actual_json, actual


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path)
    parser.add_argument("--verify", type=Path)
    args = parser.parse_args(argv)
    if args.verify is not None:
        expected = json.loads(args.verify.read_text(encoding="utf-8"))
        if not isinstance(expected, dict):
            parser.error("identity artifact must contain a JSON object")
        valid, actual = verify_payload(expected)
        summary = _summary(actual)
        summary["artifact_matches_current_checkout"] = valid
        print(json.dumps(summary, sort_keys=True))
        return 0 if valid else 2
    payload = capture_identity()
    rendered = json.dumps(payload, sort_keys=True, indent=2) + "\n"
    if args.output is not None:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(json.dumps(_summary(payload), sort_keys=True))
    return 0


def _summary(payload: dict[str, object]) -> dict[str, object]:
    return {
        key: payload[key]
        for key in (
            "file_count",
            "checkout_byte_aggregate_sha256",
            "checkout_git_blob_aggregate_sha256",
            "head_commit_oid",
            "head_tree_oid",
            "all_checkout_bytes_match_head",
        )
    }


def _head_blob_entries() -> dict[str, str]:
    completed = subprocess.run(
        ["git", "ls-tree", "-rz", "--full-tree", "HEAD"],
        cwd=ROOT,
        check=True,
        capture_output=True,
    )
    entries: dict[str, str] = {}
    for record in completed.stdout.split(b"\0"):
        if not record:
            continue
        metadata, raw_path = record.split(b"\t", 1)
        _mode, object_type, raw_oid = metadata.split(b" ", 2)
        if object_type == b"blob":
            entries[raw_path.decode("utf-8", errors="surrogateescape")] = raw_oid.decode("ascii")
    return entries


def _checkout_blob_entries(paths: tuple[str, ...]) -> dict[str, str]:
    completed = subprocess.run(
        ["git", "hash-object", "--stdin-paths"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
        input="\n".join(paths) + "\n",
    )
    object_ids = tuple(completed.stdout.splitlines())
    if len(object_ids) != len(paths):
        raise RuntimeError("Git hash-object did not return one identity per selected path")
    return dict(zip(paths, object_ids, strict=True))


def _git_blob_oid(content: bytes, object_format: str) -> str:
    digest = hashlib.new(object_format, usedforsecurity=False)
    digest.update(f"blob {len(content)}\0".encode("ascii"))
    digest.update(content)
    return digest.hexdigest()


def _aggregate(aggregate: Any, path: str, identity: str) -> None:
    aggregate.update(path.encode("utf-8"))
    aggregate.update(b"\0")
    aggregate.update(identity.encode("ascii"))
    aggregate.update(b"\n")


def _portable_hex(value: str) -> str:
    return ":".join(value[index : index + 8] for index in range(0, len(value), 8))


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], cwd=ROOT, text=True)


if __name__ == "__main__":
    raise SystemExit(main())
