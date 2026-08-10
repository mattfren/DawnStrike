import pytest

from intraday_scanner.v2.paper_ops import __main__ as paper_ops_cli


@pytest.mark.parametrize(
    "command",
    (
        "calendar",
        "reconcile",
        "report",
        "rebuild-ledger",
        "verify-calendar",
        "evidence",
        "readiness",
        "calendar-view",
        "blotter",
        "verify-blotter",
        "verify-source-bars",
    ),
)
def test_paper_ops_observers_fail_closed_without_creating_a_missing_tree(tmp_path, command) -> None:
    root = tmp_path / "absent" / "paper_ops"
    assert paper_ops_cli.main([command, "--output-root", str(root)]) == 2
    assert not root.exists()
    assert not root.parent.exists()


def test_paper_ops_cli_returns_nonzero_for_failed_reconciliation(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        paper_ops_cli,
        "reconcile",
        lambda **_kwargs: {"status": "failed", "duplicate_event_ids": ["dup"]},
    )

    status = paper_ops_cli.main(["reconcile", "--output-root", str(tmp_path / "paper_ops")])

    assert status == 2


def test_paper_ops_cli_returns_nonzero_when_run_day_reconciliation_fails(
    monkeypatch,
    tmp_path,
) -> None:
    monkeypatch.setattr(
        paper_ops_cli,
        "run_day",
        lambda **_kwargs: {
            "run_id": "run",
            "reconcile": {"status": "failed", "orphan_fills": ["fill"]},
        },
    )

    status = paper_ops_cli.main(
        [
            "run-day",
            "--date",
            "2026-07-15",
            "--output-root",
            str(tmp_path / "paper_ops"),
        ]
    )

    assert status == 2


def test_paper_ops_cli_exposes_shadow_registration_run_and_evaluation(
    monkeypatch,
    tmp_path,
) -> None:
    calls: list[tuple[str, object]] = []
    manifest = tmp_path / "candidate.json"
    manifest.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        paper_ops_cli,
        "register_shadow_challenger",
        lambda **kwargs: (
            calls.append(("register", kwargs["manifest_path"])) or {"status": "registered"}
        ),
    )
    monkeypatch.setattr(
        paper_ops_cli,
        "run_shadow_day",
        lambda **kwargs: calls.append(("run", kwargs["run_date"])) or {"status": "passed"},
    )
    monkeypatch.setattr(
        paper_ops_cli,
        "evaluate_paperops_challengers",
        lambda **kwargs: calls.append(("evaluate", kwargs["output_root"])) or {"status": "passed"},
    )
    root = tmp_path / "paper"

    assert (
        paper_ops_cli.main(
            [
                "shadow-register",
                "--manifest",
                str(manifest),
                "--output-root",
                str(root),
            ]
        )
        == 0
    )
    assert (
        paper_ops_cli.main(["shadow-run", "--date", "2026-07-15", "--output-root", str(root)]) == 0
    )
    assert paper_ops_cli.main(["challenger-evaluate", "--output-root", str(root)]) == 0
    assert calls == [
        ("register", manifest),
        ("run", paper_ops_cli.date(2026, 7, 15)),
        ("evaluate", root),
    ]


@pytest.mark.parametrize(
    ("result_status", "expected_exit"),
    (
        ("skipped_no_eligible_challengers", 0),
        ("passed_with_ineligible_challengers", 0),
        ("failed", 2),
    ),
)
def test_shadow_run_cli_distinguishes_expected_eligibility_skips_from_failure(
    monkeypatch,
    tmp_path,
    result_status: str,
    expected_exit: int,
) -> None:
    monkeypatch.setattr(
        paper_ops_cli,
        "run_shadow_day",
        lambda **_kwargs: {"status": result_status},
    )

    status = paper_ops_cli.main(
        [
            "shadow-run",
            "--date",
            "2026-07-15",
            "--output-root",
            str(tmp_path / "paper_ops"),
        ]
    )

    assert status == expected_exit


def test_shadow_run_cli_propagates_frozen_integrity_failure(
    monkeypatch,
    tmp_path,
) -> None:
    def fail_closed(**_kwargs) -> dict[str, object]:
        raise ValueError("invalid frozen challenger fixture: shadow implementation source changed")

    monkeypatch.setattr(paper_ops_cli, "run_shadow_day", fail_closed)

    with pytest.raises(ValueError, match="invalid frozen challenger"):
        paper_ops_cli.main(
            [
                "shadow-run",
                "--date",
                "2026-07-16",
                "--output-root",
                str(tmp_path / "paper_ops"),
            ]
        )
