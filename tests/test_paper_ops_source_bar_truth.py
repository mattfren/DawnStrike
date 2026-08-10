from __future__ import annotations

import csv
import hashlib
import json
import shutil
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from intraday_scanner.v2.data import MarketBar, MarketDataset, write_ohlcv_csv
from intraday_scanner.v2.data_truth import build_data_truth_snapshot, load_datatruth_snapshot
from intraday_scanner.v2.data_truth.models import DataTruthManifest
from intraday_scanner.v2.paper_ops import engine as paper_ops_engine
from intraday_scanner.v2.paper_ops.engine import init
from intraday_scanner.v2.paper_ops.ledger_rebuild import CALENDAR_FIELDNAMES
from intraday_scanner.v2.paper_ops.models import (
    PaperClose,
    PaperCloseReason,
    PaperFill,
    PaperLedgerEvent,
    PaperOpsConfig,
    PaperOpsManifest,
    PaperOrder,
    PaperOrderStatus,
    PaperPosition,
    PaperPositionStatus,
    PaperRun,
    PaperRunMode,
    stable_id,
)
from intraday_scanner.v2.paper_ops.source_bar_truth import (
    _expected_close,
    verify_source_bar_truth,
)
from intraday_scanner.v2.paper_ops.storage import write_json, write_jsonl


def _write_canonical_calendar_evidence(
    root: Path,
    snapshots: dict[str, DataTruthManifest],
) -> None:
    rows: list[dict[str, object]] = []
    for run_date, manifest in sorted(snapshots.items()):
        dataset, _ = load_datatruth_snapshot(manifest.snapshot_id, root / "data_truth_replay")
        run = PaperRun(
            run_id=_run_id(date.fromisoformat(run_date)),
            mode=PaperRunMode.REPLAY,
            run_date=run_date,
            data_snapshot_id=manifest.snapshot_id,
            created_at="2026-01-01T00:00:00+00:00",
        )
        reference_rows = paper_ops_engine._reference_calendar_rows(
            rows,
            run=run,
            manifest=manifest,
            dataset=dataset,
        )
        if len(reference_rows) == 2:
            rows.extend(reference_rows)
    with (root / "calendar" / "strategy_daily_returns.csv").open(
        "w", encoding="utf-8", newline=""
    ) as handle:
        writer = csv.DictWriter(handle, fieldnames=CALENDAR_FIELDNAMES)
        writer.writeheader()
        writer.writerows(
            {field: row.get(field, "") for field in CALENDAR_FIELDNAMES}
            for row in rows
        )


def _bar(
    *,
    open_price: float = 100.0,
    high: float = 105.0,
    low: float = 95.0,
    close: float = 101.0,
    day: int = 6,
) -> MarketBar:
    return MarketBar(
        symbol="SPY",
        timestamp=datetime(2026, 1, day, tzinfo=timezone.utc),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
    )


@pytest.mark.parametrize(
    ("direction", "bar", "stop", "target", "expected"),
    (
        ("long", _bar(open_price=89.0, high=92.0, low=88.0), 90.0, 110.0, ("stop", 89.0)),
        (
            "short",
            _bar(open_price=111.0, high=112.0, low=108.0),
            110.0,
            90.0,
            ("stop", 111.0),
        ),
        (
            "long",
            _bar(open_price=111.0, high=112.0, low=108.0),
            90.0,
            110.0,
            ("target", 111.0),
        ),
        ("short", _bar(open_price=89.0, high=92.0, low=88.0), 110.0, 90.0, ("target", 89.0)),
    ),
)
def test_gap_close_precedence_uses_the_source_open(
    direction: str,
    bar: MarketBar,
    stop: float,
    target: float,
    expected: tuple[str, float],
) -> None:
    assert (
        _expected_close(
            {"opened_at": "2026-01-05T00:00:00+00:00"},
            bar,
            5,
            stop=stop,
            target=target,
            direction=direction,
        )
        == expected
    )


@pytest.mark.parametrize(
    ("direction", "stop", "target"),
    (("long", 95.0, 105.0), ("short", 105.0, 95.0)),
)
def test_ambiguous_intraday_stop_and_target_touch_is_stop_first(
    direction: str,
    stop: float,
    target: float,
) -> None:
    assert _expected_close(
        {"opened_at": "2026-01-05T00:00:00+00:00"},
        _bar(),
        5,
        stop=stop,
        target=target,
        direction=direction,
    ) == ("stop", stop)


def test_timeout_uses_source_close_only_after_timeout_age() -> None:
    position = {"opened_at": "2026-01-01T00:00:00+00:00"}
    bar = _bar(open_price=100.0, high=102.0, low=98.0, close=101.25)

    assert _expected_close(
        position,
        bar,
        5,
        stop=95.0,
        target=105.0,
        direction="long",
    ) == ("timeout", 101.25)
    assert _expected_close(
        position,
        bar,
        6,
        stop=95.0,
        target=105.0,
        direction="long",
    ) == (None, None)


FEE_BPS = 1.0
SLIPPAGE_BPS = 5.0
STRATEGY_ID = "ts_momentum_sma_atr"
STRATEGY_VERSION = "1.0.0"
SEMANTICS_FINGERPRINT = "a" * 64


@dataclass
class _ImmutableLifecycleScenario:
    output_root: Path
    data_truth_root: Path
    events: list[dict[str, object]]
    snapshots_by_date: dict[str, DataTruthManifest]
    active_config: PaperOpsConfig
    lifecycle_config: PaperOpsConfig

    def persist_events(self) -> None:
        write_jsonl(self.output_root / "ledger" / "paper_ledger.jsonl", self.events)


def test_full_lifecycle_matches_exact_immutable_daily_snapshot_bytes(tmp_path: Path) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "passed"
    assert result.audited_run_count == 4
    assert result.audited_event_count == 4
    assert result.warnings == ()
    assert (
        json.loads(
            (scenario.output_root / "reconciliation" / "source_bar_truth_latest.json").read_text(
                encoding="utf-8"
            )
        )
        == result.to_dict()
    )


def test_known_shadow_manifest_coexists_with_champion_source_bar_audit(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    run_date = date(2026, 1, 7)
    _write_shadow_manifest(
        scenario,
        run_date,
        schema_version="v2.paper_ops_shadow_run.v1",
    )

    result = verify_source_bar_truth(
        output_root=scenario.output_root,
        mode=PaperRunMode.REPLAY,
    )

    assert result.status == "passed"
    assert result.audited_run_count == 4
    assert result.audited_event_count == 4
    assert result.warnings == ()


def test_unknown_conflicting_manifest_is_not_treated_as_a_shadow_artifact(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    run_date = date(2026, 1, 7)
    _write_shadow_manifest(
        scenario,
        run_date,
        schema_version="v2.paper_ops_unknown_run.v1",
    )

    result = verify_source_bar_truth(
        output_root=scenario.output_root,
        mode=PaperRunMode.REPLAY,
    )

    assert result.status == "failed"
    assert "run paper-run-2026-01-07 has conflicting PaperOps manifests" in result.warnings
    assert result.audited_event_count == 4


def test_event_source_bar_tamper_breaks_event_hash_and_retained_bar_match(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    payload = _event_payload(scenario, "paper_position_marked_to_market")
    source_bar = payload["source_bar"]
    assert isinstance(source_bar, dict)
    source_bar["close"] = 999.0
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "event event-position-marked source bar hash mismatch" in result.warnings
    assert "event event-position-marked source bar close differs from snapshot" in result.warnings


def test_event_source_bar_tamper_cannot_be_hidden_by_rehashing_event(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    payload = _event_payload(scenario, "paper_position_marked_to_market")
    source_bar = payload["source_bar"]
    assert isinstance(source_bar, dict)
    source_bar["close"] = 999.0
    payload["source_bar_sha256"] = _payload_sha256(source_bar)
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "event event-position-marked source bar hash mismatch" not in result.warnings
    assert "event event-position-marked source bar close differs from snapshot" in result.warnings


def test_retained_normalized_snapshot_tamper_invalidates_bound_run(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    mark_manifest = scenario.snapshots_by_date["2026-01-07"]
    assert mark_manifest.normalized_artifact_path
    retained_path = scenario.data_truth_root / mark_manifest.normalized_artifact_path
    retained_path.write_bytes(retained_path.read_bytes() + b"tampered-retained-bytes\n")

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert result.audited_run_count == 3
    assert result.audited_event_count == 3
    assert any(
        "run paper-run-2026-01-07 immutable snapshot failed verification: "
        "DataTruth immutable normalized artifact hash mismatch" in warning
        for warning in result.warnings
    )
    assert "event event-position-marked has no verified immutable run snapshot" in result.warnings


@pytest.mark.parametrize(
    ("field", "warning_label"),
    (
        ("fill_price", "price"),
        ("fee", "fee"),
        ("slippage", "slippage"),
    ),
)
def test_fill_price_and_cost_tamper_is_rejected(
    tmp_path: Path,
    field: str,
    warning_label: str,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    payload = _event_payload(scenario, "paper_fill")
    payload[field] = float(payload[field]) + 1.0
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert f"fill event-fill {warning_label} does not match source bar policy" in result.warnings


@pytest.mark.parametrize(
    ("field", "tampered_value", "expected_warning"),
    (
        (
            "close_reason",
            "stop",
            "close event-position-closed reason contradicts source bar precedence",
        ),
        (
            "close_price",
            0.0,
            "close event-position-closed price does not match source bar policy",
        ),
        (
            "gross_pnl",
            0.0,
            "close event-position-closed gross P&L does not match source bar policy",
        ),
        (
            "net_pnl",
            0.0,
            "close event-position-closed net P&L does not match source bar policy",
        ),
        (
            "r_multiple",
            0.0,
            "close event-position-closed R-multiple does not match source bar policy",
        ),
    ),
)
def test_close_reason_price_pnl_and_r_tamper_is_rejected(
    tmp_path: Path,
    field: str,
    tampered_value: object,
    expected_warning: str,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    payload = _event_payload(scenario, "paper_position_closed")
    payload[field] = tampered_value
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert expected_warning in result.warnings


def test_attested_event_with_missing_source_evidence_is_rejected(tmp_path: Path) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    payload = _event_payload(scenario, "paper_position_marked_to_market")
    payload.pop("source_bar")
    payload.pop("source_bar_sha256")
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "event event-position-marked has no source bar evidence" in result.warnings
    assert result.audited_event_count == 3


def test_fill_quantity_must_match_the_exact_originating_order(tmp_path: Path) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    fill = _event_payload(scenario, "paper_fill")
    fill["quantity"] = int(fill["quantity"]) + 1
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "fill event-fill quantity does not match its order" in result.warnings


def test_open_position_mutation_cannot_replace_order_lineage(tmp_path: Path) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    opened = _event_payload(scenario, "paper_position_opened")
    opened["stop"] = 96.0
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "position event-position-opened stop lineage mismatch" in result.warnings


def test_mark_cannot_bypass_a_close_triggered_by_its_exact_source_bar(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    mark_event = _event_row(scenario, "paper_position_marked_to_market")
    close_event = _event_row(scenario, "paper_position_closed")
    mark = mark_event["payload"]
    close = close_event["payload"]
    assert isinstance(mark, dict)
    assert isinstance(close, dict)
    for field in ("data_snapshot_id", "source_bar", "source_bar_sha256"):
        mark[field] = close[field]
    mark_event["run_id"] = close_event["run_id"]
    mark_event["trade_date"] = close_event["trade_date"]
    mark["last_mark_price"] = 111.0
    mark["unrealized_pnl"] = (111.0 - float(mark["entry_price"])) * int(mark["quantity"]) - float(
        mark["entry_fee"]
    )
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "mark event-position-marked bypasses a required target close" in result.warnings


def test_replay_manifest_cannot_escape_the_canonical_data_truth_root(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    escaped_root = tmp_path / "escaped_truth"
    shutil.copytree(scenario.data_truth_root, escaped_root)
    path = _manifest_path(scenario, date(2026, 1, 7))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["data_truth_root_relative"] = "../escaped_truth"
    _rewrite_manifest(path, manifest)

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "run paper-run-2026-01-07 DataTruth root is not canonical for replay" in result.warnings


@pytest.mark.parametrize(
    ("field", "tampered_value", "manifest_field"),
    (
        ("mode", "forward", "mode"),
        ("trade_date", "2026-01-06", "trade_date"),
    ),
)
def test_event_identity_must_match_its_run_manifest(
    tmp_path: Path,
    field: str,
    tampered_value: str,
    manifest_field: str,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    event = _event_row(scenario, "paper_position_marked_to_market")
    event[field] = tampered_value
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert (
        f"event event-position-marked does not match run manifest {manifest_field}"
        in result.warnings
    )


def test_historical_lifecycle_uses_its_frozen_policy_not_the_active_policy(
    tmp_path: Path,
) -> None:
    active = _execution_config()
    historical = _execution_config(
        version="paperops_historical_costs_v1",
        fee_bps=7.0,
        slippage_bps=25.0,
    )
    scenario = _immutable_lifecycle_scenario(
        tmp_path,
        active_config=active,
        lifecycle_config=historical,
    )

    result = verify_source_bar_truth(output_root=scenario.output_root)

    fill = _event_payload(scenario, "paper_fill")
    assert scenario.active_config.execution_policy_version != fill["execution_policy_version"]
    assert fill["fee"] == pytest.approx(
        float(fill["fill_price"]) * int(fill["quantity"]) * historical.fee_bps / 10_000.0
    )
    assert result.status == "passed"
    assert result.warnings == ()


def test_run_cannot_rebind_an_event_to_a_later_snapshot_containing_the_same_bar(
    tmp_path: Path,
) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    path = _manifest_path(scenario, date(2026, 1, 7))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    later = scenario.snapshots_by_date["2026-01-08"]
    manifest.update(
        {
            "data_snapshot_content_hash": later.snapshot_content_hash,
            "data_snapshot_id": later.snapshot_id,
            "data_snapshot_manifest_payload_hash": later.manifest_payload_hash,
            "data_snapshot_normalized_hash": later.normalized_artifact_hash,
            "data_snapshot_normalized_path": later.normalized_artifact_path,
        }
    )
    _rewrite_manifest(path, manifest)
    mark = _event_payload(scenario, "paper_position_marked_to_market")
    mark["data_snapshot_id"] = later.snapshot_id
    scenario.persist_events()

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert (
        "run paper-run-2026-01-07 DataTruth accepted end does not equal run date" in result.warnings
    )


def test_run_universe_binding_is_exact_membership_not_configuration_order(
    tmp_path: Path,
) -> None:
    config = _execution_config(universe_symbols=("ZZZ", "TST"))
    scenario = _immutable_lifecycle_scenario(tmp_path, active_config=config)

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert scenario.snapshots_by_date["2026-01-08"].symbols == ("TST", "ZZZ")
    assert config.universe_symbols == ("ZZZ", "TST")
    assert result.status == "passed"
    assert result.warnings == ()


def test_run_universe_binding_rejects_duplicate_manifest_symbols(tmp_path: Path) -> None:
    scenario = _immutable_lifecycle_scenario(tmp_path)
    path = _manifest_path(scenario, date(2026, 1, 7))
    manifest = json.loads(path.read_text(encoding="utf-8"))
    manifest["universe_symbols"] = ["TST", "TST"]
    _rewrite_manifest(path, manifest)

    result = verify_source_bar_truth(output_root=scenario.output_root)

    assert result.status == "failed"
    assert "run paper-run-2026-01-07 DataTruth universe binding mismatch" in result.warnings


def _immutable_lifecycle_scenario(
    tmp_path: Path,
    *,
    active_config: PaperOpsConfig | None = None,
    lifecycle_config: PaperOpsConfig | None = None,
) -> _ImmutableLifecycleScenario:
    output_root = tmp_path / "paper_ops"
    init(output_root=output_root)
    init(output_root=output_root)
    active_config = active_config or _execution_config()
    lifecycle_config = lifecycle_config or active_config
    data_truth_root = output_root / "data_truth_replay"
    source_csv, raw_dir, bars = _daily_source_fixture(
        tmp_path,
        lifecycle_config.universe_symbols,
    )
    snapshots_by_date: dict[str, DataTruthManifest] = {}
    for run_date in sorted(bars):
        result = build_data_truth_snapshot(
            as_of_date=run_date + timedelta(days=1),
            output_root=data_truth_root,
            created_at=datetime.combine(
                run_date + timedelta(days=1),
                datetime.min.time(),
                tzinfo=timezone.utc,
            ),
            source_csv=source_csv,
            raw_dir=raw_dir,
            allow_fetch=False,
        )
        snapshots_by_date[run_date.isoformat()] = result.manifest
        _write_paper_manifest(
            output_root,
            run_date,
            result.manifest,
            lifecycle_config,
        )

    write_json(
        output_root / "state" / "paper_ops_config.json",
        active_config.to_dict(),
    )
    _write_execution_policy_manifest(
        output_root,
        active_config,
        lifecycle_config,
    )
    events = _lifecycle_events(bars, snapshots_by_date, lifecycle_config)
    scenario = _ImmutableLifecycleScenario(
        output_root=output_root,
        data_truth_root=data_truth_root,
        events=events,
        snapshots_by_date=snapshots_by_date,
        active_config=active_config,
        lifecycle_config=lifecycle_config,
    )
    scenario.persist_events()
    _write_canonical_calendar_evidence(output_root, snapshots_by_date)
    return scenario


def _daily_source_fixture(
    tmp_path: Path,
    universe_symbols: tuple[str, ...],
) -> tuple[Path, Path, dict[date, MarketBar]]:
    bars = {
        date(2026, 1, 5): _daily_bar(date(2026, 1, 5), 99.0, 101.0, 98.0, 100.0),
        date(2026, 1, 6): _daily_bar(date(2026, 1, 6), 100.0, 104.0, 98.0, 102.0),
        date(2026, 1, 7): _daily_bar(date(2026, 1, 7), 102.0, 106.0, 101.0, 105.0),
        date(2026, 1, 8): _daily_bar(date(2026, 1, 8), 106.0, 112.0, 105.0, 111.0),
    }
    source_csv = tmp_path / "public_yahoo_ohlcv.csv"
    bars_by_symbol = {
        symbol: tuple(
            MarketBar(
                symbol=symbol,
                timestamp=bar.timestamp,
                open=bar.open,
                high=bar.high,
                low=bar.low,
                close=bar.close,
                volume=bar.volume,
            )
            for bar in bars.values()
        )
        for symbol in universe_symbols
    }
    write_ohlcv_csv(
        MarketDataset(
            dataset_id="paperops-source-bar-truth-fixture",
            source_kind="public_yahoo_chart",
            timeframe="1d",
            bars_by_symbol=bars_by_symbol,
        ),
        source_csv,
    )
    raw_dir = tmp_path / "provider_raw"
    raw_dir.mkdir()
    for symbol in universe_symbols:
        (raw_dir / f"{symbol.lower()}_chart.json").write_text(
            json.dumps({"chart": {"result": [{"symbol": symbol}], "error": None}}),
            encoding="utf-8",
        )
    return source_csv, raw_dir, bars


def _lifecycle_events(
    bars: dict[date, MarketBar],
    snapshots: dict[str, DataTruthManifest],
    config: PaperOpsConfig,
) -> list[dict[str, object]]:
    signal_bar = bars[date(2026, 1, 5)]
    fill_bar = bars[date(2026, 1, 6)]
    mark_bar = bars[date(2026, 1, 7)]
    close_bar = bars[date(2026, 1, 8)]
    quantity = 10
    entry_price = fill_bar.open * (1 + config.slippage_bps / 10_000.0)
    entry_fee = entry_price * quantity * config.fee_bps / 10_000.0
    entry_slippage = abs(entry_price - fill_bar.open) * quantity
    exit_price = 110.0 * (1 - config.slippage_bps / 10_000.0)
    exit_fee = exit_price * quantity * config.fee_bps / 10_000.0
    exit_slippage = abs(exit_price - 110.0) * quantity
    gross_pnl = (exit_price - entry_price) * quantity
    net_pnl = gross_pnl - entry_fee - exit_fee
    stop_fill = 95.0 * (1 - config.slippage_bps / 10_000.0)
    stop_gross = (stop_fill - entry_price) * quantity
    stop_fee = stop_fill * quantity * config.fee_bps / 10_000.0
    risk = max(0.0, -stop_gross) + entry_fee + stop_fee
    pick_id = "pick-1"
    order_id = stable_id("order", pick_id)
    position_id = stable_id("position", order_id)

    order = PaperOrder(
        order_id=order_id,
        pick_id=pick_id,
        run_id=_run_id(signal_bar.timestamp.date()),
        mode=PaperRunMode.REPLAY,
        trade_date=signal_bar.timestamp.date().isoformat(),
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        symbol="TST",
        direction="long",
        order_status=PaperOrderStatus.PENDING,
        expected_fill_rule="next_completed_session_open_plus_slippage",
        signal_time=signal_bar.timestamp.isoformat(),
        earliest_fill_date=fill_bar.timestamp.date().isoformat(),
        entry=signal_bar.close,
        stop=95.0,
        target=110.0,
        risk_per_unit=5.0,
        reward_per_unit=10.0,
        reward_risk=2.0,
        risk_budget=500.0,
        quantity=quantity,
        notional_exposure=signal_bar.close * quantity,
        max_loss_estimate=50.0,
        strategy_equity_basis=100_000.0,
        execution_policy_version=config.execution_policy_version,
        strategy_semantics_fingerprint=SEMANTICS_FINGERPRINT,
    )
    fill = PaperFill(
        fill_id=stable_id("fill", order.order_id, fill_bar.timestamp.isoformat()),
        order_id=order.order_id,
        run_id=_run_id(fill_bar.timestamp.date()),
        mode=PaperRunMode.REPLAY,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        symbol="TST",
        fill_time=fill_bar.timestamp.isoformat(),
        fill_price=entry_price,
        quantity=quantity,
        fee=entry_fee,
        slippage=entry_slippage,
        execution_policy_version=config.execution_policy_version,
        strategy_semantics_fingerprint=SEMANTICS_FINGERPRINT,
    )
    opened = PaperPosition(
        position_id=position_id,
        order_id=order.order_id,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        symbol="TST",
        direction="long",
        status=PaperPositionStatus.OPEN,
        opened_at=fill_bar.timestamp.isoformat(),
        quantity=quantity,
        entry_price=entry_price,
        stop=95.0,
        target=110.0,
        last_mark_price=entry_price,
        entry_fee=entry_fee,
        unrealized_pnl=-entry_fee,
        execution_policy_version=config.execution_policy_version,
        strategy_semantics_fingerprint=SEMANTICS_FINGERPRINT,
    )
    marked = PaperPosition(
        **{
            **opened.to_dict(),
            "last_mark_price": mark_bar.close,
            "unrealized_pnl": (mark_bar.close - entry_price) * quantity - entry_fee,
        }
    )
    closed = PaperClose(
        close_id=stable_id(
            "close",
            opened.position_id,
            close_bar.timestamp.isoformat(),
            PaperCloseReason.TARGET.value,
        ),
        position_id=opened.position_id,
        run_id=_run_id(close_bar.timestamp.date()),
        mode=PaperRunMode.REPLAY,
        strategy_id=STRATEGY_ID,
        strategy_version=STRATEGY_VERSION,
        symbol="TST",
        close_time=close_bar.timestamp.isoformat(),
        close_price=exit_price,
        close_reason=PaperCloseReason.TARGET,
        gross_pnl=gross_pnl,
        net_pnl=net_pnl,
        r_multiple=net_pnl / risk,
        fee=exit_fee,
        slippage=exit_slippage,
        entry_fee=entry_fee,
        execution_policy_version=config.execution_policy_version,
        strategy_semantics_fingerprint=SEMANTICS_FINGERPRINT,
    )
    return [
        _ledger_event(
            "event-order-created",
            "paper_order_created",
            signal_bar,
            order.to_dict(),
        ),
        _ledger_event(
            "event-fill",
            "paper_fill",
            fill_bar,
            _with_source_bar(fill.to_dict(), fill_bar, snapshots),
        ),
        _ledger_event(
            "event-position-opened",
            "paper_position_opened",
            fill_bar,
            _with_source_bar(opened.to_dict(), fill_bar, snapshots),
        ),
        _ledger_event(
            "event-position-marked",
            "paper_position_marked_to_market",
            mark_bar,
            _with_source_bar(marked.to_dict(), mark_bar, snapshots),
        ),
        _ledger_event(
            "event-position-closed",
            "paper_position_closed",
            close_bar,
            _with_source_bar(closed.to_dict(), close_bar, snapshots),
        ),
    ]


def _ledger_event(
    event_id: str,
    event_type: str,
    bar: MarketBar,
    payload: dict[str, object],
) -> dict[str, object]:
    return PaperLedgerEvent(
        event_id=event_id,
        event_type=event_type,
        run_id=_run_id(bar.timestamp.date()),
        mode=PaperRunMode.REPLAY,
        trade_date=bar.timestamp.date().isoformat(),
        strategy_id=STRATEGY_ID,
        symbol="TST",
        payload=payload,
    ).to_dict()


def _with_source_bar(
    payload: dict[str, object],
    bar: MarketBar,
    snapshots: dict[str, DataTruthManifest],
) -> dict[str, object]:
    source_bar: dict[str, object] = {
        "close": bar.close,
        "high": bar.high,
        "low": bar.low,
        "open": bar.open,
        "symbol": bar.symbol,
        "timestamp": bar.timestamp.isoformat(),
        "volume": bar.volume,
    }
    manifest = snapshots[bar.timestamp.date().isoformat()]
    return {
        **payload,
        "data_snapshot_id": manifest.snapshot_id,
        "source_bar": source_bar,
        "source_bar_sha256": _payload_sha256(source_bar),
    }


def _write_paper_manifest(
    output_root: Path,
    run_date: date,
    data_manifest: DataTruthManifest,
    config: PaperOpsConfig,
) -> None:
    manifest = PaperOpsManifest(
        run_id=_run_id(run_date),
        mode=PaperRunMode.REPLAY,
        run_date=run_date.isoformat(),
        data_snapshot_id=data_manifest.snapshot_id,
        output_artifacts=("ledger/paper_ledger.jsonl",),
        warnings=(),
        execution_policy_version=config.execution_policy_version,
        execution_policy_fingerprint=paper_ops_engine._execution_policy_fingerprint(config),
        universe_id=config.universe_id,
        universe_symbols=config.universe_symbols,
        data_snapshot_content_hash=data_manifest.snapshot_content_hash,
        data_snapshot_manifest_payload_hash=data_manifest.manifest_payload_hash,
        data_snapshot_normalized_hash=data_manifest.normalized_artifact_hash,
        data_snapshot_normalized_path=data_manifest.normalized_artifact_path,
        data_truth_root_relative="data_truth_replay",
    ).to_dict()
    manifest.pop("manifest_payload_hash")
    manifest["manifest_payload_hash"] = _payload_sha256(manifest)
    write_json(output_root / "manifests" / f"{_run_id(run_date)}.json", manifest)


def _write_shadow_manifest(
    scenario: _ImmutableLifecycleScenario,
    run_date: date,
    *,
    schema_version: str,
) -> None:
    snapshot = scenario.snapshots_by_date[run_date.isoformat()]
    challenger_id = "ts_momentum_sma_atr_shadow_v2"
    write_json(
        scenario.output_root / "manifests" / f"shadow_replay_{run_date}_{challenger_id}.json",
        {
            "schema_version": schema_version,
            "status": "completed",
            "date": run_date.isoformat(),
            "mode": PaperRunMode.REPLAY.value,
            "run_id": _run_id(run_date),
            "data_snapshot_id": snapshot.snapshot_id,
            "challenger_id": challenger_id,
            "strategy_id": STRATEGY_ID,
            "strategy_version": "v2.0",
            "execution_policy_version": scenario.lifecycle_config.execution_policy_version,
            "decision_coverage_status": "complete",
            "research_only": True,
            "automatic_promotion_enabled": False,
            "broker_execution_allowed": False,
        },
    )


def _write_execution_policy_manifest(
    output_root: Path,
    active_config: PaperOpsConfig,
    *configs: PaperOpsConfig,
) -> None:
    unique_configs = {
        config.execution_policy_version: config for config in (active_config, *configs)
    }
    policies: dict[str, object] = {}
    for version, config in unique_configs.items():
        configuration = paper_ops_engine._execution_policy_fingerprint_payload(config)
        policies[version] = {
            "configuration": configuration,
            "fingerprint": _payload_sha256(configuration),
            "registered_at": "2026-01-01T00:00:00+00:00",
        }
    write_json(
        output_root / "state" / "execution_policy_manifest.json",
        {
            "active_execution_policy_version": active_config.execution_policy_version,
            "policies": policies,
            "schema_version": "v2.paper_execution_policy_manifest.v1",
        },
    )


def _execution_config(
    *,
    version: str = "paperops_daily_next_open_risk_v2",
    fee_bps: float = FEE_BPS,
    slippage_bps: float = SLIPPAGE_BPS,
    universe_symbols: tuple[str, ...] = ("TST",),
) -> PaperOpsConfig:
    return PaperOpsConfig(
        execution_policy_version=version,
        fee_bps=fee_bps,
        slippage_bps=slippage_bps,
        universe_id="source-bar-truth-tst-v1",
        universe_symbols=universe_symbols,
    )


def _manifest_path(scenario: _ImmutableLifecycleScenario, run_date: date) -> Path:
    return scenario.output_root / "manifests" / f"{_run_id(run_date)}.json"


def _rewrite_manifest(path: Path, payload: dict[str, object]) -> None:
    payload.pop("manifest_payload_hash", None)
    payload["manifest_payload_hash"] = _payload_sha256(payload)
    write_json(path, payload)


def _event_payload(
    scenario: _ImmutableLifecycleScenario,
    event_type: str,
) -> dict[str, object]:
    event = next(event for event in scenario.events if event["event_type"] == event_type)
    payload = event["payload"]
    assert isinstance(payload, dict)
    return payload


def _event_row(
    scenario: _ImmutableLifecycleScenario,
    event_type: str,
) -> dict[str, object]:
    return next(event for event in scenario.events if event["event_type"] == event_type)


def _daily_bar(
    session_date: date,
    open_price: float,
    high: float,
    low: float,
    close: float,
) -> MarketBar:
    return MarketBar(
        symbol="TST",
        timestamp=datetime.combine(session_date, datetime.min.time(), tzinfo=timezone.utc),
        open=open_price,
        high=high,
        low=low,
        close=close,
        volume=1_000_000,
    )


def _run_id(run_date: date) -> str:
    return f"paper-run-{run_date.isoformat()}"


def _payload_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
