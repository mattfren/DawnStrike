"""Verify the exact local toolchain permitted to mutate Dawnstrike on Vercel."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import stat
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

SCHEMA = "dawnstrike.vercel_toolchain.v1"
PYTHON_PATH = Path(r"C:\Program Files\Dawnstrike\Python313\python.exe")
PYTHON_SHA256 = "85b71d8c6ec1905935f74be0c9869aae198d00e98f39df699ec66f9c5a84cecd"
PYTHON_SIGNER_SUBJECT = (
    "CN=Python Software Foundation, O=Python Software Foundation, "
    "L=Beaverton, S=Oregon, C=US"
)
PYTHON_SIGNER_THUMBPRINT = "847785B686B2D3879731FA9AA3F1F5D48E85D99E"
GIT_PATH = Path(r"C:\Program Files\Dawnstrike\Git-2.55.0.5\cmd\git.exe")
GIT_SHA256 = "78211c7ed73988da93a6d8a33d47ec6187f464d7ea2a9a00c182bbd7a1ecf30f"
GIT_SIGNER_SUBJECT = (
    "CN=Johannes Schindelin, O=Johannes Schindelin, "
    "L=Bruehl, C=DE"
)
GIT_SIGNER_THUMBPRINT = "2A1E97CBF0DFCDA15B0DA0AC9745014F989D4AD0"
NODE_ROOT = Path(r"C:\Program Files\Dawnstrike\Node-24.20.0")
NODE_PATH = NODE_ROOT / "node.exe"
NODE_SHA256 = "5c976096e04e5c2c1f091938926234cc9fbebfe9787ddd149351b3b0ecc707b5"
NODE_BYTE_COUNT = 93_381_448
NODE_VERSION = "24.20.0"
NODE_ARCHIVE_URI = "https://nodejs.org/dist/v24.20.0/node-v24.20.0-win-x64.zip"
NODE_ARCHIVE_SHA256 = "6cac9ffbca8f6a47091e4b5c772e0606049c3871cb67d900c0cedde630e545ba"
NODE_ARCHIVE_BYTE_COUNT = 37_539_751
NODE_BOUNDARY_MANIFEST_NAME = ".dawnstrike-node-boundary-v1.json"
NODE_BOUNDARY_MANIFEST = NODE_ROOT / NODE_BOUNDARY_MANIFEST_NAME
NODE_SIGNER_SUBJECT = (
    "CN=OpenJS Foundation, O=OpenJS Foundation, "
    "L=San Francisco, S=California, C=US"
)
NODE_SIGNER_THUMBPRINT = "D1DC7755FAB17F01224CCCEA1C2FAEDC6F963E12"
UV_PATH = Path(r"C:\Program Files\Dawnstrike\Python313\Scripts\uv.exe")
UV_SHA256 = "b5d230c79ffa3629422f48bfce0766e9827769608a79bbb4e4e540081f59d97c"
VERCEL_VERSION = "59.11.2"
VERCEL_TREE_FILE_COUNT = 7131
VERCEL_TREE_SHA256 = "3bfb7509c4bf6a8fec920566c290a385c8160b9851b2350a655f0fd8b6c9e069"
VERCEL_ROOT = Path(rf"C:\Program Files\Dawnstrike\VercelCli-{VERCEL_TREE_SHA256}")
VERCEL_BOUNDARY_MANIFEST_NAME = ".dawnstrike-vercel-cli-boundary-v1.json"
VERCEL_BOUNDARY_MANIFEST = VERCEL_ROOT / VERCEL_BOUNDARY_MANIFEST_NAME
VERCEL_ENTRY_RELATIVE = "node_modules/vercel/dist/vc.js"
VERCEL_ENTRY = VERCEL_ROOT / Path(VERCEL_ENTRY_RELATIVE)
VERCEL_ENTRY_SHA256 = "2dd6e7c273a24bf4317af867d9b7bacb4db35487b42ea77912e2e7c33fa0c152"
VERCEL_EXECUTION_MODE = "javascript"
VERCEL_GLOBAL_CONFIG_POLICY = "fresh_isolated_directory_per_provider_call"
VERCEL_NETWORK_TRUST_POLICY = "direct_node_bundled_ca_no_proxy"


class ToolchainContractError(RuntimeError):
    """The local publication toolchain is absent, changed, or unsafe."""


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _assert_no_reparse(path: Path) -> None:
    cursor = path
    while True:
        details = cursor.lstat()
        if details.st_mode and stat.S_ISLNK(details.st_mode):
            raise ToolchainContractError(f"tool path contains a symlink: {path}")
        if getattr(details, "st_file_attributes", 0) & getattr(
            stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0
        ):
            raise ToolchainContractError(f"tool path contains a reparse point: {path}")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent


def _verify_file(path: Path, expected_sha256: str, label: str) -> dict[str, Any]:
    try:
        _assert_no_reparse(path)
        if not path.is_file():
            raise ToolchainContractError(f"{label} is not a regular file")
        observed = _sha256(path)
    except OSError as exc:
        raise ToolchainContractError(f"{label} is unavailable") from exc
    if observed != expected_sha256:
        raise ToolchainContractError(f"{label} hash changed")
    return {"path": str(path), "sha256": observed, "byte_count": path.stat().st_size}


def _verify_node_boundary() -> dict[str, Any]:
    try:
        _assert_no_reparse(NODE_ROOT)
        _assert_no_reparse(NODE_BOUNDARY_MANIFEST)
        manifest_bytes = NODE_BOUNDARY_MANIFEST.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        payload = json.loads(manifest_bytes)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolchainContractError("Node boundary manifest is unavailable") from exc
    expected_keys = {
        "archive_length",
        "archive_sha256",
        "archive_uri",
        "broker_execution_enabled",
        "files",
        "node_length",
        "node_sha256",
        "node_version",
        "research_only",
        "schema_version",
        "signer_subject",
        "signer_thumbprint",
    }
    if (
        not isinstance(payload, dict)
        or set(payload) != expected_keys
        or payload.get("schema_version") != "dawnstrike.node_boundary.v1"
        or payload.get("node_version") != NODE_VERSION
        or payload.get("archive_uri") != NODE_ARCHIVE_URI
        or payload.get("archive_sha256") != NODE_ARCHIVE_SHA256
        or payload.get("archive_length") != NODE_ARCHIVE_BYTE_COUNT
        or payload.get("node_sha256") != NODE_SHA256
        or payload.get("node_length") != NODE_BYTE_COUNT
        or payload.get("signer_subject") != NODE_SIGNER_SUBJECT
        or payload.get("signer_thumbprint") != NODE_SIGNER_THUMBPRINT
        or payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
        or payload.get("files")
        != [{"path": "node.exe", "length": NODE_BYTE_COUNT, "sha256": NODE_SHA256}]
    ):
        raise ToolchainContractError("Node boundary manifest contract is invalid")
    try:
        children = sorted(NODE_ROOT.iterdir(), key=lambda path: path.name.casefold())
        for child in children:
            _assert_no_reparse(child)
    except OSError as exc:
        raise ToolchainContractError("Node boundary file set is unavailable") from exc
    if children != sorted(
        [NODE_BOUNDARY_MANIFEST, NODE_PATH], key=lambda path: path.name.casefold()
    ):
        raise ToolchainContractError("Node boundary file set differs from its seal")
    node = _verify_file(NODE_PATH, NODE_SHA256, "Node")
    if node["byte_count"] != NODE_BYTE_COUNT:
        raise ToolchainContractError("Node byte count changed")
    return {
        "root": str(NODE_ROOT),
        **node,
        "version": NODE_VERSION,
        "archive_uri": NODE_ARCHIVE_URI,
        "archive_sha256": NODE_ARCHIVE_SHA256,
        "boundary_manifest_path": str(NODE_BOUNDARY_MANIFEST),
        "boundary_manifest_sha256": manifest_sha256,
    }


def _tree_entry(root: Path, path: Path) -> tuple[str, int, str]:
    _assert_no_reparse(path)
    relative = path.relative_to(root).as_posix()
    try:
        with path.open("rb") as handle:
            before = os.fstat(handle.fileno())
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
            after = os.fstat(handle.fileno())
        named = path.stat()
    except OSError as exc:
        raise ToolchainContractError(f"Vercel CLI file is unavailable: {relative}") from exc
    identity_fields = ("st_dev", "st_ino", "st_size", "st_mtime_ns")
    if any(getattr(before, field) != getattr(after, field) for field in identity_fields) or any(
        getattr(after, field) != getattr(named, field) for field in identity_fields
    ):
        raise ToolchainContractError(f"Vercel CLI file changed during verification: {relative}")
    return relative, after.st_size, digest.hexdigest()


def _safe_manifest_entries(payload: dict[str, Any]) -> dict[str, tuple[int, str]]:
    if (
        payload.get("schema_version") != "dawnstrike.vercel_cli_boundary.v1"
        or payload.get("tree_sha256") != VERCEL_TREE_SHA256
        or payload.get("file_count") != VERCEL_TREE_FILE_COUNT
        or payload.get("entry_relative_path") != VERCEL_ENTRY_RELATIVE
        or payload.get("entry_sha256") != VERCEL_ENTRY_SHA256
        or payload.get("vercel_version") != VERCEL_VERSION
        or payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
        or not isinstance(payload.get("files"), list)
    ):
        raise ToolchainContractError("Vercel CLI boundary manifest contract is invalid")
    expected: dict[str, tuple[int, str]] = {}
    for entry in payload["files"]:
        if not isinstance(entry, dict):
            raise ToolchainContractError("Vercel CLI boundary manifest entry is invalid")
        relative = entry.get("path")
        length = entry.get("length")
        digest = entry.get("sha256")
        if (
            not isinstance(relative, str)
            or not relative
            or "\\" in relative
            or ":" in relative
            or relative.startswith("/")
            or any(part in {"", ".", ".."} for part in relative.split("/"))
            or relative.casefold() == VERCEL_BOUNDARY_MANIFEST_NAME.casefold()
            or relative.casefold() in expected
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length < 0
            or not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ToolchainContractError("Vercel CLI boundary manifest entry is invalid")
        expected[relative.casefold()] = (length, digest)
    if len(expected) != VERCEL_TREE_FILE_COUNT:
        raise ToolchainContractError("Vercel CLI boundary manifest file count is invalid")
    return expected


def _verify_vercel_tree() -> dict[str, Any]:
    try:
        _assert_no_reparse(VERCEL_ROOT)
        _assert_no_reparse(VERCEL_BOUNDARY_MANIFEST)
        manifest_bytes = VERCEL_BOUNDARY_MANIFEST.read_bytes()
        manifest_sha256 = hashlib.sha256(manifest_bytes).hexdigest()
        manifest_payload = json.loads(manifest_bytes)
        if not isinstance(manifest_payload, dict):
            raise ToolchainContractError("Vercel CLI boundary manifest is not an object")
        expected = _safe_manifest_entries(manifest_payload)
        files: list[Path] = []
        for directory, dirnames, filenames in os.walk(VERCEL_ROOT, followlinks=False):
            directory_path = Path(directory)
            _assert_no_reparse(directory_path)
            for name in dirnames:
                _assert_no_reparse(directory_path / name)
            files.extend(
                directory_path / name
                for name in filenames
                if (directory_path / name) != VERCEL_BOUNDARY_MANIFEST
            )
        files.sort(key=lambda path: str(path).casefold())
        with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as pool:
            entries = list(pool.map(lambda path: _tree_entry(VERCEL_ROOT, path), files))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ToolchainContractError("Vercel CLI tool tree is unavailable") from exc
    observed_names: set[str] = set()
    for relative, length, digest in entries:
        key = relative.casefold()
        if key in observed_names or key not in expected or expected[key] != (length, digest):
            raise ToolchainContractError("Vercel CLI tool tree differs from its sealed manifest")
        observed_names.add(key)
    if observed_names != set(expected):
        raise ToolchainContractError("Vercel CLI tool tree differs from its sealed manifest")
    canonical = "\n".join(f"{name}|{size}|{digest}" for name, size, digest in entries)
    tree_sha256 = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if len(entries) != VERCEL_TREE_FILE_COUNT or tree_sha256 != VERCEL_TREE_SHA256:
        raise ToolchainContractError("Vercel CLI tool tree identity changed")
    entry = _verify_file(VERCEL_ENTRY, VERCEL_ENTRY_SHA256, "Vercel CLI entrypoint")
    package = json.loads((VERCEL_ROOT / "node_modules" / "vercel" / "package.json").read_text())
    if package.get("name") != "vercel" or package.get("version") != VERCEL_VERSION:
        raise ToolchainContractError("Vercel CLI package identity changed")
    return {
        "root": str(VERCEL_ROOT),
        "entry_path": str(VERCEL_ENTRY),
        "entry_sha256": entry["sha256"],
        "boundary_manifest_path": str(VERCEL_BOUNDARY_MANIFEST),
        "boundary_manifest_sha256": manifest_sha256,
        "version": VERCEL_VERSION,
        "file_count": len(entries),
        "tree_sha256": tree_sha256,
    }


def verify() -> dict[str, Any]:
    if os.name != "nt":
        raise ToolchainContractError("Vercel publication toolchain is Windows-host-only")
    if str(Path(sys.executable).resolve()).casefold() != str(PYTHON_PATH).casefold():
        raise ToolchainContractError("toolchain verifier is not using approved Python")
    payload: dict[str, Any] = {
        "schema_version": SCHEMA,
        "python": _verify_file(PYTHON_PATH, PYTHON_SHA256, "Python"),
        "git": _verify_file(GIT_PATH, GIT_SHA256, "Git"),
        "node": _verify_node_boundary(),
        "uv": _verify_file(UV_PATH, UV_SHA256, "uv"),
        "vercel_cli": _verify_vercel_tree(),
        "provider_execution": {
            "mode": VERCEL_EXECUTION_MODE,
            "global_config_policy": VERCEL_GLOBAL_CONFIG_POLICY,
            "network_trust_policy": VERCEL_NETWORK_TRUST_POLICY,
            "native_binary_allowed": False,
        },
        "authenticode": {
            "python": {
                "subject": PYTHON_SIGNER_SUBJECT,
                "thumbprint": PYTHON_SIGNER_THUMBPRINT,
            },
            "git": {
                "subject": GIT_SIGNER_SUBJECT,
                "thumbprint": GIT_SIGNER_THUMBPRINT,
            },
            "node": {
                "subject": NODE_SIGNER_SUBJECT,
                "thumbprint": NODE_SIGNER_THUMBPRINT,
            },
        },
        "research_only": True,
        "broker_execution_enabled": False,
    }
    payload["toolchain_identity_sha256"] = hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("verify",))
    args = parser.parse_args()
    if args.command == "verify":
        print(json.dumps(verify(), sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except ToolchainContractError as exc:
        print(json.dumps({"status": "BLOCKED", "error": str(exc)}, sort_keys=True))
        raise SystemExit(2) from None
