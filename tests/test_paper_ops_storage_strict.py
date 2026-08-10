from __future__ import annotations

from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.storage import append_jsonl_unique, read_jsonl


def _tree_snapshot(root: Path) -> tuple[tuple[str, ...], dict[str, bytes]]:
    return (
        tuple(
            sorted(path.relative_to(root).as_posix() for path in root.rglob("*") if path.is_dir())
        ),
        {
            path.relative_to(root).as_posix(): path.read_bytes()
            for path in root.rglob("*")
            if path.is_file()
        },
    )


@pytest.mark.parametrize("record", ("[]", '"scalar"', "1", "null"))
def test_jsonl_reader_rejects_every_non_object_record(
    tmp_path: Path,
    record: str,
) -> None:
    path = tmp_path / "canonical.jsonl"
    path.write_text('{"event_id":"valid"}\n' + record + "\n", encoding="utf-8")

    with pytest.raises(ValueError, match=r"record 2.*must be a JSON object"):
        read_jsonl(path)


def test_jsonl_reader_allows_blank_lines_between_object_records(tmp_path: Path) -> None:
    path = tmp_path / "canonical.jsonl"
    path.write_text(
        '\n  \n{"event_id":"first"}\n\r\n{"event_id":"second"}\n',
        encoding="utf-8",
    )

    assert [row["event_id"] for row in read_jsonl(path)] == ["first", "second"]


@pytest.mark.parametrize("event_id", (None, "", "   ", 17))
def test_jsonl_append_rejects_non_string_or_blank_ids_before_writing(
    tmp_path: Path,
    event_id: object,
) -> None:
    path = tmp_path / "canonical.jsonl"

    with pytest.raises(ValueError, match="must be a nonblank string"):
        append_jsonl_unique(path, [{"event_id": event_id}], "event_id")

    assert read_jsonl(path) == []


def test_jsonl_append_allows_only_canonical_equal_idempotent_repeats(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical.jsonl"
    original = {"event_id": "stable", "payload": {"b": 2, "a": 1}}
    assert append_jsonl_unique(path, [original], "event_id") == 1

    reordered = {"payload": {"a": 1, "b": 2}, "event_id": "stable"}
    assert append_jsonl_unique(path, [reordered, reordered], "event_id") == 0

    with pytest.raises(ValueError, match="conflicting row reuses existing event_id"):
        append_jsonl_unique(
            path,
            [{"event_id": "stable", "payload": {"a": 999, "b": 2}}],
            "event_id",
        )

    assert read_jsonl(path) == [original]


def test_jsonl_append_rejects_conflicting_in_batch_ids_before_any_append(
    tmp_path: Path,
) -> None:
    path = tmp_path / "canonical.jsonl"

    with pytest.raises(ValueError, match="conflicting rows reuse event_id"):
        append_jsonl_unique(
            path,
            [
                {"event_id": "collision", "payload": {"version": 1}},
                {"event_id": "collision", "payload": {"version": 2}},
            ],
            "event_id",
        )

    assert read_jsonl(path) == []


def test_transaction_recovery_collision_does_not_apply_state_update(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    ledger_path = paths.ledger / "paper_ledger.jsonl"
    existing = {
        "event_id": "collision",
        "event_type": "paper_order_created",
        "payload": {"order_id": "original"},
    }
    paper_ops_engine.write_jsonl(ledger_path, [existing])
    conflicting = {
        "event_id": "collision",
        "event_type": "paper_order_created",
        "payload": {"order_id": "conflicting"},
    }
    state_updates = {"state/replay_pending_orders.json": []}
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(
        journal_path,
        {
            "events": [conflicting],
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": state_updates,
            "transaction_id": paper_ops_engine._paper_transaction_id(
                [conflicting],
                state_updates,
            ),
        },
    )

    with pytest.raises(ValueError, match="conflicting row reuses existing event_id"):
        paper_ops_engine._recover_pending_transaction(paths)

    assert read_jsonl(ledger_path) == [existing]
    assert not (paths.state / "replay_pending_orders.json").exists()
    assert journal_path.exists()


@pytest.mark.parametrize(
    "target",
    (
        "state/shadow/../forward_pending_orders.json",
        "state/shadow/./forward_pending_orders.json",
        "state\\forward_pending_orders.json",
        "state/forward_pending_orders.json:stream",
        "C:state/forward_pending_orders.json",
        "state/pending_orders.json ",
        "reports/strategy_evidence.json",
    ),
)
def test_transaction_rejects_noncanonical_targets_before_journal_or_ledger_write(
    tmp_path: Path,
    target: str,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    (paths.state / ".paper_transaction.lock").unlink(missing_ok=True)
    before = _tree_snapshot(root)

    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": {target: []},
    }
    journal["transaction_id"] = paper_ops_engine._paper_transaction_id([], journal["state_updates"])
    with pytest.raises(ValueError, match="allowlisted"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before
    assert not (paths.state / ".paper_transaction.lock").exists()
    assert not (paths.state / "paper_transaction_pending.json").exists()


def test_forbidden_producer_target_creates_no_transaction_artifacts(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    (paths.state / ".paper_transaction.lock").unlink(missing_ok=True)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="allowlisted"):
        paper_ops_engine._commit_paper_transaction(
            paths,
            events=[],
            state_updates={paths.reports / "strategy_evidence.json": {}},
        )

    assert _tree_snapshot(root) == before


@pytest.mark.parametrize(
    "target",
    (
        "state/pending_orders.json",
        "state/replay_open_positions.json",
        "state/shadow/challenger-a/forward_account.json",
        "exports/shadow_picks_demo_2026-02-28_challenger-a.json",
        "manifests/shadow_replay_2026-02-28_challenger-a.json",
    ),
)
def test_transaction_target_allowlist_accepts_only_canonical_producer_families(target: str) -> None:
    assert paper_ops_engine._is_allowed_transaction_target(target)


@pytest.mark.parametrize(
    "target",
    (
        "state/shadow/../forward_pending_orders.json",
        "state/shadow/./forward_pending_orders.json",
        "state/shadow/../forward_pending_orders.json",
        "state/shadow/../../state/pending_orders.json",
        "exports/shadow_picks_demo_2026-02-30_challenger-a.json",
        "state/shadow/../forward_account.json",
    ),
)
def test_transaction_target_allowlist_rejects_traversal_and_invalid_dates(target: str) -> None:
    assert not paper_ops_engine._is_allowed_transaction_target(target)
