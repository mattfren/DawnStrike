"""Deterministic, research-only daily strategy-learning orchestration.

This module deliberately stops at evidence inventory and unapplied challenger
proposals.  A miss-attribution implementation can be supplied through the
``StrategyEvidenceAnalyzer`` protocol without changing this safety boundary.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import tempfile
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
COMMIT_MANIFEST_SCHEMA = "dawnstrike.strategy_learning_commit_manifest.v1"
EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES = (("alphaops_v5", "dawnstrike-alphaops-v5.0.0"),)
EXPECTED_V6_DECISION_IDENTITY = (
    "alphaops_v6",
    "dawnstrike-alphaops-v6-shadow",
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
_TRUSTED_NO_EVIDENCE_TOKEN = object()
_LEARNING_MANIFEST_KEY_ENV = "DAWNSTRIKE_DAILY_LEARNING_HMAC_KEY"
_LEARNING_MANIFEST_KEY_FILE_ENV = "DAWNSTRIKE_DAILY_LEARNING_HMAC_KEY_FILE"
_LEARNING_MANIFEST_FALLBACK_KEY_ENV = "DAWNSTRIKE_FORWARD_GAP_HMAC_KEY"
_LEARNING_RESERVATION_DOMAIN = b"dawnstrike/strategy-learning/invocation-reservation/v1\0"
_LEARNING_COMMIT_DOMAIN = b"dawnstrike/strategy-learning/commit-manifest/v1\0"


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
        schema_validated: bool = False,
    ) -> None:
        if token is not _TRUSTED_RECEIPT_TOKEN:
            raise TypeError("persisted receipt provenance is private")
        super().__init__(payload)
        self._envelope = dict(envelope)
        self._schema_validated = bool(schema_validated)


def _persisted_receipt(
    payload: Mapping[str, Any],
    *,
    envelope: Mapping[str, Any],
    schema_validated: bool = False,
) -> _PersistedStrategyDecisionReceipt:
    return _PersistedStrategyDecisionReceipt(
        payload,
        envelope=envelope,
        token=_TRUSTED_RECEIPT_TOKEN,
        schema_validated=schema_validated,
    )


class _PersistedV6Decision(dict[str, Any]):
    """Private envelope for decisions loaded from alpha_v6_decisions."""

    def __init__(
        self,
        payload: Mapping[str, Any],
        *,
        envelope: Mapping[str, Any] | None = None,
        token: object,
    ) -> None:
        if token is not _TRUSTED_V6_TOKEN:
            raise TypeError("V6 persisted provenance is private")
        super().__init__(payload)
        self._envelope = dict(envelope or {})


def _persisted_v6_decision(
    payload: Mapping[str, Any], *, envelope: Mapping[str, Any] | None = None
) -> _PersistedV6Decision:
    return _PersistedV6Decision(payload, envelope=envelope, token=_TRUSTED_V6_TOKEN)


class _AuthenticatedNoEvidenceReceipts(tuple):
    """Private acquisition-manifest-bound zero-evidence receipt batch."""

    def __new__(cls, values: Sequence[Mapping[str, Any]], *, token: object):
        if token is not _TRUSTED_NO_EVIDENCE_TOKEN:
            raise TypeError("no-evidence provenance is private")
        return super().__new__(cls, tuple(dict(value) for value in values))


def _authenticated_no_evidence_receipts(
    values: Sequence[Mapping[str, Any]],
) -> _AuthenticatedNoEvidenceReceipts:
    return _AuthenticatedNoEvidenceReceipts(values, token=_TRUSTED_NO_EVIDENCE_TOKEN)


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
        if not re.fullmatch(r"[0-9a-f]{64}", self.source_hash_sha256):
            raise ValueError("source_hash_sha256 must be a canonical lowercase SHA-256 hex digest")
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


def _learning_manifest_key(root: Path) -> bytes:
    """Load the persistent daily-learning MAC key without exposing it."""

    configured = os.environ.get(_LEARNING_MANIFEST_KEY_ENV) or os.environ.get(
        _LEARNING_MANIFEST_FALLBACK_KEY_ENV
    )
    if configured:
        key = configured.encode("utf-8")
    else:
        configured_path = os.environ.get(_LEARNING_MANIFEST_KEY_FILE_ENV)
        if not configured_path:
            raise ValueError(
                f"{_LEARNING_MANIFEST_KEY_ENV}, {_LEARNING_MANIFEST_FALLBACK_KEY_ENV}, or "
                f"{_LEARNING_MANIFEST_KEY_FILE_ENV} is required"
            )
        key_path = Path(configured_path).expanduser().resolve()
        try:
            key_path.relative_to(root.parent.resolve())
        except ValueError:
            pass
        else:
            raise ValueError("daily-learning signing key must be outside the output tree")
        try:
            key = key_path.read_bytes()
        except OSError as exc:
            raise ValueError("daily-learning signing key file is unreadable") from exc
    if len(key) < 32:
        raise ValueError("daily-learning signing key is too short")
    return key


def _verify_hmac_signature(
    payload: Mapping[str, Any], *, body: Mapping[str, Any], domain: bytes, root: Path
) -> None:
    signature = str(payload.get("signature_hmac_sha256") or "")
    if not re.fullmatch(r"[0-9a-f]{64}", signature):
        raise ValueError("daily-learning signed artifact signature is missing")
    expected = hmac.new(
        _learning_manifest_key(root),
        domain + _canonical_json(body).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise ValueError("daily-learning signed artifact signature mismatch")


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


def _artifact_bytes(payload: Mapping[str, Any]) -> bytes:
    return (_canonical_json(payload) + "\n").encode("utf-8")


def _atomic_bytes_once(path: Path, payload: bytes) -> bool:
    """Durably install one immutable file without replacing a winner."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"immutable daily-learning path is a symlink: {path}")
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
            return False
        except FileExistsError:
            if path.is_symlink():
                raise ValueError(f"immutable daily-learning path is a symlink: {path}") from None
            if path.read_bytes() != payload:
                raise ValueError(
                    f"immutable daily-learning artifact changed: {path}"
                ) from None
            return True
    finally:
        temporary.unlink(missing_ok=True)


def _atomic_link_once(source: Path, destination: Path) -> bool:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if source.is_symlink() or destination.is_symlink():
        raise ValueError(
            f"immutable daily-learning generation path is a symlink: {destination}"
        )
    try:
        os.link(source, destination)
        return False
    except FileExistsError:
        if destination.is_symlink():
            raise ValueError(
                f"immutable daily-learning generation path is a symlink: {destination}"
            ) from None
        if destination.read_bytes() != source.read_bytes():
            raise ValueError(
                f"immutable daily-learning artifact changed: {destination}"
            ) from None
        return True


def _commit_manifest_body(
    root: Path,
    *,
    receipt: Mapping[str, Any],
    proposals: Mapping[str, Any],
    generation_id: str,
) -> dict[str, Any]:
    files = {}
    for name, payload in (
        ("daily_learning_receipt.json", receipt),
        ("remediation_proposals.json", proposals),
    ):
        encoded = _artifact_bytes(payload)
        files[name] = {
            "generation_path": f".generations/{generation_id}/{name}",
            "sha256": hashlib.sha256(encoded).hexdigest(),
            "size": len(encoded),
        }
    return {
        "schema_version": COMMIT_MANIFEST_SCHEMA,
        "generation_id": generation_id,
        "run_id": str(receipt.get("run_id") or ""),
        "market_date": str(receipt.get("market_date") or ""),
        "cutoff": str(receipt.get("cutoff") or ""),
        "input_hash_sha256": str(receipt.get("input_hash_sha256") or ""),
        "code_sha": str(receipt.get("code_sha") or ""),
        "files": files,
        "research_only": True,
        "broker_execution_enabled": False,
    }


def _validate_commit_manifest(root: Path) -> dict[str, Any] | None:
    path = root / "daily_learning_commit_manifest.json"
    if path.is_symlink():
        raise ValueError(f"daily-learning commit manifest is a symlink: {root}")
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"daily-learning commit manifest is unreadable: {root}") from exc
    if not isinstance(payload, dict) or payload.get("schema_version") != COMMIT_MANIFEST_SCHEMA:
        raise ValueError(f"daily-learning commit manifest is malformed: {root}")
    if (
        payload.get("research_only") is not True
        or payload.get("broker_execution_enabled") is not False
    ):
        raise ValueError(f"daily-learning commit manifest safety boundary mismatch: {root}")
    commit_hash = payload.get("manifest_sha256")
    body = {
        key: value
        for key, value in payload.items()
        if key not in {"manifest_sha256", "signature_hmac_sha256"}
    }
    if commit_hash != _sha256(body):
        raise ValueError(f"daily-learning commit manifest hash mismatch: {root}")
    signed_body = {
        key: value for key, value in payload.items() if key != "signature_hmac_sha256"
    }
    _verify_hmac_signature(
        payload,
        body=signed_body,
        domain=_LEARNING_COMMIT_DOMAIN,
        root=root,
    )
    files = body.get("files")
    if not isinstance(files, Mapping) or set(files) != {
        "daily_learning_receipt.json",
        "remediation_proposals.json",
    }:
        raise ValueError(f"daily-learning commit manifest file set mismatch: {root}")
    for name, metadata in files.items():
        if not isinstance(metadata, Mapping):
            raise ValueError(f"daily-learning commit manifest entry malformed: {name}")
        relative = str(metadata.get("generation_path") or "")
        source = root / relative
        if (
            source.is_symlink()
            or not source.is_file()
            or source.resolve().parent.parent != (root / ".generations").resolve()
        ):
            raise ValueError(f"daily-learning committed generation file missing: {name}")
        encoded = source.read_bytes()
        if len(encoded) != int(metadata.get("size") or -1) or hashlib.sha256(
            encoded
        ).hexdigest() != metadata.get("sha256"):
            raise ValueError(f"daily-learning committed generation file hash mismatch: {name}")
        destination = root / name
        if destination.is_symlink():
            raise ValueError(f"daily-learning committed artifact is a symlink: {name}")
        if destination.is_file() and destination.read_bytes() != encoded:
            raise ValueError(f"daily-learning committed artifact mismatch: {name}")
        if not destination.exists():
            _atomic_link_once(source, destination)
    return body


def _stage_and_publish_artifacts(
    root: Path,
    *,
    receipt: Mapping[str, Any],
    proposals: Mapping[str, Any],
) -> tuple[Path, Path]:
    receipt_bytes = _artifact_bytes(receipt)
    proposal_bytes = _artifact_bytes(proposals)
    generation_id = hashlib.sha256(
        b"daily-learning-generation-v1\0"
        + hashlib.sha256(receipt_bytes).digest()
        + hashlib.sha256(proposal_bytes).digest()
    ).hexdigest()
    generation = root / ".generations" / generation_id
    _atomic_bytes_once(generation / "daily_learning_receipt.json", receipt_bytes)
    _atomic_bytes_once(generation / "remediation_proposals.json", proposal_bytes)
    # Artifact names are materialized before the commit marker, but the
    # marker is the sole publication authority.  A crash at any prior point
    # is recoverable by rebuilding this same content-addressed generation.
    receipt_path = root / "daily_learning_receipt.json"
    proposal_path = root / "remediation_proposals.json"
    _atomic_link_once(generation / receipt_path.name, receipt_path)
    _atomic_link_once(generation / proposal_path.name, proposal_path)
    body = _commit_manifest_body(
        root,
        receipt=receipt,
        proposals=proposals,
        generation_id=generation_id,
    )
    payload = {**body, "manifest_sha256": _sha256(body)}
    payload["signature_hmac_sha256"] = hmac.new(
        _learning_manifest_key(root),
        _LEARNING_COMMIT_DOMAIN + _canonical_json(payload).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()
    _atomic_bytes_once(
        root / "daily_learning_commit_manifest.json",
        _artifact_bytes(payload),
    )
    return receipt_path, proposal_path


def _reuse_immutable_artifacts(
    root: Path,
    context: DailyLearningContext,
) -> dict[str, Any] | None:
    committed = _validate_commit_manifest(root)
    receipt_path = root / "daily_learning_receipt.json"
    proposal_path = root / "remediation_proposals.json"
    if not receipt_path.exists() and not proposal_path.exists():
        return None
    if committed is None:
        # Staged or legacy files without the final commit marker are not
        # authority.  The caller deterministically rebuilds the same
        # generation and publishes a new marker after every intermediate
        # crash window; an immutable path conflict then fails closed.
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
    proposal_body = {key: value for key, value in proposals.items() if key != "artifact_sha256"}
    if receipt_hash != _sha256(receipt_body) or proposal_hash != _sha256(proposal_body):
        raise ValueError(f"immutable daily-learning artifact hash mismatch: {root}")
    if committed is not None:
        for key in (
            "run_id",
            "market_date",
            "cutoff",
            "input_hash_sha256",
            "code_sha",
        ):
            if committed.get(key) != receipt.get(key):
                raise ValueError(f"daily-learning commit manifest identity mismatch: {key}")
        committed_files = committed.get("files")
        if not isinstance(committed_files, Mapping):
            raise ValueError(f"daily-learning commit manifest files are malformed: {root}")
        for name, artifact in (
            ("daily_learning_receipt.json", receipt),
            ("remediation_proposals.json", proposals),
        ):
            metadata = committed_files.get(name)
            encoded = _artifact_bytes(artifact)
            expected_path = f".generations/{committed.get('generation_id')}/{name}"
            if (
                not isinstance(metadata, Mapping)
                or metadata.get("generation_path") != expected_path
                or metadata.get("sha256") != hashlib.sha256(encoded).hexdigest()
                or metadata.get("size") != len(encoded)
            ):
                raise ValueError(f"daily-learning commit manifest artifact mismatch: {name}")
    coverage = (receipt.get("decision_receipt_learning") or {}).get("expected_strategy_coverage")
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
    for key, value in expected_identity.items():
        if receipt.get(key) != value:
            raise ValueError(
                f"immutable daily-learning invocation identity conflict: {key}: {root}"
            )
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

    return tuple((field, row.get(field)) for field in fields if row.get(field) not in (None, ""))


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


def _is_untrusted_financial_field(field: Any) -> bool:
    """Identify realized-return/P&L/R fields that cannot cross diagnostics."""

    name = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", str(field or "").strip())
    name = re.sub(r"[\s&-]+", "_", name.lower())
    if not name:
        return False
    return bool(
        re.search(
            r"(?:^|_)(?:return|roi|pnl|p_and_l|profit|loss|gain|expectancy|"
            r"p_l|profit_factor|win_rate|loss_rate|drawdown|r|r_multiple|"
            r"risk_reward|risk_reward_ratio)(?:_|$)",
            name,
        )
    )


def _contains_untrusted_financial_field(value: Any) -> bool:
    """Report whether a diagnostic row contains a financial field at any depth."""

    if isinstance(value, Mapping):
        return any(
            _is_untrusted_financial_field(key) or _contains_untrusted_financial_field(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_untrusted_financial_field(item) for item in value)
    return False


def _strip_untrusted_financial_fields(value: Any) -> Any:
    """Copy diagnostics while removing caller-authored performance numbers."""

    if isinstance(value, Mapping):
        return {
            str(key): _strip_untrusted_financial_fields(item)
            for key, item in value.items()
            if not _is_untrusted_financial_field(key)
        }
    if isinstance(value, list):
        return [_strip_untrusted_financial_fields(item) for item in value]
    if isinstance(value, tuple):
        return [_strip_untrusted_financial_fields(item) for item in value]
    return value


def _untrusted_diagnostic_row(
    row: Mapping[str, Any],
    *,
    provenance: str,
    reason: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Make an explicitly untrusted, non-learning copy of one source row."""

    diagnostic = _strip_untrusted_financial_fields(dict(row))
    if not isinstance(diagnostic, dict):  # pragma: no cover - defensive typing guard
        diagnostic = {}
    diagnostic["provenance"] = provenance
    diagnostic["learning_eligible"] = False
    if status:
        diagnostic["status"] = status
    if reason:
        diagnostic["quarantine_reason"] = reason
    return diagnostic


def _diagnostic_row(
    row: Mapping[str, Any],
    *,
    external_untrusted: bool,
    provenance: str,
    reason: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Preserve governed analyzer diagnostics; scrub only external mappings."""

    if external_untrusted:
        return _untrusted_diagnostic_row(
            row,
            provenance=provenance,
            reason=reason,
            status=status,
        )
    diagnostic = dict(row)
    if status:
        diagnostic["status"] = status
    if reason:
        diagnostic["quarantine_reason"] = reason
    return diagnostic


def _normalize_analysis(
    strategy: StrategySpec,
    context: DailyLearningContext,
    raw: Mapping[str, Any],
    *,
    external_untrusted: bool = False,
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    outcomes: list[dict[str, Any]] = []
    misses: list[dict[str, Any]] = []
    excluded_unresolved = 0
    excluded_future = 0
    missing_return = 0
    excluded_ineligible = 0
    terminal_timestamp_quarantined = 0
    evidence_timestamp_quarantined = 0
    untrusted_outcomes_quarantined = 0
    untrusted_financial_fields_scrubbed = 0
    diagnostic_provenance = "untrusted_external" if external_untrusted else "governed_analyzer"
    quarantined_untrusted_outcomes: list[dict[str, Any]] = []
    quarantined_closed = _as_sequence(
        raw.get("quarantined_closed"), "quarantined_closed", strategy.strategy_id
    )
    quarantined_evidence = _as_sequence(
        raw.get("quarantined_evidence"), "quarantined_evidence", strategy.strategy_id
    )

    for row in _as_sequence(raw.get("outcomes"), "outcomes", strategy.strategy_id):
        if external_untrusted:
            untrusted_outcomes_quarantined += 1
            if _contains_untrusted_financial_field(row):
                untrusted_financial_fields_scrubbed += 1
            quarantined_untrusted_outcomes.append(
                _diagnostic_row(
                    row,
                    external_untrusted=external_untrusted,
                    provenance=diagnostic_provenance,
                    reason="committed_point_in_time_fill_truth_required",
                    status="QUARANTINED_UNTRUSTED_OUTCOME",
                )
            )
            continue
        status = str(row.get("status", "")).upper()
        eligibility = str(row.get("eligibility") or "").lower()
        if (
            status in _UNRESOLVED_STATUSES
            or (eligibility and eligibility != "eligible")
            or str(row.get("classification") or "") == "closed_provisional"
        ):
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
            quarantined_closed = (
                *quarantined_closed,
                {
                    **dict(row),
                    "status": "QUARANTINED_TERMINAL_TIMESTAMP",
                    "quarantine_reason": cutoff_reason,
                },
            )
            continue
        if cutoff_reason:
            terminal_timestamp_quarantined += 1
            quarantined_closed = (
                *quarantined_closed,
                {
                    **dict(row),
                    "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                    "quarantine_reason": cutoff_reason,
                },
            )
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
            quarantined_evidence = (
                *quarantined_evidence,
                {
                    **dict(row),
                    "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                    "quarantine_reason": "missing_same_day_evidence_timestamp",
                },
            )
            continue
        cutoff_reason = _cutoff_violation(row, context)
        if cutoff_reason:
            if cutoff_reason and (
                cutoff_reason == "future_market_date" or cutoff_reason.endswith("_after_cutoff")
            ):
                excluded_future += 1
            else:
                evidence_timestamp_quarantined += 1
                quarantined_evidence = (
                    *quarantined_evidence,
                    {
                        **dict(row),
                        "status": "QUARANTINED_EVIDENCE_TIMESTAMP",
                        "quarantine_reason": cutoff_reason,
                    },
                )
            continue
        if external_untrusted and _contains_untrusted_financial_field(row):
            untrusted_financial_fields_scrubbed += 1
        misses.append(
            _diagnostic_row(
                row,
                external_untrusted=external_untrusted,
                provenance=diagnostic_provenance,
            )
        )

    proposals: list[dict[str, Any]] = []
    quarantined_proposals: list[dict[str, Any]] = []
    for raw_proposal in _as_sequence(
        raw.get("proposals", raw.get("remediation_proposals")),
        "proposals",
        strategy.strategy_id,
    ):
        if external_untrusted and _contains_untrusted_financial_field(raw_proposal):
            untrusted_financial_fields_scrubbed += 1
        proposal = (
            _diagnostic_row(
                raw_proposal,
                external_untrusted=external_untrusted,
                provenance=diagnostic_provenance,
            )
        )
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

    claimed_status = str(raw.get("status", "ANALYZED"))
    claimed_evidence_contract = str(raw.get("evidence_contract", "injected_unattributed_v1"))
    evidence = {
        "status": (
            "UNTRUSTED_EXTERNAL_DIAGNOSTICS" if external_untrusted else claimed_status
        ),
        "provenance": diagnostic_provenance,
        "outcome_learning_contract": "committed_point_in_time_fill_truth_required",
        "outcomes": outcomes,
        "misses": misses,
        "counts": {
            "outcomes_retained": len(outcomes),
            "misses_retained": len(misses),
            "proposals_retained": len(proposals),
            "untrusted_outcomes_quarantined": untrusted_outcomes_quarantined,
            "untrusted_financial_fields_scrubbed": untrusted_financial_fields_scrubbed,
            "unresolved_outcomes_excluded": excluded_unresolved,
            "ineligible_outcomes_excluded": excluded_ineligible,
            "future_evidence_excluded": excluded_future,
            "terminal_timestamp_quarantined": terminal_timestamp_quarantined,
            "evidence_timestamp_quarantined": evidence_timestamp_quarantined,
            "outcomes_without_return_excluded_from_return_metrics": missing_return,
            "closed_provisional_quarantined": len(quarantined_closed),
            "proposals_quarantined": len(quarantined_proposals),
        },
        "evidence_contract": (
            "dawnstrike.untrusted_external_mapping.v1"
            if external_untrusted
            else claimed_evidence_contract
        ),
        "quarantined_closed": [
            _diagnostic_row(
                row,
                external_untrusted=external_untrusted,
                provenance=diagnostic_provenance,
            )
            for row in quarantined_closed
        ],
        "quarantined_untrusted_outcomes": quarantined_untrusted_outcomes,
        "quarantined_evidence": [
            _diagnostic_row(
                row,
                external_untrusted=external_untrusted,
                provenance=diagnostic_provenance,
            )
            for row in quarantined_evidence
        ],
        "quarantined_proposals": quarantined_proposals,
    }
    if external_untrusted:
        if "status" in raw:
            evidence["claimed_status"] = claimed_status
        if "evidence_contract" in raw:
            evidence["claimed_evidence_contract"] = claimed_evidence_contract
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
        key: item for key, item in value.items() if key not in {"receipt_id", "receipt_hash_sha256"}
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
    if getattr(value, "_schema_validated", False):
        # Read-only database ingress reconstructs the typed receipt before it
        # creates this envelope.  Re-run that assertion after a frozen
        # snapshot restore so a tampered nested condition cannot become
        # learning evidence merely because its outer hash was copied.
        try:
            from intraday_scanner.decisioning.contracts import (
                ConditionResult,
                StrategyDecisionReceipt,
            )

            raw_conditions = value.get("condition_results")
            if not isinstance(raw_conditions, list) or any(
                not isinstance(item, Mapping) for item in raw_conditions
            ):
                return False, "receipt_schema_invalid"
            typed_receipt = StrategyDecisionReceipt(
                **{
                    **dict(value),
                    "condition_results": tuple(
                        ConditionResult(**dict(item)) for item in raw_conditions
                    ),
                }
            )
            if typed_receipt.canonical_json() != _canonical_json(dict(value)):
                return False, "receipt_schema_invalid"
        except (TypeError, ValueError, KeyError):
            return False, "receipt_schema_invalid"
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
    """Honor a CLI-owned signed reservation before analyzer work starts.

    This reservation closes the crash window between invoking the stage and
    writing its final receipt.  Retries reuse the original point-in-time
    boundary; conflicting source/input/code identity remains a named failure.
    Direct library calls do not create an unsigned persisted authority.
    """

    path = root / "daily_learning_invocation.json"
    body = {
        "schema_version": DAILY_LEARNING_SCHEMA,
        "reservation_phase": 1,
        "reserved_at": datetime.now(UTC).isoformat(),
        "market_date": context.market_date,
        "cutoff": context.cutoff,
        "source_identity": context.source_identity,
        "source_hash_sha256": context.source_hash_sha256,
        "input_hash_sha256": context.input_hash_sha256,
        "code_sha": context.code_sha,
    }
    if path.is_symlink():
        raise ValueError("daily-learning invocation reservation is a symlink")
    if path.exists():
        try:
            persisted = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("daily-learning invocation reservation is unreadable") from exc
        if not isinstance(persisted, dict):
            raise ValueError("daily-learning invocation reservation is not an object")
        if not re.fullmatch(
            r"[0-9a-f]{64}", str(persisted.get("signature_hmac_sha256") or "")
        ):
            raise ValueError("daily-learning invocation reservation is unauthenticated")
        reservation_hash = persisted.get("reservation_sha256")
        stored_body = {
            key: value
            for key, value in persisted.items()
            if key not in {"reservation_sha256", "signature_hmac_sha256"}
        }
        if reservation_hash != _sha256(stored_body):
            raise ValueError("daily-learning invocation reservation hash mismatch")
        _verify_hmac_signature(
            persisted,
            body=stored_body,
            domain=_LEARNING_RESERVATION_DOMAIN,
            root=root,
        )
        for key in (
            "market_date",
            "source_identity",
            "source_hash_sha256",
            "code_sha",
        ):
            if persisted.get(key) != body[key]:
                raise ValueError(f"daily-learning invocation identity conflict: {key}")
        persisted_input_hash = str(persisted.get("input_hash_sha256") or "")
        if persisted_input_hash and persisted_input_hash != context.input_hash_sha256:
            raise ValueError("daily-learning invocation identity conflict: input_hash_sha256")
        frozen = DailyLearningContext(
            market_date=str(persisted["market_date"]),
            cutoff=str(persisted["cutoff"]),
            source_identity=str(persisted["source_identity"]),
            code_sha=str(persisted["code_sha"]),
            source_hash_sha256=str(persisted["source_hash_sha256"]),
            input_hash_sha256=str(persisted.get("input_hash_sha256") or context.input_hash_sha256),
        )
        return frozen
    # The governed CLI installs the signed phase-1 reservation before calling
    # this service.  Library callers without that acquisition boundary may
    # still produce research-only artifacts, but must not mint an unsigned
    # persisted invocation authority here.
    return context


def _external_input_identity(value: Sequence[Mapping[str, Any]] | None) -> Any:
    """Canonicalize supplied receipt batches, including rejected diagnostics."""

    if value is None:
        return None
    return {
        "accepted": [dict(item) for item in value],
        "invalid_identities": list(getattr(value, "invalid_identities", ())),
        "invalid_reasons": dict(getattr(value, "invalid_reasons", {})),
    }


def _authenticated_zero_receipt(
    receipts: Sequence[Mapping[str, Any]] | None,
    *,
    lane: str,
    strategy_id: str,
    strategy_version: str,
    market_date: str,
    cutoff: str,
) -> Mapping[str, Any] | None:
    """Return a matching manifest-bound zero receipt, if one was supplied."""

    if not isinstance(receipts, _AuthenticatedNoEvidenceReceipts):
        return None
    for receipt in receipts:
        query = receipt.get("query")
        body = {
            key: value for key, value in receipt.items() if key != "receipt_sha256"
        }
        digest = str(receipt.get("receipt_sha256") or "")
        if (
            receipt.get("schema_version") == "dawnstrike.strategy_learning_no_evidence.v1"
            and receipt.get("receipt_type") == "no_evidence"
            and receipt.get("lane") == lane
            and receipt.get("strategy_id") == strategy_id
            and receipt.get("strategy_version") == strategy_version
            and receipt.get("market_date") == market_date
            and receipt.get("cutoff") == cutoff
            and receipt.get("zero_count") == 0
            and receipt.get("no_trade") is True
            and re.fullmatch(r"[0-9a-f]{64}", digest)
            and digest == _sha256(body)
            and re.fullmatch(r"[0-9a-f]{64}", str(receipt.get("reservation_sha256") or ""))
            and re.fullmatch(
                r"[0-9a-f]{64}", str(receipt.get("acquisition_manifest_sha256") or "")
            )
            and re.fullmatch(
                r"[0-9a-f]{64}", str(receipt.get("source_component_hash_sha256") or "")
            )
            and re.fullmatch(
                r"[0-9a-f]{64}", str(receipt.get("source_generation_hash_sha256") or "")
            )
            and isinstance(query, Mapping)
            and query.get("kind") == "point_in_time_zero_query"
            and query.get("market_date") == market_date
            and query.get("cutoff") == cutoff
            and query.get("strategy_id") == strategy_id
            and query.get("strategy_version") == strategy_version
        ):
            return receipt
    return None


def _has_authenticated_zero_receipt(
    receipts: Sequence[Mapping[str, Any]] | None,
    *,
    lane: str,
    strategy_id: str,
    strategy_version: str,
    market_date: str,
    cutoff: str,
) -> bool:
    return (
        _authenticated_zero_receipt(
            receipts,
            lane=lane,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            market_date=market_date,
            cutoff=cutoff,
        )
        is not None
    )


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
    no_evidence_receipts: Sequence[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Inventory the catalog and write one immutable research-only daily run."""

    source_hash = source_hash_sha256 or hashlib.sha256(source_identity.encode("utf-8")).hexdigest()
    if input_hash_sha256 is None:
        supplied_inputs = {
            name: _external_input_identity(value)
            for name, value in (
                ("decision_receipts", decision_receipts),
                ("v6_decisions", v6_decisions),
            )
            if value is not None
        }
        if supplied_inputs:
            input_hash_sha256 = _sha256(supplied_inputs)
        elif analyzer is not None and not isinstance(analyzer, EmptyEvidenceAnalyzer):
            raise ValueError(
                "input_hash_sha256 is required for non-empty external strategy evidence"
            )
    context = DailyLearningContext(
        market_date=market_date,
        cutoff=cutoff,
        source_identity=source_identity,
        code_sha=code_sha,
        source_hash_sha256=source_hash,
        input_hash_sha256=input_hash_sha256 or source_hash,
    )
    authenticated_zero_receipts = (
        no_evidence_receipts
        if isinstance(no_evidence_receipts, _AuthenticatedNoEvidenceReceipts)
        else None
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
    valid_v6 = tuple(row for row in (v6_decisions or ()) if isinstance(row, _PersistedV6Decision))
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
    if (
        v6_source_status == "NO_EVIDENCE"
        and _has_authenticated_zero_receipt(
            authenticated_zero_receipts,
            lane="v6",
            strategy_id=EXPECTED_V6_DECISION_IDENTITY[0],
            strategy_version=EXPECTED_V6_DECISION_IDENTITY[1],
            market_date=context.market_date,
            cutoff=context.cutoff,
        )
    ):
        v6_source_status = "CHECKED_ZERO_AUTHENTICATED"
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
        external_untrusted = isinstance(analyzer, MappingEvidenceAnalyzer)
        evidence, strategy_proposals = _normalize_analysis(
            strategy,
            context,
            raw,
            external_untrusted=external_untrusted,
        )
        raw_status = str(raw.get("status") or "").upper()
        # Keep the two predicates separate.  Raw evidence proves that the
        # source was non-empty, while retained evidence proves that at least
        # one point-in-time row survived normalization.  In particular, a
        # source containing only malformed/future/quarantined rows is not an
        # authenticated zero cohort; it remains incomplete unless the CLI's
        # private acquisition receipt explicitly proves zero rows.
        raw_evidence_present = any(
            bool(raw.get(field))
            for field in (
                "outcomes",
                "misses",
                "proposals",
                "remediation_proposals",
                "quarantined_closed",
                "quarantined_evidence",
                "quarantined_proposals",
            )
        )
        retained_evidence_present = bool(
            evidence.get("outcomes")
            or evidence.get("misses")
            or strategy_proposals
        )
        if raw.get("source_status"):
            source_status = str(raw["source_status"]).upper()
        elif raw_status in {"NO_ANALYSIS", ""} and isinstance(analyzer, EmptyEvidenceAnalyzer):
            source_status = "NOT_PROVIDED"
        elif raw_status in {"NO_RETAINED_ROWS", "NO_EVIDENCE"}:
            source_status = "CHECKED_ZERO"
        elif not raw:
            source_status = "NOT_PROVIDED"
        elif not raw_evidence_present:
            source_status = "NOT_PROVIDED"
        elif not retained_evidence_present:
            source_status = "CHECKED_ZERO"
        else:
            source_status = "CHECKED"
        if external_untrusted and raw_evidence_present:
            # Caller-authored mappings are diagnostics only: neither rejected
            # outcomes nor retained misses/proposals authenticate a cohort.
            # A supplied source_status cannot upgrade this boundary.
            source_status = (
                "QUARANTINED_UNTRUSTED"
                if evidence["counts"]["untrusted_outcomes_quarantined"]
                else "UNTRUSTED_EXTERNAL_DIAGNOSTICS"
            )
        # A status supplied inside an analyzer mapping is descriptive only;
        # it cannot turn an empty or entirely quarantined result into a
        # checked source.  Only the private acquisition-manifest receipt can
        # authenticate an explicit zero cohort.
        if source_status == "CHECKED_ZERO_AUTHENTICATED":
            source_status = "CHECKED_ZERO"
        if not retained_evidence_present and source_status == "CHECKED":
            source_status = "CHECKED_ZERO"
        if source_status == "CHECKED_ZERO" and _has_authenticated_zero_receipt(
            authenticated_zero_receipts,
            lane="strategy",
            strategy_id=strategy.strategy_id,
            strategy_version=strategy.version,
            market_date=context.market_date,
            cutoff=context.cutoff,
        ):
            source_status = "CHECKED_ZERO_AUTHENTICATED"
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
        no_evidence_receipts=authenticated_zero_receipts,
        market_date=context.market_date,
        cutoff=context.cutoff,
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
        str(item["evidence"].get("source_status") or "INCOMPLETE") for item in strategy_evidence
    ]
    strategy_coverage_incomplete = any(
        status not in {"CHECKED", "CHECKED_ZERO_AUTHENTICATED"}
        for status in strategy_source_statuses
    )
    strategy_coverage = {
        "schema_version": "dawnstrike.strategy_learning_strategy_coverage.v1",
        "required_strategy_count": len(strategies),
        "required": [
            {"strategy_id": item["strategy_id"], "strategy_version": item["strategy_version"]}
            for item in inventory
        ],
        "observed": [
            {
                "strategy_id": item["strategy_id"],
                "strategy_version": item["strategy_version"],
                "source_status": item["evidence"].get("source_status"),
                "evidence_count": int(
                    sum(
                        int(item["evidence"].get("counts", {}).get(field) or 0)
                        for field in ("outcomes_retained", "misses_retained", "proposals_retained")
                    )
                ),
            }
            for item in strategy_evidence
        ],
        "status": "INCOMPLETE" if strategy_coverage_incomplete else "COMPLETE",
        "missing": [
            {
                "strategy_id": item["strategy_id"],
                "strategy_version": item["strategy_version"],
                "reason": "checked_zero_requires_authenticated_no_evidence_receipt"
                if item["evidence"].get("source_status") == "CHECKED_ZERO"
                else "source_not_checked",
            }
            for item in strategy_evidence
            if item["evidence"].get("source_status")
            not in {"CHECKED", "CHECKED_ZERO_AUTHENTICATED"}
        ],
        "research_only": True,
        "broker_execution_enabled": False,
    }
    strategy_coverage["coverage_hash_sha256"] = _sha256(
        {key: value for key, value in strategy_coverage.items() if key != "coverage_hash_sha256"}
    )
    receipt_learning["strategy_coverage"] = strategy_coverage
    receipt_coverage["strategy_coverage"] = strategy_coverage
    receipt_coverage["v6_identity"] = {
        "strategy_id": EXPECTED_V6_DECISION_IDENTITY[0],
        "strategy_version": EXPECTED_V6_DECISION_IDENTITY[1],
    }
    receipt_coverage["status"] = (
        "INCOMPLETE"
        if receipt_coverage.get("status") != "COMPLETE"
        or v6_source_status in {"NOT_PROVIDED", "NO_EVIDENCE", "INTEGRITY_FAILURE"}
        or strategy_coverage_incomplete
        else "COMPLETE"
    )
    receipt_coverage["coverage_hash_sha256"] = _sha256(
        {key: value for key, value in receipt_coverage.items() if key != "coverage_hash_sha256"}
    )
    run_status = (
        "complete"
        if receipt_coverage["status"] == "COMPLETE"
        and v6_source_status
        not in {"NOT_PROVIDED", "NO_EVIDENCE", "INTEGRITY_FAILURE"}
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
    _stage_and_publish_artifacts(
        root,
        receipt=receipt,
        proposals=proposal_payload,
    )
    reused_receipt = False
    reused_proposals = False
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
            str(item) for item in receipt.get("all_blocking_failures") or () if str(item).strip()
        }
        disclosed_ids = {
            str(item) for item in receipt.get("disclosed_gaps") or () if str(item).strip()
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

            if (
                raw.get("ai_claim_contradicted") is True
                or raw.get("contradicted_by_authoritative_source") is True
            ):
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
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + int(
            observation["receipt_count"]
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
            authoritative_contradictions[key] for key in sorted(authoritative_contradictions)
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
    no_evidence_receipts: Sequence[Mapping[str, Any]] | None = None,
    market_date: str | None = None,
    cutoff: str | None = None,
) -> dict[str, Any]:
    """Emit an immutable expected-cohort coverage receipt.

    ``None`` means the caller did not provide a persisted receipt source.  An
    An explicit empty sequence is incomplete unless the authenticated
    acquisition manifest supplied a matching no-evidence receipt for the
    required AlphaOps V5 identity/date.
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
        zero_authenticated = (
            not invalid_count
            and not any(row["receipt_count"] for row in observed)
            and _has_authenticated_zero_receipt(
                no_evidence_receipts,
                lane="v5",
                strategy_id=EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES[0][0],
                strategy_version=EXPECTED_ALPHAOPS_DECISION_RECEIPT_IDENTITIES[0][1],
                market_date=str(market_date or ""),
                cutoff=str(cutoff or ""),
            )
        )
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
            "status": (
                "INCOMPLETE"
                if invalid_count
                or (not any(row["receipt_count"] for row in observed) and not zero_authenticated)
                else "COMPLETE"
            ),
            "expected": expected,
            "observed": observed,
            "missing": []
            if zero_authenticated
            else missing
            or [
                {
                    "strategy_id": row["strategy_id"],
                    "strategy_version": row["strategy_version"],
                    "reason": "no_authenticated_explicit_no_evidence_receipt",
                }
                for row in observed
                if row["receipt_count"] == 0
            ],
            "research_only": True,
            "broker_execution_enabled": False,
            "v6_source_status": "NOT_PROVIDED",
            "source_result": "INTEGRITY_FAILURE"
            if invalid_count
            else "CHECKED_ZERO_AUTHENTICATED"
            if zero_authenticated
            else (
                "NO_EVIDENCE" if not any(row["receipt_count"] for row in observed) else "PROVIDED"
            ),
        }
    body["coverage_hash_sha256"] = _sha256(body)
    return body
