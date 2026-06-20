from intraday_scanner.ai.headline_classifier import RuleBasedHeadlineClassifier
from intraday_scanner.providers.base import FilingItem, NewsItem
from intraday_scanner.services.alert_service import (
    alerts_from_news_and_filings,
    persist_deduped_alerts,
)
from intraday_scanner.storage.sqlite_store import SQLiteScanStore


def test_external_news_and_sec_alerts_are_persisted_and_deduped(tmp_path):
    store = SQLiteScanStore(tmp_path / "scanner.sqlite")
    alerts = alerts_from_news_and_filings(
        news_items=[
            NewsItem(
                ticker="NOVA",
                headline="NOVA announces registered direct offering",
                published_at="2026-06-20T09:35:00-04:00",
                source="fixture",
                url="https://example.test/news",
            )
        ],
        filing_items=[
            FilingItem(
                ticker="NOVA",
                filing_type="S-3",
                filed_at="2026-06-20T09:36:00-04:00",
                source="sec_rss",
                url="https://example.test/sec",
                headline="NOVA files shelf registration",
            )
        ],
        original_theses={"NOVA": "Fresh catalyst with strong premarket volume."},
        classifier=RuleBasedHeadlineClassifier(),
        run_id="scan-1",
    )

    assert {alert.event_type for alert in alerts} == {
        "news_dilution_risk",
        "sec_dilution_risk",
    }
    assert persist_deduped_alerts(store, alerts, run_id="scan-1") == 2
    assert persist_deduped_alerts(store, alerts, run_id="scan-1") == 0

    saved_alerts = store.load_recent_alerts()
    saved_events = store.load_recent_monitor_events()
    assert len(saved_alerts) == 2
    assert len(saved_events) == 2
    assert saved_alerts[0]["ticker"] == "NOVA"
