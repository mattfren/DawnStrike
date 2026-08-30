"""Deterministic point-in-time catalyst evidence orchestration."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any

from intraday_scanner.alpha.v6.contracts import canonical_hash
from intraday_scanner.providers.sec_edgar_provider import classify_filing_research_feature
from intraday_scanner.storage.catalyst_evidence_store import CatalystEvidenceStore

CATALYST_EVENT_TYPES = frozenset(
    {
        "financing",
        "offering",
        "warrant",
        "reverse_split",
        "earnings",
        "regulatory",
        "contract",
        "clinical",
        "unclassified",
    }
)


def build_catalyst_evidence_event(
    *,
    symbol: str,
    source_kind: str,
    canonical_url: str,
    content: str,
    published_at: str | None,
    first_seen_at: str,
    available_at: str | None = None,
    decision_at: str | None = None,
    payload: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build one immutable event and explicitly classify decision-time availability."""

    source_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    available_timestamp = available_at or first_seen_at
    available = _is_available(
        published_at,
        first_seen_at=first_seen_at,
        available_at=available_timestamp,
        decision_at=decision_at,
    )
    event_type = _event_type(content)
    payload_value = dict(payload or {})
    event_id = hashlib.sha256(f"{symbol.upper()}:{source_kind}:{source_hash}".encode()).hexdigest()[
        :32
    ]
    event = {
        "event_id": event_id,
        "symbol": symbol.upper(),
        "source_kind": source_kind,
        "canonical_url": canonical_url,
        "source_content_hash_sha256": source_hash,
        "published_at": published_at,
        "first_seen_at": first_seen_at,
        "available_at": available_timestamp,
        "available_at_decision": available,
        "decision_at": decision_at,
        "event_type": event_type,
        "polarity": _polarity(content),
        "financing_mechanism": _financing_mechanism(content),
        "novelty": "new" if available else "post_decision_new_information",
        "timing": "pre_decision" if available else "post_decision",
        "source_coverage_status": "verified_source_url" if canonical_url else "content_only",
        "promotional_status": _promotional_status(content),
        "rumor_status": _rumor_status(content),
        "squeeze_mechanics": _squeeze_mechanics(content),
        "confidence_status": "deterministic_taxonomy_only",
        "payload": payload_value,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "research_only": True,
        "broker_execution_enabled": False,
    }
    # Bind both the immutable event payload and its source identity.  The
    # decision-time join verifies these hashes before admitting an event.
    event["source_lineage_hash_sha256"] = canonical_hash(
        {
            "source_kind": source_kind,
            "canonical_url": canonical_url,
            "source_content_hash_sha256": source_hash,
        }
    )
    event["event_payload_hash_sha256"] = canonical_hash(
        {key: value for key, value in event.items() if key != "created_at"}
    )
    return event


def build_news_catalyst_events(
    items: list[dict[str, Any]],
    *,
    decision_at: str | None = None,
) -> list[dict[str, Any]]:
    events = [
        build_catalyst_evidence_event(
            symbol=str(item.get("ticker") or item.get("symbol") or ""),
            source_kind=str(item.get("source") or "news"),
            canonical_url=str(item.get("canonical_url") or item.get("url") or ""),
            content=" ".join(
                str(item.get(key) or "") for key in ("headline", "summary", "content")
            ),
            published_at=str(item.get("published_at") or "") or None,
            # Provider adapters should supply first_seen_at/available_at.  If
            # both are absent, use the local ingest clock; publisher time is
            # never treated as observation time.
            first_seen_at=str(
                item.get("first_seen_at")
                or item.get("available_at")
                or datetime.now(timezone.utc).isoformat()
            ),
            available_at=str(item.get("available_at") or "") or None,
            decision_at=decision_at,
            payload=item,
        )
        for item in items
        if str(item.get("ticker") or item.get("symbol") or "").strip()
    ]
    return sorted(events, key=lambda item: (str(item.get("published_at") or ""), item["event_id"]))


def build_filing_catalyst_event(
    filing: dict[str, Any],
    facts: dict[str, Any],
    *,
    decision_at: str,
) -> tuple[dict[str, Any], dict[str, Any]]:
    content = (
        " ".join(
            str(filing.get(key) or "") for key in ("form", "primary_doc_description", "filing_date")
        )
        + " "
        + str(facts.get("relevant_offering_terms") or "")
    )
    event = build_catalyst_evidence_event(
        symbol=str(filing.get("ticker") or ""),
        source_kind="sec_filing",
        canonical_url=str(filing.get("primary_document_url") or ""),
        content=content,
        published_at=str(filing.get("sec_acceptance_timestamp") or filing.get("filing_date") or "")
        or None,
        first_seen_at=str(filing.get("first_seen_at") or datetime.now(timezone.utc).isoformat()),
        decision_at=decision_at,
        payload={"filing": filing, "facts": facts},
    )
    feature = classify_filing_research_feature(filing, facts, decision_at=decision_at)
    event["payload"]["research_feature"] = feature
    event["event_payload_hash_sha256"] = canonical_hash(
        {
            key: value
            for key, value in event.items()
            if key not in {"created_at", "event_payload_hash_sha256", "event_self_hash_sha256"}
        }
    )
    return event, feature


def ingest_catalyst_evidence(
    *,
    db_path: str,
    evidence_root: str,
    events: list[dict[str, Any]],
    extractions: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    store = CatalystEvidenceStore(db_path, evidence_root=evidence_root)
    event_counts = [store.persist_event(event) for event in events]
    extraction_counts = [
        store.persist_extraction(extraction) for extraction in list(extractions or [])
    ]
    return {
        "event_count": len(events),
        "extraction_count": len(list(extractions or [])),
        "event_inserted": sum(row["inserted"] for row in event_counts),
        "extraction_inserted": sum(row["inserted"] for row in extraction_counts),
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _is_available(
    published_at: str | None,
    *,
    first_seen_at: str,
    available_at: str,
    decision_at: str | None,
) -> bool:
    if not decision_at:
        return False
    published = _parse_datetime(published_at) if published_at else None
    first_seen = _parse_datetime(first_seen_at)
    available = _parse_datetime(available_at)
    decision = _parse_datetime(decision_at)
    if first_seen is None or available is None or decision is None:
        return False
    if published is not None and published > first_seen:
        return False
    if available < first_seen:
        return False
    return first_seen <= decision and available <= decision


def _parse_datetime(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return None if parsed.tzinfo is None else parsed


def _event_type(content: str) -> str:
    text = content.lower()
    for marker, event_type in (
        ("reverse split", "reverse_split"),
        ("warrant", "warrant"),
        ("offering", "offering"),
        ("shelf", "financing"),
        ("earnings", "earnings"),
        ("fda", "regulatory"),
        ("clinical", "clinical"),
        ("contract", "contract"),
    ):
        if marker in text:
            return event_type
    return "unclassified"


def _polarity(content: str) -> str:
    text = content.lower()
    if any(word in text for word in ("terminated", "failed", "loss", "delisting")):
        return "negative_mechanism"
    if any(word in text for word in ("approved", "agreement", "contract", "revenue")):
        return "positive_mechanism"
    return "unknown"


def _financing_mechanism(content: str) -> str:
    text = content.lower()
    for marker, label in (
        ("at-the-market", "atm"),
        ("takedown", "takedown"),
        ("warrant", "warrant"),
        ("shelf", "shelf"),
        ("offering", "registered_offering"),
    ):
        if marker in text:
            return label
    return "none_or_unknown"


def _promotional_status(content: str) -> str:
    text = content.lower()
    return (
        "promotional_language_present"
        if any(word in text for word in ("guaranteed", "risk-free", "squeeze now"))
        else "not_detected"
    )


def _rumor_status(content: str) -> str:
    text = content.lower()
    return (
        "rumor_language_present"
        if any(word in text for word in ("rumor", "reportedly", "unconfirmed"))
        else "not_detected"
    )


def _squeeze_mechanics(content: str) -> str:
    text = content.lower()
    return (
        "squeeze_mechanics_mentioned"
        if any(word in text for word in ("short interest", "borrow", "utilization"))
        else "not_detected"
    )


__all__ = [
    "CATALYST_EVENT_TYPES",
    "build_catalyst_evidence_event",
    "build_filing_catalyst_event",
    "build_news_catalyst_events",
    "ingest_catalyst_evidence",
]
