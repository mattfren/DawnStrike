from __future__ import annotations

from pathlib import Path

from intraday_scanner.services.catalyst_evidence_service import (
    build_catalyst_evidence_event,
    build_filing_catalyst_event,
    build_news_catalyst_events,
    ingest_catalyst_evidence,
)
from intraday_scanner.storage.catalyst_evidence_store import CatalystEvidenceStore


def test_catalyst_availability_is_point_in_time_and_post_decision_is_not_retroactive() -> None:
    before = build_catalyst_evidence_event(
        symbol="NOVA",
        source_kind="news",
        canonical_url="https://example.test/before",
        content="NOVA announces a customer contract.",
        published_at="2026-08-03T13:59:00Z",
        first_seen_at="2026-08-03T14:00:00Z",
        decision_at="2026-08-03T14:00:00Z",
    )
    after = build_catalyst_evidence_event(
        symbol="NOVA",
        source_kind="news",
        canonical_url="https://example.test/after",
        content="NOVA announces a financing offering.",
        published_at="2026-08-03T14:01:00Z",
        first_seen_at="2026-08-03T14:01:00Z",
        decision_at="2026-08-03T14:00:00Z",
    )

    assert before["available_at_decision"] is True
    assert after["available_at_decision"] is False
    assert after["novelty"] == "post_decision_new_information"


def test_news_events_keep_all_articles_and_filing_feature_has_no_short_route() -> None:
    events = build_news_catalyst_events(
        [
            {
                "ticker": "NOVA",
                "headline": "first contract",
                "url": "https://example.test/a",
                "published_at": "2026-08-03T13:00:00Z",
            },
            {
                "ticker": "NOVA",
                "headline": "second offering",
                "url": "https://example.test/b",
                "published_at": "2026-08-03T13:30:00Z",
            },
        ],
        decision_at="2026-08-03T14:00:00Z",
    )
    event, feature = build_filing_catalyst_event(
        {
            "ticker": "NOVA",
            "form": "S-3",
            "filing_date": "2026-08-03",
            "primary_document_url": "https://www.sec.gov/Archives/example",
        },
        {"security_type": "common_stock", "relevant_offering_terms": "takedown"},
        decision_at="2026-08-03T14:00:00Z",
    )

    assert len(events) == 2
    assert events[0]["available_at_decision"] is True
    assert feature["route"] == "none"
    assert event["payload"]["research_feature"]["avoid_long"] is True


def test_catalyst_store_is_append_only_and_raw_content_is_external(tmp_path: Path) -> None:
    database = tmp_path / "catalyst.sqlite"
    root = tmp_path / "evidence"
    event = build_catalyst_evidence_event(
        symbol="NOVA",
        source_kind="news",
        canonical_url="https://example.test/a",
        content="NOVA announces a contract.",
        published_at="2026-08-03T13:00:00Z",
        first_seen_at="2026-08-03T13:01:00Z",
        decision_at="2026-08-03T14:00:00Z",
    )
    receipt = ingest_catalyst_evidence(
        db_path=str(database), evidence_root=str(root), events=[event]
    )
    store = CatalystEvidenceStore(database, evidence_root=root)
    raw = store.store_raw_document(source_kind="news", symbol="NOVA", content=b"source")

    assert receipt["event_inserted"] == 1
    assert store.persist_event(event)["inserted"] == 0
    assert Path(raw["path"]).is_file()
    assert raw["hash_sha256"]
