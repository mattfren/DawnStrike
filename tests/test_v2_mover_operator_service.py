from __future__ import annotations

import csv
import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from intraday_scanner import mover_pattern_operator_cli as mover_main
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent
from intraday_scanner.services.mover_pattern_operator_service import (
    MoverWorkflowInputs,
    run_mover_daily_workflow,
)
from intraday_scanner.v2.mover_pattern_lab import core as mover_core

BAR_FIELDS = ("symbol", "timestamp", "open", "high", "low", "close", "volume")


class RecordingTelegramNotifier(BaseNotifier):
    channel = "telegram"

    def __init__(self) -> None:
        self.events: list[NotificationEvent] = []

    def send(self, event: NotificationEvent) -> dict[str, Any]:
        self.events.append(event)
        encoded = event.body.encode("utf-8")
        return {
            "transport": "telegram",
            "transmitted_text": event.body,
            "transmitted_byte_count": len(encoded),
            "transmitted_bytes_sha256": hashlib.sha256(encoded).hexdigest(),
            "message_id": len(self.events),
            "telegram_response": {
                "ok": True,
                "result": {"message_id": len(self.events)},
            },
        }


class SecretFailingTelegramNotifier(BaseNotifier):
    channel = "telegram"

    def send(self, event: NotificationEvent) -> None:
        raise RuntimeError(
            "simulated https://api.telegram.org/bot123456:SecretToken/sendMessage "
            "telegram_chat_id=-123"
        )


class FakeRetainedInputAdapter:
    def __init__(
        self,
        *,
        scan_bars: Path,
        context: Path,
        reconciliation_bars: Path,
    ) -> None:
        self.scan_bars = scan_bars
        self.context = context
        self.reconciliation_bars = reconciliation_bars

    def scan_inputs(self, *, market_date: str, cutoff_et: str) -> MoverWorkflowInputs:
        assert market_date == "2026-07-20"
        assert cutoff_et == "09:45"
        return MoverWorkflowInputs(
            bars_csv=self.scan_bars,
            context_csv=self.context,
        )

    def reconciliation_inputs(self, *, market_date: str) -> MoverWorkflowInputs:
        assert market_date == "2026-07-20"
        return MoverWorkflowInputs(bars_csv=self.reconciliation_bars)


class FlexibleRetainedInputAdapter:
    def __init__(self, *, bars: Path, context: Path | None = None) -> None:
        self.bars = bars
        self.context = context or bars

    def scan_inputs(self, *, market_date: str, cutoff_et: str) -> MoverWorkflowInputs:
        return MoverWorkflowInputs(bars_csv=self.bars, context_csv=self.context)

    def reconciliation_inputs(self, *, market_date: str) -> MoverWorkflowInputs:
        return MoverWorkflowInputs(bars_csv=self.bars)


def _write_csv(path: Path, fields: tuple[str, ...], rows: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _session_bars(
    market_date: str,
    *,
    current: bool,
    symbol: str = "ABC",
    close_time: str = "16:00:00",
    utc_offset: str = "-04:00",
) -> list[dict[str, Any]]:
    timestamp = datetime.fromisoformat(f"{market_date}T09:35:00{utc_offset}")
    close_at = datetime.fromisoformat(f"{market_date}T{close_time}{utc_offset}")
    rows: list[dict[str, Any]] = []
    index = 0
    while timestamp <= close_at:
        if not current:
            open_price = close_price = 10.0
            volume = 50_000
        elif index == 0:
            open_price, close_price, volume = 10.8, 11.0, 200_000
        elif index == 1:
            open_price, close_price, volume = 11.0, 11.1, 200_000
        elif index == 2:
            open_price, close_price, volume = 11.1, 11.2, 200_000
        else:
            open_price = 11.2 + (index - 3) * 0.02
            close_price = open_price + 0.02
            volume = 100_000
        rows.append(
            {
                "symbol": symbol,
                "timestamp": timestamp.isoformat(),
                "open": round(open_price, 4),
                "high": round(max(open_price, close_price) + 0.05, 4),
                "low": round(min(open_price, close_price) - 0.05, 4),
                "close": round(close_price, 4),
                "volume": volume,
            }
        )
        timestamp += timedelta(minutes=5)
        index += 1
    return rows


def _write_config(tmp_path: Path) -> Path:
    config_path = tmp_path / "mover_daily.json"
    config_path.write_text(
        json.dumps(
            {
                "schema_version": "dawnstrike.mover_daily_workflow.v1",
                "input_adapter": "retained_csv",
                "bars_csv_template": "inputs/{market_date}/bars_{cutoff_token}.csv",
                "context_csv_template": "inputs/{market_date}/context_{cutoff_token}.csv",
                "reconciliation_bars_csv_template": (
                    "inputs/{market_date}/bars_after_close.csv"
                ),
                "cutoffs_et": ["09:45"],
                "reconcile_not_before_et": "16:10",
                "output_root": "lab",
                "notification_db_path": "notifications.sqlite",
                "env_file": ".env",
                "notification_channel": "telegram",
                "min_baseline_sessions": 1,
                "bar_interval_minutes": 5,
                "notional_per_trade": 1000.0,
                "slippage_bps": 10.0,
                "fee_bps": 1.0,
                "research_only": True,
                "broker_execution_enabled": False,
            },
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    return config_path


def _write_forward_inputs(
    tmp_path: Path,
    *,
    symbols: tuple[str, ...] = ("ABC",),
) -> FakeRetainedInputAdapter:
    prior = [
        row
        for symbol in symbols
        for row in _session_bars("2026-07-17", current=False, symbol=symbol)
    ]
    current = [
        row
        for symbol in symbols
        for row in _session_bars("2026-07-20", current=True, symbol=symbol)
    ]
    cutoff_bars = [
        row
        for row in current
        if str(row["timestamp"]) <= "2026-07-20T09:45:00-04:00"
    ]
    scan_bars = tmp_path / "scan_bars.csv"
    reconciliation_bars = tmp_path / "reconciliation_bars.csv"
    _write_csv(scan_bars, BAR_FIELDS, prior + cutoff_bars)
    _write_csv(reconciliation_bars, BAR_FIELDS, prior + current)

    universe_artifact = tmp_path / "universe.json"
    universe_payload = {
        "schema_version": "v2.mover_candidate_universe.v1",
        "market_date": "2026-07-20",
        "feature_cutoff_at": "2026-07-20T09:45:00-04:00",
        "system_received_at": "2026-07-20T08:30:00-04:00",
        "evidence_mode": "forward_observation",
        "universe_selection_method": "scheduled_universe",
        "expected_symbols": list(symbols),
        "expected_symbols_complete": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    universe_artifact.write_text(
        json.dumps(universe_payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    digest = hashlib.sha256(
        json.dumps(
            universe_payload, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    universe_ref = f"sha256:{digest}:{universe_artifact.resolve()}"
    context = tmp_path / "context.csv"
    context_rows = [
        {
            "market_date": "2026-07-20",
            "symbol": symbol,
            "context_observed_at": "2026-07-20T09:44:00-04:00",
            "universe_selected_at": "2026-07-20T08:30:00-04:00",
            "universe_source_ref": universe_ref,
            "universe_selection_method": "scheduled_universe",
            "spread_pct": 0.5,
            "split_adjusted": True,
            "reverse_split_days": 180,
            "reverse_split_lookback_clear": True,
            "recent_offering_days": 60,
            "offering_lookback_clear": True,
            "halt_state": "clear",
            "source_conflict": False,
            "catalyst_verified": False,
            "catalyst_published_at": "",
            "catalyst_source_url": "",
            "catalyst_source_type": "",
            "catalyst_artifact_ref": "",
            "source_refs": universe_ref,
        }
        for symbol in symbols
    ]
    _write_csv(context, tuple(context_rows[0]), context_rows)
    return FakeRetainedInputAdapter(
        scan_bars=scan_bars,
        context=context,
        reconciliation_bars=reconciliation_bars,
    )


def test_daily_operator_capture_scan_reconcile_calendar_and_notification(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path)
    notifier = RecordingTelegramNotifier()
    scan_now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: scan_now)

    scan = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=scan_now,
    )

    assert scan["status"] == "passed", json.dumps(scan, indent=2, default=str)
    assert scan["signal_count"] == 1
    snapshot = json.loads(Path(scan["snapshots_path"]).read_text(encoding="utf-8"))
    assert snapshot["evidence_mode"] == "forward_observation"
    assert snapshot["system_received_at"] == scan_now.isoformat()
    assert snapshot["source_captured_at"] == scan_now.isoformat()
    assert snapshot["forward_receipt_ref"] in snapshot["source_refs"]
    assert "source-validated forward paper signal" in notifier.events[-1].body
    assert "No order was placed" in notifier.events[-1].body

    duplicate = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=scan_now,
    )
    assert duplicate["status"] == "passed"
    assert duplicate["notification"]["status"] == "duplicate_suppressed"
    assert duplicate["notification"]["duplicate_suppressed_count"] == 1
    assert len(notifier.events) == 1

    reconcile_now = datetime.fromisoformat("2026-07-20T16:10:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: reconcile_now)
    reconciliation = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[notifier],
        now=reconcile_now,
    )

    assert reconciliation["status"] == "passed", json.dumps(reconciliation, indent=2)
    assert reconciliation["closed_trade_count"] == 1
    assert reconciliation["verification_status"] == "passed"
    assert reconciliation["analysis_series_mode"] == (
        "cumulative_compatible_daily_runs"
    )
    assert reconciliation["included_run_pair_count"] == 1
    assert Path(reconciliation["analysis_path"]).is_file()
    assert Path(reconciliation["calendar_path"]).is_file()
    assert Path(reconciliation["calendar_html_path"]).is_file()
    assert "after-cost" in notifier.events[-1].body
    assert "Research/paper evidence only" in notifier.events[-1].body
    assert reconciliation["broker_execution_enabled"] is False
    assert len(notifier.events) == 2


def test_daily_operator_notifies_explicit_fail_closed_input_reason(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    missing = tmp_path / "missing.csv"
    adapter = FakeRetainedInputAdapter(
        scan_bars=missing,
        context=missing,
        reconciliation_bars=missing,
    )
    notifier = RecordingTelegramNotifier()

    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=datetime.fromisoformat("2026-07-20T09:45:30-04:00"),
    )

    assert result["status"] == "blocked"
    assert "cutoff bars CSV is missing" in result["blockers"][0]
    assert result["notification"]["status"] == "delivered"
    assert "scan blocked" in notifier.events[0].body
    assert "No paper return was inferred" in notifier.events[0].body
    assert "No order was placed" in notifier.events[0].body
    assert result["broker_execution_enabled"] is False


def test_daily_operator_outbox_redacts_notification_secrets(tmp_path: Path) -> None:
    config_path = _write_config(tmp_path)
    missing = tmp_path / "missing.csv"
    adapter = FakeRetainedInputAdapter(
        scan_bars=missing,
        context=missing,
        reconciliation_bars=missing,
    )

    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[SecretFailingTelegramNotifier()],
        now=datetime.fromisoformat("2026-07-20T09:45:30-04:00"),
    )

    assert result["status"] == "blocked"
    assert result["notification"]["status"] == "delivery_unknown"
    serialized = json.dumps(result, sort_keys=True)
    serialized += Path(result["notification"]["outbox_path"]).read_text(
        encoding="utf-8"
    )
    assert "SecretToken" not in serialized
    assert "telegram_chat_id=-123" not in serialized
    assert "<redacted>" in serialized


def test_scan_workflow_survives_unknown_telegram_delivery_and_reconciles(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path)
    scan_now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: scan_now)

    scan = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[SecretFailingTelegramNotifier()],
        now=scan_now,
    )

    assert scan["status"] == "passed"
    assert scan["workflow_status"] == "passed"
    assert scan["notification_status"] == "delivery_unknown"
    assert scan["notification"]["memberships"][0]["automatic_retry_allowed"] is False

    retry_notifier = RecordingTelegramNotifier()
    suppressed = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[retry_notifier],
        now=scan_now,
    )
    assert suppressed["status"] == "passed"
    assert suppressed["notification_status"] == "duplicate_suppressed"
    assert suppressed["notification"]["memberships"][0][
        "original_delivery_status"
    ] == "delivery_unknown"
    assert retry_notifier.events == []

    reconcile_now = datetime.fromisoformat("2026-07-20T16:10:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: reconcile_now)
    reconciliation = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[retry_notifier],
        now=reconcile_now,
    )
    assert reconciliation["status"] == "passed", json.dumps(reconciliation, indent=2)
    assert reconciliation["closed_trade_count"] == 1
    assert len(retry_notifier.events) == 1


def test_telegram_attempt_is_durable_before_transport_call(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path)
    outbox_root = tmp_path / "lab" / "operator" / "notifications" / "outbox"

    class InspectingTimeoutNotifier(BaseNotifier):
        channel = "telegram"

        def send(self, event: NotificationEvent) -> None:
            outboxes = list(outbox_root.glob("*.json"))
            assert len(outboxes) == 1
            attempt = json.loads(outboxes[0].read_text(encoding="utf-8"))
            assert attempt["status"] == "attempting"
            assert attempt["automatic_retry_allowed"] is False
            raise TimeoutError("uncertain transport timeout")

    now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: now)
    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[InspectingTimeoutNotifier()],
        now=now,
    )

    assert result["status"] == "passed"
    assert result["notification_status"] == "delivery_unknown"
    membership = result["notification"]["memberships"][0]
    assert membership["automatic_retry_allowed"] is False
    assert json.loads(Path(membership["outbox_path"]).read_text(encoding="utf-8"))[
        "status"
    ] == "delivery_unknown"


@pytest.mark.parametrize(
    ("field", "tampered"),
    [
        ("schema_version", "wrong.schema"),
        ("market_date", "2026-07-21"),
        ("cutoff_et", "10:00"),
        ("config_fingerprint", "0" * 64),
        ("signals_sha256", "f" * 64),
    ],
)
def test_reconciliation_rejects_tampered_mutable_scan_state(
    tmp_path: Path,
    monkeypatch: Any,
    field: str,
    tampered: str,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path)
    notifier = RecordingTelegramNotifier()
    scan_now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: scan_now)
    scan = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=scan_now,
    )
    state_path = Path(scan["operator_state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state[field] = tampered
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[notifier],
        now=datetime.fromisoformat("2026-07-20T16:10:30-04:00"),
    )
    assert result["status"] == "blocked"
    assert any("immutable validation" in reason for reason in result["blockers"])


def test_terminal_rerun_rejects_operator_receipt_outside_canonical_root(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path)
    notifier = RecordingTelegramNotifier()
    scan_now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: scan_now)
    scan = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=scan_now,
    )
    assert scan["status"] == "passed"

    receipt_path = Path(scan["operator_receipt_path"])
    relocated = tmp_path / "relocated_operator_receipt.json"
    relocated.write_bytes(receipt_path.read_bytes())
    state_path = Path(scan["operator_state_path"])
    state = json.loads(state_path.read_text(encoding="utf-8"))
    content_sha = str(scan["operator_receipt_content_sha256"])
    state["operator_receipt_path"] = str(relocated.resolve())
    state["operator_receipt_ref"] = f"sha256:{content_sha}:{relocated.resolve()}"
    state_path.write_text(json.dumps(state, sort_keys=True), encoding="utf-8")

    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=scan_now,
    )

    assert result["status"] == "blocked"
    assert any("outside its canonical path" in reason for reason in result["blockers"])


def test_reconciliation_rejects_hash_changed_scan_artifact(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path)
    notifier = RecordingTelegramNotifier()
    scan_now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: scan_now)
    scan = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=scan_now,
    )
    signals_path = Path(scan["signals_path"])
    signals_path.write_text(
        signals_path.read_text(encoding="utf-8") + " \n",
        encoding="utf-8",
    )

    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[notifier],
        now=datetime.fromisoformat("2026-07-20T16:10:30-04:00"),
    )
    assert result["status"] == "blocked"
    assert any("artifact hash mismatch" in reason for reason in result["blockers"])


def test_reconciliation_honors_published_early_close_and_retains_receipt(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    bars = tmp_path / "early_close_bars.csv"
    rows = _session_bars(
        "2026-11-25",
        current=False,
        utc_offset="-05:00",
    ) + _session_bars(
        "2026-11-27",
        current=True,
        close_time="13:00:00",
        utc_offset="-05:00",
    )
    _write_csv(bars, BAR_FIELDS, rows)
    adapter = FlexibleRetainedInputAdapter(bars=bars)
    notifier = RecordingTelegramNotifier()

    too_early = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-11-27",
        input_adapter=adapter,
        notifiers=[notifier],
        now=datetime.fromisoformat("2026-11-27T13:09:59-05:00"),
    )
    assert too_early["status"] == "not_applicable_yet"
    assert too_early["notification_status"] == "not_applicable"
    assert too_early["notification"]["event_count"] == 0
    assert "published official close" in too_early["reason"]
    assert notifier.events == []

    accepted_receipt = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-11-27",
        input_adapter=adapter,
        notifiers=[notifier],
        now=datetime.fromisoformat("2026-11-27T13:10:00-05:00"),
    )
    assert accepted_receipt["status"] == "blocked"  # No corresponding scan state.
    assert accepted_receipt["published_close_at"] == "2026-11-27T13:00:00-05:00"
    assert accepted_receipt["reconcile_not_before_at"] == "2026-11-27T13:10:00-05:00"
    assert Path(accepted_receipt["reconciliation_receipt_path"]).is_file()
    assert accepted_receipt["reconciliation_receipt_ref"].startswith("sha256:")


def test_reconciliation_rejects_bar_after_authoritative_system_receipt(
    tmp_path: Path,
) -> None:
    config_path = _write_config(tmp_path)
    rows = _session_bars("2026-07-17", current=False) + _session_bars(
        "2026-07-20", current=True
    )
    future = dict(rows[-1])
    future["timestamp"] = "2026-07-20T16:15:00-04:00"
    rows.append(future)
    bars = tmp_path / "future_bars.csv"
    _write_csv(bars, BAR_FIELDS, rows)

    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=FlexibleRetainedInputAdapter(bars=bars),
        notifiers=[RecordingTelegramNotifier()],
        now=datetime.fromisoformat("2026-07-20T16:10:00-04:00"),
    )
    assert result["status"] == "blocked"
    assert "after authoritative system receipt" in result["blockers"][0]


def test_notification_membership_matches_every_retained_signal(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path, symbols=("ABC", "XYZ"))
    notifier = RecordingTelegramNotifier()
    now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: now)

    result = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=now,
    )
    signals = [
        json.loads(line)
        for line in Path(result["signals_path"]).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    memberships = [
        row["delivery_membership"] for row in result["notification"]["memberships"]
    ]
    assert result["signal_count"] == 2
    assert len(notifier.events) == len(signals) == len(memberships)
    assert {row["signal_id"] for row in signals} == {
        row["signal_id"] for row in memberships
    }
    assert all(row["measured_cohort_member"] is True for row in memberships)
    assert all(len(event.body) <= 850 for event in notifier.events)

    reconcile_now = datetime.fromisoformat("2026-07-20T16:10:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: reconcile_now)
    reconciliation = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[notifier],
        now=reconcile_now,
    )
    outcome_memberships = [
        row["delivery_membership"]
        for row in reconciliation["notification"]["memberships"]
    ]
    assert reconciliation["status"] == "passed"
    assert reconciliation["closed_trade_count"] == 2
    assert {row["signal_id"] for row in signals} == {
        row["signal_id"] for row in outcome_memberships
    }
    assert all(row["trade_status"] == "closed" for row in outcome_memberships)
    assert len(notifier.events) == 4


def test_pending_reconciliation_is_retryable_then_closes_once(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    config_path = _write_config(tmp_path)
    adapter = _write_forward_inputs(tmp_path)
    notifier = RecordingTelegramNotifier()
    scan_now = datetime.fromisoformat("2026-07-20T09:45:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: scan_now)
    scan = run_mover_daily_workflow(
        config_path=config_path,
        stage="scan",
        market_date="2026-07-20",
        cutoff_et="09:45",
        input_adapter=adapter,
        notifiers=[notifier],
        now=scan_now,
    )
    assert scan["status"] == "passed"

    partial_rows = _session_bars("2026-07-17", current=False)
    partial_rows.extend(
        row
        for row in _session_bars("2026-07-20", current=True)
        if str(row["timestamp"]) <= "2026-07-20T09:45:00-04:00"
    )
    close_witness = dict(partial_rows[-1])
    close_witness.update(
        {
            "symbol": "XYZ",
            "timestamp": "2026-07-20T16:00:00-04:00",
            "open": 20.0,
            "high": 20.1,
            "low": 19.9,
            "close": 20.0,
            "volume": 100_000,
        }
    )
    partial_rows.append(close_witness)
    partial_path = tmp_path / "partial_reconciliation.csv"
    _write_csv(partial_path, BAR_FIELDS, partial_rows)
    adapter.reconciliation_bars = partial_path
    reconcile_now = datetime.fromisoformat("2026-07-20T16:10:30-04:00")
    monkeypatch.setattr(mover_core, "_utc_now", lambda: reconcile_now)

    pending = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[notifier],
        now=reconcile_now,
    )
    assert pending["status"] == "incomplete_pending"
    assert pending["pending_trade_count"] == 1
    first_receipt = pending["operator_receipt_ref"]

    adapter.reconciliation_bars = tmp_path / "reconciliation_bars.csv"
    closed = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[notifier],
        now=reconcile_now,
    )
    assert closed["status"] == "passed", json.dumps(closed, indent=2)
    assert closed["closed_trade_count"] == 1
    assert closed["pending_trade_count"] == 0
    assert first_receipt in closed["prior_operator_attempt_receipt_refs"]
    assert closed["operator_attempt_receipt_refs"][-1] == closed["operator_receipt_ref"]
    assert closed["notification"]["event_count"] == 1
    assert closed["notification"]["memberships"][0]["delivery_membership"][
        "trade_status"
    ] == "closed"

    event_count = len(notifier.events)
    duplicate = run_mover_daily_workflow(
        config_path=config_path,
        stage="reconcile",
        market_date="2026-07-20",
        input_adapter=adapter,
        notifiers=[notifier],
        now=reconcile_now,
    )
    assert duplicate["notification_status"] == "duplicate_suppressed"
    assert len(notifier.events) == event_count


def test_daily_run_cli_exits_nonzero_for_incomplete_pending(
    monkeypatch: Any,
    tmp_path: Path,
) -> None:
    config = tmp_path / "config.json"
    config.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(
        mover_main,
        "run_mover_daily_workflow",
        lambda **kwargs: {"status": "incomplete_pending"},
    )
    assert (
        mover_main.main(
            [
                "--config",
                str(config),
                "--stage",
                "reconcile",
            ]
        )
        == 2
    )
