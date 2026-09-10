"""R2 observation-to-V6 observational dataset adapter.

This path is deliberately separate from committed FillTruth.  It consumes an
authenticated observation manifest/receipt and raw event stream, preserves
coverage and maturity failures, and can produce research-only observational
labels without broker orders or execution evidence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from intraday_scanner.alpha.path_replay import ELIGIBILITY_POLICY_VERSION
from intraday_scanner.alpha.v6.contracts import (
    LABEL_SCHEMA_VERSION,
    canonical_hash,
    point_in_time_valid,
)
from intraday_scanner.observation.contracts import UniverseManifest, parse_utc

OBSERVATIONAL_LABEL_FAMILY = "observational_matured_return"
OBSERVATIONAL_EVIDENCE_CLASS = "observational_matured"
OBSERVATIONAL_BAR_TARGET_ID = "one_minute_bar_close_return_60m_gross"
OBSERVATIONAL_BAR_EVIDENCE_CLASS = "observational_one_minute_bar_close_return_60m_gross"
MATURITY_MINUTES = (60, 240, 600)
PATH_AVAILABILITY_FIELDS = ("gap", "close")


def build_observation_dataset(
    *,
    manifest: Mapping[str, Any] | str | Path,
    producer_receipt: Mapping[str, Any] | str | Path,
    raw_events: Sequence[Mapping[str, Any]] | str | Path,
    decisions: Sequence[Mapping[str, Any]],
    as_of: str | datetime | None = None,
    target_contract: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Build an observational packet from one immutable R2 session.

    ``READY`` means the source collection is complete and at least one
    selected decision has every required path/maturity clock.  ``PARTIAL``
    and other non-ready states retain diagnostics but produce no eligible
    labels.  The caller may use the packet for daily reporting even when the
    dataset is empty.
    """

    target_contract_value = _target_contract(target_contract)
    manifest_value = _load_json(manifest)
    receipt_value = _load_json(producer_receipt)
    events, raw_file_hash = _load_jsonl(raw_events)
    try:
        typed_manifest = UniverseManifest.from_mapping(manifest_value)
    except (TypeError, ValueError) as exc:
        return _failed_packet("INVALID_SCHEMA", f"universe_manifest:{exc}")
    identity_errors = _receipt_identity_errors(
        typed_manifest, receipt_value, events, raw_file_hash=raw_file_hash
    )
    if identity_errors:
        return _failed_packet("INVALID_SCHEMA", ";".join(identity_errors))
    expectation_status = str(
        typed_manifest.collection_expectation.get("status") or ""
    ).upper()
    receipt_status = str(receipt_value.get("status") or "").upper()
    if expectation_status != "COMPLETE" or receipt_status != "READY":
        return _base_packet(
            typed_manifest,
            receipt_value,
            status="PARTIAL" if receipt_status in {"PARTIAL", "READY"} else "FAILED_COLLECTION",
            reason="collection_not_complete",
            decisions=list(decisions),
            labels=[],
            coverage=_coverage(typed_manifest, events),
            close_identity=_close_identity(manifest_value),
            target_contract=target_contract_value,
        )
    decision_rows = [dict(row) for row in decisions]
    close_identity = _close_identity(manifest_value)
    event_rows = _validated_events(events)
    if event_rows is None:
        return _base_packet(
            typed_manifest, receipt_value, status="INVALID_SCHEMA",
            reason="raw_event_schema_invalid", decisions=decision_rows, labels=[],
            coverage=_coverage(typed_manifest, events),
            close_identity=_close_identity(manifest_value),
            target_contract=target_contract_value,
        )
    now = _timestamp(as_of) if as_of is not None else max(
        (_timestamp(row["available_at"]) for row in event_rows), default=datetime.now(UTC)
    )
    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for event in event_rows:
        by_symbol.setdefault(str(event["symbol"]).upper(), []).append(event)
    labels: list[dict[str, Any]] = []
    diagnostics: list[dict[str, Any]] = []
    entry_by_symbol = {
        entry.symbol: entry for entry in typed_manifest.entries if entry.membership == "selected"
    }
    for decision in decision_rows:
        ticker = str(decision.get("ticker") or "").upper()
        if ticker not in entry_by_symbol:
            continue
        # A decision from another market date/session is never allowed to be
        # joined to this manifest.  Keeping this check at the producer
        # boundary prevents a validly shaped artifact from becoming a
        # cross-session training row.
        decision_market_date = str(decision.get("market_date") or "")
        try:
            decision_at = (
                _timestamp(decision.get("decision_at"))
                if decision.get("decision_at")
                else None
            )
        except (TypeError, ValueError):
            diagnostics.append(
                _diagnostic(decision, "DELAYED_INELIGIBLE", "decision_timestamp_invalid")
            )
            continue
        if decision_market_date != typed_manifest.market_date or (
            decision_at is not None and decision_at.date().isoformat() != typed_manifest.market_date
        ):
            diagnostics.append(_diagnostic(decision, "AMBIGUOUS", "decision_session_mismatch"))
            continue
        membership = decision.get("universe_membership")
        if (
            not isinstance(membership, Mapping)
            or str(membership.get("universe_id") or "")
            != typed_manifest.universe_generation_id
            or str(decision.get("strategy_version") or "")
            != "dawnstrike-alphaops-v6-shadow"
        ):
            diagnostics.append(_diagnostic(decision, "AMBIGUOUS", "strategy_or_universe_identity"))
            continue
        required_inputs = set(entry_by_symbol[ticker].required_inputs)
        observed_inputs = {
            str(event.get("kind") or "").strip().lower()
            for event in by_symbol.get(ticker, [])
        }
        missing_inputs = sorted(required_inputs - observed_inputs)
        if missing_inputs:
            diagnostics.append(
                _diagnostic(decision, "MISSING_INPUT", ",".join(missing_inputs))
            )
            continue
        decision_at = _timestamp(decision.get("decision_at"))
        if decision_at is None or not point_in_time_valid(decision):
            diagnostics.append(
                _diagnostic(decision, "DELAYED_OR_INELIGIBLE", "decision_chronology")
            )
            continue
        source_events = by_symbol.get(ticker, [])
        result = _matured_observation(
            decision=decision,
            events=source_events,
            now=now,
            session_id=typed_manifest.session_id,
            close_identity=close_identity,
            target_contract=target_contract_value,
        )
        diagnostics.append(result["diagnostic"])
        if result.get("label") is not None:
            labels.append(result["label"])
    diagnostic_statuses = {str(item.get("status") or "") for item in diagnostics}
    status = "READY" if labels else (
        "WAITING_IMMATURE" if "IMMATURE" in diagnostic_statuses
        else "MISSING_INPUT" if "MISSING_INPUT" in diagnostic_statuses
        else "AMBIGUOUS" if "AMBIGUOUS" in diagnostic_statuses
        else "CENSORED" if "CENSORED" in diagnostic_statuses
        else "VALID_NO_TRADE" if not decision_rows else "DELAYED_INELIGIBLE"
    )
    return _base_packet(
        typed_manifest, receipt_value, status=status,
        reason=None if labels else "no_mature_eligible_observations",
        decisions=decision_rows, labels=labels,
        coverage=_coverage(typed_manifest, events), diagnostics=diagnostics,
        close_identity=close_identity,
        target_contract=target_contract_value,
    )


def _matured_observation(
    *, decision: Mapping[str, Any], events: Sequence[Mapping[str, Any]],
    now: datetime, session_id: str, close_identity: Mapping[str, Any] | None,
    target_contract: Mapping[str, Any] | None,
) -> dict[str, Any]:
    decision_at = _timestamp(decision.get("decision_at"))
    assert decision_at is not None
    ordered = sorted(events, key=lambda row: (_timestamp(row["event_time"]), str(row["event_id"])))
    selected: dict[str, dict[str, Any]] = {}
    if close_identity is None:
        return {"diagnostic": _diagnostic(
            decision, "CENSORED", "session_close_identity_missing"
        )}
    close_at = _timestamp(str(close_identity["close_at"]))
    for field, predicate in (
        ("gap", lambda event: _timestamp(event["event_time"]) >= decision_at),
        ("close", lambda event: _timestamp(event["event_time"]) <= close_at),
    ):
        candidates = [event for event in ordered if predicate(event)]
        if candidates:
            selected[field] = candidates[0] if field == "gap" else candidates[-1]
    missing = [field for field in PATH_AVAILABILITY_FIELDS if field not in selected]
    close_is_proxy = bool(selected.get("close", {}).get("close_proxy"))
    maturity: dict[str, str] = {}
    maturity_events: dict[str, dict[str, Any]] = {}
    for minutes in MATURITY_MINUTES:
        cutoff = decision_at + timedelta(minutes=minutes)
        if close_is_proxy and cutoff >= close_at:
            maturity[str(minutes)] = "CENSORED"
            continue
        candidates = [event for event in ordered if _timestamp(event["event_time"]) == cutoff]
        if not candidates:
            maturity[str(minutes)] = "IMMATURE"
            continue
        event = candidates[0]
        if _timestamp(event["available_at"]) > now:
            maturity[str(minutes)] = "DELAYED"
            continue
        maturity[str(minutes)] = "MATURE"
        maturity_events[str(minutes)] = event
    target_minutes = int(target_contract["horizon_minutes"]) if target_contract else None
    target_status = maturity.get(str(target_minutes)) if target_minutes else None
    if target_contract and target_status != "MATURE":
        return {"diagnostic": _diagnostic(
            decision, "IMMATURE" if target_status == "IMMATURE" else "DELAYED_INELIGIBLE",
            f"target_{target_minutes}m_{str(target_status or 'MISSING').lower()}",
        )}
    if missing or (
        not target_contract
        and not all(value in {"MATURE", "CENSORED"} for value in maturity.values())
    ):
        return {"diagnostic": _diagnostic(
            decision,
            "IMMATURE"
            if not missing and "IMMATURE" in maturity.values()
            else "DELAYED_INELIGIBLE",
            ",".join(missing)
            or ",".join(
                f"maturity_{key}_{value}"
                for key, value in maturity.items()
                if value != "MATURE"
            ),
        )}
    gap = selected["gap"]
    close = selected["close"]
    if _timestamp(close["event_time"]) != close_at and not close_is_proxy:
        return {"diagnostic": _diagnostic(
            decision, "CENSORED", "session_close_observation_missing"
        )}
    target_event = maturity_events.get(str(target_minutes)) if target_minutes else close
    open_value = _number((gap.get("payload") or {}).get("o"))
    target_value = _number((target_event.get("payload") or {}).get("c"))
    if open_value is None or target_value is None or open_value <= 0:
        return {"diagnostic": _diagnostic(decision, "AMBIGUOUS", "path_value_missing")}
    label_value = (target_value / open_value - 1.0) * 100.0
    evidence_class = (
        str(target_contract["evidence_class"])
        if target_contract
        else OBSERVATIONAL_EVIDENCE_CLASS
    )
    target_id = str(target_contract["target_id"]) if target_contract else None
    label_identity = {
        "decision_id": decision.get("decision_id"),
        "strategy_id": decision.get("strategy_id") or "alphaops_v6",
        "strategy_version": decision.get("strategy_version"),
        "family": OBSERVATIONAL_LABEL_FAMILY,
        "target_id": target_id,
        "value": label_value,
        "session_id": session_id,
        "maturity": maturity,
    }
    label = {
        **dict(decision),
        "decision_id": decision.get("decision_id"),
        "label_family": OBSERVATIONAL_LABEL_FAMILY,
        "label_value": round(label_value, 10),
        "learning_eligible": True,
        "return_label_eligible": True,
        "label_schema_version": LABEL_SCHEMA_VERSION,
        "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
        "evidence_class": evidence_class,
        "fill_truth_status": "not_applicable_observation",
        "fill_truth_bound": False,
        "research_only": True,
        "broker_execution_enabled": False,
        "return_basis": (
            str(target_contract["return_basis"])
            if target_contract
            else "observed_path_close_proxy_vs_gap_open"
            if close_is_proxy
            else "observed_path_close_vs_gap_open"
        ),
        "close_observation_semantics": (
            str(target_contract["price_basis"])
            if target_contract
            else "one_minute_bar_close_proxy"
            if close_is_proxy
            else "session_close_event"
        ),
        "horizon_unit": "minutes",
        "observational_target_id": target_id,
        "observational_evidence_class": evidence_class,
        "target_contract": dict(target_contract or {}),
        "target_horizon_minutes": target_minutes,
        "target_price_time": target_event["event_time"],
        "target_price_offset_minutes": (
            _timestamp(target_event["event_time"]) - decision_at
        ).total_seconds() / 60.0,
        "return_units": str(target_contract["units"]) if target_contract else "percent",
        "return_denominator": "gap_open_price",
        "gross_return": True,
        "costs_excluded": True,
        "cost_availability_status": "excluded_by_observational_target",
        "close_identity": dict(close_identity),
        "observed_path": {"gap": gap, "target": target_event, "close": close},
        "maturity_minutes": list(MATURITY_MINUTES),
        "maturity_status": maturity,
        "maturity_at": {
            key: event["event_time"] for key, event in maturity_events.items()
        },
        "label_available_at": max(
            _timestamp(gap["available_at"]),
            _timestamp(close["available_at"]),
            _timestamp(target_event["available_at"]),
        ).isoformat(),
        "source_artifact_hash_sha256": gap.get("source_artifact_hash_sha256"),
        "source_artifact_hashes": sorted({
            str(gap.get("source_artifact_hash_sha256") or ""),
            str(close.get("source_artifact_hash_sha256") or ""),
            str(target_event.get("source_artifact_hash_sha256") or ""),
        } - {""}),
    }
    label["truth_lineage_hash_sha256"] = canonical_hash({
        "manifest_sha256": decision.get("universe_manifest_sha256"),
        "event_ids": [
            gap["event_id"],
            close["event_id"],
            *[event["event_id"] for event in maturity_events.values()],
        ],
        "evidence_class": evidence_class,
    })
    label["label_id"] = "v6o-" + canonical_hash(label_identity)[:28]
    label["label_payload_hash_sha256"] = canonical_hash({
        "label_id": label["label_id"], "label_identity": label_identity,
        "truth_lineage_hash_sha256": label["truth_lineage_hash_sha256"],
    })
    return {"label": label, "diagnostic": _diagnostic(decision, "MATURE", "eligible_observation")}


def _base_packet(manifest: UniverseManifest, receipt: Mapping[str, Any], *, status: str,
                 reason: str | None, decisions: list[dict[str, Any]], labels: list[dict[str, Any]],
                 coverage: list[dict[str, Any]], diagnostics: list[dict[str, Any]] | None = None,
                 close_identity: Mapping[str, Any] | None = None,
                 target_contract: Mapping[str, Any] | None = None) -> dict[str, Any]:
    return {
        "schema_version": "dawnstrike.v6.observational_dataset.v1",
        "status": status,
        "reason": reason,
        "session_id": manifest.session_id,
        "market_date": manifest.market_date,
        "universe_generation_id": manifest.universe_generation_id,
        "universe_manifest_sha256": manifest.manifest_sha256,
        "source_config_sha256": manifest.source_config_sha256,
        "capture_receipt_sha256": receipt.get("capture_receipt_sha256"),
        "producer_receipt_sha256": canonical_hash(receipt),
        "strategy_identity": {
            "strategy_id": "alphaops_v6",
            "strategy_version": "dawnstrike-alphaops-v6-shadow",
        },
        "decisions": decisions,
        "labels": labels,
        "row_count": len(labels),
        "coverage": coverage,
        "diagnostics": diagnostics or [],
        "maturity_minutes": list(MATURITY_MINUTES),
        "path_availability_fields": list(PATH_AVAILABILITY_FIELDS),
        "horizon_unit": "minutes",
        "close_identity": dict(close_identity or {}),
        "target_contract": dict(target_contract or {}),
        "evidence_class": (
            str(target_contract["evidence_class"])
            if target_contract
            else OBSERVATIONAL_EVIDENCE_CLASS
        ),
        "research_only": True,
        "broker_execution_enabled": False,
        "training_identity": {
            "dataset_schema_version": "dawnstrike.v6.observational_dataset.v1",
            "eligibility_policy_version": ELIGIBILITY_POLICY_VERSION,
            "dataset_hash_sha256": canonical_hash({"labels": labels, "decisions": decisions}),
        },
    }


def _target_contract(value: Mapping[str, Any] | None) -> dict[str, Any] | None:
    if value is None:
        return None
    contract = dict(value)
    required = {
        "target_id": OBSERVATIONAL_BAR_TARGET_ID,
        "horizon_minutes": 60,
        "units": "percent",
        "price_basis": "one_minute_bar_close_proxy",
        "return_basis": OBSERVATIONAL_BAR_TARGET_ID,
        "evidence_class": OBSERVATIONAL_BAR_EVIDENCE_CLASS,
    }
    if any(contract.get(key) != expected for key, expected in required.items()):
        raise ValueError("unsupported observational target contract")
    contract.update(
        {
            "denominator": "gap_open_price",
            "gross": True,
            "costs_excluded": True,
            "isolation": "research_only_observational_target",
        }
    )
    return contract


def _failed_packet(status: str, reason: str) -> dict[str, Any]:
    return {"schema_version": "dawnstrike.v6.observational_dataset.v1", "status": status,
            "reason": reason, "row_count": 0, "labels": [], "decisions": [],
            "research_only": True, "broker_execution_enabled": False}


def _close_identity(value: Mapping[str, Any]) -> dict[str, Any] | None:
    raw = value.get("session_close_identity")
    if not isinstance(raw, Mapping):
        return None
    close_at = str(raw.get("close_at") or "")
    try:
        parse_utc(close_at, label="session_close_identity.close_at")
    except ValueError:
        return None
    if not str(raw.get("calendar_version") or "").strip():
        return None
    if not isinstance(raw.get("early_close"), bool):
        return None
    return {
        "close_at": close_at,
        "early_close": raw["early_close"],
        "calendar_version": str(raw["calendar_version"]),
    }


def _coverage(
    manifest: UniverseManifest, events: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    counts: dict[tuple[str, str], int] = {}
    for event in events:
        key = (str(event.get("scope") or ""), str(event.get("symbol") or "").upper())
        counts[key] = counts.get(key, 0) + 1
    return [{**entry.as_dict(), "observation_count": counts.get((entry.scope, entry.symbol), 0),
             "status": "OBSERVED" if counts.get((entry.scope, entry.symbol), 0) else (
                 "MISSING_DEPENDENCY" if entry.membership == "missing_input"
                 and "producer_missing" in entry.reason_codes
                 else "MISSING_INPUT" if entry.membership == "missing_input"
                 else "MISSING_OBSERVATION")}
            for entry in manifest.entries]


def _receipt_identity_errors(
    manifest: UniverseManifest,
    receipt: Mapping[str, Any],
    events: Sequence[Mapping[str, Any]],
    *,
    raw_file_hash: str | None,
) -> list[str]:
    errors = []
    for field, expected in (
        ("session_id", manifest.session_id),
        ("manifest_sha256", manifest.manifest_sha256),
        ("source_config_sha256", manifest.source_config_sha256),
    ):
        actual = receipt.get(field)
        if field == "manifest_sha256" and not actual:
            actual = receipt.get("universe_manifest_sha256")
        if actual != expected:
            errors.append(f"receipt_{field}_mismatch")
    raw_hash = receipt.get("raw_events_sha256")
    if raw_hash and not _sha256(raw_hash):
        errors.append("receipt_raw_events_hash_invalid")
    if raw_file_hash and raw_hash != raw_file_hash:
        errors.append("receipt_raw_events_hash_mismatch")
    return errors


def _validated_events(events: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]] | None:
    output = []
    for row in events:
        try:
            for field in (
                "event_id",
                "session_id",
                "scope",
                "symbol",
                "event_time",
                "available_at",
            ):
                if not str(row.get(field) or "").strip():
                    return None
            event_time = parse_utc(str(row["event_time"]), label="event_time")
            available_at = parse_utc(str(row["available_at"]), label="available_at")
            if available_at < event_time or not isinstance(row.get("payload"), Mapping):
                return None
            # Keep the authenticated source event JSON serializable.  Parsed
            # timestamps are validation-only and must never leak into the
            # persisted observed path as datetime objects.
            output.append({**dict(row), "symbol": str(row["symbol"]).upper()})
        except (TypeError, ValueError):
            return None
    return output


def _diagnostic(
    decision: Mapping[str, Any], status: str, reason: str
) -> dict[str, Any]:
    return {
        "decision_id": decision.get("decision_id"),
        "ticker": decision.get("ticker"),
        "status": status,
        "reason": reason,
    }


def _load_json(value: Mapping[str, Any] | str | Path) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return dict(value)
    return json.loads(Path(value).read_text(encoding="utf-8"))


def _load_jsonl(
    value: Sequence[Mapping[str, Any]] | str | Path,
) -> tuple[list[dict[str, Any]], str | None]:
    if not isinstance(value, (str, Path)):
        return [dict(row) for row in value], None
    raw = Path(value).read_bytes()
    return (
        [json.loads(line) for line in raw.decode("utf-8").splitlines() if line.strip()],
        hashlib.sha256(raw).hexdigest(),
    )


def _timestamp(value: Any) -> datetime:
    return parse_utc(str(value), label="timestamp")


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed == parsed and abs(parsed) != float("inf") else None


def _sha256(value: Any) -> bool:
    return len(str(value or "")) == 64 and all(
        char in "0123456789abcdef" for char in str(value).lower()
    )


__all__ = [
    "OBSERVATIONAL_EVIDENCE_CLASS",
    "OBSERVATIONAL_LABEL_FAMILY",
    "build_observation_dataset",
]
