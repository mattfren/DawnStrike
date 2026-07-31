from __future__ import annotations

from pathlib import Path

import pytest

from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.engine import init
from intraday_scanner.v2.paper_ops.storage import read_json, write_json, write_jsonl
from intraday_scanner.v2.paper_ops.trade_blotter import (
    build_trade_blotter,
    verify_trade_blotter,
)


class _SourceTruthStub:
    status = "passed"
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "warnings": []}


@pytest.fixture(autouse=True)
def _stub_source_truth(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "intraday_scanner.v2.paper_ops.trade_blotter.verify_source_bar_truth",
        lambda *, output_root, mode=None: _SourceTruthStub(),
    )


def _registry_row(root: Path) -> dict[str, object]:
    rows = read_json(root / "state" / "strategy_registry.json", [])
    assert isinstance(rows, list)
    return dict(rows[0])


def _event(
    event_id: str,
    event_type: str,
    payload: dict[str, object],
    *,
    run_id: str = "run-entry",
    trade_date: str = "2026-01-05",
) -> dict[str, object]:
    return {
        "event_id": event_id,
        "event_type": event_type,
        "mode": "forward",
        "payload": payload,
        "run_id": run_id,
        "strategy_id": payload.get("strategy_id"),
        "symbol": payload.get("symbol"),
        "trade_date": trade_date,
    }


def _lifecycle(
    root: Path,
    *,
    prefix: str = "champion",
    challenger_id: str = "",
    net_pnl: float = 9.0,
    include_close: bool = True,
) -> list[dict[str, object]]:
    registry = _registry_row(root)
    strategy_id = str(registry["strategy_id"])
    version = str(registry["strategy_version"])
    policy = str(registry["execution_policy_version"])
    fingerprint = str(registry["strategy_semantics_fingerprint"])
    lineage = {
        "challenger_id": challenger_id,
        "execution_policy_version": policy,
        "strategy_id": strategy_id,
        "strategy_semantics_fingerprint": fingerprint,
        "strategy_status": registry["strategy_status"],
        "strategy_version": version,
        "symbol": "AAA",
    }
    pick_id = f"pick-{prefix}"
    order_id = f"order-{prefix}"
    position_id = f"position-{prefix}"
    decision = {
        **lineage,
        "decision": "accepted",
        "entry_reference": 10.0,
        "mode": "forward",
        "pick_id": pick_id,
        "reason": "accepted",
        "run_id": "run-entry",
        "setup_score": 80.0,
        "signal_time": "2026-01-05T21:00:00+00:00",
        "stop": 9.0,
        "target": 12.0,
        "trade_date": "2026-01-05",
    }
    order = {
        **lineage,
        "direction": "long",
        "earliest_fill_date": "2026-01-06",
        "entry": 10.0,
        "expected_fill_rule": "daily signal fills no earlier than next valid bar open",
        "mode": "forward",
        "order_id": order_id,
        "pick_id": pick_id,
        "quantity": 100,
        "run_id": "run-entry",
        "signal_time": decision["signal_time"],
        "stop": 9.0,
        "target": 12.0,
        "trade_date": "2026-01-05",
    }
    fill = {
        **lineage,
        "fee": 0.1,
        "fill_id": f"fill-{prefix}",
        "fill_price": 10.0,
        "fill_time": "2026-01-06T14:30:00+00:00",
        "mode": "forward",
        "order_id": order_id,
        "quantity": 100,
        "run_id": "run-fill",
        "slippage": 0.25,
    }
    position = {
        **lineage,
        "direction": "long",
        "entry_fee": 0.1,
        "entry_price": 10.0,
        "last_mark_price": 10.0,
        "opened_at": fill["fill_time"],
        "order_id": order_id,
        "position_id": position_id,
        "quantity": 100,
        "status": "open",
        "stop": 9.0,
        "target": 12.0,
        "unrealized_pnl": -0.1,
    }
    mark = {**position, "last_mark_price": 11.0, "unrealized_pnl": 99.9}
    events = [
        _event(f"decision-{prefix}", "paper_pick_decision", decision),
        _event(f"order-{prefix}", "paper_order_created", order),
        _event(
            f"fill-{prefix}",
            "paper_fill",
            fill,
            run_id="run-fill",
            trade_date="2026-01-06",
        ),
        _event(f"open-{prefix}", "paper_position_opened", position, trade_date="2026-01-06"),
        _event(
            f"mark-{prefix}",
            "paper_position_marked_to_market",
            mark,
            trade_date="2026-01-06",
        ),
    ]
    if include_close:
        fee_rate = 1.0 / 10_000.0
        slippage_rate = 5.0 / 10_000.0
        close_price = (net_pnl + 1_000.0 + 0.1) / (100.0 * (1.0 - fee_rate))
        exit_fee = close_price * 100.0 * fee_rate
        gross_pnl = (close_price - 10.0) * 100.0
        stop_fill = 9.0 * (1.0 - slippage_rate)
        risk_amount = (10.0 - stop_fill) * 100.0 + 0.1 + stop_fill * 100.0 * fee_rate
        close = {
            **lineage,
            "close_id": f"close-{prefix}",
            "close_price": close_price,
            "close_reason": "timeout",
            "close_time": "2026-01-07T21:00:00+00:00",
            "entry_fee": 0.1,
            "fee": exit_fee,
            "gross_pnl": gross_pnl,
            "mode": "forward",
            "net_pnl": net_pnl,
            "position_id": position_id,
            "r_multiple": net_pnl / risk_amount,
            "run_id": "run-close",
            "slippage": 0.3,
        }
        events.append(
            _event(
                f"close-{prefix}",
                "paper_position_closed",
                close,
                run_id="run-close",
                trade_date="2026-01-07",
            )
        )
    return events


def _seed_manifests(root: Path) -> None:
    write_json(
        root / "manifests" / "entry.json",
        {"data_snapshot_id": "snapshot-entry", "run_id": "run-entry"},
    )
    write_json(
        root / "manifests" / "fill.json",
        {"data_snapshot_id": "snapshot-fill", "run_id": "run-fill"},
    )
    write_json(
        root / "manifests" / "close.json",
        {"data_snapshot_id": "snapshot-close", "run_id": "run-close"},
    )


def test_trade_blotter_joins_exact_round_trip_and_latest_mark(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", _lifecycle(root))

    result = build_trade_blotter(output_root=root)
    verification = verify_trade_blotter(output_root=root)

    assert result["status"] == "passed"
    assert verification["status"] == "passed"
    row = result["rows"][0]
    assert row["lifecycle_status"] == "closed"
    assert row["last_mark_price"] == 11.0
    assert row["fees_paid"] == pytest.approx(0.1 + row["exit_fee"])
    assert row["slippage_paid"] == 0.55
    assert row["net_pnl"] == 9.0
    assert row["trade_return_pct"] == 0.009
    assert result["schema_version"] == "v2.paper_trade_blotter.v2"
    assert row["run_id"] == "run-entry"
    assert row["data_snapshot_id"] == "snapshot-entry"
    assert row["fill_run_id"] == "run-fill"
    assert row["fill_data_snapshot_id"] == "snapshot-fill"
    assert row["close_run_id"] == "run-close"
    assert row["close_data_snapshot_id"] == "snapshot-close"
    stored = read_json(root / "exports" / "paper_trade_blotter.json", {})
    assert isinstance(stored, dict)
    stored_rows = stored["rows"]
    assert isinstance(stored_rows, list)
    assert stored_rows[0]["fill_run_id"] == "run-fill"
    assert stored_rows[0]["fill_data_snapshot_id"] == "snapshot-fill"
    csv_fields = (
        root / "exports" / "paper_trade_blotter.csv"
    ).read_text(encoding="utf-8").splitlines()[0].split(",")
    assert csv_fields[:8] == [
        "record_id",
        "mode",
        "signal_date",
        "run_id",
        "fill_run_id",
        "close_run_id",
        "data_snapshot_id",
        "fill_data_snapshot_id",
    ]


def test_trade_blotter_open_trade_keeps_return_na(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    write_jsonl(
        root / "ledger" / "paper_ledger.jsonl",
        _lifecycle(root, include_close=False),
    )

    result = build_trade_blotter(output_root=root)
    row = result["rows"][0]

    assert result["status"] == "passed"
    assert row["lifecycle_status"] == "open"
    assert row["last_mark_price"] == 11.0
    assert row["trade_return_pct"] is None
    assert result["capital_weighted_closed_trade_return_pct"] is None


def test_trade_blotter_rejects_fill_envelope_payload_run_mismatch(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = _lifecycle(root)
    fill_event = next(event for event in events if event["event_type"] == "paper_fill")
    fill_event["run_id"] = "run-fill-conflict"
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", events)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "failed"
    assert result["rows"][0]["run_id"] == "run-entry"
    assert result["rows"][0]["fill_run_id"] == "run-fill-conflict"
    assert any(
        "envelope/payload run_id mismatch" in warning
        for warning in result["warnings"]
    )


def test_trade_blotter_rejects_missing_fill_envelope_run_id(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = _lifecycle(root)
    fill_event = next(event for event in events if event["event_type"] == "paper_fill")
    fill_event["run_id"] = ""
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", events)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "failed"
    assert result["rows"][0]["fill_run_id"] == ""
    assert any(
        "fill envelope run_id is missing" in warning
        for warning in result["warnings"]
    )


def test_trade_blotter_fails_when_fill_snapshot_is_unknown(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    (root / "manifests" / "fill.json").unlink()
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", _lifecycle(root))

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "failed"
    assert result["rows"][0]["data_snapshot_id"] == "snapshot-entry"
    assert result["rows"][0]["fill_data_snapshot_id"] == "unknown"
    assert any(
        "unknown fill data snapshot" in warning for warning in result["warnings"]
    )


def test_trade_blotter_fails_on_orphan_fill(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    registry = _registry_row(root)
    write_jsonl(
        root / "ledger" / "paper_ledger.jsonl",
        [
            _event(
                "orphan-fill",
                "paper_fill",
                {
                    "execution_policy_version": registry["execution_policy_version"],
                    "fill_id": "orphan-fill",
                    "order_id": "missing-order",
                    "strategy_id": registry["strategy_id"],
                    "strategy_version": registry["strategy_version"],
                    "symbol": "AAA",
                },
            )
        ],
    )

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "failed"
    assert any("orphan fill" in warning for warning in result["warnings"])


def test_trade_blotter_separates_champion_and_shadow_series(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = [
        *_lifecycle(root, prefix="champion", net_pnl=10.0),
        *_lifecycle(
            root,
            prefix="shadow",
            challenger_id="candidate-1",
            net_pnl=-5.0,
        ),
    ]
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", events)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "passed"
    assert {row["series_role"] for row in result["rows"]} == {"champion", "challenger"}
    assert len(result["series_summaries"]) == 2
    assert result["official_champion_total_net_pnl"] == 10.0
    assert result["total_net_pnl"] == 5.0


@pytest.mark.parametrize(
    ("field", "warning_fragment"),
    (
        ("gross_pnl", "gross_pnl arithmetic mismatch"),
        ("net_pnl", "net_pnl arithmetic mismatch"),
        ("r_multiple", "r_multiple arithmetic mismatch"),
        ("fee", "exit fee does not match execution policy"),
        ("entry_fee", "close entry fee lineage mismatch"),
    ),
)
def test_trade_blotter_recomputes_close_economics(
    tmp_path: Path,
    field: str,
    warning_fragment: str,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = _lifecycle(root)
    close = next(
        event["payload"]
        for event in events
        if event["event_type"] == "paper_position_closed"
    )
    assert isinstance(close, dict)
    close[field] = float(close[field]) + 1.0
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", events)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "failed"
    assert any(warning_fragment in warning for warning in result["warnings"])


@pytest.mark.parametrize(
    ("field", "value", "warning_fragment"),
    (
        ("close_reason", "invented", "close reason is invalid"),
        ("slippage", -1.0, "exit slippage is missing or invalid"),
        ("slippage", "nan", "exit slippage is missing or invalid"),
    ),
)
def test_trade_blotter_rejects_invalid_close_metadata(
    tmp_path: Path,
    field: str,
    value: object,
    warning_fragment: str,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = _lifecycle(root)
    close = next(
        event["payload"]
        for event in events
        if event["event_type"] == "paper_position_closed"
    )
    assert isinstance(close, dict)
    close[field] = value
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", events)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "failed"
    assert any(warning_fragment in warning for warning in result["warnings"])


def test_trade_blotter_rejects_linked_semantics_mismatch(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = _lifecycle(root)
    fill = next(event["payload"] for event in events if event["event_type"] == "paper_fill")
    assert isinstance(fill, dict)
    fill["strategy_semantics_fingerprint"] = "0" * 64
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", events)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "failed"
    assert any("fill strategy_semantics_fingerprint" in item for item in result["warnings"])


def test_trade_blotter_accepts_explicit_cross_day_order_lifecycle_run_ids(
    tmp_path: Path,
) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = _lifecycle(root)
    order_event = next(event for event in events if event["event_type"] == "paper_order_created")
    assert isinstance(order_event["payload"], dict)
    order = order_event["payload"]
    block = {
        **order,
        "decision": "blocked",
        "lifecycle_run_id": "paper_ops:replay:2026-01-06:snapshot-next",
        "origin_run_id": order["run_id"],
        "reason": "fill_risk_budget_exceeded",
    }
    events = [
        event
        for event in events
        if event["event_type"]
        not in {
            "paper_fill",
            "paper_position_opened",
            "paper_position_marked_to_market",
            "paper_position_closed",
        }
    ]
    events.append(
        {
            "event_id": "blocked-next-day",
            "event_type": "paper_order_blocked",
            "mode": order["mode"],
            "run_id": block["lifecycle_run_id"],
            "strategy_id": order["strategy_id"],
            "symbol": order["symbol"],
            "payload": block,
        }
    )
    write_jsonl(root / "ledger" / "paper_ledger.jsonl", events)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "passed", result["warnings"]
    assert result["rows"][0]["lifecycle_status"] == "blocked"


def test_trade_blotter_recovers_pending_transaction_before_read(tmp_path: Path) -> None:
    root = tmp_path / "paper_ops"
    init(output_root=root)
    _seed_manifests(root)
    events = _lifecycle(root)
    state_updates: dict[str, object] = {}
    journal = {
        "events": events,
        "schema_version": "v2.paper_transaction.v1",
        "state_updates": state_updates,
        "transaction_id": paper_ops_engine._paper_transaction_id(events, state_updates),
    }
    journal_path = root / "state" / "paper_transaction_pending.json"
    write_json(journal_path, journal)

    result = build_trade_blotter(output_root=root)

    assert result["status"] == "passed"
    assert result["row_count"] == 1
    assert not journal_path.exists()
