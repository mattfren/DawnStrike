from __future__ import annotations

import csv
import hashlib
import json
from functools import lru_cache
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from intraday_scanner.cli import build_arg_parser
from intraday_scanner.cli import main as cli_main
from intraday_scanner.errors import NotificationError
from intraday_scanner.notifiers.base import NotificationEvent
from intraday_scanner.services import paperops_fleet_telegram_service
from intraday_scanner.services.paperops_fleet_telegram_service import (
    _capped_digest_message,
    _eligible_catalog_strategy_ids,
    _load_ledger_events,
    build_paperops_fleet_digest,
    send_paperops_fleet_digest,
)
from intraday_scanner.services.strategy_fleet_report_service import (
    ALPHAOPS_HORIZON,
    ALPHAOPS_SOURCE,
    PAPEROPS_HORIZON,
    PAPEROPS_SOURCE,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore
from intraday_scanner.v2.paper_ops.engine import (
    _config_from_payload,
    _execution_policy_fingerprint_payload,
    _strategy_semantics_fingerprint,
    _strategy_semantics_payload,
)
from intraday_scanner.v2.paper_ops.models import PAPER_EXECUTION_POLICY_VERSION
from intraday_scanner.v2.strategies import build_strategy_catalog

DAY = "2026-07-15"
STRATEGIES = tuple(
    sorted(
        strategy.strategy_id
        for strategy in build_strategy_catalog()
        if strategy.status not in {"baseline", "benchmark", "parked", "quarantined", "rejected"}
    )
)


class _CalendarTruthStub:
    def __init__(
        self,
        *,
        status: str = "passed",
        math_mismatches: tuple[str, ...] = (),
        ledger_mismatches: tuple[str, ...] = (),
    ) -> None:
        self.status = status
        self.math_mismatches = math_mismatches
        self.ledger_mismatches = ledger_mismatches

    def to_dict(self) -> dict[str, object]:
        return {
            "duplicate_rows": [],
            "ledger_mismatches": list(self.ledger_mismatches),
            "math_mismatches": list(self.math_mismatches),
            "missing_rows": [],
            "schema_version": "v2.paper_ops_calendar_truth.v2",
            "status": self.status,
            "warnings": [],
        }


class _SourceTruthStub:
    status = "passed"
    warnings: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {"status": self.status, "warnings": []}


@pytest.fixture(autouse=True)
def _stub_calendar_truth_verifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "intraday_scanner.services.paperops_fleet_telegram_service.verify_calendar_truth",
        lambda *, output_root: _CalendarTruthStub(),
    )
    monkeypatch.setattr(
        "intraday_scanner.services.paperops_fleet_telegram_service.verify_source_bar_truth",
        lambda *, output_root, mode: _SourceTruthStub(),
    )


def test_digest_distinguishes_forward_lifecycle_no_trade_replay_and_baselines(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["status"] == "ready"
    assert digest["ready"] is True
    message = digest["message"]
    assert f"FORWARD — {len(STRATEGIES)} daily-swing strategies" in message
    assert "opened 1, closed 0 | forward paper" in message
    assert "1 paper entry pending | trade return N/A" in message
    assert "held 1; no new fill" in message
    assert "accepted signal, no paper fill | trade return N/A" in message
    assert "no setup | trade return N/A" in message
    assert "Benchmark (equal-weight buy/hold): +0.40%" in message
    assert "Cash no-trade policy: +0.00%" in message
    assert "Replay: excluded from forward results (21 stored row(s))." in message
    assert "No triggered/closed official paper trade; return N/A (not 0%)." in message
    assert "N/A means no eligible trade return; it is never converted to 0%." in message
    assert "trade return +0.00%" not in message
    assert digest["summary"]["classifications"] == {
        "fill_activity": 1,
        "held": 1,
        "no_fill": 1,
        "no_setup": len(STRATEGIES) - 4,
        "pending": 1,
    }
    assert digest["summary"]["active_execution_policy_version"] == (
        PAPER_EXECUTION_POLICY_VERSION
    )
    assert digest["summary"]["active_policy_fingerprint"]
    assert len(digest["summary"]["active_policy_fingerprint"]) == 64
    assert all(
        len(value) == 64
        for value in digest["summary"]["strategy_semantics_fingerprints"].values()
    )
    assert len(digest["summary"]["strategy_registry_sha256"]) == 64


def test_catalog_fleet_identity_is_dynamic_and_excludes_reference_policies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        paperops_fleet_telegram_service,
        "build_strategy_catalog",
        lambda: (
            SimpleNamespace(strategy_id="new_research_strategy", status="experimental"),
            SimpleNamespace(strategy_id="validated_strategy", status="validated"),
            SimpleNamespace(strategy_id="benchmark", status="benchmark"),
            SimpleNamespace(strategy_id="cash", status="baseline"),
        ),
    )

    assert _eligible_catalog_strategy_ids() == (
        "new_research_strategy",
        "validated_strategy",
    )


def test_digest_reports_registered_strategies_pending_activation_without_day_evidence(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    pending_ids = tuple(
        strategy_id for strategy_id in STRATEGIES if strategy_id.startswith("gap_up_")
    )
    assert len(pending_ids) == 2

    semantics_path = paper_root / "state" / "strategy_semantics_manifest.json"
    semantics = json.loads(semantics_path.read_text(encoding="utf-8"))
    for strategy_id in pending_ids:
        entry = semantics["strategies"][f"{strategy_id}@v1.0"]
        entry.update(
            {
                "activation_policy": "next_market_session_after_registration",
                "coverage_inception_date": "2026-07-16",
                "registered_at": f"{DAY}T21:00:00-05:00",
            }
        )
    semantics_path.write_text(
        json.dumps(semantics, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    calendar_path = paper_root / "calendar" / "strategy_daily_returns.csv"
    with calendar_path.open("r", encoding="utf-8", newline="") as handle:
        calendar_rows = list(csv.DictReader(handle))
    retained_calendar_rows = [
        row for row in calendar_rows if row["strategy_id"] not in pending_ids
    ]
    with calendar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(calendar_rows[0]))
        writer.writeheader()
        writer.writerows(retained_calendar_rows)

    decisions_path = paper_root / "exports" / f"strategy_decisions_forward_{DAY}.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions_path.write_text(
        json.dumps(
            [row for row in decisions if row["strategy_id"] not in pending_ids],
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ledger_path.write_text(
        "".join(
            json.dumps(event, sort_keys=True) + "\n"
            for event in ledger
            if event["strategy_id"] not in pending_ids
        ),
        encoding="utf-8",
    )

    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["daily_rows"] = [
        row
        for row in report["daily_rows"]
        if row.get("strategy_id") not in pending_ids
    ]
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is True
    assert digest["summary"]["registered_strategy_count"] == len(STRATEGIES)
    assert digest["summary"]["forward_strategy_count"] == len(STRATEGIES) - 2
    assert digest["summary"]["pending_activation_strategy_count"] == 2
    assert all(
        digest["summary"]["strategy_activation_by_id"][strategy_id] == {
            "coverage_inception_date": "2026-07-16",
            "status": "registered_not_yet_eligible",
        }
        for strategy_id in pending_ids
    )
    assert f"FORWARD — {len(STRATEGIES) - 2} eligible daily-swing strategies" in digest[
        "message"
    ]
    assert "REGISTERED — 2 pending activation" in digest["message"]
    assert "Gap-up continuation: registered / not yet eligible" in digest["message"]
    assert "ATR gap-up continuation: registered / not yet eligible" in digest["message"]
    assert "starts 2026-07-16 | return N/A (pending)" in digest["message"]


def test_digest_still_fails_closed_when_eligible_strategy_row_is_missing(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    missing_id = STRATEGIES[0]
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["daily_rows"] = [
        row
        for row in report["daily_rows"]
        if row.get("strategy_id") != missing_id
    ]
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert f"missing forward strategy rows: {missing_id}" in digest["blockers"]


@pytest.mark.parametrize("invalid_return", [float("nan"), float("inf"), float("-inf")])
def test_digest_blocks_non_finite_strategy_return(
    tmp_path: Path,
    invalid_return: float,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    paper_row = next(
        row for row in report["daily_rows"] if row.get("horizon") == PAPEROPS_HORIZON
    )
    paper_row["normalized_daily_return_pct"] = invalid_return
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("return evidence is missing" in value for value in digest["blockers"])
    assert "nan%" not in str(digest.get("message", "")).lower()
    assert "inf%" not in str(digest.get("message", "")).lower()


@pytest.mark.parametrize("invalid_count", [float("nan"), float("inf"), 0.5])
def test_digest_rejects_non_finite_or_fractional_lifecycle_count(
    tmp_path: Path,
    invalid_count: float,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    paper_row = next(
        row for row in report["daily_rows"] if row.get("horizon") == PAPEROPS_HORIZON
    )
    paper_row["trades_opened"] = invalid_count
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(NotificationError, match="finite whole number"):
        build_paperops_fleet_digest(
            market_date=DAY,
            db_path=db_path,
            paper_ops_root=paper_root,
            fleet_report_path=report_path,
        )


def test_digest_renders_exact_closed_trade_economics(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    strategy_id = STRATEGIES[4]
    calendar_path = paper_root / "calendar" / "strategy_daily_returns.csv"
    with calendar_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    for row in rows:
        if row["strategy_id"] == strategy_id:
            row["trades_closed"] = "1"
            row["daily_return_pct"] = "-0.000120412"
    with calendar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    for row in report["daily_rows"]:
        if row.get("strategy_id") == strategy_id:
            row["trades_closed"] = 1
            row["normalized_daily_return_pct"] = -0.0120412
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    order_id = f"order:historical:{strategy_id}:SPY"
    position_id = f"position:{order_id}"
    order_payload = {
        "order_id": order_id,
        "pick_id": f"pick:historical:{strategy_id}:SPY",
        "run_id": "paper_ops:forward:2026-07-14:snapshot",
        "mode": "forward",
        "trade_date": "2026-07-14",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "SPY",
        "direction": "short",
        "order_status": "pending",
        "expected_fill_rule": "daily signal fills no earlier than next valid bar open",
        "earliest_fill_date": "2026-07-14",
        "entry": 100.0,
        "stop": 105.0,
        "target": 90.0,
        "quantity": 2,
        "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
    }
    fill_payload = {
        "fill_id": f"fill:{order_id}",
        "order_id": order_id,
        "run_id": "paper_ops:forward:2026-07-14:snapshot",
        "mode": "forward",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "SPY",
        "fill_time": "2026-07-14T13:30:00+00:00",
        "fill_price": 100.0,
        "quantity": 2,
        "fee": 0.02,
        "slippage": 0.10005002501250715,
        "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
    }
    position_payload = {
        "position_id": position_id,
        "order_id": order_id,
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "SPY",
        "direction": "short",
        "status": "open",
        "opened_at": "2026-07-14T13:30:00+00:00",
        "quantity": 2,
        "entry_price": 100.0,
        "stop": 105.0,
        "target": 90.0,
        "last_mark_price": 100.0,
        "entry_fee": 0.02,
        "realized_pnl": 0.0,
        "unrealized_pnl": -0.02,
        "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
    }
    close_payload = {
        "close_id": f"close:{position_id}",
        "position_id": position_id,
        "run_id": f"paper_ops:forward:{DAY}:snapshot",
        "mode": "forward",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "SPY",
        "close_time": f"{DAY}T20:00:00+00:00",
        "close_price": 106.0,
        "close_reason": "stop",
        "gross_pnl": -12.0,
        "net_pnl": -12.0412,
        "r_multiple": -1.1867915965590627,
        "fee": 0.0212,
        "slippage": 0.1059470264867457,
        "entry_fee": 0.02,
        "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
    }
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
        for event in (
            _ledger_event(
                strategy_id,
                "paper_order_created",
                order_payload,
                event_id=f"event:historical-close-order:{strategy_id}",
                trade_date="2026-07-14",
            ),
            _ledger_event(
                strategy_id,
                "paper_fill",
                fill_payload,
                event_id=f"event:historical-close-fill:{strategy_id}",
                trade_date="2026-07-14",
            ),
            _ledger_event(
                strategy_id,
                "paper_position_opened",
                position_payload,
                event_id=f"event:historical-close-open:{strategy_id}",
                trade_date="2026-07-14",
            ),
            _ledger_event(
                strategy_id,
                "paper_position_closed",
                close_payload,
                event_id=f"event:close:{strategy_id}",
            ),
        ):
            handle.write(json.dumps(event, sort_keys=True) + "\n")

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is True
    assert (
        "SPY SHORT | fill $100.00 x 2 | stop $105.00 / target $90.00 | "
        "closed stop @ $106.00 | after-cost net $-12.04"
    ) in digest["message"]
    close_detail = next(
        detail
        for detail in digest["lifecycle_details"]
        if detail["strategy_id"] == strategy_id and detail["kind"] == "closed"
    )
    assert close_detail["net_pnl_after_costs"] == -12.0412
    assert close_detail["close_reason"] == "stop"


@pytest.mark.parametrize(
    ("mutation", "expected_blocker"),
    [
        ("payload_symbol", "close symbol does not match opened position"),
        ("event_symbol", "close symbol does not match opened position"),
        ("payload_run", "close payload run does not match calendar run"),
        ("event_run", "run identity mismatch"),
        ("direction", "close direction contradicts opened position"),
        ("gross_pnl", "close gross P&L does not match canonical recomputation"),
        ("net_pnl", "close after-cost net P&L does not match canonical recomputation"),
        ("r_multiple", "close R-multiple does not match canonical recomputation"),
        ("fee", "close exit fee does not match canonical recomputation"),
        ("slippage", "close slippage does not match canonical recomputation"),
        ("entry_fee", "close entry fee does not match canonical recomputation"),
        ("close_reason", "close reason is not a supported engine outcome"),
    ],
)
def test_digest_blocks_mutated_close_lineage_or_economics(
    tmp_path: Path,
    mutation: str,
    expected_blocker: str,
) -> None:
    paper_root, report_path, db_path, close_event_id = _write_mutable_closed_fixture(
        tmp_path
    )
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    close_event = next(event for event in ledger if event["event_id"] == close_event_id)
    close_payload = close_event["payload"]
    if mutation == "payload_symbol":
        close_payload["symbol"] = "QQQ"
    elif mutation == "event_symbol":
        close_event["symbol"] = "QQQ"
    elif mutation == "payload_run":
        close_payload["run_id"] = "paper_ops:forward:2026-07-15:wrong"
    elif mutation == "event_run":
        close_event["run_id"] = "paper_ops:forward:2026-07-15:wrong"
    elif mutation == "direction":
        close_payload["direction"] = "short"
    elif mutation == "close_reason":
        close_payload["close_reason"] = "manual"
    else:
        close_payload[mutation] = float(close_payload[mutation]) + 1.0
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(expected_blocker in value for value in digest["blockers"])


def test_digest_blocks_coherent_entry_fee_corruption_across_all_artifacts(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path, close_event_id = _write_mutable_closed_fixture(
        tmp_path
    )
    strategy_id = STRATEGIES[0]
    order_id = f"order:prior:{strategy_id}:SPY"
    position_id = f"position:{order_id}"
    corrupt_entry_fee = 1.0405
    gross_pnl = -5.0
    exit_fee = 0.04
    corrupt_net_pnl = gross_pnl - corrupt_entry_fee - exit_fee
    stop_fill = 95.0 * (1.0 - 5.0 / 10_000.0)
    corrupt_risk = (
        max(0.0, -((stop_fill - 101.25) * 4))
        + corrupt_entry_fee
        + stop_fill * 4 * 1.0 / 10_000.0
    )

    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    for event in ledger:
        payload = event["payload"]
        if event["event_type"] == "paper_fill" and payload.get("order_id") == order_id:
            payload["fee"] = corrupt_entry_fee
        if payload.get("position_id") == position_id:
            if event["event_type"] in {
                "paper_position_opened",
                "paper_position_checked_no_action",
            }:
                payload["entry_fee"] = corrupt_entry_fee
            elif event["event_id"] == close_event_id:
                payload["entry_fee"] = corrupt_entry_fee
                payload["net_pnl"] = corrupt_net_pnl
                payload["r_multiple"] = corrupt_net_pnl / corrupt_risk
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    calendar_path = paper_root / "calendar" / "strategy_daily_returns.csv"
    with calendar_path.open("r", encoding="utf-8", newline="") as handle:
        calendar_rows = list(csv.DictReader(handle))
    for row in calendar_rows:
        if row["strategy_id"] == strategy_id:
            row["daily_return_pct"] = str(corrupt_net_pnl / 100_000.0)
    with calendar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(calendar_rows[0]))
        writer.writeheader()
        writer.writerows(calendar_rows)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    for row in report["daily_rows"]:
        if row.get("strategy_id") == strategy_id:
            row["normalized_daily_return_pct"] = corrupt_net_pnl / 100_000.0 * 100.0
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(
        "originating fill fee does not match canonical recomputation" in value
        for value in digest["blockers"]
    )


def test_digest_blocks_originating_fill_slippage_corruption(tmp_path: Path) -> None:
    paper_root, report_path, db_path, _ = _write_mutable_closed_fixture(tmp_path)
    strategy_id = STRATEGIES[0]
    order_id = f"order:prior:{strategy_id}:SPY"
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
    ]
    fill_event = next(
        event
        for event in ledger
        if event["event_type"] == "paper_fill"
        and event["payload"].get("order_id") == order_id
    )
    fill_event["payload"]["slippage"] = (
        float(fill_event["payload"]["slippage"]) + 1.0
    )
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(
        "originating fill slippage does not match canonical recomputation" in value
        for value in digest["blockers"]
    )


def test_stale_same_day_artifacts_block_after_live_registry_version_rollover(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    registry_path = paper_root / "state" / "strategy_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry[0]["strategy_version"] = "v2.0"
    registry_path.write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("live strategy registry version" in value for value in digest["blockers"])


def test_stale_same_day_artifacts_block_after_active_policy_rollover(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    rolled_policy = "paperops_daily_next_open_risk_v3"
    config_path = paper_root / "state" / "paper_ops_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["execution_policy_version"] = rolled_policy
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    _write_live_lineage(paper_root, execution_policy_version=rolled_policy)

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("live registry execution policy" in value for value in digest["blockers"])


def test_direct_digest_blocks_same_policy_config_semantic_drift(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    config_path = paper_root / "state" / "paper_ops_config.json"
    config = json.loads(config_path.read_text(encoding="utf-8"))
    config["fee_bps"] = 9.0
    config_path.write_text(
        json.dumps(config, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(
        "current PaperOps config semantics do not match" in value
        for value in digest["blockers"]
    )
    assert any(
        "fingerprint does not match current PaperOps config semantics" in value
        for value in digest["blockers"]
    )


def test_digest_blocks_unbounded_policy_fingerprint(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    manifest_path = paper_root / "state" / "execution_policy_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["policies"][PAPER_EXECUTION_POLICY_VERSION]["fingerprint"] = (
        "paper_execution_policy:not-bounded"
    )
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("not bounded SHA-256" in value for value in digest["blockers"])


def test_digest_blocks_coherent_stale_strategy_code_semantics(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    strategy_id = STRATEGIES[0]
    manifest_path = paper_root / "state" / "strategy_semantics_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    entry = manifest["strategies"][f"{strategy_id}@v1.0"]
    entry["configuration"]["entry_logic"] += " stale-code-edit"
    stale_fingerprint = hashlib.sha256(
        json.dumps(
            entry["configuration"], sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    entry["fingerprint"] = stale_fingerprint
    manifest_path.write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    registry_path = paper_root / "state" / "strategy_registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    for row in registry:
        if row["strategy_id"] == strategy_id:
            row["strategy_semantics_fingerprint"] = stale_fingerprint
    registry_path.write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    decisions_path = paper_root / "exports" / f"strategy_decisions_forward_{DAY}.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    for row in decisions:
        if row["strategy_id"] == strategy_id:
            row["strategy_semantics_fingerprint"] = stale_fingerprint
    decisions_path.write_text(
        json.dumps(decisions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    for event in ledger:
        if event["strategy_id"] == strategy_id:
            event["payload"]["strategy_semantics_fingerprint"] = stale_fingerprint
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(
        "current implementation semantics do not match manifest" in value
        for value in digest["blockers"]
    )
    assert any(
        "registry semantics fingerprint does not match current code" in value
        for value in digest["blockers"]
    )


@pytest.mark.parametrize("artifact", ["decision", "ledger_event"])
def test_digest_blocks_stale_artifact_strategy_semantics_fingerprint(
    tmp_path: Path,
    artifact: str,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    if artifact == "decision":
        path = paper_root / "exports" / f"strategy_decisions_forward_{DAY}.json"
        rows = json.loads(path.read_text(encoding="utf-8"))
        rows[0]["strategy_semantics_fingerprint"] = "0" * 64
        path.write_text(
            json.dumps(rows, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
    else:
        path = paper_root / "ledger" / "paper_ledger.jsonl"
        rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        same_day = next(event for event in rows if event["trade_date"] == DAY)
        same_day["payload"]["strategy_semantics_fingerprint"] = "0" * 64
        path.write_text(
            "".join(json.dumps(event, sort_keys=True) + "\n" for event in rows),
            encoding="utf-8",
        )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("strategy semantics mismatch" in value for value in digest["blockers"])


def test_digest_blocks_same_day_lifecycle_from_different_run(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    same_day = next(event for event in ledger if event["trade_date"] == DAY)
    same_day["run_id"] = "paper_ops:forward:2026-07-15:different_snapshot"
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("run identity mismatch" in value for value in digest["blockers"])


def test_digest_blocks_accepted_pick_without_exact_order_resolution(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [json.loads(line) for line in ledger_path.read_text(encoding="utf-8").splitlines()]
    missing_pick_id = f"pick:forward:{DAY}:{STRATEGIES[3]}:SPY"
    retained = [
        event
        for event in ledger
        if event.get("payload", {}).get("pick_id") != missing_pick_id
    ]
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in retained),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(
        missing_pick_id in value and "0 exact order resolution events" in value
        for value in digest["blockers"]
    )


def test_digest_event_key_ignores_unrelated_replay_ledger_append(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    first = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                {
                    "event_id": "unrelated:replay:append",
                    "event_type": "paper_position_marked_to_market",
                    "mode": "replay",
                    "payload": {"strategy_id": STRATEGIES[0], "symbol": "SPY"},
                    "run_id": "paper_ops:replay:2099-01-01:unrelated",
                    "strategy_id": STRATEGIES[0],
                    "symbol": "SPY",
                    "trade_date": "2099-01-01",
                },
                sort_keys=True,
            )
            + "\n"
        )
    second = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert first["ready"] is True
    assert second["ready"] is True
    assert second["event_key"] == first["event_key"]


def test_digest_turns_incomplete_ledger_tail_into_blocker(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8", newline="") as handle:
        handle.write('{"event_id":')

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("incomplete JSONL tail" in value for value in digest["blockers"])


def test_digest_blocks_scalar_record_in_canonical_ledger(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write("[]\n")

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("must be a JSON object" in value for value in digest["blockers"])


def test_digest_blocks_blank_and_globally_duplicate_event_ids(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ledger[0]["event_id"] = ""
    duplicate = json.loads(json.dumps(ledger[1]))
    duplicate["event_id"] = ledger[1]["event_id"]
    duplicate["strategy_id"] = STRATEGIES[1]
    duplicate["payload"]["strategy_id"] = STRATEGIES[1]
    duplicate["payload"]["strategy_semantics_fingerprint"] = (
        _current_semantic_fingerprints()[STRATEGIES[1]]
    )
    if duplicate["payload"].get("order_id"):
        duplicate["payload"]["order_id"] = (
            str(duplicate["payload"]["order_id"]) + ":distinct-entity"
        )
    ledger.append(duplicate)
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("blank event_id" in value for value in digest["blockers"])
    assert any("duplicate event_id" in value for value in digest["blockers"])


@pytest.mark.parametrize(
    ("mutation", "blocker_text"),
    (
        ({"mode": "replay"}, "mode is not forward"),
        ({"trade_date": "2099-01-01"}, "date does not match"),
        ({"strategy_id": "foreign_strategy"}, "unknown strategy"),
    ),
)
def test_digest_blocks_every_foreign_row_in_day_decision_artifact(
    tmp_path: Path,
    mutation: dict[str, str],
    blocker_text: str,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    decisions_path = paper_root / "exports" / f"strategy_decisions_forward_{DAY}.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions[0].update(mutation)
    decisions_path.write_text(
        json.dumps(decisions, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(blocker_text in value for value in digest["blockers"])


def test_digest_blocks_scalar_decision_row_and_foreign_same_day_lifecycle(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    decisions_path = paper_root / "exports" / f"strategy_decisions_forward_{DAY}.json"
    decisions = json.loads(decisions_path.read_text(encoding="utf-8"))
    decisions.append("scalar-contamination")
    decisions_path.write_text(
        json.dumps(decisions, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    foreign = json.loads(
        json.dumps(
            next(
                event
                for event in ledger
                if event["trade_date"] == DAY
                and event["event_type"] == "paper_order_created"
            )
        )
    )
    foreign["event_id"] = "foreign:same-day:lifecycle"
    foreign["strategy_id"] = "foreign_strategy"
    foreign["payload"]["strategy_id"] = "foreign_strategy"
    ledger.append(foreign)
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any("decision row" in value for value in digest["blockers"])
    assert any(
        "ledger lifecycle references unknown strategy" in value
        for value in digest["blockers"]
    )


def test_digest_blocks_ledger_envelope_payload_lineage_mismatch(tmp_path: Path) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    ledger = [
        json.loads(line)
        for line in ledger_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    ledger[0]["symbol"] = "FOREIGN"
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger),
        encoding="utf-8",
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert any(
        "envelope/payload symbol mismatch" in value for value in digest["blockers"]
    )


@pytest.mark.parametrize(
    "event_type",
    ("paper_order_pending_no_fill_data", "paper_order_blocked"),
)
def test_ledger_lineage_accepts_explicit_cross_day_order_lifecycle(
    tmp_path: Path,
    event_type: str,
) -> None:
    origin_run_id = "paper_ops:forward:2026-07-14:origin"
    lifecycle_run_id = f"paper_ops:forward:{DAY}:lifecycle"
    row = {
        "event_id": f"cross-day:{event_type}",
        "event_type": event_type,
        "mode": "forward",
        "payload": {
            "lifecycle_run_id": lifecycle_run_id,
            "mode": "forward",
            "origin_run_id": origin_run_id,
            "run_id": origin_run_id,
            "strategy_id": STRATEGIES[0],
            "symbol": "SPY",
            "trade_date": "2026-07-14",
        },
        "run_id": lifecycle_run_id,
        "strategy_id": STRATEGIES[0],
        "symbol": "SPY",
        "trade_date": DAY,
    }
    ledger_path = tmp_path / "paper_ledger.jsonl"
    ledger_path.write_text(json.dumps(row, sort_keys=True) + "\n", encoding="utf-8")
    blockers: list[str] = []

    assert _load_ledger_events(ledger_path, blockers) == [row]
    assert blockers == []


def test_telegram_digest_caps_lifecycle_and_references_full_artifact() -> None:
    message, displayed, truncated = _capped_digest_message(
        prefix=["header"],
        lifecycle_lines=[f"• lifecycle {index} " + ("x" * 120) for index in range(100)],
        suffix=["footer"],
        artifact_reference="PaperOps/notifications/full.json",
    )

    assert len(message) <= 3900
    assert displayed < 100
    assert truncated is True
    assert "Full exact lifecycle artifact: PaperOps/notifications/full.json" in message


def test_direct_cli_recovers_pending_paper_transaction_before_reads(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    journal_path = paper_root / "state" / "paper_transaction_pending.json"
    journal_contents = {"events": [], "state_updates": {}}
    transaction_id = hashlib.sha256(
        json.dumps(
            journal_contents,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()
    journal_path.write_text(
        json.dumps(
            {
                "schema_version": "v2.paper_transaction.v1",
                "transaction_id": transaction_id,
                **journal_contents,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )

    exit_code = cli_main(
        [
            "strategy-fleet-telegram",
            "--date",
            DAY,
            "--db-path",
            str(db_path),
            "--paper-ops-root",
            str(paper_root),
            "--fleet-report",
            str(report_path),
            "--notify",
            "console",
            "--max-attempts",
            "1",
        ]
    )

    assert exit_code == 0
    assert not journal_path.exists()


def test_direct_cli_blocks_independent_calendar_truth_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    calendar_path = paper_root / "calendar" / "strategy_daily_returns.csv"
    with calendar_path.open("r", encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    rows[0]["daily_return_pct"] = "0.5"
    with calendar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report_row = next(
        row
        for row in report["daily_rows"]
        if row.get("strategy_id") == rows[0]["strategy_id"]
    )
    report_row["normalized_daily_return_pct"] = 50.0
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    calls: list[Path] = []

    def failed_truth(*, output_root: Path) -> _CalendarTruthStub:
        calls.append(output_root)
        return _CalendarTruthStub(
            status="failed",
            math_mismatches=("mutated daily return mismatch",),
        )

    monkeypatch.setattr(
        "intraday_scanner.services.paperops_fleet_telegram_service.verify_calendar_truth",
        failed_truth,
    )

    exit_code = cli_main(
        [
            "strategy-fleet-telegram",
            "--date",
            DAY,
            "--db-path",
            str(db_path),
            "--paper-ops-root",
            str(paper_root),
            "--fleet-report",
            str(report_path),
            "--notify",
            "console",
            "--max-attempts",
            "1",
        ]
    )

    assert calls == [paper_root]
    assert exit_code == 1
    assert "mutated daily return mismatch" in capsys.readouterr().err
    blocked = json.loads(
        (
            paper_root
            / "notifications"
            / "paperops_fleet_digest"
            / "blocked"
            / f"{DAY}.json"
        ).read_text(encoding="utf-8")
    )
    assert any(
        "calendar truth math_mismatches: mutated daily return mismatch" in value
        for value in blocked["blockers"]
    )


def test_digest_fails_closed_when_exact_forward_decisions_are_missing(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    (paper_root / "exports" / f"strategy_decisions_forward_{DAY}.json").unlink()

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
    )

    assert digest["ready"] is False
    assert "Evidence INCOMPLETE" in digest["message"]
    assert any("decisions" in reason for reason in digest["blockers"])
    with pytest.raises(NotificationError, match="digest blocked"):
        send_paperops_fleet_digest(
            market_date=DAY,
            db_path=db_path,
            paper_ops_root=paper_root,
            fleet_report_path=report_path,
            notify="console",
            max_attempts=1,
            retry_delay_seconds=0,
        )
    assert SQLiteScanStore(db_path).load_recent_notifications() == []
    assert (
        paper_root
        / "notifications"
        / "paperops_fleet_digest"
        / "blocked"
        / f"{DAY}.json"
    ).is_file()


def test_failed_delivery_stays_in_outbox_then_retries_and_dedupes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)

    class FailingTelegram:
        channel = "telegram"

        def send(self, event: NotificationEvent) -> None:
            raise NotificationError(
                "simulated Telegram outage "
                "https://api.telegram.org/bot123456:SecretToken/sendMessage "
                "TELEGRAM_CHAT_ID=-123"
            )

    monkeypatch.setattr(
        "intraday_scanner.services.paperops_fleet_telegram_service.build_notifiers",
        lambda config: [FailingTelegram()],
    )
    with pytest.raises(NotificationError, match="simulated Telegram outage"):
        send_paperops_fleet_digest(
            market_date=DAY,
            db_path=db_path,
            paper_ops_root=paper_root,
            fleet_report_path=report_path,
            max_attempts=1,
            retry_delay_seconds=0,
        )

    outbox_files = list(
        (paper_root / "notifications" / "paperops_fleet_digest" / "outbox").glob(
            "*.json"
        )
    )
    assert len(outbox_files) == 1
    failed = json.loads(outbox_files[0].read_text(encoding="utf-8"))
    assert failed["status"] == "delivery_failed"
    assert failed["attempt_count"] == 1
    assert "simulated Telegram outage" in failed["last_error"]
    assert "SecretToken" not in outbox_files[0].read_text(encoding="utf-8")
    assert "TELEGRAM_CHAT_ID=-123" not in outbox_files[0].read_text(encoding="utf-8")
    assert SQLiteScanStore(db_path).load_recent_notifications() == []

    class SuccessfulTelegram:
        channel = "telegram"

        def send(self, event: NotificationEvent) -> None:
            assert "Research/paper only" in event.body

    monkeypatch.setattr(
        "intraday_scanner.services.paperops_fleet_telegram_service.build_notifiers",
        lambda config: [SuccessfulTelegram()],
    )
    delivered = send_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
        max_attempts=1,
        retry_delay_seconds=0,
    )
    duplicate = send_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
        max_attempts=1,
        retry_delay_seconds=0,
    )

    assert delivered["notification_stats"] == {"sent": 1, "skipped": 0}
    assert delivered["attempt_count"] == 2
    lifecycle_artifact = Path(delivered["lifecycle_artifact_path"])
    assert lifecycle_artifact.is_file()
    assert json.loads(lifecycle_artifact.read_text(encoding="utf-8"))[
        "lifecycle_details"
    ]
    assert duplicate["notification_stats"] == {"sent": 0, "skipped": 1}
    assert duplicate["attempt_count"] == 2
    final = json.loads(outbox_files[0].read_text(encoding="utf-8"))
    assert final["status"] == "delivered"
    assert final["last_error"] is None
    assert len(SQLiteScanStore(db_path).load_recent_notifications()) == 1


def test_cli_and_eod_chain_gate_digest_after_verified_fleet() -> None:
    parsed = build_arg_parser().parse_args(
        [
            "strategy-fleet-telegram",
            "--date",
            DAY,
            "--notify",
            "console",
        ]
    )
    assert parsed.command == "strategy-fleet-telegram"
    assert parsed.max_attempts == 3

    batch = Path("scripts/run_alphaops_eod_full.bat").read_text(encoding="utf-8")
    digest_command = "intraday_scanner.cli strategy-fleet-telegram"
    assert digest_command in batch
    assert batch.index("paper_ops verify-calendar") < batch.index(digest_command)
    shadow_run = batch.index("paper_ops shadow-run")
    post_reconcile = batch.index("paper_ops reconcile", shadow_run)
    post_verify = batch.index("paper_ops verify-calendar", post_reconcile)
    post_rebuild = batch.index("paper_ops rebuild-ledger", post_verify)
    blotter = batch.index("paper_ops blotter", post_rebuild)
    verify_blotter = batch.index("paper_ops verify-blotter", blotter)
    challenger_evaluate = batch.index("paper_ops challenger-evaluate", verify_blotter)
    assert (
        shadow_run
        < post_reconcile
        < post_verify
        < post_rebuild
        < blotter
        < verify_blotter
        < challenger_evaluate
    )
    assert "set PAPEROPS_SHADOW_ATTEMPTED=1" in batch[:shadow_run]
    assert 'if "%PAPEROPS_SHADOW_ATTEMPTED%"=="1"' in batch[shadow_run:post_reconcile]
    assert 'if "%POST_SHADOW_TRUTH_OK%"=="1"' in batch
    assert 'if not "%POST_SHADOW_TRUTH_OK%"=="1" set PAPEROPS_DIGEST_READY=0' in batch
    assert 'if not "%PAPEROPS_BLOTTER_OK%"=="1" set PAPEROPS_DIGEST_READY=0' in batch
    init_failure = batch.index("Shadow challenger registry initialization failed.")
    shadow_failure = batch.index("Frozen shadow challenger execution failed.")
    assert "set PAPEROPS_VERIFY_OK=0" in batch[init_failure:shadow_run]
    assert "set POST_SHADOW_TRUTH_OK=0" in batch[init_failure:shadow_run]
    assert "set PAPEROPS_VERIFY_OK=0" in batch[shadow_failure:post_reconcile]
    assert "set POST_SHADOW_TRUTH_OK=0" in batch[shadow_failure:post_reconcile]
    assert batch.index("strategy-fleet-report") < batch.index(digest_command)
    assert 'if "%PAPEROPS_DIGEST_READY%"=="1"' in batch
    assert "durable outbox remains available for retry" in batch
    assert "set EXITCODE=1" in batch[batch.index(digest_command) :]


def test_sourced_zero_selection_alpha_day_sends_complete_paperops_digest(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path, contract_path = _write_optional_alpha_fixture(
        tmp_path
    )

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
        alpha_run_contract_path=contract_path,
    )
    delivered = send_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
        alpha_run_contract_path=contract_path,
        notify="console",
        max_attempts=1,
        retry_delay_seconds=0,
    )

    assert digest["ready"] is True
    assert digest["summary"]["alpha_optional"] is True
    assert (
        digest["summary"]["alpha_status"]
        == "sourced_no_signal_scorecard_unavailable"
    )
    assert "Source-complete no_signal run; official scorecard unavailable" in digest["message"]
    assert "Return N/A (not 0%)." in digest["message"]
    assert "Alpha: sourced no_signal; scorecard N/A" in digest["message"]
    assert delivered["notification_stats"] == {"sent": 1, "skipped": 0}


def test_missing_alpha_truth_is_explicit_and_never_masquerades_as_no_pick(
    tmp_path: Path,
) -> None:
    paper_root, report_path, db_path, contract_path = _write_optional_alpha_fixture(
        tmp_path
    )
    contract_path.unlink()

    digest = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=db_path,
        paper_ops_root=paper_root,
        fleet_report_path=report_path,
        alpha_run_contract_path=contract_path,
    )

    assert digest["ready"] is False
    assert digest["status"] == "blocked_incomplete_evidence"
    assert any("not interpreted as no-pick" in value for value in digest["blockers"])
    assert "Evidence INCOMPLETE" in digest["message"]
    assert "Source-complete no_signal" not in digest["message"]
    assert "No triggered/closed official paper trade" not in digest["message"]


def test_partial_or_conflicting_alpha_truth_stays_blocked(tmp_path: Path) -> None:
    partial_root, partial_report_path, partial_db = _write_digest_fixture(
        tmp_path / "partial"
    )
    partial_report = json.loads(partial_report_path.read_text(encoding="utf-8"))
    partial_report["status"] = "partial"
    partial_report["sources"][ALPHAOPS_SOURCE]["status"] = "partial"
    partial_report["warnings"] = ["AlphaOps has an incomplete daily scorecard."]
    alpha_row = next(
        row
        for row in partial_report["daily_rows"]
        if row.get("source_system") == ALPHAOPS_SOURCE
    )
    alpha_row["evidence_status"] = "partial"
    partial_report_path.write_text(
        json.dumps(partial_report, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    partial = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=partial_db,
        paper_ops_root=partial_root,
        fleet_report_path=partial_report_path,
    )

    conflict_root, conflict_report, conflict_db, conflict_contract = (
        _write_optional_alpha_fixture(tmp_path / "conflict", conflicting_selection=True)
    )
    conflicting = build_paperops_fleet_digest(
        market_date=DAY,
        db_path=conflict_db,
        paper_ops_root=conflict_root,
        fleet_report_path=conflict_report,
        alpha_run_contract_path=conflict_contract,
    )

    assert partial["ready"] is False
    assert any("reconciliation is partial" in value for value in partial["blockers"])
    assert conflicting["ready"] is False
    assert any("conflicts with selected-signal" in value for value in conflicting["blockers"])


def _write_optional_alpha_fixture(
    tmp_path: Path,
    *,
    conflicting_selection: bool = False,
) -> tuple[Path, Path, Path, Path]:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    contract_path = tmp_path / "alpha_run_contract.json"
    store = SQLiteScanStore(db_path)
    store.initialize()
    scan_id = "alpha-valid-no-edge"
    selection = {
        "selection_id": "selection:conflict" if conflicting_selection else "selection:no-trade",
        "scan_id": scan_id,
        "signal_id": (
            f"{scan_id}:1:ABC"
            if conflicting_selection
            else f"no_trade:{scan_id}:{DAY}"
        ),
        "ticker": "ABC" if conflicting_selection else "NO_TRADE",
        "rank": 1,
        "strategy_id": "alphaops_v4",
        "strategy_version": "dawnstrike-alphaops-v4",
        "cohort": "official_telegram",
        "decision": "selected" if conflicting_selection else "no_trade",
        "selected_at": f"{DAY}T13:10:00+00:00",
        "event_key": f"alphaops:{scan_id}:alpha_no_trade",
        "body_sha256": "a" * 64,
    }
    store.persist_signal_selections([selection])
    contract = {
        "alertable_count": 0,
        "broker_execution": "disabled",
        "enrichment_status": "complete",
        "market_date": DAY,
        "model_version": "dawnstrike-alphaops-v4",
        "notification_channel": "telegram",
        "notification_dry_run": False,
        "notification_status": "delivery_recorded",
        "primary_veto": "no clean edge",
        "producer": "alphaops",
        "producer_run_id": scan_id,
        "ranked_count": 3,
        "research_only": True,
        "schema_version": "alphaops.run_contract.v1",
        "selection_outcome": "valid_no_edge",
        "signal_count": 0,
        "source_status": "success",
    }
    contract_path.write_text(
        json.dumps(contract, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["status"] = "partial"
    report["warnings"] = [
        "AlphaOps scorecard table yielded no rows in the requested range."
    ]
    report["sources"][ALPHAOPS_SOURCE] = {
        "status": "empty",
        "path": str(db_path),
        "row_count": 0,
    }
    report["daily_rows"] = [
        row
        for row in report["daily_rows"]
        if row.get("source_system") != ALPHAOPS_SOURCE
    ]
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return paper_root, report_path, db_path, contract_path


def _write_digest_fixture(tmp_path: Path) -> tuple[Path, Path, Path]:
    paper_root = tmp_path / "paper_ops_live"
    db_path = tmp_path / "alpha.sqlite"
    calendar_path = paper_root / "calendar" / "strategy_daily_returns.csv"
    decisions_path = paper_root / "exports" / f"strategy_decisions_forward_{DAY}.json"
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    config_path = paper_root / "state" / "paper_ops_config.json"
    report_path = tmp_path / "strategy_fleet_report.json"
    calendar_path.parent.mkdir(parents=True)
    decisions_path.parent.mkdir(parents=True)
    ledger_path.parent.mkdir(parents=True)
    config_path.parent.mkdir(parents=True)
    SQLiteScanStore(db_path).initialize()

    calendar_rows: list[dict[str, Any]] = []
    paper_rows: list[dict[str, Any]] = []
    decisions: list[dict[str, Any]] = []
    ledger_events: list[dict[str, Any]] = []
    semantic_fingerprints = _current_semantic_fingerprints()
    for index, strategy_id in enumerate(STRATEGIES):
        lifecycle = {
            "trades_opened": 1 if index == 0 else 0,
            "trades_closed": 0,
            "pending_orders": 1 if index == 1 else 0,
            "open_positions": 1 if index in {0, 2} else 0,
        }
        calendar_rows.append(
            {
                "date": DAY,
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "execution_policy_version": "paperops_daily_next_open_risk_v2",
                "strategy_semantics_fingerprint": semantic_fingerprints[strategy_id],
                "run_id": f"paper_ops:forward:{DAY}:snapshot",
                "daily_return_pct": -0.0015 if index == 2 else 0.0,
                **lifecycle,
            }
        )
        paper_rows.append(
            {
                "date": DAY,
                "source_system": PAPEROPS_SOURCE,
                "horizon": PAPEROPS_HORIZON,
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "execution_policy_version": "paperops_daily_next_open_risk_v2",
                "strategy_status": "experimental",
                "cohort": "paper_forward",
                "return_observed": True,
                "normalized_daily_return_pct": -0.15 if index == 2 else 0.0,
                "trades_opened": lifecycle["trades_opened"],
                "trades_closed": lifecycle["trades_closed"],
                "benchmark_return_pct": 0.4,
                "cash_return_pct": 0.0,
                "source_run_id": f"paper_ops:forward:{DAY}:snapshot",
            }
        )
        status = "accepted" if index in {0, 1, 3} else "no_setup"
        decision_date_field = "trade_date" if status == "accepted" else "market_date"
        pick_id = f"pick:forward:{DAY}:{strategy_id}:SPY"
        decisions.append(
            {
                decision_date_field: DAY,
                "mode": "forward",
                "pick_id": pick_id,
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "execution_policy_version": "paperops_daily_next_open_risk_v2",
                "strategy_semantics_fingerprint": semantic_fingerprints[strategy_id],
                "run_id": f"paper_ops:forward:{DAY}:snapshot",
                "symbol": "SPY",
                "direction": "long" if status == "accepted" else "",
                "entry_reference": 100.0 if status == "accepted" else None,
                "stop": 95.0 if status == "accepted" else None,
                "target": 110.0 if status == "accepted" else None,
                "decision_status": status,
                "trade_return_eligible": status == "accepted",
                "trade_return_pct": None,
            }
        )

        if index == 0:
            order_id = f"order:prior:{strategy_id}:SPY"
            position_id = f"position:{order_id}"
            order_payload = {
                "order_id": order_id,
                "pick_id": f"pick:prior:{strategy_id}:SPY",
                "run_id": "paper_ops:forward:2026-07-14:snapshot",
                "mode": "forward",
                "trade_date": "2026-07-14",
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "symbol": "SPY",
                "direction": "long",
                "order_status": "pending",
                "expected_fill_rule": (
                    "daily signal fills no earlier than next valid bar open"
                ),
                "earliest_fill_date": DAY,
                "entry": 100.0,
                "stop": 95.0,
                "target": 110.0,
                "quantity": 4,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            }
            fill_payload = {
                "fill_id": f"fill:{order_id}",
                "order_id": order_id,
                "run_id": f"paper_ops:forward:{DAY}:snapshot",
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "symbol": "SPY",
                "fill_time": f"{DAY}T13:30:00+00:00",
                "fill_price": 101.25,
                "quantity": 4,
                "fee": 0.0405,
                "slippage": 0.20239880059966708,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            }
            position_payload = {
                "position_id": position_id,
                "order_id": order_id,
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "symbol": "SPY",
                "direction": "long",
                "status": "open",
                "opened_at": f"{DAY}T13:30:00+00:00",
                "quantity": 4,
                "entry_price": 101.25,
                "stop": 95.0,
                "target": 110.0,
                "last_mark_price": 102.0,
                "entry_fee": 0.0405,
                "realized_pnl": 0.0,
                "unrealized_pnl": 2.96,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            }
            ledger_events.extend(
                [
                    _ledger_event(
                        strategy_id,
                        "paper_order_created",
                        order_payload,
                        event_id=f"event:historical-order:{strategy_id}",
                        trade_date="2026-07-14",
                    ),
                    _ledger_event(
                        strategy_id,
                        "paper_fill",
                        fill_payload,
                        event_id=f"event:fill:{strategy_id}",
                    ),
                    _ledger_event(
                        strategy_id,
                        "paper_position_opened",
                        position_payload,
                        event_id=f"event:open:{strategy_id}",
                    ),
                    _ledger_event(
                        strategy_id,
                        "paper_position_checked_no_action",
                        position_payload,
                        event_id=f"event:check:{strategy_id}",
                    ),
                    _ledger_event(
                        strategy_id,
                        "paper_order_blocked",
                        {
                            **order_payload,
                            "order_id": f"order:{DAY}:{strategy_id}:SPY",
                            "pick_id": pick_id,
                            "run_id": f"paper_ops:forward:{DAY}:snapshot",
                            "trade_date": DAY,
                            "lifecycle_run_id": f"paper_ops:forward:{DAY}:snapshot",
                            "origin_run_id": f"paper_ops:forward:{DAY}:snapshot",
                            "reason": "max_concurrent_positions",
                        },
                        event_id=f"event:blocked:{strategy_id}",
                    ),
                ]
            )
        elif index == 1:
            order_payload = {
                "order_id": f"order:{DAY}:{strategy_id}:SPY",
                "pick_id": pick_id,
                "run_id": f"paper_ops:forward:{DAY}:snapshot",
                "mode": "forward",
                "trade_date": DAY,
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "symbol": "SPY",
                "direction": "long",
                "order_status": "pending",
                "expected_fill_rule": (
                    "daily signal fills no earlier than next valid bar open"
                ),
                "signal_time": f"{DAY}T20:00:00+00:00",
                "earliest_fill_date": "2026-07-16",
                "entry": 100.0,
                "stop": 95.0,
                "target": 110.0,
                "risk_per_unit": 5.0,
                "risk_budget": 500.0,
                "quantity": 100,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            }
            ledger_events.append(
                _ledger_event(
                    strategy_id,
                    "paper_order_created",
                    order_payload,
                    event_id=f"event:order:{strategy_id}",
                )
            )
        elif index == 2:
            order_id = f"order:prior:{strategy_id}:SPY"
            order_payload = {
                "order_id": order_id,
                "pick_id": f"pick:prior:{strategy_id}:SPY",
                "run_id": "paper_ops:forward:2026-07-14:snapshot",
                "mode": "forward",
                "trade_date": "2026-07-14",
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "symbol": "SPY",
                "direction": "long",
                "order_status": "pending",
                "expected_fill_rule": (
                    "daily signal fills no earlier than next valid bar open"
                ),
                "earliest_fill_date": "2026-07-14",
                "entry": 100.0,
                "stop": 96.0,
                "target": 112.0,
                "quantity": 5,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            }
            fill_payload = {
                "fill_id": f"fill:{order_id}",
                "order_id": order_id,
                "run_id": "paper_ops:forward:2026-07-14:snapshot",
                "mode": "forward",
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "symbol": "SPY",
                "fill_time": "2026-07-14T13:30:00+00:00",
                "fill_price": 100.5,
                "quantity": 5,
                "fee": 0.05025,
                "slippage": 0.2511244377810584,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            }
            position_payload = {
                "position_id": f"position:{order_id}",
                "order_id": order_id,
                "strategy_id": strategy_id,
                "strategy_version": "v1.0",
                "symbol": "SPY",
                "direction": "long",
                "status": "open",
                "opened_at": "2026-07-14T13:30:00+00:00",
                "quantity": 5,
                "entry_price": 100.5,
                "stop": 96.0,
                "target": 112.0,
                "last_mark_price": 99.0,
                "entry_fee": 0.05025,
                "realized_pnl": 0.0,
                "unrealized_pnl": -7.55,
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
            }
            ledger_events.extend(
                [
                    _ledger_event(
                        strategy_id,
                        "paper_order_created",
                        order_payload,
                        event_id=f"event:historical-order:{strategy_id}",
                        trade_date="2026-07-14",
                    ),
                    _ledger_event(
                        strategy_id,
                        "paper_fill",
                        fill_payload,
                        event_id=f"event:historical-fill:{strategy_id}",
                        trade_date="2026-07-14",
                    ),
                    _ledger_event(
                        strategy_id,
                        "paper_position_opened",
                        position_payload,
                        event_id=f"event:historical-open:{strategy_id}",
                        trade_date="2026-07-14",
                    ),
                    _ledger_event(
                        strategy_id,
                        "paper_position_marked_to_market",
                        position_payload,
                        event_id=f"event:mark:{strategy_id}",
                    ),
                ]
            )
        elif index == 3:
            ledger_events.append(
                _ledger_event(
                    strategy_id,
                    "paper_order_blocked",
                    {
                        "order_id": f"order:{DAY}:{strategy_id}:SPY",
                        "pick_id": pick_id,
                        "run_id": f"paper_ops:forward:{DAY}:snapshot",
                        "mode": "forward",
                        "trade_date": DAY,
                        "lifecycle_run_id": f"paper_ops:forward:{DAY}:snapshot",
                        "origin_run_id": f"paper_ops:forward:{DAY}:snapshot",
                        "strategy_id": strategy_id,
                        "strategy_version": "v1.0",
                        "symbol": "SPY",
                        "direction": "long",
                        "expected_fill_rule": (
                            "daily signal fills no earlier than next valid bar open"
                        ),
                        "earliest_fill_date": "2026-07-16",
                        "entry": 100.0,
                        "stop": 95.0,
                        "target": 110.0,
                        "quantity": 100,
                        "reason": "max_concurrent_positions",
                        "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
                    },
                    event_id=f"event:blocked:{strategy_id}",
                )
            )

    with calendar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(calendar_rows[0]))
        writer.writeheader()
        writer.writerows(calendar_rows)
    decisions_path.write_text(
        json.dumps(decisions, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    ledger_path.write_text(
        "".join(json.dumps(event, sort_keys=True) + "\n" for event in ledger_events),
        encoding="utf-8",
    )
    config_path.write_text(
        json.dumps(
            {
                "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
                "universe_symbols": ["SPY"],
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    _write_live_lineage(
        paper_root,
        execution_policy_version=PAPER_EXECUTION_POLICY_VERSION,
    )
    alpha_row = {
        "date": DAY,
        "source_system": ALPHAOPS_SOURCE,
        "horizon": ALPHAOPS_HORIZON,
        "mode": "reconciled",
        "strategy_id": "alphaops_v4",
        "strategy_version": "dawnstrike-alphaops-v4",
        "execution_policy_version": "alphaops_intraday_first_touch_v1",
        "strategy_status": "paper_research",
        "cohort": "official_telegram",
        "evidence_status": "complete",
        "return_observed": False,
        "normalized_daily_return_pct": None,
        "trades_opened": 0,
        "trades_closed": 0,
        "unresolved_count": 0,
        "benchmark_return_pct": 0.2,
    }
    report = {
        "schema_version": "dawnstrike.strategy_fleet_report.v3",
        "status": "complete",
        "warnings": [],
        "date_range": {"start": DAY, "end": DAY},
        "sources": {
            ALPHAOPS_SOURCE: {
                "status": "complete",
                "path": str(db_path),
                "row_count": 1,
            },
            PAPEROPS_SOURCE: {
                "status": "complete",
                "path": str(calendar_path),
                "expected_strategy_ids": list(STRATEGIES),
                "excluded_non_forward_rows": 21,
            },
        },
        "daily_rows": [alpha_row, *paper_rows],
        "strategy_summaries": [],
    }
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    return paper_root, report_path, db_path


def _write_mutable_closed_fixture(
    tmp_path: Path,
) -> tuple[Path, Path, Path, str]:
    paper_root, report_path, db_path = _write_digest_fixture(tmp_path)
    strategy_id = STRATEGIES[0]
    calendar_path = paper_root / "calendar" / "strategy_daily_returns.csv"
    with calendar_path.open("r", encoding="utf-8", newline="") as handle:
        calendar_rows = list(csv.DictReader(handle))
    for row in calendar_rows:
        if row["strategy_id"] == strategy_id:
            row["trades_closed"] = "1"
            row["open_positions"] = "0"
            row["daily_return_pct"] = "-0.000050805"
    with calendar_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=tuple(calendar_rows[0]))
        writer.writeheader()
        writer.writerows(calendar_rows)

    report = json.loads(report_path.read_text(encoding="utf-8"))
    for row in report["daily_rows"]:
        if row.get("strategy_id") == strategy_id:
            row["trades_closed"] = 1
            row["normalized_daily_return_pct"] = -0.0050805
    report_path.write_text(
        json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )

    position_id = f"position:order:prior:{strategy_id}:SPY"
    close_event_id = f"event:mutable-close:{strategy_id}"
    close_payload = {
        "close_id": f"close:{position_id}",
        "position_id": position_id,
        "run_id": f"paper_ops:forward:{DAY}:snapshot",
        "mode": "forward",
        "strategy_id": strategy_id,
        "strategy_version": "v1.0",
        "symbol": "SPY",
        "close_time": f"{DAY}T20:00:00+00:00",
        "close_price": 100.0,
        "close_reason": "stop",
        "gross_pnl": -5.0,
        "net_pnl": -5.0805,
        "r_multiple": -0.20106076024118744,
        "fee": 0.04,
        "slippage": 0.2001000500250143,
        "entry_fee": 0.0405,
        "execution_policy_version": PAPER_EXECUTION_POLICY_VERSION,
    }
    ledger_path = paper_root / "ledger" / "paper_ledger.jsonl"
    with ledger_path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(
            json.dumps(
                _ledger_event(
                    strategy_id,
                    "paper_position_closed",
                    close_payload,
                    event_id=close_event_id,
                ),
                sort_keys=True,
            )
            + "\n"
        )
    return paper_root, report_path, db_path, close_event_id


def _ledger_event(
    strategy_id: str,
    event_type: str,
    payload: dict[str, Any],
    *,
    event_id: str,
    trade_date: str = DAY,
) -> dict[str, Any]:
    exact_payload = {
        **payload,
        "strategy_semantics_fingerprint": _current_semantic_fingerprints()[strategy_id],
    }
    return {
        "event_id": event_id,
        "event_type": event_type,
        "run_id": f"paper_ops:forward:{trade_date}:snapshot",
        "mode": "forward",
        "trade_date": trade_date,
        "strategy_id": strategy_id,
        "symbol": exact_payload.get("symbol"),
        "payload": exact_payload,
        "schema_version": "v2.paper_ledger_event.v1",
    }


@lru_cache(maxsize=1)
def _current_semantic_fingerprints() -> dict[str, str]:
    return {
        strategy.strategy_id: _strategy_semantics_fingerprint(strategy)
        for strategy in build_strategy_catalog()
    }


def _write_live_lineage(
    paper_root: Path,
    *,
    execution_policy_version: str,
) -> None:
    state = paper_root / "state"
    state.mkdir(parents=True, exist_ok=True)
    catalog_by_id = {
        strategy.strategy_id: strategy for strategy in build_strategy_catalog()
    }
    registry = [
        {
            "allow_entries": True,
            "execution_policy_version": execution_policy_version,
            "paper_status": "eligible",
            "schema_version": "v2.paper_strategy_config.v2",
            "strategy_id": strategy_id,
            "strategy_status": "experimental",
            "strategy_version": "v1.0",
            "strategy_semantics_fingerprint": _strategy_semantics_fingerprint(
                catalog_by_id[strategy_id]
            ),
        }
        for strategy_id in STRATEGIES
    ]
    config_payload = json.loads(
        (state / "paper_ops_config.json").read_text(encoding="utf-8")
    )
    configuration = _execution_policy_fingerprint_payload(
        _config_from_payload(config_payload)
    )
    fingerprint = hashlib.sha256(
        json.dumps(configuration, sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()
    manifest = {
        "active_execution_policy_version": execution_policy_version,
        "policies": {
            execution_policy_version: {
                "configuration": configuration,
                "fingerprint": fingerprint,
                "registered_at": f"{DAY}T00:00:00+00:00",
            }
        },
        "schema_version": "v2.paper_execution_policy_manifest.v1",
    }
    (state / "strategy_registry.json").write_text(
        json.dumps(registry, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    semantics_manifest = {
        "schema_version": "v2.strategy_semantics_manifest.v1",
        "strategies": {
            f"{strategy_id}@v1.0": {
                "configuration": _strategy_semantics_payload(
                    catalog_by_id[strategy_id]
                ),
                "fingerprint": _strategy_semantics_fingerprint(
                    catalog_by_id[strategy_id]
                ),
                "registered_at": f"{DAY}T00:00:00+00:00",
            }
            for strategy_id in STRATEGIES
        },
    }
    (state / "strategy_semantics_manifest.json").write_text(
        json.dumps(semantics_manifest, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    (state / "execution_policy_manifest.json").write_text(
        json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
