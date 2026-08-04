"""Immutable scenario records and strict boundary validation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

SCENARIO_POLICY_VERSION = "dawnstrike-news-scenario-v1"
SCENARIO_FEATURE_SCHEMA_VERSION = "dawnstrike-news-scenario-features-v1"
SCENARIO_EXTRACTION_SCHEMA_VERSION = "dawnstrike-news-extraction-v3"
SCENARIO_PROMPT_VERSION = "dawnstrike-news-extraction-prompt-v4"
SCENARIO_STRATEGY_ID = "news_scenario_v1"
SCENARIO_FORWARD_COHORT = "scenario_forward"
SCENARIO_REPLAY_COHORT = "scenario_historical_replay"

EVENT_TYPES = {
    "earnings_guidance",
    "financing_dilution",
    "regulatory_fda",
    "contract_customer",
    "mergers_acquisitions",
    "management_change",
    "litigation",
    "analyst_action",
    "product_event",
    "cybersecurity",
    "recall",
    "macro_sector",
    "exchange_halt",
    "bankruptcy_distress",
    "rumor",
    "other",
}
CLAIM_STATUSES = {
    "verified_fact",
    "company_claim",
    "attributed_third_party_claim",
    "rumor",
    "opinion",
    "unclear",
}
MECHANISM_POLARITIES = {"positive", "negative", "mixed", "unclear"}
FORBIDDEN_EXTRACTION_KEYS = {
    "action",
    "allocation",
    "bull_probability",
    "trade_action",
    "confidence",
    "confidence_score",
    "direction",
    "downside",
    "recommendation",
    "rating",
    "buy",
    "sell",
    "short",
    "long",
    "target",
    "target_price",
    "entry",
    "entry_level",
    "exit",
    "exit_level",
    "invalidation_level",
    "stop_loss",
    "take_profit",
    "probability",
    "odds",
    "likelihood",
    "expected_return",
    "projected_return",
    "return_pct",
    "position_size",
    "sizing",
    "upside",
}
FORBIDDEN_EXTRACTION_TEXT = (
    (
        "recommendation",
        re.compile(
            r"\b(?:buy|sell|hold|short|long|overweight|underweight|outperform|"
            r"underperform)\b.{0,32}\b(?:rating|recommendation|signal|call)\b|"
            r"\b(?:rating|recommendation|signal|call)\b.{0,32}\b(?:buy|sell|hold|"
            r"short|long|overweight|underweight|outperform|underperform)\b|"
            r"\b(?:rate|rates|rated|recommend(?:s|ed|ing)?)\b.{0,32}\b(?:buy|sell|"
            r"hold|short|long|overweight|underweight|outperform|underperform)\b|"
            r"\b(?:should|must)\s+(?:buy|sell|hold|short|go\s+long|go\s+short)\b|"
            r"\b(?:enter|exit)\s+(?:a\s+)?(?:trade|position|long|short)\b|"
            r"^\s*(?:buy|sell|short|hold|go\s+long|go\s+short)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "price-level",
        re.compile(
            r"\b(?:price\s+target|target\s+price|entry\s+(?:price|level|trigger)|"
            r"exit\s+(?:price|level|trigger)|stop[ -]?loss|take[ -]?profit|"
            r"(?:entry|exit)\s+(?:at|above|below|near)\s+[$\d]|"
            r"invalidation\s+(?:price|level)|target\s+(?:of|at)\s+[$\d])\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "probability",
        re.compile(
            r"\b(?:probabilit(?:y|ies)|likelihood|odds|chance\s+of|"
            r"percent\s+(?:likely|chance))\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "return",
        re.compile(
            r"\b(?:(?:expected|projected|estimated|forecast)\s+returns?|"
            r"returns?\s+of\s+[-+]?\d|[-+]?\d+(?:\.\d+)?\s*%\s+"
            r"(?:upside|downside|return)|return\s+on\s+investment|roi)\b",
            flags=re.IGNORECASE,
        ),
    ),
    (
        "position-sizing",
        re.compile(
            r"\b(?:position\s+siz(?:e|ing)|portfolio\s+allocation|allocate\s+"
            r"\d|shares?\s+to\s+(?:buy|sell)|notional\s+(?:size|amount))\b",
            flags=re.IGNORECASE,
        ),
    ),
)


def utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def canonical_hash(value: Any) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def source_tier(source: str, source_url: str = "") -> str:
    value = f"{source} {source_url}".lower()
    if any(
        token in value
        for token in (
            "sec.gov",
            "businesswire",
            "business wire",
            "globenewswire",
            "globe newswire",
            "prnewswire",
            "pr newswire",
            "company",
            "investor",
        )
    ):
        return "T1"
    if any(
        token in value
        for token in ("reuters", "bloomberg", "associated press", "wsj", "cnbc", "marketwatch")
    ):
        return "T2"
    if source.strip() or source_url.strip():
        return "T3"
    return "UNKNOWN"


def _reject_forbidden(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = sorted({str(key).lower() for key in value} & FORBIDDEN_EXTRACTION_KEYS)
        if forbidden:
            raise ValueError(
                "Extraction contains forbidden decision field(s): " + ", ".join(forbidden)
            )
        for nested in value.values():
            _reject_forbidden(nested)
    elif isinstance(value, list):
        for nested in value:
            _reject_forbidden(nested)
    elif isinstance(value, str):
        for label, pattern in FORBIDDEN_EXTRACTION_TEXT:
            if pattern.search(value):
                raise ValueError(f"Extraction contains forbidden {label} content")


@dataclass(frozen=True)
class ScenarioNewsArticle:
    article_id: str
    symbols: tuple[str, ...]
    headline: str
    summary: str
    content: str
    source: str
    source_url: str
    created_at: str
    updated_at: str = ""
    author: str = ""
    provider: str = "alpaca"
    fetched_at: str = field(default_factory=utc_now_iso)
    first_seen_at: str = field(default_factory=utc_now_iso)
    timing_kind: str = "forward_observed"

    @property
    def tier(self) -> str:
        return source_tier(self.source, self.source_url)

    @property
    def content_hash_sha256(self) -> str:
        return canonical_hash(
            {"headline": self.headline, "summary": self.summary, "content": self.content}
        )

    @property
    def source_lineage_hash_sha256(self) -> str:
        return canonical_hash(
            {
                "provider": self.provider,
                "article_id": self.article_id,
                "symbols": self.symbols,
                "source": self.source,
                "source_url": self.source_url,
                "created_at": self.created_at,
                "updated_at": self.updated_at,
                "timing_kind": self.timing_kind,
            }
        )

    def as_dict(self, *, include_content: bool = True) -> dict[str, Any]:
        payload = asdict(self)
        payload["symbols"] = list(self.symbols)
        payload["source_tier"] = self.tier
        payload["content_hash_sha256"] = self.content_hash_sha256
        payload["source_lineage_hash_sha256"] = self.source_lineage_hash_sha256
        if not include_content:
            payload.pop("content", None)
        return payload


@dataclass(frozen=True)
class ScenarioClaim:
    event_type: str
    mechanism_polarity: str
    factual_claim: str
    evidence_spans: tuple[str, ...]
    materiality: str
    uncertainty_flags: tuple[str, ...] = ()
    claim_status: str = "unclear"
    causal_mechanism: str = ""
    affected_business_variable: str = ""
    horizon: str = "unknown"
    novelty: str = "unknown"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ScenarioClaim:
        event_type = str(value.get("event_type") or "").lower()
        if event_type not in EVENT_TYPES:
            raise ValueError("Claim event_type is missing or invalid")
        mechanism_polarity = str(value.get("mechanism_polarity") or "").lower()
        if mechanism_polarity not in MECHANISM_POLARITIES:
            raise ValueError("Claim mechanism_polarity is missing or invalid")
        materiality = str(value.get("materiality") or "").lower()
        if materiality not in {"high", "medium", "low", "unknown"}:
            raise ValueError("Claim materiality is missing or invalid")
        claim_status = str(value.get("claim_status") or "").lower()
        if claim_status not in CLAIM_STATUSES:
            raise ValueError("Claim claim_status is missing or invalid")
        horizon = str(value.get("horizon") or "").lower()
        if horizon not in {"immediate", "near_term", "medium_term", "long_term", "unknown"}:
            raise ValueError("Claim horizon is missing or invalid")
        novelty = str(value.get("novelty") or "").lower()
        if novelty not in {"new", "known_update", "restatement", "unknown"}:
            raise ValueError("Claim novelty is missing or invalid")
        factual_claim = str(value.get("factual_claim") or "").strip()
        evidence_spans = tuple(
            str(item).strip() for item in value.get("evidence_spans", []) if str(item).strip()
        )
        causal_mechanism = str(value.get("causal_mechanism") or "").strip()
        affected_business_variable = str(value.get("affected_business_variable") or "").strip()
        if not factual_claim or not evidence_spans:
            raise ValueError("Claim requires a factual_claim and supporting evidence")
        if not causal_mechanism or not affected_business_variable:
            raise ValueError("Claim mechanism fields must be explicit, using unclear if unknown")
        return cls(
            event_type=event_type,
            mechanism_polarity=mechanism_polarity,
            factual_claim=factual_claim,
            evidence_spans=evidence_spans,
            materiality=materiality,
            uncertainty_flags=tuple(
                str(item).strip()
                for item in value.get("uncertainty_flags", [])
                if str(item).strip()
            ),
            claim_status=claim_status,
            causal_mechanism=causal_mechanism,
            affected_business_variable=affected_business_variable,
            horizon=horizon,
            novelty=novelty,
        )


@dataclass(frozen=True)
class ScenarioExtraction:
    extraction_id: str
    article_id: str
    status: str
    claims: tuple[ScenarioClaim, ...]
    abstain_reason: str = ""
    model: str = ""
    requested_model: str = ""
    response_id: str = ""
    prompt_version: str = SCENARIO_PROMPT_VERSION
    schema_version: str = SCENARIO_EXTRACTION_SCHEMA_VERSION
    input_hash_sha256: str = ""
    output_hash_sha256: str = ""
    created_at: str = field(default_factory=utc_now_iso)
    usage: dict[str, Any] = field(default_factory=dict)
    prompt_injection_detected: bool = False
    contradictions: tuple[str, ...] = ()
    dependencies: tuple[str, ...] = ()
    unresolved_unknowns: tuple[str, ...] = ()

    @classmethod
    def from_dict(
        cls,
        *,
        article_id: str,
        value: dict[str, Any],
        model: str,
        requested_model: str = "",
        response_id: str = "",
        usage: dict[str, Any] | None = None,
    ) -> ScenarioExtraction:
        _reject_forbidden(value)
        status = str(value.get("status") or "abstain").lower()
        if status not in {"ok", "abstain", "rejected"}:
            raise ValueError("Extraction status must be ok, abstain, or rejected")
        claims = tuple(
            ScenarioClaim.from_dict(item)
            for item in value.get("claims", [])
            if isinstance(item, dict)
        )
        reason = str(value.get("abstain_reason") or "").strip()
        if status == "ok" and not claims:
            raise ValueError("Successful extraction must contain at least one factual claim")
        if status != "ok" and claims:
            raise ValueError("Failed extraction must not contain claims")
        actual_model = model.strip()
        requested = requested_model.strip() or actual_model
        if status == "ok" and not actual_model:
            raise ValueError("Successful extraction requires the actual returned model identifier")
        if status != "ok" and not reason:
            reason = "extractor_abstained"
        output_hash = canonical_hash(value)
        return cls(
            extraction_id=canonical_hash(
                {
                    "article_id": article_id,
                    "output": output_hash,
                    "model": actual_model,
                    "requested_model": requested,
                }
            )[:32],
            article_id=article_id,
            status=status,
            claims=claims,
            abstain_reason=reason,
            model=actual_model,
            requested_model=requested,
            response_id=response_id,
            input_hash_sha256=str(value.get("input_hash_sha256") or ""),
            output_hash_sha256=output_hash,
            usage=dict(usage or {}),
            prompt_injection_detected=bool(value.get("prompt_injection_detected")),
            contradictions=tuple(
                str(item).strip() for item in value.get("contradictions", []) if str(item).strip()
            ),
            dependencies=tuple(
                str(item).strip() for item in value.get("dependencies", []) if str(item).strip()
            ),
            unresolved_unknowns=tuple(
                str(item).strip()
                for item in value.get("unresolved_unknowns", [])
                if str(item).strip()
            ),
        )

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["claims"] = [asdict(claim) for claim in self.claims]
        return payload


@dataclass(frozen=True)
class ScenarioDecision:
    decision_id: str
    article_id: str
    ticker: str
    market_date: str
    decision_at: str
    event_type: str
    direction: str
    directional_evidence_score: float
    action: str
    reason_codes: tuple[str, ...]
    source_tier: str
    source_lineage_hash_sha256: str
    feature_hash_sha256: str
    features: dict[str, Any]
    entry_trigger: float | None = None
    invalidation_level: float | None = None
    target_1: float | None = None
    time_stop: str = "market_close"
    calibration_status: str = "UNCALIBRATED"
    policy_version: str = SCENARIO_POLICY_VERSION
    feature_schema_version: str = SCENARIO_FEATURE_SCHEMA_VERSION
    cohort: str = SCENARIO_FORWARD_COHORT
    research_only: bool = True
    broker_execution_enabled: bool = False

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["reason_codes"] = list(self.reason_codes)
        return payload
