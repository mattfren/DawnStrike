from __future__ import annotations

from scripts import run_detect_secrets_tracked


def test_helper_passes_all_tracked_files_to_hook_in_process(monkeypatch) -> None:
    files = tuple(f"tracked/path-{index}.txt" for index in range(2_000))
    observed: list[list[str]] = []
    monkeypatch.setattr(
        run_detect_secrets_tracked,
        "tracked_files",
        lambda *, include_untracked: files if include_untracked else (),
    )
    monkeypatch.setattr(
        run_detect_secrets_tracked,
        "pre_commit_hook_main",
        lambda argv: observed.append(argv) or 0,
    )

    assert (
        run_detect_secrets_tracked.main(
            ["--baseline", ".secrets.baseline", "--include-untracked"]
        )
        == 0
    )
    assert observed == [["--baseline", ".secrets.baseline", *files]]
