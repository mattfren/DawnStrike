import json
from pathlib import Path


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


def test_stage_builder_declares_dependency_free_python_stage() -> None:
    script = Path("scripts/build_vercel_public_stage.ps1").read_text(encoding="utf-8")
    assert "dawnstrike-public-stage" in script
    assert "dependencies = []" in script
    assert "api/public/**" in script
    assert '$functionPublic = Join-Path $stage "api\\public"' in script
    assert "performance-snapshot.json" in script
    assert "performance-snapshot-manifest.json" in script
    assert "scenarios.json.manifest.json" in script
    assert "scenario_b64" in script
    assert "opportunity-projection.json.manifest.json" in script
    assert "opportunity_b64" in script
    assert '"static_file_hashes_verified":true' in script
    assert "api\\public_state.py" in script
    assert "function Read-RawJsonObject" in script
    assert "ConvertFrom-Json -InputObject $raw" in script
    assert "return the original text" in script
    assert "System.Text.Json" not in script
    assert "$stateJson = '{' + ($stateParts -join ',') + '}'" in script
    assert "ISO timestamps and" in script
    assert "StageRoot must resolve inside the project build directory" in script
    assert "StageRoot must not overlap the source public artifact" in script
    assert "[System.IO.Path]::GetFullPath($stageCandidate)" in script
    assert "$securityHeaders = @(" in script
    assert "Content-Security-Policy" in script
    assert "frame-ancestors 'none'" in script
    assert "headers = $securityHeaders" in script


def test_candidate_verifier_reads_optional_config_fields_under_strict_mode() -> None:
    script = Path("scripts/verify_vercel_candidate.ps1").read_text(encoding="utf-8")

    assert '$config.PSObject.Properties["routes"]' in script
    assert '$config.PSObject.Properties["crons"]' in script
    assert "$config.routes" not in script
    assert "$config.crons" not in script
    assert "$null -ne $routesProperty" in script
    assert "$null -ne $cronsProperty" in script


def test_daily_vercel_publisher_builds_once_verifies_and_can_roll_back() -> None:
    script = Path("scripts/publish_vercel_public.ps1").read_text(encoding="utf-8")

    assert "vercel@58.4.0" in script
    assert "sysconfig.get_path('scripts')" in script
    assert "build --yes --project" in script
    assert '"--prebuilt"' in script
    assert "verify_vercel_candidate.ps1" in script
    assert "AllowDegraded" in script
    assert "promote" in script
    assert "rollback" in script
    assert "AdditionalProductionAliases" in script
    assert "Set-VercelAlias" in script
    assert '($AliasUrl -replace "^https?://", "").TrimEnd("/")' in script
    assert "function Get-OptionalJsonProperty" in script
    assert '$InputObject.PSObject.Properties[$Name]' in script
    assert '-Name "deployment"' in script
    assert '-Name "originalDeploymentId"' in script
    assert "$deploymentResponse.deployment" not in script
    assert "$_.meta" not in script
    assert "$priorProduction.id" not in script
    assert "$deployment.readyState" not in script
    assert "$stderrPath = [System.IO.Path]::GetTempFileName()" in script
    assert "2> $stderrPath" in script
    assert "[System.IO.File]::ReadAllText($stderrPath).Trim()" in script
    assert "curl progress can otherwise be interleaved" in script
    assert "Promoted deployment does not match the verified preview" in script
    assert "Production does not match the verified preview" in script
    assert "foreach ($alias in $allProductionAliases)" in script
    assert "Assert-PublicationState" in script
    assert "Production verification did not converge" in script
    assert "Start-Sleep -Seconds 3" in script
