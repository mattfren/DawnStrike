import hashlib
import json
from pathlib import Path


def test_generated_runtime_authority_is_exact_and_closed_world() -> None:
    authority_path = Path("config/vercel_generated_runtime_authority.v1.json")
    raw = authority_path.read_bytes()
    assert hashlib.sha256(raw).hexdigest() == (
        "3629e094ef8d8c7f64e4ebd0111e76af9e848fab03cd61355d215f2d387cc4a4"
    )
    authority = json.loads(raw)
    assert authority["authority_contract_sha256"] == (
        "26d187b155c3b30486457cef3a3bd3c0830a1bf882a039c76a04e3059de198e3"
    )
    assert authority["generated_map_sha256"] == (
        "9f19cbdeee79bacdc9ce2959f3bbf05cccd0b80ebcf498f36248d820d55a9818"
    )
    assert authority["vendor_map_sha256"] == (
        "30b99a313bf016f9e14e291b57c3590ab766cfde0a3313f7ec53c6ac72a03904"
    )
    assert len(authority["generated_files"]) == 8
    assert len(authority["vendor_files"]) == 640
    assert {record["target"] for record in authority["generated_files"]} == {
        ".python-version",
        ".vercel/output/config.json",
        ".vercel/output/functions/api/health.func/.vc-config.json",
        ".vercel/output/functions/api/health.func/vc__handler__python.py",
        ".vercel/output/functions/api/readiness.func/.vc-config.json",
        ".vercel/output/functions/api/readiness.func/vc__handler__python.py",
        "api/public_state.py",
        "uv.lock",
    }
    assert all(record["kind"] == "vendor_file" for record in authority["vendor_files"])


def test_vercel_config_is_static_and_minimal() -> None:
    config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
    assert config["outputDirectory"] == "build/public"
    assert config["git"] == {"deploymentEnabled": False}
    assert sorted(config["functions"]) == ["api/health.py", "api/readiness.py"]
    expected_excludes = "{requirements.in,requirements.lock,**/__pycache__/**,**/*.pyc}"
    assert all(
        function["excludeFiles"] == expected_excludes for function in config["functions"].values()
    )
    assert "routes" not in config
    assert "crons" not in config

    ignored = Path(".vercelignore").read_text(encoding="utf-8").splitlines()
    assert {"pyproject.toml", "requirements.in", "requirements.lock"} <= set(ignored)
    assert Path(".python-version").read_text(encoding="utf-8").strip() == "3.13"

    assert len(config["headers"]) == 1
    assert config["headers"][0]["source"] == "/(.*)"
    security_headers = {
        header["key"]: header["value"] for header in config["headers"][0]["headers"]
    }
    assert security_headers == {
        "Content-Security-Policy": (
            "default-src 'self'; base-uri 'none'; object-src 'none'; "
            "frame-ancestors 'none'; form-action 'none'; script-src 'self'; "
            "style-src 'self' 'unsafe-inline'; img-src 'self' data:; font-src 'self'; "
            "connect-src 'self'; manifest-src 'self'; upgrade-insecure-requests"
        ),
        "Cross-Origin-Opener-Policy": "same-origin",
        "Cross-Origin-Resource-Policy": "same-origin",
        "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
        "Referrer-Policy": "no-referrer",
        "X-Content-Type-Options": "nosniff",
        "X-Frame-Options": "DENY",
    }


def test_stage_builder_pins_the_exact_vercel_python_runtime() -> None:
    script = Path("scripts/build_vercel_public_stage.ps1").read_text(encoding="utf-8")
    assert "dawnstrike-public-stage" in script
    assert 'dependencies = ["vercel-runtime==0.22.1"]' in script
    assert "function_public/**" in script
    assert '$functionPublic = Join-Path $stage "function_public"' in script
    assert 'Copy-Item -Path (Join-Path $stagePublic "*") -Destination $functionPublic' in script
    assert script.count('Copy-Item -Path (Join-Path $publicSource "*")') == 1
    assert "Static and function public artifact snapshots diverged." in script
    assert "performance-snapshot.json" not in script
    assert "performance-snapshot-manifest.json" not in script
    assert "Assert-VercelPublicArtifactInventory" in script
    assert "scenario_b64" not in script
    assert "opportunity_b64" not in script
    assert "snapshot_b64" not in script
    assert "calendar_b64" not in script
    assert "static_file_hashes_verified" in script
    assert "api\\public_state.py" in script
    assert "Copy-Item -LiteralPath (Join-Path $resolvedRoot \"api\\public_state.py\")" not in script
    assert "System.Text.Json" not in script
    assert "StageRoot must resolve inside the project build directory" in script
    assert "StageRoot must not overlap the source public artifact" in script
    assert "[System.IO.Path]::GetFullPath($stageCandidate)" in script
    assert "$securityHeaders = @(" in script
    assert "Content-Security-Policy" in script
    assert "frame-ancestors 'none'" in script
    assert "headers = $securityHeaders" in script
    assert "vercel_source_contract.ps1" in script
    assert "ExpectedSourceTree" in script
    assert "Write-VercelGitBlob" in script
    assert "vercel-source-manifest.json" in script
    assert 'Destination (Join-Path $stagePublic "vercel-source-manifest.json")' in script
    assert 'Destination (Join-Path $functionPublic "vercel-source-manifest.json")' in script
    assert "Assert-VercelGitSourceStable" in script
    assert "Assert-VercelBuiltPackage" in Path(
        "scripts/vercel_source_contract.ps1"
    ).read_text(encoding="utf-8")
    assert "vercel\\output" in Path(
        "scripts/vercel_source_contract.ps1"
    ).read_text(encoding="utf-8")


def test_candidate_verifier_reads_optional_config_fields_under_strict_mode() -> None:
    script = Path("scripts/verify_vercel_candidate.ps1").read_text(encoding="utf-8")

    assert '$config.PSObject.Properties["routes"]' in script
    assert '$config.PSObject.Properties["crons"]' in script
    assert "$config.routes" not in script
    assert "$config.crons" not in script
    assert "$null -ne $routesProperty" in script
    assert "$null -ne $cronsProperty" in script
    assert "ExpectedSourceTree" in script
    assert "Assert-VercelStagedSourceManifest" in script


def test_daily_vercel_publisher_builds_once_verifies_and_can_roll_back() -> None:
    script = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")

    assert "vercel_toolchain_contract.py" in script
    assert "$vercelEntryPath" in script
    assert "toolchain_identity_sha256" in script
    assert "New-VercelDeterministicBuiltPackage -StageRoot $stage" in script
    assert '-Arguments @("build", "--yes", "--project", $ProjectId)' not in script
    assert "function Invoke-VercelProcess" not in script
    assert "No local Node, Python, uv, Vercel" in script
    assert "&prebuilt=1" in script
    assert "New-VercelFrozenPreviewDeployment" in script
    assert "Get-VercelRemoteDeploymentFileAttestation" in script
    assert "verify_vercel_candidate.ps1" in script
    assert "AllowDegraded" in script
    assert "promote" in script
    assert "rollback" in script
    assert "AdditionalProductionAliases" in script
    assert "Set-VercelAlias" in script
    assert '([string]$Value -replace "^https?://", "").TrimEnd("/")' in script
    assert "function Get-OptionalJsonProperty" in script
    assert '$InputObject.PSObject.Properties[$Name]' in script
    assert "$deploymentResponse.deployment" not in script
    assert "$_.meta" not in script
    assert "$priorProduction.id" not in script
    assert "$deployment.readyState" not in script
    assert "Assert-VercelNodeIdentity" in script
    assert "Get-Command node.exe" not in script
    assert '"node_modules\\npm\\bin\\npx-cli.js"' not in script
    assert '. (Join-Path $PSScriptRoot "dawnstrike_job_process.ps1")' in script
    assert "Invoke-DawnstrikeJobProcess" in script
    assert "VercelBuildTimeoutSeconds = 600" in script
    assert "VercelCommandTimeoutSeconds = 180" in script
    assert "& npx" not in script
    assert "$handler.AllowAutoRedirect = $false" in script
    assert "taskkill.exe" not in script
    assert "Promoted deployment does not match the verified preview" in script
    assert "Production does not match the verified preview" in script
    assert "A timeout can occur after Vercel accepted the promotion" in script
    assert script.index("$promoted = $true") < script.index(
        "Request-VercelProductionPromotion -PreviewDeploymentId $deploymentId"
    )
    assert "foreach ($alias in $allProductionAliases)" in script
    assert "Assert-PublicationState" in script
    assert "Production verification did not converge" in script
    assert "Start-Sleep -Seconds 3" in script
    assert "vercel_source_contract.ps1" in script
    assert "ExpectedSourceTree" in script
    assert "Publication source HEAD changed during staging or deployment" in Path(
        "scripts/vercel_source_contract.ps1"
    ).read_text(encoding="utf-8")
    assert script.count("Assert-VercelStagedSourceManifest") >= 2
    assert "Assert-RemoteVercelSourceManifest" in script
    build_position = script.index("New-VercelDeterministicBuiltPackage -StageRoot $stage")
    deploy_position = script.index(
        "New-VercelFrozenPreviewDeployment `",
        build_position,
    )
    built_positions = [
        index for index in range(len(script))
        if script.startswith("Assert-VercelBuiltPackage", index)
    ]
    assert len(built_positions) >= 3
    # The package is checked once as soon as direct construction completes, again
    # immediately before the frozen CAS deploy, and once more before promotion.
    assert build_position < built_positions[0] < built_positions[1] < deploy_position
    assert built_positions[2] > deploy_position


def test_promotion_rollback_restores_each_alias_snapshot() -> None:
    script = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")
    snapshot_loop = script.index(
        'foreach ($alias in $allProductionAliases) {',
        script.index("$priorProductionAliases"),
    )
    promotion_marker = script.index("$promoted = $true")
    rollback_loop = script.index('foreach ($alias in $allProductionAliases) {', promotion_marker)
    assert snapshot_loop < promotion_marker < rollback_loop
    assert "Get-VercelAliasObservation -Alias ([string]$alias)" in script
    assert "$priorProductionAliases[[string]$alias]" in script
    assert "$priorAlias = $priorProductionAliases[[string]$alias]" in script
    assert "-DeploymentUrl ([string]$priorAlias.url)" in script
    assert "a complete per-alias production snapshot was not captured" in script
    assert "function Assert-VercelAliasRestored" in script
    assert "Get-VercelAliasObservation -Alias $AliasUrl" in script
    assert "Rollback verification for $AliasUrl resolved the wrong deployment ID" in script
    assert "Rollback verification for $AliasUrl resolved the wrong deployment URL" in script
    assert "daily-deployment-rollback-result.json" in script
    assert "candidate_no_longer_live = [bool]$rollbackSucceeded" in script
    assert 'status = if ($rollbackSucceeded) { "ROLLED_BACK" } else { "ROLLBACK_FAILED" }' in script
    assert "alias_errors = @($rollbackErrors)" in script
    assert "source_manifest_available" in script
    assert "Request-VercelProductionRollback -DeploymentId ([string]$priorPrimary.id)" in script
    assert '"--token"' not in script
    assert "primary_rollback_proof" in script
    assert "Add-VercelFunctionPublicBindings" in script
    assert "Assert-VercelNoEnvironmentArtifacts" in script
    assert "$($result.Stderr)" not in script
