import json
from pathlib import Path


def test_vercel_config_is_static_and_minimal() -> None:
    config = json.loads(Path("vercel.json").read_text(encoding="utf-8"))
    assert config["outputDirectory"] == "build/public"
    assert sorted(config["functions"]) == ["api/health.py", "api/readiness.py"]
    assert "routes" not in config
    assert "crons" not in config
