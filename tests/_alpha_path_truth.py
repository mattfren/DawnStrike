from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import date, datetime, time, timedelta, timezone
from typing import Any

from intraday_scanner.alpha.path_replay import (
    ELIGIBILITY_POLICY_VERSION,
    ENTRY_MODE_ALREADY_ENTERED,
    ENTRY_RECEIPT_ID_PREFIX,
    ENTRY_RECEIPT_SCHEMA_VERSION,
    PathReplayResult,
    canonical_path_contract_valid,
    canonical_path_return_eligible,
    resolve_path,
)

UTC = timezone.utc
SOURCE_HASH = "a" * 64
PRIMARY_BENCHMARK_HASH = "b" * 64
SECONDARY_BENCHMARK_HASH = "c" * 64
DECISION_SOURCE_HASH = "4" * 64
RECONCILIATION_HASH = "d" * 64
COST_RECEIPT_HASH = "e" * 64
RETURN_TRUTH_SCHEMA_VERSION = "dawnstrike.alphaops.return_truth.v2"
COST_TRUTH_SCHEMA_VERSION = "dawnstrike.alphaops.cost_truth.v2"
RECONCILIATION_SCHEMA_VERSION = "dawnstrike.alphaops.reconciliation.v2"
REPLAY_BINDING_SCHEMA_VERSION = "dawnstrike.path_replay_binding.v1"
FUTURE_EVIDENCE_SCHEMA_VERSION = "dawnstrike.future_evidence_receipt.v1"
LABEL_SCHEMA_VERSION = "dawnstrike-alphaops-v6-label-schema-v2"
DATASET_SCHEMA_VERSION = "dawnstrike-alphaops-v6-dataset-v2"


def _stable_json(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def _sha(payload: object) -> str:
    return hashlib.sha256(_stable_json(payload).encode()).hexdigest()


def _ordered_hash(events: list[dict[str, object]]) -> str:
    canonical = [
        {
            key: (
                value.astimezone(UTC).isoformat()
                if isinstance(value, datetime)
                else str(value).upper()
                if key == "event_type"
                else value
            )
            for key, value in sorted(event.items())
        }
        for event in events
    ]
    canonical.sort(
        key=lambda row: (str(row.get("observed_at")), _stable_json(row))
    )
    return hashlib.sha256(_stable_json(canonical).encode()).hexdigest()


def _start(market_date: str) -> datetime:
    return datetime.combine(date.fromisoformat(market_date), time(14, 30), UTC)


def _bar(
    start: datetime,
    minute: int,
    *,
    open: float,
    high: float,
    low: float,
    close: float,
) -> dict[str, object]:
    return {
        "observed_at": start + timedelta(minutes=minute),
        "open": open,
        "high": high,
        "low": low,
        "close": close,
    }


def _future_evidence_receipt(
    bars: list[dict[str, object]],
    *,
    symbol: str,
    market_date: str,
    raw_artifact_identity: str,
) -> dict[str, object]:
    canonical_bars = [
        {
            "observed_at": row["observed_at"].astimezone(UTC).isoformat(),
            "open": float(row["open"]),
            "high": float(row["high"]),
            "low": float(row["low"]),
            "close": float(row["close"]),
        }
        for row in sorted(bars, key=lambda row: row["observed_at"])
    ]
    first_bar_at = str(canonical_bars[0]["observed_at"])
    last_bar_at = str(canonical_bars[-1]["observed_at"])
    body: dict[str, object] = {
        "schema_version": FUTURE_EVIDENCE_SCHEMA_VERSION,
        "subject": {"symbol": symbol, "market_date": market_date},
        "raw_artifact_identity": raw_artifact_identity,
        "raw_bar_hash_sha256": _sha(canonical_bars),
        "bar_count": len(canonical_bars),
        "first_bar_at": first_bar_at,
        "last_bar_at": last_bar_at,
        "coverage_start": first_bar_at,
        "coverage_end": (
            datetime.fromisoformat(last_bar_at) + timedelta(minutes=1)
        ).isoformat(),
        "coverage_complete": True,
    }
    digest = _sha(body)
    return {
        **body,
        "receipt_id": f"future-evidence-v1-{digest}",
        "receipt_hash_sha256": digest,
    }


def _default_replay_binding(*, symbol: str, market_date: str) -> dict[str, object]:
    return {
        "schema_version": REPLAY_BINDING_SCHEMA_VERSION,
        "subject": {"symbol": symbol, "market_date": market_date},
        "origin": {
            "kind": "alpha_paper_selection",
            "id": "fixture-selection",
            "lineage": {
                "selection_id": "fixture-selection",
                "scan_id": "fixture-scan",
                "signal_id": "fixture-signal",
            },
            "context_hash_sha256": "f" * 64,
        },
    }


def canonical_path_result(
    *,
    market_date: str = "2026-08-03",
    case: str = "ordered_target",
    decision_at: datetime | None = None,
    source_artifact_identity: str | None = None,
    source_artifact_hash_sha256: str | None = None,
    replay_binding: Mapping[str, object] | None = None,
    future_evidence_receipt: Mapping[str, object] | None = None,
    authenticated_entry: bool = False,
) -> PathReplayResult:
    """Build a real v2 receipt through the production resolver."""

    start = decision_at or _start(market_date)
    bars: list[dict[str, object]]
    halts: list[tuple[datetime, datetime]] = []
    events: list[dict[str, object]] = []
    ordered = False
    source_conflict = False
    corporate_action = False
    close = start + timedelta(minutes=2)

    if case == "ordered_target":
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 1, open=10.6, high=11.1, low=10.4, close=11.0),
        ]
        events = [
            {"observed_at": start, "event_type": "TRADE", "price": 10.6},
            {
                "observed_at": start + timedelta(seconds=30),
                "event_type": "TRADE",
                "price": 10.4,
            },
            {
                "observed_at": start + timedelta(minutes=1, seconds=15),
                "event_type": "TRADE",
                "price": 11.0,
            },
        ]
        ordered = True
    elif case == "ordered_stop":
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 1, open=10.6, high=10.7, low=8.9, close=9.0),
        ]
        events = [
            {"observed_at": start, "event_type": "TRADE", "price": 10.6},
            {
                "observed_at": start + timedelta(seconds=30),
                "event_type": "TRADE",
                "price": 10.4,
            },
            {
                "observed_at": start + timedelta(minutes=1, seconds=15),
                "event_type": "TRADE",
                "price": 8.9,
            },
        ]
        ordered = True
    elif case == "timeout":
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 1, open=10.6, high=10.9, low=10.3, close=10.7),
        ]
    elif case == "not_triggered":
        bars = [
            _bar(start, 0, open=10.0, high=10.3, low=9.8, close=10.1),
            _bar(start, 1, open=10.1, high=10.4, low=9.9, close=10.2),
        ]
    elif case == "entry_censored":
        bars = [
            _bar(start, 0, open=10.0, high=11.2, low=8.8, close=10.5),
            _bar(start, 1, open=10.5, high=11.1, low=10.2, close=11.0),
        ]
    elif case == "same_censored":
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 1, open=10.6, high=11.2, low=8.8, close=9.5),
        ]
    elif case == "missing_interval":
        close = start + timedelta(minutes=3)
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 2, open=10.6, high=11.1, low=10.4, close=11.0),
        ]
    elif case == "halt":
        close = start + timedelta(minutes=3)
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 2, open=10.6, high=11.1, low=10.4, close=11.0),
        ]
        halts = [(start + timedelta(minutes=1), start + timedelta(minutes=2))]
    elif case == "source_conflict":
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 1, open=10.6, high=11.1, low=10.4, close=11.0),
        ]
        source_conflict = True
    elif case == "corporate_action":
        bars = [
            _bar(start, 0, open=10.6, high=10.8, low=10.2, close=10.6),
            _bar(start, 1, open=10.6, high=11.1, low=10.4, close=11.0),
        ]
        corporate_action = True
    else:
        raise ValueError(f"unsupported canonical path case: {case}")

    ordered_kwargs: dict[str, object] = {}
    if ordered:
        ordered_kwargs = {
            "ordered_events": events,
            "ordered_evidence_complete": True,
            "ordered_evidence_identity": f"trades:NOVA:{market_date}",
            "ordered_evidence_hash_sha256": _ordered_hash(events),
            "ordered_evidence_start": start,
            "ordered_evidence_end": close,
        }
    evidence = dict(
        future_evidence_receipt
        or _future_evidence_receipt(
            bars,
            symbol="NOVA",
            market_date=market_date,
            raw_artifact_identity=(
                source_artifact_identity or f"bars:NOVA:{market_date}"
            ),
        )
    )
    bound_replay = dict(
        replay_binding
        or _default_replay_binding(symbol="NOVA", market_date=market_date)
    )
    entry_receipt = (
        _authenticated_path_entry_receipt(
            replay_binding=bound_replay,
            effective_at=start,
        )
        if authenticated_entry
        else None
    )
    result = resolve_path(
        bars,
        decision_at=start,
        trigger=10.5,
        target=11.0,
        stop=9.0,
        halt_intervals=halts,
        session_close=close,
        source_conflict=source_conflict,
        corporate_action_unresolved=corporate_action,
        source_artifact_identity=evidence["receipt_id"],
        # Current path truth binds the source hash to the authenticated future-
        # evidence receipt.  ``source_artifact_hash_sha256`` is retained as a
        # fixture-call compatibility input, but it must never replace that
        # authenticated digest.
        source_artifact_hash_sha256=evidence["receipt_hash_sha256"],
        source_coverage_complete=True,
        replay_binding=bound_replay,
        future_evidence_receipt=evidence,
        entry_mode=(ENTRY_MODE_ALREADY_ENTERED if entry_receipt is not None else None),
        entry_receipt=entry_receipt,
        **ordered_kwargs,
    )
    receipt = result.to_dict()
    assert canonical_path_contract_valid(receipt)
    if case in {"ordered_target", "ordered_stop", "timeout"}:
        assert canonical_path_return_eligible(receipt)
    else:
        assert not canonical_path_return_eligible(receipt)
    return result


def canonical_path_receipt(
    *,
    market_date: str = "2026-08-03",
    case: str = "ordered_target",
    decision_at: datetime | None = None,
    source_artifact_identity: str | None = None,
    source_artifact_hash_sha256: str | None = None,
    replay_binding: Mapping[str, object] | None = None,
    future_evidence_receipt: Mapping[str, object] | None = None,
    authenticated_entry: bool = False,
) -> dict[str, Any]:
    return canonical_path_result(
        market_date=market_date,
        case=case,
        decision_at=decision_at,
        source_artifact_identity=source_artifact_identity,
        source_artifact_hash_sha256=source_artifact_hash_sha256,
        replay_binding=replay_binding,
        future_evidence_receipt=future_evidence_receipt,
        authenticated_entry=authenticated_entry,
    ).to_dict()


def canonical_return_outcome(
    *,
    market_date: str = "2026-08-03",
    case: str = "ordered_target",
    prospective: bool = True,
    causal_identity: Mapping[str, object],
    net_excess_return_pct: float | None = None,
    source_artifact_identity: str | None = None,
    source_artifact_hash_sha256: str | None = None,
    symbol: str = "NOVA",
    replay_binding: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    canonical_causal_identity = dict(causal_identity)
    bound_replay = dict(
        replay_binding
        or _fixture_replay_binding_from_causal(
            canonical_causal_identity,
            symbol=symbol,
            market_date=market_date,
        )
    )
    receipt = canonical_path_receipt(
        market_date=market_date,
        case=case,
        source_artifact_identity=source_artifact_identity,
        source_artifact_hash_sha256=source_artifact_hash_sha256,
        replay_binding=bound_replay,
        authenticated_entry=True,
    )
    entry = receipt.get("entry_price")
    exit_price = receipt.get("exit_price")
    return_eligible = canonical_path_return_eligible(receipt)
    if not return_eligible:
        raise ValueError(f"case {case!r} does not have canonical return truth")
    gross = (
        ((float(exit_price) / float(entry)) - 1.0) * 100.0
        if return_eligible and entry is not None and exit_price is not None
        else None
    )
    fee_bps = 1.0
    commission_per_share = 0.0
    entry_slippage_bps = 50.0
    exit_slippage_bps = 50.0
    after_cost: float | None = None
    if gross is not None:
        entry_fill = float(entry) * (1.0 + entry_slippage_bps / 10_000.0)
        exit_fill = float(exit_price) * (1.0 - exit_slippage_bps / 10_000.0)
        quantity = 1_000.0 / entry_fill
        entry_fee = (
            entry_fill * quantity * fee_bps / 10_000.0
            + quantity * commission_per_share
        )
        exit_fee = (
            exit_fill * quantity * fee_bps / 10_000.0
            + quantity * commission_per_share
        )
        after_cost = (
            (((exit_fill - entry_fill) * quantity) - entry_fee - exit_fee)
            / 1_000.0
            * 100.0
        )
    primary_benchmark = (
        after_cost - net_excess_return_pct
        if after_cost is not None and net_excess_return_pct is not None
        else 0.5
    )
    secondary_benchmark = 0.25
    source_bar_count = len(receipt["replay_input_manifest"]["bars"])
    authenticated_binding = receipt["replay_input_manifest"]["replay_binding"]
    mfe = receipt.get("mfe_price")
    mae = receipt.get("mae_price")
    mfe_pct = (
        ((float(mfe) / float(entry)) - 1.0) * 100.0
        if mfe is not None and entry is not None
        else None
    )
    mae_pct = (
        ((float(mae) / float(entry)) - 1.0) * 100.0
        if mae is not None and entry is not None
        else None
    )
    cost_components = {
        "notional_per_trade": 1_000.0,
        "entry_slippage_bps": entry_slippage_bps,
        "exit_slippage_bps": exit_slippage_bps,
        "fee_bps_per_side": fee_bps,
        "commission_per_share_per_side": commission_per_share,
    }
    cost_body = {
        "schema_version": COST_TRUTH_SCHEMA_VERSION,
        "path_replay_id": receipt["path_replay_id"],
        "raw_entry_price": entry,
        "raw_exit_price": exit_price,
        "gross_return_pct": gross,
        "after_cost_return_pct": after_cost,
        "observed_cost_model_identity": "observed-cost-v2",
        "modeled_cost_model_identity": "modeled-cost-v2",
        "components": cost_components,
    }
    cost_hash = _sha(cost_body)
    cost_receipt = {
        **cost_body,
        "receipt_id": f"cost-v2-{cost_hash}",
        "receipt_hash_sha256": cost_hash,
    }
    reconciliation_components = {
        "path_replay_id": receipt["path_replay_id"],
        "cost_receipt_hash_sha256": cost_hash,
        "primary_benchmark_symbol": "SPY",
        "primary_benchmark_return_pct": primary_benchmark,
        "primary_benchmark_source_bar_hash_sha256": PRIMARY_BENCHMARK_HASH,
        "secondary_benchmark_symbol": "IWM",
        "secondary_benchmark_return_pct": secondary_benchmark,
        "secondary_benchmark_source_bar_hash_sha256": SECONDARY_BENCHMARK_HASH,
        "after_cost_return_pct": after_cost,
        "net_excess_return_pct": (
            after_cost - primary_benchmark if after_cost is not None else None
        ),
        "causal_decision_identity": canonical_causal_identity,
    }
    reconciliation_hash = _sha(
        {
            "schema_version": RECONCILIATION_SCHEMA_VERSION,
            "status": "PASSED",
            "components": reconciliation_components,
        }
    )
    reconciliation_receipt = {
        "schema_version": RECONCILIATION_SCHEMA_VERSION,
        "receipt_id": f"reconciliation-v2-{reconciliation_hash}",
        "receipt_hash_sha256": reconciliation_hash,
        "status": "PASSED",
        "components": reconciliation_components,
    }
    return_truth_body = {
        "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
        "path_replay_id": receipt["path_replay_id"],
        "path_replay_receipt_hash_sha256": receipt["replay_receipt_hash_sha256"],
        "source_artifact_hash_sha256": receipt["source_artifact_hash_sha256"],
        "source_bar_count": source_bar_count,
        "replay_binding": authenticated_binding,
        "cost_receipt_hash_sha256": cost_hash,
        "benchmark_source_bar_hash_sha256": PRIMARY_BENCHMARK_HASH,
        "secondary_benchmark_source_bar_hash_sha256": SECONDARY_BENCHMARK_HASH,
        "reconciliation_receipt_hash_sha256": reconciliation_hash,
        "after_cost_return_pct": after_cost,
        "net_excess_return_pct": (
            after_cost - primary_benchmark if after_cost is not None else None
        ),
        "causal_decision_identity": canonical_causal_identity,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "retrospective_research_eligible": return_eligible,
        "prospective_promotion_eligible": return_eligible and prospective,
        "evidence_cohort": "forward-current-v2",
        "no_lookahead": True,
        "validated_against_signal_timestamp": True,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    return_truth_hash = _sha(return_truth_body)
    outcome_identity = {
        "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
        "return_truth_hash_sha256": return_truth_hash,
        "causal_decision_identity": canonical_causal_identity,
        "replay_binding": authenticated_binding,
    }
    return {
        **receipt,
        "path_replay_receipt": receipt,
        "replay_binding": authenticated_binding,
        "outcome_id": f"outcome-v2-{_sha(outcome_identity)}",
        "outcome_status": "complete_sourced",
        "activation_status": "ACTIVATED" if entry is not None else "NOT_ACTIVATED",
        "source_bar_hash_sha256": receipt["source_artifact_hash_sha256"],
        "source_bar_count": source_bar_count,
        "exit_event": receipt["path_event"],
        "gross_return_pct": gross,
        "after_cost_return_pct": after_cost,
        "return_truth_schema_version": RETURN_TRUTH_SCHEMA_VERSION,
        "return_truth_hash_sha256": return_truth_hash,
        "cost_schema_version": COST_TRUTH_SCHEMA_VERSION,
        "cost_receipt_id": cost_receipt["receipt_id"],
        "cost_receipt_hash_sha256": cost_hash,
        "cost_receipt": cost_receipt,
        "observed_cost_model_identity": "observed-cost-v2",
        "modeled_cost_model_identity": "modeled-cost-v2",
        "cost_components": cost_components,
        "benchmark_symbol": "SPY",
        "benchmark_return_pct": primary_benchmark,
        "benchmark_source_bar_hash_sha256": PRIMARY_BENCHMARK_HASH,
        "benchmark_independent_reconciliation_status": "PASSED",
        "secondary_benchmark_symbol": "IWM",
        "secondary_benchmark_return_pct": secondary_benchmark,
        "secondary_benchmark_source_bar_hash_sha256": SECONDARY_BENCHMARK_HASH,
        "secondary_benchmark_independent_reconciliation_status": "PASSED",
        "net_excess_return_pct": (
            after_cost - primary_benchmark if after_cost is not None else None
        ),
        "independent_reconciliation_status": "PASSED",
        "reconciliation_schema_version": RECONCILIATION_SCHEMA_VERSION,
        "reconciliation_receipt_id": reconciliation_receipt["receipt_id"],
        "reconciliation_receipt_hash_sha256": reconciliation_hash,
        "reconciliation_receipt": reconciliation_receipt,
        "causal_decision_identity": canonical_causal_identity,
        "learning_eligible": return_eligible,
        "retrospective_research_eligible": return_eligible,
        "prospective_promotion_eligible": return_eligible and prospective,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "no_lookahead": True,
        "validated_against_signal_timestamp": True,
        "evidence_cohort": "forward-current-v2",
        "research_only": True,
        "broker_execution_enabled": False,
        "excursion_exact": receipt["excursion_exact"],
        "mfe_pct": mfe_pct,
        "mae_pct": mae_pct,
        "max_favorable_excursion_pct": mfe_pct,
        "max_adverse_excursion_pct": mae_pct,
    }


def canonical_ineligible_outcome(
    *,
    causal_identity: Mapping[str, object],
    market_date: str = "2026-08-03",
    case: str,
    symbol: str = "NOVA",
    replay_binding: Mapping[str, object] | None = None,
) -> dict[str, Any]:
    canonical_causal_identity = dict(causal_identity)
    bound_replay = dict(
        replay_binding
        or _fixture_replay_binding_from_causal(
            canonical_causal_identity,
            symbol=symbol,
            market_date=market_date,
        )
    )
    receipt = canonical_path_receipt(
        market_date=market_date,
        case=case,
        replay_binding=bound_replay,
    )
    if canonical_path_return_eligible(receipt):
        raise ValueError(f"case {case!r} unexpectedly has canonical return truth")
    not_triggered = receipt["path_truth_status"] == "NOT_TRIGGERED"
    source_bar_count = len(receipt["replay_input_manifest"]["bars"])
    authenticated_binding = receipt["replay_input_manifest"]["replay_binding"]
    outcome_identity = {
        "schema_version": RETURN_TRUTH_SCHEMA_VERSION,
        "path_replay_id": receipt["path_replay_id"],
        "path_replay_receipt_hash_sha256": receipt[
            "replay_receipt_hash_sha256"
        ],
        "causal_decision_identity": canonical_causal_identity,
        "replay_binding": authenticated_binding,
    }
    return {
        **receipt,
        "path_replay_receipt": receipt,
        "replay_binding": authenticated_binding,
        "outcome_id": f"outcome-v2-{_sha(outcome_identity)}",
        "outcome_status": "not_triggered" if not_triggered else "captured_ineligible",
        "activation_status": "NOT_TRIGGERED" if not_triggered else "INELIGIBLE",
        "source_bar_hash_sha256": receipt["source_artifact_hash_sha256"],
        "source_bar_count": source_bar_count,
        "exit_event": receipt["path_event"],
        "after_cost_return_pct": None,
        "net_excess_return_pct": None,
        "cost_schema_version": None,
        "cost_receipt_id": None,
        "cost_receipt_hash_sha256": None,
        "cost_receipt": None,
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
        "causal_decision_identity": canonical_causal_identity,
        "learning_eligible": not_triggered,
        "activation_label_eligible": not_triggered,
        "retrospective_research_eligible": False,
        "prospective_promotion_eligible": False,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "no_lookahead": True,
        "validated_against_signal_timestamp": True,
        "evidence_cohort": "forward-current-v2",
        "research_only": True,
        "broker_execution_enabled": False,
    }


def causal_identity_from(
    decision: Mapping[str, object],
    *,
    kind: str,
    id_key: str = "decision_id",
    time_key: str = "decision_at",
) -> dict[str, object]:
    decision_context = _decision_context_projection(decision, kind=kind)
    return {
        "kind": kind,
        "decision_id": decision[id_key],
        "decision_at": decision[time_key],
        "input_hash_sha256": decision["input_hash_sha256"],
        "source_lineage_hash_sha256": decision["source_lineage_hash_sha256"],
        "decision_context_hash_sha256": _sha(decision_context),
    }


def replay_binding_from(
    decision: Mapping[str, object],
    *,
    kind: str,
    id_key: str = "decision_id",
) -> dict[str, object]:
    causal = causal_identity_from(
        decision,
        kind=kind,
        id_key=id_key,
        time_key="selected_at" if kind == "alpha_paper_selection" else "decision_at",
    )
    if kind == "alpha_v6_shadow_decision":
        lineage = {
            key: decision[key]
            for key in (
                "decision_id",
                "scan_id",
                "source_signal_id",
                "shadow_signal_id",
            )
        }
    elif kind == "alpha_paper_selection":
        lineage = {
            key: decision[key]
            for key in ("selection_id", "scan_id", "signal_id")
        }
    else:
        raise ValueError(f"unsupported replay-binding kind: {kind}")
    return {
        "schema_version": REPLAY_BINDING_SCHEMA_VERSION,
        "subject": {
            "symbol": decision["ticker"],
            "market_date": decision["market_date"],
        },
        "origin": {
            "kind": kind,
            "id": decision[id_key],
            "lineage": lineage,
            "context_hash_sha256": causal["decision_context_hash_sha256"],
        },
    }


def _fixture_replay_binding_from_causal(
    causal: Mapping[str, object],
    *,
    symbol: str,
    market_date: str,
) -> dict[str, object]:
    kind = str(causal["kind"])
    origin_id = str(causal["decision_id"])
    if kind == "alpha_v6_shadow_decision":
        lineage = {
            "decision_id": origin_id,
            "scan_id": f"scan-{origin_id}",
            "source_signal_id": f"source-{origin_id}",
            "shadow_signal_id": f"shadow-{origin_id}",
        }
    elif kind == "alpha_paper_selection":
        suffix = (
            origin_id.removeprefix("selection-")
            if origin_id.startswith("selection-")
            else origin_id
        )
        lineage = {
            "selection_id": origin_id,
            "scan_id": f"scan-{suffix}",
            "signal_id": f"signal-{suffix}",
        }
    else:
        raise ValueError(f"unsupported replay-binding kind: {kind}")
    return {
        "schema_version": REPLAY_BINDING_SCHEMA_VERSION,
        "subject": {"symbol": symbol, "market_date": market_date},
        "origin": {
            "kind": kind,
            "id": origin_id,
            "lineage": lineage,
            "context_hash_sha256": causal["decision_context_hash_sha256"],
        },
    }


def _authenticated_path_entry_receipt(
    *,
    replay_binding: Mapping[str, object],
    effective_at: datetime,
) -> dict[str, object]:
    """Build one exact hashed already-entered receipt for return fixtures."""

    origin = replay_binding.get("origin")
    if not isinstance(origin, Mapping):
        raise ValueError("replay binding origin is required for entry receipt")
    effective = effective_at.astimezone(UTC)
    body: dict[str, object] = {
        "schema_version": ENTRY_RECEIPT_SCHEMA_VERSION,
        "entry_mode": ENTRY_MODE_ALREADY_ENTERED,
        "raw_entry_price": 10.6,
        "effective_at": effective.isoformat(),
        "source_observation_id": (
            f"fixture-entry:{replay_binding['subject']['symbol']}:{effective.isoformat()}"
        ),
        "source_bar_hash_sha256": DECISION_SOURCE_HASH,
        "source_observed_at": (effective - timedelta(minutes=1)).isoformat(),
        "source_bar_completed_at": effective.isoformat(),
        "replay_origin": {
            key: origin[key]
            for key in ("kind", "id", "lineage")
        },
    }
    digest = _sha(body)
    return {
        **body,
        "receipt_id": f"{ENTRY_RECEIPT_ID_PREFIX}{digest}",
        "receipt_hash_sha256": digest,
    }


def _decision_context_projection(
    decision: Mapping[str, object],
    *,
    kind: str,
) -> dict[str, object]:
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
    elif kind == "alpha_paper_selection":
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
        )
    else:
        raise ValueError(f"unsupported causal identity kind: {kind}")
    return {
        "kind": kind,
        "decision": {field: decision.get(field) for field in fields},
    }


def canonical_v6_decision(
    decision_id: str = "decision-current-v2",
    *,
    market_date: str = "2026-08-03",
) -> dict[str, object]:
    return {
        "decision_id": decision_id,
        "scan_id": f"scan-{decision_id}",
        "source_signal_id": f"source-{decision_id}",
        "shadow_signal_id": f"shadow-{decision_id}",
        "market_date": market_date,
        "decision_at": f"{market_date}T12:10:00+00:00",
        "ticker": "NOVA",
        "strategy_version": "dawnstrike-alphaops-v6-shadow",
        "model_version": "dawnstrike-alphaops-v6-research-suite-v2",
        "feature_schema_version": "dawnstrike-alphaops-v6-feature-schema-v1",
        "feature_hash_sha256": "5" * 64,
        "input_hash_sha256": "6" * 64,
        "source_lineage_hash_sha256": "7" * 64,
        "action": "SHADOW_TRACK",
        "decision_state": "SELECTED",
        "setup_key": "breakout",
        "regime_key": "SELECTIVE",
        "source_summary": {
            "status": "complete",
            "primary_source": "licensed-primary",
            "source_artifact_identity": f"alpha-v6-input:{decision_id}",
            "source_artifact_hash_sha256": DECISION_SOURCE_HASH,
        },
        "signal_facts": {
            "ticker": "NOVA",
            "rank": 1,
            "alpha_score": 80.0,
            "entry_watch_level": 10.5,
            "target_1": 11.0,
            "invalidation_level": 9.0,
            "can_alert": True,
            "alert_gate_status": "ALLOWED",
            "no_trade_reason": "",
            "source_confidence": 90.0,
            "source": "licensed-primary",
            "source_url": "https://example.test/nova",
        },
        "cost_model_version": "dawnstrike-alphaops-v6-conservative-cost-v1",
        "estimated_round_trip_cost_bps": 25.0,
        "point_in_time": {"all_inputs_observed_at_or_before_decision": True},
        "safety_vetoes": [],
        "score_components": {},
        "research_only": True,
        "broker_execution_enabled": False,
        "evidence_cohort": "forward-current-v2",
    }


def canonical_v6_label(
    decision: Mapping[str, object] | None = None,
    *,
    family: str = "benchmark_relative_excess_return",
    prospective: bool = True,
    value: float | None = None,
    case: str = "ordered_target",
) -> dict[str, Any]:
    bound_decision = dict(decision or canonical_v6_decision())
    outcome = canonical_return_outcome(
        market_date=str(bound_decision["market_date"]),
        symbol=str(bound_decision["ticker"]),
        case=case,
        prospective=prospective,
        net_excess_return_pct=value,
        causal_identity=causal_identity_from(
            bound_decision,
            kind="alpha_v6_shadow_decision",
        ),
        replay_binding=replay_binding_from(
            bound_decision,
            kind="alpha_v6_shadow_decision",
        ),
    )
    label_value = (
        float(outcome["net_excess_return_pct"])
        if value is None
        else value
    )
    lineage = {
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "eligibility_policy_version": outcome["eligibility_policy_version"],
        "return_truth_schema_version": outcome["return_truth_schema_version"],
        "return_truth_hash_sha256": outcome["return_truth_hash_sha256"],
        "path_replay_id": outcome["path_replay_id"],
        "replay_receipt_hash_sha256": outcome["replay_receipt_hash_sha256"],
        "source_artifact_hash_sha256": outcome["source_artifact_hash_sha256"],
        "cost_receipt_hash_sha256": outcome["cost_receipt_hash_sha256"],
        "benchmark_source_bar_hash_sha256": outcome[
            "benchmark_source_bar_hash_sha256"
        ],
        "secondary_benchmark_source_bar_hash_sha256": outcome[
            "secondary_benchmark_source_bar_hash_sha256"
        ],
        "reconciliation_receipt_hash_sha256": outcome[
            "reconciliation_receipt_hash_sha256"
        ],
        "causal_decision_identity": outcome["causal_decision_identity"],
        "retrospective_research_eligible": outcome[
            "retrospective_research_eligible"
        ],
        "prospective_promotion_eligible": outcome[
            "prospective_promotion_eligible"
        ],
        "evidence_cohort": outcome["evidence_cohort"],
        "no_lookahead": outcome["no_lookahead"],
        "validated_against_signal_timestamp": outcome[
            "validated_against_signal_timestamp"
        ],
        "research_only": outcome["research_only"],
        "broker_execution_enabled": outcome["broker_execution_enabled"],
    }
    lineage_hash = _sha(lineage)
    identity = {
        "schema_version": LABEL_SCHEMA_VERSION,
        "decision_id": bound_decision["decision_id"],
        "family": family,
        "value": label_value,
        "truth_lineage_hash_sha256": lineage_hash,
    }
    label_id = f"v6l-v2-{_sha(identity)}"
    return {
        **outcome,
        "label_id": label_id,
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "decision_id": bound_decision["decision_id"],
        "market_date": bound_decision["market_date"],
        "observed_at": outcome["exit_time"],
        "label_family": family,
        "label_value": label_value,
        "learning_eligible": True,
        "return_label_eligible": True,
        "return_truth_contract_present": True,
        "return_truth_status": "COMPLETE_CURRENT_CONTRACT",
        "source_artifact_hash_sha256": outcome["source_artifact_hash_sha256"],
        "benchmark_hash_sha256": outcome["benchmark_source_bar_hash_sha256"],
        "secondary_benchmark_hash_sha256": outcome[
            "secondary_benchmark_source_bar_hash_sha256"
        ],
        "benchmark_reconciliation_status": outcome[
            "benchmark_independent_reconciliation_status"
        ],
        "secondary_benchmark_reconciliation_status": outcome[
            "secondary_benchmark_independent_reconciliation_status"
        ],
        "truth_lineage_hash_sha256": lineage_hash,
        "label_payload_hash_sha256": _sha({**identity, "label_id": label_id}),
        "exclusion_reason": None,
    }
