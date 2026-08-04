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
SCENARIO_EXTRACTION_SCHEMA_VERSION = "dawnstrike-news-extraction-v2"
SCENARIO_PROMPT_VERSION = "dawnstrike-news-extraction-prompt-v3"
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
FORBIDDEN_EXTRACTION_KEYS = {
    "action",
    "trade_action",
    "recommendation",
    "buy",
    "sell",
    "short",
    "target",
    "target_price",
    "entry",
    "exit",
    "probability",
    "expected_return",
    "position_size",
    "sizing",
}
FORBIDDEN_EXTRACTION_TEXT = re.compile(
    r"\b(?:price\s+target|target\s+price)\b",
    flags=re.IGNORECASE,
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
            "globenewswire",
            "prnewswire",
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
    elif isinstance(value, str) and FORBIDDEN_EXTRACTION_TEXT.search(value):
        raise ValueError("Extraction contains forbidden price-target content")


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
    direction: str
    factual_claim: str
    evidence_spans: tuple[str, ...]
    materiality: str
    uncertainty_flags: tuple[str, ...] = ()
    claim_status: str = "unknown"
    causal_mechanism: str = ""
    affected_business_variable: str = ""
    horizon: str = "unknown"
    novelty: str = "unknown"

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ScenarioClaim:
        event_type = str(value.get("event_type") or "other").lower()
        if event_type not in EVENT_TYPES:
            event_type = "other"
        direction = str(value.get("direction") or "unknown").lower()
        if direction not in {"bullish", "bearish", "mixed", "unknown"}:
            direction = "unknown"
        materiality = str(value.get("materiality") or "unknown").lower()
        if materiality not in {"high", "medium", "low", "unknown"}:
            materiality = "unknown"
        claim_status = str(value.get("claim_status") or "unknown").lower()
        if claim_status not in {"confirmed", "reported", "rumor", "disputed", "unknown"}:
            claim_status = "unknown"
        horizon = str(value.get("horizon") or "unknown").lower()
        if horizon not in {"immediate", "near_term", "medium_term", "long_term", "unknown"}:
            horizon = "unknown"
        novelty = str(value.get("novelty") or "unknown").lower()
        if novelty not in {"new", "known_update", "restatement", "unknown"}:
            novelty = "unknown"
        return cls(
            event_type=event_type,
            direction=direction,
            factual_claim=str(value.get("factual_claim") or "").strip(),
            evidence_spans=tuple(
                str(item).strip() for item in value.get("evidence_spans", []) if str(item).strip()
            ),
            materiality=materiality,
            uncertainty_flags=tuple(
                str(item).strip()
                for item in value.get("uncertainty_flags", [])
                if str(item).strip()
            ),
            claim_status=claim_status,
            causal_mechanism=str(value.get("causal_mechanism") or "").strip(),
            affected_business_variable=str(
                value.get("affected_business_variable") or ""
            ).strip(),
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
        if status != "ok" and not reason:
            reason = "extractor_abstained"
        output_hash = canonical_hash(value)
        return cls(
            extraction_id=canonical_hash(
                {"article_id": article_id, "output": output_hash, "model": model}
            )[:32],
            article_id=article_id,
            status=status,
            claims=claims,
            abstain_reason=reason,
            model=model,
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
