"""Deterministic, point-in-time Scenario article prefilter.

This module is deliberately independent of providers and language models.  It
only applies versioned metadata/content contracts at decision time.  Passing
the prefilter means *eligible for factual extraction*, never a trade.
"""

from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from typing import Any

from intraday_scanner.scenario import contracts as _contracts_module
from intraday_scanner.scenario import point_in_time as _point_in_time_module
from intraday_scanner.scenario.contracts import ScenarioNewsArticle, canonical_hash
from intraday_scanner.scenario.point_in_time import parse_aware_timestamp

SCENARIO_PREFILTER_POLICY_VERSION = "dawnstrike-scenario-prefilter-v1"
SCENARIO_PREFILTER_CONFIG_VERSION = "dawnstrike-scenario-prefilter-config-v1"
DEFAULT_MAX_AGE_SECONDS = 24 * 60 * 60
DEFAULT_MIN_TEXT_CHARS = 40
SUPPORTED_SOURCE_TIERS = frozenset({"T1", "T2"})
_SYMBOL = re.compile(r"^[A-Z][A-Z0-9.-]{0,9}$")
_URL = re.compile(r"^https?://[^\s]+$", re.IGNORECASE)
# These patterns are deliberately narrow.  A missing keyword is not evidence
# that an otherwise sufficiently detailed factual article is immaterial.
_EXPLICIT_NON_MATERIAL_PATTERNS = (
    re.compile(r"^\s*(?:advertisement|sponsored content|promotional offer)\s*$", re.I),
)


@dataclass(frozen=True, slots=True)
class ScenarioPrefilterObservation:
    """Immutable prospective observation; it is never a trading decision."""

    candidate_id: str
    decision_id: str
    article_id: str
    decision_at: str
    status: str
    reason_codes: tuple[str, ...]
    content_hash_sha256: str
    source_lineage_hash_sha256: str
    policy_version: str = SCENARIO_PREFILTER_POLICY_VERSION
    non_trade_observation: bool = True

    def as_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "decision_id": self.decision_id,
            "article_id": self.article_id,
            "decision_at": self.decision_at,
            "prefilter_decision": self.status,
            "reason_codes": list(self.reason_codes),
            "content_hash_sha256": self.content_hash_sha256,
            "source_lineage_hash_sha256": self.source_lineage_hash_sha256,
            "observation_policy": self.policy_version,
            "non_trade_observation": self.non_trade_observation,
            "research_only": True,
            "broker_execution_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class ScenarioPrefilterResult:
    """Sorted prefilter output and its prospective non-trade observations."""

    eligible_articles: tuple[ScenarioNewsArticle, ...]
    observations: tuple[ScenarioPrefilterObservation, ...]
    decision_at: str
    config: dict[str, Any]
    config_hash_sha256: str

    @property
    def counts(self) -> dict[str, int]:
        counts: dict[str, int] = defaultdict(int)
        for observation in self.observations:
            counts[observation.status] += 1
        return dict(sorted(counts.items()))


def prefilter_scenario_articles(
    articles: Iterable[ScenarioNewsArticle],
    *,
    decision_at: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
    supported_source_tiers: Iterable[str] = SUPPORTED_SOURCE_TIERS,
) -> ScenarioPrefilterResult:
    """Apply deterministic article checks before any OpenAI extraction.

    Input order is intentionally irrelevant.  Invalid decision timestamps
    produce ABSTAIN observations and no eligible articles.  No market values,
    probabilities, returns, or actions are inferred here.
    """

    decision = parse_aware_timestamp(decision_at)
    if decision is None:
        normalized_decision_at = str(decision_at or "")
    else:
        normalized_decision_at = _iso(decision)
    max_age_seconds = _strict_nonnegative_int(max_age_seconds, "max_age_seconds")
    min_text_chars = _strict_positive_int(min_text_chars, "min_text_chars")
    tiers = _strict_source_tiers(supported_source_tiers)
    policy_config = {
        "config_version": SCENARIO_PREFILTER_CONFIG_VERSION,
        "policy_version": SCENARIO_PREFILTER_POLICY_VERSION,
        "max_age_seconds": max_age_seconds,
        "min_text_chars": min_text_chars,
        "supported_source_tiers": sorted(tiers),
    }
    config_hash = canonical_hash(policy_config)
    material: list[tuple[tuple[str, ...], ScenarioNewsArticle]] = []
    observations: list[ScenarioPrefilterObservation] = []
    raw_articles = list(articles)
    # Validate every record before duplicate selection.  A stale or malformed
    # copy must never reserve an identity and poison a valid copy.
    ordered = sorted(raw_articles, key=_stable_sort_key)
    records: list[dict[str, Any]] = []
    duplicate_ordinals: dict[tuple[str, str, str], int] = defaultdict(int)
    for article in ordered:
        content_hash, lineage_hash = _safe_hashes(article)
        duplicate_key = (
            _text(article, "article_id"),
            content_hash,
            lineage_hash,
        )
        ordinal = duplicate_ordinals[duplicate_key]
        duplicate_ordinals[duplicate_key] += 1
        candidate_id = _candidate_id(article, content_hash, lineage_hash, ordinal)
        decision_id = canonical_hash(
            {
                "candidate_id": candidate_id,
                "decision_at": normalized_decision_at,
                "policy_version": SCENARIO_PREFILTER_POLICY_VERSION,
                "config_hash_sha256": config_hash,
            }
        )[:32]
        reasons = _violations(
            article,
            decision=decision,
            max_age_seconds=max_age_seconds,
            min_text_chars=min_text_chars,
            supported_source_tiers=tiers,
        )
        records.append(
            {
                "article": article,
                "content_hash": content_hash,
                "lineage_hash": lineage_hash,
                "candidate_id": candidate_id,
                "decision_id": decision_id,
                "reasons": set(reasons),
                "identity_keys": {
                    ("article_id", _text(article, "article_id")),
                    ("content_hash", content_hash),
                    ("lineage_hash", lineage_hash),
                },
            }
        )

    components = _duplicate_components(records)
    selected: set[int] = set()
    for component in components:
        eligible_indexes = [index for index in component if not records[index]["reasons"]]
        if eligible_indexes:
            winner = min(
                eligible_indexes, key=lambda index: _representative_sort_key(records[index])
            )
            selected.add(winner)
            for index in component:
                if index != winner:
                    records[index]["reasons"].add("duplicate_article")

    for index, record in enumerate(records):
        reasons = sorted(record["reasons"])
        status = "PREFILTERED" if index in selected else (
            "AVOID" if {"non_material_content", "duplicate_article"} & set(reasons)
            else "ABSTAIN"
        )
        article = record["article"]
        if index in selected:
            material.append((_stable_sort_key(article), article))
        observations.append(
            ScenarioPrefilterObservation(
                candidate_id=record["candidate_id"],
                decision_id=record["decision_id"],
                article_id=_text(article, "article_id"),
                decision_at=normalized_decision_at,
                status=status,
                reason_codes=tuple(reasons),
                content_hash_sha256=record["content_hash"],
                source_lineage_hash_sha256=record["lineage_hash"],
            )
        )
    eligible = tuple(
        article for _, article in sorted(material, key=lambda item: item[0])
    )
    observations.sort(key=lambda item: (item.candidate_id, item.decision_id))
    return ScenarioPrefilterResult(
        eligible_articles=eligible,
        observations=tuple(observations),
        decision_at=normalized_decision_at,
        config=policy_config,
        config_hash_sha256=config_hash,
    )


def evaluate_scenario_prefilter(
    article: ScenarioNewsArticle,
    *,
    decision_at: str,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    min_text_chars: int = DEFAULT_MIN_TEXT_CHARS,
    supported_source_tiers: Iterable[str] = SUPPORTED_SOURCE_TIERS,
) -> ScenarioPrefilterObservation:
    """Evaluate one article using the same canonical batch contract."""

    result = prefilter_scenario_articles(
        [article],
        decision_at=decision_at,
        max_age_seconds=max_age_seconds,
        min_text_chars=min_text_chars,
        supported_source_tiers=supported_source_tiers,
    )
    return result.observations[0]


# Short alias for callers that already use the generic article vocabulary.
prefilter_articles = prefilter_scenario_articles


def _violations(
    article: Any,
    *,
    decision: datetime | None,
    max_age_seconds: int,
    min_text_chars: int,
    supported_source_tiers: frozenset[str],
) -> list[str]:
    reasons: list[str] = []
    if decision is None:
        reasons.append("evidence_cutoff_timestamp_missing_or_invalid")
    article_id = _text(article, "article_id")
    symbols = getattr(article, "symbols", ())
    if not article_id:
        reasons.append("article_identity_missing")
    if not isinstance(symbols, (tuple, list)) or not symbols or any(
        not _SYMBOL.fullmatch(str(symbol).strip().upper()) for symbol in symbols
    ):
        reasons.append("symbol_identity_missing_or_invalid")
    source = _text(article, "source")
    source_url = _text(article, "source_url")
    tier = _text(article, "tier").upper()
    if tier not in supported_source_tiers or not source or not _URL.fullmatch(source_url):
        reasons.append("unsupported_source")
    created = parse_aware_timestamp(_text(article, "created_at"))
    if created is None:
        reasons.append("created_at_missing_or_invalid")
    elif decision is not None:
        age = (decision - created).total_seconds()
        if age < 0:
            reasons.append("created_at_future")
        elif age > max_age_seconds:
            reasons.append("article_stale")
    for field in ("updated_at", "fetched_at", "first_seen_at"):
        value = _text(article, field)
        if value and parse_aware_timestamp(value) is None:
            reasons.append(f"{field}_missing_or_invalid")
    delay = getattr(article, "provider_delay_seconds", None)
    if delay is not None and (isinstance(delay, bool) or not _finite(delay) or float(delay) < 0):
        reasons.append("provider_delay_missing_or_invalid")
    body = " ".join(_text(article, field) for field in ("summary", "content"))
    normalized_body = re.sub(r"\s+", " ", body).strip()
    if len(normalized_body) < min_text_chars:
        reasons.append("non_material_content")
    elif any(pattern.fullmatch(normalized_body) for pattern in _EXPLICIT_NON_MATERIAL_PATTERNS):
        reasons.append("non_material_content")
    return reasons


def _strict_nonnegative_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{field} must be a non-negative integer")
    return value


def _strict_positive_int(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{field} must be positive")
    return value


def _strict_source_tiers(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, (str, bytes)):
        raise ValueError("supported_source_tiers must be a non-empty iterable of source tiers")
    try:
        materialized = tuple(values)
    except TypeError as exc:
        raise ValueError(
            "supported_source_tiers must be a non-empty iterable of source tiers"
        ) from exc
    if not materialized or any(not isinstance(value, str) for value in materialized):
        raise ValueError("supported_source_tiers contains an invalid source tier")
    tiers = frozenset(materialized)
    if not tiers or not tiers <= SUPPORTED_SOURCE_TIERS:
        raise ValueError("supported_source_tiers contains an unsupported source tier")
    return tiers


def _duplicate_components(records: list[dict[str, Any]]) -> list[tuple[int, ...]]:
    parent = list(range(len(records)))

    def find(index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left: int, right: int) -> None:
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    first_by_identity: dict[tuple[str, str], int] = {}
    for index, record in enumerate(records):
        for key in record["identity_keys"]:
            if not key[1]:
                continue
            previous = first_by_identity.setdefault(key, index)
            union(previous, index)
    components: dict[int, list[int]] = defaultdict(list)
    for index in range(len(records)):
        components[find(index)].append(index)
    return [tuple(value) for value in components.values()]


def _representative_sort_key(record: dict[str, Any]) -> tuple[Any, ...]:
    article = record["article"]
    tier_rank = {"T1": 0, "T2": 1}.get(_text(article, "tier").upper(), 2)
    return (
        tier_rank,
        -len(" ".join(_text(article, field) for field in ("headline", "summary", "content"))),
        _stable_sort_key(article),
        record["content_hash"],
        record["lineage_hash"],
    )


def prefilter_implementation_hash_sha256() -> str:
    """Hash this module and its local timestamp/contract helper modules."""

    modules = (
        ("intraday_scanner.scenario.prefilter", Path(__file__)),
        ("intraday_scanner.scenario.contracts", Path(_contracts_module.__file__)),
        ("intraday_scanner.scenario.point_in_time", Path(_point_in_time_module.__file__)),
    )
    parts = [
        {"module": name, "source_sha256": sha256(path.read_bytes()).hexdigest()}
        for name, path in modules
    ]
    return sha256(canonical_hash(parts).encode("ascii")).hexdigest()


def _candidate_id(article: Any, content_hash: str, lineage_hash: str, ordinal: int) -> str:
    return canonical_hash(
        {
            "article_id": _text(article, "article_id"),
            "content_hash_sha256": content_hash,
            "source_lineage_hash_sha256": lineage_hash,
            "duplicate_ordinal": ordinal,
            "policy_version": SCENARIO_PREFILTER_POLICY_VERSION,
        }
    )[:32]


def _safe_hashes(article: Any) -> tuple[str, str]:
    # Recompute identities from the immutable input fields.  Never trust a
    # caller-supplied hash property: a forged hash must not become lineage.
    content_hash = canonical_hash(
        {field: _text(article, field) for field in ("headline", "summary", "content")}
    )
    symbols = getattr(article, "symbols", ())
    lineage_hash = canonical_hash(
        {
            "provider": _text(article, "provider"),
            "article_id": _text(article, "article_id"),
            "symbols": _normalized_symbols(symbols),
            "source": _text(article, "source"),
            "source_url": _text(article, "source_url"),
            "created_at": _text(article, "created_at"),
            "updated_at": _text(article, "updated_at"),
            "timing_kind": _text(article, "timing_kind"),
        }
    )
    return content_hash, lineage_hash


def _stable_sort_key(article: Any) -> tuple[str, ...]:
    return tuple(
        _text(article, field)
        for field in ("article_id", "created_at", "source_url", "headline", "content")
    )


def _normalized_symbols(symbols: Any) -> tuple[str, ...]:
    if not isinstance(symbols, (tuple, list)):
        return ()
    return tuple(sorted(str(item).upper().strip() for item in symbols))


def _text(value: Any, field: str) -> str:
    try:
        current = getattr(value, field, "")
    except Exception:
        return ""
    return current.strip() if isinstance(current, str) else str(current or "").strip()


def _finite(value: Any) -> bool:
    try:
        return math.isfinite(float(value))
    except (TypeError, ValueError, OverflowError):
        return False


def _iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


__all__ = [
    "DEFAULT_MAX_AGE_SECONDS",
    "DEFAULT_MIN_TEXT_CHARS",
    "SCENARIO_PREFILTER_CONFIG_VERSION",
    "SCENARIO_PREFILTER_POLICY_VERSION",
    "SUPPORTED_SOURCE_TIERS",
    "ScenarioPrefilterObservation",
    "ScenarioPrefilterResult",
    "evaluate_scenario_prefilter",
    "prefilter_articles",
    "prefilter_implementation_hash_sha256",
    "prefilter_scenario_articles",
]
