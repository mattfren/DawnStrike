"""Immutable weekly and prospective receipts for the nine v2 challengers.

The catalog already contains nine one-variable, research-only challenger
specifications.  This service gives them a common date protocol and receipt
shape.  It does not run the champion slate, replace a strategy, or interpret
missing outcomes as zero.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from collections import defaultdict
from collections.abc import Mapping, Sequence
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from intraday_scanner.alpha.fill_truth import has_authenticated_committed_fill_truth
from intraday_scanner.v2.strategies import build_challenger_catalog
from intraday_scanner.v2.strategy_identity import strategy_semantics_fingerprint

SCHEMA_VERSION = "dawnstrike.strategy_challenger_evaluation.v1"
EVALUATION_VERSION = "dawnstrike-weekly-purged-shadow-evaluation-20260829.v1"
CHALLENGER_COUNT = 9
MINIMUM_PAIR_SAMPLES = 20
MINIMUM_PAIR_SESSIONS = 5
FORBIDDEN_PROSPECTIVE_FIELDS = frozenset(
    {
        "outcome",
        "outcome_status",
        "after_cost_return_pct",
        "after_cost_return",
        "after_cost_utility_pct",
        "realized_net_return_pct",
        "realized_net_excess_return_pct",
        "realized_after_cost_return",
        "realized_after_cost_return_pct",
        "trade_return",
        "gross_pnl",
        "net_pnl",
        "gross_pnl_cents",
        "net_pnl_cents",
        "fees_cents",
        "slippage_cents",
        "fill_truth",
        "fill_truth_hash_sha256",
        "fill_truth_status",
        "fill_truth_bound",
        "entry_fill_price",
        "exit_fill_price",
        "entry_fill_at",
        "exit_fill_at",
        "entry_price",
        "exit_price",
        "entry_quote",
        "exit_quote",
        "entry_quote_mid_price",
        "exit_quote_mid_price",
        "observed_cost_bps",
        "execution_cost_bps",
        "trade_status",
        "position_status",
        "closed_at",
        "paper_truth",
        # Canonical aliases are explicit so nested caller payloads cannot hide
        # an outcome under a different spelling.
        "return",
        "returns",
        "return_pct",
        "returns_pct",
        "net_return",
        "net_return_pct",
        "net_after_cost_return",
        "net_after_cost_return_pct",
        "gross_return",
        "gross_return_pct",
        "pnl",
        "pnl_pct",
        "profit_loss",
        "profitandloss",
        "fill",
        "fills",
        "entry_fill",
        "exit_fill",
        "commission",
        "fee",
        "fees",
        "slippage",
        "execution_cost",
        "realized_pnl",
        "unrealized_pnl",
        "realized_return",
        "realized_return_pct",
        "unrealized_return",
        "unrealized_return_pct",
        "closed",
        "closed_date",
    }
)
ALLOWED_CHALLENGER_PARAMETER_DELTAS = frozenset(
    {"candidate_version", "research_only", "unsupported_data_policy", "challenger_gate_policy"}
)
MARKET_TIMEZONE = ZoneInfo("America/Chicago")
LOCAL_EVIDENCE_MANIFEST = "strategy_challenger_evidence_manifest.json"
LOCAL_EVIDENCE_MANIFEST_SCHEMA = "dawnstrike.strategy_challenger_evidence_manifest.v1"
LOCAL_RECEIPT_DIRECTORY = "strategy_challenger_evaluation"


class StrategyChallengerEvidenceError(ValueError):
    """Raised when retained challenger evidence is present but unsafe."""


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(
            value, sort_keys=True, separators=(",", ":"), allow_nan=False, default=str
        ).encode("utf-8")
    ).hexdigest()


def build_challenger_registry() -> tuple[dict[str, Any], ...]:
    """Return the existing nine challengers as immutable one-variable specs."""

    champions = {spec.strategy_id: spec for spec in _build_actual_catalog()}
    entries: list[dict[str, Any]] = []
    for spec in build_challenger_catalog():
        champion = champions.get(spec.strategy_id)
        if champion is None:
            raise ValueError(f"challenger has no actual champion spec: {spec.strategy_id}")
        if spec.version == champion.version:
            raise ValueError(f"challenger version must differ from champion: {spec.strategy_id}")
        if spec.parameters.get("candidate_version") != spec.version:
            raise ValueError(f"challenger version parameter is not bound: {spec.strategy_id}")
        changed_original = {
            name
            for name, value in champion.parameters.items()
            if spec.parameters.get(name) != value
        }
        if changed_original:
            raise ValueError(f"challenger changed champion parameters: {spec.strategy_id}")
        parameter_deltas = set(spec.parameters) - set(champion.parameters)
        if parameter_deltas != set(ALLOWED_CHALLENGER_PARAMETER_DELTAS):
            raise ValueError(
                f"challenger parameter delta is not the governed gate bundle: {spec.strategy_id}"
            )
        configuration = {
            "strategy_id": spec.strategy_id,
            "strategy_version": spec.version,
            "parameters": dict(spec.parameters),
            "semantics_fingerprint": strategy_semantics_fingerprint(spec),
            "controlled_change": "existing catalog challenger only; one variable",
            "gate_policy_bundle": spec.parameters.get("challenger_gate_policy"),
            "parameter_delta_keys": sorted(parameter_deltas),
        }
        entries.append(
            {
                "challenger_id": spec.strategy_id,
                "challenger_version": spec.version,
                "champion_strategy_id": spec.strategy_id,
                "champion_strategy_version": champion.version,
                "champion_semantics_fingerprint": strategy_semantics_fingerprint(champion),
                "challenger_semantics_fingerprint": strategy_semantics_fingerprint(spec),
                "configuration": configuration,
                "configuration_hash_sha256": canonical_hash(configuration),
                "one_variable": True,
                "one_variable_validation": {
                    "actual_champion_spec_version_bound": True,
                    "allowed_parameter_delta_keys": sorted(ALLOWED_CHALLENGER_PARAMETER_DELTAS),
                    "gate_policy_bundle_bound": True,
                },
                "research_only": True,
                "promotion_eligible": False,
                "automatic_promotion": False,
                "broker_execution_enabled": False,
            }
        )
    if len(entries) != CHALLENGER_COUNT:
        raise ValueError(f"expected {CHALLENGER_COUNT} catalog challengers")
    return tuple(entries)


def _build_actual_catalog() -> tuple[Any, ...]:
    """Load the catalog without treating a literal version as governance."""

    from intraday_scanner.v2.strategies import build_strategy_catalog

    return build_strategy_catalog()


def _market_date(row: Mapping[str, Any]) -> date | None:
    raw = str(row.get("market_date") or "")
    try:
        observed = date.fromisoformat(raw)
    except ValueError:
        return None
    return observed if observed.isoformat() == raw else None


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


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


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None and parsed.utcoffset() is not None else None


def _decision_market_date(row: Mapping[str, Any]) -> date | None:
    decision_at = _timestamp(row.get("decision_at"))
    observed = _market_date(row)
    if decision_at is None or observed is None:
        return None
    return observed if decision_at.astimezone(MARKET_TIMEZONE).date() == observed else None


def _window_contains_market_date(observed: date, window: Mapping[str, Any]) -> bool:
    try:
        if window.get("date") is not None:
            raw = str(window["date"])
            return raw == date.fromisoformat(raw).isoformat() == observed.isoformat()
        start_raw = str(window["start"])
        end_raw = str(window["end"])
        start = date.fromisoformat(start_raw)
        end = date.fromisoformat(end_raw)
    except (KeyError, TypeError, ValueError):
        return False
    return (
        start_raw == start.isoformat()
        and end_raw == end.isoformat()
        and start <= observed <= end
    )


def _normalized_field_name(value: object) -> str:
    text = str(value or "")
    text = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", text)
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def _contains_forbidden_prospective_field(value: object) -> bool:
    if isinstance(value, Mapping):
        for key, nested in value.items():
            if _normalized_field_name(key) in FORBIDDEN_PROSPECTIVE_FIELDS:
                return True
            if _contains_forbidden_prospective_field(nested):
                return True
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return any(_contains_forbidden_prospective_field(item) for item in value)
    return False


def _candidate_identity(row: Mapping[str, Any]) -> str | None:
    candidate_hash = row.get("candidate_identity_hash_sha256")
    if _is_hash(candidate_hash):
        return str(candidate_hash)
    ticker = str(row.get("ticker") or row.get("symbol") or "").strip()
    direction = str(row.get("direction") or "").strip().lower()
    return f"{ticker}|{direction}" if ticker and direction else None


def build_weekly_purged_splits(
    rows: Sequence[Mapping[str, Any]], *, minimum_training_weeks: int = 4, embargo_weeks: int = 1
) -> list[dict[str, Any]]:
    """Create expanding weekly folds with a complete week embargo."""

    if minimum_training_weeks < 1 or embargo_weeks < 0:
        raise ValueError("minimum_training_weeks must be positive and embargo_weeks non-negative")
    weeks = sorted(
        {
            (observed - timedelta(days=observed.weekday())).isoformat()
            for row in rows
            if (observed := _decision_market_date(row)) is not None
        }
    )
    folds: list[dict[str, Any]] = []
    for index in range(minimum_training_weeks + embargo_weeks, len(weeks)):
        training = weeks[: index - embargo_weeks]
        embargoed = weeks[index - embargo_weeks : index]
        test = [weeks[index]]
        folds.append(
            {
                "fold_id": f"weekly-purged-{weeks[index]}",
                "training_weeks": training,
                "embargoed_weeks": embargoed,
                "test_weeks": test,
                "no_lookahead": bool(training) and max(training) < min(test),
                "purged": bool(set(training).isdisjoint(embargoed))
                and bool(set(embargoed).isdisjoint(test)),
            }
        )
    return folds


def _row_week(row: Mapping[str, Any]) -> str | None:
    observed = _decision_market_date(row)
    return (observed - timedelta(days=observed.weekday())).isoformat() if observed else None


def _validated_truth_rows(
    rows: Sequence[Mapping[str, Any]],
    *,
    challenger_id: str,
    challenger: Mapping[str, Any],
    source_hash: str,
    code_sha: str,
    window_hash: str,
    window: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Accept only canonical closed-paper rows and quarantine identity issues."""

    accepted: dict[str, dict[str, Any]] = {}
    rejected: dict[str, int] = {}
    conflicted: set[str] = set()
    for source in rows:
        row = dict(source)
        identity = str(row.get("record_id") or "").strip()
        truth = row.get("paper_truth")
        truth_mapping = truth if isinstance(truth, Mapping) else None
        return_payload = (
            truth_mapping.get("return_payload") if truth_mapping is not None else None
        )
        observed_market_date = _decision_market_date(row)
        required = (
            identity,
            str(row.get("decision_id") or "").strip(),
            str(row.get("pair_id") or "").strip(),
            str(row.get("account_id") or "").strip(),
            observed_market_date is not None,
            _window_contains_market_date(observed_market_date, window)
            if observed_market_date is not None
            else False,
            row.get("series_role") in {"champion", "challenger"},
            row.get("record_type") in {"closed_paper_position", "closed_trade"},
            _number(row.get("after_cost_return_pct")) is not None,
            truth_mapping is not None,
            truth_mapping is not None and truth_mapping.get("status") == "closed",
            truth_mapping is not None and _is_hash(truth_mapping.get("fill_truth_hash_sha256")),
            truth_mapping is not None and _is_hash(truth_mapping.get("source_lineage_hash_sha256")),
            isinstance(return_payload, Mapping)
            and truth_mapping is not None
            and _is_hash(truth_mapping.get("return_payload_hash_sha256")),
            isinstance(return_payload, Mapping)
            and truth_mapping is not None
            and truth_mapping.get("return_payload_hash_sha256") == canonical_hash(return_payload),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("record_id") or "") == identity,
            isinstance(return_payload, Mapping)
            and str(return_payload.get("decision_id") or "") == str(row.get("decision_id") or ""),
            "source_decision_id" not in row
            or (
                isinstance(return_payload, Mapping)
                and str(return_payload.get("source_decision_id") or "")
                == str(row.get("source_decision_id") or "")
            ),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("pair_id") or "") == str(row.get("pair_id") or ""),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("market_date") or "") == str(row.get("market_date") or ""),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("account_id") or "") == str(row.get("account_id") or ""),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("series_role") or "") == str(row.get("series_role") or ""),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("champion_strategy_id") or "")
            == str(challenger["champion_strategy_id"]),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("challenger_strategy_id") or "")
            == str(challenger["challenger_id"]),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("champion_strategy_version") or "")
            == str(challenger["champion_strategy_version"]),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("challenger_strategy_version") or "")
            == str(challenger["challenger_version"]),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("challenger_configuration_hash_sha256") or "")
            == str(challenger["configuration_hash_sha256"]),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("source_manifest_hash_sha256") or "") == source_hash,
            isinstance(return_payload, Mapping)
            and str(return_payload.get("code_sha") or "") == code_sha,
            isinstance(return_payload, Mapping)
            and str(return_payload.get("window_hash_sha256") or "") == window_hash,
            isinstance(return_payload, Mapping)
            and _number(return_payload.get("allocation_weight"))
            == _number(row.get("allocation_weight")),
            isinstance(return_payload, Mapping)
            and _number(return_payload.get("account_weight"))
            == _number(row.get("account_weight")),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("fill_truth_hash_sha256") or "")
            == str(
                truth_mapping.get("fill_truth_hash_sha256")
                if truth_mapping is not None
                else ""
            ),
            isinstance(return_payload, Mapping)
            and str(return_payload.get("source_lineage_hash_sha256") or "")
            == str(
                truth_mapping.get("source_lineage_hash_sha256")
                if truth_mapping is not None
                else ""
            ),
            not ("ticker" in row or "symbol" in row)
            or (
                isinstance(return_payload, Mapping)
                and str(return_payload.get("ticker") or return_payload.get("symbol") or "")
                == str(row.get("ticker") or row.get("symbol") or "")
            ),
            "direction" not in row
            or (
                isinstance(return_payload, Mapping)
                and str(return_payload.get("direction") or "") == str(row.get("direction") or "")
            ),
            "candidate_identity_hash_sha256" not in row
            or (
                isinstance(return_payload, Mapping)
                and str(return_payload.get("candidate_identity_hash_sha256") or "")
                == str(row.get("candidate_identity_hash_sha256") or "")
            ),
            _candidate_identity(row) is not None,
            isinstance(return_payload, Mapping)
            and (
                (
                    _is_hash(row.get("candidate_identity_hash_sha256"))
                    and str(return_payload.get("candidate_identity_hash_sha256") or "")
                    == str(row.get("candidate_identity_hash_sha256"))
                )
                or (
                    str(row.get("ticker") or row.get("symbol") or "").strip()
                    and str(row.get("direction") or "").strip().lower()
                    and str(return_payload.get("ticker") or return_payload.get("symbol") or "")
                    == str(row.get("ticker") or row.get("symbol") or "")
                    and str(return_payload.get("direction") or "").strip().lower()
                    == str(row.get("direction") or "").strip().lower()
                )
            ),
            isinstance(return_payload, Mapping)
            and _number(return_payload.get("after_cost_return_pct"))
            == _number(row.get("after_cost_return_pct")),
            has_authenticated_committed_fill_truth(row),
            str(row.get("champion_strategy_version") or "")
            == str(challenger["champion_strategy_version"]),
            str(row.get("champion_strategy_id") or "")
            == str(challenger["champion_strategy_id"]),
            str(row.get("challenger_strategy_id") or "")
            == str(challenger_id),
            str(row.get("challenger_id") or "")
            == str(challenger_id),
            str(row.get("challenger_strategy_version") or row.get("strategy_version") or "")
            == str(challenger["challenger_version"]),
            str(row.get("challenger_configuration_hash_sha256") or "")
            == str(challenger["configuration_hash_sha256"]),
            str(row.get("source_manifest_hash_sha256") or "") == source_hash,
            str(row.get("code_sha") or "") == code_sha,
            str(row.get("window_hash_sha256") or "") == window_hash,
            _number(row.get("allocation_weight")) is not None
            and float(row["allocation_weight"]) > 0,
            _number(row.get("account_weight")) is not None
            and float(row["account_weight"]) > 0,
        )
        if not all(required):
            rejected["missing_or_unauthenticated_closed_paper_truth"] = rejected.get(
                "missing_or_unauthenticated_closed_paper_truth", 0
            ) + 1
            continue
        fingerprint = canonical_hash(row)
        existing = accepted.get(identity)
        if existing is not None:
            if canonical_hash(existing) != fingerprint:
                conflicted.add(identity)
                rejected["conflicting_record_identity"] = rejected.get(
                    "conflicting_record_identity", 0
                ) + 1
            else:
                rejected["duplicate_record_identity"] = rejected.get(
                    "duplicate_record_identity", 0
                ) + 1
            continue
        accepted[identity] = row
    for identity in conflicted:
        accepted.pop(identity, None)
    return list(accepted.values()), rejected


def _fold_metrics(
    rows: Sequence[Mapping[str, Any]],
    *,
    challenger_id: str,
    challenger: Mapping[str, Any],
    source_hash: str,
    code_sha: str,
    window_hash: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    valid, rejected = _validated_truth_rows(
        rows,
        challenger_id=challenger_id,
        challenger=challenger,
        source_hash=source_hash,
        code_sha=code_sha,
        window_hash=window_hash,
        window=window,
    )
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in valid:
        if row.get("series_role") == "challenger" and str(
            row.get("challenger_id") or row.get("strategy_id") or ""
        ) != challenger_id:
            continue
        key = (str(row.get("market_date")), str(row.get("account_id")))
        role = str(row["series_role"])
        grouped[(*key, role)].append(row)
    paired_groups: set[tuple[str, str]] = set()
    by_account_day: dict[tuple[str, str], dict[str, list[dict[str, Any]]]] = defaultdict(dict)
    for (market_date, account_id, role), role_rows in grouped.items():
        by_account_day[(market_date, account_id)][role] = role_rows
    for key, roles in by_account_day.items():
        if set(roles) != {"champion", "challenger"}:
            rejected["unmatched_account_day_role"] = rejected.get(
                "unmatched_account_day_role", 0
            ) + 1
            continue
        duplicate_pair_identity = any(
            len({str(row["pair_id"]) for row in role_rows}) != len(role_rows)
            or len({str(row["decision_id"]) for row in role_rows}) != len(role_rows)
            for role_rows in roles.values()
        )
        if duplicate_pair_identity:
            rejected["duplicate_paired_decision_identity"] = rejected.get(
                "duplicate_paired_decision_identity", 0
            ) + 1
            continue
        allocation_sets = {
            role: {
                str(row["pair_id"]): float(row["allocation_weight"])
                for row in role_rows
            }
            for role, role_rows in roles.items()
        }
        decision_sets = {
            role: {
                str(row["pair_id"]): str(
                    row.get("source_decision_id") or row["decision_id"]
                )
                for row in role_rows
            }
            for role, role_rows in roles.items()
        }
        candidate_sets = {
            role: {
                str(row["pair_id"]): _candidate_identity(row)
                for row in role_rows
            }
            for role, role_rows in roles.items()
        }
        if (
            allocation_sets["champion"] != allocation_sets["challenger"]
            or decision_sets["champion"] != decision_sets["challenger"]
            or candidate_sets["champion"] != candidate_sets["challenger"]
        ):
            rejected["mismatched_paired_decision_or_allocation_set"] = rejected.get(
                "mismatched_paired_decision_or_allocation_set", 0
            ) + 1
            continue
        paired_groups.add(key)
    pairs: dict[tuple[str, str], dict[str, dict[str, Any]]] = defaultdict(dict)
    for (market_date, account_id, role), role_rows in grouped.items():
        if (market_date, account_id) not in paired_groups:
            continue
        role_rows = sorted(role_rows, key=lambda row: str(row["record_id"]))
        total_weight = sum(float(row["allocation_weight"]) for row in role_rows)
        if not math.isfinite(total_weight) or total_weight <= 0 or total_weight > 1.0 + 1e-9:
            rejected["invalid_complete_allocation"] = rejected.get(
                "invalid_complete_allocation", 0
            ) + 1
            continue
        account_weights = {float(row["account_weight"]) for row in role_rows}
        if len(account_weights) != 1:
            rejected["conflicting_account_weight"] = rejected.get(
                "conflicting_account_weight", 0
            ) + 1
            continue
        aggregate = sum(
            float(row["after_cost_return_pct"]) * float(row["allocation_weight"])
            for row in role_rows
        )
        pairs[(market_date, account_id)][role] = {
            "after_cost_return_pct": aggregate,
            "account_weight": next(iter(account_weights)),
            "record_id": canonical_hash(sorted(row["record_id"] for row in role_rows)),
        }
    complete: list[dict[str, dict[str, Any]]] = []
    complete_keys: list[tuple[str, str]] = []
    for key in sorted(pairs):
        pair = pairs[key]
        if {"champion", "challenger"} <= set(pair):
            if not math.isclose(
                float(pair["champion"]["account_weight"]),
                float(pair["challenger"]["account_weight"]),
                rel_tol=0.0,
                abs_tol=1e-12,
            ):
                rejected["conflicting_paired_account_weight"] = rejected.get(
                    "conflicting_paired_account_weight", 0
                ) + 1
                continue
            complete.append(pair)
            complete_keys.append(key)
    # Account weights describe invested account capital, not a probability
    # distribution.  Keep uninvested cash in each day and quarantine a day
    # whose explicit account weights over-allocate it.
    day_weight: dict[str, float] = defaultdict(float)
    for key, pair in zip(complete_keys, complete, strict=True):
        day_weight[key[0]] += float(pair["champion"]["account_weight"])
    invalid_days = {
        market_date
        for market_date, total in day_weight.items()
        if not math.isfinite(total) or total > 1.0 + 1e-9
    }
    if invalid_days:
        rejected["overallocated_account_day"] = rejected.get(
            "overallocated_account_day", 0
        ) + len(invalid_days)
        filtered = [
            (key, pair)
            for key, pair in zip(complete_keys, complete, strict=True)
            if key[0] not in invalid_days
        ]
        complete_keys = [key for key, _ in filtered]
        complete = [pair for _, pair in filtered]
    if not complete:
        return {
            "status": "BLOCKED_NO_EXACT_COMMON_OOS_PAIRS",
            "sample_size": 0,
            "session_count": 0,
            "after_cost_expectancy_pct": None,
            "paired_excess_expectancy_pct": None,
            "rejected_counts": dict(sorted(rejected.items())),
        }
    champion_values_optional = [
        _number(pair["champion"]["after_cost_return_pct"]) for pair in complete
    ]
    challenger_values_optional = [
        _number(pair["challenger"]["after_cost_return_pct"]) for pair in complete
    ]
    if any(value is None for value in champion_values_optional + challenger_values_optional):
        raise AssertionError("validated truth row lost its canonical return")
    champion_values = [value for value in champion_values_optional if value is not None]
    challenger_values = [value for value in challenger_values_optional if value is not None]
    if len(champion_values) != len(complete) or len(challenger_values) != len(complete):
        raise AssertionError("validated truth row lost its canonical return")
    deltas = [
        challenger_return - champion_return
        for challenger_return, champion_return in zip(
            challenger_values, champion_values, strict=True
        )
    ]
    sessions: dict[str, list[tuple[float, float, float]]] = defaultdict(list)
    for key, champion_return, challenger_return, delta in zip(
        complete_keys,
        champion_values,
        challenger_values,
        deltas,
        strict=True,
    ):
        weight = float(pairs[key]["champion"]["account_weight"])
        sessions[key[0]].append(
            (
                champion_return * weight,
                challenger_return * weight,
                delta * weight,
            )
        )
    session_returns = {
        market_date: (
            sum(champion for champion, _, _ in values),
            sum(challenger for _, challenger, _ in values),
            sum(delta for _, _, delta in values),
        )
        for market_date, values in sessions.items()
    }
    session_count = len(session_returns)
    equal_weight_champion = sum(value[0] for value in session_returns.values()) / session_count
    equal_weight_challenger = sum(value[1] for value in session_returns.values()) / session_count
    equal_weight_delta = sum(value[2] for value in session_returns.values()) / session_count
    sample_ok = len(complete) >= MINIMUM_PAIR_SAMPLES
    session_ok = session_count >= MINIMUM_PAIR_SESSIONS
    enough = sample_ok and session_ok
    return {
        "status": "EVALUABLE_PAIRED_OOS" if enough else "BLOCKED_INSUFFICIENT_PAIRED_OOS",
        "sample_size": len(complete),
        "session_count": session_count,
        "minimum_sample_size_met": sample_ok,
        "minimum_session_count_met": session_ok,
        "minimum_sample_size": MINIMUM_PAIR_SAMPLES,
        "minimum_session_count": MINIMUM_PAIR_SESSIONS,
        "champion_after_cost_expectancy_pct": round(equal_weight_champion, 6)
        if enough
        else None,
        "challenger_after_cost_expectancy_pct": round(equal_weight_challenger, 6)
        if enough
        else None,
        "after_cost_expectancy_pct": round(equal_weight_challenger, 6)
        if enough
        else None,
        "paired_excess_expectancy_pct": round(equal_weight_delta, 6) if enough else None,
        "rejected_counts": dict(sorted(rejected.items())),
    }


def _rows_for_challenger(
    rows: Sequence[Mapping[str, Any]], challenger_id: str
) -> list[dict[str, Any]]:
    return [
        dict(row)
        for row in rows
        if str(row.get("challenger_id") or row.get("strategy_id") or "") == challenger_id
    ]


def build_weekly_purged_evaluation_receipt(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_manifest: Mapping[str, Any],
    code_sha: str,
    window: Mapping[str, Any],
    minimum_training_weeks: int = 4,
    embargo_weeks: int = 1,
) -> dict[str, Any]:
    """Build one immutable, common-protocol walk-forward receipt."""

    if not _is_code_identity(code_sha) or not source_manifest or not window:
        raise ValueError("code_sha, source_manifest, and window are required")
    source_rows = sorted((dict(row) for row in rows), key=canonical_hash)
    registry = build_challenger_registry()
    source_hash = canonical_hash(source_manifest)
    window_hash = canonical_hash(window)
    # Fold-driving dates come only from fully authenticated paired truth. A
    # caller cannot shift the expanding train/test boundary by adding a
    # malformed, unauthenticated, or otherwise ineligible future row.
    validated_cohort: dict[str, dict[str, Any]] = {}
    for entry in registry:
        valid_rows, _ = _validated_truth_rows(
            source_rows,
            challenger_id=entry["challenger_id"],
            challenger=entry,
            source_hash=source_hash,
            code_sha=code_sha,
            window_hash=window_hash,
            window=window,
        )
        for row in valid_rows:
            validated_cohort[canonical_hash(row)] = row
    cohort_rows = sorted(validated_cohort.values(), key=canonical_hash)
    folds = build_weekly_purged_splits(
        cohort_rows,
        minimum_training_weeks=minimum_training_weeks,
        embargo_weeks=embargo_weeks,
    )
    challenger_receipts: list[dict[str, Any]] = []
    for entry in registry:
        challenger_rows = [
            row
            for row in source_rows
            if row.get("series_role") == "champion"
            or str(row.get("challenger_id") or row.get("strategy_id") or "")
            == entry["challenger_id"]
        ]
        by_week = defaultdict(list)
        for row in challenger_rows:
            if (week := _row_week(row)) is not None:
                by_week[week].append(row)
        fold_receipts = []
        for fold in folds:
            fold_rows = [row for week in fold["test_weeks"] for row in by_week.get(week, [])]
            metrics = _fold_metrics(
                fold_rows,
                challenger_id=entry["challenger_id"],
                challenger=entry,
                source_hash=source_hash,
                code_sha=str(code_sha),
                window_hash=window_hash,
                window=window,
            )
            fold_receipts.append({**fold, **metrics})
        oos_weeks = {week for fold in folds for week in fold["test_weeks"]}
        oos_rows = [row for row in challenger_rows if _row_week(row) in oos_weeks]
        challenger_receipts.append(
            {
                **entry,
                "folds": fold_receipts,
                "overall_paired_metrics": _fold_metrics(
                    oos_rows,
                    challenger_id=entry["challenger_id"],
                    challenger=entry,
                    source_hash=source_hash,
                    code_sha=str(code_sha),
                    window_hash=window_hash,
                    window=window,
                ),
                "fold_count": len(fold_receipts),
                "evaluable_fold_count": sum(
                    fold["status"] == "EVALUABLE_PAIRED_OOS" for fold in fold_receipts
                ),
            }
        )
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "evaluation_kind": "weekly_purged_walk_forward",
        "evaluation_method": "paired_exact_common_oos_receipt_aggregation",
        "training_or_selection_performed": False,
        "interpretation": "receipt_protocol_only; no strategy is fitted or selected",
        "input_rows_hash_sha256": canonical_hash(source_rows),
        "source_manifest": dict(source_manifest),
        "source_manifest_hash_sha256": canonical_hash(source_manifest),
        "code_sha": str(code_sha),
        "window": dict(window),
        "window_hash_sha256": canonical_hash(window),
        "configuration": {
            "minimum_training_weeks": minimum_training_weeks,
            "embargo_weeks": embargo_weeks,
            "week_anchor": "monday",
            "missing_outcome_policy": "null_and_not_evaluable",
            "pairing": "exact_common_oos_market_date_and_account",
            "minimum_pair_samples": MINIMUM_PAIR_SAMPLES,
            "minimum_pair_sessions": MINIMUM_PAIR_SESSIONS,
        },
        "configuration_hash_sha256": canonical_hash(
            {
                "minimum_training_weeks": minimum_training_weeks,
                "embargo_weeks": embargo_weeks,
                "week_anchor": "monday",
                "missing_outcome_policy": "null_and_not_evaluable",
                "pairing": "exact_common_oos_market_date_and_account",
                "minimum_pair_samples": MINIMUM_PAIR_SAMPLES,
                "minimum_pair_sessions": MINIMUM_PAIR_SESSIONS,
            }
        ),
        "challengers": challenger_receipts,
        "research_only": True,
        "promotion_eligible": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "champion_slate_unchanged": True,
        "missing_outcomes_are_zero": False,
    }
    receipt["receipt_hash_sha256"] = canonical_hash(receipt)
    return receipt


def build_prospective_shadow_evaluation_receipt(
    observations: Sequence[Mapping[str, Any]],
    *,
    source_manifest: Mapping[str, Any],
    code_sha: str,
    window: Mapping[str, Any],
) -> dict[str, Any]:
    """Receipt prospective decisions without asserting unobserved outcomes."""

    if not _is_code_identity(code_sha) or not source_manifest or not window:
        raise ValueError("code_sha, source_manifest, and window are required")
    registry_ids = {entry["challenger_id"] for entry in build_challenger_registry()}
    registry = {entry["challenger_id"]: entry for entry in build_challenger_registry()}
    source_hash = canonical_hash(source_manifest)
    window_hash = canonical_hash(window)
    rows = sorted((dict(row) for row in observations), key=canonical_hash)
    valid: list[dict[str, Any]] = []
    rejected: dict[str, int] = {}
    identities: dict[str, str] = {}
    conflicted: set[str] = set()
    for row in rows:
        strategy_id = str(row.get("challenger_id") or row.get("strategy_id") or "")
        decision_id = str(row.get("decision_id") or "").strip()
        if strategy_id not in registry_ids:
            rejected["unknown_challenger"] = rejected.get("unknown_challenger", 0) + 1
            continue
        if not decision_id:
            rejected["missing_decision_identity"] = rejected.get("missing_decision_identity", 0) + 1
            continue
        if row.get("research_only") is not True or row.get("broker_execution_enabled") is not False:
            rejected["research_only_broker_contract"] = rejected.get(
                "research_only_broker_contract", 0
            ) + 1
            continue
        observed_market_date = _decision_market_date(row)
        if observed_market_date is None:
            rejected["missing_decision_time"] = rejected.get("missing_decision_time", 0) + 1
            continue
        if not _window_contains_market_date(observed_market_date, window):
            rejected["decision_outside_evaluation_window"] = rejected.get(
                "decision_outside_evaluation_window", 0
            ) + 1
            continue
        entry = registry[strategy_id]
        if not str(row.get("strategy_version") or "").strip() or not (
            _is_hash(row.get("configuration_hash_sha256"))
            and _is_hash(row.get("source_lineage_hash_sha256"))
            and _is_code_identity(row.get("code_sha"))
            and _is_hash(row.get("window_hash_sha256"))
        ):
            rejected["missing_decision_lineage"] = rejected.get("missing_decision_lineage", 0) + 1
            continue
        if _contains_forbidden_prospective_field(row):
            rejected["outcome_fields_in_prospective_decision"] = rejected.get(
                "outcome_fields_in_prospective_decision", 0
            ) + 1
            continue
        if not (
            str(row.get("strategy_version")) == str(entry["challenger_version"])
            and str(row.get("configuration_hash_sha256"))
            == str(entry["configuration_hash_sha256"])
            and str(row.get("source_lineage_hash_sha256")) == source_hash
            and str(row.get("source_manifest_hash_sha256")) == source_hash
            and str(row.get("code_sha")) == str(code_sha)
            and str(row.get("window_hash_sha256")) == window_hash
        ):
            rejected["decision_lineage_mismatch"] = rejected.get("decision_lineage_mismatch", 0) + 1
            continue
        allowed_keys = (
            "decision_id",
            "challenger_id",
            "strategy_id",
            "strategy_version",
            "market_date",
            "decision_at",
            "configuration_hash_sha256",
            "source_lineage_hash_sha256",
            "source_manifest_hash_sha256",
            "code_sha",
            "window_hash_sha256",
            "research_only",
            "broker_execution_enabled",
        )
        sanitized = {key: row[key] for key in allowed_keys if key in row}
        fingerprint = canonical_hash(sanitized)
        if decision_id in identities:
            if identities[decision_id] != fingerprint:
                conflicted.add(decision_id)
                rejected["conflicting_decision_identity"] = rejected.get(
                    "conflicting_decision_identity", 0
                ) + 1
            else:
                rejected["duplicate_decision_identity"] = rejected.get(
                    "duplicate_decision_identity", 0
                ) + 1
            continue
        identities[decision_id] = fingerprint
        valid.append(sanitized)
    if conflicted:
        valid = [row for row in valid if str(row.get("decision_id") or "") not in conflicted]
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "evaluation_kind": "prospective_shadow",
        "input_observations_hash_sha256": canonical_hash(rows),
        "source_manifest": dict(source_manifest),
        "source_manifest_hash_sha256": canonical_hash(source_manifest),
        "code_sha": str(code_sha),
        "window": dict(window),
        "window_hash_sha256": canonical_hash(window),
        "status": "PENDING_PROSPECTIVE_OUTCOMES",
        "observation_count": len(valid),
        "rejected_observation_counts": dict(sorted(rejected.items())),
        "outcomes": None,
        "observations": valid,
        "research_only": True,
        "promotion_eligible": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "champion_slate_unchanged": True,
        "missing_outcomes_are_zero": False,
    }
    receipt["receipt_hash_sha256"] = canonical_hash(receipt)
    return receipt


def persist_evaluation_receipt(path: str | Path, receipt: Mapping[str, Any]) -> bool:
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
            raise ValueError(f"immutable evaluation receipt changed: {target}")
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
                raise ValueError(f"immutable evaluation receipt changed: {target}") from None
            return True
        return False
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _safe_local_path(root: Path, raw: object, *, field: str) -> Path:
    relative = str(raw or "").strip()
    if not relative or Path(relative).is_absolute():
        raise StrategyChallengerEvidenceError(f"{field} must be a relative local path")
    candidate = (root / relative).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError as exc:
        raise StrategyChallengerEvidenceError(f"{field} escapes approved evidence root") from exc
    return candidate


def _read_local_json(path: Path, *, field: str) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        raise StrategyChallengerEvidenceError(f"{field} is unreadable or malformed") from exc


def _validate_local_manifest(
    root: Path,
    payload: object,
    *,
    market_date: str,
    code_sha: str,
) -> tuple[dict[str, Any], dict[str, Any], list[dict[str, Any]], list[dict[str, Any]]]:
    if payload is None:
        return {}, {}, [], []
    if not isinstance(payload, dict):
        raise StrategyChallengerEvidenceError("challenger evidence manifest must be an object")
    if payload.get("schema_version") != LOCAL_EVIDENCE_MANIFEST_SCHEMA:
        raise StrategyChallengerEvidenceError("challenger evidence manifest schema is unsupported")
    if str(payload.get("market_date") or "") != market_date:
        raise StrategyChallengerEvidenceError("challenger evidence manifest market date mismatch")
    if str(payload.get("code_sha") or "") != code_sha:
        raise StrategyChallengerEvidenceError("challenger evidence manifest code SHA mismatch")
    window = payload.get("window")
    if not isinstance(window, dict) or not window:
        raise StrategyChallengerEvidenceError(
            "challenger evidence manifest frozen window is missing"
        )
    declared_window_hash = str(payload.get("window_hash_sha256") or "")
    if not _is_hash(declared_window_hash) or declared_window_hash != canonical_hash(window):
        raise StrategyChallengerEvidenceError("challenger evidence manifest window hash mismatch")
    if not _window_contains_market_date(date.fromisoformat(market_date), window):
        # The manifest window is frozen and must include the receipt's date;
        # a stale or shifted window is an integrity failure, not insufficiency.
        raise StrategyChallengerEvidenceError(
            "challenger evidence frozen window does not contain market date"
        )
    calendar = payload.get("calendar")
    if not isinstance(calendar, dict) or str(calendar.get("market_date") or "") != market_date:
        raise StrategyChallengerEvidenceError(
            "challenger evidence manifest calendar binding is invalid"
        )
    input_hashes = payload.get("input_hashes")
    if not isinstance(input_hashes, dict) or not input_hashes:
        raise StrategyChallengerEvidenceError(
            "challenger evidence manifest input hashes are missing"
        )
    for name, digest in input_hashes.items():
        if not isinstance(name, str) or not _is_hash(digest):
            raise StrategyChallengerEvidenceError(
                "challenger evidence manifest input hash is invalid"
            )
    source_manifest = dict(payload.get("source_manifest") or {})
    if not source_manifest:
        raise StrategyChallengerEvidenceError("challenger evidence source manifest is missing")
    source_hash = str(payload.get("source_manifest_hash_sha256") or "")
    if not _is_hash(source_hash) or source_hash != canonical_hash(source_manifest):
        raise StrategyChallengerEvidenceError("challenger evidence source manifest hash mismatch")

    def rows_for(field: str) -> list[dict[str, Any]]:
        raw_path = payload.get(field)
        if not raw_path:
            return []
        path = _safe_local_path(root, raw_path, field=field)
        if not path.is_file():
            return []
        observed_hash = _sha256_file(path)
        declared = str(input_hashes.get(str(raw_path)) or "")
        if not declared or observed_hash != declared:
            raise StrategyChallengerEvidenceError(f"{field} content hash mismatch")
        loaded = _read_local_json(path, field=field)
        if not isinstance(loaded, list) or any(not isinstance(row, dict) for row in loaded):
            raise StrategyChallengerEvidenceError(f"{field} must contain an array of objects")
        return [dict(row) for row in loaded]

    decisions = rows_for("prospective_decisions_path")
    outcomes = rows_for("closed_fill_truth_path")
    return source_manifest, window, decisions, outcomes


def _assert_unique_evidence_identity(
    rows: Sequence[Mapping[str, Any]], *, identity_fields: Sequence[str], label: str
) -> None:
    seen: dict[str, str] = {}
    for row in rows:
        identity = next(
            (
                str(row.get(field) or "").strip()
                for field in identity_fields
                if row.get(field)
            ),
            "",
        )
        if not identity:
            raise StrategyChallengerEvidenceError(f"{label} identity is missing")
        fingerprint = canonical_hash(row)
        if identity in seen:
            raise StrategyChallengerEvidenceError(
                f"conflicting or duplicate {label} identity: {identity}"
            )
        seen[identity] = fingerprint


def _validate_prospective_decisions(
    rows: Sequence[Mapping[str, Any]],
    *,
    source_hash: str,
    code_sha: str,
    window_hash: str,
    window: Mapping[str, Any],
) -> None:
    registry = {entry["challenger_id"]: entry for entry in build_challenger_registry()}
    _assert_unique_evidence_identity(
        rows, identity_fields=("decision_id",), label="prospective decision"
    )
    for row in rows:
        strategy_id = str(row.get("challenger_id") or row.get("strategy_id") or "")
        entry = registry.get(strategy_id)
        if entry is None:
            raise StrategyChallengerEvidenceError(
                "prospective decision references unknown challenger"
            )
        if row.get("research_only") is not True or row.get("broker_execution_enabled") is not False:
            raise StrategyChallengerEvidenceError(
                "prospective decision violates research-only contract"
            )
        observed = _decision_market_date(row)
        if observed is None or not _window_contains_market_date(observed, window):
            raise StrategyChallengerEvidenceError(
                "prospective decision date/window binding is invalid"
            )
        if _contains_forbidden_prospective_field(row):
            raise StrategyChallengerEvidenceError(
                "prospective decision contains retrospective or counterfactual outcome fields"
            )
        if (
            str(row.get("code_sha") or "") != code_sha
            or str(row.get("source_lineage_hash_sha256") or "") != source_hash
            or str(row.get("source_manifest_hash_sha256") or "") != source_hash
            or str(row.get("window_hash_sha256") or "") != window_hash
            or str(row.get("strategy_version") or "") != str(entry["challenger_version"])
            or str(row.get("configuration_hash_sha256") or "")
            != str(entry["configuration_hash_sha256"])
        ):
            raise StrategyChallengerEvidenceError(
                "prospective decision lineage hash mismatch"
            )


def _not_evaluable_receipt(
    *,
    market_date: str,
    code_sha: str,
    finalize_lineage: Mapping[str, Any],
    source_manifest: Mapping[str, Any] | None,
    window: Mapping[str, Any] | None,
    reasons: Sequence[str],
    decisions: Sequence[Mapping[str, Any]] = (),
    outcomes: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    source = dict(source_manifest) if source_manifest else None
    frozen_window = dict(window) if window else None
    reason_set = tuple(sorted(dict.fromkeys(str(item) for item in reasons)))
    receipt: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "evaluation_kind": "weekly_purged_shadow",
        "status": reason_set[0] if reason_set else "NOT_EVALUABLE",
        "market_date": market_date,
        "code_sha": code_sha,
        "source_manifest": source,
        "source_manifest_hash_sha256": canonical_hash(source) if source is not None else None,
        "window": frozen_window,
        "window_hash_sha256": canonical_hash(frozen_window) if frozen_window is not None else None,
        "calendar": (source or {}).get("calendar") if source else None,
        "data_hashes": (source or {}).get("data_hashes") if source else None,
        "configuration": (source or {}).get("configuration") if source else None,
        "configuration_hash_sha256": (
            canonical_hash((source or {}).get("configuration"))
            if isinstance((source or {}).get("configuration"), Mapping)
            else None
        ),
        "input_hashes": (source or {}).get("input_hashes") if source else None,
        "input_observations_hash_sha256": canonical_hash([dict(row) for row in decisions]),
        "input_outcomes_hash_sha256": canonical_hash([dict(row) for row in outcomes]),
        "daily_finalize_lineage": dict(finalize_lineage),
        "daily_finalize_lineage_hash_sha256": canonical_hash(finalize_lineage),
        "non_evaluable_reasons": list(reason_set),
        "metrics": None,
        "challengers": [
            {
                "challenger_id": entry["challenger_id"],
                "challenger_version": entry["challenger_version"],
                "configuration_hash_sha256": entry["configuration_hash_sha256"],
                "metrics": None,
            }
            for entry in build_challenger_registry()
        ],
        "observation_count": len(decisions),
        "authenticated_closed_fill_truth_count": len(outcomes),
        "research_only": True,
        "promotion_eligible": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "champion_slate_unchanged": True,
        "missing_outcomes_are_zero": False,
        "missing_fill_truth_producer": (
            "No governed CommitBridge adapter currently authenticates point-in-time closed "
            "FillTruth; JSON self-hashes and PaperOps replay are insufficient."
        ),
    }
    receipt["receipt_hash_sha256"] = canonical_hash(receipt)
    return receipt


def run_strategy_challenger_weekly_adapter(
    *,
    db_path: str | Path,
    state_root: str | Path,
    market_date: str,
    code_sha: str,
    out_root: str | Path | None = None,
) -> dict[str, Any]:
    """Produce one immutable weekly challenger receipt from approved local evidence.

    The current repository intentionally has no authenticated FillTruth producer.  The
    adapter therefore completes as explicitly non-evaluable when the fixed evidence
    manifest or CommitBridge evidence is absent, while failing nonzero for tampering.
    """

    if not _is_code_identity(code_sha):
        raise StrategyChallengerEvidenceError("exact release code SHA is required")
    try:
        requested_date = date.fromisoformat(market_date)
    except ValueError as exc:
        raise StrategyChallengerEvidenceError("market_date must be an ISO date") from exc
    if requested_date.isoformat() != market_date:
        raise StrategyChallengerEvidenceError("market_date must use canonical ISO form")

    from scripts.verify_daily_finalize_receipt import verify as verify_finalize

    finalize = verify_finalize(db_path, market_date, code_sha)
    if finalize.get("status") != "READY" or finalize.get("ready") is not True:
        raise StrategyChallengerEvidenceError(
            "weekly challenger evaluation requires a completed same-SHA Daily Finalize gate"
        )
    finalize_lineage = {
        "run_id": finalize.get("run_id"),
        "market_date": finalize.get("market_date"),
        "release_sha": finalize.get("release_sha"),
        "status": finalize.get("status"),
        "publication_identity_ready": finalize.get("publication_identity_ready"),
    }
    if (
        finalize_lineage["release_sha"] != code_sha
        or finalize_lineage["market_date"] != market_date
    ):
        raise StrategyChallengerEvidenceError(
            "Daily Finalize lineage is not bound to requested date/SHA"
        )

    root = Path(state_root).resolve()
    manifest_path = root / LOCAL_EVIDENCE_MANIFEST
    payload = _read_local_json(manifest_path, field="challenger evidence manifest")
    source_manifest, window, decisions, outcomes = _validate_local_manifest(
        root,
        payload,
        market_date=market_date,
        code_sha=code_sha,
    )
    reasons: list[str] = []
    if payload is None:
        reasons.extend(
            (
                "NOT_EVALUABLE_SOURCE_MANIFEST_MISSING",
                "NOT_EVALUABLE_EVIDENCE_MISSING",
                "NOT_EVALUABLE_CALENDAR_MISSING",
                "NOT_EVALUABLE_HOLDOUT_MINIMA_MISSING",
                "NOT_EVALUABLE_AUTHENTICATED_FILL_TRUTH_MISSING",
            )
        )
    else:
        source_hash = canonical_hash(source_manifest)
        window_hash = canonical_hash(window)
        _validate_prospective_decisions(
            decisions,
            source_hash=source_hash,
            code_sha=code_sha,
            window_hash=window_hash,
            window=window,
        )
        if outcomes:
            _assert_unique_evidence_identity(
                outcomes,
                identity_fields=("record_id", "outcome_id", "decision_id"),
                label="closed FillTruth outcome",
            )
        if not decisions:
            reasons.append("NOT_EVALUABLE_PROSPECTIVE_DECISIONS_MISSING")
        if not outcomes:
            reasons.append("NOT_EVALUABLE_AUTHENTICATED_FILL_TRUTH_MISSING")
        calendar = source_manifest.get("calendar")
        if not isinstance(calendar, dict) or calendar.get("is_session") is not True:
            reasons.append("NOT_EVALUABLE_CALENDAR_MISSING")
        holdout = source_manifest.get("holdout")
        if not isinstance(holdout, dict) or not holdout.get("market_dates"):
            reasons.append("NOT_EVALUABLE_HOLDOUT_MINIMA_MISSING")
        for row in decisions:
            if _contains_forbidden_prospective_field(row):
                raise StrategyChallengerEvidenceError(
                    "prospective decision contains retrospective or counterfactual outcome fields"
                )
        # This boundary remains false until a private CommitBridge adapter is supplied.
        if outcomes and not any(has_authenticated_committed_fill_truth(row) for row in outcomes):
            reasons.append("NOT_EVALUABLE_AUTHENTICATED_FILL_TRUTH_MISSING")

    authenticated = bool(outcomes) and all(
        has_authenticated_committed_fill_truth(row) for row in outcomes
    )
    if not reasons and decisions and authenticated:
        # This is the only eligible branch.  It delegates fold construction,
        # exact-common pairing, and missing-value semantics to the existing
        # evaluator rather than creating a second scoring implementation.
        evaluated = run_strategy_challenger_evaluation(
            decisions,
            outcome_rows=outcomes,
            source_manifest=source_manifest,
            code_sha=code_sha,
            window=window,
        )
        weekly = evaluated["weekly_purged_walk_forward"]
        challenger_metrics = {
            str(item["challenger_id"]): item.get("overall_paired_metrics")
            for item in weekly.get("challengers", [])
            if isinstance(item, dict) and item.get("challenger_id")
        }
        all_gates_met = len(challenger_metrics) == CHALLENGER_COUNT and all(
            isinstance(metrics, dict)
            and metrics.get("status") == "EVALUABLE_PAIRED_OOS"
            for metrics in challenger_metrics.values()
        )
        if not all_gates_met:
            reasons.extend(
                (
                    "NOT_EVALUABLE_SAMPLE_MINIMUM_MISSING",
                    "NOT_EVALUABLE_SESSION_MINIMUM_MISSING",
                )
            )
        receipt = dict(evaluated)
        receipt.update(
            {
                "status": (
                    "EVALUABLE_WEEKLY_PURGED_SHADOW"
                    if all_gates_met
                    else "NOT_EVALUABLE_SAMPLE_OR_SESSION_MINIMUM_MISSING"
                ),
                "market_date": market_date,
                "source_manifest": dict(source_manifest),
                "calendar": source_manifest.get("calendar"),
                "daily_finalize_lineage": dict(finalize_lineage),
                "daily_finalize_lineage_hash_sha256": canonical_hash(finalize_lineage),
                "non_evaluable_reasons": sorted(dict.fromkeys(reasons)),
                "metrics": (
                    {"challengers": challenger_metrics}
                    if all_gates_met
                    else None
                ),
                "missing_fill_truth_producer": None,
            }
        )
    else:
        receipt = _not_evaluable_receipt(
            market_date=market_date,
            code_sha=code_sha,
            finalize_lineage=finalize_lineage,
            source_manifest=source_manifest or None,
            window=window or None,
            reasons=reasons or ("NOT_EVALUABLE_AUTHENTICATED_FILL_TRUTH_MISSING",),
            decisions=decisions,
            outcomes=outcomes,
        )
    receipt["evidence_manifest_path"] = str(manifest_path) if manifest_path.is_file() else None
    receipt["evidence_manifest_hash_sha256"] = (
        _sha256_file(manifest_path) if manifest_path.is_file() else None
    )
    if isinstance(payload, dict):
        receipt["input_hashes"] = dict(payload.get("input_hashes") or {})
    receipt["receipt_hash_sha256"] = canonical_hash(
        {key: value for key, value in receipt.items() if key != "receipt_hash_sha256"}
    )
    destination = Path(out_root) if out_root is not None else root / "outputs"
    target = destination / LOCAL_RECEIPT_DIRECTORY / f"{market_date}.json"
    inserted = persist_evaluation_receipt(target, receipt)
    return {**receipt, "receipt_path": str(target), "receipt_inserted": inserted}


# Stable aliases make the two evidence classes obvious at call sites while
# retaining one implementation and one receipt schema.
build_weekly_purged_walk_forward_receipt = build_weekly_purged_evaluation_receipt
build_prospective_shadow_receipt = build_prospective_shadow_evaluation_receipt


def run_strategy_challenger_evaluation(
    prospective_decisions: Sequence[Mapping[str, Any]],
    *,
    outcome_rows: Sequence[Mapping[str, Any]] = (),
    source_manifest: Mapping[str, Any],
    code_sha: str,
    window: Mapping[str, Any],
    out_path: str | Path | None = None,
) -> dict[str, Any]:
    """Build and optionally persist paired weekly/prospective receipts."""

    weekly = build_weekly_purged_evaluation_receipt(
        outcome_rows,
        source_manifest=source_manifest,
        code_sha=code_sha,
        window=window,
    )
    prospective = build_prospective_shadow_evaluation_receipt(
        prospective_decisions,
        source_manifest=source_manifest,
        code_sha=code_sha,
        window=window,
    )
    combined: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "evaluation_version": EVALUATION_VERSION,
        "weekly_purged_walk_forward": weekly,
        "prospective_shadow": prospective,
        "research_only": True,
        "promotion_eligible": False,
        "automatic_promotion": False,
        "broker_execution_enabled": False,
        "champion_slate_unchanged": True,
        "missing_outcomes_are_zero": False,
    }
    combined["prospective_decisions_hash_sha256"] = canonical_hash(
        sorted((dict(row) for row in prospective_decisions), key=canonical_hash)
    )
    combined["outcome_rows_hash_sha256"] = canonical_hash(
        sorted((dict(row) for row in outcome_rows), key=canonical_hash)
    )
    combined["source_manifest_hash_sha256"] = canonical_hash(source_manifest)
    combined["code_sha"] = str(code_sha)
    combined["window_hash_sha256"] = canonical_hash(window)
    combined["configuration_hash_sha256"] = canonical_hash(
        {"weekly": weekly["configuration_hash_sha256"], "prospective": "decision_contract_v1"}
    )
    combined["receipt_hash_sha256"] = canonical_hash(combined)
    if out_path is not None:
        persist_evaluation_receipt(out_path, combined)
    return combined


__all__ = [
    "CHALLENGER_COUNT",
    "EVALUATION_VERSION",
    "SCHEMA_VERSION",
    "LOCAL_EVIDENCE_MANIFEST",
    "LOCAL_EVIDENCE_MANIFEST_SCHEMA",
    "StrategyChallengerEvidenceError",
    "build_challenger_registry",
    "build_prospective_shadow_evaluation_receipt",
    "build_prospective_shadow_receipt",
    "build_weekly_purged_evaluation_receipt",
    "build_weekly_purged_walk_forward_receipt",
    "build_weekly_purged_splits",
    "canonical_hash",
    "persist_evaluation_receipt",
    "run_strategy_challenger_weekly_adapter",
    "run_strategy_challenger_evaluation",
]
