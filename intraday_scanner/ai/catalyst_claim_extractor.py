"""Strict factual catalyst extraction with source-hash cache identity."""

from __future__ import annotations

from typing import Any

from intraday_scanner.ai.scenario_claim_extractor import extract_claims
from intraday_scanner.scenario.contracts import ScenarioNewsArticle, canonical_hash

CATALYST_PROMPT_VERSION = "dawnstrike-catalyst-fact-only-v1"
CATALYST_SCHEMA_VERSION = "dawnstrike.catalyst_claim_extraction.v1"
FORBIDDEN_OUTPUT_FIELDS = frozenset(
    {
        "score",
        "grade",
        "probability",
        "prediction",
        "trade_direction",
        "target",
        "stop",
        "size",
        "recommendation",
        "expected_return",
    }
)


def catalyst_input_hash(
    article: ScenarioNewsArticle,
    *,
    model: str,
    max_article_chars: int,
) -> str:
    return canonical_hash(
        {
            "source_content_hash_sha256": article.content_hash_sha256,
            "source_lineage_hash_sha256": article.source_lineage_hash_sha256,
            "prompt_version": CATALYST_PROMPT_VERSION,
            "schema_version": CATALYST_SCHEMA_VERSION,
            "model": model,
            "max_article_chars": max_article_chars,
        }
    )


def extract_catalyst_claims(
    *,
    article: ScenarioNewsArticle,
    api_key: str,
    model: str,
    timeout_seconds: float,
    max_article_chars: int,
    client: Any | None = None,
) -> dict[str, Any]:
    """Return factual claims or an explicit abstention; never a trade action."""

    input_hash = catalyst_input_hash(
        article,
        model=model,
        max_article_chars=max_article_chars,
    )
    if _looks_injected(article.content) or _looks_injected(article.headline):
        return _rejected(
            article,
            input_hash=input_hash,
            reason="prompt_injection_detected",
        )
    extraction = extract_claims(
        article=article,
        api_key=api_key,
        model=model,
        timeout_seconds=timeout_seconds,
        max_article_chars=max_article_chars,
        client=client,
    )
    payload = extraction.as_dict()
    claims = []
    for claim in payload.get("claims", []):
        if not isinstance(claim, dict):
            continue
        claim_payload = {
            **claim,
            "evidence_spans": list(claim.get("evidence_spans", [])),
            "uncertainty_flags": list(claim.get("uncertainty_flags", [])),
            "financing_mechanism": _financing_mechanism(claim),
            "promotional_status": _promotional_status(article),
            "rumor_status": _rumor_status(article),
            "squeeze_mechanics": _squeeze_mechanics(claim),
            "source_coverage_status": "single_source_verified_span",
            "confidence_status": "fact_claim_status_only",
        }
        if _forbidden_keys(claim_payload):
            return _rejected(
                article,
                input_hash=input_hash,
                reason="forbidden_decision_field",
            )
        claims.append(claim_payload)
    status = str(payload.get("status") or "abstain")
    return {
        "extraction_id": canonical_hash(
            {
                "article_id": article.article_id,
                "input_hash_sha256": input_hash,
                "output_hash_sha256": extraction.output_hash_sha256,
            }
        )[:32],
        "article_id": article.article_id,
        "status": status,
        "claims": claims if status == "ok" else [],
        "abstain_reason": str(payload.get("abstain_reason") or ""),
        "prompt_injection_detected": bool(payload.get("prompt_injection_detected")),
        "source_content_hash_sha256": article.content_hash_sha256,
        "input_hash_sha256": input_hash,
        "output_hash_sha256": canonical_hash({"status": status, "claims": claims}),
        "prompt_version": CATALYST_PROMPT_VERSION,
        "schema_version": CATALYST_SCHEMA_VERSION,
        "model": extraction.model,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _rejected(
    article: ScenarioNewsArticle, *, input_hash: str, reason: str
) -> dict[str, Any]:
    return {
        "extraction_id": canonical_hash(
            {"article_id": article.article_id, "input": input_hash, "reason": reason}
        )[:32],
        "article_id": article.article_id,
        "status": "rejected",
        "claims": [],
        "abstain_reason": reason,
        "prompt_injection_detected": reason == "prompt_injection_detected",
        "source_content_hash_sha256": article.content_hash_sha256,
        "input_hash_sha256": input_hash,
        "output_hash_sha256": canonical_hash({"status": "rejected", "reason": reason}),
        "prompt_version": CATALYST_PROMPT_VERSION,
        "schema_version": CATALYST_SCHEMA_VERSION,
        "model": "",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _forbidden_keys(value: dict[str, Any]) -> set[str]:
    return {str(key).lower() for key in value} & FORBIDDEN_OUTPUT_FIELDS


def _looks_injected(text: str) -> bool:
    normalized = text.lower()
    return any(
        marker in normalized
        for marker in (
            "ignore previous instructions",
            "system message",
            "call a tool",
            "output a buy",
        )
    )


def _financing_mechanism(claim: dict[str, Any]) -> str:
    text = " ".join(
        str(claim.get(key) or "") for key in ("factual_claim", "causal_mechanism")
    ).lower()
    for marker, label in (
        ("at-the-market", "atm"),
        ("shelf", "shelf"),
        ("warrant", "warrant"),
        ("offering", "registered_offering"),
        ("takedown", "takedown"),
    ):
        if marker in text:
            return label
    return "none_or_unknown"


def _promotional_status(article: ScenarioNewsArticle) -> str:
    text = f"{article.headline} {article.content}".lower()
    return (
        "promotional_language_present"
        if any(
            word in text for word in ("guaranteed", "risk-free", "squeeze now")
        )
        else "not_detected"
    )


def _rumor_status(article: ScenarioNewsArticle) -> str:
    text = f"{article.headline} {article.content}".lower()
    return (
        "rumor_language_present"
        if any(
            word in text for word in ("rumor", "reportedly", "unconfirmed")
        )
        else "not_detected"
    )


def _squeeze_mechanics(claim: dict[str, Any]) -> str:
    text = " ".join(
        str(claim.get(key) or "") for key in ("factual_claim", "causal_mechanism")
    ).lower()
    return (
        "squeeze_mechanics_mentioned"
        if any(
            word in text for word in ("short interest", "borrow", "utilization")
        )
        else "not_detected"
    )


__all__ = [
    "CATALYST_PROMPT_VERSION",
    "CATALYST_SCHEMA_VERSION",
    "catalyst_input_hash",
    "extract_catalyst_claims",
]
