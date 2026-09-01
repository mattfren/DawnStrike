"""Exact-host and hostile checks for the production Vercel toolchain boundary."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

from scripts import vercel_toolchain_contract as contract


@pytest.mark.skipif(sys.platform != "win32", reason="production toolchain is Windows-only")
def test_exact_vercel_toolchain_is_current() -> None:
    payload = contract.verify()
    assert payload["schema_version"] == contract.SCHEMA
    assert payload["vercel_cli"]["version"] == "58.4.0"
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


def test_publisher_has_no_path_or_npx_provider_resolution() -> None:
    publisher = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    source_contract = Path("scripts/vercel_source_contract.ps1").read_text(
        encoding="utf-8"
    )
    assert "Get-Command node.exe" not in publisher
    assert "--yes\", \"vercel@" not in publisher
    assert '@($vercelEntryPath) + @("--global-config", $callConfigRoot)' in publisher
    assert 'VERCEL_CLI_USE_NATIVE_BINARY = "0"' in publisher
    assert 'VERCEL_VC_NATIVE = "0"' in publisher
    assert '"--global-config", $callConfigRoot' in publisher
    for variable in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NODE_EXTRA_CA_CERTS",
        "NODE_TLS_REJECT_UNAUTHORIZED",
        "SSL_CERT_FILE",
        "CURL_CA_BUNDLE",
        "VERCEL_ORG_ID",
        "VERCEL_PROJECT_ID",
        "VERCEL_TOKEN",
    ):
        assert f'{variable} = ""' in publisher
    provider = publisher.split("function Invoke-VercelProcess", 1)[1].split(
        "function Assert-RemoteVercelSourceManifest", 1
    )[0]
    assert provider.index("Assert-VercelPublicationToolchainStable") < provider.index(
        "Invoke-DawnstrikeJobProcess"
    )
    assert "if ($name -like 'GIT_*')" in source_contract
    assert "$startInfo.EnvironmentVariables.Remove($name)" in source_contract
    assert "GIT_CONFIG_NOSYSTEM" in source_contract
