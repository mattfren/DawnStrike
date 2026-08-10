import pytest

from intraday_scanner.v2.paper_ops import __main__ as paper_ops_cli
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.calendar_truth import verify_calendar_truth
from intraday_scanner.v2.paper_ops.calendar_view import write_calendar_view
from intraday_scanner.v2.paper_ops.engine import calendar, init, reconcile, report
from intraday_scanner.v2.paper_ops.ledger_rebuild import rebuild_ledger
from intraday_scanner.v2.paper_ops.observer_safety import PaperOpsObserverBlocked
from intraday_scanner.v2.paper_ops.readiness import forward_readiness
from intraday_scanner.v2.paper_ops.source_bar_truth import verify_source_bar_truth
from intraday_scanner.v2.paper_ops.storage import write_json
from intraday_scanner.v2.paper_ops.strategy_evidence import score_strategy_evidence
from intraday_scanner.v2.paper_ops.trade_blotter import build_trade_blotter, verify_trade_blotter


def _tree_snapshot(root):
    directories = tuple(
        sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())
    )
    files = {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }
    return directories, files


def _seed_pending_journal(root, journal_kind):
    journal = root / "state" / "paper_transaction_pending.json"
    if journal_kind == "malformed":
        journal.write_bytes(b'{"schema_version":')
        return journal
    if journal_kind == "partial":
        write_json(
            journal,
            {
                "events": [],
                "schema_version": "v2.paper_transaction.v1",
            },
        )
        return journal
    event = {
        "event_id": "observer-recovery-probe",
        "event_type": "paper_no_setup_decision",
        "mode": "forward",
        "payload": {"mode": "forward"},
        "run_id": "observer-recovery-run",
        "strategy_id": "observer-recovery-strategy",
        "symbol": "TST",
        "trade_date": "2026-01-02",
    }
    state_updates = {"state/observer_recovery_probe.json": {"recovered": True}}
    write_json(
        journal,
        {
            "events": [event],
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": state_updates,
            "transaction_id": paper_ops_engine._paper_transaction_id(
                [event],
                state_updates,
            ),
        },
    )
    return journal


def _seed_calendar_variant(root, calendar_kind):
    path = root / "calendar" / "strategy_daily_returns.csv"
    if calendar_kind == "zero_byte":
        path.write_bytes(b"")
        return
    if calendar_kind == "header_only":
        path.write_text(
            ",".join(paper_ops_engine.CALENDAR_FIELDNAMES) + "\n",
            encoding="utf-8",
        )
        return
    if calendar_kind == "wrong_header":
        path.write_text(
            "date,mode,strategy_id\n2026-01-02,forward,fixture\n",
            encoding="utf-8",
        )
        return
    row = {field: 0 for field in paper_ops_engine.CALENDAR_FIELDNAMES}
    row.update(
        {
            "date": "2026-01-02",
            "mode": "forward",
            "strategy_id": "fixture-strategy",
            "strategy_version": "fixture-v1",
            "strategy_status": "candidate",
            "execution_policy_version": "fixture-policy-v1",
            "strategy_semantics_fingerprint": "unknown",
            "data_snapshot_id": "fixture-snapshot",
            "daily_return_pct": "not-a-number",
            "warnings": "",
            "run_id": "fixture-run",
        }
    )
    paper_ops_engine.write_csv(
        path,
        [row],
        paper_ops_engine.CALENDAR_FIELDNAMES,
    )


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


@pytest.mark.parametrize(
    ("observer", "command"),
    (
        (calendar, "calendar"),
        (report, "report"),
        (write_calendar_view, "calendar-view"),
    ),
)
@pytest.mark.parametrize(
    "calendar_kind",
    ("zero_byte", "header_only", "wrong_header", "malformed_numeric"),
)
def test_calendar_observers_reject_invalid_evidence_without_writes(
    tmp_path,
    observer,
    command,
    calendar_kind,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_calendar_variant(root, calendar_kind)
    before_direct = _tree_snapshot(root)

    with pytest.raises(PaperOpsObserverBlocked):
        observer(output_root=root)

    assert _tree_snapshot(root) == before_direct
    before_cli = _tree_snapshot(root)
    assert paper_ops_cli.main([command, "--output-root", str(root)]) == 2
    assert _tree_snapshot(root) == before_cli


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
@pytest.mark.parametrize("journal_kind", ("valid", "malformed", "partial"))
def test_paper_ops_observers_retain_pending_journal_without_lock(
    tmp_path,
    command,
    journal_kind,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    lock = root / "state" / ".paper_transaction.lock"
    if lock.exists():
        lock.unlink()
    journal = _seed_pending_journal(root, journal_kind)
    before = _tree_snapshot(root)

    status = paper_ops_cli.main([command, "--output-root", str(root)])

    after = _tree_snapshot(root)
    assert status == 2
    assert after == before
    assert journal.exists()
    assert not (root / "state" / ".paper_transaction.lock").exists()


@pytest.mark.parametrize(
    "observer",
    (
        calendar,
        reconcile,
        report,
        rebuild_ledger,
        verify_calendar_truth,
        score_strategy_evidence,
        forward_readiness,
        write_calendar_view,
        build_trade_blotter,
        verify_trade_blotter,
        verify_source_bar_truth,
    ),
)
@pytest.mark.parametrize("journal_kind", ("valid", "malformed", "partial"))
def test_direct_observers_block_before_pending_journal_recovery(
    tmp_path,
    observer,
    journal_kind,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    lock = root / "state" / ".paper_transaction.lock"
    if lock.exists():
        lock.unlink()
    journal = _seed_pending_journal(root, journal_kind)
    before = _tree_snapshot(root)

    with pytest.raises(PaperOpsObserverBlocked, match="BLOCKED_PENDING_RECOVERY"):
        observer(output_root=root)

    after = _tree_snapshot(root)
    assert after == before
    assert journal.exists()
    assert not lock.exists()


def test_rebuild_writer_cli_recovers_valid_pending_journal(monkeypatch, tmp_path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    journal = _seed_pending_journal(root, "valid")
    monkeypatch.setattr(paper_ops_cli, "_result_exit_code", lambda *_args: 0)

    status = paper_ops_cli.main(
        ["rebuild-ledger", "--write-rebuilt", "--output-root", str(root)]
    )

    assert status == 0
    assert not journal.exists()
    assert paper_ops_engine.read_json(
        root / "state" / "observer_recovery_probe.json",
        {},
    ) == {"recovered": True}
    assert any(
        row.get("event_id") == "observer-recovery-probe"
        for row in paper_ops_engine.read_jsonl(root / "ledger" / "paper_ledger.jsonl")
    )


def test_paper_ops_cli_returns_nonzero_for_failed_reconciliation(
    monkeypatch,
    tmp_path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    (root / "ledger" / "paper_ledger.jsonl").write_text(
        '{"event_id":"fixture","event_type":"paper_no_setup_decision",'
        '"mode":"forward","payload":{},"run_id":"fixture",'
        '"strategy_id":"fixture","symbol":"TST","trade_date":"2026-01-02"}\n',
        encoding="utf-8",
    )
    called = False

    def failed_reconciliation(**_kwargs):
        nonlocal called
        called = True
        return {"status": "failed", "duplicate_event_ids": ["dup"]}

    monkeypatch.setattr(
        paper_ops_cli,
        "reconcile",
        failed_reconciliation,
    )

    status = paper_ops_cli.main(["reconcile", "--output-root", str(root)])

    assert status == 2
    assert called


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
