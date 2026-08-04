"""Immutable AlphaOps official-cohort membership and delivery verification."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from intraday_scanner.storage.sqlite_store import SQLiteScanStore

OFFICIAL_COHORT = "official_telegram"
_MEMBERSHIP_FIELDS = (
    "selection_id",
    "scan_id",
    "signal_id",
    "ticker",
    "rank",
    "strategy_id",
    "strategy_version",
    "cohort",
    "decision",
    "selected_at",
    "event_key",
    "body_sha256",
)
_DELIVERED_STATUSES = frozenset({"delivered", "delivered_legacy"})


@dataclass(frozen=True)
class OfficialCohortValidation:
    cohort: dict[str, Any] | None
    selections: tuple[dict[str, Any], ...]
    errors: tuple[str, ...]
    recovered: bool = False


def membership_sha256(selections: list[dict[str, Any]]) -> str:
    projections = sorted(
        (
            _membership_projection(row)
            for row in selections
        ),
        key=lambda row: str(row.get("selection_id") or ""),
    )
    return hashlib.sha256(
        json.dumps(
            projections,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()


def _membership_projection(row: dict[str, Any]) -> dict[str, Any]:
    projection: dict[str, Any] = {}
    for field in _MEMBERSHIP_FIELDS:
        value = row.get(field)
        if field == "rank":
            projection[field] = (
                None if value is None or value == "" else int(str(value))
            )
        else:
            projection[field] = str(value or "")
    return projection


def build_official_cohort_row(
    *,
    market_date: str,
    strategy_id: str,
    strategy_version: str,
    scan_id: str,
    event_key: str,
    body_sha256: str,
    claimed_at: str,
    selections: list[dict[str, Any]],
) -> dict[str, Any]:
    if not selections:
        raise ValueError("official cohort requires at least one selection")
    selected_date = market_date[:10]
    identity = (
        f"{selected_date}|{strategy_id}|{strategy_version}|{OFFICIAL_COHORT}"
    )
    membership_hash = membership_sha256(selections)
    row: dict[str, Any] = {
        "official_cohort_id": (
            "official-cohort:"
            + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]
        ),
        "market_date": selected_date,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cohort": OFFICIAL_COHORT,
        "scan_id": scan_id,
        "event_key": event_key,
        "body_sha256": body_sha256,
        "membership_sha256": membership_hash,
        "claimed_at": claimed_at,
    }
    row["payload_json"] = {
        **row,
        "selection_ids": sorted(str(item["selection_id"]) for item in selections),
        "signal_ids": sorted(str(item["signal_id"]) for item in selections),
        "member_count": len(selections),
        "missing_truth_is_zero": False,
        "research_only": True,
        "broker_execution_enabled": False,
    }
    return row


def validate_or_recover_official_cohort(
    store: SQLiteScanStore,
    *,
    market_date: str,
    strategy_id: str,
    strategy_version: str,
    persist_recovery: bool = True,
) -> OfficialCohortValidation:
    selected_date = market_date[:10]
    cohort = store.load_official_strategy_cohort(
        market_date=selected_date,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
        cohort=OFFICIAL_COHORT,
    )
    recovered = False
    if cohort is None:
        candidate_rows = [
            row
            for row in store.load_signal_selections(
                strategy_id=strategy_id,
                cohort=OFFICIAL_COHORT,
                limit=50_000,
            )
            if str(row.get("selected_at") or "")[:10] == selected_date
            and str(row.get("strategy_version") or "") == strategy_version
        ]
        groups = {
            (
                str(row.get("scan_id") or ""),
                str(row.get("event_key") or ""),
                str(row.get("body_sha256") or ""),
            )
            for row in candidate_rows
        }
        if not candidate_rows or len(groups) != 1:
            return OfficialCohortValidation(
                cohort=None,
                selections=(),
                errors=("frozen official cohort is absent or ambiguous",),
            )
        scan_id, event_key, body_sha256 = next(iter(groups))
        delivery_errors = _delivery_errors(store, candidate_rows)
        if delivery_errors:
            return OfficialCohortValidation(
                cohort=None,
                selections=tuple(candidate_rows),
                errors=tuple(delivery_errors),
            )
        candidate = build_official_cohort_row(
            market_date=selected_date,
            strategy_id=strategy_id,
            strategy_version=strategy_version,
            scan_id=scan_id,
            event_key=event_key,
            body_sha256=body_sha256,
            claimed_at=min(str(row.get("selected_at") or "") for row in candidate_rows),
            selections=candidate_rows,
        )
        if persist_recovery:
            store.persist_official_signal_cohort(candidate, candidate_rows)
            cohort = store.load_official_strategy_cohort(
                market_date=selected_date,
                strategy_id=strategy_id,
                strategy_version=strategy_version,
                cohort=OFFICIAL_COHORT,
            )
            recovered = True
        else:
            cohort = candidate
    if cohort is None:
        return OfficialCohortValidation(
            cohort=None,
            selections=(),
            errors=("frozen official cohort is unavailable",),
        )
    selections = store.load_signal_selections(
        scan_id=str(cohort.get("scan_id") or ""),
        event_key=str(cohort.get("event_key") or ""),
        strategy_id=strategy_id,
        cohort=OFFICIAL_COHORT,
        limit=50_000,
    )
    errors = _cohort_errors(
        cohort,
        selections,
        market_date=selected_date,
        strategy_id=strategy_id,
        strategy_version=strategy_version,
    )
    errors.extend(_delivery_errors(store, selections))
    return OfficialCohortValidation(
        cohort=cohort,
        selections=tuple(selections),
        errors=tuple(dict.fromkeys(errors)),
        recovered=recovered,
    )


def _cohort_errors(
    cohort: dict[str, Any],
    selections: list[dict[str, Any]],
    *,
    market_date: str,
    strategy_id: str,
    strategy_version: str,
) -> list[str]:
    errors: list[str] = []
    expected = {
        "market_date": market_date,
        "strategy_id": strategy_id,
        "strategy_version": strategy_version,
        "cohort": OFFICIAL_COHORT,
    }
    for field, value in expected.items():
        if str(cohort.get(field) or "") != value:
            errors.append(f"official cohort {field} mismatch")
    if not selections:
        errors.append("official cohort has no exact selection members")
        return errors
    for row in selections:
        if str(row.get("selected_at") or "")[:10] != market_date:
            errors.append("official selection market_date mismatch")
        for field in (
            "scan_id",
            "event_key",
            "body_sha256",
            "strategy_id",
            "strategy_version",
            "cohort",
        ):
            if str(row.get(field) or "") != str(cohort.get(field) or ""):
                errors.append(f"official selection {field} mismatch")
    if membership_sha256(selections) != str(cohort.get("membership_sha256") or ""):
        errors.append("official cohort membership_sha256 mismatch")
    return errors


def _delivery_errors(
    store: SQLiteScanStore,
    selections: list[dict[str, Any]],
) -> list[str]:
    if not selections:
        return ["official cohort has no delivered selection members"]
    selection_ids = {str(row.get("selection_id") or "") for row in selections}
    deliveries = [
        row
        for row in store.load_notification_deliveries(limit=50_000)
        if str(row.get("selection_id") or "") in selection_ids
        and str(row.get("channel") or "").lower() == "telegram"
        and str(row.get("delivery_status") or "").lower() in _DELIVERED_STATUSES
    ]
    by_selection = {
        str(row.get("selection_id") or ""): row
        for row in deliveries
    }
    errors: list[str] = []
    for selection in selections:
        selection_id = str(selection.get("selection_id") or "")
        delivery = by_selection.get(selection_id)
        if delivery is None:
            errors.append(f"official selection {selection_id} lacks Telegram delivery proof")
            continue
        for field in (
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
            if str(delivery.get(field) or "") != str(selection.get(field) or ""):
                errors.append(f"official delivery {selection_id} {field} mismatch")
    return errors


__all__ = [
    "OFFICIAL_COHORT",
    "OfficialCohortValidation",
    "build_official_cohort_row",
    "membership_sha256",
    "validate_or_recover_official_cohort",
]
