from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.errors import StorageError
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


def test_catalyst_naive_observation_time_fails_closed() -> None:
    event = build_catalyst_evidence_event(
        symbol="NOVA",
        source_kind="news",
        canonical_url="https://example.test/naive",
        content="NOVA announces a contract.",
        published_at="2026-08-03T13:59:00",
        first_seen_at="2026-08-03T14:00:00",
        decision_at="2026-08-03T14:00:00Z",
    )
    assert event["available_at_decision"] is False


def test_news_events_keep_all_articles_and_filing_feature_has_no_short_route() -> None:
    events = build_news_catalyst_events(
        [
            {
                "ticker": "NOVA",
                "headline": "first contract",
                "url": "https://example.test/a",
                "published_at": "2026-08-03T13:00:00Z",
                "first_seen_at": "2026-08-03T13:00:00Z",
            },
            {
                "ticker": "NOVA",
                "headline": "second offering",
                "url": "https://example.test/b",
                "published_at": "2026-08-03T13:30:00Z",
                "first_seen_at": "2026-08-03T13:30:00Z",
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
    assert event["event_payload_hash_sha256"] == canonical_hash(
        {
            key: value
            for key, value in event.items()
            if key not in {"created_at", "event_payload_hash_sha256", "event_self_hash_sha256"}
        }
    )


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


def test_catalyst_duplicate_semantics_reuse_but_conflict_fails_closed(tmp_path: Path) -> None:
    root = tmp_path / "evidence"
    store = CatalystEvidenceStore(tmp_path / "catalyst.sqlite", evidence_root=root)
    event = build_catalyst_evidence_event(
        symbol="NOVA",
        source_kind="news",
        canonical_url="https://example.test/a",
        content="NOVA announces a contract.",
        published_at="2026-08-03T13:00:00Z",
        first_seen_at="2026-08-03T13:01:00Z",
        decision_at="2026-08-03T14:00:00Z",
    )
    assert store.persist_event(event)["inserted"] == 1
    same_semantics = {**event, "created_at": "2026-08-03T13:02:00Z"}
    assert store.persist_event(same_semantics)["inserted"] == 0
    conflict = {**same_semantics, "polarity": "negative_mechanism"}
    conflict["event_payload_hash_sha256"] = canonical_hash(
        {
            key: value
            for key, value in conflict.items()
            if key not in {"created_at", "event_payload_hash_sha256", "event_self_hash_sha256"}
        }
    )
    with pytest.raises(StorageError, match="identity conflict"):
        store.persist_event(conflict)


def test_catalyst_concurrent_exact_duplicate_has_one_insert(tmp_path: Path) -> None:
    store = CatalystEvidenceStore(tmp_path / "catalyst.sqlite", evidence_root=tmp_path / "evidence")
    event = build_catalyst_evidence_event(
        symbol="NOVA",
        source_kind="news",
        canonical_url="https://example.test/concurrent",
        content="NOVA announces a contract.",
        published_at="2026-08-03T13:00:00Z",
        first_seen_at="2026-08-03T13:01:00Z",
        decision_at="2026-08-03T14:00:00Z",
    )
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(store.persist_event, [event, {**event, "created_at": "later"}]))
    assert sorted(result["inserted"] for result in results) == [0, 1]
    assert sorted(result["row_count"] for result in results) == [1, 1]
