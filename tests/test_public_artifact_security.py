from __future__ import annotations

from scripts.verify_public_artifact import ABSOLUTE_PATH_PATTERN, verify


def test_public_artifact_security_rejects_windows_and_unix_host_paths(tmp_path) -> None:
    assert ABSOLUTE_PATH_PATTERN.search(r"C:\r\dawnstrike-state")
    assert ABSOLUTE_PATH_PATTERN.search("/home/operator/dawnstrike")
    assert "missing:index.html" in verify(tmp_path / "missing")["errors"]
