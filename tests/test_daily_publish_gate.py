from pathlib import Path

from scripts.verify_public_artifact import verify


def test_artifact_gate_reports_bounded_public_payload(tmp_path: Path) -> None:
    result = verify(tmp_path / "missing-public")
    assert result["status"] == "FAIL"
    assert "missing:build-manifest.json" in result["errors"]
