"""Validation helpers for contextual evidence returned by providers or AI."""

from __future__ import annotations

import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

from intraday_scanner.decisioning.contracts import ConditionResult, ConditionStatus, EvidenceClaim

ALLOWED_CLAIM_TYPES = frozenset(
    {
        "entity_identity",
        "catalyst_event",
        "offering_or_dilution",
        "corporate_action",
        "regulatory_halt_notice",
        "sector_industry",
        "earnings_window",
        "material_adverse_event",
        "squeeze_event",
    }
)
FORBIDDEN_FIELDS = frozenset(
    {
        "price",
        "volume",
        "vwap",
        "spread",
        "float",
        "float_value",
        "gap_percentage",
        "entry",
        "stop",
        "target",
        "reward_risk",
        "expected_return",
        "probability",
        "position_size",
        "buy",
        "sell",
        "short",
        "recommendation",
    }
)


def source_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def validate_claim(
    claim: dict[str, Any],
    *,
    symbol: str,
    decision_at: str,
) -> EvidenceClaim | None:
    """Return a typed claim only if it is cited, point-in-time, and safe."""

    if any(key.lower() in FORBIDDEN_FIELDS for key in claim):
        return None
    if str(claim.get("claim_type") or "") not in ALLOWED_CLAIM_TYPES:
        return None
    if str(claim.get("symbol") or "").upper() != symbol.upper():
        return None
    statement = str(claim.get("statement") or "").strip()
    lowered = statement.lower()
    if not statement or any(
        token in lowered
        for token in ("ignore previous", "system message", "reveal prompt", "api key")
    ):
        return None
    urls = tuple(str(url).strip() for url in claim.get("source_urls") or () if str(url).strip())
    if not urls or any(
        urlsplit(url).scheme not in {"http", "https"} or not urlsplit(url).netloc for url in urls
    ):
        return None
    published = str(claim.get("published_at") or "")
    try:
        published_dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
        decision_dt = datetime.fromisoformat(decision_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if published_dt.tzinfo is None or decision_dt.tzinfo is None or published_dt > decision_dt:
        return None
    hashes = tuple(str(item) for item in claim.get("source_hashes") or ())
    if len(hashes) != len(urls):
        hashes = tuple(source_hash(url) for url in urls)
    try:
        return EvidenceClaim(
            claim_id=str(claim.get("claim_id") or "claim-" + source_hash(statement)[:16]),
            symbol=symbol,
            condition_id=str(claim.get("condition_id") or ""),
            claim_type=str(claim["claim_type"]),
            statement=statement,
            source_urls=urls,
            source_hashes=hashes,
            published_at=published,
            effective_at=claim.get("effective_at"),
            authoritative=bool(claim.get("authoritative")),
            supported=bool(claim.get("supported", True)),
        )
    except (KeyError, TypeError, ValueError):
        return None


def result_for_claim(
    condition_id: str, claim: EvidenceClaim | None, *, reason: str
) -> ConditionResult:
    if claim is None:
        return ConditionResult(condition_id, ConditionStatus.MISSING_DISCLOSED, reason=reason)
    status = ConditionStatus.RESOLVED_FROM_SOURCE if claim.supported else ConditionStatus.FAIL
    return ConditionResult(
        condition_id,
        status,
        observed_value=claim.statement,
        reason=reason,
        source_urls=claim.source_urls,
        source_hashes=claim.source_hashes,
        effective_at=claim.effective_at or claim.published_at,
        resolver_id="strategy_gap_resolver",
        resolution_method="cited_public_source",
    )


__all__ = [
    "ALLOWED_CLAIM_TYPES",
    "FORBIDDEN_FIELDS",
    "result_for_claim",
    "source_hash",
    "validate_claim",
]
