"""Frozen, research-only after-cost utility comparison for strategy scores.

Strategy scores in the legacy catalog are not promised to share a semantic
scale.  This module gives the research surface one explicitly frozen scale and
records the conversion, while leaving the champion slate untouched.  It does
not select, publish, or promote a strategy.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.empirical_execution_cost_challenger import (
    EMPIRICAL_COST_CHALLENGER_VERSION,
    _canonical_session_date,
    _observation_cost_bps,
    _point_in_time_quote_fill,
    _window_contains,
)
from intraday_scanner.alpha.empirical_execution_cost_challenger import (
    FROZEN_CONFIGURATION as EMPIRICAL_COST_CONFIGURATION,
)
from intraday_scanner.alpha.fill_truth import has_authenticated_committed_fill_truth

SCHEMA_VERSION = "dawnstrike.alpha.shadow_utility_reranker.v1"
RERANKER_VERSION = "dawnstrike-after-cost-utility-reranker-20260829.v1"
PROVISIONAL_COST_MODEL_VERSION = "alphaops-v5-cost-model-50bps-0.005ps"
PROVISIONAL_COST_ASSUMPTIONS: dict[str, Any] = {
    "entry_slippage_bps": 50.0,
    "exit_slippage_bps": 50.0,
    "commission_per_share_per_side": 0.005,
}
# These are intentionally literals: changing one is a new reranker version.
FROZEN_CONFIGURATION: dict[str, Any] = {
    "utility_lcb_field": "expected_return_lcb_pct",
    "calibration_input_field": "oos_calibration",
    "cost_bps_field": "expected_cost_bps",
    "cost_model_version_field": "cost_model_version",
    "score_unit": "governed_expected_return_lcb_pct",
    "basis_points_to_return_pct": 0.01,
    "missing_utility_lcb_policy": "blocked_null",
    "missing_cost_policy": "explicit_missing_blocked_null",
    "tie_break": "strategy_id_then_strategy_version",
    "champion_slate_mutation": False,
    "provisional_cost_model_version": PROVISIONAL_COST_MODEL_VERSION,
}


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
        ).encode("utf-8")
    ).hexdigest()


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _score(row: Mapping[str, Any]) -> float | None:
    return _number(row.get("expected_return_lcb_pct", row.get("utility_lcb_pct")))


def _cost_bps(row: Mapping[str, Any]) -> float | None:
    return _number(row.get("expected_cost_bps", row.get("cost_bps")))


def _is_hash(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    if len(text) != 64:
        return False
    return all(char in "0123456789abcdef" for char in text)


def _is_code_identity(value: object) -> bool:
    if not isinstance(value, str):
        return False
    text = value
    if len(text) not in {40, 64}:
        return False
    return all(char in "0123456789abcdef" for char in text)


def _calibration_is_bound(row: Mapping[str, Any]) -> bool:
    """Require a governed LCB receipt identity, never an arbitrary score."""

    calibration = row.get("oos_calibration")
    if not isinstance(calibration, Mapping):
        calibration = row.get("governed_expected_return_lcb_receipt")
    if not isinstance(calibration, Mapping):
        return False
    output = calibration.get("output")
    if not isinstance(output, Mapping):
        return False
    output_payload = dict(output)
    expected = _number(row.get("expected_return_lcb_pct", row.get("utility_lcb_pct")))
    unsigned_calibration = {
        key: value for key, value in calibration.items() if key != "receipt_hash_sha256"
    }
    required = (
        calibration.get("status") == "AUTHENTICATED_OOS_CALIBRATION",
        _is_hash(calibration.get("input_hash_sha256")),
        _is_hash(calibration.get("output_hash_sha256")),
        _is_hash(calibration.get("source_hash_sha256")),
        _is_hash(calibration.get("configuration_hash_sha256")),
        _is_hash(calibration.get("model_hash_sha256")),
        _is_hash(calibration.get("window_hash_sha256")),
        _is_hash(calibration.get("receipt_hash_sha256")),
        calibration.get("receipt_hash_sha256") == canonical_hash(unsigned_calibration),
        calibration.get("output_hash_sha256") == canonical_hash(output_payload),
        _number(output_payload.get("expected_return_lcb_pct")) == expected,
        str(output_payload.get("strategy_id") or "") == str(row.get("strategy_id") or ""),
        str(output_payload.get("strategy_version") or "")
        == str(row.get("strategy_version") or ""),
        str(output_payload.get("model_run_id") or "")
        == str(calibration.get("model_run_id") or ""),
        str(output_payload.get("window_id") or "") == str(calibration.get("window_id") or ""),
        output_payload.get("configuration_hash_sha256")
        == calibration.get("configuration_hash_sha256"),
        output_payload.get("model_hash_sha256") == calibration.get("model_hash_sha256"),
        output_payload.get("source_hash_sha256") == calibration.get("source_hash_sha256"),
        output_payload.get("window_hash_sha256") == calibration.get("window_hash_sha256"),
        output_payload.get("decision_id") == calibration.get("decision_id"),
        output_payload.get("code_sha") == calibration.get("code_sha"),
        calibration.get("input_hash_sha256") == row.get("input_hash_sha256"),
        calibration.get("source_hash_sha256") == row.get("source_manifest_hash_sha256"),
        calibration.get("configuration_hash_sha256")
        == row.get("calibration_configuration_hash_sha256"),
        calibration.get("model_hash_sha256") == row.get("calibration_model_hash_sha256"),
        calibration.get("window_hash_sha256") == row.get("window_hash_sha256"),
        calibration.get("decision_id") == row.get("decision_id"),
        calibration.get("code_sha") == row.get("code_sha"),
        _is_code_identity(calibration.get("code_sha")),
        bool(str(calibration.get("model_run_id") or "").strip()),
        bool(str(calibration.get("window_id") or "").strip()),
        str(calibration.get("model_run_id")) == str(row.get("model_run_id") or ""),
        str(calibration.get("window_id")) == str(row.get("window_id") or ""),
    )
    return all(required)


def _cost_is_bound(row: Mapping[str, Any]) -> bool:
    """Consume only a producer receipt whose evidence remains authenticated."""

    receipt = row.get("cost_receipt")
    output = receipt.get("output") if isinstance(receipt, Mapping) else None
    if not isinstance(receipt, Mapping) or not isinstance(output, Mapping):
        return False
    model_version = str(row.get("cost_model_version") or "")
    unsigned_receipt = {
        key: value for key, value in receipt.items() if key != "receipt_hash_sha256"
    }
    selected = _number(row.get("expected_cost_bps", row.get("cost_bps")))
    quantile = str(row.get("cost_quantile") or "").strip().lower()
    evidence_rows = receipt.get("evidence_rows")
    selected_output = output.get(f"{quantile}_cost_bps") if quantile in {"p75", "p90"} else None
    def _evidence_is_bound(evidence: object) -> bool:
        if not isinstance(evidence, Mapping):
            return False
        observed = _nonnegative_number(evidence.get("observed_cost_bps"))
        if (
            observed is None
            or not _point_in_time_quote_fill(evidence)
            or not has_authenticated_committed_fill_truth(evidence)
        ):
            return False
        recomputed = _observation_cost_bps(evidence)
        return recomputed is not None and math.isclose(
            float(recomputed), observed, rel_tol=0.0, abs_tol=1e-9
        )

    evidence_ids = [
        str(evidence.get("observation_id") or "")
        for evidence in evidence_rows
        if isinstance(evidence, Mapping)
    ] if isinstance(evidence_rows, list) else []
    evidence_sessions = {
        session
        for evidence in evidence_rows
        if isinstance(evidence, Mapping)
        and (session := _canonical_session_date(evidence)) is not None
    } if isinstance(evidence_rows, list) else set()
    receipt_window = receipt.get("window")
    evidence_window_bound = isinstance(receipt_window, Mapping) and all(
        _window_contains(session, receipt_window) for session in evidence_sessions
    )
    required = (
        receipt.get("status") == "EMPIRICAL_COST_EVALUABLE",
        receipt.get("challenger_version") == EMPIRICAL_COST_CHALLENGER_VERSION,
        receipt.get("research_only") is True,
        receipt.get("broker_execution_enabled") is False,
        isinstance(evidence_rows, list),
        bool(evidence_rows),
        _count_at_least(receipt.get("authenticated_observation_count"), 20),
        _count_at_least(receipt.get("authenticated_session_count"), 5),
        receipt.get("minimum_observations_met") is True,
        receipt.get("minimum_sessions_met") is True,
        receipt.get("input_observations_hash_sha256")
        == row.get("cost_input_observations_hash_sha256"),
        _is_hash(receipt.get("input_observations_hash_sha256")),
        _is_hash(receipt.get("output_hash_sha256")),
        _is_hash(receipt.get("configuration_hash_sha256")),
        _is_hash(receipt.get("model_hash_sha256")),
        _is_hash(receipt.get("window_hash_sha256")),
        _is_hash(receipt.get("receipt_hash_sha256")),
        _is_hash(row.get("cost_receipt_hash_sha256")),
        receipt.get("receipt_hash_sha256") == row.get("cost_receipt_hash_sha256"),
        receipt.get("receipt_hash_sha256") == canonical_hash(unsigned_receipt),
        receipt.get("configuration_hash_sha256") == canonical_hash(EMPIRICAL_COST_CONFIGURATION),
        isinstance(receipt.get("configuration"), Mapping)
        and receipt.get("configuration_hash_sha256")
        == canonical_hash(receipt.get("configuration")),
        receipt.get("output_hash_sha256") == canonical_hash(dict(output)),
        receipt.get("source_manifest_hash_sha256")
        == row.get("cost_source_manifest_hash_sha256"),
        isinstance(receipt.get("source_manifest"), Mapping)
        and receipt.get("source_manifest_hash_sha256")
        == canonical_hash(receipt.get("source_manifest")),
        receipt.get("window_hash_sha256") == row.get("cost_window_hash_sha256"),
        evidence_window_bound,
        isinstance(receipt_window, Mapping)
        and receipt.get("window_hash_sha256") == canonical_hash(receipt_window),
        receipt.get("code_sha") == row.get("cost_code_sha"),
        _is_code_identity(receipt.get("code_sha")),
        str(receipt.get("model_version") or "") == model_version,
        model_version == EMPIRICAL_COST_CHALLENGER_VERSION,
        receipt.get("model_hash_sha256") == canonical_hash(
            {
                "model_version": EMPIRICAL_COST_CHALLENGER_VERSION,
                "configuration_hash_sha256": canonical_hash(EMPIRICAL_COST_CONFIGURATION),
            }
        ),
        quantile in {"p75", "p90"},
        _number(selected_output) == selected,
        output.get("model_version") == model_version,
        receipt.get("provisional_champion_cost_model_version")
        == PROVISIONAL_COST_MODEL_VERSION,
        receipt.get("provisional_champion_cost_model_unchanged") is True,
        receipt.get("authenticated_observation_count") == len(evidence_rows)
        if isinstance(evidence_rows, list)
        else False,
        len(evidence_ids) == len(evidence_rows)
        and all(evidence_ids)
        and len(evidence_ids) == len(set(evidence_ids))
        if isinstance(evidence_rows, list)
        else False,
        receipt.get("authenticated_session_count") == len(evidence_sessions)
        if isinstance(evidence_rows, list)
        else False,
        all(_evidence_is_bound(evidence) for evidence in evidence_rows)
        if isinstance(evidence_rows, list)
        else False,
    )
    return all(required)


def _nonnegative_number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) and number >= 0 else None


def _count_at_least(value: object, minimum: int) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value >= minimum


def rerank_strategy_scores(
    rows: Sequence[Mapping[str, Any]],
    *,
    configuration: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Return a copied, shadow-only after-cost ranking.

    Missing score or cost is represented by a null utility and a blocked
    status.  In particular, missing cost is never interpreted as zero.  The
    input sequence is never modified and no champion selection is returned.
    """

    config = dict(FROZEN_CONFIGURATION if configuration is None else configuration)
    if config != FROZEN_CONFIGURATION:
        raise ValueError("the Cycle-2 reranker configuration is frozen")
    scored: list[dict[str, Any]] = []
    identity_rows: dict[str, list[dict[str, Any]]] = {}
    for source in rows:
        identity = str(source.get("decision_id") or source.get("row_id") or "").strip()
        if identity:
            identity_rows.setdefault(identity, []).append(dict(source))
    quarantined_identities = {
        identity
        for identity, members in identity_rows.items()
        if len(members) > 1
    }
    for source in rows:
        row = dict(source)
        row_identity = str(row.get("decision_id") or row.get("row_id") or "").strip()
        duplicate = not row_identity or row_identity in quarantined_identities
        score = _score(source)
        cost_bps = _cost_bps(source)
        if duplicate:
            utility = None
            status = "BLOCKED_DUPLICATE_OR_MISSING_ROW_IDENTITY"
        elif score is None:
            utility = None
            status = "BLOCKED_MISSING_EXPECTED_RETURN_LCB"
        elif cost_bps is None:
            utility = None
            status = "BLOCKED_MISSING_EXECUTION_COST"
        elif (
            source.get("research_only") is not True
            or source.get("broker_execution_enabled") is not False
        ):
            utility = None
            status = "BLOCKED_RESEARCH_ONLY_BROKER_CONTRACT"
        elif not str(source.get("cost_model_version") or "").strip():
            utility = None
            status = "BLOCKED_MISSING_COST_MODEL_VERSION"
        elif not _calibration_is_bound(source):
            utility = None
            status = "BLOCKED_MISSING_AUTHENTICATED_OOS_CALIBRATION"
        elif not _cost_is_bound(source):
            utility = None
            status = "BLOCKED_MISSING_AUTHENTICATED_COST_EVIDENCE"
        elif cost_bps < 0:
            utility = None
            status = "BLOCKED_INVALID_EXECUTION_COST"
        else:
            utility = round(
                score - cost_bps * float(config["basis_points_to_return_pct"]),
                8,
            )
            status = "EVALUABLE_SHADOW_ONLY_GOVERNED_LCB"
        row.update(
            {
                "reranker_version": RERANKER_VERSION,
                "gross_score": score,
                "expected_cost_bps": cost_bps,
                "after_cost_utility_pct": utility,
                "after_cost_utility_status": status,
                "research_only": True,
                "promotion_eligible": False,
                "champion_slate_unchanged": True,
            }
        )
        scored.append(row)
    evaluable = [row for row in scored if row["after_cost_utility_pct"] is not None]
    evaluable.sort(
        key=lambda row: (
            -float(row["after_cost_utility_pct"]),
            str(row.get("strategy_id") or ""),
            str(row.get("strategy_version") or ""),
        )
    )
    rank_by_identity = {
        str(row.get("decision_id") or row.get("row_id")): index
        for index, row in enumerate(evaluable, start=1)
    }
    for row in scored:
        identity = str(row.get("decision_id") or row.get("row_id") or "")
        row["after_cost_rank"] = rank_by_identity.get(identity)
    return scored


def build_shadow_utility_receipt(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_manifest: Mapping[str, Any],
    code_sha: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    """Build a hash-bound reranker receipt; this function never promotes."""

    if not _is_code_identity(code_sha):
        raise ValueError("code_sha is required")
    if not isinstance(source_manifest, Mapping) or not source_manifest:
        raise ValueError("source_manifest is required")
    if not isinstance(window, Mapping) or not window:
        raise ValueError("evaluation window is required")
    source_rows = sorted((dict(row) for row in rows), key=canonical_hash)
    ranked = rerank_strategy_scores(source_rows)
    source_hash = canonical_hash(source_manifest)
    window_hash = canonical_hash(window)
    for row in ranked:
        if not (
            row.get("source_manifest_hash_sha256") == source_hash
            and row.get("window_hash_sha256") == window_hash
            and row.get("code_sha") == code_sha
        ):
            row["after_cost_utility_pct"] = None
            row["after_cost_utility_status"] = "BLOCKED_ENCLOSING_LINEAGE_MISMATCH"
            row["after_cost_rank"] = None
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "reranker_version": RERANKER_VERSION,
        "configuration": dict(FROZEN_CONFIGURATION),
        "configuration_hash_sha256": canonical_hash(FROZEN_CONFIGURATION),
        "input_rows_hash_sha256": canonical_hash(source_rows),
        "source_manifest": dict(source_manifest),
        "source_manifest_hash_sha256": canonical_hash(source_manifest),
        "code_sha": str(code_sha),
        "window": dict(window),
        "window_hash_sha256": canonical_hash(window),
        "rows": ranked,
        "research_only": True,
        "promotion_eligible": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "champion_slate_unchanged": True,
        "champion_cost_model_version": PROVISIONAL_COST_MODEL_VERSION,
        "champion_cost_model_assumptions": dict(PROVISIONAL_COST_ASSUMPTIONS),
        "missing_outcomes_are_zero": False,
    }
    receipt["receipt_hash_sha256"] = canonical_hash(receipt)
    return receipt


def persist_immutable_receipt(path: str | Path, receipt: Mapping[str, Any]) -> bool:
    """Write once, or verify byte identity when the receipt already exists."""

    target = Path(path)
    declared_hash = str(receipt.get("receipt_hash_sha256") or "")
    unsigned = {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    if not declared_hash or declared_hash != canonical_hash(unsigned):
        raise ValueError("receipt self-hash is missing or invalid")
    target.parent.mkdir(parents=True, exist_ok=True)
    encoded = (
        json.dumps(dict(receipt), sort_keys=True, separators=(",", ":"), allow_nan=False)
        + "\n"
    )
    if target.exists():
        if target.read_text(encoding="utf-8") != encoded:
            raise ValueError(f"immutable receipt changed: {target}")
        return True
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary_name, target)
        except FileExistsError:
            if target.read_text(encoding="utf-8") != encoded:
                raise ValueError(f"immutable receipt changed: {target}") from None
            return True
        return False
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


__all__ = [
    "FROZEN_CONFIGURATION",
    "FrozenShadowUtilityReranker",
    "PROVISIONAL_COST_MODEL_VERSION",
    "RERANKER_VERSION",
    "SCHEMA_VERSION",
    "build_shadow_utility_receipt",
    "canonical_hash",
    "persist_immutable_receipt",
    "rerank_strategy_scores",
]


@dataclass(frozen=True, slots=True)
class FrozenShadowUtilityReranker:
    """Small immutable facade for callers that prefer an object contract."""

    version: str = RERANKER_VERSION
    configuration_hash_sha256: str = canonical_hash(FROZEN_CONFIGURATION)

    def rerank(self, rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
        return rerank_strategy_scores(rows)
