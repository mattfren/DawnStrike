"""Fail-closed validation for current AlphaOps return truth.

This module deliberately has no persistence or service dependencies.  It accepts
only the current, fully bound path/cost/benchmark/reconciliation contract and
provides an audit classification for current non-return path receipts.  Legacy
rows remain readable by their owners, but are never promoted here.
"""

from __future__ import annotations

import copy
import hashlib
import hmac
import json
import math
import re
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any

from intraday_scanner.alpha.episode_identity import build_episode_identity
from intraday_scanner.alpha.path_replay import (
    ELIGIBILITY_POLICY_VERSION,
    ENTRY_MODE_ALREADY_ENTERED,
    ENTRY_RECEIPT_ID_PREFIX,
    ENTRY_RECEIPT_SCHEMA_VERSION,
    PathTruthStatus,
    canonical_path_contract_valid,
    canonical_path_return_eligible,
)
from intraday_scanner.alpha.v5_policy import (
    ALPHAOPS_V5_ACCOUNT_ID,
    ALPHAOPS_V5_COST_MODEL_VERSION,
    ALPHAOPS_V5_POLICY_VERSION,
    ALPHAOPS_V5_STRATEGY_ID,
    ALPHAOPS_V5_STRATEGY_VERSION,
    DEFAULT_V5_POLICY,
    evaluate_v5_official_paper,
)

RETURN_TRUTH_SCHEMA_VERSION = "dawnstrike.alphaops.return_truth.v2"
COST_TRUTH_SCHEMA_VERSION = "dawnstrike.alphaops.cost_truth.v2"
RECONCILIATION_SCHEMA_VERSION = "dawnstrike.alphaops.reconciliation.v2"

CURRENT_RETURN_TRUTH = "CURRENT_RETURN_TRUTH"
CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED = "CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED"
CURRENT_CENSORED_PATH = "CURRENT_CENSORED_PATH"
LEGACY_OR_INCOMPLETE = "LEGACY_OR_INCOMPLETE"
TERMINAL_MISSING = "TERMINAL_MISSING"
PAPER_ENTER_INTENT_RECEIPT_SCHEMA_VERSION = (
    "dawnstrike.alphaops.paper_enter_intent.v2"
)
PAPER_ENTER_INTENT_RECEIPT_ID_PREFIX = "paper-enter-intent-v2-"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_PAPER_ENTER_INTENT_BODY_KEYS = frozenset(
    {
        "schema_version",
        "intent_id",
        "selection_id",
        "episode_id",
        "matched_strategy_ids",
        "primary_strategy_id",
        "episode_dedup_counts",
        "scan_id",
        "signal_id",
        "ticker",
        "market_date",
        "mode",
        "lifecycle_state",
        "action",
        "decision_time",
        "decision_price",
        "trigger_price",
        "stop_price",
        "target_price",
        "quantity",
        "notional",
        "risk_amount",
        "source_observation_id",
        "source_bar_hash_sha256",
        "source_observed_at",
        "source_bar_completed_at",
        "source_observation_receipt",
        "account_id",
        "execution_policy_version",
        "cost_model_version",
        "decision_fingerprint",
        "decision_trace",
        "official_paper_eligible",
        "raw_intent_record",
    }
)
_PAPER_ENTER_INTENT_RECEIPT_KEYS = frozenset(
    {
        *_PAPER_ENTER_INTENT_BODY_KEYS,
        "receipt_id",
        "receipt_hash_sha256",
    }
)
_COST_COMPONENT_KEYS = frozenset(
    {
        "notional_per_trade",
        "entry_slippage_bps",
        "exit_slippage_bps",
        "fee_bps_per_side",
        "commission_per_share_per_side",
    }
)
_COST_BODY_KEYS = frozenset(
    {
        "schema_version",
        "path_replay_id",
        "raw_entry_price",
        "raw_exit_price",
        "gross_return_pct",
        "after_cost_return_pct",
        "observed_cost_model_identity",
        "modeled_cost_model_identity",
        "components",
    }
)
_COST_RECEIPT_KEYS = _COST_BODY_KEYS | {
    "receipt_id",
    "receipt_hash_sha256",
}
_CAUSAL_IDENTITY_KEYS = frozenset(
    {
        "kind",
        "decision_id",
        "decision_at",
        "input_hash_sha256",
        "source_lineage_hash_sha256",
        "decision_context_hash_sha256",
    }
)
_RECONCILIATION_COMPONENT_KEYS = frozenset(
    {
        "path_replay_id",
        "cost_receipt_hash_sha256",
        "primary_benchmark_symbol",
        "primary_benchmark_return_pct",
        "primary_benchmark_source_bar_hash_sha256",
        "secondary_benchmark_symbol",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_source_bar_hash_sha256",
        "after_cost_return_pct",
        "net_excess_return_pct",
        "causal_decision_identity",
    }
)
_RECONCILIATION_RECEIPT_KEYS = frozenset(
    {
        "schema_version",
        "receipt_id",
        "receipt_hash_sha256",
        "status",
        "components",
    }
)
_NA_NULL_FIELDS = frozenset(
    {
        "after_cost_return_pct",
        "net_excess_return_pct",
        "cost_schema_version",
        "cost_receipt_id",
        "cost_receipt_hash_sha256",
        "cost_receipt",
        "benchmark_symbol",
        "benchmark_return_pct",
        "benchmark_source_bar_hash_sha256",
        "secondary_benchmark_symbol",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_source_bar_hash_sha256",
        "reconciliation_schema_version",
        "reconciliation_receipt_id",
        "reconciliation_receipt_hash_sha256",
        "reconciliation_receipt",
    }
)
_NA_STATUS_FIELDS = frozenset(
    {
        "benchmark_independent_reconciliation_status",
        "secondary_benchmark_independent_reconciliation_status",
        "independent_reconciliation_status",
    }
)
_RETURN_PROJECTION_KEYS = frozenset(
    {
        "path_replay_receipt",
        "replay_binding",
        "outcome_id",
        "outcome_status",
        "activation_status",
        "source_bar_hash_sha256",
        "source_bar_count",
        "exit_event",
        "gross_return_pct",
        "after_cost_return_pct",
        "return_truth_schema_version",
        "return_truth_hash_sha256",
        "cost_schema_version",
        "cost_receipt_id",
        "cost_receipt_hash_sha256",
        "cost_receipt",
        "observed_cost_model_identity",
        "modeled_cost_model_identity",
        "cost_components",
        "benchmark_symbol",
        "benchmark_return_pct",
        "benchmark_source_bar_hash_sha256",
        "benchmark_independent_reconciliation_status",
        "secondary_benchmark_symbol",
        "secondary_benchmark_return_pct",
        "secondary_benchmark_source_bar_hash_sha256",
        "secondary_benchmark_independent_reconciliation_status",
        "net_excess_return_pct",
        "reconciliation_schema_version",
        "independent_reconciliation_status",
        "reconciliation_receipt_id",
        "reconciliation_receipt_hash_sha256",
        "reconciliation_receipt",
        "causal_decision_identity",
        "learning_eligible",
        "activation_label_eligible",
        "retrospective_research_eligible",
        "prospective_promotion_eligible",
        "eligibility_policy_version",
        "no_lookahead",
        "validated_against_signal_timestamp",
        "evidence_cohort",
        "research_only",
        "broker_execution_enabled",
        "excursion_exact",
        "mfe_pct",
        "mae_pct",
        "max_favorable_excursion_pct",
        "max_adverse_excursion_pct",
    }
)


def canonical_return_truth_valid(
    payload: object,
    *,
    decision: object,
) -> bool:
    """Return whether *payload* carries complete current return truth."""

    try:
        return not _return_violations(payload, decision)
    except Exception:
        return False


def canonical_return_truth_violations(
    payload: object,
    *,
    decision: object,
) -> tuple[str, ...]:
    """Return deterministic validation violations without raising."""

    try:
        return tuple(_return_violations(payload, decision))
    except Exception:
        return ("return_truth:unreadable",)


def classify_canonical_return_truth(
    payload: object,
    *,
    decision: object,
) -> str:
    """Classify current truth while quarantining legacy or incomplete rows."""

    try:
        if isinstance(payload, Mapping) and payload.get("outcome_status") == TERMINAL_MISSING:
            return (
                TERMINAL_MISSING
                if set(payload) == {"outcome_status"}
                else LEGACY_OR_INCOMPLETE
            )
        common = _common_current_path_violations(payload, decision)
        if common:
            return LEGACY_OR_INCOMPLETE
        if not _return_violations(payload, decision):
            return CURRENT_RETURN_TRUTH
        if _nonreturn_violations(payload, not_triggered=True) == []:
            return CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED
        if _nonreturn_violations(payload, not_triggered=False) == []:
            return CURRENT_CENSORED_PATH
        return LEGACY_OR_INCOMPLETE
    except Exception:
        return LEGACY_OR_INCOMPLETE


def canonical_return_truth_projection(
    payload: object,
    *,
    decision: object,
) -> dict[str, Any]:
    """Deep-copy only fields validated under a current canonical contract."""

    classification = classify_canonical_return_truth(payload, decision=decision)
    if classification not in {
        CURRENT_RETURN_TRUTH,
        CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
        CURRENT_CENSORED_PATH,
    } or not isinstance(payload, Mapping):
        return {}
    receipt = payload.get("path_replay_receipt")
    if not isinstance(receipt, dict):
        return {}
    keys = set(receipt) | set(_RETURN_PROJECTION_KEYS)
    if classification == CURRENT_RETURN_TRUTH:
        keys.discard("activation_label_eligible")
    else:
        keys.difference_update(
            {
                "mfe_pct",
                "mae_pct",
                "max_favorable_excursion_pct",
                "max_adverse_excursion_pct",
            }
        )
    try:
        return {
            key: copy.deepcopy(payload[key])
            for key in sorted(keys)
            if key in payload
        }
    except Exception:
        return {}


def canonical_paper_selection_context(
    selection: Mapping[str, object],
    *,
    delivery: Mapping[str, object],
) -> dict[str, object]:
    """Build the current paper-decision context from exact persisted evidence.

    ``signal_selections`` exposes immutable identity columns plus the original
    payload, while delivery truth lives in a separate immutable row.  This
    adapter refuses conflicting projections and derives every hash from those
    two persisted records; callers cannot supply self-asserted lineage hashes.
    """

    payload = selection.get("payload_json")
    persisted = payload if isinstance(payload, Mapping) else {}
    fields = (
        "selection_id",
        "scan_id",
        "signal_id",
        "ticker",
        "strategy_id",
        "strategy_version",
        "cohort",
        "decision",
        "selected_at",
        "event_key",
        "body_sha256",
    )
    selected: dict[str, object] = {}
    for field in fields:
        column_value = selection.get(field)
        payload_value = persisted.get(field)
        if payload_value is not None and not _json_equal(
            payload_value,
            column_value,
        ):
            raise ValueError(f"paper selection {field} conflicts with payload")
        selected[field] = column_value
    for field in fields:
        if not _nonblank_text(selected.get(field)):
            raise ValueError(f"paper selection {field} is missing")
    body_hash = selected["body_sha256"]
    if not _valid_sha(body_hash):
        raise ValueError("paper selection body_sha256 is not canonical")
    selected_at = _canonical_utc(selected["selected_at"])
    if selected_at is None:
        raise ValueError("paper selection selected_at is not canonical UTC")
    market_date = selected_at.date().isoformat()
    payload_market_date = persisted.get("market_date")
    if payload_market_date is not None and payload_market_date != market_date:
        raise ValueError("paper selection market_date conflicts with selected_at")
    if str(selected["cohort"]) != "official_telegram":
        raise ValueError("paper selection is not the official Telegram cohort")
    if persisted.get("research_only") is not True:
        raise ValueError("paper selection is not research-only")
    if persisted.get("broker_execution_enabled") is not False:
        raise ValueError("paper selection broker execution must be disabled")
    signal_payload = persisted.get("signal")
    decision_payload = persisted.get("decision_payload")
    if not isinstance(signal_payload, Mapping):
        raise ValueError("paper selection lacks its immutable signal payload")
    if not isinstance(decision_payload, Mapping):
        raise ValueError("paper selection lacks its immutable decision payload")
    selected_decision = str(selected["decision"])
    payload_decision = (
        "no_trade"
        if decision_payload.get("no_trade") is True
        else str(
            decision_payload.get("decision_tier")
            or decision_payload.get("decision")
            or ""
        )
    )
    if payload_decision != selected_decision:
        raise ValueError("paper selection decision conflicts with decision payload")
    if selected_decision == "no_trade":
        if str(selected["ticker"]).upper() != "NO_TRADE":
            raise ValueError("paper no-trade selection ticker is inconsistent")
    elif str(selected["ticker"]).upper() == "NO_TRADE":
        raise ValueError("paper selected decision cannot use the no-trade ticker")
    signal_scan_id = str(signal_payload.get("scan_id") or "")
    selection_scan_id = str(selected["scan_id"])
    if signal_scan_id != selection_scan_id:
        from intraday_scanner.services.luna_research_slate_service import (
            validated_frozen_selection_signal,
        )

        frozen_signal = validated_frozen_selection_signal(
            dict(selection),
            market_date=market_date,
            allowed_cohorts=("official_telegram",),
        )
        if frozen_signal is None or not _json_equal(frozen_signal, signal_payload):
            raise ValueError(
                "paper selection signal scan conflicts without exact frozen-slate lineage"
            )
    for field in ("signal_id", "ticker", "market_date"):
        signal_value = signal_payload.get(field)
        if field == "signal_id":
            signal_value = signal_value or signal_payload.get("signal_key")
        if not _json_equal(
            signal_value,
            {
                "signal_id": selected["signal_id"],
                "ticker": str(selected["ticker"]).upper(),
                "market_date": market_date,
            }[field],
        ):
            raise ValueError(f"paper selection signal {field} conflicts with identity")

    delivery_fields = (
        "membership_id",
        "selection_id",
        "scan_id",
        "signal_id",
        "ticker",
        "strategy_id",
        "strategy_version",
        "cohort",
        "decision",
        "selected_at",
        "event_key",
        "channel",
        "delivery_status",
        "body_sha256",
    )
    delivered = {field: delivery.get(field) for field in delivery_fields}
    for field in delivery_fields:
        if not _nonblank_text(delivered.get(field)):
            raise ValueError(f"paper delivery {field} is missing")
    for field in (
        "selection_id",
        "scan_id",
        "signal_id",
        "ticker",
        "strategy_id",
        "strategy_version",
        "cohort",
        "decision",
        "selected_at",
        "event_key",
        "body_sha256",
    ):
        if not _json_equal(delivered[field], selected[field]):
            raise ValueError(f"paper delivery {field} conflicts with selection")
    if delivered["channel"] != "telegram":
        raise ValueError("paper selection lacks Telegram delivery truth")
    if delivered["delivery_status"] != "delivered":
        raise ValueError("paper selection lacks current delivered status")
    delivery_payload = delivery.get("payload_json")
    if not isinstance(delivery_payload, Mapping):
        raise ValueError("paper delivery lacks its immutable payload")
    for field in delivery_fields:
        if not _json_equal(delivery_payload.get(field), delivered[field]):
            raise ValueError(f"paper delivery {field} conflicts with payload")
    delivered_body = delivery_payload.get("body")
    if not _nonblank_text(delivered_body):
        raise ValueError("paper delivery lacks its rendered notification body")
    rendered_body_hash = hashlib.sha256(str(delivered_body).encode("utf-8")).hexdigest()
    if not _secure_equal(body_hash, rendered_body_hash):
        raise ValueError("paper delivery body hash is invalid")
    if delivery_payload.get("research_only") is not True:
        raise ValueError("paper delivery is not research-only")
    if selected_decision != "no_trade":
        official_tickers = _rendered_official_candidate_tickers(delivered_body)
        if (
            official_tickers is None
            or official_tickers.count(str(selected["ticker"]).upper()) != 1
        ):
            raise ValueError(
                "paper delivery official candidate section must contain selected "
                "ticker exactly once"
            )

    selection_evidence = {
        "schema_version": "dawnstrike.alphaops.paper_selection_context.v3",
        "selection": selected,
        "delivery": delivered,
        "delivered_body": delivered_body,
        "signal": signal_payload,
        "decision_payload": decision_payload,
    }
    source_hash = _hash_payload(selection_evidence)
    input_hash = _hash_payload(
        {
            "schema_version": "dawnstrike.alphaops.paper_selection_input.v3",
            "selection": selected,
            "signal": signal_payload,
            "decision_payload": decision_payload,
        }
    )
    lineage_hash = _hash_payload(
        {
            "schema_version": "dawnstrike.alphaops.paper_selection_lineage.v3",
            "selection_id": selected["selection_id"],
            "scan_id": selected["scan_id"],
            "signal_id": selected["signal_id"],
            "body_sha256": body_hash,
            "delivery": delivered,
        }
    )
    if source_hash is None or input_hash is None or lineage_hash is None:
        raise ValueError("paper selection evidence is not canonical JSON")
    delivery_identity = {
        "membership_id": delivered["membership_id"],
        "channel": delivered["channel"],
        "event_key": delivered["event_key"],
        "delivery_status": delivered["delivery_status"],
        "body_sha256": delivered["body_sha256"],
    }
    return {
        "selection_id": selected["selection_id"],
        "scan_id": selected["scan_id"],
        "signal_id": selected["signal_id"],
        "ticker": str(selected["ticker"]).upper(),
        "market_date": market_date,
        "strategy_id": selected["strategy_id"],
        "strategy_version": selected["strategy_version"],
        "cohort": selected["cohort"],
        "decision": selected["decision"],
        "selected_at": selected["selected_at"],
        "input_hash_sha256": input_hash,
        "source_lineage_hash_sha256": lineage_hash,
        "delivery_identity": delivery_identity,
        "source_artifact_identity": (
            f"alpha-paper-selection:{selected['selection_id']}"
        ),
        "source_artifact_hash_sha256": source_hash,
        "research_only": True,
        "broker_execution_enabled": False,
        "authoritative_signal": copy.deepcopy(dict(signal_payload)),
    }


def canonical_paper_enter_intent_context(
    selection: Mapping[str, object],
    *,
    intent_record: Mapping[str, object],
    source_observation_record: Mapping[str, object],
) -> dict[str, object]:
    """Bind a selected V5 plan to one exact watcher entry and source bar."""

    _require_current_paper_selection(selection)
    selected_at = _canonical_utc(selection.get("selected_at"))
    activation_at = _aware_utc(DEFAULT_V5_POLICY.activation_timestamp)
    if not (
        selection.get("decision") == "clean_edge"
        and selection.get("strategy_id") == ALPHAOPS_V5_STRATEGY_ID
        and selection.get("strategy_version") == ALPHAOPS_V5_STRATEGY_VERSION
        and selected_at is not None
        and activation_at is not None
        and selected_at >= activation_at
    ):
        raise ValueError("paper entry intent requires a clean-edge selection")
    intent_columns, intent_payload = _raw_record_parts(
        intent_record,
        label="trade intent",
    )
    observation_columns, observation_payload = _raw_record_parts(
        source_observation_record,
        label="price observation",
    )
    _validate_intent_column_projection(intent_columns, intent_payload)
    source_receipt = _canonical_source_observation_receipt(
        observation_columns,
        observation_payload,
        selection=selection,
    )
    _validate_current_v5_entry_intent(
        intent_columns,
        intent_payload,
        selection=selection,
        source_receipt=source_receipt,
    )
    body: dict[str, object] = {
        "schema_version": PAPER_ENTER_INTENT_RECEIPT_SCHEMA_VERSION,
        **{
            field: copy.deepcopy(intent_payload[field])
            for field in (
                "intent_id",
                "selection_id",
                "episode_id",
                "matched_strategy_ids",
                "primary_strategy_id",
                "episode_dedup_counts",
                "signal_id",
                "ticker",
                "market_date",
                "mode",
                "lifecycle_state",
                "action",
                "decision_time",
                "decision_price",
                "trigger_price",
                "stop_price",
                "target_price",
                "quantity",
                "notional",
                "risk_amount",
                "source_observation_id",
                "source_bar_hash_sha256",
                "source_observed_at",
                "source_bar_completed_at",
                "account_id",
                "execution_policy_version",
                "cost_model_version",
                "decision_fingerprint",
                "decision_trace",
                "official_paper_eligible",
            )
        },
        "scan_id": selection["scan_id"],
        "source_observation_receipt": source_receipt,
        "raw_intent_record": {
            "columns": copy.deepcopy(dict(intent_columns)),
            "payload_json": copy.deepcopy(dict(intent_payload)),
        },
    }
    receipt_hash = _hash_payload(body)
    if receipt_hash is None:
        raise ValueError("paper entry intent receipt is not canonical JSON")
    receipt = {
        **body,
        "receipt_id": f"{PAPER_ENTER_INTENT_RECEIPT_ID_PREFIX}{receipt_hash}",
        "receipt_hash_sha256": receipt_hash,
    }
    selection_input_hash = selection.get("input_hash_sha256")
    selection_lineage_hash = selection.get("source_lineage_hash_sha256")
    input_hash = _hash_payload(
        {
            "schema_version": "dawnstrike.alphaops.paper_enter_input.v1",
            "selection_input_hash_sha256": selection_input_hash,
            "entry_intent_receipt_hash_sha256": receipt_hash,
        }
    )
    lineage_hash = _hash_payload(
        {
            "schema_version": "dawnstrike.alphaops.paper_enter_lineage.v1",
            "selection_source_lineage_hash_sha256": selection_lineage_hash,
            "entry_intent_receipt_hash_sha256": receipt_hash,
        }
    )
    if input_hash is None or lineage_hash is None:
        raise ValueError("paper enter context hashes are unavailable")
    return {
        **copy.deepcopy(dict(selection)),
        "intent_id": receipt["intent_id"],
        "decision_at": receipt["decision_time"],
        "input_hash_sha256": input_hash,
        "source_lineage_hash_sha256": lineage_hash,
        "source_artifact_identity": receipt["receipt_id"],
        "source_artifact_hash_sha256": receipt_hash,
        "entry_intent_receipt": receipt,
    }


def build_canonical_path_entry_receipt(
    decision: Mapping[str, object],
) -> dict[str, object]:
    """Project one validated paper-enter decision into the path seed receipt."""

    binding = canonical_replay_binding(
        decision,
        kind="alpha_paper_enter_intent",
    )
    intent = decision.get("entry_intent_receipt")
    if not _canonical_paper_enter_intent_receipt_valid(intent):
        raise ValueError("paper enter decision lacks a canonical intent receipt")
    assert isinstance(intent, Mapping)
    raw_entry_price = _number(intent.get("decision_price"))
    if raw_entry_price is None or raw_entry_price <= 0.0:
        raise ValueError("paper enter decision has an invalid entry price")
    binding_origin = binding["origin"]
    assert isinstance(binding_origin, Mapping)
    body: dict[str, object] = {
        "schema_version": ENTRY_RECEIPT_SCHEMA_VERSION,
        "entry_mode": ENTRY_MODE_ALREADY_ENTERED,
        "raw_entry_price": raw_entry_price,
        "effective_at": intent["decision_time"],
        "source_observation_id": intent["source_observation_id"],
        "source_bar_hash_sha256": intent["source_bar_hash_sha256"],
        "source_observed_at": intent["source_observed_at"],
        "source_bar_completed_at": intent["source_bar_completed_at"],
        "replay_origin": {
            key: copy.deepcopy(binding_origin[key])
            for key in ("kind", "id", "lineage")
        },
    }
    digest = _hash_payload(body)
    if digest is None:
        raise ValueError("path entry receipt is not canonical JSON")
    return {
        **body,
        "receipt_id": f"{ENTRY_RECEIPT_ID_PREFIX}{digest}",
        "receipt_hash_sha256": digest,
    }


def canonical_replay_binding(
    decision: Mapping[str, object],
    *,
    kind: str,
) -> dict[str, object]:
    """Return the exact replay origin authenticated by a current decision."""

    violations = _decision_context_contract_violations(decision, kind=kind)
    context_hash = _decision_context_hash(decision, kind=kind)
    if violations or context_hash is None:
        detail = ", ".join(violations) or "context hash unavailable"
        raise ValueError(f"canonical decision context is invalid: {detail}")
    lineage_fields: tuple[str, ...]
    if kind == "alpha_v6_shadow_decision":
        id_key = "decision_id"
        lineage_fields = (
            "decision_id",
            "scan_id",
            "source_signal_id",
            "shadow_signal_id",
        )
    elif kind == "alpha_paper_enter_intent":
        id_key = "intent_id"
        lineage_fields = ("selection_id", "scan_id", "signal_id", "intent_id")
    elif kind == "alpha_paper_selection":
        id_key = "selection_id"
        lineage_fields = ("selection_id", "scan_id", "signal_id")
    else:
        raise ValueError(f"unsupported canonical decision kind: {kind}")
    return {
        "schema_version": "dawnstrike.path_replay_binding.v1",
        "subject": {
            "symbol": decision["ticker"],
            "market_date": decision["market_date"],
        },
        "origin": {
            "kind": kind,
            "id": decision[id_key],
            "lineage": {field: decision[field] for field in lineage_fields},
            "context_hash_sha256": context_hash,
        },
    }


def build_canonical_return_truth(
    *,
    path_replay_receipt: Mapping[str, object],
    decision: Mapping[str, object],
    decision_kind: str,
    notional_per_trade: float,
    entry_slippage_bps: float,
    exit_slippage_bps: float,
    fee_bps_per_side: float,
    commission_per_share_per_side: float,
    observed_cost_model_identity: str,
    modeled_cost_model_identity: str,
    benchmark_return_pct: float | None,
    benchmark_source_bar_hash_sha256: str | None,
    benchmark_independent_reconciliation_status: str,
    secondary_benchmark_return_pct: float | None,
    secondary_benchmark_source_bar_hash_sha256: str | None,
    secondary_benchmark_independent_reconciliation_status: str,
    prospective_promotion_eligible: bool,
) -> dict[str, Any]:
    """Construct one current canonical outcome without repricing path truth."""

    receipt = dict(path_replay_receipt)
    if not canonical_path_contract_valid(receipt):
        raise ValueError("path replay receipt is not canonical")
    binding = canonical_replay_binding(decision, kind=decision_kind)
    manifest = receipt.get("replay_input_manifest")
    if not isinstance(manifest, Mapping) or not _json_equal(
        manifest.get("replay_binding"),
        binding,
    ):
        raise ValueError("path replay receipt is not bound to the decision")
    causal = _canonical_causal_identity(decision, kind=decision_kind)
    bars = manifest.get("bars")
    if not isinstance(bars, list):
        raise ValueError("path replay receipt bars are unavailable")
    common: dict[str, Any] = {
        **receipt,
        "path_replay_receipt": receipt,
        "replay_binding": binding,
        "source_bar_hash_sha256": receipt.get("source_artifact_hash_sha256"),
        "source_bar_count": len(bars),
        "exit_event": receipt.get("path_event"),
        "causal_decision_identity": causal,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "no_lookahead": True,
        "validated_against_signal_timestamp": True,
        "evidence_cohort": "forward-current-v2",
        "research_only": True,
        "broker_execution_enabled": False,
    }
    if not canonical_path_return_eligible(receipt):
        result = _build_canonical_nonreturn_truth(common, receipt)
        classification = classify_canonical_return_truth(result, decision=decision)
        if classification not in {
            CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED,
            CURRENT_CENSORED_PATH,
        }:
            raise ValueError("canonical non-return projection did not validate")
        return result

    numeric = (
        notional_per_trade,
        entry_slippage_bps,
        exit_slippage_bps,
        fee_bps_per_side,
        commission_per_share_per_side,
    )
    if any(type(value) not in {int, float} or not math.isfinite(float(value)) for value in numeric):
        raise ValueError("canonical cost inputs must be finite numbers")
    if (
        float(notional_per_trade) <= 0.0
        or float(entry_slippage_bps) < 0.0
        or float(exit_slippage_bps) < 0.0
        or float(fee_bps_per_side) < 0.0
        or float(commission_per_share_per_side) < 0.0
    ):
        raise ValueError("canonical cost inputs are outside the allowed range")
    entry = _number(receipt.get("entry_price"))
    exit_price = _number(receipt.get("exit_price"))
    if entry is None or exit_price is None or entry <= 0.0 or exit_price <= 0.0:
        raise ValueError("canonical path lacks executable entry and exit prices")
    gross = ((exit_price / entry) - 1.0) * 100.0
    entry_fill = entry * (1.0 + float(entry_slippage_bps) / 10_000.0)
    exit_fill = exit_price * (1.0 - float(exit_slippage_bps) / 10_000.0)
    quantity = float(notional_per_trade) / entry_fill
    entry_fee = (
        entry_fill * quantity * float(fee_bps_per_side) / 10_000.0
        + quantity * float(commission_per_share_per_side)
    )
    exit_fee = (
        exit_fill * quantity * float(fee_bps_per_side) / 10_000.0
        + quantity * float(commission_per_share_per_side)
    )
    after_cost = (
        (((exit_fill - entry_fill) * quantity) - entry_fee - exit_fee)
        / float(notional_per_trade)
        * 100.0
    )
    cost_components = {
        "notional_per_trade": float(notional_per_trade),
        "entry_slippage_bps": float(entry_slippage_bps),
        "exit_slippage_bps": float(exit_slippage_bps),
        "fee_bps_per_side": float(fee_bps_per_side),
        "commission_per_share_per_side": float(commission_per_share_per_side),
    }
    cost_body = {
        "schema_version": COST_TRUTH_SCHEMA_VERSION,
        "path_replay_id": receipt["path_replay_id"],
        "raw_entry_price": entry,
        "raw_exit_price": exit_price,
        "gross_return_pct": gross,
        "after_cost_return_pct": after_cost,
        "observed_cost_model_identity": observed_cost_model_identity,
        "modeled_cost_model_identity": modeled_cost_model_identity,
        "components": cost_components,
    }
    cost_hash = _hash_payload(cost_body)
    if cost_hash is None:
        raise ValueError("canonical cost receipt is not serializable")
    cost_receipt = {
        **cost_body,
        "receipt_id": f"cost-v2-{cost_hash}",
        "receipt_hash_sha256": cost_hash,
    }
    if (
        benchmark_return_pct is None
        or secondary_benchmark_return_pct is None
        or not _valid_sha(benchmark_source_bar_hash_sha256)
        or not _valid_sha(secondary_benchmark_source_bar_hash_sha256)
        or benchmark_independent_reconciliation_status != "PASSED"
        or secondary_benchmark_independent_reconciliation_status != "PASSED"
    ):
        raise ValueError("canonical benchmark truth is incomplete")
    primary_return = _number(benchmark_return_pct)
    secondary_return = _number(secondary_benchmark_return_pct)
    if primary_return is None or secondary_return is None:
        raise ValueError("canonical benchmark returns must be finite")
    net_excess = after_cost - primary_return
    reconciliation_components = {
        "path_replay_id": receipt["path_replay_id"],
        "cost_receipt_hash_sha256": cost_hash,
        "primary_benchmark_symbol": "SPY",
        "primary_benchmark_return_pct": primary_return,
        "primary_benchmark_source_bar_hash_sha256": benchmark_source_bar_hash_sha256,
        "secondary_benchmark_symbol": "IWM",
        "secondary_benchmark_return_pct": secondary_return,
        "secondary_benchmark_source_bar_hash_sha256": (
            secondary_benchmark_source_bar_hash_sha256
        ),
        "after_cost_return_pct": after_cost,
        "net_excess_return_pct": net_excess,
        "causal_decision_identity": causal,
    }
    reconciliation_body = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "status": "PASSED",
        "components": reconciliation_components,
    }
    reconciliation_hash = _hash_payload(reconciliation_body)
    if reconciliation_hash is None:
        raise ValueError("canonical reconciliation receipt is not serializable")
    reconciliation_receipt = {
        **reconciliation_body,
        "receipt_id": f"reconciliation-v2-{reconciliation_hash}",
        "receipt_hash_sha256": reconciliation_hash,
    }
    result = {
        **common,
        "outcome_status": "complete_sourced",
        "activation_status": "ACTIVATED",
        "gross_return_pct": gross,
        "after_cost_return_pct": after_cost,
        "return_truth_schema_version": RETURN_TRUTH_SCHEMA_VERSION,
        "cost_schema_version": COST_TRUTH_SCHEMA_VERSION,
        "cost_receipt_id": cost_receipt["receipt_id"],
        "cost_receipt_hash_sha256": cost_hash,
        "cost_receipt": cost_receipt,
        "observed_cost_model_identity": observed_cost_model_identity,
        "modeled_cost_model_identity": modeled_cost_model_identity,
        "cost_components": cost_components,
        "benchmark_symbol": "SPY",
        "benchmark_return_pct": primary_return,
        "benchmark_source_bar_hash_sha256": benchmark_source_bar_hash_sha256,
        "benchmark_independent_reconciliation_status": "PASSED",
        "secondary_benchmark_symbol": "IWM",
        "secondary_benchmark_return_pct": secondary_return,
        "secondary_benchmark_source_bar_hash_sha256": (
            secondary_benchmark_source_bar_hash_sha256
        ),
        "secondary_benchmark_independent_reconciliation_status": "PASSED",
        "net_excess_return_pct": net_excess,
        "reconciliation_schema_version": RECONCILIATION_SCHEMA_VERSION,
        "independent_reconciliation_status": "PASSED",
        "reconciliation_receipt_id": reconciliation_receipt["receipt_id"],
        "reconciliation_receipt_hash_sha256": reconciliation_hash,
        "reconciliation_receipt": reconciliation_receipt,
        "learning_eligible": True,
        "retrospective_research_eligible": True,
        "prospective_promotion_eligible": prospective_promotion_eligible,
        "excursion_exact": receipt.get("excursion_exact"),
    }
    _add_excursion_percentages(result, receipt)
    truth_hash = _canonical_return_truth_hash(result, receipt)
    result["return_truth_hash_sha256"] = truth_hash
    result["outcome_id"] = _canonical_outcome_id(
        truth_hash=truth_hash,
        causal=causal,
        binding=binding,
    )
    if not canonical_return_truth_valid(result, decision=decision):
        detail = ", ".join(canonical_return_truth_violations(result, decision=decision))
        raise ValueError(f"canonical return projection did not validate: {detail}")
    return result


def _canonical_causal_identity(
    decision: Mapping[str, object],
    *,
    kind: str,
) -> dict[str, object]:
    context_hash = _decision_context_hash(decision, kind=kind)
    if context_hash is None:
        raise ValueError("canonical decision context hash is unavailable")
    if kind == "alpha_v6_shadow_decision":
        id_key, time_key = "decision_id", "decision_at"
    elif kind == "alpha_paper_enter_intent":
        id_key, time_key = "intent_id", "decision_at"
    elif kind == "alpha_paper_selection":
        id_key, time_key = "selection_id", "selected_at"
    else:
        raise ValueError(f"unsupported canonical decision kind: {kind}")
    return {
        "kind": kind,
        "decision_id": decision[id_key],
        "decision_at": decision[time_key],
        "input_hash_sha256": decision["input_hash_sha256"],
        "source_lineage_hash_sha256": decision["source_lineage_hash_sha256"],
        "decision_context_hash_sha256": context_hash,
    }


def _build_canonical_nonreturn_truth(
    common: dict[str, Any],
    receipt: Mapping[str, object],
) -> dict[str, Any]:
    not_triggered = receipt.get("path_truth_status") == PathTruthStatus.NOT_TRIGGERED.value
    identity_hash = _hash_payload(
        {
            "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
            "path_replay_id": receipt.get("path_replay_id"),
            "path_replay_receipt_hash_sha256": receipt.get(
                "replay_receipt_hash_sha256"
            ),
            "causal_decision_identity": common["causal_decision_identity"],
            "replay_binding": common["replay_binding"],
        }
    )
    if identity_hash is None:
        raise ValueError("canonical non-return identity is unavailable")
    result = {
        **common,
        "outcome_id": f"outcome-v2-{identity_hash}",
        "outcome_status": "not_triggered" if not_triggered else "captured_ineligible",
        "activation_status": "NOT_TRIGGERED" if not_triggered else "INELIGIBLE",
        "gross_return_pct": None,
        "after_cost_return_pct": None,
        "net_excess_return_pct": None,
        "return_truth_schema_version": None,
        "return_truth_hash_sha256": None,
        "cost_schema_version": None,
        "cost_receipt_id": None,
        "cost_receipt_hash_sha256": None,
        "cost_receipt": None,
        "observed_cost_model_identity": None,
        "modeled_cost_model_identity": None,
        "cost_components": None,
        "benchmark_symbol": None,
        "benchmark_return_pct": None,
        "benchmark_source_bar_hash_sha256": None,
        "benchmark_independent_reconciliation_status": "NOT_APPLICABLE",
        "secondary_benchmark_symbol": None,
        "secondary_benchmark_return_pct": None,
        "secondary_benchmark_source_bar_hash_sha256": None,
        "secondary_benchmark_independent_reconciliation_status": "NOT_APPLICABLE",
        "reconciliation_schema_version": None,
        "independent_reconciliation_status": "NOT_APPLICABLE",
        "reconciliation_receipt_id": None,
        "reconciliation_receipt_hash_sha256": None,
        "reconciliation_receipt": None,
        "learning_eligible": not_triggered,
        "activation_label_eligible": not_triggered,
        "retrospective_research_eligible": False,
        "prospective_promotion_eligible": False,
    }
    return result


def _add_excursion_percentages(
    result: dict[str, Any],
    receipt: Mapping[str, object],
) -> None:
    entry = _number(receipt.get("entry_price"))
    for price_key, pct_key, alias_key in (
        ("mfe_price", "mfe_pct", "max_favorable_excursion_pct"),
        ("mae_price", "mae_pct", "max_adverse_excursion_pct"),
    ):
        price = _number(receipt.get(price_key))
        value = (
            ((price / entry) - 1.0) * 100.0
            if receipt.get("excursion_exact") is True
            and price is not None
            and entry is not None
            and entry > 0.0
            else None
        )
        result[pct_key] = value
        result[alias_key] = value


def _canonical_return_truth_hash(
    payload: Mapping[str, object],
    receipt: Mapping[str, object],
) -> str:
    body = {
        "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
        "path_replay_id": receipt.get("path_replay_id"),
        "path_replay_receipt_hash_sha256": receipt.get(
            "replay_receipt_hash_sha256"
        ),
        "source_artifact_hash_sha256": receipt.get(
            "source_artifact_hash_sha256"
        ),
        "source_bar_count": payload.get("source_bar_count"),
        "replay_binding": payload.get("replay_binding"),
        "cost_receipt_hash_sha256": payload.get("cost_receipt_hash_sha256"),
        "benchmark_source_bar_hash_sha256": payload.get(
            "benchmark_source_bar_hash_sha256"
        ),
        "secondary_benchmark_source_bar_hash_sha256": payload.get(
            "secondary_benchmark_source_bar_hash_sha256"
        ),
        "reconciliation_receipt_hash_sha256": payload.get(
            "reconciliation_receipt_hash_sha256"
        ),
        "after_cost_return_pct": payload.get("after_cost_return_pct"),
        "net_excess_return_pct": payload.get("net_excess_return_pct"),
        "causal_decision_identity": payload.get("causal_decision_identity"),
        "eligibility_policy_version": payload.get("eligibility_policy_version"),
        "retrospective_research_eligible": payload.get(
            "retrospective_research_eligible"
        ),
        "prospective_promotion_eligible": payload.get(
            "prospective_promotion_eligible"
        ),
        "evidence_cohort": payload.get("evidence_cohort"),
        "no_lookahead": payload.get("no_lookahead"),
        "validated_against_signal_timestamp": payload.get(
            "validated_against_signal_timestamp"
        ),
        "research_only": payload.get("research_only"),
        "broker_execution_enabled": payload.get("broker_execution_enabled"),
    }
    digest = _hash_payload(body)
    if digest is None:
        raise ValueError("canonical return truth is not serializable")
    return digest


def _canonical_outcome_id(
    *,
    truth_hash: str,
    causal: Mapping[str, object],
    binding: Mapping[str, object],
) -> str:
    digest = _hash_payload(
        {
            "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
            "return_truth_hash_sha256": truth_hash,
            "causal_decision_identity": causal,
            "replay_binding": binding,
        }
    )
    if digest is None:
        raise ValueError("canonical outcome identity is not serializable")
    return f"outcome-v2-{digest}"


def _return_violations(payload: object, decision: object) -> list[str]:
    violations = _common_current_path_violations(payload, decision)
    if violations or not isinstance(payload, Mapping):
        return violations
    receipt = payload["path_replay_receipt"]
    if not canonical_path_return_eligible(receipt):
        _add(violations, "path_replay_receipt:return_ineligible")
    causal = payload.get("causal_decision_identity")
    causal_kind = causal.get("kind") if isinstance(causal, Mapping) else None
    if causal_kind not in {
        "alpha_v6_shadow_decision",
        "alpha_paper_enter_intent",
    }:
        _add(violations, "causal_decision_identity:return_origin_kind")

    _require_equal(
        violations,
        payload,
        "return_truth_schema_version",
        RETURN_TRUTH_SCHEMA_VERSION,
    )
    _require_equal(violations, payload, "outcome_status", "complete_sourced")
    _require_equal(violations, payload, "activation_status", "ACTIVATED")
    _require_exact_bool(violations, payload, "learning_eligible", True)
    _require_exact_bool(
        violations,
        payload,
        "retrospective_research_eligible",
        True,
    )
    prospective = payload.get("prospective_promotion_eligible")
    if type(prospective) is not bool:
        _add(violations, "prospective_promotion_eligible:expected_bool")

    cost = _validate_cost_truth(payload, receipt, violations)
    _validate_benchmark_truth(payload, violations)
    _validate_reconciliation_truth(payload, cost, violations)
    _validate_excursion_truth(payload, receipt, violations)
    truth_hash = _validate_return_truth_hash(payload, receipt, violations)
    _validate_return_outcome_id(payload, truth_hash, violations)
    return violations


def _common_current_path_violations(
    payload: object,
    decision: object,
) -> list[str]:
    violations: list[str] = []
    if not isinstance(payload, Mapping):
        return ["return_truth:expected_mapping"]
    receipt = payload.get("path_replay_receipt")
    if not isinstance(receipt, dict):
        return ["path_replay_receipt:expected_object"]
    if not canonical_path_contract_valid(receipt):
        _add(violations, "path_replay_receipt:invalid")
        return violations
    for key, expected in receipt.items():
        if key not in payload:
            _add(violations, f"{key}:missing_flat_projection")
        elif not _json_equal(payload[key], expected):
            _add(violations, f"{key}:flat_projection_mismatch")

    _validate_replay_binding(payload, decision, receipt, violations)

    source_hash = receipt.get("source_artifact_hash_sha256")
    if not _valid_sha(source_hash):
        _add(violations, "source_artifact_hash_sha256:invalid")
    _require_equal(violations, payload, "source_bar_hash_sha256", source_hash)
    source_count = payload.get("source_bar_count")
    if type(source_count) is not int or source_count < 0:
        _add(violations, "source_bar_count:expected_nonnegative_int")
    manifest = receipt.get("replay_input_manifest")
    replay_bars = manifest.get("bars") if isinstance(manifest, Mapping) else None
    if not isinstance(replay_bars, list) or source_count != len(replay_bars):
        _add(violations, "source_bar_count:replay_row_count_mismatch")
    source_identity = receipt.get("source_artifact_identity")
    if not _nonblank_text(source_identity):
        _add(violations, "source_artifact_identity:invalid")
    if receipt.get("source_coverage_complete") is not True:
        _add(violations, "source_coverage_complete:required")
    _require_equal(violations, payload, "exit_event", receipt.get("path_event"))
    _require_equal(
        violations,
        payload,
        "eligibility_policy_version",
        ELIGIBILITY_POLICY_VERSION,
    )
    _require_exact_bool(violations, payload, "no_lookahead", True)
    _require_exact_bool(
        violations,
        payload,
        "validated_against_signal_timestamp",
        True,
    )
    _require_equal(violations, payload, "evidence_cohort", "forward-current-v2")
    _require_exact_bool(violations, payload, "research_only", True)
    _require_exact_bool(violations, payload, "broker_execution_enabled", False)
    _validate_causal_identity(payload, decision, receipt, violations)
    return violations


def _validate_replay_binding(
    payload: Mapping[str, object],
    decision: object,
    receipt: Mapping[str, object],
    violations: list[str],
) -> None:
    manifest = receipt.get("replay_input_manifest")
    binding = manifest.get("replay_binding") if isinstance(manifest, Mapping) else None
    if not isinstance(binding, Mapping):
        _add(violations, "replay_binding:required")
        return
    _require_equal(violations, payload, "replay_binding", binding)
    future_receipt = (
        manifest.get("future_evidence_receipt")
        if isinstance(manifest, Mapping)
        else None
    )
    future_subject = (
        future_receipt.get("subject")
        if isinstance(future_receipt, Mapping)
        else None
    )
    if not isinstance(future_subject, Mapping) or not _json_equal(
        binding.get("subject"),
        future_subject,
    ):
        _add(violations, "replay_binding:future_evidence_receipt_binding")
    if not isinstance(future_receipt, Mapping) or not (
        receipt.get("source_artifact_identity")
        == future_receipt.get("receipt_id")
        and receipt.get("source_artifact_hash_sha256")
        == future_receipt.get("receipt_hash_sha256")
    ):
        _add(violations, "replay_binding:future_evidence_source_binding")
    if not isinstance(decision, Mapping):
        _add(violations, "replay_binding:decision_mapping")
        return
    origin = binding.get("origin")
    if not isinstance(origin, Mapping):
        _add(violations, "replay_binding:origin")
        return
    kind = origin.get("kind")
    lineage_fields: tuple[str, ...]
    if kind == "alpha_v6_shadow_decision":
        id_key = "decision_id"
        lineage_fields = (
            "decision_id",
            "scan_id",
            "source_signal_id",
            "shadow_signal_id",
        )
    elif kind == "alpha_paper_enter_intent":
        id_key = "intent_id"
        lineage_fields = ("selection_id", "scan_id", "signal_id", "intent_id")
    elif kind == "alpha_paper_selection":
        id_key = "selection_id"
        lineage_fields = ("selection_id", "scan_id", "signal_id")
    else:
        _add(violations, "replay_binding:origin_kind")
        return
    expected = {
        "schema_version": "dawnstrike.path_replay_binding.v1",
        "subject": {
            "symbol": decision.get("ticker"),
            "market_date": decision.get("market_date"),
        },
        "origin": {
            "kind": kind,
            "id": decision.get(id_key),
            "lineage": {field: decision.get(field) for field in lineage_fields},
            "context_hash_sha256": _decision_context_hash(decision, kind=kind),
        },
    }
    if not _json_equal(binding, expected):
        _add(violations, "replay_binding:decision_binding")


def _validate_cost_truth(
    payload: Mapping[str, object],
    path_receipt: Mapping[str, object],
    violations: list[str],
) -> Mapping[str, object] | None:
    _require_equal(
        violations,
        payload,
        "cost_schema_version",
        COST_TRUTH_SCHEMA_VERSION,
    )
    cost = payload.get("cost_receipt")
    if not isinstance(cost, Mapping) or set(cost) != _COST_RECEIPT_KEYS:
        _add(violations, "cost_receipt:invalid_keys")
        return None
    if cost.get("schema_version") != COST_TRUTH_SCHEMA_VERSION:
        _add(violations, "cost_receipt:schema_version")
    body = {key: cost[key] for key in _COST_BODY_KEYS}
    cost_hash = _hash_payload(body)
    if cost_hash is None:
        _add(violations, "cost_receipt:noncanonical")
        return None
    expected_id = f"cost-v2-{cost_hash}"
    if not _secure_equal(cost.get("receipt_hash_sha256"), cost_hash):
        _add(violations, "cost_receipt:hash_mismatch")
    if cost.get("receipt_id") != expected_id:
        _add(violations, "cost_receipt:id_mismatch")
    _require_equal(violations, payload, "cost_receipt_id", expected_id)
    _require_equal(violations, payload, "cost_receipt_hash_sha256", cost_hash)

    path_id = path_receipt.get("path_replay_id")
    if cost.get("path_replay_id") != path_id:
        _add(violations, "cost_receipt:path_replay_id")
    for field in ("observed_cost_model_identity", "modeled_cost_model_identity"):
        if not _nonblank_text(cost.get(field)):
            _add(violations, f"cost_receipt:{field}")
        _require_equal(violations, payload, field, cost.get(field))

    components = cost.get("components")
    if not isinstance(components, Mapping) or set(components) != _COST_COMPONENT_KEYS:
        _add(violations, "cost_receipt:components")
        return cost
    _require_equal(violations, payload, "cost_components", components)
    notional = components.get("notional_per_trade")
    entry_slippage = components.get("entry_slippage_bps")
    exit_slippage = components.get("exit_slippage_bps")
    fee = components.get("fee_bps_per_side")
    commission = components.get("commission_per_share_per_side")
    raw_entry = cost.get("raw_entry_price")
    raw_exit = cost.get("raw_exit_price")
    notional_number = _number(notional)
    entry_slippage_number = _number(entry_slippage)
    exit_slippage_number = _number(exit_slippage)
    fee_number = _number(fee)
    commission_number = _number(commission)
    raw_entry_number = _number(raw_entry)
    raw_exit_number = _number(raw_exit)
    numeric = (
        notional_number,
        entry_slippage_number,
        exit_slippage_number,
        fee_number,
        commission_number,
        raw_entry_number,
        raw_exit_number,
    )
    if any(value is None for value in numeric):
        _add(violations, "cost_receipt:numeric_types")
        return cost
    assert notional_number is not None
    assert entry_slippage_number is not None
    assert exit_slippage_number is not None
    assert fee_number is not None
    assert commission_number is not None
    assert raw_entry_number is not None
    assert raw_exit_number is not None
    if notional_number <= 0.0 or raw_entry_number <= 0.0 or raw_exit_number <= 0.0:
        _add(violations, "cost_receipt:positive_values")
        return cost
    if any(
        value < 0.0
        for value in (
            entry_slippage_number,
            exit_slippage_number,
            fee_number,
            commission_number,
        )
    ):
        _add(violations, "cost_receipt:negative_cost")
        return cost
    if not _close(raw_entry_number, path_receipt.get("entry_price")):
        _add(violations, "cost_receipt:entry_price")
    if not _close(raw_exit_number, path_receipt.get("exit_price")):
        _add(violations, "cost_receipt:exit_price")
    gross = ((raw_exit_number / raw_entry_number) - 1.0) * 100.0
    entry_fill = raw_entry_number * (1.0 + entry_slippage_number / 10_000.0)
    exit_fill = raw_exit_number * (1.0 - exit_slippage_number / 10_000.0)
    quantity = notional_number / entry_fill
    entry_fee = (
        entry_fill * quantity * fee_number / 10_000.0
        + quantity * commission_number
    )
    exit_fee = (
        exit_fill * quantity * fee_number / 10_000.0
        + quantity * commission_number
    )
    after_cost = (
        (((exit_fill - entry_fill) * quantity) - entry_fee - exit_fee)
        / notional_number
        * 100.0
    )
    if not _close(cost.get("gross_return_pct"), gross):
        _add(violations, "cost_receipt:gross_arithmetic")
    if not _close(cost.get("after_cost_return_pct"), after_cost):
        _add(violations, "cost_receipt:after_cost_arithmetic")
    _require_number_copy(
        violations,
        payload,
        "gross_return_pct",
        cost.get("gross_return_pct"),
    )
    _require_number_copy(
        violations,
        payload,
        "after_cost_return_pct",
        cost.get("after_cost_return_pct"),
    )
    return cost


def _validate_benchmark_truth(
    payload: Mapping[str, object],
    violations: list[str],
) -> None:
    _require_equal(violations, payload, "benchmark_symbol", "SPY")
    _require_equal(violations, payload, "secondary_benchmark_symbol", "IWM")
    for field in ("benchmark_return_pct", "secondary_benchmark_return_pct"):
        if not _finite_number(payload.get(field)):
            _add(violations, f"{field}:invalid")
    for field in (
        "benchmark_source_bar_hash_sha256",
        "secondary_benchmark_source_bar_hash_sha256",
    ):
        if not _valid_sha(payload.get(field)):
            _add(violations, f"{field}:invalid")
    for field in (
        "benchmark_independent_reconciliation_status",
        "secondary_benchmark_independent_reconciliation_status",
    ):
        _require_equal(violations, payload, field, "PASSED")
    after_cost = _number(payload.get("after_cost_return_pct"))
    primary = _number(payload.get("benchmark_return_pct"))
    excess = _number(payload.get("net_excess_return_pct"))
    if (
        after_cost is None
        or primary is None
        or excess is None
        or not _close(excess, after_cost - primary)
    ):
        _add(violations, "net_excess_return_pct:arithmetic")


def _validate_reconciliation_truth(
    payload: Mapping[str, object],
    cost: Mapping[str, object] | None,
    violations: list[str],
) -> None:
    _require_equal(
        violations,
        payload,
        "reconciliation_schema_version",
        RECONCILIATION_SCHEMA_VERSION,
    )
    _require_equal(
        violations,
        payload,
        "independent_reconciliation_status",
        "PASSED",
    )
    receipt = payload.get("reconciliation_receipt")
    if not isinstance(receipt, Mapping) or set(receipt) != _RECONCILIATION_RECEIPT_KEYS:
        _add(violations, "reconciliation_receipt:invalid_keys")
        return
    if receipt.get("schema_version") != RECONCILIATION_SCHEMA_VERSION:
        _add(violations, "reconciliation_receipt:schema_version")
    if receipt.get("status") != "PASSED":
        _add(violations, "reconciliation_receipt:status")
    components = receipt.get("components")
    if not isinstance(components, Mapping) or set(components) != _RECONCILIATION_COMPONENT_KEYS:
        _add(violations, "reconciliation_receipt:components")
        return
    body = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "status": "PASSED",
        "components": components,
    }
    receipt_hash = _hash_payload(body)
    if receipt_hash is None:
        _add(violations, "reconciliation_receipt:noncanonical")
        return
    expected_id = f"reconciliation-v2-{receipt_hash}"
    if not _secure_equal(receipt.get("receipt_hash_sha256"), receipt_hash):
        _add(violations, "reconciliation_receipt:hash_mismatch")
    if receipt.get("receipt_id") != expected_id:
        _add(violations, "reconciliation_receipt:id_mismatch")
    _require_equal(violations, payload, "reconciliation_receipt_id", expected_id)
    _require_equal(
        violations,
        payload,
        "reconciliation_receipt_hash_sha256",
        receipt_hash,
    )
    expected_components = {
        "path_replay_id": payload.get("path_replay_id"),
        "cost_receipt_hash_sha256": (
            cost.get("receipt_hash_sha256") if cost is not None else None
        ),
        "primary_benchmark_symbol": payload.get("benchmark_symbol"),
        "primary_benchmark_return_pct": payload.get("benchmark_return_pct"),
        "primary_benchmark_source_bar_hash_sha256": payload.get(
            "benchmark_source_bar_hash_sha256"
        ),
        "secondary_benchmark_symbol": payload.get("secondary_benchmark_symbol"),
        "secondary_benchmark_return_pct": payload.get(
            "secondary_benchmark_return_pct"
        ),
        "secondary_benchmark_source_bar_hash_sha256": payload.get(
            "secondary_benchmark_source_bar_hash_sha256"
        ),
        "after_cost_return_pct": payload.get("after_cost_return_pct"),
        "net_excess_return_pct": payload.get("net_excess_return_pct"),
        "causal_decision_identity": payload.get("causal_decision_identity"),
    }
    if not _json_equal(components, expected_components):
        _add(violations, "reconciliation_receipt:component_binding")


def _validate_return_truth_hash(
    payload: Mapping[str, object],
    path_receipt: Mapping[str, object],
    violations: list[str],
) -> str | None:
    body = {
        "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
        "path_replay_id": path_receipt.get("path_replay_id"),
        "path_replay_receipt_hash_sha256": path_receipt.get(
            "replay_receipt_hash_sha256"
        ),
        "source_artifact_hash_sha256": path_receipt.get(
            "source_artifact_hash_sha256"
        ),
        "source_bar_count": payload.get("source_bar_count"),
        "replay_binding": payload.get("replay_binding"),
        "cost_receipt_hash_sha256": payload.get("cost_receipt_hash_sha256"),
        "benchmark_source_bar_hash_sha256": payload.get(
            "benchmark_source_bar_hash_sha256"
        ),
        "secondary_benchmark_source_bar_hash_sha256": payload.get(
            "secondary_benchmark_source_bar_hash_sha256"
        ),
        "reconciliation_receipt_hash_sha256": payload.get(
            "reconciliation_receipt_hash_sha256"
        ),
        "after_cost_return_pct": payload.get("after_cost_return_pct"),
        "net_excess_return_pct": payload.get("net_excess_return_pct"),
        "causal_decision_identity": payload.get("causal_decision_identity"),
        "eligibility_policy_version": payload.get("eligibility_policy_version"),
        "retrospective_research_eligible": payload.get(
            "retrospective_research_eligible"
        ),
        "prospective_promotion_eligible": payload.get(
            "prospective_promotion_eligible"
        ),
        "evidence_cohort": payload.get("evidence_cohort"),
        "no_lookahead": payload.get("no_lookahead"),
        "validated_against_signal_timestamp": payload.get(
            "validated_against_signal_timestamp"
        ),
        "research_only": payload.get("research_only"),
        "broker_execution_enabled": payload.get("broker_execution_enabled"),
    }
    truth_hash = _hash_payload(body)
    if truth_hash is None or not _secure_equal(
        payload.get("return_truth_hash_sha256"), truth_hash
    ):
        _add(violations, "return_truth_hash_sha256:mismatch")
    return truth_hash


def _validate_return_outcome_id(
    payload: Mapping[str, object],
    truth_hash: str | None,
    violations: list[str],
) -> None:
    if truth_hash is None:
        _add(violations, "outcome_id:unbound_return_truth")
        return
    identity_hash = _hash_payload(
        {
            "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
            "return_truth_hash_sha256": truth_hash,
            "causal_decision_identity": payload.get("causal_decision_identity"),
            "replay_binding": payload.get("replay_binding"),
        }
    )
    expected = f"outcome-v2-{identity_hash}" if identity_hash is not None else None
    if expected is None or payload.get("outcome_id") != expected:
        _add(violations, "outcome_id:mismatch")


def _validate_excursion_truth(
    payload: Mapping[str, object],
    receipt: Mapping[str, object],
    violations: list[str],
) -> None:
    entry = receipt.get("entry_price")
    exact = receipt.get("excursion_exact")
    _require_equal(violations, payload, "excursion_exact", exact)
    fields = (
        ("mfe_price", "mfe_at", "mfe_pct", "max_favorable_excursion_pct"),
        ("mae_price", "mae_at", "mae_pct", "max_adverse_excursion_pct"),
    )
    for price_key, time_key, pct_key, alias_key in fields:
        price = receipt.get(price_key)
        observed_at = receipt.get(time_key)
        if exact is True:
            price_number = _number(price)
            if price_number is None or _canonical_utc(observed_at) is None:
                _add(violations, f"{price_key}:incomplete_exact_excursion")
                continue
            entry_number = _number(entry)
            if entry_number is None or entry_number <= 0.0:
                _add(violations, "entry_price:invalid_for_excursion")
                continue
            expected_pct = ((price_number / entry_number) - 1.0) * 100.0
            _require_number_copy(violations, payload, pct_key, expected_pct)
            _require_number_copy(violations, payload, alias_key, expected_pct)
        else:
            if price is not None or observed_at is not None:
                _add(violations, f"{price_key}:unexpected_exact_excursion")
            _require_equal(violations, payload, pct_key, None)
            _require_equal(violations, payload, alias_key, None)


def _validate_causal_identity(
    payload: Mapping[str, object],
    decision: object,
    path_receipt: Mapping[str, object],
    violations: list[str],
) -> None:
    causal = payload.get("causal_decision_identity")
    if not isinstance(causal, Mapping) or set(causal) != _CAUSAL_IDENTITY_KEYS:
        _add(violations, "causal_decision_identity:invalid_keys")
        return
    if not isinstance(decision, Mapping):
        _add(violations, "decision:expected_mapping")
        return
    kind = causal.get("kind")
    if kind == "alpha_v6_shadow_decision":
        id_key, time_key = "decision_id", "decision_at"
    elif kind == "alpha_paper_enter_intent":
        id_key, time_key = "intent_id", "decision_at"
    elif kind == "alpha_paper_selection":
        id_key, time_key = "selection_id", "selected_at"
    else:
        _add(violations, "causal_decision_identity:kind")
        return
    _validate_decision_context_safety(
        payload,
        decision,
        kind=kind,
        violations=violations,
    )
    expected = {
        "kind": kind,
        "decision_id": decision.get(id_key),
        "decision_at": decision.get(time_key),
        "input_hash_sha256": decision.get("input_hash_sha256"),
        "source_lineage_hash_sha256": decision.get(
            "source_lineage_hash_sha256"
        ),
        "decision_context_hash_sha256": _decision_context_hash(
            decision,
            kind=kind,
        ),
    }
    if not _json_equal(causal, expected):
        _add(violations, "causal_decision_identity:decision_binding")
    if not _nonblank_text(causal.get("decision_id")):
        _add(violations, "causal_decision_identity:decision_id")
    if not _valid_sha(causal.get("input_hash_sha256")):
        _add(violations, "causal_decision_identity:input_hash")
    if not _valid_sha(causal.get("source_lineage_hash_sha256")):
        _add(violations, "causal_decision_identity:source_lineage_hash")
    if not _valid_sha(causal.get("decision_context_hash_sha256")):
        _add(violations, "causal_decision_identity:decision_context_hash")
    causal_at = _canonical_utc(causal.get("decision_at"))
    manifest = path_receipt.get("replay_input_manifest")
    replay_at = (
        _canonical_utc(manifest.get("decision_at"))
        if isinstance(manifest, Mapping)
        else None
    )
    if causal_at is None or replay_at is None or causal_at > replay_at:
        _add(violations, "causal_decision_identity:time_binding")


def _decision_context_hash(
    decision: Mapping[str, object],
    *,
    kind: str,
) -> str | None:
    if _decision_context_contract_violations(decision, kind=kind):
        return None
    fields: tuple[str, ...]
    if kind == "alpha_v6_shadow_decision":
        fields = (
            "decision_id",
            "scan_id",
            "source_signal_id",
            "shadow_signal_id",
            "market_date",
            "decision_at",
            "ticker",
            "strategy_version",
            "model_version",
            "feature_schema_version",
            "feature_hash_sha256",
            "input_hash_sha256",
            "source_lineage_hash_sha256",
            "action",
            "decision_state",
            "setup_key",
            "regime_key",
            "point_in_time",
            "source_summary",
            "safety_vetoes",
            "research_only",
            "broker_execution_enabled",
            "evidence_cohort",
            "experiment_assignment",
            "signal_facts",
            "cost_model_version",
            "estimated_round_trip_cost_bps",
        )
    elif kind in {"alpha_paper_selection", "alpha_paper_enter_intent"}:
        fields = (
            "selection_id",
            "scan_id",
            "signal_id",
            "ticker",
            "market_date",
            "strategy_id",
            "strategy_version",
            "cohort",
            "decision",
            "selected_at",
            "input_hash_sha256",
            "source_lineage_hash_sha256",
            "delivery_identity",
            "source_artifact_identity",
            "source_artifact_hash_sha256",
            "research_only",
            "broker_execution_enabled",
            *(
                ("intent_id", "decision_at", "entry_intent_receipt")
                if kind == "alpha_paper_enter_intent"
                else ()
            ),
        )
    else:
        return None
    return _hash_payload(
        {
            "kind": kind,
            "decision": {field: decision.get(field) for field in fields},
        }
    )


def _validate_decision_context_safety(
    payload: Mapping[str, object],
    decision: Mapping[str, object],
    *,
    kind: str,
    violations: list[str],
) -> None:
    for violation in _decision_context_contract_violations(decision, kind=kind):
        _add(violations, f"decision_context:{violation}")
    if payload.get("research_only") is not decision.get("research_only"):
        _add(violations, "decision_context:research_only_binding")
    if payload.get("broker_execution_enabled") is not decision.get(
        "broker_execution_enabled"
    ):
        _add(violations, "decision_context:broker_binding")
    if kind == "alpha_v6_shadow_decision":
        if decision.get("evidence_cohort") != "forward-current-v2":
            _add(violations, "decision_context:evidence_cohort")
        if payload.get("evidence_cohort") != decision.get("evidence_cohort"):
            _add(violations, "decision_context:evidence_cohort_binding")
        return

    if payload.get("evidence_cohort") != "forward-current-v2":
        _add(violations, "decision_context:paper_evidence_cohort_binding")
    if decision.get("strategy_id") != "alphaops_v5":
        _add(violations, "decision_context:paper_strategy_id")
    if decision.get("strategy_version") != "dawnstrike-alphaops-v5.0.0":
        _add(violations, "decision_context:paper_strategy_version")
    if decision.get("decision") != "clean_edge":
        _add(violations, "decision_context:paper_decision")


def _decision_context_contract_violations(
    decision: Mapping[str, object],
    *,
    kind: str,
) -> list[str]:
    violations: list[str] = []
    text_fields: tuple[str, ...]
    if kind == "alpha_v6_shadow_decision":
        text_fields = (
            "decision_id",
            "scan_id",
            "source_signal_id",
            "shadow_signal_id",
            "ticker",
            "strategy_version",
            "model_version",
            "feature_schema_version",
            "action",
            "decision_state",
            "setup_key",
            "regime_key",
            "evidence_cohort",
        )
        for field in text_fields:
            if not _nonblank_text(decision.get(field)):
                _add(violations, field)
        if _canonical_market_date(decision.get("market_date")) is None:
            _add(violations, "market_date")
        if _canonical_utc(decision.get("decision_at")) is None:
            _add(violations, "decision_at")
        for field in (
            "feature_hash_sha256",
            "input_hash_sha256",
            "source_lineage_hash_sha256",
        ):
            if not _valid_sha(decision.get(field)):
                _add(violations, field)
        if decision.get("action") != "SHADOW_TRACK":
            _add(violations, "action")
        if decision.get("decision_state") != "SELECTED":
            _add(violations, "decision_state")
        if decision.get("research_only") is not True:
            _add(violations, "research_only")
        if decision.get("broker_execution_enabled") is not False:
            _add(violations, "broker_execution_enabled")
        if decision.get("evidence_cohort") != "forward-current-v2":
            _add(violations, "evidence_cohort")
        point_in_time = decision.get("point_in_time")
        if not isinstance(point_in_time, Mapping) or point_in_time.get(
            "all_inputs_observed_at_or_before_decision"
        ) is not True:
            _add(violations, "point_in_time")
        safety_vetoes = decision.get("safety_vetoes")
        if not isinstance(safety_vetoes, list) or safety_vetoes:
            _add(violations, "safety_vetoes")
        source_summary = decision.get("source_summary")
        if not isinstance(source_summary, Mapping):
            _add(violations, "source_summary")
        else:
            status = source_summary.get("status")
            if not isinstance(status, str) or status not in {"complete", "success"}:
                _add(violations, "source_summary_status")
            if not _nonblank_text(source_summary.get("primary_source")):
                _add(violations, "source_summary_primary_source")
            if not _nonblank_text(source_summary.get("source_artifact_identity")):
                _add(violations, "source_summary_identity")
            if not _valid_sha(source_summary.get("source_artifact_hash_sha256")):
                _add(violations, "source_summary_hash")
        assignment = decision.get("experiment_assignment")
        if assignment is not None:
            if not isinstance(assignment, Mapping) or set(assignment) != {
                "experiment_id",
                "arm",
                "configuration_hash_sha256",
            }:
                _add(violations, "experiment_assignment")
            elif not (
                _nonblank_text(assignment.get("experiment_id"))
                and assignment.get("arm") in {"baseline", "candidate"}
                and _valid_sha(assignment.get("configuration_hash_sha256"))
            ):
                _add(violations, "experiment_assignment")
        signal_facts = decision.get("signal_facts")
        expected_fact_keys = {
            "ticker",
            "rank",
            "alpha_score",
            "entry_watch_level",
            "target_1",
            "invalidation_level",
            "can_alert",
            "alert_gate_status",
            "no_trade_reason",
            "source_confidence",
            "source",
            "source_url",
        }
        if not isinstance(signal_facts, Mapping) or set(signal_facts) != expected_fact_keys:
            _add(violations, "signal_facts")
        else:
            if signal_facts.get("ticker") != decision.get("ticker"):
                _add(violations, "signal_facts_ticker")
            rank = signal_facts.get("rank")
            if type(rank) is not int or rank <= 0:
                _add(violations, "signal_facts_rank")
            for field in (
                "alpha_score",
                "entry_watch_level",
                "target_1",
                "invalidation_level",
                "source_confidence",
            ):
                if _number(signal_facts.get(field)) is None:
                    _add(violations, f"signal_facts_{field}")
            entry = _number(signal_facts.get("entry_watch_level"))
            target = _number(signal_facts.get("target_1"))
            stop = _number(signal_facts.get("invalidation_level"))
            if not (
                entry is not None
                and target is not None
                and stop is not None
                and target > entry > stop > 0.0
            ):
                _add(violations, "signal_facts_plan")
            if signal_facts.get("can_alert") is not True:
                _add(violations, "signal_facts_can_alert")
            for field in ("alert_gate_status", "source", "source_url"):
                if not _nonblank_text(signal_facts.get(field)):
                    _add(violations, f"signal_facts_{field}")
            if signal_facts.get("no_trade_reason") not in {None, ""}:
                _add(violations, "signal_facts_no_trade_reason")
        if (
            decision.get("cost_model_version")
            != "dawnstrike-alphaops-v6-conservative-cost-v1"
        ):
            _add(violations, "cost_model_version")
        cost_bps = _number(decision.get("estimated_round_trip_cost_bps"))
        if cost_bps is None or cost_bps <= 0.0:
            _add(violations, "estimated_round_trip_cost_bps")
        return violations

    if kind not in {"alpha_paper_selection", "alpha_paper_enter_intent"}:
        return ["unsupported_kind"]
    text_fields = (
        "selection_id",
        "scan_id",
        "signal_id",
        "ticker",
        "strategy_id",
        "strategy_version",
        "cohort",
        "decision",
        "source_artifact_identity",
    )
    for field in text_fields:
        if not _nonblank_text(decision.get(field)):
            _add(violations, field)
    if _canonical_market_date(decision.get("market_date")) is None:
        _add(violations, "market_date")
    if _canonical_utc(decision.get("selected_at")) is None:
        _add(violations, "selected_at")
    for field in (
        "input_hash_sha256",
        "source_lineage_hash_sha256",
        "source_artifact_hash_sha256",
    ):
        if not _valid_sha(decision.get(field)):
            _add(violations, field)
    if decision.get("cohort") != "official_telegram":
        _add(violations, "cohort")
    if not _nonblank_text(decision.get("decision")):
        _add(violations, "decision")
    if decision.get("research_only") is not True:
        _add(violations, "research_only")
    if decision.get("broker_execution_enabled") is not False:
        _add(violations, "broker_execution_enabled")
    delivery = decision.get("delivery_identity")
    if not isinstance(delivery, Mapping) or not (
        delivery.get("channel") == "telegram"
        and delivery.get("delivery_status") == "delivered"
    ):
        _add(violations, "delivery_identity")
    if kind == "alpha_paper_enter_intent":
        if not _nonblank_text(decision.get("intent_id")):
            _add(violations, "intent_id")
        if _canonical_utc(decision.get("decision_at")) is None:
            _add(violations, "decision_at")
        intent_receipt = decision.get("entry_intent_receipt")
        if not _canonical_paper_enter_decision_valid(decision):
            _add(violations, "entry_intent_receipt")
        elif isinstance(intent_receipt, Mapping):
            for field, expected in (
                ("intent_id", decision.get("intent_id")),
                ("selection_id", decision.get("selection_id")),
                ("scan_id", decision.get("scan_id")),
                ("signal_id", decision.get("signal_id")),
                ("ticker", decision.get("ticker")),
                ("market_date", decision.get("market_date")),
                ("decision_time", decision.get("decision_at")),
            ):
                if not _json_equal(intent_receipt.get(field), expected):
                    _add(violations, f"entry_intent_receipt_{field}")
    return violations


def _nonreturn_violations(
    payload: object,
    *,
    not_triggered: bool,
) -> list[str]:
    violations: list[str] = []
    if not isinstance(payload, Mapping):
        return ["return_truth:expected_mapping"]
    receipt = payload.get("path_replay_receipt")
    if not isinstance(receipt, Mapping):
        return ["path_replay_receipt:expected_object"]
    status = receipt.get("path_truth_status")
    if not_triggered:
        if status != PathTruthStatus.NOT_TRIGGERED.value:
            return ["path_truth_status:not_not_triggered"]
        _require_equal(violations, payload, "outcome_status", "not_triggered")
        _require_equal(violations, payload, "activation_status", "NOT_TRIGGERED")
        _require_exact_bool(violations, payload, "learning_eligible", True)
        _require_exact_bool(violations, payload, "activation_label_eligible", True)
    else:
        if status == PathTruthStatus.NOT_TRIGGERED.value:
            return ["path_truth_status:not_censored"]
        if canonical_path_return_eligible(receipt):
            return ["path_replay_receipt:has_return_truth"]
        _require_equal(
            violations,
            payload,
            "outcome_status",
            "captured_ineligible",
        )
        _require_equal(violations, payload, "activation_status", "INELIGIBLE")
        _require_exact_bool(violations, payload, "learning_eligible", False)
        _require_exact_bool(violations, payload, "activation_label_eligible", False)
    _require_exact_bool(
        violations,
        payload,
        "retrospective_research_eligible",
        False,
    )
    _require_exact_bool(
        violations,
        payload,
        "prospective_promotion_eligible",
        False,
    )
    for field in _NA_NULL_FIELDS:
        _require_equal(violations, payload, field, None)
    for field in _NA_STATUS_FIELDS:
        _require_equal(violations, payload, field, "NOT_APPLICABLE")
    for field in (
        "return_truth_schema_version",
        "return_truth_hash_sha256",
        "gross_return_pct",
        "observed_cost_model_identity",
        "modeled_cost_model_identity",
        "cost_components",
    ):
        if field in payload and payload.get(field) is not None:
            _add(violations, f"{field}:unexpected_return_truth")
    identity_hash = _hash_payload(
        {
            "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
            "path_replay_id": receipt.get("path_replay_id"),
            "path_replay_receipt_hash_sha256": receipt.get(
                "replay_receipt_hash_sha256"
            ),
            "causal_decision_identity": payload.get("causal_decision_identity"),
            "replay_binding": payload.get("replay_binding"),
        }
    )
    expected_id = f"outcome-v2-{identity_hash}" if identity_hash is not None else None
    if expected_id is None or payload.get("outcome_id") != expected_id:
        _add(violations, "outcome_id:mismatch")
    return violations


def _require_equal(
    violations: list[str],
    payload: Mapping[str, object],
    field: str,
    expected: object,
) -> None:
    if field not in payload:
        _add(violations, f"{field}:missing")
    elif not _json_equal(payload[field], expected):
        _add(violations, f"{field}:mismatch")


def _require_exact_bool(
    violations: list[str],
    payload: Mapping[str, object],
    field: str,
    expected: bool,
) -> None:
    if field not in payload or type(payload[field]) is not bool:
        _add(violations, f"{field}:expected_bool")
    elif payload[field] is not expected:
        _add(violations, f"{field}:mismatch")


def _require_number_copy(
    violations: list[str],
    payload: Mapping[str, object],
    field: str,
    expected: object,
) -> None:
    if field not in payload or not _close(payload[field], expected):
        _add(violations, f"{field}:mismatch")


def _canonical_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or value != value.strip() or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        canonical = parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None
    if parsed.utcoffset() != timezone.utc.utcoffset(None):
        return None
    return canonical if canonical.isoformat() == value else None


def _require_current_paper_selection(selection: Mapping[str, object]) -> None:
    violations = _decision_context_contract_violations(
        selection,
        kind="alpha_paper_selection",
    )
    if violations:
        raise ValueError(
            "paper selection context is invalid: " + ", ".join(violations)
        )


def _raw_record_parts(
    record: Mapping[str, object],
    *,
    label: str,
) -> tuple[Mapping[str, object], Mapping[str, object]]:
    if set(record) != {"columns", "payload_json"}:
        raise ValueError(f"{label} record has invalid outer keys")
    columns = record.get("columns")
    payload = record.get("payload_json")
    if not isinstance(columns, Mapping) or not isinstance(payload, Mapping):
        raise ValueError(f"{label} record is not a raw column/payload pair")
    return columns, payload


def _validate_intent_column_projection(
    columns: Mapping[str, object],
    payload: Mapping[str, object],
) -> None:
    column_keys = {
        "intent_id",
        "signal_id",
        "market_date",
        "ticker",
        "episode_id",
        "strategy_id",
        "account_id",
        "mode",
        "lifecycle_state",
        "action",
        "decision_time",
        "decision_price",
        "trigger_price",
        "stop_price",
        "target_price",
        "quantity",
        "notional",
        "risk_amount",
        "reason",
        "blocked_reason",
        "source_observation_id",
        "notification_event_key",
        "created_at",
    }
    payload_keys = column_keys | {
        "source_bar_hash_sha256",
        "source_observed_at",
        "source_bar_completed_at",
        "selection_id",
        "episode_id",
        "matched_strategy_ids",
        "primary_strategy_id",
        "episode_dedup_counts",
        "strategy_id",
        "strategy_version",
        "cohort",
        "account_id",
        "execution_policy_version",
        "cost_model_version",
        "decision_fingerprint",
        "official_paper_eligible",
        "decision_trace",
        "direction",
    }
    if set(columns) != column_keys or set(payload) != payload_keys:
        raise ValueError("trade intent raw record has an unexpected schema")
    for field in column_keys:
        if not _json_equal(columns[field], payload[field]):
            raise ValueError(f"trade intent {field} conflicts with its column")


def _canonical_source_observation_receipt(
    columns: Mapping[str, object],
    payload: Mapping[str, object],
    *,
    selection: Mapping[str, object],
) -> dict[str, object]:
    column_keys = {
        "observation_id",
        "signal_id",
        "market_date",
        "ticker",
        "requested_at",
        "observed_at",
        "price",
        "price_type",
        "source",
        "source_kind",
        "provider",
        "provider_status",
        "freshness_seconds",
        "tolerance_seconds",
        "is_usable",
        "created_at",
    }
    payload_keys = {
        "bar",
        "bar_completed_at",
        "is_complete",
        "source_bar_hash_sha256",
        "no_lookahead",
        "price_rule",
        "quote",
        "quote_ask",
        "quote_bid",
        "quote_freshness_seconds",
        "quote_observed_at",
        "quote_raw_payload_json",
        "quote_source",
        "quote_source_hash_sha256",
        "quote_status",
    }
    if set(columns) != column_keys or set(payload) != payload_keys:
        raise ValueError("price observation raw record has an unexpected schema")
    ticker = selection.get("ticker")
    market_date = selection.get("market_date")
    if not (
        columns.get("ticker") == ticker
        and columns.get("market_date") == market_date
        and columns.get("signal_id") in {"", selection.get("signal_id")}
    ):
        raise ValueError("price observation subject conflicts with selection")
    requested_at = _canonical_utc(columns.get("requested_at"))
    observed_at = _canonical_utc(columns.get("observed_at"))
    completed_at = _canonical_utc(payload.get("bar_completed_at"))
    created_at = _canonical_utc(columns.get("created_at"))
    if None in {requested_at, observed_at, completed_at, created_at}:
        raise ValueError("price observation timestamps are not canonical UTC")
    assert requested_at is not None
    assert observed_at is not None
    assert completed_at is not None
    if not (observed_at < completed_at <= requested_at):
        raise ValueError("price observation chronology is invalid")
    bar = payload.get("bar")
    if not isinstance(bar, Mapping):
        raise ValueError("price observation lacks its source bar")
    raw_hash = _hash_payload(bar)
    if raw_hash is None or payload.get("source_bar_hash_sha256") != raw_hash:
        raise ValueError("price observation source bar hash is invalid")
    bar_at = _aware_utc(bar.get("timestamp") or bar.get("t"))
    if bar_at != observed_at or completed_at != observed_at + timedelta(minutes=1):
        raise ValueError("price observation bar time is inconsistent")
    bar_ticker = str(bar.get("ticker") or bar.get("symbol") or "").upper()
    if bar_ticker != ticker:
        raise ValueError("price observation bar ticker is inconsistent")
    open_price = _raw_number(bar.get("open") if "open" in bar else bar.get("o"))
    high = _raw_number(bar.get("high") if "high" in bar else bar.get("h"))
    low = _raw_number(bar.get("low") if "low" in bar else bar.get("l"))
    close = _raw_number(bar.get("close") if "close" in bar else bar.get("c"))
    price = _number(columns.get("price"))
    if not (
        open_price is not None
        and high is not None
        and low is not None
        and close is not None
        and min(open_price, high, low, close) > 0.0
        and high >= max(open_price, close)
        and low <= min(open_price, close)
        and price is not None
        and math.isclose(price, close, rel_tol=0.0, abs_tol=1e-12)
    ):
        raise ValueError("price observation OHLC/price truth is invalid")
    freshness = columns.get("freshness_seconds")
    tolerance = columns.get("tolerance_seconds")
    expected_freshness = int((requested_at - completed_at).total_seconds())
    if not (
        type(freshness) is int
        and freshness == expected_freshness
        and type(tolerance) is int
        and tolerance > 0
        and 0 <= freshness <= tolerance
        and columns.get("is_usable") == 1
        and payload.get("is_complete") is True
        and payload.get("no_lookahead") is True
        and columns.get("price_type") == "last_bar_close_at_or_before"
        and payload.get("price_rule")
        == "latest minute bar with completion <= requested_at"
    ):
        raise ValueError("price observation completeness contract is invalid")
    source = columns.get("source")
    expected_provider = {
        "csv": ("local_minute_bars", "csv_minute_bars"),
        "yahoo": ("public_web_market_data", "yahoo_finance_chart"),
        "alpaca": ("market_data_api", "alpaca_market_data"),
    }.get(source if isinstance(source, str) else "")
    if expected_provider is None or (
        columns.get("source_kind"), columns.get("provider")
    ) != expected_provider:
        raise ValueError("price observation provider identity is invalid")
    expected_status = "exact" if freshness == 0 else "fresh_prior_bar"
    if columns.get("provider_status") != expected_status:
        raise ValueError("price observation provider status is inconsistent")
    quote_raw_json = payload.get("quote_raw_payload_json")
    try:
        quote_raw = json.loads(quote_raw_json) if isinstance(quote_raw_json, str) else None
        canonical_quote_raw = json.dumps(
            quote_raw, sort_keys=True, separators=(",", ":"), ensure_ascii=True
        )
    except (TypeError, ValueError, json.JSONDecodeError):
        quote_raw = None
        canonical_quote_raw = ""
    raw_quote = quote_raw.get("quote") if isinstance(quote_raw, Mapping) else None
    quote_observed_at = _canonical_utc(payload.get("quote_observed_at"))
    quote_bid = _number(payload.get("quote_bid"))
    quote_ask = _number(payload.get("quote_ask"))
    quote_freshness = _number(payload.get("quote_freshness_seconds"))
    if not (
        isinstance(quote_raw, Mapping)
        and isinstance(raw_quote, Mapping)
        and isinstance(quote_raw_json, str)
        and quote_raw_json == canonical_quote_raw
        and _json_equal(payload.get("quote"), quote_raw)
        and str(quote_raw.get("ticker") or "").upper() == ticker
        and quote_bid is not None
        and quote_ask is not None
        and quote_bid > 0
        and quote_ask >= quote_bid
        and _number(raw_quote.get("bp")) == quote_bid
        and _number(raw_quote.get("ap")) == quote_ask
        and quote_observed_at is not None
        and _canonical_utc(raw_quote.get("t")) == quote_observed_at
        and quote_observed_at <= requested_at
        and quote_freshness is not None
        and quote_freshness
        == (requested_at - quote_observed_at).total_seconds()
        and 0 <= quote_freshness <= tolerance
        and payload.get("quote_status") == "USABLE"
        and str(payload.get("quote_source") or "").startswith("alpaca_market_data_")
        and _secure_equal(
            payload.get("quote_source_hash_sha256"), _hash_payload(quote_raw)
        )
    ):
        raise ValueError("price observation quote truth is invalid")
    observation_id = columns.get("observation_id")
    identity_head = columns.get("signal_id") or ticker
    expected_id = re.sub(
        r"[^A-Za-z0-9_.:-]+",
        "_",
        f"{identity_head}:{ticker}:{source}:{columns.get('requested_at')}",
    )
    if observation_id != expected_id:
        raise ValueError("price observation identity is invalid")
    body: dict[str, object] = {
        "schema_version": "dawnstrike.alphaops.price_observation.v1",
        "columns": copy.deepcopy(dict(columns)),
        "payload_json": copy.deepcopy(dict(payload)),
    }
    digest = _hash_payload(body)
    if digest is None:
        raise ValueError("price observation receipt is not canonical JSON")
    return {
        **body,
        "receipt_id": f"price-observation-v1-{digest}",
        "receipt_hash_sha256": digest,
    }


def _validate_current_v5_entry_intent(
    columns: Mapping[str, object],
    payload: Mapping[str, object],
    *,
    selection: Mapping[str, object],
    source_receipt: Mapping[str, object],
) -> None:
    source_columns = source_receipt.get("columns")
    source_payload = source_receipt.get("payload_json")
    if not isinstance(source_columns, Mapping) or not isinstance(
        source_payload,
        Mapping,
    ):
        raise ValueError("source observation receipt is malformed")
    exact_expected = {
        "selection_id": selection.get("selection_id"),
        "signal_id": selection.get("signal_id"),
        "ticker": selection.get("ticker"),
        "market_date": selection.get("market_date"),
        "strategy_id": ALPHAOPS_V5_STRATEGY_ID,
        "strategy_version": ALPHAOPS_V5_STRATEGY_VERSION,
        "cohort": "official_telegram",
        "mode": "paper_execute",
        "lifecycle_state": "ENTRY_TRIGGERED",
        "action": "ENTER_LONG",
        "direction": "long",
        "account_id": ALPHAOPS_V5_ACCOUNT_ID,
        "execution_policy_version": ALPHAOPS_V5_POLICY_VERSION,
        "cost_model_version": ALPHAOPS_V5_COST_MODEL_VERSION,
        "official_paper_eligible": True,
        "source_observation_id": source_columns.get("observation_id"),
        "source_bar_hash_sha256": source_payload.get(
            "source_bar_hash_sha256"
        ),
        "source_observed_at": source_payload.get("quote_observed_at"),
        "source_bar_completed_at": source_payload.get("bar_completed_at"),
    }
    for field, expected in exact_expected.items():
        if not _json_equal(payload.get(field), expected):
            raise ValueError(f"paper entry intent {field} is inconsistent")
    decision_time = _canonical_utc(payload.get("decision_time"))
    requested_at = _canonical_utc(source_columns.get("requested_at"))
    if (
        decision_time is None
        or decision_time != requested_at
        or _canonical_utc(payload.get("created_at")) is None
    ):
        raise ValueError("paper entry intent decision time is not causal")
    trace = payload.get("decision_trace")
    if not isinstance(trace, Mapping):
        raise ValueError("paper entry intent lacks its V5 trace")
    authoritative_signal = selection.get("authoritative_signal")
    if not isinstance(authoritative_signal, Mapping):
        raise ValueError("paper selection lacks authoritative signal facts")
    evaluation_signal = {
        **copy.deepcopy(dict(authoritative_signal)),
        "selection_id": selection["selection_id"],
        "strategy_id": selection["strategy_id"],
        "strategy_version": selection["strategy_version"],
        "cohort": selection["cohort"],
        "decision": selection["decision"],
        "selected_at": selection["selected_at"],
    }
    expected_episode = build_episode_identity(evaluation_signal)
    if payload.get("episode_id") != expected_episode.episode_id:
        raise ValueError("paper entry intent episode identity is inconsistent")
    matched_strategy_ids = payload.get("matched_strategy_ids")
    primary_strategy_id = payload.get("primary_strategy_id")
    if not (
        isinstance(matched_strategy_ids, list)
        and matched_strategy_ids
        and all(
            isinstance(item, str) and item == item.strip() and item
            for item in matched_strategy_ids
        )
        and matched_strategy_ids == sorted(set(matched_strategy_ids))
        and ALPHAOPS_V5_STRATEGY_ID in matched_strategy_ids
        and primary_strategy_id == ALPHAOPS_V5_STRATEGY_ID
    ):
        raise ValueError("paper entry intent strategy episode metadata is invalid")
    _validate_episode_dedup_counts(payload.get("episode_dedup_counts"))
    observation = {
        **copy.deepcopy(dict(source_columns)),
        **copy.deepcopy(dict(source_payload)),
        "bar_observed_at": source_columns["observed_at"],
        "observed_at": source_payload["quote_observed_at"],
        "price": source_payload["quote_ask"],
        "current_price": source_payload["quote_ask"],
        "bar_completed_at": source_payload["bar_completed_at"],
        "source_bar_hash_sha256": source_payload["source_bar_hash_sha256"],
    }
    sizing = trace.get("sizing")
    if not isinstance(sizing, Mapping):
        raise ValueError("paper entry intent lacks V5 sizing truth")
    simulated_equity = _number(sizing.get("simulated_equity"))
    existing_notional = _number(sizing.get("existing_symbol_notional"))
    if simulated_equity is None or existing_notional is None:
        raise ValueError("paper entry intent sizing inputs are invalid")
    expected_trace = evaluate_v5_official_paper(
        evaluation_signal,
        observation,
        simulated_equity=sizing["simulated_equity"],
        existing_symbol_notional=sizing["existing_symbol_notional"],
        decision_time=str(payload["decision_time"]),
        policy=DEFAULT_V5_POLICY,
    ).to_dict()
    if not _json_equal(trace, expected_trace):
        raise ValueError("paper entry intent V5 trace cannot be reproduced")
    computed = expected_trace["computed"]
    expected_fields = {
        "decision_price": computed["entry_price_observed"],
        "trigger_price": computed["trigger_price"],
        "stop_price": computed["stop_price"],
        "target_price": computed["target_price"],
        "quantity": float(expected_trace["sizing"]["shares"]),
        "notional": expected_trace["sizing"]["proposed_notional"],
        "risk_amount": expected_trace["sizing"]["proposed_risk"],
        "decision_fingerprint": expected_trace["decision_fingerprint"],
    }
    for field, expected in expected_fields.items():
        if not _json_equal(payload.get(field), expected):
            raise ValueError(f"paper entry intent {field} is not reproducible")
    intent_basis = (
        f"paper_execute:{selection['market_date']}:{expected_episode.episode_id}:"
        f"{selection['ticker']}:ENTER_LONG:{payload['decision_time']}:"
        f"{payload['decision_price']}"
    )
    expected_intent_id = "ti_" + hashlib.sha256(intent_basis.encode()).hexdigest()[:24]
    if payload.get("intent_id") != expected_intent_id:
        raise ValueError("paper entry intent ID is not canonical")
    if payload.get("notification_event_key") != f"trade_intent:{expected_intent_id}":
        raise ValueError("paper entry notification identity is inconsistent")
    after_cost_r = float(computed["actual_after_cost_reward_risk"])
    shares = int(expected_trace["sizing"]["shares"])
    expected_reason = (
        f"AlphaOps v5 official-paper candidate passed at {after_cost_r:.2f}R "
        f"after modeled costs; {shares} shares risk-sized from simulated equity."
    )
    if payload.get("reason") != expected_reason or payload.get("blocked_reason") != "":
        raise ValueError("paper entry intent reason is not canonical")


def _validate_episode_dedup_counts(value: object) -> None:
    if not isinstance(value, Mapping):
        raise ValueError("paper entry intent episode de-dup counts are missing")
    count_fields = {
        "raw_pair_count",
        "unique_symbol_count",
        "unique_episode_count",
        "unique_reservation_count",
        "duplicate_collapse_count",
        "overlapping_reservation_collapse_count",
        "conflicting_direction_episode_count",
        "blocked_count",
    }
    if set(value) != {*count_fields, "status"}:
        raise ValueError("paper entry intent episode de-dup schema is invalid")
    if value.get("status") != "FROZEN_IDENTITY_ACTIVE":
        raise ValueError("paper entry intent episode de-dup is not frozen")
    if any(type(value.get(field)) is not int or int(value[field]) < 0 for field in count_fields):
        raise ValueError("paper entry intent episode de-dup counts are invalid")
    raw_pairs = int(value["raw_pair_count"])
    unique_symbols = int(value["unique_symbol_count"])
    unique_episodes = int(value["unique_episode_count"])
    unique_reservations = int(value["unique_reservation_count"])
    duplicate_collapses = int(value["duplicate_collapse_count"])
    overlap_collapses = int(value["overlapping_reservation_collapse_count"])
    conflicts = int(value["conflicting_direction_episode_count"])
    blocked = int(value["blocked_count"])
    if not (
        raw_pairs >= 1
        and 1 <= unique_symbols <= unique_episodes <= raw_pairs
        and 1 <= unique_reservations <= unique_episodes
        and blocked == 0
        and conflicts == 0
        and raw_pairs == unique_episodes + duplicate_collapses
        and unique_episodes == unique_reservations + overlap_collapses
    ):
        raise ValueError("paper entry intent episode de-dup counts conflict")


def _rendered_official_candidate_tickers(body: object) -> list[str] | None:
    """Parse numbered tickers from the rendered official-candidate section."""

    if not isinstance(body, str):
        return None
    in_official_section = False
    found_official_section = False
    tickers: list[str] = []
    for line in body.splitlines():
        normalized = line.strip().upper()
        if normalized in {"OFFICIAL PAPER CANDIDATES", "PAPER PLAN QUALIFIED"}:
            in_official_section = True
            found_official_section = True
            continue
        if in_official_section and normalized.startswith("RESEARCH WATCHLIST"):
            break
        if not in_official_section:
            continue
        match = re.match(
            r"^\s*\d+\)\s*([A-Za-z][A-Za-z0-9._-]*)(?:\s+[—-]|\s*$)",
            line,
        )
        if match:
            tickers.append(match.group(1).upper())
    return tickers if found_official_section else None


def _canonical_paper_enter_intent_receipt_valid(value: object) -> bool:
    if not isinstance(value, Mapping) or set(value) != _PAPER_ENTER_INTENT_RECEIPT_KEYS:
        return False
    body = {key: value[key] for key in _PAPER_ENTER_INTENT_BODY_KEYS}
    digest = _hash_payload(body)
    return bool(
        digest is not None
        and value.get("schema_version")
        == PAPER_ENTER_INTENT_RECEIPT_SCHEMA_VERSION
        and value.get("receipt_id")
        == f"{PAPER_ENTER_INTENT_RECEIPT_ID_PREFIX}{digest}"
        and _secure_equal(value.get("receipt_hash_sha256"), digest)
    )


def _canonical_paper_enter_decision_valid(
    decision: Mapping[str, object],
) -> bool:
    receipt = decision.get("entry_intent_receipt")
    if not _canonical_paper_enter_intent_receipt_valid(receipt):
        return False
    assert isinstance(receipt, Mapping)
    raw_intent = receipt.get("raw_intent_record")
    source_receipt = receipt.get("source_observation_receipt")
    if not isinstance(raw_intent, Mapping) or not isinstance(
        source_receipt,
        Mapping,
    ):
        return False
    try:
        columns, payload = _raw_record_parts(raw_intent, label="trade intent")
        _validate_intent_column_projection(columns, payload)
        source_columns = source_receipt.get("columns")
        source_payload = source_receipt.get("payload_json")
        if not isinstance(source_columns, Mapping) or not isinstance(
            source_payload,
            Mapping,
        ):
            return False
        expected_source = _canonical_source_observation_receipt(
            source_columns,
            source_payload,
            selection=decision,
        )
        if not _json_equal(source_receipt, expected_source):
            return False
        _validate_current_v5_entry_intent(
            columns,
            payload,
            selection=decision,
            source_receipt=expected_source,
        )
        for field in (
            _PAPER_ENTER_INTENT_BODY_KEYS
            - {"schema_version", "source_observation_receipt", "raw_intent_record"}
        ):
            if field == "scan_id":
                expected = decision.get("scan_id")
            else:
                expected = payload.get(field)
            if not _json_equal(receipt.get(field), expected):
                return False
        return True
    except (KeyError, TypeError, ValueError):
        return False


def _aware_utc(value: object) -> datetime | None:
    if not isinstance(value, str) or value != value.strip() or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(timezone.utc)
    except (OverflowError, ValueError):
        return None


def _raw_number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        if type(value) is int:
            number = float(value)
        elif type(value) is float:
            number = value
        elif isinstance(value, str):
            number = float(value)
        else:
            return None
    except (OverflowError, TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _canonical_market_date(value: object) -> date | None:
    if not isinstance(value, str) or value != value.strip() or not value:
        return None
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.isoformat() == value else None


def _number(value: object) -> float | None:
    if type(value) is int:
        try:
            number = float(value)
        except OverflowError:
            return None
        return number if math.isfinite(number) else None
    if type(value) is float and math.isfinite(value):
        return value
    return None


def _finite_number(value: object) -> bool:
    return _number(value) is not None


def _close(actual: object, expected: object) -> bool:
    actual_number = _number(actual)
    expected_number = _number(expected)
    return bool(
        actual_number is not None
        and expected_number is not None
        and math.isclose(
            actual_number,
            expected_number,
            rel_tol=0.0,
            abs_tol=1e-9,
        )
    )


def _nonblank_text(value: object) -> bool:
    return isinstance(value, str) and value == value.strip() and bool(value)


def _valid_sha(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


def _hash_payload(payload: object) -> str | None:
    encoded = _canonical_json(payload)
    return hashlib.sha256(encoded.encode()).hexdigest() if encoded is not None else None


def _canonical_json(payload: object) -> str | None:
    try:
        return json.dumps(
            payload,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (RecursionError, TypeError, ValueError):
        return None


def _json_equal(left: object, right: object) -> bool:
    left_json = _canonical_json(left)
    right_json = _canonical_json(right)
    return left_json is not None and right_json is not None and left_json == right_json


def _secure_equal(actual: object, expected: str) -> bool:
    return isinstance(actual, str) and hmac.compare_digest(actual, expected)


def _add(violations: list[str], violation: str) -> None:
    if violation not in violations:
        violations.append(violation)


__all__ = [
    "COST_TRUTH_SCHEMA_VERSION",
    "build_canonical_return_truth",
    "build_canonical_path_entry_receipt",
    "canonical_paper_enter_intent_context",
    "canonical_paper_selection_context",
    "canonical_replay_binding",
    "CURRENT_ACTIVATION_ONLY_NOT_TRIGGERED",
    "CURRENT_CENSORED_PATH",
    "CURRENT_RETURN_TRUTH",
    "LEGACY_OR_INCOMPLETE",
    "RECONCILIATION_SCHEMA_VERSION",
    "RETURN_TRUTH_SCHEMA_VERSION",
    "TERMINAL_MISSING",
    "canonical_return_truth_projection",
    "canonical_return_truth_valid",
    "canonical_return_truth_violations",
    "classify_canonical_return_truth",
]
