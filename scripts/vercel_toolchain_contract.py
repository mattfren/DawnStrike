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
PYTHON_SHA256 = "ef8f51028ac5329641985112f8efb1c2d4c47c86b8011ddf7e6fae21e2b4e5a1"
PYTHON_SIGNER_SUBJECT = (
    "CN=Python Software Foundation, O=Python Software Foundation, "
    "L=Beaverton, S=Oregon, C=US"
)
PYTHON_SIGNER_THUMBPRINT = "9BA3C2E210C7E8296C5056515BFC0B0BBA78AC48"
GIT_PATH = Path(r"C:\Program Files\Git\cmd\git.exe")
GIT_SHA256 = "37c5725818d602e951ba2563b870d62763322956b73373da4c33a0b566a80bc9"
GIT_SIGNER_SUBJECT = (
    "CN=Johannes Schindelin, O=Johannes Schindelin, "
    "S=Nordrhein-Westfalen, C=DE"
)
GIT_SIGNER_THUMBPRINT = "3EB14A3AEF84B7153E139397F0A49E2FAC662B0E"
NODE_PATH = Path(r"C:\Program Files\nodejs\node.exe")
NODE_SHA256 = "58e74bf02fc5bbacc41dcb8bef089961cd5bddd37830b87784e4fc624d145d1f"
NODE_SIGNER_SUBJECT = (
    "CN=OpenJS Foundation, O=OpenJS Foundation, "
    "L=San Francisco, S=California, C=US"
)
NODE_SIGNER_THUMBPRINT = "C293811538EEFF337F0AD4F2DCB7E7B388CDA38B"
CURL_PATH = Path(r"C:\Windows\System32\curl.exe")
CURL_SHA256 = "73d24149ff289afc49ec41f08918ef9faa727d39ad993e929757dc2ddafab805"
CURL_SIGNER_SUBJECT = (
    "CN=Microsoft Windows, O=Microsoft Corporation, "
    "L=Redmond, S=Washington, C=US"
)
CURL_SIGNER_THUMBPRINT = "DC91E564D5BC1E3A8E02D6A8508682ABEA8A2443"
NPX_PATH = Path(r"C:\Program Files\nodejs\node_modules\npm\bin\npx-cli.js")
NPX_SHA256 = "a9ca027c18c5bd7da278230edc7a174ff1d8b6b558e0e0a4a2c9c2fae346d66b"
UV_PATH = Path(r"C:\Program Files\Dawnstrike\Python313\Scripts\uv.exe")
UV_SHA256 = "268cd62b99395eb53825795518e067e4b27ec4b445175df343824689f307c807"
VERCEL_ROOT = Path(r"C:\Users\MattFields\AppData\Local\npm-cache\_npx\055e0f88b112d3d7")
VERCEL_ENTRY = VERCEL_ROOT / "node_modules" / "vercel" / "dist" / "vc.js"
VERCEL_ENTRY_SHA256 = "56b16d6893212069398eb30e2d96943421cd8a5ba7ea3372a1dd5743ed23d363"
VERCEL_VERSION = "58.4.0"
VERCEL_TREE_FILE_COUNT = 6838
VERCEL_TREE_SHA256 = "1b666342aa264ad61d79cecc49b38e2e0675f776f20fc195e83a86ff60b99e2f"
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


def _tree_entry(root: Path, path: Path) -> tuple[str, int, str]:
    _assert_no_reparse(path)
    relative = path.relative_to(root).as_posix()
    return relative, path.stat().st_size, _sha256(path)


def _verify_vercel_tree() -> dict[str, Any]:
    try:
        _assert_no_reparse(VERCEL_ROOT)
        files: list[Path] = []
        for directory, dirnames, filenames in os.walk(VERCEL_ROOT, followlinks=False):
            directory_path = Path(directory)
            _assert_no_reparse(directory_path)
            for name in dirnames:
                _assert_no_reparse(directory_path / name)
            files.extend(directory_path / name for name in filenames)
        files.sort(key=lambda path: str(path).casefold())
        with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 4) * 2)) as pool:
            entries = list(pool.map(lambda path: _tree_entry(VERCEL_ROOT, path), files))
    except OSError as exc:
        raise ToolchainContractError("Vercel CLI tool tree is unavailable") from exc
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
        "node": _verify_file(NODE_PATH, NODE_SHA256, "Node"),
        "curl": _verify_file(CURL_PATH, CURL_SHA256, "curl"),
        "npx": _verify_file(NPX_PATH, NPX_SHA256, "npx entrypoint"),
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
            "curl": {
                "subject": CURL_SIGNER_SUBJECT,
                "thumbprint": CURL_SIGNER_THUMBPRINT,
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
