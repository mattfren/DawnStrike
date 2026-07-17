from __future__ import annotations

import csv
import hashlib
from datetime import date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from intraday_scanner.cli import main
from intraday_scanner.errors import StorageError
from intraday_scanner.notifiers.telegram_formatter import (
    format_alpha_watch,
    select_alpha_watch_rows,
)
from intraday_scanner.services import alpha_cycle_service
from intraday_scanner.services.alpha_cycle_service import (
    alpha_cycle,
    alpha_learn,
    alpha_monitor,
)
from intraday_scanner.services.alpha_paper_service import (
    ALPHAOPS_COHORT,
    ALPHAOPS_STRATEGY_ID,
    alpha_paper_reconcile,
    classify_alpha_telegram_delivery,
    freeze_alpha_telegram_cohort,
    persist_alpha_telegram_delivery,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore

ET = ZoneInfo("America/New_York")
RUN_DATE = date(2026, 7, 16)
SELECTED_AT = "2026-07-16T08:10:00-04:00"


def _telegram_receipt(text: str, *, message_id: int = 101) -> dict[str, object]:
    encoded = text.encode("utf-8")
    telegram_response = {"ok": True, "result": {"message_id": message_id}}
    return {
        "transport": "telegram",
        "transmitted_text": text,
        "transmitted_byte_count": len(encoded),
        "transmitted_bytes_sha256": hashlib.sha256(encoded).hexdigest(),
        "http_status": 200,
        "provider_response": {"http_status": 200, **telegram_response},
        "telegram_response": {"http_status": 200, **telegram_response},
        "message_id": message_id,
    }


def _signal(
    ticker: str = "AAA",
    *,
    rank: int = 1,
    trigger: float = 10.0,
    stop: float = 9.0,
    target: float = 12.0,
    can_alert: bool = True,
) -> dict[str, object]:
    return {
        "signal_key": f"scan-1:{rank}:{ticker}",
        "scan_id": "scan-1",
        "ticker": ticker,
        "rank": rank,
        "can_alert": can_alert,
        "alpha_score": 80 - rank,
        "review_label": "READY",
        "confidence_bucket": "MEDIUM",
        "setup_key": "opening_drive",
        "entry_trigger": trigger,
        "invalidation": stop,
        "target_1": target,
        "source": "sourced_test_fixture",
    }


def _seed_delivered(
    db_path: Path,
    *,
    signals: list[dict[str, object]] | None = None,
    no_trade: bool = False,
    body: str | None = None,
    run_date: date = RUN_DATE,
    selected_at: str = SELECTED_AT,
) -> tuple[SQLiteScanStore, list[dict[str, object]], str]:
    store = SQLiteScanStore(db_path)
    store.initialize()
    event_key = "alphaops:scan-1:alpha_morning_watch"
    rows = signals or [_signal()]
    message = body or (
        "Dawnstrike AlphaOps: no clean edge"
        if no_trade
        else format_alpha_watch(signals=rows, edge_label="HIGH")
    )
    no_trade_row = (
        {
            "signal_id": f"no_trade:scan-1:{run_date.isoformat()}",
            "scan_id": "scan-1",
            "ticker": "NO_TRADE",
            "rank": 0,
        }
        if no_trade
        else None
    )
    selections = freeze_alpha_telegram_cohort(
        store,
        scan_id="scan-1",
        selected_at=selected_at,
        event_key=event_key,
        body=message,
        rendered_rows=[] if no_trade else select_alpha_watch_rows(rows),
        no_trade_row=no_trade_row,
    )
    store.record_notification(
        event_key=f"{event_key}:telegram",
        channel="telegram",
        run_id="scan-1",
        payload={
            "title": "Alpha",
            "body": message,
            "payload": {"run_id": "scan-1"},
            "transport_receipt": _telegram_receipt(message),
        },
    )
    persist_alpha_telegram_delivery(
        store,
        selections=selections,
        delivery_status="delivered",
        transport_receipt=_telegram_receipt(message),
        attempted_at="2026-07-16T08:11:00-04:00",
    )
    return store, selections, message


def _write_bars(
    path: Path,
    symbols: list[str],
    *,
    scenario: str = "target",
    omit_index: int | None = None,
    run_date: date = RUN_DATE,
    bar_count: int = 390,
) -> Path:
    start = datetime.combine(run_date, time(9, 31), ET)
    rows: list[dict[str, object]] = []
    for symbol in symbols:
        for index in range(bar_count):
            if index == omit_index:
                continue
            timestamp = start + timedelta(minutes=index)
            open_price = 9.5
            high = 9.8
            low = 9.4
            close = 9.6
            if scenario != "not_triggered" and index >= 10:
                open_price = 10.0
                high = 10.4
                low = 9.7
                close = 10.1
            if scenario == "target" and index == 20:
                high = 12.2
                low = 10.0
                close = 12.0
            if scenario == "stop" and index == 20:
                high = 10.3
                low = 8.8
                close = 9.0
            if scenario == "stop_first" and index == 20:
                high = 12.3
                low = 8.8
                close = 10.0
            if scenario == "eod" and index == bar_count - 1:
                high = 11.1
                low = 9.9
                close = 11.0
            rows.append(
                {
                    "symbol": symbol,
                    "timestamp": timestamp.isoformat(),
                    "open": open_price,
                    "high": high,
                    "low": low,
                    "close": close,
                    "volume": 1000 + index,
                }
            )
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    return path


def test_formatter_and_cohort_share_exact_three_row_truncation_and_body_hash(
    tmp_path: Path,
) -> None:
    signals = [_signal(chr(64 + index) * 3, rank=index) for index in range(1, 6)]
    rendered = select_alpha_watch_rows(signals)
    body = format_alpha_watch(signals=signals, edge_label="HIGH")
    store = SQLiteScanStore(tmp_path / "alpha.sqlite")
    first = freeze_alpha_telegram_cohort(
        store,
        scan_id="scan-1",
        selected_at=SELECTED_AT,
        event_key="alphaops:scan-1:watch",
        body=body,
        rendered_rows=rendered,
    )
    second = freeze_alpha_telegram_cohort(
        store,
        scan_id="scan-1",
        selected_at=SELECTED_AT,
        event_key="alphaops:scan-1:watch",
        body=body,
        rendered_rows=rendered,
    )

    assert len(rendered) == len(first) == len(second) == 3
    assert [row["ticker"] for row in first] == ["AAA", "BBB", "CCC"]
    assert {row["body_sha256"] for row in first} == {
        hashlib.sha256(body.encode()).hexdigest()
    }
    assert len(store.load_signal_selections(cohort=ALPHAOPS_COHORT)) == 3
    with pytest.raises(StorageError, match="different Telegram cohort"):
        freeze_alpha_telegram_cohort(
            store,
            scan_id="scan-1",
            selected_at=SELECTED_AT,
            event_key="alphaops:scan-1:watch",
            body=body + " changed",
            rendered_rows=rendered,
        )


def test_dry_run_and_failure_never_become_delivered_membership(tmp_path: Path) -> None:
    store = SQLiteScanStore(tmp_path / "alpha.sqlite")
    signal = _signal()
    body = format_alpha_watch(signals=[signal], edge_label="HIGH")
    event_key = "alphaops:scan-1:watch"
    selections = freeze_alpha_telegram_cohort(
        store,
        scan_id="scan-1",
        selected_at=SELECTED_AT,
        event_key=event_key,
        body=body,
        rendered_rows=[signal],
    )
    dry_status = classify_alpha_telegram_delivery(
        store,
        event_key=event_key,
        body=body,
        dry_run=True,
        notification_stats={"sent": 1},
    )
    persist_alpha_telegram_delivery(store, selections=selections, delivery_status=dry_status)
    persist_alpha_telegram_delivery(store, selections=selections, delivery_status="failed")

    deliveries = store.load_notification_deliveries(cohort=ALPHAOPS_COHORT)
    assert len(deliveries) == 1
    assert deliveries[0]["delivery_status"] == "failed"
    result = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=None,
        out_dir=tmp_path / "out",
        persist=True,
    )
    assert result["exit_code"] == 2
    assert "not a proven real delivery" in " ".join(result["blocked_reasons"])


def test_real_delivery_dedupe_is_idempotent_and_no_trade_is_explicit(tmp_path: Path) -> None:
    store, selections, message = _seed_delivered(
        tmp_path / "alpha.sqlite",
        no_trade=True,
    )
    persist_alpha_telegram_delivery(
        store,
        selections=selections,
        delivery_status="delivered",
        transport_receipt=_telegram_receipt(message),
        attempted_at="2026-07-16T08:12:00-04:00",
    )
    result = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=None,
        out_dir=tmp_path / "out",
        persist=True,
    )

    assert result["exit_code"] == 0
    assert result["no_trade"] is True
    assert result["scorecard"]["session_status"] == "no_signal"
    assert result["scorecard"]["average_net_return_pct"] is None
    assert result["scorecard"]["net_pnl"] is None
    assert len(store.load_notification_deliveries(cohort=ALPHAOPS_COHORT)) == 1


@pytest.mark.parametrize(
    ("scenario", "expected_reason", "closed", "activated"),
    [
        ("target", "target", 1, True),
        ("stop", "stop", 1, True),
        ("stop_first", "stop", 1, True),
        ("eod", "eod", 1, True),
        ("not_triggered", None, 0, False),
    ],
)
def test_authoritative_lifecycle_states_and_costs(
    tmp_path: Path,
    scenario: str,
    expected_reason: str | None,
    closed: int,
    activated: bool,
) -> None:
    case = tmp_path / scenario
    store, _, _ = _seed_delivered(case / "alpha.sqlite")
    bars = _write_bars(case / "bars.csv", ["AAA"], scenario=scenario)
    result = alpha_paper_reconcile(
        db_path=case / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=bars,
        out_dir=case / "out",
        persist=True,
        slippage_bps=10,
        fee_bps=2,
    )

    assert result["exit_code"] == 0
    assert result["evaluations"][0]["activated"] is activated
    assert result["scorecard"]["closed_count"] == closed
    if expected_reason is None:
        assert result["evaluations"][0]["terminal_state"] == "not_triggered"
        assert result["evaluations"][0]["net_return_pct"] is None
        assert result["paper_trades"] == []
        return_labels = [
            row for row in result["learning_labels"] if row["label_family"] == "return_after_cost"
        ]
        assert return_labels[0]["eligible"] is False
        assert return_labels[0]["label_value"] is None
    else:
        trade = result["paper_trades"][0]
        assert trade["exit_reason"] == expected_reason
        assert trade["fees"] > 0
        assert trade["slippage_cost"] > 0
        assert trade["intrabar_ambiguity_policy"] == "stop_first"
        assert store.load_strategy_paper_trades(
            strategy_id=ALPHAOPS_STRATEGY_ID,
            cohort=ALPHAOPS_COHORT,
        )


def test_incomplete_grid_and_body_tamper_fail_closed(tmp_path: Path) -> None:
    _, selections, body = _seed_delivered(tmp_path / "alpha.sqlite")
    incomplete = _write_bars(tmp_path / "incomplete.csv", ["AAA"], omit_index=42)
    result = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=incomplete,
        out_dir=tmp_path / "out-incomplete",
        persist=True,
    )
    assert result["exit_code"] == 2
    assert "incomplete one-minute" in " ".join(result["blocked_reasons"])

    event_key = str(selections[0]["event_key"])
    other = SQLiteScanStore(tmp_path / "tampered.sqlite")
    frozen = freeze_alpha_telegram_cohort(
        other,
        scan_id="scan-1",
        selected_at=SELECTED_AT,
        event_key=event_key,
        body=body,
        rendered_rows=[_signal()],
    )
    other.record_notification(
        event_key=f"{event_key}:telegram",
        channel="telegram",
        payload={
            "body": body + " tampered",
            "transport_receipt": _telegram_receipt(body + " tampered"),
        },
    )
    with pytest.raises(StorageError, match="transmitted bytes differ"):
        persist_alpha_telegram_delivery(
            other,
            selections=frozen,
            delivery_status="delivered",
            transport_receipt=_telegram_receipt(body + " tampered"),
        )


def test_aggregate_bars_ignore_unrelated_symbols_and_retain_exact_cohort(
    tmp_path: Path,
) -> None:
    _seed_delivered(tmp_path / "alpha.sqlite")
    aggregate = _write_bars(tmp_path / "aggregate.csv", ["AAA", "BBB"])
    result = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=aggregate,
        out_dir=tmp_path / "out",
        persist=True,
        reconciled_at="2026-07-16T16:05:00-04:00",
    )

    assert result["exit_code"] == 0
    assert result["source_bar_raw_sha256"] != result["source_bar_upstream_raw_sha256"]
    retained = Path(str(result["retained_source_path"]))
    with retained.open("r", encoding="utf-8", newline="") as handle:
        symbols = {row["symbol"] for row in csv.DictReader(handle)}
    assert symbols == {"AAA"}
    assert hashlib.sha256(retained.read_bytes()).hexdigest() == result["source_bar_raw_sha256"]


def test_canonical_per_symbol_directory_resolves_exact_delivered_set(tmp_path: Path) -> None:
    _seed_delivered(tmp_path / "alpha.sqlite")
    canonical_root = tmp_path / "canonical"
    _write_bars(
        canonical_root / "AAA" / f"{RUN_DATE.isoformat()}_canonical_intraday.csv",
        ["AAA"],
    )
    result = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=canonical_root,
        out_dir=tmp_path / "out",
        persist=True,
    )

    assert result["exit_code"] == 0
    assert Path(str(result["retained_source_path"])).is_file()


def test_official_market_date_cohort_lock_rejects_replacement(tmp_path: Path) -> None:
    store, _, body = _seed_delivered(tmp_path / "alpha.sqlite")
    replacement = _signal("BBB")
    with pytest.raises(StorageError, match="already frozen for this market date"):
        freeze_alpha_telegram_cohort(
            store,
            scan_id="scan-2",
            selected_at=SELECTED_AT,
            event_key="alphaops:scan-2:alpha_morning_watch",
            body=body,
            rendered_rows=[replacement],
        )
    official = store.load_official_strategy_cohort(
        market_date=RUN_DATE.isoformat(),
        strategy_id=ALPHAOPS_STRATEGY_ID,
        strategy_version="dawnstrike-alphaops-v4",
        cohort=ALPHAOPS_COHORT,
    )
    assert official is not None
    assert official["scan_id"] == "scan-1"


def test_published_early_close_requires_and_accepts_exact_shortened_grid(
    tmp_path: Path,
) -> None:
    early_date = date(2026, 11, 27)
    db_path = tmp_path / "early.sqlite"
    _seed_delivered(
        db_path,
        run_date=early_date,
        selected_at="2026-11-27T08:10:00-05:00",
    )
    bars = _write_bars(
        tmp_path / "early.csv",
        ["AAA"],
        scenario="target",
        run_date=early_date,
        bar_count=210,
    )
    result = alpha_paper_reconcile(
        db_path=db_path,
        market_date=early_date.isoformat(),
        bars_csv=bars,
        out_dir=tmp_path / "early-out",
        persist=True,
    )

    assert result["exit_code"] == 0
    assert result["source_bar_normalized_sha256"]


def test_reconciliation_and_cli_are_idempotent_and_return_blocked_code_two(tmp_path: Path) -> None:
    store, _, _ = _seed_delivered(tmp_path / "alpha.sqlite")
    bars = _write_bars(tmp_path / "bars.csv", ["AAA"])
    kwargs = {
        "db_path": tmp_path / "alpha.sqlite",
        "market_date": RUN_DATE.isoformat(),
        "bars_csv": bars,
        "out_dir": tmp_path / "out",
        "persist": True,
        "reconciled_at": "2026-07-16T16:05:00-04:00",
    }
    first = alpha_paper_reconcile(**kwargs)
    second = alpha_paper_reconcile(**kwargs)
    assert first["exit_code"] == second["exit_code"] == 0
    assert first["evaluations"][0]["evaluation_id"] == second["evaluations"][0]["evaluation_id"]
    assert len(store.load_strategy_evaluations(strategy_id=ALPHAOPS_STRATEGY_ID)) == 1
    assert len(store.load_strategy_paper_trades(strategy_id=ALPHAOPS_STRATEGY_ID)) == 1

    blocked = main(
        [
            "alpha-paper-reconcile",
            "--db-path",
            str(tmp_path / "missing.sqlite"),
            "--market-date",
            RUN_DATE.isoformat(),
            "--out-dir",
            str(tmp_path / "missing-out"),
            "--persist",
        ]
    )
    assert blocked == 2


def test_learning_is_blocked_until_reconcile_then_consumes_split_canonical_labels(
    tmp_path: Path,
) -> None:
    store, _, _ = _seed_delivered(tmp_path / "alpha.sqlite")
    blocked = alpha_learn(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
    )
    assert blocked["status"] == "blocked_incomplete"

    result = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=_write_bars(tmp_path / "bars.csv", ["AAA"], scenario="target"),
        out_dir=tmp_path / "out",
        persist=True,
    )
    assert result["exit_code"] == 0
    learned = alpha_learn(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
    )
    assert learned["status"] == "complete"
    assert learned["activation_label_count"] == 1
    assert learned["return_label_count"] == 1
    assert learned["manual_outcomes_consumed"] == 0
    production = store.load_alpha_outcome_labels()
    assert len(production) == 1
    assert production[0]["label_source"] == "strategy_learning"
    assert production[0]["after_cost"] is True
    memory = store.load_alpha_setup_memory()["opening_drive"]
    assert memory["activation_sample_size"] == 1
    assert memory["activation_adjustment_eligible"] is False


def test_reconciliation_truth_is_immutable_across_changed_source_bars(tmp_path: Path) -> None:
    store, _, _ = _seed_delivered(tmp_path / "alpha.sqlite")
    first_bars = _write_bars(tmp_path / "target.csv", ["AAA"], scenario="target")
    first = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=first_bars,
        out_dir=tmp_path / "out",
        persist=True,
        reconciled_at="2026-07-16T16:05:00-04:00",
    )
    identical = alpha_paper_reconcile(
        db_path=tmp_path / "alpha.sqlite",
        market_date=RUN_DATE.isoformat(),
        bars_csv=first_bars,
        out_dir=tmp_path / "out-rerun",
        persist=True,
        reconciled_at="2026-07-16T16:10:00-04:00",
    )

    assert first["exit_code"] == identical["exit_code"] == 0
    assert identical["persistence"]["strategy_reconciliation"]["evaluations"] == {
        "inserted": 0,
        "updated": 0,
    }

    changed_bars = _write_bars(tmp_path / "stop.csv", ["AAA"], scenario="stop")
    with pytest.raises(StorageError, match="Immutable strategy_evaluations identity"):
        alpha_paper_reconcile(
            db_path=tmp_path / "alpha.sqlite",
            market_date=RUN_DATE.isoformat(),
            bars_csv=changed_bars,
            out_dir=tmp_path / "out-changed",
            persist=True,
            reconciled_at="2026-07-16T16:15:00-04:00",
        )
    persisted = store.load_strategy_paper_trades(
        strategy_id=ALPHAOPS_STRATEGY_ID,
        cohort=ALPHAOPS_COHORT,
    )
    assert len(persisted) == 1
    assert persisted[0]["exit_reason"] == "target"


def test_monitor_uses_only_current_proven_cohort_and_sourced_quotes(tmp_path: Path) -> None:
    previous_db = tmp_path / "previous.sqlite"
    _seed_delivered(
        previous_db,
        run_date=date(2026, 7, 15),
        selected_at="2026-07-15T08:10:00-04:00",
    )
    as_of = datetime(2026, 7, 16, 10, 0, tzinfo=ET)
    previous = alpha_monitor(
        db_path=previous_db,
        notify="console",
        dry_run=True,
        as_of=as_of,
    )
    assert previous["status"] == "blocked_incomplete"
    assert "requested date" in " ".join(previous["blocked_reasons"])

    current_db = tmp_path / "current.sqlite"
    _seed_delivered(current_db)
    missing = alpha_monitor(
        db_path=current_db,
        notify="console",
        dry_run=True,
        as_of=as_of,
    )
    assert missing["status"] == "blocked_incomplete"
    assert missing["blocked_symbols"] == ["AAA"]
    quote = {
        "AAA": {
            "observation_id": "quote-aaa-1",
            "price": 10.25,
            "source": "licensed_quote_fixture",
            "provider": "test_provider",
            "observed_at": "2026-07-16T10:00:00-04:00",
            "price_type": "last_bar_close",
            "is_usable": True,
        }
    }
    first = alpha_monitor(
        db_path=current_db,
        notify="console",
        dry_run=True,
        current_prices=quote,
        as_of=as_of,
    )
    second = alpha_monitor(
        db_path=current_db,
        notify="console",
        dry_run=True,
        current_prices=quote,
        as_of=as_of,
    )
    assert first["status"] == second["status"] == "checked"
    assert first["quote_source_refs"][0]["observation_id"] == "quote-aaa-1"
    assert first["notification_stats"]["sent"] == 1
    assert second["notification_stats"]["skipped"] == 1


def test_source_failure_freezes_one_delivered_no_trade_daily_denominator(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class ReceiptTelegram:
        channel = "telegram"

        def send(self, event):  # noqa: ANN001, ANN201
            return _telegram_receipt(event.body)

    calls = {"collect": 0}

    def failed_collect(**_kwargs):  # noqa: ANN003, ANN202
        calls["collect"] += 1
        return {
            "status": "failed",
            "source_summary": {
                "candidate_count": 0,
                "successful_sources": 0,
                "failed_sources": 2,
            },
        }

    monkeypatch.setattr(alpha_cycle_service, "web_auto_collect", failed_collect)
    monkeypatch.setattr(
        alpha_cycle_service,
        "build_notifiers",
        lambda _config: [ReceiptTelegram()],
    )
    monkeypatch.setattr(
        alpha_cycle_service,
        "_expected_telegram_transmission",
        lambda event, **_kwargs: event.body,
    )
    as_of = datetime(2026, 7, 16, 8, 10, tzinfo=ET)
    db_path = tmp_path / "alpha.sqlite"
    first = alpha_cycle(
        config_path=tmp_path / "unused.yaml",
        db_path=db_path,
        out_dir=tmp_path / "cycle",
        notify="telegram",
        dry_run=False,
        as_of=as_of,
    )
    second = alpha_cycle(
        config_path=tmp_path / "unused.yaml",
        db_path=db_path,
        out_dir=tmp_path / "cycle-retry",
        notify="telegram",
        dry_run=False,
        as_of=as_of,
    )

    assert first["status"] == "no_trade"
    assert first["official_telegram_delivery_status"] == "delivered"
    assert second["status"] == "official_cohort_already_frozen"
    assert calls["collect"] == 1
    reconciled = alpha_paper_reconcile(
        db_path=db_path,
        market_date=RUN_DATE.isoformat(),
        bars_csv=None,
        out_dir=tmp_path / "reconcile",
        persist=True,
    )
    assert reconciled["exit_code"] == 0
    assert reconciled["scorecard"]["no_trade_count"] == 1


def test_eod_wrapper_propagates_truth_codes_and_contains_no_execution_stage() -> None:
    body = Path("scripts/run_alphaops_eod_full.bat").read_text(encoding="utf-8").lower()
    assert "alpha-paper-reconcile" in body
    assert "alpha-learn" in body
    assert "exit /b 2" in body
    assert "alpha-capture-outcomes" not in body
    assert "trade-watch" not in body
    assert "live_execute" not in body
    assert "broker" in body and "no broker" in body
