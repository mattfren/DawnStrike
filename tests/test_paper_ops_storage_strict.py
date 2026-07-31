from __future__ import annotations

from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.storage import append_jsonl_unique, read_jsonl


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
    state_updates = {"state/collision_probe.json": {"applied": True}}
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
    assert not (paths.state / "collision_probe.json").exists()
    assert journal_path.exists()
