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

CONDITION_CLAIM_TYPES: dict[str, frozenset[str]] = {
    "company_identity": frozenset({"entity_identity"}),
    "sector_industry": frozenset({"sector_industry"}),
    "breakout_catalyst": frozenset({"catalyst_event"}),
    "catalyst_identified": frozenset({"catalyst_event"}),
    "catalyst_event": frozenset({"catalyst_event"}),
    "catalyst_timing": frozenset({"catalyst_event", "earnings_window"}),
    "earnings_window": frozenset({"earnings_window"}),
    "offering_or_dilution": frozenset({"offering_or_dilution"}),
    "corporate_action": frozenset({"corporate_action"}),
    "corporate_action_basis": frozenset({"corporate_action"}),
    "regulatory_event": frozenset({"regulatory_halt_notice", "material_adverse_event"}),
    "material_adverse_event": frozenset({"material_adverse_event"}),
    "recent_filing_risk": frozenset(
        {"offering_or_dilution", "corporate_action", "material_adverse_event"}
    ),
    "squeeze_event": frozenset({"squeeze_event"}),
}
_AUTHORITATIVE_CONDITION_TYPES = frozenset(
    {"regulatory_halt_notice", "corporate_action", "offering_or_dilution"}
)


def source_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def validate_claim(
    claim: dict[str, Any],
    *,
    symbol: str,
    decision_at: str,
    allowed_condition_ids: set[str] | frozenset[str] | None = None,
) -> EvidenceClaim | None:
    """Return a typed claim only if it is cited, point-in-time, and safe."""

    if any(key.lower() in FORBIDDEN_FIELDS for key in claim):
        return None
    claim_type = str(claim.get("claim_type") or "").strip()
    if claim_type not in ALLOWED_CLAIM_TYPES:
        return None
    condition_id = str(claim.get("condition_id") or "").strip()
    if not condition_id or (
        allowed_condition_ids is not None and condition_id not in allowed_condition_ids
    ):
        return None
    expected_types = CONDITION_CLAIM_TYPES.get(condition_id)
    if expected_types is not None and claim_type not in expected_types:
        return None
    if str(claim.get("symbol") or "").strip().upper() != symbol.strip().upper():
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
    effective = claim.get("effective_at")
    if effective:
        try:
            effective_dt = datetime.fromisoformat(str(effective).replace("Z", "+00:00"))
        except ValueError:
            return None
        if effective_dt.tzinfo is None or effective_dt > decision_dt:
            return None
    raw_hashes = claim.get("source_hashes")
    hashes = tuple(str(item) for item in raw_hashes or ())
    if raw_hashes in (None, ()) or len(hashes) == 0:
        hashes = tuple(source_hash(url) for url in urls)
    elif len(hashes) != len(urls):
        return None
    authoritative = claim.get("authoritative")
    supported = claim.get("supported", True)
    if not isinstance(authoritative, bool) or not isinstance(supported, bool):
        return None
    if claim_type in _AUTHORITATIVE_CONDITION_TYPES and not authoritative:
        return None
    try:
        return EvidenceClaim(
            claim_id=str(claim.get("claim_id") or "claim-" + source_hash(statement)[:16]),
            symbol=symbol,
            condition_id=condition_id,
            claim_type=claim_type,
            statement=statement,
            source_urls=urls,
            source_hashes=hashes,
            published_at=published,
            effective_at=effective,
            authoritative=authoritative,
            supported=supported,
        )
    except (KeyError, TypeError, ValueError):
        return None


def result_for_claim(
    condition_id: str,
    claim: EvidenceClaim | None,
    *,
    reason: str,
    requested_model: str = "",
    actual_model: str = "",
    confidence: float | None = None,
    contradictions: tuple[str, ...] = (),
) -> ConditionResult:
    if claim is None:
        return ConditionResult(
            condition_id,
            ConditionStatus.MISSING_DISCLOSED,
            reason=reason,
            resolver_id="strategy_gap_resolver",
            resolution_method="cited_public_source",
            requested_model=requested_model,
            actual_model=actual_model,
            confidence=confidence,
            contradictions=contradictions,
        )
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
        requested_model=requested_model,
        actual_model=actual_model,
        confidence=confidence,
        contradictions=contradictions,
    )


__all__ = [
    "ALLOWED_CLAIM_TYPES",
    "CONDITION_CLAIM_TYPES",
    "FORBIDDEN_FIELDS",
    "result_for_claim",
    "source_hash",
    "validate_claim",
]
