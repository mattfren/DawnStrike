from __future__ import annotations

from pathlib import Path

from scripts.verify_public_artifact_security import scan_public_artifact


def test_safe_public_artifact_passes_security_gate(tmp_path: Path) -> None:
    artifact = tmp_path / "public"
    artifact.mkdir()
    (artifact / "status.json").write_text(
        '{"source_status":"HEALTHY","holdout_status":"INSUFFICIENT_EVIDENCE"}',
        encoding="utf-8",
    )

    assert scan_public_artifact(artifact) == []


def test_public_artifact_rejects_private_values(tmp_path: Path) -> None:
    artifact = tmp_path / "public"
    artifact.mkdir()
    (artifact / "unsafe.json").write_text(
        """{
          "database_path": "C:\\\\r\\\\dawnstrike-state\\\\shadow_real.sqlite",
          "holdout_evaluation_id": "v6eval-0123456789abcdef",
          "api_key": "not-a-real-key-123456789"
        }""",  # pragma: allowlist secret
        encoding="utf-8",
    )

    rules = {violation.rule for violation in scan_public_artifact(artifact)}

    assert {"local_or_runtime_path", "raw_holdout_identifier", "credential_value"} <= rules
