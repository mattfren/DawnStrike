from intraday_scanner.config import ScannerConfig
from intraday_scanner.notifiers import ConsoleNotifier, dispatch_events, scan_events_from_payload
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_raw_scanner_rows_cannot_emit_ticker_notifications(tmp_path):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite")
    store = SQLiteScanStore(config.database_path)
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    ScanService(provider, store=store).run(config, persist=True)
    latest = store.load_latest_scan()
    assert latest is not None

    events = scan_events_from_payload(latest, config)
    assert all(event.channel_hint not in {"top_explosive", "score_threshold"} for event in events)


def test_fully_gated_notifications_are_deduped(tmp_path, capsys):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite")
    store = SQLiteScanStore(config.database_path)
    row = _alertable_row()
    events = scan_events_from_payload(
        {
            "summary": {"run_id": "gated-run"},
            "top_explosive": [row],
            "ranked_candidates": [row],
            "avoid_list": [],
        },
        config,
    )

    assert events
    first = dispatch_events(events, [ConsoleNotifier()], store)
    second = dispatch_events(events, [ConsoleNotifier()], store)

    output = capsys.readouterr().out
    assert row["ticker"] in output
    assert first["sent"] > 0
    assert second["sent"] == 0
    assert second["skipped"] == first["sent"]


def _alertable_row() -> dict[str, object]:
    return {
        "ticker": "NOVA",
        "score": 90.0,
        "can_alert": True,
        "no_trade_reason": "",
        "source_confidence": 90.0,
        "source_count": 2,
        "premarket_price": 10.0,
        "premarket_volume": 500_000,
        "previous_close": 9.5,
        "float_shares": 1_000_000,
        "premarket_high": 10.2,
        "premarket_low": 9.8,
        "catalyst_summary": "Sourced filing",
        "catalyst_category": "filing",
        "catalyst_confidence": 0.9,
        "confidence_bucket": "HIGH",
        "edge_bucket": "HIGH",
        "setup_grade": "A",
        "data_quality_score": 95.0,
        "gap_pct": 5.0,
        "entry_trigger": 10.0,
        "invalidation_level": 9.5,
        "target_1": 11.0,
        "halt_status": "CLEAR",
        "sec_risk_status": "CLEAR",
        "corporate_action_status": "CLEAR",
        "source_quality_status": "VERIFIED",
    }
