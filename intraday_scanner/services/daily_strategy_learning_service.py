"""Deterministic, research-only daily strategy-learning orchestration.

This module deliberately stops at evidence inventory and unapplied challenger
proposals.  A miss-attribution implementation can be supplied through the
``StrategyEvidenceAnalyzer`` protocol without changing this safety boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any, Protocol

from intraday_scanner.v2.strategies import (
    StrategySpec,
    build_alphaops_intraday_strategy,
    build_strategy_catalog,
)
from intraday_scanner.v2.strategies.catalog import describe_strategy

DAILY_LEARNING_SCHEMA = "dawnstrike.strategy_learning_daily.v1"
PROPOSAL_SCHEMA = "dawnstrike.strategy_remediation_proposals.v1"
EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES = (
    ("alphaops_v5", "dawnstrike-alphaops-v5.0.0"),
)
_UNRESOLVED_STATUSES = frozenset(
    {
        "MISSING",
        "UNRESOLVED",
        "PENDING",
        "TERMINAL_MISSING",
        "RECONCILIATION_PENDING",
        "CENSORED_UNRESOLVED",
    }
)
_TERMINAL_TIMESTAMP_FIELDS = (
    "_terminal_event_at",
    "terminal_event_at",
    "closed_at",
    "close_time",
    "exit_time",
    "exit_timestamp",
    "resolved_at",
    "completed_at",
)
_EVIDENCE_TIMESTAMP_FIELDS = (
    *_TERMINAL_TIMESTAMP_FIELDS,
    "evidence_at",
    "observed_at",
    "event_at",
    "decision_at",
    "generated_at",
    "created_at",
    "proposal_at",
    "proposed_at",
)

# The token is intentionally private.  A JSON/evidence-file mapping cannot
# claim that it came from the append-only receipt table merely by adding a
# marker field.  The readonly DB adapter creates this envelope below.
_TRUSTED_RECEIPT_TOKEN = object()
_TRUSTED_V6_TOKEN = object()


class _PersistedStrategyDecisionReceipt(dict[str, Any]):
    """Receipt payload plus authenticated readonly-row provenance.

    This is a private boundary: callers can pass ordinary mappings for
    diagnostics, but only this envelope can contribute to certification.
    ``created_at`` and the row envelope are kept out of the canonical payload
    so the receipt hash remains the hash produced by StrategyDecisionReceipt.
    """

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        envelope: Mapping[str, Any],
        token: object,
    ) -> None:
        if token is not _TRUSTED_RECEIPT_TOKEN:
            raise TypeError("persisted receipt provenance is private")
        super().__init__(payload)
        self._envelope = dict(envelope)


def _persisted_receipt(
    payload: Mapping[str, Any], *, envelope: Mapping[str, Any]
) -> _PersistedStrategyDecisionReceipt:
    return _PersistedStrategyDecisionReceipt(
        payload, envelope=envelope, token=_TRUSTED_RECEIPT_TOKEN
    )


class _PersistedV6Decision(dict[str, Any]):
    """Private envelope for decisions loaded from alpha_v6_decisions."""

    def __init__(self, payload: Mapping[str, Any], *, token: object) -> None:
        if token is not _TRUSTED_V6_TOKEN:
            raise TypeError("V6 persisted provenance is private")
        super().__init__(payload)


def _persisted_v6_decision(payload: Mapping[str, Any]) -> _PersistedV6Decision:
    return _PersistedV6Decision(payload, token=_TRUSTED_V6_TOKEN)


class StrategyEvidenceAnalyzer(Protocol):
    """Injection boundary for the causal backtest/miss module."""

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class DailyLearningContext:
    market_date: str
    cutoff: str
    source_identity: str
    code_sha: str
    source_hash_sha256: str
    input_hash_sha256: str = ""

    def __post_init__(self) -> None:
        try:
            date.fromisoformat(self.market_date)
        except ValueError as exc:
            raise ValueError("market_date must be an ISO date (YYYY-MM-DD)") from exc
        if not self.source_identity.strip():
            raise ValueError("source_identity is required to freeze the evidence boundary")
        if not self.code_sha.strip():
            raise ValueError("code_sha is required to freeze code identity")
        try:
            cutoff = datetime.fromisoformat(self.cutoff.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("cutoff must be an ISO datetime") from exc
        if cutoff.tzinfo is None:
            raise ValueError("cutoff must include a timezone")
        if len(self.source_hash_sha256) != 64:
            raise ValueError("source_hash_sha256 must be a SHA-256 hex digest")
        if self.input_hash_sha256 and not re.fullmatch(r"[0-9a-f]{64}", self.input_hash_sha256):
            raise ValueError("input_hash_sha256 must be a canonical lowercase SHA-256 hex digest")


class EmptyEvidenceAnalyzer:
    """Safe default until the causal miss-attribution module is connected."""

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]:
        del strategy, context
        return {"status": "NO_ANALYSIS", "outcomes": [], "misses": [], "proposals": []}


class MappingEvidenceAnalyzer:
    """Adapter for a JSON mapping keyed by strategy ID, useful for CLI/replay tests."""

    def __init__(self, payload: Mapping[str, Any]) -> None:
        self._payload = payload

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]:
        del context
        value = self._payload.get(strategy.strategy_id, self._payload.get("default", {}))
        if value is None:
            return {}
        if not isinstance(value, Mapping):
            raise ValueError(f"evidence for {strategy.strategy_id} must be an object")
        return value


def _build_daily_strategy_catalog() -> tuple[StrategySpec, ...]:
    """Return the mechanical catalog plus the governed active AlphaOps spec."""

    catalog = (*build_strategy_catalog(), build_alphaops_intraday_strategy({}))
    identities = [(item.strategy_id, item.version) for item in catalog]
    if len(identities) != len(set(identities)):
        raise ValueError("daily strategy catalog contains duplicate identities")
    return catalog


class AttributionReportAnalyzer:
    """Adapt deterministic strategy-attribution output into the daily loop.

    Only closed rows enter the outcome list. Open marks, no-trades, missing
    truth, and conflicts remain miss/evidence records and cannot become return
    labels. Remediation hypotheses remain unapplied research proposals.
    """

    def __init__(self, report: Any) -> None:
        payload = report.to_dict() if hasattr(report, "to_dict") else report
        if not isinstance(payload, Mapping):
            raise ValueError("attribution report must be an object")
        rows = payload.get("rows", ())
        summaries = payload.get("summaries", ())
        if not isinstance(rows, Sequence) or isinstance(rows, (str, bytes)):
            raise ValueError("attribution report rows must be a list")
        if not isinstance(summaries, Sequence) or isinstance(summaries, (str, bytes)):
            raise ValueError("attribution report summaries must be a list")
        self._schema = str(payload.get("schema_version") or "unknown_attribution_contract")
        self._rows = tuple(dict(row) for row in rows if isinstance(row, Mapping))
        self._summaries = tuple(
            dict(summary) for summary in summaries if isinstance(summary, Mapping)
        )

    def analyze(
        self,
        strategy: StrategySpec,
        context: DailyLearningContext,
    ) -> Mapping[str, Any]:
        del context
        rows = tuple(
            row
            for row in self._rows
            if row.get("strategy_id") == strategy.strategy_id
            and row.get("strategy_version") in {None, "", strategy.version}
        )
        summaries = tuple(
            summary
            for summary in self._summaries
            if summary.get("strategy_id") == strategy.strategy_id
            and summary.get("strategy_version") in {None, "", strategy.version}
        )
        outcomes: list[dict[str, Any]] = []
        quarantined_closed: list[dict[str, Any]] = []
        for row in rows:
            if str(row.get("state")) != "closed":
                continue
            eligibility = str(row.get("eligibility") or "").lower()
            classification = str(row.get("classification") or "")
            if eligibility != "eligible" or classification == "closed_provisional":
                quarantined_closed.append(
                    {
                        **row,
                        "status": "CLOSED_PROVISIONAL",
                        "eligibility_reason": row.get("eligibility_reason")
                        or "closed_lifecycle_is_not_learning_eligible",
                    }
                )
                continue
            outcomes.append({**row, "status": "RESOLVED"})
        misses = [
            dict(row)
            for row in rows
            if (
                str(row.get("classification")) not in {"closed_win", "closed_flat"}
                or str(row.get("eligibility") or "").lower() != "eligible"
            )
        ]
        proposals: list[dict[str, Any]] = []
        if strategy.status not in {"benchmark", "baseline"}:
            grouped: dict[str, dict[str, Any]] = {}
            for summary in summaries:
                eligibility = summary.get("eligibility")
                eligible_count = (
                    int(eligibility.get("eligible_count") or 0)
                    if isinstance(eligibility, Mapping)
                    else 0
                )
                hypotheses = summary.get("remediation_hypotheses", ())
                if not isinstance(hypotheses, Sequence) or isinstance(hypotheses, (str, bytes)):
                    continue
                for hypothesis in hypotheses:
                    if not isinstance(hypothesis, Mapping):
                        continue
                    root_cause = str(hypothesis.get("hypothesis_id") or "unknown_evidence")
                    current = grouped.setdefault(
                        root_cause,
                        {
                            "root_cause_category": root_cause,
                            "supporting_miss_count": 0,
                            "eligible_sample_count": 0,
                            "hypothesis": str(hypothesis.get("action") or "Collect evidence."),
                            "controlled_change": {
                                "scope": "research_challenger_only",
                                "component": root_cause,
                            },
                            "evidence_cohorts": [],
                            "evidence_hashes": [],
                        },
                    )
                    current["supporting_miss_count"] += int(hypothesis.get("trigger_count") or 0)
                    current["eligible_sample_count"] += eligible_count
                    cohort = summary.get("cohort")
                    if cohort and cohort not in current["evidence_cohorts"]:
                        current["evidence_cohorts"].append(cohort)
                    for evidence_hash in summary.get("evidence_hashes", ()):
                        if evidence_hash not in current["evidence_hashes"]:
                            current["evidence_hashes"].append(evidence_hash)
            proposals = [grouped[key] for key in sorted(grouped)]
        return {
            "status": "ATTRIBUTED" if rows else "NO_RETAINED_ROWS",
            "evidence_contract": self._schema,
            "outcomes": outcomes,
            "misses": misses,
            "quarantined_closed": quarantined_closed,
            "counts": {
                "closed_provisional_quarantined": len(quarantined_closed),
            },
            "proposals": proposals,
        }


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _write_json_idempotent(path: Path, payload: Mapping[str, Any]) -> bool:
    encoded = _canonical_json(payload) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = path.read_text(encoding="utf-8")
        if existing != encoded:
            raise ValueError(f"immutable daily-learning artifact changed: {path}")
        return True
    path.write_text(encoded, encoding="utf-8")
    return False


def _reuse_immutable_artifacts(
    root: Path,
    context: DailyLearningContext,
) -> dict[str, Any] | None:
    receipt_path = root / "daily_learning_receipt.json"
    proposal_path = root / "remediation_proposals.json"
    if not receipt_path.exists() and not proposal_path.exists():
        return None
    if not receipt_path.is_file() or not proposal_path.is_file():
        raise ValueError(f"immutable daily-learning artifact set is incomplete: {root}")
    try:
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        proposals = json.loads(proposal_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"immutable daily-learning artifact cannot be read: {root}") from exc
    if not isinstance(receipt, dict) or not isinstance(proposals, dict):
        raise ValueError(f"immutable daily-learning artifact must be an object: {root}")

    receipt_hash = str(receipt.get("receipt_sha256") or "")
    receipt_body = {key: value for key, value in receipt.items() if key != "receipt_sha256"}
    proposal_hash = str(proposals.get("artifact_sha256") or "")
    proposal_body = {
        key: value for key, value in proposals.items() if key != "artifact_sha256"
    }
    if receipt_hash != _sha256(receipt_body) or proposal_hash != _sha256(proposal_body):
        raise ValueError(f"immutable daily-learning artifact hash mismatch: {root}")
    coverage = (receipt.get("decision_receipt_learning") or {}).get(
        "expected_strategy_coverage"
    )
    if isinstance(coverage, Mapping):
        coverage_body = {
            key: value for key, value in coverage.items() if key != "coverage_hash_sha256"
        }
        if coverage.get("coverage_hash_sha256") != _sha256(coverage_body):
            raise ValueError(f"immutable decision-receipt coverage hash mismatch: {root}")

    expected_identity = {
        "schema_version": DAILY_LEARNING_SCHEMA,
        "market_date": context.market_date,
        "cutoff": context.cutoff,
        "source_identity": context.source_identity,
        "source_hash_sha256": context.source_hash_sha256,
        "input_hash_sha256": context.input_hash_sha256 or context.source_hash_sha256,
        "code_sha": context.code_sha,
    }
    if any(receipt.get(key) != value for key, value in expected_identity.items()):
        raise ValueError(f"immutable daily-learning invocation identity changed: {root}")
    if proposals.get("schema_version") != PROPOSAL_SCHEMA or any(
        proposals.get(key) != receipt.get(key)
        for key in ("run_id", "market_date", "cutoff", "input_hash_sha256")
    ):
        raise ValueError(f"immutable daily-learning artifact identity mismatch: {root}")
    required_safety = {
        "research_only": True,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }
    if any(
        receipt.get(key) is not value or proposals.get(key) is not value
        for key, value in required_safety.items()
    ):
        raise ValueError(f"immutable daily-learning safety boundary mismatch: {root}")
    if (
        receipt.get("daily_fit_performed") is not False
        or receipt.get("champion_mutated") is not False
    ):
        raise ValueError(f"immutable daily-learning receipt is not research-only: {root}")

    return {
        "status": str(receipt.get("status") or "complete"),
        "run_id": str(receipt["run_id"]),
        "market_date": context.market_date,
        "strategy_count": int(receipt.get("strategy_count") or 0),
        "proposal_count": int(receipt.get("proposal_count") or 0),
        "receipt_path": str(receipt_path),
        "proposals_path": str(proposal_path),
        "idempotent_reused": True,
        "research_only": True,
        "daily_fit_performed": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "decision_receipt_learning": receipt.get("decision_receipt_learning") or {},
        "input_hash_sha256": receipt.get("input_hash_sha256") or "",
    }


def _as_sequence(value: Any, field: str, strategy_id: str) -> Sequence[Mapping[str, Any]]:
    if value is None:
        return ()
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{field} for {strategy_id} must be a list")
    rows: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise ValueError(f"{field}[{index}] for {strategy_id} must be an object")
        rows.append(item)
    return rows


def _date_is_after(value: Any, market_date: str) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return date.fromisoformat(value[:10]) > date.fromisoformat(market_date)
    except ValueError:
        return False


def _parse_aware_timestamp(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text or "T" not in text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _cutoff_datetime(context: DailyLearningContext) -> datetime:
    parsed = _parse_aware_timestamp(context.cutoff)
    if parsed is None:
        # DailyLearningContext has already checked the shape and timezone.  A
        # defensive exception here keeps an invalid cutoff from becoming an
        # implicit unbounded learning window.
        raise ValueError("cutoff must be an aware ISO datetime")
    return parsed


def _populated_timestamps(
    row: Mapping[str, Any], fields: Sequence[str]
) -> tuple[tuple[str, Any], ...]:
    """Return every populated timestamp alias, preserving field identity."""

    return tuple(
        (field, row.get(field))
        for field in fields
        if row.get(field) not in (None, "")
    )


def _cutoff_violation(
    row: Mapping[str, Any],
    context: DailyLearningContext,
    *,
    require_terminal_timestamp: bool = False,
) -> str | None:
    """Return a deterministic quarantine reason for point-in-time evidence."""

    market_value = row.get("market_date")
    if market_value not in (None, ""):
        try:
            date.fromisoformat(str(market_value))
        except ValueError:
            return "malformed_market_date"
    if _date_is_after(market_value, context.market_date):
        return "future_market_date"
    cutoff = _cutoff_datetime(context)
    terminal_values = _populated_timestamps(row, _TERMINAL_TIMESTAMP_FIELDS)
    if require_terminal_timestamp and not terminal_values:
        return "missing_terminal_timestamp"
    # Never use the first alias as authority.  A conflicting populated alias
    # may be malformed or after the cutoff even when an earlier alias looks
    # valid; every alias must therefore be parseable and before the boundary.
    for _field, value in terminal_values:
        parsed = _parse_aware_timestamp(value)
        if parsed is None:
            return "unparseable_terminal_timestamp"
        if parsed > cutoff:
            return "terminal_after_cutoff"
    for field, value in _populated_timestamps(row, _EVIDENCE_TIMESTAMP_FIELDS):
        if field in _TERMINAL_TIMESTAMP_FIELDS:
            continue
        parsed = _parse_aware_timestamp(value)
        if parsed is None:
            # Non-terminal evidence timestamps are only relevant when a caller
            # actually supplies one; malformed evidence cannot be ordered.
            return f"unparseable_{field}"
        if parsed > cutoff:
            return f"{field}_after_cutoff"
    return None


def _requires_orderable_evidence(row: Mapping[str, Any], context: DailyLearningContext) -> bool:
    """Whether a non-terminal observation needs an event timestamp.

    A historical, explicitly dated row is ordered by its date.  Same-day and
    undated rows must carry at least one aware event timestamp so they cannot
    be smuggled into a point-in-time run as if their observation time were
    known.
    """

    row_date = str(row.get("market_date") or "").strip()[:10]
    return not row_date or row_date == context.market_date


def _has_valid_ordering_timestamp(row: Mapping[str, Any]) -> bool:
    return any(
        _parse_aware_timestamp(value) is not None
        for _field, value in _populated_timestamps(row, _EVIDENCE_TIMESTAMP_FIELDS)
    )


def _normalize_analysis(
    strategy: StrategySpec,
    context: DailyLearningContext,
    raw: Mapping[str, Any],
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcomes: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    excluded_unresolved = 0
    excluded_future = 0
    missing_return = 0
    excluded_ineligible = 0
    terminal_timestamp_quarantined = 0
    evidence_timestamp_quarantined = 0
    quarantined_closed = _as_sequence(
        raw.get("quarantined_closed"), "quarantined_closed", strategy.strategy_id
    )
    quarantined_evidence = _as_sequence(
        raw.get("quarantined_evidence"), "quarantined_evidence", strategy.strategy_id
    )

    for row in _as_sequence(raw.get("outcomes"), "outcomes", strategy.strategy_id):
        status = str(row.get("status", "")).upper()
        eligibility = str(row.get("eligibility") or "").lower()
        if status in _UNRESOLVED_STATUSES or (
            eligibility and eligibility != "eligible"
        ) or str(row.get("classification") or "") == "closed_provisional":
            excluded_unresolved += 1
            excluded_ineligible += int(bool(eligibility and eligibility != "eligible"))
            continue
        cutoff_reason = _cutoff_violation(
            row,
            context,
            # An outcome is terminal by virtue of being in the outcomes
            # channel.  Requiring an aware terminal event here prevents a
            # same-day current-state row from becoming a historical return.
            require_terminal_timestamp=True,
        )
        if cutoff_reason and (
            cutoff_reason == "future_market_date" or cutoff_reason.endswith("_after_cutoff")
        ):
            excluded_future += 1
            continue
        if cutoff_reason in {"missing_terminal_timestamp", "unparseable_terminal_timestamp"}:
            terminal_timestamp_quarantined += 1
            quarantined_closed = (*quarantined_closed, {
                **dict(row),
                "status": "QUARANTINED_TERMINAL_TIMESTAMP",
                "quarantine_reason": cutoff_reason,
            })
            continue
        if cutoff_reason:
            terminal_timestamp_quarantined += 1
            quarantined_closed = (*quarantined_closed, {
                **dict(row),
                "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                "quarantine_reason": cutoff_reason,
            })
            continue
        normalized = dict(row)
        normalized.pop("synthetic_return", None)
        if "return_pct" not in normalized and "net_return_pct" not in normalized:
            missing_return += 1
        outcomes.append(normalized)

    for row in _as_sequence(raw.get("misses"), "misses", strategy.strategy_id):
        # Same-day misses need an event/evidence timestamp so the exact cutoff
        # can order them.  Historical dates have an unambiguous date boundary.
        if _requires_orderable_evidence(row, context) and not _has_valid_ordering_timestamp(row):
            evidence_timestamp_quarantined += 1
            quarantined_evidence = (*quarantined_evidence, {
                **dict(row),
                "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                "quarantine_reason": "missing_same_day_evidence_timestamp",
            })
            continue
        cutoff_reason = _cutoff_violation(row, context)
        if cutoff_reason:
            if cutoff_reason and (
                cutoff_reason == "future_market_date" or cutoff_reason.endswith("_after_cutoff")
            ):
                excluded_future += 1
            else:
                evidence_timestamp_quarantined += 1
                quarantined_evidence = (*quarantined_evidence, {
                    **dict(row),
                    "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                    "quarantine_reason": cutoff_reason,
                })
            continue
        misses.append(dict(row))

    proposals: list[dict[str, Any]] = []
    quarantined_proposals: list[dict[str, Any]] = []
    for raw_proposal in _as_sequence(
        raw.get("proposals", raw.get("remediation_proposals")),
        "proposals",
        strategy.strategy_id,
    ):
        proposal = dict(raw_proposal)
        proposal["strategy_id"] = strategy.strategy_id
        proposal["strategy_version"] = strategy.version
        proposal["status"] = "PROPOSED_NOT_APPLIED"
        proposal["applied"] = False
        proposal["automatic_policy_change"] = False
        proposal["automatic_promotion"] = False
        proposal["research_only"] = True
        proposal["broker_execution_enabled"] = False
        proposal["missing_outcomes_are_zero"] = False
        proposal.pop("proposal_id", None)
        proposal["proposal_id"] = "rem-" + _sha256(proposal)[:24]
        if _requires_orderable_evidence(proposal, context) and not _has_valid_ordering_timestamp(
            proposal
        ):
            quarantined_proposals.append(
                {
                    **proposal,
                    "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                    "quarantine_reason": "missing_same_day_or_undated_proposal_timestamp",
                }
            )
            evidence_timestamp_quarantined += 1
            continue
        proposal_cutoff_reason = _cutoff_violation(proposal, context)
        if proposal_cutoff_reason:
            if proposal_cutoff_reason in {"future_market_date"} or proposal_cutoff_reason.endswith(
                "_after_cutoff"
            ):
                excluded_future += 1
            else:
                evidence_timestamp_quarantined += 1
            quarantined_proposals.append(
                {
                    **proposal,
                    "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                    "quarantine_reason": proposal_cutoff_reason,
                }
            )
            continue
        proposals.append(proposal)

    evidence = {
        "status": str(raw.get("status", "ANALYZED")),
        "outcomes": outcomes,
        "misses": misses,
        "counts": {
            "outcomes_retained": len(outcomes),
            "misses_retained": len(misses),
            "proposals_retained": len(proposals),
            "unresolved_outcomes_excluded": excluded_unresolved,
            "ineligible_outcomes_excluded": excluded_ineligible,
            "future_evidence_excluded": excluded_future,
            "terminal_timestamp_quarantined": terminal_timestamp_quarantined,
            "evidence_timestamp_quarantined": evidence_timestamp_quarantined,
            "outcomes_without_return_excluded_from_return_metrics": missing_return,
            "closed_provisional_quarantined": len(quarantined_closed),
            "proposals_quarantined": len(quarantined_proposals),
        },
        "evidence_contract": str(raw.get("evidence_contract", "injected_unattributed_v1")),
        "quarantined_closed": [dict(row) for row in quarantined_closed],
        "quarantined_evidence": [dict(row) for row in quarantined_evidence],
        "quarantined_proposals": quarantined_proposals,
    }
    return evidence, proposals


def _validate_persisted_decision_receipt(
    value: Mapping[str, Any],
    *,
    market_date: str,
    cutoff: datetime,
) -> tuple[bool, str]:
    """Validate the authenticated receipt ingress used by daily learning."""

    if not isinstance(value, _PersistedStrategyDecisionReceipt):
        return False, "receipt_not_from_persisted_readonly_source"
    digest = str(value.get("receipt_hash_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        return False, "receipt_hash_missing_or_noncanonical"
    body = {
        key: item
        for key, item in value.items()
        if key not in {"receipt_id", "receipt_hash_sha256"}
    }
    try:
        expected = _sha256(body)
    except (TypeError, ValueError):
        return False, "receipt_payload_not_canonical"
    if digest != expected:
        return False, "receipt_hash_mismatch"
    if str(value.get("receipt_id") or "") != "sdr-" + digest[:24]:
        return False, "receipt_id_not_derived_from_hash"
    decision_at = _parse_aware_timestamp(value.get("decision_at"))
    if decision_at is None:
        return False, "decision_at_missing_or_unparseable"
    if decision_at > cutoff:
        return False, "decision_after_cutoff"
    if value.get("market_date") != market_date:
        return False, "market_date_mismatch"
    if value.get("research_only") is not True:
        return False, "research_only_required"
    if value.get("broker_execution_enabled") is not False:
        return False, "broker_execution_must_be_false"
    envelope = value._envelope
    for field in (
        "receipt_id",
        "receipt_hash_sha256",
        "strategy_id",
        "strategy_version",
        "symbol",
        "market_date",
        "pick_tier",
        "research_pick_eligible",
        "paper_entry_eligible",
        "source_identity",
        "input_hash_sha256",
    ):
        if field not in envelope or field not in value:
            return False, f"persisted_envelope_{field}_mismatch"
        if field in {"research_pick_eligible", "paper_entry_eligible"}:
            if (
                not isinstance(value.get(field), bool)
                or isinstance(envelope[field], bool)
                or envelope[field] not in (0, 1)
            ):
                return False, f"persisted_envelope_{field}_mismatch"
            if bool(envelope[field]) != value.get(field):
                return False, f"persisted_envelope_{field}_mismatch"
        elif not isinstance(value.get(field), str) or not isinstance(envelope[field], str):
            return False, f"persisted_envelope_{field}_mismatch"
        elif envelope[field] != value.get(field):
            return False, f"persisted_envelope_{field}_mismatch"
    created_at = _parse_aware_timestamp(envelope.get("created_at"))
    if created_at is None:
        return False, "persisted_created_at_missing_or_unparseable"
    if created_at > cutoff:
        return False, "persisted_created_at_after_cutoff"
    return True, ""


def _validate_decision_receipt_ingress(
    receipts: Sequence[Mapping[str, Any]] | None,
    *,
    market_date: str,
    cutoff: datetime,
) -> tuple[tuple[Mapping[str, Any], ...], dict[str, Any]]:
    """Validate every supplied receipt and expose rejected rows by reason."""

    if receipts is None:
        return (), {"source_status": "NOT_PROVIDED", "invalid_count": 0, "invalid_reasons": {}}
    accepted: list[Mapping[str, Any]] = []
    reasons: dict[str, int] = {}
    for receipt in receipts:
        valid, reason = _validate_persisted_decision_receipt(
            receipt, market_date=market_date, cutoff=cutoff
        )
        if valid:
            accepted.append(receipt)
        else:
            reasons[reason] = reasons.get(reason, 0) + 1
    persisted_invalid_reasons = getattr(receipts, "invalid_reasons", {})
    for reason, count in dict(persisted_invalid_reasons).items():
        reasons[str(reason)] = reasons.get(str(reason), 0) + int(count)
    return tuple(accepted), {
        "source_status": "CHECKED",
        "invalid_count": sum(reasons.values()),
        "invalid_reasons": dict(sorted(reasons.items())),
    }


def _freeze_invocation_identity(root: Path, context: DailyLearningContext) -> DailyLearningContext:
    """Persist the first cutoff before any analyzer work starts.

    This reservation closes the crash window between invoking the stage and
    writing its final receipt.  Retries reuse the original point-in-time
    boundary; conflicting source/input/code identity remains a named failure.
    """

    path = root / "daily_learning_invocation.json"
    body = {
        "schema_version": DAILY_LEARNING_SCHEMA,
        "market_date": context.market_date,
        "cutoff": context.cutoff,
        "source_identity": context.source_identity,
        "source_hash_sha256": context.source_hash_sha256,
        "input_hash_sha256": context.input_hash_sha256,
        "code_sha": context.code_sha,
    }
    if path.exists():
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("daily-learning invocation reservation is unreadable") from exc
        if not isinstance(persisted, dict):
            raise ValueError("daily-learning invocation reservation is not an object")
        reservation_hash = persisted.get("reservation_sha256")
        stored_body = {
            key: value
            for key, value in persisted.items()
            if key != "reservation_sha256"
        }
        if reservation_hash != _sha256(stored_body):
            raise ValueError("daily-learning invocation reservation hash mismatch")
        for key in (
            "market_date",
            "source_identity",
            "source_hash_sha256",
            "input_hash_sha256",
            "code_sha",
        ):
            if persisted.get(key) != body[key]:
                raise ValueError(f"daily-learning invocation identity conflict: {key}")
        frozen = DailyLearningContext(
            market_date=str(persisted["market_date"]),
            cutoff=str(persisted["cutoff"]),
            source_identity=str(persisted["source_identity"]),
            code_sha=str(persisted["code_sha"]),
            source_hash_sha256=str(persisted["source_hash_sha256"]),
            input_hash_sha256=str(persisted["input_hash_sha256"]),
        )
        return frozen
    root.mkdir(parents=True, exist_ok=True)
    _write_json_idempotent(path, {**body, "reservation_sha256": _sha256(body)})
    return context


def run_daily_strategy_learning(
    *,
    market_date: str,
    cutoff: str,
    source_identity: str,
    code_sha: str,
    out_dir: str | Path,
    source_hash_sha256: str | None = None,
    input_hash_sha256: str | None = None,
    analyzer: StrategyEvidenceAnalyzer | None = None,
    decision_receipts: Sequence[Mapping[str, Any]] | None = None,
    v6_decisions: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inventory the catalog and write one immutable research-only daily run."""

    source_hash = source_hash_sha256 or hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
    context = DailyLearningContext(
        market_date=market_date,
        cutoff=cutoff,
        source_identity=source_identity,
        code_sha=code_sha,
        source_hash_sha256=source_hash,
        input_hash_sha256=input_hash_sha256 or source_hash,
    )
    root = Path(out_dir) / context.market_date
    context = _freeze_invocation_identity(root, context)
    reused = _reuse_immutable_artifacts(root, context)
    if reused is not None:
        return reused
    valid_receipts, receipt_ingress = _validate_decision_receipt_ingress(
        decision_receipts,
        market_date=context.market_date,
        cutoff=_cutoff_datetime(context),
    )
    valid_v6 = tuple(
        row for row in (v6_decisions or ()) if isinstance(row, _PersistedV6Decision)
    )
    v6_invalid_count = len(tuple(v6_decisions or ())) - len(valid_v6)
    v6_invalid_reasons = dict(getattr(v6_decisions, "invalid_reasons", {}))
    if v6_invalid_count:
        v6_invalid_reasons["decision_not_from_persisted_readonly_source"] = (
            v6_invalid_reasons.get("decision_not_from_persisted_readonly_source", 0)
            + v6_invalid_count
        )
    v6_source_status = (
        "NOT_PROVIDED"
        if v6_decisions is None
        else "INTEGRITY_FAILURE"
        if v6_invalid_count or int(getattr(v6_decisions, "invalid_count", 0) or 0)
        else "NO_EVIDENCE"
        if not valid_v6
        else "PROVIDED"
    )
    analyzer = analyzer or EmptyEvidenceAnalyzer()
    strategies = sorted(
        _build_daily_strategy_catalog(), key=lambda item: (item.strategy_id, item.version)
    )
    inventory: list[dict[str, Any]] = []
    strategy_evidence: list[dict[str, Any]] = []
    proposals: list[dict[str, Any]] = []
    for strategy in strategies:
        descriptor = describe_strategy(strategy)
        descriptor["strategy_version"] = strategy.version
        descriptor["strategy_definition_hash_sha256"] = _sha256(descriptor)
        inventory.append(descriptor)
        raw = analyzer.analyze(strategy, context)
        if not isinstance(raw, Mapping):
            raise ValueError(f"analyzer result for {strategy.strategy_id} must be an object")
        evidence, strategy_proposals = _normalize_analysis(strategy, context, raw)
        raw_status = str(raw.get("status") or "").upper()
        if raw.get("source_status"):
            source_status = str(raw["source_status"])
        elif raw_status in {"NO_ANALYSIS", ""} and isinstance(analyzer, EmptyEvidenceAnalyzer):
            source_status = "NOT_PROVIDED"
        elif raw_status in {"NO_RETAINED_ROWS", "NO_EVIDENCE"}:
            source_status = "CHECKED_ZERO"
        elif not raw:
            source_status = "NOT_PROVIDED"
        else:
            source_status = "CHECKED"
        evidence["source_status"] = source_status
        strategy_evidence.append(
            {
                "strategy_id": strategy.strategy_id,
                "strategy_version": strategy.version,
                "evidence": evidence,
            }
        )
        proposals.extend(strategy_proposals)

    # Keep raw receipt observations visible for diagnostics, but only the
    # authenticated persisted subset can contribute to certification.
    receipt_learning = _aggregate_decision_receipts(valid_receipts)
    receipt_learning["valid_receipt_count"] = len(valid_receipts)
    receipt_learning["invalid_receipt_count"] = int(receipt_ingress["invalid_count"])
    receipt_learning["invalid_receipt_reasons"] = receipt_ingress["invalid_reasons"]
    receipt_learning["v6_source_status"] = v6_source_status
    receipt_learning["v6_decision_count"] = len(valid_v6)
    receipt_learning["v6_invalid_count"] = v6_invalid_count + int(
        getattr(v6_decisions, "invalid_count", 0) or 0
    )
    receipt_learning["v6_invalid_reasons"] = dict(sorted(v6_invalid_reasons.items()))
    receipt_coverage = _decision_receipt_coverage(
        valid_receipts if decision_receipts is not None else None,
        ingress=receipt_ingress,
    )
    receipt_coverage["ingress"] = receipt_ingress
    receipt_learning["expected_strategy_coverage"] = receipt_coverage
    receipt_coverage["v6_source_status"] = v6_source_status
    receipt_coverage["v6_decision_count"] = len(valid_v6)
    receipt_coverage["v6_invalid_count"] = receipt_learning["v6_invalid_count"]
    receipt_coverage["v6_invalid_reasons"] = receipt_learning["v6_invalid_reasons"]
    receipt_coverage["coverage_hash_sha256"] = _sha256(
        {key: value for key, value in receipt_coverage.items() if key != "coverage_hash_sha256"}
    )
    strategy_source_statuses = [
        str(item["evidence"].get("source_status") or "INCOMPLETE")
        for item in strategy_evidence
    ]
    strategy_coverage_incomplete = any(
        status in {"NOT_PROVIDED", "INTEGRITY_FAILURE", "INCOMPLETE"}
        for status in strategy_source_statuses
    )
    run_status = (
        "complete"
        if receipt_coverage["status"] == "COMPLETE"
        and v6_source_status not in {"NOT_PROVIDED", "INTEGRITY_FAILURE"}
        and not strategy_coverage_incomplete
        else "incomplete"
    )

    immutable_identity = {
        "schema_version": DAILY_LEARNING_SCHEMA,
        "market_date": context.market_date,
        "cutoff": context.cutoff,
        "source_identity": context.source_identity,
        "source_hash_sha256": context.source_hash_sha256,
        "input_hash_sha256": context.input_hash_sha256,
        "code_sha": context.code_sha,
        "catalog": [
            {
                "strategy_id": item["strategy_id"],
                "version": item["version"],
                "strategy_definition_hash_sha256": item["strategy_definition_hash_sha256"],
            }
            for item in inventory
        ],
        "evidence_hash_sha256": _sha256(strategy_evidence),
        "decision_receipt_hash_sha256": _sha256(receipt_learning),
        "decision_receipt_coverage_hash_sha256": receipt_coverage["coverage_hash_sha256"],
    }
    run_id = "dslearn-" + _sha256(immutable_identity)[:24]
    proposal_payload = {
        "schema_version": PROPOSAL_SCHEMA,
        "run_id": run_id,
        "market_date": context.market_date,
        "cutoff": context.cutoff,
        "input_hash_sha256": context.input_hash_sha256,
        "proposals": proposals,
        "research_only": True,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }
    proposal_payload["artifact_sha256"] = _sha256(proposal_payload)
    receipt = {
        **immutable_identity,
        "run_id": run_id,
        "strategy_count": len(inventory),
        "catalog": inventory,
        "strategy_evidence": strategy_evidence,
        "decision_receipt_learning": receipt_learning,
        "status": run_status,
        "proposal_count": len(proposals),
        "artifacts": {
            "remediation_proposals": str(root / "remediation_proposals.json"),
        },
        "research_only": True,
        "daily_fit_performed": False,
        "challenger_evaluation_performed": False,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "champion_mutated": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
        "same_day_unresolved_excluded": True,
        "artifact_contract": "immutable_hash_bound_receipt_v1",
    }
    receipt["receipt_sha256"] = _sha256(receipt)
    receipt_path = root / "daily_learning_receipt.json"
    proposal_path = root / "remediation_proposals.json"
    reused_receipt = _write_json_idempotent(receipt_path, receipt)
    reused_proposals = _write_json_idempotent(proposal_path, proposal_payload)
    return {
        "status": run_status,
        "run_id": run_id,
        "market_date": context.market_date,
        "strategy_count": len(inventory),
        "proposal_count": len(proposals),
        "receipt_path": str(receipt_path),
        "proposals_path": str(proposal_path),
        "idempotent_reused": reused_receipt and reused_proposals,
        "research_only": True,
        "daily_fit_performed": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "decision_receipt_learning": receipt_learning,
        "input_hash_sha256": context.input_hash_sha256,
    }


__all__ = [
    "DAILY_LEARNING_SCHEMA",
    "PROPOSAL_SCHEMA",
    "DailyLearningContext",
    "EmptyEvidenceAnalyzer",
    "AttributionReportAnalyzer",
    "MappingEvidenceAnalyzer",
    "StrategyEvidenceAnalyzer",
    "EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES",
    "run_daily_strategy_learning",
]


def _aggregate_decision_receipts(receipts: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Summarize receipt evidence without changing any policy automatically.

    Outcome labels are accepted only when an upstream source explicitly supplies
    them. Missing, open, or conflicting outcomes stay visible and never become
    a zero-return label.
    """

    by_condition: dict[tuple[str, str, str, str, bool, bool, str], dict[str, Any]] = {}
    by_strategy: dict[tuple[str, str], dict[str, Any]] = {}
    tier_counts: dict[str, int] = {}
    outcome_counts: dict[str, int] = {}
    resolved_gaps: dict[tuple[str, str, str], dict[str, Any]] = {}
    disclosed_gap_outcomes: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    winner_exclusions: dict[tuple[str, str, str], dict[str, Any]] = {}
    authoritative_contradictions: dict[tuple[str, str, str], dict[str, Any]] = {}
    blocking_counts: dict[tuple[str, str, str], int] = {}

    valid_receipt_count = 0
    for receipt in receipts:
        if not isinstance(receipt, Mapping):
            continue
        valid_receipt_count += 1
        strategy_id = str(receipt.get("strategy_id") or "UNKNOWN")
        strategy_version = str(receipt.get("strategy_version") or "UNKNOWN")
        tier = str(receipt.get("pick_tier") or "UNKNOWN")
        research_eligible = bool(receipt.get("research_pick_eligible"))
        paper_eligible = bool(receipt.get("paper_entry_eligible"))
        outcome_state = _receipt_outcome_state(receipt)
        tier_counts[tier] = tier_counts.get(tier, 0) + 1
        outcome_counts[outcome_state] = outcome_counts.get(outcome_state, 0) + 1

        strategy_key = (strategy_id, strategy_version)
        strategy_row = by_strategy.setdefault(
            strategy_key,
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "receipt_count": 0,
                "tier_counts": {},
                "outcome_state_counts": {},
                "research_pick_eligible_count": 0,
                "paper_entry_eligible_count": 0,
            },
        )
        strategy_row["receipt_count"] += 1
        strategy_row["tier_counts"][tier] = strategy_row["tier_counts"].get(tier, 0) + 1
        strategy_row["outcome_state_counts"][outcome_state] = (
            strategy_row["outcome_state_counts"].get(outcome_state, 0) + 1
        )
        strategy_row["research_pick_eligible_count"] += int(research_eligible)
        strategy_row["paper_entry_eligible_count"] += int(paper_eligible)

        blocking_ids = {
            str(item)
            for item in receipt.get("all_blocking_failures") or ()
            if str(item).strip()
        }
        disclosed_ids = {
            str(item)
            for item in receipt.get("disclosed_gaps") or ()
            if str(item).strip()
        }
        for condition_id in blocking_ids:
            blocking_key = (strategy_id, strategy_version, condition_id)
            blocking_counts[blocking_key] = blocking_counts.get(blocking_key, 0) + 1

        condition_results = receipt.get("condition_results") or ()
        if not isinstance(condition_results, Sequence) or isinstance(
            condition_results, (str, bytes)
        ):
            condition_results = ()
        for raw in condition_results:
            if not isinstance(raw, Mapping):
                continue
            condition_id = str(raw.get("condition_id") or "").strip()
            if not condition_id:
                continue
            status = str(raw.get("status") or "UNKNOWN")
            key = (
                strategy_id,
                strategy_version,
                condition_id,
                status,
                research_eligible,
                paper_eligible,
                outcome_state,
            )
            row = by_condition.setdefault(
                key,
                {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "condition_id": condition_id,
                    "condition_status": status,
                    "pick_tier": tier,
                    "research_pick_eligible": research_eligible,
                    "paper_entry_eligible": paper_eligible,
                    "outcome_state": outcome_state,
                    "receipt_count": 0,
                    "blocking_candidate_count": 0,
                    "disclosed_gap_count": 0,
                    "ai_resolved_count": 0,
                },
            )
            row["receipt_count"] += 1
            row["blocking_candidate_count"] += int(condition_id in blocking_ids)
            row["disclosed_gap_count"] += int(condition_id in disclosed_ids)
            is_ai_resolved = status == "RESOLVED_FROM_SOURCE" and str(
                raw.get("resolver_id") or ""
            ) not in {"", "deterministic"}
            row["ai_resolved_count"] += int(is_ai_resolved)

            if is_ai_resolved:
                resolved_key = (strategy_id, strategy_version, condition_id)
                resolved_row = resolved_gaps.setdefault(
                    resolved_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "resolved_count": 0,
                    },
                )
                resolved_row["resolved_count"] += 1

            if condition_id in disclosed_ids and outcome_state in {"WIN", "LOSS"}:
                gap_key = (strategy_id, strategy_version, condition_id, outcome_state)
                gap_row = disclosed_gap_outcomes.setdefault(
                    gap_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "outcome_state": outcome_state,
                        "count": 0,
                    },
                )
                gap_row["count"] += 1

            if outcome_state == "WIN" and condition_id in blocking_ids:
                winner_key = (strategy_id, strategy_version, condition_id)
                winner_row = winner_exclusions.setdefault(
                    winner_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "eventual_winner_count": 0,
                    },
                )
                winner_row["eventual_winner_count"] += 1

            if raw.get("ai_claim_contradicted") is True or raw.get(
                "contradicted_by_authoritative_source"
            ) is True:
                contradiction_key = (strategy_id, strategy_version, condition_id)
                contradiction_row = authoritative_contradictions.setdefault(
                    contradiction_key,
                    {
                        "strategy_id": strategy_id,
                        "strategy_version": strategy_version,
                        "condition_id": condition_id,
                        "authoritative_contradiction_count": 0,
                    },
                )
                contradiction_row["authoritative_contradiction_count"] += 1

        for raw in receipt.get("contradicted_claims") or ():
            if not isinstance(raw, Mapping):
                continue
            condition_id = str(raw.get("condition_id") or "").strip()
            if not condition_id or raw.get("authoritative") is not True:
                continue
            contradiction_key = (strategy_id, strategy_version, condition_id)
            contradiction_row = authoritative_contradictions.setdefault(
                contradiction_key,
                {
                    "strategy_id": strategy_id,
                    "strategy_version": strategy_version,
                    "condition_id": condition_id,
                    "authoritative_contradiction_count": 0,
                },
            )
            contradiction_row["authoritative_contradiction_count"] += 1

    blocking_rows: list[dict[str, Any]] = [
        {
            "strategy_id": strategy_id,
            "strategy_version": strategy_version,
            "condition_id": condition_id,
            "blocking_candidate_count": count,
        }
        for (strategy_id, strategy_version, condition_id), count in blocking_counts.items()
    ]
    blocking_rows.sort(
        key=lambda row: (
            -int(row["blocking_candidate_count"]),
            str(row["strategy_id"]),
            str(row["strategy_version"]),
            str(row["condition_id"]),
        )
    )
    legacy_conditions: dict[str, dict[str, Any]] = {}
    for observation in by_condition.values():
        condition_id = str(observation["condition_id"])
        summary = legacy_conditions.setdefault(
            condition_id,
            {"condition_id": condition_id, "status_counts": {}, "receipt_count": 0},
        )
        status = str(observation["condition_status"])
        summary["status_counts"][status] = (
            summary["status_counts"].get(status, 0) + int(observation["receipt_count"])
        )
        summary["receipt_count"] += int(observation["receipt_count"])
    return {
        "receipt_count": valid_receipt_count,
        "tier_counts": tier_counts,
        "outcome_state_counts": outcome_counts,
        "strategies": [by_strategy[key] for key in sorted(by_strategy)],
        "conditions": [legacy_conditions[key] for key in sorted(legacy_conditions)],
        "condition_observations": [by_condition[key] for key in sorted(by_condition)],
        "conditions_most_frequently_blocking": blocking_rows,
        "ai_resolvable_gaps_successfully_resolved": [
            resolved_gaps[key] for key in sorted(resolved_gaps)
        ],
        "disclosed_gap_outcomes": [
            disclosed_gap_outcomes[key] for key in sorted(disclosed_gap_outcomes)
        ],
        "conditions_that_excluded_eventual_winners": [
            winner_exclusions[key] for key in sorted(winner_exclusions)
        ],
        "ai_claims_later_contradicted": [
            authoritative_contradictions[key]
            for key in sorted(authoritative_contradictions)
        ],
        "research_only": True,
        "automatic_policy_change": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "missing_outcomes_are_zero": False,
    }


def _receipt_outcome_state(receipt: Mapping[str, Any]) -> str:
    raw = receipt.get("outcome_state")
    if raw is None:
        raw = receipt.get("outcome_status")
    if raw is None:
        raw = receipt.get("outcome")
    if isinstance(raw, Mapping):
        raw = raw.get("state") or raw.get("status") or raw.get("classification")
    value = str(raw or "").strip().upper()
    if value in {"WIN", "WON", "CLOSED_WIN", "PROFIT", "PROFITABLE"}:
        return "WIN"
    if value in {"LOSS", "LOST", "CLOSED_LOSS", "LOSSING", "UNPROFITABLE"}:
        return "LOSS"
    if value in {"FLAT", "CLOSED_FLAT", "BREAKEVEN", "BREAK_EVEN"}:
        return "FLAT"
    if value in {"OPEN", "PENDING", "UNRESOLVED", "MISSING", "UNKNOWN", ""}:
        return "MISSING_OUTCOME"
    return value


def _decision_receipt_coverage(
    receipts: Sequence[Mapping[str, Any]] | None,
    *,
    ingress: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Emit an immutable expected-cohort coverage receipt.

    ``None`` means the caller did not provide a persisted receipt source.  An
    explicit empty sequence means that source was checked and yielded zero
    AlphaOps evidence: ``COMPLETE`` for the checked lane with a governed
    ``NO_EVIDENCE`` source result.  The overall run still requires actual
    strategy evidence before it can be complete.
    """

    expected = [
        {"strategy_id": strategy_id, "strategy_version": strategy_version}
        for strategy_id, strategy_version in EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES
    ]
    if receipts is None:
        observed = [
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "receipt_count": 0,
            }
            for strategy_id, strategy_version in EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES
        ]
        body = {
            "schema_version": "dawnstrike.strategy_decision_coverage.v1",
            "status": "NOT_PROVIDED",
            "expected": expected,
            "observed": observed,
            "missing": [
                {
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "reason": "decision_receipts_not_provided",
                }
                for row in observed
            ],
            "research_only": True,
            "broker_execution_enabled": False,
            # V6 decisions are persisted in alpha_v6_decisions, not this
            # receipt table.  Keep that producer lane explicit rather than
            # inventing an impossible V6 StrategyDecisionReceipt cohort.
            "v6_source_status": "NOT_PROVIDED",
        }
    else:
        observed_counts: dict[tuple[str, str], int] = {}
        for receipt in receipts:
            if not isinstance(receipt, Mapping):
                continue
            key = (
                str(receipt.get("strategy_id") or ""),
                str(receipt.get("strategy_version") or ""),
            )
            observed_counts[key] = observed_counts.get(key, 0) + 1
        observed = [
            {
                "strategy_id": strategy_id,
                "strategy_version": strategy_version,
                "receipt_count": observed_counts.get((strategy_id, strategy_version), 0),
            }
            for strategy_id, strategy_version in EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES
        ]
        invalid_count = int((ingress or {}).get("invalid_count") or 0)
        missing = []
        if invalid_count:
            missing = [
                {
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "reason": "invalid_or_quarantined_persisted_receipts",
                }
                for row in observed
                if row["receipt_count"] == 0
            ]
        body = {
            "schema_version": "dawnstrike.strategy_decision_coverage.v1",
            "status": "INCOMPLETE" if invalid_count else "COMPLETE",
            "expected": expected,
            "observed": observed,
            "missing": missing,
            "research_only": True,
            "broker_execution_enabled": False,
            "v6_source_status": "NOT_PROVIDED",
            "source_result": "INTEGRITY_FAILURE" if invalid_count else (
                "NO_EVIDENCE" if not any(row["receipt_count"] for row in observed) else "PROVIDED"
            ),
        }
    body["coverage_hash_sha256"] = _sha256(body)
    return body
