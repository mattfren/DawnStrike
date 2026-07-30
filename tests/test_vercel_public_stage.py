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
