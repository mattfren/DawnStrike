import json
from pathlib import Path


def test_vercel_config_is_static_and_minimal() -> None:
    config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
    assert config["outputDirectory"] == "build/public"
    assert sorted(config["functions"]) == ["api/health.py", "api/readiness.py"]
    assert "routes" not in config
    assert "crons" not in config


def test_stage_builder_declares_dependency_free_python_stage() -> None:
    script = Path("scripts/build_vercel_public_stage.ps1").read_text(encoding="utf-8")
    assert "dawnstrike-public-stage" in script
    assert "dependencies = []" in script
    assert 'api/public/**' in script
    assert '$functionPublic = Join-Path $stage "api\\public"' in script
    assert 'performance-snapshot.json' in script
    assert 'performance-snapshot-manifest.json' in script
    assert 'static_file_hashes_verified = $true' in script
    assert 'api\\public_state.py' in script


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
    assert "$_.meta.originalDeploymentId -eq $deployment.id" in script
    assert "Promoted deployment does not match the verified preview" in script
    assert "Production does not match the verified preview" in script
    assert "foreach ($alias in $allProductionAliases)" in script
    assert "Assert-PublicationState" in script
    assert "Production verification did not converge" in script
    assert "Start-Sleep -Seconds 3" in script
