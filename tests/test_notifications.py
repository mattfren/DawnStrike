import hashlib
import json

from intraday_scanner.config import ScannerConfig
from intraday_scanner.notifiers import ConsoleNotifier, dispatch_events, scan_events_from_payload
from intraday_scanner.notifiers.base import BaseNotifier, NotificationEvent
from intraday_scanner.notifiers.webhooks import TelegramNotifier
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


def test_dry_run_record_does_not_block_later_real_delivery(tmp_path):
    class RecordingNotifier(BaseNotifier):
        channel = "telegram"

        def __init__(self) -> None:
            self.sent: list[str] = []

        def send(self, event: NotificationEvent) -> None:
            self.sent.append(event.event_key)

    store = SQLiteScanStore(tmp_path / "notifications.sqlite")
    notifier = RecordingNotifier()
    event = NotificationEvent(
        event_key="alphaops:scan-1:watch",
        title="Alpha",
        body="exact body",
        channel_hint="watch",
        payload={"run_id": "scan-1"},
    )

    simulated = dispatch_events([event], [notifier], store, dry_run=True)
    delivered = dispatch_events([event], [notifier], store, dry_run=False)
    deduped = dispatch_events([event], [notifier], store, dry_run=False)

    assert simulated == {"sent": 1, "skipped": 0}
    assert delivered == {"sent": 1, "skipped": 0}
    assert deduped == {"sent": 0, "skipped": 1}
    assert notifier.sent == [event.event_key]
    persisted = store.load_notification(f"{event.event_key}:telegram")
    assert persisted is not None
    assert persisted.get("dry_run") is not True


def test_telegram_notifier_returns_exact_transmitted_bytes_and_message_id(monkeypatch):
    sent: dict[str, object] = {}

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, traceback):
            return False

        def read(self):
            return json.dumps(
                {"ok": True, "result": {"message_id": 4123}}
            ).encode("utf-8")

    def fake_urlopen(request, timeout):
        sent["payload"] = json.loads(request.data.decode("utf-8"))
        sent["timeout"] = timeout
        return Response()

    monkeypatch.setattr(
        "intraday_scanner.notifiers.webhooks.urllib.request.urlopen",
        fake_urlopen,
    )
    compact = "PAPER ONLY — exact delivered membership"
    event = NotificationEvent(
        event_key="mover:signal:1",
        title="Ignored compact title",
        body=compact,
        channel_hint="daily_summary",
        payload={"telegram_compact_message": compact},
    )
    notifier = TelegramNotifier(
        ScannerConfig(
            telegram_bot_token="not-persisted",
            telegram_chat_id="-100",
            telegram_message_style="compact",
        )
    )

    receipt = notifier.send(event)

    encoded = compact.encode("utf-8")
    assert sent["payload"] == {"chat_id": "-100", "text": compact}
    assert receipt["transmitted_text"] == compact
    assert receipt["transmitted_byte_count"] == len(encoded)
    assert receipt["transmitted_bytes_sha256"] == hashlib.sha256(encoded).hexdigest()
    assert receipt["message_id"] == 4123
    assert receipt["telegram_response"]["ok"] is True


def test_dispatch_can_capture_and_persist_transport_receipt(tmp_path):
    class ReceiptNotifier(BaseNotifier):
        channel = "telegram"

        def send(self, event: NotificationEvent):
            encoded = event.body.encode("utf-8")
            return {
                "transmitted_text": event.body,
                "transmitted_byte_count": len(encoded),
                "transmitted_bytes_sha256": hashlib.sha256(encoded).hexdigest(),
                "message_id": 9,
            }

    store = SQLiteScanStore(tmp_path / "notifications.sqlite")
    event = NotificationEvent(
        event_key="mover:signal:receipt",
        title="Mover",
        body="exact bytes",
        channel_hint="daily_summary",
    )

    result = dispatch_events(
        [event],
        [ReceiptNotifier()],
        store,
        capture_transport_receipts=True,
    )

    assert result["sent"] == 1
    assert result["deliveries"][0]["transport_receipt"]["message_id"] == 9
    persisted = store.load_notification("mover:signal:receipt:telegram")
    assert persisted is not None
    assert persisted["transport_receipt"]["transmitted_text"] == "exact bytes"
