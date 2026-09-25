"""Exact-host and hostile checks for the production Vercel toolchain boundary."""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

from scripts import vercel_toolchain_contract as contract


def _seal_node_fixture(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    node = root / "node.exe"
    node.parent.mkdir(parents=True)
    node.write_bytes(b"sealed-node-fixture")
    node_hash = hashlib.sha256(node.read_bytes()).hexdigest()
    manifest = root / ".dawnstrike-node-boundary-v1.json"
    payload = {
        "schema_version": "dawnstrike.node_boundary.v1",
        "node_version": "24.20.0-test",
        "archive_uri": "https://nodejs.org/test/node.zip",
        "archive_sha256": "a" * 64,
        "archive_length": 100,
        "node_sha256": node_hash,
        "node_length": node.stat().st_size,
        "signer_subject": "CN=Fixture",
        "signer_thumbprint": "B" * 40,
        "files": [{"path": "node.exe", "length": node.stat().st_size, "sha256": node_hash}],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(contract, "NODE_ROOT", root)
    monkeypatch.setattr(contract, "NODE_PATH", node)
    monkeypatch.setattr(contract, "NODE_SHA256", node_hash)
    monkeypatch.setattr(contract, "NODE_BYTE_COUNT", node.stat().st_size)
    monkeypatch.setattr(contract, "NODE_VERSION", payload["node_version"])
    monkeypatch.setattr(contract, "NODE_ARCHIVE_URI", payload["archive_uri"])
    monkeypatch.setattr(contract, "NODE_ARCHIVE_SHA256", payload["archive_sha256"])
    monkeypatch.setattr(contract, "NODE_ARCHIVE_BYTE_COUNT", payload["archive_length"])
    monkeypatch.setattr(contract, "NODE_SIGNER_SUBJECT", payload["signer_subject"])
    monkeypatch.setattr(contract, "NODE_SIGNER_THUMBPRINT", payload["signer_thumbprint"])
    monkeypatch.setattr(contract, "NODE_BOUNDARY_MANIFEST", manifest)
    return manifest


def _seal_vercel_fixture(monkeypatch: pytest.MonkeyPatch, root: Path) -> Path:
    entry = root / "node_modules" / "vercel" / "dist" / "vc.js"
    package = root / "node_modules" / "vercel" / "package.json"
    entry.parent.mkdir(parents=True)
    entry.write_bytes(b"console.log('sealed fixture')\n")
    package.write_text(
        json.dumps({"name": "vercel", "version": contract.VERCEL_VERSION}), encoding="utf-8"
    )
    manifest_name = ".dawnstrike-vercel-cli-boundary-v1.json"
    entries = []
    for path in sorted(root.rglob("*"), key=lambda value: str(value).casefold()):
        if not path.is_file() or path.name == manifest_name:
            continue
        content = path.read_bytes()
        entries.append(
            {
                "path": path.relative_to(root).as_posix(),
                "length": len(content),
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        )
    canonical = "\n".join(
        f"{item['path']}|{item['length']}|{item['sha256']}" for item in entries
    )
    tree_hash = hashlib.sha256(canonical.encode()).hexdigest()
    manifest = root / manifest_name
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.vercel_cli_boundary.v1",
                "tree_sha256": tree_hash,
                "file_count": len(entries),
                "entry_relative_path": entry.relative_to(root).as_posix(),
                "entry_sha256": hashlib.sha256(entry.read_bytes()).hexdigest(),
                "vercel_version": contract.VERCEL_VERSION,
                "files": entries,
                "research_only": True,
                "broker_execution_enabled": False,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(contract, "VERCEL_ROOT", root)
    monkeypatch.setattr(contract, "VERCEL_BOUNDARY_MANIFEST_NAME", manifest_name)
    monkeypatch.setattr(contract, "VERCEL_BOUNDARY_MANIFEST", manifest)
    monkeypatch.setattr(contract, "VERCEL_ENTRY_RELATIVE", entry.relative_to(root).as_posix())
    monkeypatch.setattr(contract, "VERCEL_ENTRY", entry)
    monkeypatch.setattr(
        contract,
        "VERCEL_ENTRY_SHA256",
        hashlib.sha256(entry.read_bytes()).hexdigest(),
    )
    monkeypatch.setattr(contract, "VERCEL_TREE_FILE_COUNT", len(entries))
    monkeypatch.setattr(contract, "VERCEL_TREE_SHA256", tree_hash)
    return manifest


@pytest.mark.skipif(
    sys.platform != "win32"
    or str(Path(sys.executable).resolve()).casefold()
    != str(contract.PYTHON_PATH).casefold()
    or not contract.VERCEL_BOUNDARY_MANIFEST.is_file(),
    reason="exact protected production toolchain is not installed for this interpreter",
)
def test_exact_vercel_toolchain_is_current() -> None:
    payload = contract.verify()
    assert payload["schema_version"] == contract.SCHEMA
    assert payload["vercel_cli"]["version"] == "59.11.2"
    assert payload["vercel_cli"]["file_count"] == contract.VERCEL_TREE_FILE_COUNT
    assert payload["vercel_cli"]["tree_sha256"] == contract.VERCEL_TREE_SHA256
    assert payload["provider_execution"] == {
        "mode": "javascript",
        "global_config_policy": "fresh_isolated_directory_per_provider_call",
        "network_trust_policy": "direct_node_bundled_ca_no_proxy",
        "native_binary_allowed": False,
    }
    assert payload["research_only"] is True
    assert payload["broker_execution_enabled"] is False


def test_file_hash_mismatch_is_rejected(tmp_path: Path) -> None:
    executable = tmp_path / "tool.exe"
    executable.write_bytes(b"attacker")
    with pytest.raises(contract.ToolchainContractError, match="hash changed"):
        contract._verify_file(executable, "0" * 64, "hostile tool")


def test_sealed_protected_node_boundary_is_exact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Node-fixture"
    manifest = _seal_node_fixture(monkeypatch, root)

    payload = contract._verify_node_boundary()

    assert payload["root"] == str(root)
    assert payload["path"] == str(root / "node.exe")
    assert payload["boundary_manifest_path"] == str(manifest)

    (root / "extra.js").write_bytes(b"hostile")
    with pytest.raises(contract.ToolchainContractError, match="file set differs"):
        contract._verify_node_boundary()


def test_sealed_node_boundary_rejects_manifest_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "Node-fixture"
    manifest = _seal_node_fixture(monkeypatch, root)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["node_sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(contract.ToolchainContractError, match="manifest contract"):
        contract._verify_node_boundary()


def test_sealed_protected_vercel_tree_is_accepted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "VercelCli-fixture"
    _seal_vercel_fixture(monkeypatch, root)

    payload = contract._verify_vercel_tree()

    assert payload["root"] == str(root)
    assert payload["boundary_manifest_path"] == str(
        root / ".dawnstrike-vercel-cli-boundary-v1.json"
    )
    assert len(payload["boundary_manifest_sha256"]) == 64


def test_sealed_vercel_tree_rejects_post_seal_file_mutation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "VercelCli-fixture"
    _seal_vercel_fixture(monkeypatch, root)
    (root / "node_modules" / "vercel" / "dist" / "vc.js").write_bytes(b"hostile")

    with pytest.raises(contract.ToolchainContractError, match="sealed manifest"):
        contract._verify_vercel_tree()


def test_sealed_vercel_tree_rejects_manifest_substitution(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / "VercelCli-fixture"
    manifest = _seal_vercel_fixture(monkeypatch, root)
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["tree_sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(contract.ToolchainContractError, match="manifest contract"):
        contract._verify_vercel_tree()


def test_publisher_has_no_path_or_npx_provider_resolution() -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    source_contract = Path("scripts/vercel_source_contract.ps1").read_text(
        encoding="utf-8"
    )
    assert "Get-Command node.exe" not in publisher
    assert r"C:\Users\MattFields\AppData\Local\npm-cache" not in publisher
    assert r"C:\Program Files\Dawnstrike\VercelCli-" in publisher
    assert r"C:\Program Files\Dawnstrike\Node-24.20.0" in publisher
    assert ".dawnstrike-node-boundary-v1.json" in publisher
    assert "Assert-DawnstrikeProcessProtectedPath -Path $expectedNodeRoot" in publisher
    assert ".dawnstrike-vercel-cli-boundary-v1.json" in publisher
    assert "Assert-DawnstrikeProcessProtectedPath -Path $expectedVercelRoot" in publisher
    assert "--yes\", \"vercel@" not in publisher
    assert "function Invoke-VercelProcess" not in publisher
    assert '"--global-config", $callConfigRoot' not in publisher
    assert "New-VercelDeterministicBuiltPackage -StageRoot $stage" in publisher
    assert "New-VercelFrozenPreviewDeployment" in publisher
    assert "Invoke-VercelBoundedApiRequest" in publisher
    assert "AuthenticationHeaderValue]::new('Bearer', $token)" in publisher
    assert '"--token"' not in publisher
    assert "if ($name -like 'GIT_*')" in source_contract
    assert "$startInfo.EnvironmentVariables.Remove($name)" in source_contract
    assert "GIT_CONFIG_NOSYSTEM" in source_contract
