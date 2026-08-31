from __future__ import annotations

import sys
from pathlib import Path

from scripts import run_pytest_shard


def test_manifest_is_written_only_after_selected_tests_finish(
    tmp_path: Path, monkeypatch
) -> None:
    manifest = tmp_path / "artifacts" / "pytest-shard-0.json"
    observed: dict[str, object] = {}

    monkeypatch.setattr(
        run_pytest_shard,
        "collect_nodes",
        lambda: ("tests/test_example.py::test_one", "tests/test_example.py::test_two"),
    )

    def fake_run(arguments, *, check):
        observed["arguments"] = arguments
        observed["manifest_present_during_tests"] = manifest.exists()

        class Result:
            returncode = 0

        return Result()

    monkeypatch.setattr(run_pytest_shard.subprocess, "run", fake_run)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_pytest_shard.py",
            "--shard-index",
            "0",
            "--shard-count",
            "2",
            "--manifest",
            str(manifest),
        ],
    )

    assert run_pytest_shard.main() == 0
    assert observed["manifest_present_during_tests"] is False
    assert observed["arguments"][-1] == "tests/test_example.py::test_one"
    assert manifest.is_file()
