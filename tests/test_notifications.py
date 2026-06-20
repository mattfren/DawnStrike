from intraday_scanner.config import ScannerConfig
from intraday_scanner.notifiers import ConsoleNotifier, dispatch_events, scan_events_from_payload
from intraday_scanner.providers.csv_provider import CsvSnapshotProvider
from intraday_scanner.services.scan_service import ScanService
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_console_notifications_are_deduped(tmp_path, capsys):
    config = ScannerConfig(database_path=tmp_path / "scanner.sqlite")
    store = SQLiteScanStore(config.database_path)
    provider = CsvSnapshotProvider("sample_data/premarket_snapshot_sample.csv")
    result = ScanService(provider, store=store).run(config, persist=True)
    latest = store.load_latest_scan()
    assert latest is not None

    events = scan_events_from_payload(latest, config)
    assert events
    first = dispatch_events(events, [ConsoleNotifier()], store)
    second = dispatch_events(events, [ConsoleNotifier()], store)

    output = capsys.readouterr().out
    assert result.ranked_candidates[0].ticker in output
    assert first["sent"] > 0
    assert second["sent"] == 0
    assert second["skipped"] == first["sent"]
