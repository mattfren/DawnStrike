"""Materialize only hash-owned locked dependencies into a protected stage.

This script is executed by the administrator-owned host installer with the
official protected Python interpreter and ``-I -B -S``.  The source Python
environment is user-writable and is therefore treated only as a byte cache:
every copied payload must be owned by a source-approved wheel ``RECORD`` and
must match that row's SHA-256 and size.  Extra distributions and unowned files
are never copied into the privileged boundary.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import re
import stat
from pathlib import Path, PurePosixPath

APPROVED_RECORD_SET_SHA256 = (
    "447a0d12feffcfd6c353d9acb4cfd1e5cc1b35e3548cd7e9ad58666516b4b3af"  # pragma: allowlist secret
)


def _fail(message: str) -> None:
    raise RuntimeError(message)


def _is_reparse(path: Path) -> bool:
    try:
        attributes = getattr(path.lstat(), "st_file_attributes", 0)
    except OSError:
        return True
    return path.is_symlink() or bool(attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT)


def _regular_source(path: Path, prefix: Path, label: str) -> Path:
    absolute = Path(os.path.abspath(path))
    if os.path.commonpath((str(absolute), str(prefix))) != str(prefix):
        _fail(f"{label} escapes the approved source prefix")
    if _is_reparse(absolute) or any(_is_reparse(parent) for parent in absolute.parents):
        _fail(f"{label} contains a reparse point")
    resolved = absolute.resolve(strict=True)
    if os.path.commonpath((str(resolved), str(prefix))) != str(prefix):
        _fail(f"{label} resolves outside the approved source prefix")
    if not resolved.is_file():
        _fail(f"{label} is not a regular file")
    return resolved


def _locked_requirements(path: Path) -> dict[str, str]:
    requirements: dict[str, str] = {}
    saw_hash = False
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        match = re.match(
            r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_,.-]+\])?==([^\s\\]+)",
            stripped,
        )
        if match:
            name = re.sub(r"[-_.]+", "-", match.group(1)).lower()
            version = match.group(2)
            if name in requirements and requirements[name] != version:
                _fail(f"requirements.lock contains conflicting pins for {name}")
            requirements[name] = version
        if "--hash=sha256:" in stripped:
            saw_hash = True
    if not requirements:
        _fail("requirements.lock contains no exact package pins")
    if not saw_hash:
        _fail("requirements.lock contains no SHA-256 hashes")
    return requirements


def _record_contract(
    dist: importlib.metadata.Distribution,
    *,
    source_prefix: Path,
) -> tuple[dict[Path, tuple[bytes, int | None]], Path, bytes, str]:
    files = dist.files
    if files is None:
        _fail(f"installed dependency {dist.metadata['Name']} has no RECORD")
    records = [item for item in files if str(item).endswith(".dist-info/RECORD")]
    if len(records) != 1:
        _fail(f"installed dependency {dist.metadata['Name']} has an ambiguous RECORD")
    record_path = _regular_source(
        Path(dist.locate_file(records[0])), source_prefix, "dependency RECORD"
    )
    record_bytes = record_path.read_bytes()
    record_sha256 = hashlib.sha256(record_bytes).hexdigest()
    try:
        rows = csv.reader(io.StringIO(record_bytes.decode("utf-8"), newline=""))
        owned: dict[Path, tuple[bytes, int | None]] = {}
        for row in rows:
            if len(row) != 3:
                _fail("installed dependency has malformed RECORD data")
            relative, hash_spec, size_text = row
            target = _regular_source(
                Path(dist.locate_file(PurePosixPath(relative))),
                source_prefix,
                "dependency payload",
            )
            unhashed_allowed = relative.endswith(".dist-info/RECORD") or relative.endswith(
                ".pyc"
            )
            if not hash_spec:
                if not unhashed_allowed:
                    _fail("installed dependency contains an unhashed payload")
                continue
            algorithm, separator, encoded = hash_spec.partition("=")
            if separator != "=" or algorithm != "sha256" or not encoded:
                _fail("installed dependency RECORD uses an unapproved digest")
            try:
                expected = base64.urlsafe_b64decode(encoded + "===")
            except (ValueError, binascii.Error) as exc:
                raise RuntimeError("installed dependency RECORD digest is invalid") from exc
            if len(expected) != hashlib.sha256().digest_size:
                _fail("installed dependency RECORD digest length is invalid")
            try:
                expected_size = int(size_text) if size_text else None
            except ValueError as exc:
                raise RuntimeError("installed dependency RECORD size is invalid") from exc
            prior = owned.get(target)
            contract = (expected, expected_size)
            if prior is not None and prior != contract:
                _fail("installed dependency RECORD payload ownership conflicts")
            owned[target] = contract
    except UnicodeDecodeError as exc:
        raise RuntimeError("installed dependency RECORD is not UTF-8") from exc
    return owned, record_path, record_bytes, record_sha256


def _verified_bytes(path: Path, contract: tuple[bytes, int | None]) -> bytes:
    contents = path.read_bytes()
    expected, expected_size = contract
    if expected_size is not None and len(contents) != expected_size:
        _fail(f"dependency payload size changed: {path}")
    if hashlib.sha256(contents).digest() != expected:
        _fail(f"dependency payload hash changed: {path}")
    return contents


def _write_stage_file(source: Path, contents: bytes, source_prefix: Path, stage: Path) -> None:
    relative = source.relative_to(source_prefix)
    destination = stage / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        _fail(f"dependency stage path is duplicated: {relative}")
    with destination.open("xb") as stream:
        stream.write(contents)


def materialize(source_prefix: Path, stage: Path, requirements_lock: Path) -> dict[str, object]:
    source = source_prefix.resolve(strict=True)
    destination = stage.resolve(strict=True)
    if source == destination or source in destination.parents or destination in source.parents:
        _fail("dependency source and protected stage overlap")
    site_packages = source / "Lib" / "site-packages"
    if not site_packages.is_dir() or _is_reparse(site_packages):
        _fail("dependency source site-packages is not a regular directory")
    if any(destination.iterdir()):
        _fail("protected dependency stage is not empty")

    requirements = _locked_requirements(requirements_lock)
    installed: dict[str, list[importlib.metadata.Distribution]] = {}
    for dist in importlib.metadata.distributions(path=[str(site_packages)]):
        name = dist.metadata.get("Name")
        if not name:
            _fail("installed dependency metadata has no package name")
        normalized = re.sub(r"[-_.]+", "-", name).lower()
        installed.setdefault(normalized, []).append(dist)

    payloads: dict[Path, tuple[bytes, int | None]] = {}
    records: dict[Path, bytes] = {}
    contract_rows: list[str] = []
    for name, version in sorted(requirements.items()):
        matches = installed.get(name, [])
        if len(matches) != 1 or matches[0].version != version:
            _fail(f"source dependency does not exactly match requirements.lock: {name}")
        owned, record_path, record_bytes, record_sha256 = _record_contract(
            matches[0], source_prefix=source
        )
        for path, contract in owned.items():
            prior = payloads.get(path)
            if prior is not None and prior != contract:
                _fail("dependency payload ownership is ambiguous")
            payloads[path] = contract
        prior_record = records.get(record_path)
        if prior_record is not None and prior_record != record_bytes:
            _fail("dependency RECORD ownership is ambiguous")
        records[record_path] = record_bytes
        contract_rows.append(f"{name}\0{version}\0{record_sha256}\n")

    record_set = hashlib.sha256("".join(contract_rows).encode()).hexdigest()
    if record_set != APPROVED_RECORD_SET_SHA256:
        _fail("dependency RECORD set is not the source-approved runtime contract")

    for path in sorted(payloads, key=lambda item: os.path.normcase(str(item))):
        _write_stage_file(path, _verified_bytes(path, payloads[path]), source, destination)
    for path in sorted(records, key=lambda item: os.path.normcase(str(item))):
        _write_stage_file(path, records[path], source, destination)

    return {
        "schema_version": "dawnstrike.dependency_materialization.v1",
        "status": "PASS",
        "distribution_count": len(requirements),
        "payload_count": len(payloads),
        "record_count": len(records),
        "record_set_sha256": record_set,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-prefix", required=True)
    parser.add_argument("--stage-prefix", required=True)
    parser.add_argument("--requirements-lock", required=True)
    args = parser.parse_args()
    payload = materialize(
        Path(args.source_prefix), Path(args.stage_prefix), Path(args.requirements_lock)
    )
    print(json.dumps(payload, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
