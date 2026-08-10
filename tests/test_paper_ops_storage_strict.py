from __future__ import annotations

import copy
import hashlib
import json
from datetime import date, datetime, timedelta, timezone
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


def _canonical_event(
    seed: str = "canonical-event", *, symbol: str = "TST"
) -> dict[str, object]:
    run_id = "paper_ops:forward:2026-01-02:fixture-snapshot"
    signal_second = 0 if seed == "canonical-event" else sum(seed.encode("utf-8")) % 60
    signal_time = f"2026-01-02T20:00:{signal_second:02d}+00:00"
    config = paper_ops_engine.PaperOpsConfig()
    catalog = tuple(paper_ops_engine.build_strategy_catalog())
    strategy_config = paper_ops_engine._strategy_configs(config, catalog)[-1]
    strategy = next(item for item in catalog if item.strategy_id == strategy_config.strategy_id)
    strategy_id = strategy.strategy_id
    strategy_version = strategy.version
    policy = config.execution_policy_version
    semantics = paper_ops_engine._strategy_semantics_fingerprint(strategy)
    pick_id = paper_ops_engine.stable_id(
        "forward",
        "2026-01-02",
        strategy_id,
        strategy_version,
        policy,
        symbol,
        signal_time,
        "long",
    )
    run = paper_ops_engine.PaperRun(
        run_id=run_id,
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        run_date="2026-01-02",
        data_snapshot_id="fixture-snapshot",
        created_at=signal_time,
    )
    pick = paper_ops_engine.PaperPick(
        pick_id=pick_id,
        run_id=run_id,
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        trade_date="2026-01-02",
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        strategy_status="shadow",
        symbol=symbol,
        signal_time=signal_time,
        direction="long",
        setup_score=1.0,
        entry_reference=100.0,
        stop=95.0,
        target=110.0,
        risk_per_unit=5.0,
        reward_per_unit=10.0,
        reward_risk=2.0,
        decision=paper_ops_engine.PaperPickDecision.ACCEPTED,
        reason="canonical fixture",
        evidence=("canonical fixture evidence",),
        execution_policy_version=policy,
        strategy_semantics_fingerprint=semantics,
    )
    order = paper_ops_engine._order_from_pick(
        pick,
        run,
        config,
        equity_basis=config.starting_equity,
    )
    order_id = order.order_id
    event_id = paper_ops_engine.stable_id(
        "paper_ops_event",
        "forward",
        "2026-01-02",
        "enter",
        "paper_order_created",
        order_id,
    )
    return {
        "event_id": event_id,
        "event_type": "paper_order_created",
        "mode": "forward",
        "payload": order.to_dict(),
        "run_id": run_id,
        "schema_version": "v2.paper_ledger_event.v1",
        "strategy_id": strategy_id,
        "symbol": symbol,
        "trade_date": "2026-01-02",
    }


def _canonical_pick(event: dict[str, object]) -> dict[str, object]:
    order = event["payload"]
    assert isinstance(order, dict)
    return {
        "pick_id": order["pick_id"],
        "run_id": order["run_id"],
        "mode": order["mode"],
        "trade_date": order["trade_date"],
        "strategy_id": order["strategy_id"],
        "strategy_version": order["strategy_version"],
        "strategy_status": "shadow",
        "symbol": order["symbol"],
        "signal_time": order["signal_time"],
        "direction": order["direction"],
        "setup_score": 1.0,
        "entry_reference": order["entry"],
        "stop": order["stop"],
        "target": order["target"],
        "risk_per_unit": abs(float(order["entry"]) - float(order["stop"])),
        "reward_per_unit": order["reward_per_unit"],
        "reward_risk": order["reward_risk"],
        "decision": "accepted",
        "reason": "canonical fixture",
        "evidence": ["canonical fixture evidence"],
        "warnings": order["warnings"],
        "execution_policy_version": order["execution_policy_version"],
        "strategy_semantics_fingerprint": order["strategy_semantics_fingerprint"],
        "schema_version": "v2.paper_pick.v2",
    }


def _seed_run_manifest(root: Path, event: dict[str, object]) -> None:
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    payload = event["payload"]
    assert isinstance(payload, dict)
    run_id = str(event["run_id"])
    manifest = paper_ops_engine.PaperOpsManifest(
        run_id=run_id,
        mode=paper_ops_engine.PaperRunMode(str(event["mode"])),
        run_date=str(event["trade_date"]),
        data_snapshot_id="fixture-snapshot",
        output_artifacts=(),
        warnings=(),
        execution_policy_version=str(payload["execution_policy_version"]),
        execution_policy_fingerprint="b" * 64,
        universe_id="fixture-universe",
        universe_symbols=(str(event["symbol"]),),
    ).to_dict()
    unhashed = dict(manifest)
    unhashed.pop("manifest_payload_hash", None)
    manifest["manifest_payload_hash"] = hashlib.sha256(
        json.dumps(unhashed, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    paper_ops_engine.write_json(
        paths.manifests / f"{paper_ops_engine._safe_filename(run_id)}.json", manifest
    )


def _seed_canonical_event_evidence(root: Path, event: dict[str, object]) -> None:
    _seed_run_manifest(root, event)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    run_id = str(event["run_id"])
    pick = _canonical_pick(event)
    paper_ops_engine.write_json(
        paths.exports / f"picks_{event['mode']}_{event['trade_date']}.json", [pick]
    )
    scan = {
        "event_id": paper_ops_engine.stable_id(
            "paper_ops_event",
            event["mode"],
            event["trade_date"],
            "scan",
            "paper_pick_decision",
            pick["pick_id"],
        ),
        "event_type": "paper_pick_decision",
        "run_id": run_id,
        "mode": event["mode"],
        "trade_date": event["trade_date"],
        "strategy_id": event["strategy_id"],
        "symbol": event["symbol"],
        "payload": pick,
        "schema_version": "v2.paper_ledger_event.v1",
    }
    paper_ops_engine._validate_transaction_event_rows([scan])
    paper_ops_engine._validate_run_and_origin_evidence(paths, [scan], {})
    paper_ops_engine.append_jsonl_unique(
        paths.ledger / "paper_ledger.jsonl", [scan], "event_id"
    )


def test_canonical_seeded_order_transaction_applies_exact_ledger_and_state(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    event = _canonical_event("positive")
    _seed_canonical_event_evidence(root, event)
    updates = {"state/pending_orders.json": [event["payload"]]}
    journal = {
        "events": [event],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([event], updates),
    }

    paper_ops_engine._apply_transaction_journal(paths, journal)

    ledger = read_jsonl(paths.ledger / "paper_ledger.jsonl")
    assert ledger[-1] == event
    assert paper_ops_engine.read_json(paths.state / "pending_orders.json", None) == [
        event["payload"]
    ]


@pytest.mark.parametrize(
    ("evidence", "message"),
    (
        ("missing-manifest", "transaction run manifest is missing"),
        ("missing-origin", "created order lacks its accepted scan decision"),
    ),
)
def test_created_order_requires_manifest_and_accepted_origin_evidence(
    tmp_path: Path,
    evidence: str,
    message: str,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    event = _canonical_event("missing-evidence")
    if evidence == "missing-origin":
        _seed_run_manifest(root, event)
    updates = {"state/pending_orders.json": [event["payload"]]}
    journal = {
        "events": [event],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([event], updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match=message):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before
    assert journal_path.exists()


def test_append_events_rejects_malformed_event_without_ledger_or_tree_change(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    order_event = _canonical_event("append-malformed")
    _seed_run_manifest(root, order_event)
    pick = _canonical_pick(order_event)
    pick.pop("execution_policy_version")
    event = paper_ops_engine.PaperLedgerEvent(
        event_id=paper_ops_engine.stable_id(
            "paper_ops_event",
            "forward",
            "2026-01-02",
            "scan",
            "paper_pick_decision",
            pick["pick_id"],
        ),
        event_type="paper_pick_decision",
        run_id=str(order_event["run_id"]),
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        trade_date="2026-01-02",
        strategy_id=str(order_event["strategy_id"]),
        symbol="TST",
        payload=pick,
    )
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="event policy is malformed"):
        paper_ops_engine._append_events(paths, [event])

    assert _tree_snapshot(root) == before


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
    existing = _canonical_event("collision")
    assert isinstance(existing["payload"], dict)
    _seed_canonical_event_evidence(root, existing)
    existing["payload"]["entry"] = 101.0
    append_jsonl_unique(ledger_path, [existing], "event_id")
    ledger_before_recovery = read_jsonl(ledger_path)
    conflicting = _canonical_event("collision")
    assert isinstance(conflicting["payload"], dict)
    state_updates = {"state/pending_orders.json": [conflicting["payload"]]}
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

    with pytest.raises(ValueError, match="ledger conflict"):
        paper_ops_engine._recover_pending_transaction(paths)

    assert read_jsonl(ledger_path) == ledger_before_recovery
    assert not (paths.state / "replay_pending_orders.json").exists()
    assert journal_path.exists()


@pytest.mark.parametrize(
    ("mutation", "message"),
    (
        (lambda row: row.pop("schema_version"), "events are malformed"),
        (lambda row: row.update(schema_version="unknown"), "events are malformed"),
        (lambda row: row.update(event_id=""), "events are malformed"),
        (lambda row: row.update(event_type=None), "events are malformed"),
        (lambda row: row.update(event_type="unknown"), "event type is unsupported"),
        (lambda row: row.update(run_id=""), "events are malformed"),
        (lambda row: row.update(strategy_id=""), "events are malformed"),
        (lambda row: row.update(symbol=17), "events are malformed"),
        (lambda row: row.update(mode="live"), "events are malformed"),
        (lambda row: row.update(trade_date="2026-02-30"), "events are malformed"),
        (lambda row: row.update(payload=[]), "events are malformed"),
        (lambda row: row["payload"].pop("order_id"), "event entity is malformed"),
        (lambda row: row["payload"].pop("schema_version"), "event schema is malformed"),
        (
            lambda row: row["payload"].update(schema_version="attacker-v1"),
            "event schema is malformed",
        ),
        (
            lambda row: row["payload"].pop("execution_policy_version"),
            "event policy is malformed",
        ),
        (
            lambda row: row["payload"].update(execution_policy_version=""),
            "event policy is malformed",
        ),
        (
            lambda row: row["payload"].update(mode="replay"),
            "event identity conflicts",
        ),
        (
            lambda row: row["payload"].update(run_id="other"),
            "event identity conflicts",
        ),
        (
            lambda row: row["payload"].update(strategy_id="other"),
            "event identity conflicts",
        ),
        (
            lambda row: row["payload"].update(symbol="OTHER"),
            "event identity conflicts",
        ),
        (
            lambda row: row["payload"].update(lifecycle_run_id="other"),
            "event identity conflicts",
        ),
        (
            lambda row: row.update(
                payload={
                    "execution_policy_version": "fixture-policy",
                    "order_id": "order:attacker",
                    "schema_version": "v2.paper_order.v2",
                }
            ),
            "payload fields are malformed",
        ),
        (
            lambda row: (
                row.update(run_id="attacker-run"),
                row["payload"].update(run_id="attacker-run"),
            ),
            "payload run identity is malformed",
        ),
        (
            lambda row: row.update(event_id="noncanonical-event-id"),
            "event identity is noncanonical",
        ),
        (
            lambda row: row["payload"].update(order_id="order:attacker"),
            "order identity is noncanonical",
        ),
        (
            lambda row: row["payload"].update(
                pick_id="pick:attacker",
                order_id="order:pick:attacker",
            ),
            "order pick identity is noncanonical",
        ),
        (
            lambda row: row["payload"].update(quantity="10"),
            "order quantity or warnings are invalid",
        ),
        (
            lambda row: row["payload"].update(entry=float("inf")),
            "order numeric field is invalid",
        ),
        (
            lambda row: row["payload"].update(direction="flat"),
            "order enum is invalid",
        ),
        (
            lambda row: row["payload"].update(earliest_fill_date="2026-02-30"),
            "order date is invalid",
        ),
        (
            lambda row: row["payload"].update(signal_time="2026-01-02T20:00:00Z"),
            "order date is invalid",
        ),
        (
            lambda row: row["payload"].update(signal_time="2026-01-02T20:00:00+0000"),
            "order date is invalid",
        ),
    ),
    ids=(
        "missing-schema",
        "invalid-schema",
        "missing-event-id",
        "invalid-event-type",
        "unknown-event-type",
        "missing-run-id",
        "missing-strategy-id",
        "invalid-symbol",
        "invalid-mode",
        "invalid-date",
        "nonobject-payload",
        "missing-entity-id",
        "missing-payload-schema",
        "invalid-payload-schema",
        "missing-policy",
        "empty-policy",
        "contradictory-mode",
        "contradictory-run",
        "contradictory-strategy",
        "contradictory-symbol",
        "contradictory-lifecycle-run",
        "schema-tag-only-payload",
        "noncanonical-run-id",
        "noncanonical-event-id",
        "noncanonical-entity-id",
        "noncanonical-pick-id",
        "coercion-only-quantity",
        "nonfinite-entry",
        "invalid-direction",
        "invalid-order-date",
        "noncanonical-z-datetime",
        "noncanonical-offset-datetime",
    ),
)
def test_checksum_valid_journal_rejects_malformed_event_before_any_write(
    tmp_path: Path,
    mutation: object,
    message: str,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    (paths.state / ".paper_transaction.lock").unlink(missing_ok=True)
    event = _canonical_event()
    assert callable(mutation)
    mutation(event)
    updates = {"state/pending_orders.json": [event["payload"]]}
    journal = {
        "events": [event],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
    }
    journal["transaction_id"] = paper_ops_engine._paper_transaction_id([event], updates)
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match=message):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before
    assert journal_path.exists()


def test_checksum_valid_journal_rejects_duplicate_event_ids_before_any_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    (paths.state / ".paper_transaction.lock").unlink(missing_ok=True)
    events = [_canonical_event("duplicate"), _canonical_event("duplicate")]
    updates = {"state/pending_orders.json": [events[0]["payload"]]}
    journal = {
        "events": events,
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
    }
    journal["transaction_id"] = paper_ops_engine._paper_transaction_id(events, updates)
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="duplicate event IDs"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before
    assert journal_path.exists()


def test_transaction_validation_accepts_origin_identity_with_current_lifecycle_binding() -> None:
    event = _canonical_event()
    assert isinstance(event["payload"], dict)
    payload = event["payload"]
    event["event_type"] = "paper_order_pending_no_fill_data"
    payload.update(
        lifecycle_run_id=event["run_id"],
        origin_run_id="paper_ops:forward:2026-01-01:origin-snapshot",
        run_id="paper_ops:forward:2026-01-01:origin-snapshot",
        trade_date="2026-01-01",
        signal_time="2026-01-01T20:00:00+00:00",
        earliest_fill_date="2026-01-02",
    )
    payload["pick_id"] = paper_ops_engine.stable_id(
        "forward",
        "2026-01-01",
        payload["strategy_id"],
        payload["strategy_version"],
        payload["execution_policy_version"],
        payload["symbol"],
        payload["signal_time"],
        payload["direction"],
    )
    payload["order_id"] = paper_ops_engine.stable_id("order", payload["pick_id"])
    event["event_id"] = paper_ops_engine.stable_id(
        "paper_ops_event",
        "forward",
        "2026-01-02",
        "check",
        "paper_order_pending_no_fill_data",
        f"{payload['order_id']}:pending_check:2026-01-02",
    )

    paper_ops_engine._validate_transaction_event_rows([event])


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


@pytest.mark.parametrize(
    ("target", "payload"),
    (
        ("state/pending_orders.json", {"attacker": "not-an-array"}),
        ("state/open_positions.json", {"attacker": "not-an-array"}),
        ("state/paper_accounts.json", []),
        ("state/shadow/challenger-a/forward_account.json", []),
        ("exports/shadow_picks_forward_2026-01-02_challenger-a.json", {}),
        (
            "exports/shadow_strategy_decisions_forward_2026-01-02_challenger-a.json",
            {},
        ),
        (
            "exports/shadow_order_decisions_forward_2026-01-02_challenger-a.json",
            {},
        ),
        ("manifests/shadow_forward_2026-01-02_challenger-a.json", []),
    ),
)
def test_checksum_valid_journal_rejects_wrong_target_container_before_any_write(
    tmp_path: Path,
    target: str,
    payload: object,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    updates = {target: payload}
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before
    assert journal_path.exists()


@pytest.mark.parametrize(
    "mutation",
    (
        lambda rows: rows.append(copy.deepcopy(rows[0])),
        lambda rows: rows[0].update(quantity="10"),
        lambda rows: rows[0].update(entry=float("nan")),
        lambda rows: rows[0].update(schema_version="attacker-v1"),
        lambda rows: rows[0].update(max_loss_estimate=-1_000_000.0),
    ),
    ids=(
        "duplicate-id",
        "coercion-only",
        "nonfinite",
        "unsupported-schema",
        "negative-max-loss",
    ),
)
def test_checksum_valid_journal_rejects_invalid_order_state_contract(
    tmp_path: Path,
    mutation: object,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    event = _canonical_event()
    rows = [copy.deepcopy(event["payload"])]
    assert callable(mutation)
    mutation(rows)
    updates = {"state/pending_orders.json": rows}
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before
    assert journal_path.exists()


def test_checksum_valid_journal_rejects_invented_account_equity(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    payload = paper_ops_engine.read_json(paths.state / "paper_accounts.json", {})
    assert isinstance(payload, dict)
    accounts = payload["accounts"]
    assert isinstance(accounts, list) and accounts
    accounts[0]["current_equity"] = float(accounts[0]["current_equity"]) + 1.0
    updates = {"state/paper_accounts.json": payload}
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="account equity identity conflicts"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


@pytest.mark.parametrize(
    "variant", ("missing-created", "unrelated-injection", "unrelated-deletion")
)
def test_order_event_state_transition_conflicts_are_exact_no_ops(
    tmp_path: Path,
    variant: str,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    event = _canonical_event("created")
    _seed_canonical_event_evidence(root, event)
    created = copy.deepcopy(event["payload"])
    unrelated = copy.deepcopy(_canonical_event("unrelated", symbol="ALT")["payload"])
    if variant == "unrelated-deletion":
        paper_ops_engine.write_json(paths.state / "pending_orders.json", [unrelated])
    updates = {
        "state/pending_orders.json": (
            []
            if variant == "missing-created"
            else [created, unrelated]
            if variant == "unrelated-injection"
            else [created]
        )
    }
    journal = {
        "events": [event],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([event], updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="pending-order transition conflicts"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_event_free_transaction_cannot_erase_canonical_state(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    existing = copy.deepcopy(_canonical_event("existing")["payload"])
    paper_ops_engine.write_json(paths.state / "pending_orders.json", [existing])
    updates = {"state/pending_orders.json": []}
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="event-free transaction state update is not a no-op"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_event_free_producer_transaction_accepts_only_exact_state_no_op(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    pending = paper_ops_engine.read_json(paths.state / "pending_orders.json", None)
    before = _tree_snapshot(root)

    paper_ops_engine._commit_paper_transaction(
        paths,
        events=[],
        state_updates={paths.state / "pending_orders.json": pending},
    )

    assert _tree_snapshot(root) == before


@pytest.mark.parametrize("producer", (False, True), ids=("journal-apply", "producer"))
def test_transaction_rejects_existing_directory_target_before_any_write(
    tmp_path: Path,
    producer: bool,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    target = paths.state / "pending_orders.json"
    target.unlink()
    target.mkdir()
    updates = {"state/pending_orders.json": []}
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="not a regular file"):
        if producer:
            paper_ops_engine._commit_paper_transaction(
                paths,
                events=[],
                state_updates={target: []},
            )
        else:
            paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_recovery_retains_journal_and_tree_for_existing_directory_target(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    target = paths.state / "pending_orders.json"
    target.unlink()
    target.mkdir()
    updates = {"state/pending_orders.json": []}
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="not a regular file"):
        paper_ops_engine._recover_pending_transaction(paths)

    assert _tree_snapshot(root) == before
    assert journal_path.exists()


def test_transaction_preflights_every_target_before_earlier_state_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    invalid_target = paths.state / "replay_pending_orders.json"
    invalid_target.mkdir()
    updates = {
        "state/pending_orders.json": [],
        "state/replay_pending_orders.json": [],
    }
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="not a regular file"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_transaction_rejects_existing_nondirectory_parent_before_any_write(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    shadow_parent = paths.state / "shadow"
    shadow_parent.write_text("not-a-directory", encoding="utf-8")
    target = "state/shadow/challenger-a/forward_pending_orders.json"
    updates = {target: []}
    journal = {
        "events": [],
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id([], updates),
    }
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="parent is not a directory"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def _seed_pending_canonical_order(
    root: Path,
    *,
    symbol: str = "TST",
) -> tuple[paper_ops_engine.PaperOpsPaths, dict[str, object]]:
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    event = _canonical_event("lifecycle", symbol=symbol)
    _seed_canonical_event_evidence(root, event)
    updates = {"state/pending_orders.json": [event["payload"]]}
    paper_ops_engine._apply_transaction_journal(
        paths,
        {
            "events": [event],
            "schema_version": "v2.paper_transaction.v1",
            "state_updates": updates,
            "transaction_id": paper_ops_engine._paper_transaction_id([event], updates),
        },
    )
    return paths, event


def _canonical_fill_open_transaction(
    paths: paper_ops_engine.PaperOpsPaths,
    created_event: dict[str, object],
) -> tuple[
    list[paper_ops_engine.PaperLedgerEvent],
    dict[Path, object],
    paper_ops_engine.PaperPosition,
]:
    order_row = created_event["payload"]
    assert isinstance(order_row, dict)
    order = paper_ops_engine._order_from_row(order_row)
    run_date = date(2026, 1, 5)
    run = paper_ops_engine._paper_run(
        run_date=run_date,
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        data_snapshot_id="fixture-snapshot",
    )
    bar = paper_ops_engine.MarketBar(
        symbol=order.symbol,
        timestamp=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
        open=100.0,
        high=105.0,
        low=97.0,
        close=103.0,
        volume=1_000,
    )
    config = paper_ops_engine._config(paths)
    fill = paper_ops_engine._fill_order(order, bar, run, config)
    opened = paper_ops_engine._position_from_fill(order, fill)
    checked, close_record = paper_ops_engine._check_position(opened, bar, run, config)
    assert close_record is None
    events = [
        paper_ops_engine._event(
            run,
            paper_ops_engine.PaperJobPhase.CHECK,
            fill.strategy_id,
            fill.symbol,
            "paper_fill",
            fill.fill_id,
            paper_ops_engine._with_source_bar(fill.to_dict(), bar, run),
        ),
        paper_ops_engine._event(
            run,
            paper_ops_engine.PaperJobPhase.CHECK,
            opened.strategy_id,
            opened.symbol,
            "paper_position_opened",
            opened.position_id,
            paper_ops_engine._with_source_bar(opened.to_dict(), bar, run),
        ),
        paper_ops_engine._event(
            run,
            paper_ops_engine.PaperJobPhase.CHECK,
            checked.strategy_id,
            checked.symbol,
            "paper_position_checked_no_action",
            f"{checked.position_id}:checked:{run_date.isoformat()}",
            paper_ops_engine._with_source_bar(checked.to_dict(), bar, run),
        ),
    ]
    _seed_run_manifest(paths.root, events[0].to_dict())
    accounts = paper_ops_engine._accounts(paths)
    accounts = paper_ops_engine._apply_mark(accounts, checked)
    accounts = paper_ops_engine._recalculate_unrealized_accounts(
        accounts, [checked.to_dict()]
    )
    updates: dict[Path, object] = {
        paths.state / "pending_orders.json": [],
        paths.state / "open_positions.json": [checked.to_dict()],
        paths.state / "paper_accounts.json": paper_ops_engine._account_state_payload(
            paths,
            paper_ops_engine.PaperRunMode.FORWARD,
            accounts,
        ),
    }
    return events, updates, checked


def _seed_open_canonical_position(
    root: Path,
) -> tuple[paper_ops_engine.PaperOpsPaths, paper_ops_engine.PaperPosition]:
    paths, created_event = _seed_pending_canonical_order(root)
    events, updates, position = _canonical_fill_open_transaction(paths, created_event)
    paper_ops_engine._commit_paper_transaction(paths, events=events, state_updates=updates)
    return paths, position


def _canonical_close_transaction(
    paths: paper_ops_engine.PaperOpsPaths,
    position: paper_ops_engine.PaperPosition,
) -> tuple[
    list[paper_ops_engine.PaperLedgerEvent],
    dict[Path, object],
    paper_ops_engine.MarketBar,
]:
    run = paper_ops_engine._paper_run(
        run_date=date(2026, 1, 6),
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        data_snapshot_id="fixture-snapshot",
    )
    trigger_bar = paper_ops_engine.MarketBar(
        symbol=position.symbol,
        timestamp=datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc),
        open=103.0,
        high=111.0,
        low=98.0,
        close=108.0,
        volume=1_000,
    )
    config = paper_ops_engine._config(paths)
    _, close_record = paper_ops_engine._check_position(position, trigger_bar, run, config)
    assert close_record is not None
    event = paper_ops_engine._event(
        run,
        paper_ops_engine.PaperJobPhase.CLOSE,
        close_record.strategy_id,
        close_record.symbol,
        "paper_position_closed",
        close_record.close_id,
        paper_ops_engine._with_source_bar(close_record.to_dict(), trigger_bar, run),
    )
    _seed_run_manifest(paths.root, event.to_dict())
    accounts = paper_ops_engine._accounts(paths)
    accounts = paper_ops_engine._apply_close(accounts, close_record)
    accounts = paper_ops_engine._recalculate_unrealized_accounts(accounts, [])
    updates: dict[Path, object] = {
        paths.state / "open_positions.json": [],
        paths.state / "paper_accounts.json": paper_ops_engine._account_state_payload(
            paths,
            paper_ops_engine.PaperRunMode.FORWARD,
            accounts,
        ),
    }
    return [event], updates, trigger_bar


def _write_checksum_valid_journal(
    paths: paper_ops_engine.PaperOpsPaths,
    event_rows: list[dict[str, object]],
    updates: dict[str, object],
) -> dict[str, object]:
    journal = {
        "events": event_rows,
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id(event_rows, updates),
    }
    paper_ops_engine.write_json(paths.state / "paper_transaction_pending.json", journal)
    return journal


def _scan_event(event: dict[str, object]) -> dict[str, object]:
    pick = _canonical_pick(event)
    return {
        "event_id": paper_ops_engine.stable_id(
            "paper_ops_event",
            event["mode"],
            event["trade_date"],
            "scan",
            "paper_pick_decision",
            pick["pick_id"],
        ),
        "event_type": "paper_pick_decision",
        "run_id": event["run_id"],
        "mode": event["mode"],
        "trade_date": event["trade_date"],
        "strategy_id": event["strategy_id"],
        "symbol": event["symbol"],
        "payload": pick,
        "schema_version": "v2.paper_ledger_event.v1",
    }


def _append_scan_event(
    paths: paper_ops_engine.PaperOpsPaths,
    event: dict[str, object],
) -> dict[str, object]:
    scan = _scan_event(event)
    paper_ops_engine._validate_transaction_event_rows([scan])
    paper_ops_engine.append_jsonl_unique(
        paths.ledger / "paper_ledger.jsonl", [scan], "event_id"
    )
    return scan


def _enter_block_event(
    event: dict[str, object],
    reason: str,
) -> dict[str, object]:
    order_row = event["payload"]
    assert isinstance(order_row, dict)
    order = paper_ops_engine._order_from_row(order_row)
    run = paper_ops_engine.PaperRun(
        run_id=str(event["run_id"]),
        mode=paper_ops_engine.PaperRunMode(str(event["mode"])),
        run_date=str(event["trade_date"]),
        data_snapshot_id="fixture-snapshot",
        created_at="2026-01-02T21:00:00+00:00",
    )
    payload = paper_ops_engine._blocked_order_payload(order, reason, run)
    return paper_ops_engine._event(
        run,
        paper_ops_engine.PaperJobPhase.ENTER,
        order.strategy_id,
        order.symbol,
        "paper_order_blocked",
        f"{order.order_id}:{reason}",
        payload,
    ).to_dict()


def _seed_existing_and_incoming_order(
    root: Path,
) -> tuple[
    paper_ops_engine.PaperOpsPaths,
    dict[str, object],
    dict[str, object],
]:
    paths, existing = _seed_pending_canonical_order(root)
    incoming = _canonical_event("incoming")
    _seed_canonical_event_evidence(root, incoming)
    return paths, existing, incoming


def _seed_bound_check_run(
    paths: paper_ops_engine.PaperOpsPaths,
    *,
    run_date: date,
    run_bar: dict[str, float | int] | None,
    prior_bar_date: date = date(2026, 1, 2),
) -> tuple[paper_ops_engine.PaperRun, paper_ops_engine.MarketBar]:
    config = paper_ops_engine._config(paths)
    raw_dir = paths.root / "fixture_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    source_csv = paths.root / "fixture_source.csv"
    rows: list[dict[str, object]] = []
    for symbol in config.universe_symbols:
        rows.append(
            {
                "symbol": symbol,
                "timestamp": f"{prior_bar_date.isoformat()}T19:00:00+00:00",
                "open": 99.0,
                "high": 101.0,
                "low": 98.0,
                "close": 100.0,
                "volume": 1_000,
            }
        )
        if run_bar is not None:
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": f"{run_date.isoformat()}T21:00:00+00:00",
                    **run_bar,
                }
            )
    paper_ops_engine.write_csv(
        source_csv,
        rows,
        ("symbol", "timestamp", "open", "high", "low", "close", "volume"),
    )
    result = paper_ops_engine.build_data_truth_snapshot(
        as_of_date=run_date + timedelta(days=1),
        output_root=paths.root.parent / "v2_data_truth",
        created_at=datetime.combine(
            run_date + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc
        ),
        source_csv=source_csv,
        raw_dir=raw_dir,
        allow_fetch=False,
    )
    run = paper_ops_engine._paper_run(
        run_date=run_date,
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        data_snapshot_id=result.manifest.snapshot_id,
    )
    paper_ops_engine._ensure_run_manifest(
        paths,
        run,
        config=config,
        data_manifest=result.manifest,
        data_truth_root=paths.root.parent / "v2_data_truth",
    )
    dataset, _ = paper_ops_engine.load_datatruth_snapshot(
        result.manifest.snapshot_id,
        paths.root.parent / "v2_data_truth",
    )
    bar = next(
        item
        for item in dataset.bars_by_symbol["SPY"]
        if item.timestamp > datetime(2026, 1, 2, 20, 0, tzinfo=timezone.utc)
    )
    return run, bar


def _check_block_event(
    order_row: dict[str, object],
    run: paper_ops_engine.PaperRun,
    bar: paper_ops_engine.MarketBar,
    reason: str,
) -> dict[str, object]:
    order = paper_ops_engine._order_from_row(order_row)
    payload = paper_ops_engine._blocked_order_payload(
        order,
        reason,
        run,
        source_bar=bar,
    )
    return paper_ops_engine._event(
        run,
        paper_ops_engine.PaperJobPhase.CHECK,
        order.strategy_id,
        order.symbol,
        "paper_order_blocked",
        f"{order.order_id}:{reason}",
        payload,
    ).to_dict()


def _check_block_updates(
    paths: paper_ops_engine.PaperOpsPaths,
) -> dict[str, object]:
    return {
        "state/pending_orders.json": [],
        "state/open_positions.json": paper_ops_engine.read_json(
            paths.state / "open_positions.json", []
        ),
        "state/paper_accounts.json": paper_ops_engine.read_json(
            paths.state / "paper_accounts.json", {}
        ),
    }


@pytest.mark.parametrize("variant", ("missing", "malformed", "conflicting"))
def test_enter_block_requires_exact_accepted_pick_evidence(
    tmp_path: Path,
    variant: str,
) -> None:
    root = tmp_path / "paper_ops"
    paths, existing = _seed_pending_canonical_order(root)
    incoming = _canonical_event("incoming")
    _seed_run_manifest(root, incoming)
    if variant != "missing":
        _append_scan_event(paths, incoming)
        if variant == "malformed":
            picks: object = {"attacker": "not-an-array"}
        else:
            conflicting = _canonical_pick(incoming)
            conflicting["entry_reference"] = 999.0
            picks = [conflicting]
        paper_ops_engine.write_json(
            paths.exports / "picks_forward_2026-01-02.json", picks
        )
    event = _enter_block_event(incoming, "duplicate_strategy_symbol_exposure")
    existing_order = existing["payload"]
    assert isinstance(existing_order, dict)
    updates = {"state/pending_orders.json": [existing_order]}
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="enter-blocked order"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_enter_created_order_rejects_fabricated_equity_basis(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    event = _canonical_event("oversized")
    _seed_canonical_event_evidence(root, event)
    pick = paper_ops_engine._pick_from_row(_canonical_pick(event))
    run = paper_ops_engine.PaperRun(
        run_id=str(event["run_id"]),
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        run_date=str(event["trade_date"]),
        data_snapshot_id="fixture-snapshot",
        created_at=pick.signal_time,
    )
    inflated = paper_ops_engine._order_from_pick(
        pick,
        run,
        paper_ops_engine._config(paths),
        equity_basis=1_000_000_000.0,
    ).to_dict()
    event["payload"] = inflated
    updates = {"state/pending_orders.json": [inflated]}
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="pick/account execution semantics"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_enter_created_order_rejects_computed_block_reason(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, existing, incoming = _seed_existing_and_incoming_order(root)
    existing_order = existing["payload"]
    incoming_order = incoming["payload"]
    assert isinstance(existing_order, dict) and isinstance(incoming_order, dict)
    updates = {"state/pending_orders.json": [existing_order, incoming_order]}
    journal = _write_checksum_valid_journal(paths, [incoming], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="created order should have been blocked"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_enter_created_order_cannot_use_state_only_idempotence_bypass(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    event = _canonical_event("state-only-idempotence")
    _seed_canonical_event_evidence(root, event)
    order = event["payload"]
    assert isinstance(order, dict)
    paper_ops_engine.write_json(paths.state / "pending_orders.json", [order])
    updates = {"state/pending_orders.json": [order]}
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="created order should have been blocked"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_enter_block_rejects_order_that_should_be_created(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    incoming = _canonical_event("unnecessary-block")
    _seed_canonical_event_evidence(root, incoming)
    event = _enter_block_event(incoming, "max_concurrent_positions")
    updates = {"state/pending_orders.json": []}
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="enter-blocked order should have been created"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_enter_block_rejects_incorrect_computed_reason(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, existing, incoming = _seed_existing_and_incoming_order(root)
    event = _enter_block_event(incoming, "max_concurrent_positions")
    existing_order = existing["payload"]
    assert isinstance(existing_order, dict)
    updates = {"state/pending_orders.json": [existing_order]}
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="payload conflicts with producer semantics"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_check_block_rejects_safe_fill_bar(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, created = _seed_pending_canonical_order(root, symbol="SPY")
    run, bar = _seed_bound_check_run(
        paths,
        run_date=date(2026, 1, 5),
        run_bar={
            "open": 100.0,
            "high": 105.0,
            "low": 97.0,
            "close": 103.0,
            "volume": 1_000,
        },
    )
    order = created["payload"]
    assert isinstance(order, dict)
    event = _check_block_event(order, run, bar, "gap_through_stop")
    updates = _check_block_updates(paths)
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="check-blocked order should have filled"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_check_block_rejects_incorrect_computed_reason(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, created = _seed_pending_canonical_order(root, symbol="SPY")
    run, bar = _seed_bound_check_run(
        paths,
        run_date=date(2026, 1, 5),
        run_bar={
            "open": 94.0,
            "high": 100.0,
            "low": 90.0,
            "close": 96.0,
            "volume": 1_000,
        },
    )
    order = created["payload"]
    assert isinstance(order, dict)
    event = _check_block_event(order, run, bar, "missed_fill_session")
    updates = _check_block_updates(paths)
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="exact lifecycle decision"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_check_block_rejects_non_next_immutable_source_bar(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, created = _seed_pending_canonical_order(root, symbol="SPY")
    run, _ = _seed_bound_check_run(
        paths,
        run_date=date(2026, 1, 5),
        run_bar={
            "open": 94.0,
            "high": 100.0,
            "low": 90.0,
            "close": 96.0,
            "volume": 1_000,
        },
    )
    forged = paper_ops_engine.MarketBar(
        symbol="SPY",
        timestamp=datetime(2026, 1, 5, 22, 0, tzinfo=timezone.utc),
        open=94.0,
        high=100.0,
        low=90.0,
        close=96.0,
        volume=1_000,
    )
    order = created["payload"]
    assert isinstance(order, dict)
    event = _check_block_event(order, run, forged, "gap_through_stop")
    updates = _check_block_updates(paths)
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="not the next immutable bar"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


@pytest.mark.parametrize("variant", ("missing", "tampered"))
def test_check_block_requires_verified_bound_snapshot(
    tmp_path: Path,
    variant: str,
) -> None:
    root = tmp_path / "paper_ops"
    paths, created = _seed_pending_canonical_order(root, symbol="SPY")
    order = created["payload"]
    assert isinstance(order, dict)
    if variant == "missing":
        run = paper_ops_engine._paper_run(
            run_date=date(2026, 1, 5),
            mode=paper_ops_engine.PaperRunMode.FORWARD,
            data_snapshot_id="fixture-snapshot",
        )
        bar = paper_ops_engine.MarketBar(
            symbol="SPY",
            timestamp=datetime(2026, 1, 5, 21, 0, tzinfo=timezone.utc),
            open=94.0,
            high=100.0,
            low=90.0,
            close=96.0,
            volume=1_000,
        )
        event = _check_block_event(order, run, bar, "gap_through_stop")
        _seed_run_manifest(root, event)
    else:
        run, bar = _seed_bound_check_run(
            paths,
            run_date=date(2026, 1, 5),
            run_bar={
                "open": 94.0,
                "high": 100.0,
                "low": 90.0,
                "close": 96.0,
                "volume": 1_000,
            },
        )
        event = _check_block_event(order, run, bar, "gap_through_stop")
        manifest = paper_ops_engine.read_json(
            paths.manifests / f"{paper_ops_engine._safe_filename(run.run_id)}.json", {}
        )
        assert isinstance(manifest, dict)
        normalized = paths.root.parent / "v2_data_truth" / str(
            manifest["data_snapshot_normalized_path"]
        )
        normalized.write_bytes(normalized.read_bytes() + b"\n")
    updates = _check_block_updates(paths)
    journal = _write_checksum_valid_journal(paths, [event], updates)
    before = _tree_snapshot(root)

    with pytest.raises((FileNotFoundError, ValueError)):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


@pytest.mark.parametrize(
    ("run_date", "prior_bar_date", "run_bar", "reason"),
    (
        (
            date(2026, 1, 5),
            date(2026, 1, 2),
            {
                "open": 94.0,
                "high": 100.0,
                "low": 90.0,
                "close": 96.0,
                "volume": 1_000,
            },
            "gap_through_stop",
        ),
        (
            date(2026, 1, 6),
            date(2026, 1, 5),
            {
                "open": 100.0,
                "high": 105.0,
                "low": 97.0,
                "close": 103.0,
                "volume": 1_000,
            },
            "missed_fill_session",
        ),
    ),
)
def test_canonical_check_block_decision_applies(
    tmp_path: Path,
    run_date: date,
    prior_bar_date: date,
    run_bar: dict[str, float | int],
    reason: str,
) -> None:
    root = tmp_path / "paper_ops"
    paths, created = _seed_pending_canonical_order(root, symbol="SPY")
    run, bar = _seed_bound_check_run(
        paths,
        run_date=run_date,
        run_bar=run_bar,
        prior_bar_date=prior_bar_date,
    )
    order = created["payload"]
    assert isinstance(order, dict)
    event = _check_block_event(order, run, bar, reason)
    updates = _check_block_updates(paths)
    journal = _write_checksum_valid_journal(paths, [event], updates)

    paper_ops_engine._apply_transaction_journal(paths, journal)

    assert paper_ops_engine.read_json(paths.state / "pending_orders.json", None) == []
    assert read_jsonl(paths.ledger / "paper_ledger.jsonl")[-1] == event


def test_cross_series_fill_open_lineage_is_exact_tree_no_op(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, created_event = _seed_pending_canonical_order(root)
    events, path_updates, _ = _canonical_fill_open_transaction(paths, created_event)
    event_rows = paper_ops_engine._serialize_transaction_events(events)
    updates = paper_ops_engine._serialize_transaction_updates(paths, path_updates)
    created_payload = created_event["payload"]
    assert isinstance(created_payload, dict)
    registry = paper_ops_engine.read_json(paths.state / "strategy_registry.json", [])
    assert isinstance(registry, list)
    other = next(
        row
        for row in registry
        if isinstance(row, dict) and row["strategy_id"] != created_payload["strategy_id"]
    )
    for event in event_rows:
        payload = event["payload"]
        assert isinstance(payload, dict)
        event["strategy_id"] = other["strategy_id"]
        event["symbol"] = "BBB"
        payload.update(
            strategy_id=other["strategy_id"],
            strategy_version=other["strategy_version"],
            strategy_semantics_fingerprint=other["strategy_semantics_fingerprint"],
            symbol="BBB",
        )
        source_bar = payload.get("source_bar")
        if isinstance(source_bar, dict):
            source_bar["symbol"] = "BBB"
            payload["source_bar_sha256"] = paper_ops_engine._transaction_payload_sha256(
                source_bar
            )
    positions = updates["state/open_positions.json"]
    assert isinstance(positions, list) and isinstance(positions[0], dict)
    positions[0].update(
        strategy_id=other["strategy_id"],
        strategy_version=other["strategy_version"],
        strategy_semantics_fingerprint=other["strategy_semantics_fingerprint"],
        symbol="BBB",
    )
    journal = _write_checksum_valid_journal(paths, event_rows, updates)
    before = _tree_snapshot(root)

    with pytest.raises(
        ValueError,
        match="fill economics conflict with order/source-bar lineage",
    ):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_close_without_account_update_is_exact_tree_no_op(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, position = _seed_open_canonical_position(root)
    events, path_updates, _ = _canonical_close_transaction(paths, position)
    event_rows = paper_ops_engine._serialize_transaction_events(events)
    updates = paper_ops_engine._serialize_transaction_updates(paths, path_updates)
    updates.pop("state/paper_accounts.json")
    journal = _write_checksum_valid_journal(paths, event_rows, updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="champion transaction target set conflicts"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_impossible_close_economics_is_exact_tree_no_op(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, position = _seed_open_canonical_position(root)
    events, path_updates, trigger_bar = _canonical_close_transaction(paths, position)
    event_rows = paper_ops_engine._serialize_transaction_events(events)
    updates = paper_ops_engine._serialize_transaction_updates(paths, path_updates)
    safe_bar = {
        "close": 103.0,
        "high": 105.0,
        "low": 97.0,
        "open": 103.0,
        "symbol": trigger_bar.symbol,
        "timestamp": trigger_bar.timestamp.isoformat(),
        "volume": trigger_bar.volume,
    }
    payload = event_rows[0]["payload"]
    assert isinstance(payload, dict)
    payload["source_bar"] = safe_bar
    payload["source_bar_sha256"] = paper_ops_engine._transaction_payload_sha256(safe_bar)
    journal = _write_checksum_valid_journal(paths, event_rows, updates)
    before = _tree_snapshot(root)

    with pytest.raises(
        ValueError,
        match="close conflicts with position/source-bar lifecycle semantics",
    ):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def test_champion_shadow_mark_alias_is_exact_tree_no_op(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    paths, position = _seed_open_canonical_position(root)
    run_date = date(2026, 1, 6)
    run = paper_ops_engine._paper_run(
        run_date=run_date,
        mode=paper_ops_engine.PaperRunMode.FORWARD,
        data_snapshot_id="fixture-snapshot",
    )
    bar = paper_ops_engine.MarketBar(
        symbol=position.symbol,
        timestamp=datetime(2026, 1, 6, 21, 0, tzinfo=timezone.utc),
        open=103.0,
        high=105.0,
        low=97.0,
        close=104.0,
        volume=1_000,
    )
    config = paper_ops_engine._config(paths)
    checked, close_record = paper_ops_engine._check_position(position, bar, run, config)
    assert close_record is None
    event = paper_ops_engine._event(
        run,
        paper_ops_engine.PaperJobPhase.CLOSE,
        checked.strategy_id,
        checked.symbol,
        "paper_position_marked_to_market",
        f"{checked.position_id}:mark:{run_date.isoformat()}",
        paper_ops_engine._with_source_bar(checked.to_dict(), bar, run),
    )
    _seed_run_manifest(root, event.to_dict())
    accounts = paper_ops_engine._accounts(paths)
    accounts = paper_ops_engine._apply_mark(accounts, checked)
    accounts = paper_ops_engine._recalculate_unrealized_accounts(
        accounts, [checked.to_dict()]
    )
    path_updates: dict[Path, object] = {
        paths.state / "open_positions.json": [checked.to_dict()],
        paths.state / "paper_accounts.json": paper_ops_engine._account_state_payload(
            paths,
            paper_ops_engine.PaperRunMode.FORWARD,
            accounts,
        ),
    }
    event_rows = paper_ops_engine._serialize_transaction_events([event])
    event_rows[0]["event_id"] = paper_ops_engine.stable_id(
        "paper_ops_event",
        "forward",
        run_date.isoformat(),
        "close",
        "paper_position_marked_to_market",
        f"{checked.position_id}:shadow_mark:{run_date.isoformat()}",
    )
    updates = paper_ops_engine._serialize_transaction_updates(paths, path_updates)
    journal = _write_checksum_valid_journal(paths, event_rows, updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="phase identity conflicts with producer context"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


@pytest.mark.parametrize(
    ("phase", "mutation"),
    (
        ("enter", "extra"),
        ("enter", "omitted"),
        ("check", "extra"),
        ("check", "omitted"),
        ("close", "extra"),
        ("close", "omitted"),
        ("shadow", "extra"),
        ("shadow", "omitted"),
    ),
)
def test_eventful_transaction_target_sets_are_exact_tree_no_ops(
    tmp_path: Path,
    phase: str,
    mutation: str,
) -> None:
    root = tmp_path / "paper_ops"
    if phase == "enter":
        paper_ops_engine.init(output_root=root)
        paths = paper_ops_engine.PaperOpsPaths.create(root)
        event = _canonical_event("target-set-enter")
        _seed_canonical_event_evidence(root, event)
        event_rows = [event]
        updates: dict[str, object] = {
            "state/pending_orders.json": [event["payload"]]
        }
        omitted_target = "state/pending_orders.json"
        extra_target = "state/paper_accounts.json"
    elif phase == "check":
        paths, created_event = _seed_pending_canonical_order(root)
        events, path_updates, _ = _canonical_fill_open_transaction(paths, created_event)
        event_rows = paper_ops_engine._serialize_transaction_events(events)
        updates = paper_ops_engine._serialize_transaction_updates(paths, path_updates)
        omitted_target = "state/paper_accounts.json"
        extra_target = "state/replay_paper_accounts.json"
    elif phase == "close":
        paths, position = _seed_open_canonical_position(root)
        events, path_updates, _ = _canonical_close_transaction(paths, position)
        event_rows = paper_ops_engine._serialize_transaction_events(events)
        updates = paper_ops_engine._serialize_transaction_updates(paths, path_updates)
        omitted_target = "state/paper_accounts.json"
        extra_target = "state/replay_paper_accounts.json"
    else:
        paper_ops_engine.init(output_root=root)
        paths = paper_ops_engine.PaperOpsPaths.create(root)
        event_rows, updates = _canonical_shadow_transaction(paths)
        omitted_target = "manifests/shadow_forward_2026-01-02_challenger-a.json"
        extra_target = "state/pending_orders.json"
    if mutation == "omitted":
        updates.pop(omitted_target)
    else:
        if phase == "shadow":
            updates[extra_target] = []
        else:
            account_state = paper_ops_engine.read_json(
                paths.state / "paper_accounts.json", {}
            )
            updates[extra_target] = copy.deepcopy(account_state)
    journal = _write_checksum_valid_journal(paths, event_rows, updates)
    before = _tree_snapshot(root)

    with pytest.raises(ValueError, match="transaction target set conflicts with producer"):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


def _canonical_shadow_transaction(
    paths: paper_ops_engine.PaperOpsPaths,
) -> tuple[list[dict[str, object]], dict[str, object]]:
    order_event = _canonical_event("shadow")
    _seed_run_manifest(paths.root, order_event)
    assert isinstance(order_event["payload"], dict)
    order_event["payload"]["challenger_id"] = "challenger-a"
    order = {
        field: value for field, value in order_event["payload"].items() if field != "challenger_id"
    }
    logic_hash = "b" * 64
    pick = {
        "decision": "accepted",
        "direction": order["direction"],
        "entry_reference": order["entry"],
        "evidence": ["canonical shadow fixture"],
        "execution_policy_version": order["execution_policy_version"],
        "mode": order["mode"],
        "pick_id": order["pick_id"],
        "reason": "accepted",
        "reward_per_unit": order["reward_per_unit"],
        "reward_risk": order["reward_risk"],
        "risk_per_unit": order["risk_per_unit"],
        "run_id": order["run_id"],
        "schema_version": "v2.paper_pick.v2",
        "setup_score": 85.0,
        "signal_time": order["signal_time"],
        "stop": order["stop"],
        "strategy_id": order["strategy_id"],
        "strategy_semantics_fingerprint": order["strategy_semantics_fingerprint"],
        "strategy_status": "candidate",
        "strategy_version": order["strategy_version"],
        "symbol": order["symbol"],
        "target": order["target"],
        "trade_date": order["trade_date"],
        "warnings": [],
    }
    pick_event = {
        "event_id": paper_ops_engine.stable_id(
            "paper_ops_event",
            "forward",
            "2026-01-02",
            "scan",
            "paper_pick_decision",
            pick["pick_id"],
        ),
        "event_type": "paper_pick_decision",
        "mode": "forward",
        "payload": {
            **pick,
            "challenger_id": "challenger-a",
            "logic_artifact_sha256": logic_hash,
        },
        "run_id": order["run_id"],
        "schema_version": "v2.paper_ledger_event.v1",
        "strategy_id": order["strategy_id"],
        "symbol": order["symbol"],
        "trade_date": "2026-01-02",
    }
    events = [pick_event, order_event]
    decision_rows = [
        {
            **pick,
            "challenger_id": "challenger-a",
            "decision_status": "accepted",
            "logic_artifact_sha256": logic_hash,
            "trade_return_eligible": True,
            "trade_return_pct": None,
        }
    ]
    account = {
        "current_equity": 100_000.0,
        "execution_policy_version": order["execution_policy_version"],
        "realized_pnl": 0.0,
        "schema_version": "v2.strategy_paper_account.v3",
        "starting_equity": 100_000.0,
        "strategy_id": order["strategy_id"],
        "strategy_semantics_fingerprint": order["strategy_semantics_fingerprint"],
        "strategy_version": order["strategy_version"],
        "unrealized_pnl": 0.0,
    }
    core = paper_ops_engine.read_json(paths.state / "paper_accounts.json", {})
    assert isinstance(core, dict) and isinstance(core.get("accounts"), list)
    assert core["accounts"][-1] == account
    manifest = {
        "automatic_promotion_enabled": False,
        "broker_execution_allowed": False,
        "calendar_warnings": [],
        "challenger_id": "challenger-a",
        "closes": 0,
        "data_snapshot_id": "fixture-snapshot",
        "date": "2026-01-02",
        "decision_artifact_sha256": paper_ops_engine._transaction_payload_sha256(decision_rows),
        "decision_coverage": 1,
        "decision_coverage_status": "complete",
        "decision_symbols_sha256": paper_ops_engine._transaction_payload_sha256(
            [order["symbol"]]
        ),
        "execution_policy_version": order["execution_policy_version"],
        "fills": 0,
        "logic_artifact_sha256": logic_hash,
        "mode": "forward",
        "open_positions": 0,
        "orders_blocked": 0,
        "orders_created": 1,
        "pending_orders": 1,
        "research_only": True,
        "run_id": order["run_id"],
        "schema_version": "v2.paper_ops_shadow_run.v1",
        "status": "completed",
        "strategy_id": order["strategy_id"],
        "strategy_semantics_fingerprint": order["strategy_semantics_fingerprint"],
        "strategy_version": order["strategy_version"],
        "transaction_event_count": len(events),
        "transaction_event_ids_sha256": paper_ops_engine._transaction_payload_sha256(
            sorted(str(row["event_id"]) for row in events)
        ),
        "transaction_events_sha256": paper_ops_engine._transaction_payload_sha256(events),
    }
    updates = {
        "state/shadow/challenger-a/forward_pending_orders.json": [order],
        "state/shadow/challenger-a/forward_open_positions.json": [],
        "state/shadow/challenger-a/forward_account.json": {
            "account": account,
            "schema_version": "v2.paper_ops_shadow_account.v1",
        },
        "state/paper_accounts.json": core,
        "exports/shadow_strategy_decisions_forward_2026-01-02_challenger-a.json": decision_rows,
        "exports/shadow_picks_forward_2026-01-02_challenger-a.json": [pick],
        "exports/shadow_order_decisions_forward_2026-01-02_challenger-a.json": [
            {"decision": "created", "reason": "risk_checks_passed", **order}
        ],
        "manifests/shadow_forward_2026-01-02_challenger-a.json": manifest,
    }
    return events, updates


@pytest.mark.parametrize(
    "variant",
    (
        "core-mirror",
        "position-unrealized",
        "manifest-event-count",
        "decision-entry",
        "pick-entry",
        "order-entry",
    ),
)
def test_shadow_transaction_cross_binding_conflicts_fail_before_any_write(
    tmp_path: Path,
    variant: str,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    events, updates = _canonical_shadow_transaction(paths)
    shadow_payload = updates["state/shadow/challenger-a/forward_account.json"]
    core = updates["state/paper_accounts.json"]
    decisions = updates[
        "exports/shadow_strategy_decisions_forward_2026-01-02_challenger-a.json"
    ]
    picks = updates["exports/shadow_picks_forward_2026-01-02_challenger-a.json"]
    order_decisions = updates[
        "exports/shadow_order_decisions_forward_2026-01-02_challenger-a.json"
    ]
    manifest = updates["manifests/shadow_forward_2026-01-02_challenger-a.json"]
    assert isinstance(shadow_payload, dict) and isinstance(shadow_payload["account"], dict)
    assert isinstance(core, dict) and isinstance(core["accounts"], list)
    assert isinstance(decisions, list) and isinstance(decisions[0], dict)
    assert isinstance(picks, list) and isinstance(picks[0], dict)
    assert isinstance(order_decisions, list) and isinstance(order_decisions[0], dict)
    assert isinstance(manifest, dict)
    if variant == "core-mirror":
        shadow_payload["account"].update(realized_pnl=1.0, current_equity=100_001.0)
    elif variant == "position-unrealized":
        shadow_payload["account"].update(unrealized_pnl=1.0, current_equity=100_001.0)
        core["accounts"][-1] = copy.deepcopy(shadow_payload["account"])
    elif variant == "manifest-event-count":
        manifest["transaction_event_count"] = int(manifest["transaction_event_count"]) + 1
    elif variant == "decision-entry":
        decisions[0]["entry_reference"] = 999.0
        manifest["decision_artifact_sha256"] = paper_ops_engine._transaction_payload_sha256(
            decisions
        )
    elif variant == "pick-entry":
        picks[0]["entry_reference"] = 999.0
    else:
        config = paper_ops_engine._config(paths)
        entry = 101.0
        stop = float(order_decisions[0]["stop"])
        target = float(order_decisions[0]["target"])
        rate = config.slippage_bps / 10_000.0
        entry_fill = entry * (1 + rate)
        stop_fill = stop * (1 - rate)
        risk_per_unit = (entry_fill - stop_fill) + (
            entry_fill + stop_fill
        ) * config.fee_bps / 10_000.0
        risk_budget = float(order_decisions[0]["risk_budget"])
        quantity = int(risk_budget / risk_per_unit)
        order_decisions[0].update(
            entry=entry,
            max_loss_estimate=quantity * risk_per_unit,
            notional_exposure=quantity * entry,
            quantity=quantity,
            reward_per_unit=target - entry,
            reward_risk=(target - entry) / (entry - stop),
            risk_per_unit=risk_per_unit,
        )
    journal = {
        "events": events,
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id(events, updates),
    }
    journal_path = paths.state / "paper_transaction_pending.json"
    paper_ops_engine.write_json(journal_path, journal)
    before = _tree_snapshot(root)

    with pytest.raises(
        ValueError,
        match=(
            "shadow and core account state conflict"
            if variant == "core-mirror"
            else "unrealized PnL conflicts with (?:final )?positions"
            if variant == "position-unrealized"
            else "shadow manifest event count conflicts"
            if variant == "manifest-event-count"
            else "shadow scan artifacts conflict with ledger decisions"
            if variant in {"decision-entry", "pick-entry"}
            else "shadow order-decision artifacts conflict with ledger events"
        ),
    ):
        paper_ops_engine._apply_transaction_journal(paths, journal)

    assert _tree_snapshot(root) == before


@pytest.mark.parametrize("artifact", ("manifest", "decision-export"))
def test_shadow_immutable_evidence_cannot_be_overwritten(
    tmp_path: Path,
    artifact: str,
) -> None:
    root = tmp_path / "paper_ops"
    paper_ops_engine.init(output_root=root)
    paths = paper_ops_engine.PaperOpsPaths.create(root)
    events, updates = _canonical_shadow_transaction(paths)
    first_journal = {
        "events": events,
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": updates,
        "transaction_id": paper_ops_engine._paper_transaction_id(events, updates),
    }
    paper_ops_engine._apply_transaction_journal(paths, first_journal)
    changed = copy.deepcopy(updates)
    if artifact == "manifest":
        manifest = changed["manifests/shadow_forward_2026-01-02_challenger-a.json"]
        assert isinstance(manifest, dict)
        manifest["calendar_warnings"] = ["attacker changed immutable evidence"]
    else:
        decisions = changed[
            "exports/shadow_strategy_decisions_forward_2026-01-02_challenger-a.json"
        ]
        assert isinstance(decisions, list) and isinstance(decisions[0], dict)
        decisions[0]["entry_reference"] = 999.0
        manifest = changed["manifests/shadow_forward_2026-01-02_challenger-a.json"]
        assert isinstance(manifest, dict)
        manifest["decision_artifact_sha256"] = paper_ops_engine._transaction_payload_sha256(
            decisions
        )
    replay_journal = {
        "events": events,
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": changed,
        "transaction_id": paper_ops_engine._paper_transaction_id(events, changed),
    }
    before = _tree_snapshot(root)

    with pytest.raises(
        ValueError,
        match="immutable transaction output conflicts with persisted evidence",
    ):
        paper_ops_engine._apply_transaction_journal(paths, replay_journal)

    assert _tree_snapshot(root) == before
